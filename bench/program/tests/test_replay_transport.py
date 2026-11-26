import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock

PROGRAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROGRAM))
spec = importlib.util.spec_from_file_location("replay_transport", PROGRAM / "replay-transport.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class ReplayCorrelationTests(unittest.TestCase):
    def test_failure_before_parser_joins_downstream_socket_even_with_overlapping_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / "proxy.log"
            offers = root / "offers.jsonl"
            rows = [
                {"event": "downstream_accept", "seq": 0,
                 "fields": {"connection": 7, "peer_port": 34567}},
                {"event": "socket_read_eof", "seq": 1,
                 "fields": {"connection": 7, "side": "upstream"}},
                {"event": "socket_read_eof", "seq": 2,
                 "fields": {"connection": 7, "side": "downstream"}},
                {"event": "transport_diagnostics_end", "attempted": 3, "dropped": 0, "limit": 250000},
            ]
            proxy.write_text("".join(json.dumps(row) + "\n" for row in rows))
            offers.write_text(json.dumps({"n": 0, "local_port": 34567, "outcome": "transport_error",
                                         "start_ns": 1, "end_ns": 2}) + "\n")
            report = replay.correlate(proxy, offers, root / "warmup.jsonl")
            self.assertFalse(report["truncated"])
            [failure] = report["failures"]
            self.assertIsNone(failure["request"])
            self.assertEqual(failure["downstream"], 7)
            self.assertEqual([event["seq"] for event in failure["events"]], [0, 2])

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("ss"), "requires Linux iproute2")
    def test_kernel_sampler_matches_established_sockets(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as server, socket.socket() as client:
            server.bind(("127.0.0.1", 0))
            server.listen()
            client.connect(server.getsockname())
            connection, _ = server.accept()
            with connection:
                output = Path(directory) / "samples.jsonl"
                stop = Mock()
                stop.is_set.side_effect = [False, True]
                ports = [server.getsockname()[1], client.getsockname()[1]]
                replay.sample_sockets(os.getpid(), ports, output, stop)
                [row] = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual(row["exit"], 0, row["stderr"])
                self.assertIn("ESTAB", row["ss"])
                for port in ports:
                    self.assertIn(f"127.0.0.1:{port}", row["ss"])


if __name__ == "__main__":
    unittest.main()
