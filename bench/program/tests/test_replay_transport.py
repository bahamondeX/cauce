import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
