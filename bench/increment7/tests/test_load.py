import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import load


class LoadGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hits = 0
        app = web.Application()

        async def echo(_request):
            self.hits += 1
            return web.Response(body=b"abc")

        async def fail(_request):
            self.hits += 1
            return web.Response(status=503, body=b"unavailable")

        async def slow(_request):
            self.hits += 1
            await asyncio.sleep(0.3)
            return web.Response(body=b"slow")

        async def sse(_request):
            self.hits += 1
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(_request)
            for number in range(5):
                await response.write(f"data: {number}\n\n".encode())
            await response.write_eof()
            return response

        async def websocket_echo(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                if message.type.name == "TEXT":
                    await ws.send_str(message.data)
            return ws

        app.router.add_get("/echo", echo)
        app.router.add_get("/fail", fail)
        app.router.add_get("/slow", slow)
        app.router.add_get("/sse", sse)
        app.router.add_get("/ws", websocket_echo)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.runner = runner

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def invoke(self, route, *options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        raw_path = root / "results.jsonl"
        summary_path = root / "summary.json"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "load.py"),
            "--url",
            f"http://127.0.0.1:{self.port}{route}",
            "--output",
            str(raw_path),
            "--summary",
            str(summary_path),
            *map(str, options),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        self.assertEqual(process.returncode, 0, (stdout.decode(), stderr.decode()))
        rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
        summary = json.loads(summary_path.read_text())
        return rows, summary

    async def test_open_loop_counts_latency_and_connection_reuse(self):
        rows, summary = await self.invoke(
            "/echo",
            "--rate", 20,
            "--seconds", 0.5,
            "--concurrency", 2,
            "--queue", 2,
            "--expect-bytes", 3,
        )
        self.assertEqual(summary["offered"], 10)
        self.assertEqual(len(rows), 10)
        self.assertEqual(summary["completed"], 10)
        self.assertEqual(summary["2xx"], 10)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["drops"], 0)
        self.assertEqual(summary["rejections"], 0)
        self.assertGreater(summary["connection_new"], 0)
        self.assertGreater(summary["connection_reused"], 0)
        offsets = [row["scheduled_offset_s"] for row in rows]
        self.assertAlmostEqual(offsets[1] - offsets[0], 1 / 20)
        self.assertIsNotNone(summary["percentiles_ms"]["scheduled_latency"]["success"]["p99"])
        self.assertTrue(all(row["outcome"] == "success" for row in rows))

    async def test_http_failures_are_not_counted_as_success(self):
        rows, summary = await self.invoke(
            "/fail", "--rate", 10, "--seconds", 0.2, "--concurrency", 1
        )
        self.assertEqual(summary["offered"], 2)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["2xx"], 0)
        self.assertEqual(summary["http_errors"], 2)
        self.assertEqual(summary["errors"], 0)
        self.assertTrue(all(row["outcome"] == "http_error" and row["status"] == 503 for row in rows))

    async def test_full_bounded_queue_rejects_offers_without_synthetic_responses(self):
        rows, summary = await self.invoke(
            "/slow",
            "--rate", 40,
            "--seconds", 0.2,
            "--concurrency", 1,
            "--queue", 1,
            "--timeout", 1,
        )
        self.assertEqual(summary["offered"], 8)
        self.assertEqual(len(rows), 8)
        self.assertGreater(summary["rejections"], 0)
        self.assertEqual(summary["drops"], 0)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["successful_responses"], 2)
        self.assertEqual(summary["completed_by_offer_end"], 0)
        self.assertEqual(summary["successful_responses_by_offer_end"], 0)
        self.assertEqual(sum(row["outcome"] == "rejected" for row in rows), summary["rejections"])

    async def test_sse_first_event_and_five_event_completion(self):
        rows, summary = await self.invoke(
            "/sse",
            "--rate", 5,
            "--seconds", 0.2,
            "--concurrency", 1,
            "--mode", "sse",
            "--sse-events", 5,
        )
        self.assertEqual(summary["offered"], 1)
        self.assertEqual(summary["successful_responses"], 1)
        self.assertEqual(rows[0]["sse_events"], 5)
        self.assertGreaterEqual(rows[0]["first_event_ms"], 0)

    async def test_warmup_gate_preserves_session_and_excludes_preload_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, warmup_done, resume = (root / name for name in ("ready", "warm", "resume"))
            raw_path, summary_path = root / "rows.jsonl", root / "summary.json"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "load.py"),
                "--url", f"http://127.0.0.1:{self.port}/echo",
                "--output", str(raw_path),
                "--summary", str(summary_path),
                "--rate", "10",
                "--seconds", "0.2",
                "--concurrency", "1",
                "--queue", "1",
                "--warmup", "0.2",
                "--ready-file", str(ready),
                "--warmup-done-file", str(warmup_done),
                "--resume-file", str(resume),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            deadline = asyncio.get_running_loop().time() + 5
            while not warmup_done.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(ready.exists())
            self.assertTrue(warmup_done.exists())
            warmup_hits = self.hits
            await asyncio.sleep(0.05)
            self.assertEqual(self.hits, warmup_hits, "measurement started before resume marker")
            resume.write_text("go\n")
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            summary = json.loads(summary_path.read_text())
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            self.assertEqual(summary["warmup_requests_excluded"], 2)
            self.assertAlmostEqual(summary["warmup_seconds"], 0.2, delta=0.08)
            self.assertEqual(summary["offered"], 2)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["outcome"] == "success" for row in rows))

    async def test_cold_warmup_is_skipped_and_summary_is_complete(self):
        _rows, summary = await self.invoke(
            "/echo", "--rate", 10, "--seconds", 0.2, "--concurrency", 2,
            "--queue", 1, "--cold", "--warmup", 0.2,
        )
        self.assertEqual(summary["offered"], 2)
        self.assertEqual(summary["warmup_requests_offered"], 0)
        self.assertEqual(summary["warmup_requests_excluded"], 0)
        self.assertTrue(summary["warmup_skipped_cold"])

    async def test_websocket_handshake_is_recorded_without_unsupported_trace_arg(self):
        rows, summary = await self.invoke(
            "/ws", "--rate", 5, "--seconds", 0.2, "--concurrency", 2,
            "--mode", "ws",
        )
        self.assertEqual(summary["offered"], 1)
        self.assertEqual(summary["successful_responses"], 1)
        self.assertEqual(summary["2xx"], 0)
        self.assertEqual(rows[0]["status"], 101)
        self.assertTrue(rows[0]["ws_echo_ok"])
        self.assertGreaterEqual(rows[0]["ws_echo_ms"], 0)
        self.assertTrue(rows[0]["connection_attempted"])

    async def test_measurement_marker_release_gates_output_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "measurement.done"
            release = root / "measurement.release"
            observed = []

            def observe_write(_args, _rows, _summary):
                observed.append(marker.exists())

            with patch.object(load, "write_outputs", side_effect=observe_write):
                task = asyncio.create_task(load.async_main([
                    "--url", f"http://127.0.0.1:{self.port}/echo",
                    "--rate", "5", "--seconds", "0.2", "--concurrency", "1",
                    "--queue", "1", "--output", str(root / "rows.jsonl"),
                    "--measurement-done-file", str(marker),
                    "--measurement-release-file", str(release),
                ]))
                deadline = asyncio.get_running_loop().time() + 5
                while not marker.exists() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(marker.exists())
                self.assertFalse(observed, "result serialization started before runner release")
                release.write_text("snapshot-complete\n")
                result = await asyncio.wait_for(task, timeout=5)
            self.assertEqual(result, 0)
            self.assertTrue(marker.exists())
            self.assertEqual(observed, [True])

    def test_nan_rate_is_rejected(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "load.py"),
             "--url", "http://127.0.0.1/", "--rate", "nan", "--output", "/tmp/no-output.jsonl"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("finite", result.stderr)

    def test_nearest_rank_quantiles(self):
        self.assertEqual(load.percentiles([1.0, 2.0, 3.0, 4.0]), {"p50": 2.0, "p95": 4.0, "p99": 4.0})
        self.assertEqual(load.percentiles([]), {"p50": None, "p95": None, "p99": None})


if __name__ == "__main__":
    unittest.main()
