#!/usr/bin/env python3
"""Run Cauce against the standard grpcio health service over real sockets."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from integration import BIN, ROOT, port


class HealthTests(unittest.TestCase):
    def test_standard_service_health_ejection_recovery_and_reload(self):
        request = health_pb2.HealthCheckRequest
        serving = health_pb2.HealthCheckResponse.SERVING
        not_serving = health_pb2.HealthCheckResponse.NOT_SERVING
        backend_health = health.HealthServicer()

        with ThreadPoolExecutor(max_workers=4) as workers, tempfile.TemporaryDirectory() as directory:
            backend = grpc.server(workers)
            health_pb2_grpc.add_HealthServicer_to_server(backend_health, backend)
            backend_port = backend.add_insecure_port("127.0.0.1:0")
            self.assertGreater(backend_port, 0)
            backend.start()
            try:
                listen, admin = port(), port()
                config = (
                    (ROOT / "examples/http2-health.yaml").read_text()
                    .replace("127.0.0.1:8080", f"127.0.0.1:{listen}")
                    .replace("localhost:8080", f"localhost:{listen}")
                    .replace("127.0.0.1:9090", f"127.0.0.1:{admin}")
                    .replace("127.0.0.1:8001", f"127.0.0.1:{backend_port}")
                    .replace("example.v1.Api", "grpc.health.v1.Health")
                    .replace("service: grpc.health.v1.Health", "service: readiness")
                    .replace("interval_ms: 2s", "interval_ms: 100ms")
                    .replace("timeout_ms: 500ms", "timeout_ms: 100ms")
                )
                path = Path(directory) / "proxy.yaml"
                path.write_text(config)
                log = Path(directory) / "stderr"
                backend_health.set("canary", serving)
                backend_health.set("readiness", not_serving)
                with log.open("w") as stderr:
                    proxy = subprocess.Popen(
                        [BIN, path], cwd=directory, env={**os.environ, "TOKIO_WORKER_THREADS": "2"},
                        stderr=stderr,
                    )
                try:
                    self.await_value(lambda: '"started"' in log.read_text(), True)
                    with grpc.insecure_channel(
                        f"127.0.0.1:{listen}",
                        options=[("grpc.default_authority", f"localhost:{listen}")],
                    ) as channel:
                        stub = health_pb2_grpc.HealthStub(channel)

                        def canary():
                            try:
                                return stub.Check(request(service="canary"), timeout=0.5).status
                            except grpc.RpcError as error:
                                return error.code()

                        self.await_value(canary, grpc.StatusCode.UNAVAILABLE)
                        backend_health.set("readiness", serving)
                        self.await_value(canary, serving)
                        backend_health.set("readiness", not_serving)
                        self.await_value(canary, grpc.StatusCode.UNAVAILABLE)
                        backend_health.set("readiness", serving)
                        self.await_value(canary, serving)

                        path.write_text(config.replace("service: readiness", "path: /invalid"))
                        proxy.send_signal(signal.SIGHUP)
                        self.await_value(lambda: '"reload_rejected"' in log.read_text(), True)
                        self.assertEqual(canary(), serving)

                        path.write_text(config.replace("service: readiness", "service: other"))
                        proxy.send_signal(signal.SIGHUP)
                        self.await_value(lambda: '"reloaded"' in log.read_text(), True)
                        self.await_value(canary, grpc.StatusCode.UNAVAILABLE)
                        backend_health.set("other", serving)
                        self.await_value(canary, serving)
                finally:
                    proxy.terminate()
                    self.assertEqual(proxy.wait(timeout=8), 0, log.read_text())
            finally:
                backend.stop(0).wait(timeout=5)

    def await_value(self, probe, expected):
        deadline = time.monotonic() + 8
        while True:
            observed = probe()
            if observed == expected:
                return
            if time.monotonic() >= deadline:
                self.fail(f"expected {expected}, observed {observed}")
            time.sleep(0.03)


if __name__ == "__main__":
    unittest.main()
