#!/usr/bin/env python3
"""Bounded open-loop HTTP/1.1 load generator for the increment 7 lab."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import contextvars
import json
import math
import platform
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp


SCENARIOS = (
    "normal",
    "delayed",
    "saturation",
    "tls-warm",
    "tls-cold",
    "sse",
    "slow-reader",
    "ws",
)
WS_PROBE_TEXT = "cauce-increment7-probe"
ACTIVE_TRACE_ROW: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "active_trace_row", default=None
)


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (1-indexed), stable for small sample sets."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def percentiles(values: list[float]) -> dict[str, float | None]:
    return {f"p{int(q * 100)}": percentile(values, q) for q in (0.50, 0.95, 0.99)}


def scenario_url(url: str, scenario: str | None) -> str:
    """Map lab scenarios to fixture routes while preserving scheme/authority."""
    if scenario is None:
        return url
    routes = {
        "normal": ("/echo", []),
        "delayed": ("/delay", [("ms", "250")]),
        "saturation": ("/hold", [("ms", "1500")]),
        "tls-warm": ("/echo", []),
        "tls-cold": ("/echo", [("cold", "1")]),
        "sse": ("/sse", []),
        "slow-reader": ("/blob", [("bytes", "1048576")]),
        "ws": ("/ws", []),
    }
    parts = urlsplit(url)
    route, query = routes[scenario]
    return urlunsplit((parts.scheme, parts.netloc, route, urlencode(query), ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url_pos", nargs="?", help="target URL (or pass --url)")
    parser.add_argument("--url", dest="url_option", help="target URL")
    parser.add_argument("--host", help="HTTP Host and TLS server name override")
    parser.add_argument("--rate", type=float, default=100.0, help="offered arrivals per second")
    parser.add_argument("--seconds", "--duration", dest="seconds", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=16, help="maximum active requests")
    parser.add_argument("--queue", type=int, help="bounded pending-request queue (default: 2x concurrency)")
    parser.add_argument("--timeout", type=float, default=5.0, help="total request timeout in seconds")
    parser.add_argument("--output", required=True, help="raw JSONL output path")
    parser.add_argument("--summary", help="summary JSON path (default: <output>.summary.json)")
    parser.add_argument("--ca-file", help="CA bundle for HTTPS verification")
    parser.add_argument("--cold", action="store_true", help="new client session and TLS context per request")
    parser.add_argument("--expect-bytes", type=int, help="expected response body size")
    parser.add_argument("--read-delay-ms", type=float, default=0.0, help="pause between body chunks")
    parser.add_argument("--mode", choices=("http", "sse", "ws"), help="request mode override")
    parser.add_argument("--scenario", choices=SCENARIOS, help="increment 7 fixture scenario")
    parser.add_argument("--warmup", type=float, default=0.0, help="unreported warmup duration")
    parser.add_argument("--ready-file", help="write when the client session is initialized")
    parser.add_argument("--warmup-done-file", help="write after warmup, before waiting to measure")
    parser.add_argument("--measurement-done-file", help="write after offered work drains, before writing result files")
    parser.add_argument("--measurement-release-file", help="wait for this marker after measurement before result serialization")
    parser.add_argument("--resume-file", help="wait for this marker before starting offered arrivals")
    parser.add_argument("--resume-timeout", type=float, default=120.0,
                        help="maximum wait for --resume-file")
    parser.add_argument("--seed", type=int, default=0, help="recorded campaign seed")
    parser.add_argument("--sse-events", type=int, help="complete SSE events before a sample completes")
    parser.add_argument("--max-scheduler-lag-ms", type=float, default=250.0,
                        help="arrivals later than this are recorded as generator drops")
    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    args.url = args.url_option or args.url_pos
    if not args.url:
        parser.error("provide a URL as the first argument or with --url")
    numeric = (args.rate, args.seconds, args.timeout, args.warmup, args.read_delay_ms,
               args.max_scheduler_lag_ms, args.resume_timeout)
    if not all(math.isfinite(value) for value in numeric):
        parser.error("rate, durations, and thresholds must be finite numbers")
    if not (0 < args.rate <= 100_000 and 0 < args.seconds <= 3600):
        parser.error("rate must be in (0, 100000] and seconds in (0, 3600]")
    if math.floor(args.rate * args.seconds) < 1:
        parser.error("rate * seconds must offer at least one request")
    if args.rate * args.seconds > 100_000:
        parser.error("total offered arrivals are capped at 100000 to bound result memory")
    if not (1 <= args.concurrency <= 1024):
        parser.error("concurrency must be in [1, 1024]")
    if args.queue is None:
        args.queue = args.concurrency * 2
    if not (1 <= args.queue <= 65_536):
        parser.error("queue must be in [1, 65536]")
    if not (0 < args.timeout <= 3600):
        parser.error("timeout must be in (0, 3600]")
    if args.warmup < 0 or args.warmup > 3600:
        parser.error("warmup must be in [0, 3600]")
    if args.rate * args.warmup > 100_000:
        parser.error("warmup arrivals are capped at 100000 to bound client work")
    if args.resume_timeout <= 0:
        parser.error("resume-timeout must be positive")
    if args.expect_bytes is not None and args.expect_bytes < 0:
        parser.error("expect-bytes cannot be negative")
    if args.read_delay_ms < 0 or args.max_scheduler_lag_ms < 0:
        parser.error("read delay and max scheduler lag cannot be negative")
    if args.sse_events is not None and args.sse_events < 1:
        parser.error("sse-events must be at least one")
    if args.scenario in ("tls-warm", "tls-cold"):
        args.cold = args.cold or args.scenario == "tls-cold"
    if args.scenario == "slow-reader" and args.read_delay_ms == 0:
        args.read_delay_ms = 2.0
    if args.scenario == "slow-reader" and args.expect_bytes is None:
        args.expect_bytes = 1_048_576
    if args.scenario == "sse" and args.sse_events is None:
        args.sse_events = 5
    if args.scenario == "tls-cold":
        args.cold = True
    if args.scenario is not None:
        args.url = scenario_url(args.url, args.scenario)
    parts = urlsplit(args.url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        parser.error("URL must be an absolute http:// or https:// URL")
    if args.scenario in ("tls-warm", "tls-cold") and parts.scheme != "https":
        parser.error("tls-warm and tls-cold scenarios require an https:// URL")
    args.mode = args.mode or ("sse" if args.scenario == "sse" else "ws" if args.scenario == "ws" else "http")
    if args.mode == "sse" and args.sse_events is None:
        args.sse_events = 1
    if args.mode == "ws" and args.expect_bytes is not None:
        parser.error("expect-bytes does not apply to WebSocket handshakes")
    args.host_header = args.host
    args.server_name = args.host.split(":", 1)[0] if args.host else parts.hostname


def make_ssl_context(args: argparse.Namespace) -> ssl.SSLContext | bool:
    if urlsplit(args.url).scheme != "https":
        return True
    return ssl.create_default_context(cafile=args.ca_file)


def make_trace_config() -> aiohttp.TraceConfig:
    trace = aiohttp.TraceConfig()

    async def connection_create_start(_session, ctx, _params):
        row = getattr(ctx, "trace_request_ctx", None) or ACTIVE_TRACE_ROW.get()
        if row is not None:
            row["connection_attempted"] = True

    async def connection_create_end(_session, ctx, _params):
        row = getattr(ctx, "trace_request_ctx", None) or ACTIVE_TRACE_ROW.get()
        if row is not None:
            row["connection_new"] = True

    async def connection_reused(_session, ctx, _params):
        row = getattr(ctx, "trace_request_ctx", None) or ACTIVE_TRACE_ROW.get()
        if row is not None:
            row["connection_reused"] = True

    trace.on_connection_create_start.append(connection_create_start)
    trace.on_connection_create_end.append(connection_create_end)
    trace.on_connection_reuseconn.append(connection_reused)
    return trace


def make_session(args: argparse.Namespace) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        limit=args.concurrency,
        limit_per_host=args.concurrency,
        force_close=args.cold,
        ssl=make_ssl_context(args),
    )
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trace_configs=[make_trace_config()],
        auto_decompress=False,
    )


def request_headers(args: argparse.Namespace) -> dict[str, str]:
    headers = {"User-Agent": "cauce-increment7-load/1.0", "Accept-Encoding": "identity"}
    if args.host_header:
        headers["Host"] = args.host_header
    return headers


async def perform_warmup(args: argparse.Namespace, session: aiohttp.ClientSession) -> dict[str, int]:
    """Warm the shared pool at the offered rate; these requests are not samples."""
    if args.warmup <= 0 or args.cold:
        return {"offered": 0, "completed": 0, "errors": 0, "rejections": 0}
    count = int(args.rate * args.warmup)
    started = asyncio.get_running_loop().time()
    pending: asyncio.Queue[int | None] = asyncio.Queue(maxsize=args.queue)
    warmup_stats = {"offered": 0, "completed": 0, "errors": 0, "rejections": 0}

    async def warm_worker() -> None:
        while True:
            number = await pending.get()
            try:
                if number is None:
                    return
                try:
                    if args.mode == "ws":
                        async with session.ws_connect(
                            args.url, headers=request_headers(args), server_hostname=args.server_name
                        ) as ws:
                            await ws.send_str(WS_PROBE_TEXT)
                            message = await ws.receive(timeout=args.timeout)
                            if message.type != aiohttp.WSMsgType.TEXT or message.data != WS_PROBE_TEXT:
                                raise ValueError("warmup_websocket_echo_mismatch")
                    else:
                        async with session.get(
                            args.url,
                            headers=request_headers(args),
                            server_hostname=args.server_name,
                        ) as response:
                            async for _chunk in response.content.iter_chunked(64 * 1024):
                                if args.read_delay_ms:
                                    await asyncio.sleep(args.read_delay_ms / 1000)
                    warmup_stats["completed"] += 1
                except Exception:
                    warmup_stats["errors"] += 1
            finally:
                pending.task_done()

    workers = [asyncio.create_task(warm_worker()) for _ in range(args.concurrency)]
    for index in range(count):
        due = started + index / args.rate
        await asyncio.sleep(max(0.0, due - asyncio.get_running_loop().time()))
        if pending.full():
            warmup_stats["rejections"] += 1
        else:
            pending.put_nowait(index)
    warmup_stats["offered"] = count
    await pending.join()
    for _ in workers:
        await pending.put(None)
    await asyncio.gather(*workers)
    remaining = started + args.warmup - asyncio.get_running_loop().time()
    if remaining > 0:
        await asyncio.sleep(remaining)
    return warmup_stats


def write_marker(path: str | None) -> None:
    if path is None:
        return
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ready\n", encoding="utf-8")


async def wait_for_resume(path: str | None, timeout: float) -> None:
    if path is None:
        return
    deadline = asyncio.get_running_loop().time() + timeout
    while not Path(path).exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"resume marker did not appear: {path}")
        await asyncio.sleep(0.01)


async def read_http_response(
    args: argparse.Namespace,
    session: aiohttp.ClientSession,
    row: dict[str, Any],
    started: float,
) -> None:
    async with session.get(
        args.url,
        headers=request_headers(args),
        server_hostname=args.server_name,
        trace_request_ctx=row,
    ) as response:
        row["status"] = response.status
        row["ttfb_ms"] = (asyncio.get_running_loop().time() - started) * 1000
        body_bytes = 0
        sse_events = 0
        event_buffer = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            if args.read_delay_ms:
                await asyncio.sleep(args.read_delay_ms / 1000)
            body_bytes += len(chunk)
            if args.mode == "sse" and sse_events < (args.sse_events or 1):
                event_buffer.extend(chunk)
                if len(event_buffer) > 1024 * 1024:
                    raise ValueError("SSE event exceeded 1 MiB without an event delimiter")
                normalized = event_buffer.replace(b"\r\n", b"\n")
                while b"\n\n" in normalized and sse_events < (args.sse_events or 1):
                    _, _, remainder = normalized.partition(b"\n\n")
                    sse_events += 1
                    if row.get("first_event_ms") is None:
                        row["first_event_ms"] = (asyncio.get_running_loop().time() - started) * 1000
                    normalized = remainder
                event_buffer[:] = normalized
                if sse_events >= (args.sse_events or 1):
                    break
        row["bytes"] = body_bytes
        row["sse_events"] = sse_events if args.mode == "sse" else None
        if not 200 <= response.status < 300:
            row["outcome"] = "http_error"
        elif args.mode == "sse" and sse_events < (args.sse_events or 1):
            row["outcome"] = "validation_error"
            row["error"] = "sse_event_shortfall"
        elif args.expect_bytes is not None and body_bytes != args.expect_bytes:
            row["outcome"] = "validation_error"
            row["error"] = "expected_bytes_mismatch"
            row["expected_bytes"] = args.expect_bytes
        else:
            row["outcome"] = "success"


async def perform_request(
    args: argparse.Namespace,
    shared_session: aiohttp.ClientSession | None,
    row: dict[str, Any],
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    row["request_start_s"] = started
    session = shared_session
    owned_session = None
    request_started = started
    try:
        if session is None:
            # A fresh SSLContext and connector per request disables TLS session reuse.
            owned_session = make_session(args)
            session = owned_session
        request_started = loop.time()
        row["client_setup_ms"] = (request_started - started) * 1000
        if args.mode == "ws":
            token = ACTIVE_TRACE_ROW.set(row)
            try:
                async with session.ws_connect(
                    args.url,
                    headers=request_headers(args),
                    server_hostname=args.server_name,
                ) as websocket:
                    row["status"] = websocket._response.status
                    row["ttfb_ms"] = (loop.time() - request_started) * 1000
                    row["bytes"] = 0
                    row["sse_events"] = None
                    if row["status"] != 101:
                        row["outcome"] = "http_error"
                    else:
                        echo_started = loop.time()
                        await websocket.send_str(WS_PROBE_TEXT)
                        message = await websocket.receive(timeout=args.timeout)
                        row["ws_echo_ms"] = (loop.time() - echo_started) * 1000
                        row["ws_echo_ok"] = (
                            message.type == aiohttp.WSMsgType.TEXT and message.data == WS_PROBE_TEXT
                        )
                        row["outcome"] = "success" if row["ws_echo_ok"] else "validation_error"
            finally:
                ACTIVE_TRACE_ROW.reset(token)
        else:
            await read_http_response(args, session, row, request_started)
    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as error:
        row["outcome"] = "timeout"
        row["error"] = type(error).__name__
    except Exception as error:  # one failed request still has its own raw record
        row["outcome"] = "error"
        row["error"] = type(error).__name__
        row["error_message"] = str(error)[:240]
    finally:
        finished = loop.time()
        row["service_ms"] = (finished - request_started) * 1000
        row["scheduled_latency_ms"] = (finished - row["scheduled_at"]) * 1000
        if owned_session is not None:
            await owned_session.close()


async def run_load(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    loop = asyncio.get_running_loop()
    rows: list[dict[str, Any]] = []
    work: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=args.queue)
    shared_session = None if args.cold else make_session(args)
    warmup_stats = {"offered": 0, "completed": 0, "errors": 0, "rejections": 0}
    warmup_started = loop.time()
    try:
        write_marker(args.ready_file)
        if shared_session is not None:
            warmup_stats = await perform_warmup(args, shared_session)
        warmup_elapsed = loop.time() - warmup_started
        write_marker(args.warmup_done_file)
        await wait_for_resume(args.resume_file, args.resume_timeout)

        start = loop.time()
        offered = math.floor(args.rate * args.seconds)

        async def worker() -> None:
            while True:
                row = await work.get()
                try:
                    if row is None:
                        return
                    row["worker_start_s"] = loop.time()
                    row["queue_ms"] = (row["worker_start_s"] - row["enqueued_at"]) * 1000
                    await perform_request(args, shared_session, row)
                    row["complete_s"] = loop.time()
                    rows.append(row)
                finally:
                    work.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(args.concurrency)]
        scheduler_drops = queue_rejections = 0
        for number in range(offered):
            scheduled_at = start + number / args.rate
            await asyncio.sleep(max(0.0, scheduled_at - loop.time()))
            dispatched_at = loop.time()
            lag_ms = (dispatched_at - scheduled_at) * 1000
            row: dict[str, Any] = {
                "n": number,
                "scenario": args.scenario,
                "mode": args.mode,
                "scheduled_offset_s": scheduled_at - start,
                "scheduled_at": scheduled_at,
                "scheduler_lag_ms": lag_ms,
                "queue_ms": None,
                "service_ms": None,
                "ttfb_ms": None,
                "first_event_ms": None,
                "connection_new": False,
                "connection_reused": False,
                "connection_attempted": False,
                "status": None,
                "bytes": 0,
            }
            if lag_ms > args.max_scheduler_lag_ms:
                row["outcome"] = "dropped"
                row["drop_reason"] = "scheduler_lag_limit"
                row["scheduled_latency_ms"] = lag_ms
                row["complete_s"] = dispatched_at
                scheduler_drops += 1
                rows.append(row)
            elif work.full():
                row["outcome"] = "rejected"
                row["reject_reason"] = "bounded_queue_full"
                row["scheduled_latency_ms"] = lag_ms
                row["complete_s"] = dispatched_at
                queue_rejections += 1
                rows.append(row)
            else:
                row["enqueued_at"] = dispatched_at
                work.put_nowait(row)

        offered_end = loop.time()
        offered_window_end = start + args.seconds
        remaining_offer_window = offered_window_end - loop.time()
        if remaining_offer_window > 0:
            await asyncio.sleep(remaining_offer_window)
        offered_end = offered_window_end
        await work.join()
        drained_at = loop.time()
        for _ in workers:
            await work.put(None)
        await asyncio.gather(*workers)
        total_elapsed = drained_at - start
    finally:
        if shared_session is not None:
            await shared_session.close()

    rows.sort(key=lambda row: row["n"])
    counts = Counter(row["outcome"] for row in rows)
    successes = [row for row in rows if row["outcome"] == "success"]
    two_xx_rows = [row for row in rows if isinstance(row.get("status"), int) and 200 <= row["status"] < 300]

    def metric_percentiles(name: str) -> dict[str, dict[str, float | None]]:
        all_values = [float(row[name]) for row in rows if row.get(name) is not None]
        success_values = [float(row[name]) for row in successes if row.get(name) is not None]
        return {"success": percentiles(success_values), "all": percentiles(all_values)}

    completed = sum(row.get("status") is not None for row in rows)
    completed_by_offer_end = sum(
        row.get("status") is not None
        and row.get("complete_s") is not None
        and row["complete_s"] <= offered_end
        for row in rows
    )
    successful_by_offer_end = sum(
        row.get("outcome") == "success" and row.get("complete_s", float("inf")) <= offered_end
        for row in rows
    )
    two_xx = len(two_xx_rows)
    summary: dict[str, Any] = {
        "schema": "cauce.increment7.load.v1",
        "url": args.url,
        "host": args.host_header,
        "scenario": args.scenario,
        "mode": args.mode,
        "protocol": "HTTP/1.1" if args.mode != "ws" else "HTTP/1.1 WebSocket upgrade",
        "cold": args.cold,
        "offered_rate_per_second": args.rate,
        "offered_seconds": args.seconds,
        "offered": len(rows),
        "completed": completed,
        "completed_by_offer_end": completed_by_offer_end,
        "successful_responses_by_offer_end": successful_by_offer_end,
        "2xx": two_xx,
        "http_errors": counts["http_error"],
        "errors": counts["error"] + counts["validation_error"],
        "timeouts": counts["timeout"],
        "rejections": queue_rejections,
        "drops": scheduler_drops,
        "successful_responses": len(successes),
        "offered_goodput_per_second": len(successes) / args.seconds,
        "offer_window_goodput_per_second": successful_by_offer_end / args.seconds,
        "total_elapsed_including_drain_seconds": total_elapsed,
        "drain_seconds": max(0.0, drained_at - offered_end),
        "total_goodput_per_second": len(successes) / total_elapsed if total_elapsed > 0 else None,
        "connection_new": sum(bool(row.get("connection_new")) for row in rows),
        "connection_reused": sum(bool(row.get("connection_reused")) for row in rows),
        "connection_attempted": sum(bool(row.get("connection_attempted")) for row in rows),
        "warmup_requests_offered": warmup_stats["offered"],
        "warmup_requests_excluded": warmup_stats["completed"],
        "warmup_errors": warmup_stats["errors"],
        "warmup_rejections": warmup_stats["rejections"],
        "warmup_seconds": warmup_elapsed,
        "warmup_skipped_cold": bool(args.cold and args.warmup > 0),
        "percentiles_ms": {
            "scheduled_latency": metric_percentiles("scheduled_latency_ms"),
            "service": metric_percentiles("service_ms"),
            "client_setup": metric_percentiles("client_setup_ms"),
            "queue": metric_percentiles("queue_ms"),
            "scheduler_lag": metric_percentiles("scheduler_lag_ms"),
            "ttfb": metric_percentiles("ttfb_ms"),
            "first_sse_event": metric_percentiles("first_event_ms"),
        },
        "config": {
            "concurrency": args.concurrency,
            "queue_capacity": args.queue,
            "timeout_seconds": args.timeout,
            "max_scheduler_lag_ms": args.max_scheduler_lag_ms,
            "read_delay_ms": args.read_delay_ms,
            "expect_bytes": args.expect_bytes,
            "seed": args.seed,
        },
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform(), "aiohttp": aiohttp.__version__},
        "latency_origin": "scheduled_arrival_including_queue_and_generator_delay",
        "notes": [
            "Success means a completed 2xx response (or WebSocket 101) with configured body/event expectations met.",
            "HTTP status failures, transport errors, timeouts, bounded-queue rejections, and scheduler drops stay separate.",
            "Service latency starts when a worker begins the request; scheduled latency starts at the planned arrival.",
            "Cold mode creates a new connector and SSL context per request, preventing connection and TLS session reuse.",
        ],
    }
    return rows, summary


def write_outputs(args: argparse.Namespace, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    raw_path = Path(args.output)
    summary_path = Path(args.summary) if args.summary else raw_path.with_suffix(".summary.json")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            # Monotonic absolute timestamps are process-local; offsets and
            # all derived latency values remain in the portable raw record.
            internal = {"scheduled_at", "enqueued_at", "request_start_s", "worker_start_s", "complete_s"}
            raw = {key: value for key, value in row.items() if key not in internal}
            handle.write(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)
    rows, summary = await run_load(args)
    write_marker(args.measurement_done_file)
    await wait_for_resume(args.measurement_release_file, args.resume_timeout)
    write_outputs(args, rows, summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
