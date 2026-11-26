#!/usr/bin/env python3
"""Exercise actual PostgreSQL, Redis and OpenSSH listeners through Cauce."""
import contextlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from interop import CAUCE, endpoint, eventual, free_port, group, start_process, tcp, tcp_config, write_config

REDIS_IMAGE = "redis@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"


def command(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def ready(port):
    with tcp(port):
        return True


class RealServices(unittest.TestCase):
    def test_postgres_redis_and_sshd(self):
        with contextlib.ExitStack() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="c7-service-")))
            pg, redis, ssh, pg_proxy, redis_proxy, ssh_proxy = (free_port() for _ in range(6))
            print(command("postgres", "--version"), flush=True)
            data = tmp / "pg"
            command("initdb", "-D", str(data), "-U", "cauce", "-A", "trust", "--no-locale")
            command("pg_ctl", "-D", str(data), "-l", str(tmp / "postgres.log"), "-w", "start",
                    "-o", f"-h 127.0.0.1 -p {pg} -k {tmp}")
            stack.callback(command, "pg_ctl", "-D", str(data), "-w", "stop", "-m", "fast")

            redis_id = command("docker", "run", "--pull=never", "--rm", "-d",
                               "-p", f"127.0.0.1:{redis}:6379",
                               REDIS_IMAGE, "redis-server", "--save", "", "--appendonly", "no")
            stack.callback(command, "docker", "stop", "-t", "1", redis_id)
            print(command("docker", "exec", redis_id, "redis-server", "--version"), flush=True)
            eventual(lambda: ready(redis))

            hostkey = tmp / "ssh_host_ed25519"
            command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey))
            sshd = os.environ.get("SSHD", "/usr/sbin/sshd")
            ssh_config = tmp / "sshd.conf"
            ssh_config.write_text(f"""ListenAddress 127.0.0.1
Port {ssh}
HostKey {hostkey}
PidFile {tmp / "sshd.pid"}
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
""")
            ssh_log = tmp / "sshd.log"
            start_process(stack, [sshd, "-D", "-e", "-f", str(ssh_config)], ssh_log)
            try:
                eventual(lambda: ready(ssh))
            except AssertionError:
                self.fail(ssh_log.read_text())

            spec = group("postgres", endpoint(pg)) + tcp_config(pg_proxy, "postgres")
            spec += group("redis", endpoint(redis)) + tcp_config(redis_proxy, "redis")
            spec += group("ssh", endpoint(ssh)) + tcp_config(ssh_proxy, "ssh")
            config = tmp / "cauce.toml"
            write_config(config, free_port(), spec)
            start_process(stack, [CAUCE, str(config)], tmp / "cauce.log")
            eventual(lambda: ready(pg_proxy))

            self.assertEqual(command("psql", "-XAt", "-h", "127.0.0.1", "-p", str(pg_proxy),
                                     "-U", "cauce", "-d", "postgres", "-c", "select 42"), "42")
            print("PostgreSQL query returned 42", flush=True)
            with tcp(redis_proxy) as client, client.makefile("rb") as response:
                client.sendall(b"*1\r\n$4\r\nPING\r\n*2\r\n$4\r\nECHO\r\n$5\r\ncauce\r\n")
                self.assertEqual(response.readline(), b"+PONG\r\n")
                self.assertEqual(response.readline(), b"$5\r\n")
                self.assertEqual(response.readline(), b"cauce\r\n")
            print("Redis RESP PING and binary-safe ECHO", flush=True)
            try:
                scan = command("ssh-keyscan", "-T", "3", "-t", "ed25519",
                               "-p", str(ssh_proxy), "127.0.0.1")
                through_proxy = [line.split()[1:] for line in scan.splitlines() if not line.startswith("#")]
            except RuntimeError as error:
                self.fail(f"{error}\n{ssh_log.read_text()}")
            self.assertEqual(through_proxy, [hostkey.with_suffix(".pub").read_text().split()[:2]])
            print("OpenSSH key exchange returned the configured Ed25519 host key", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
