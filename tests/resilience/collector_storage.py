#!/usr/bin/env python3
"""Bounded real-Docker collector storage failures and recovery.

Only per-container tmpfs storage is pressured. The host/Colima filesystem is
never filled. Storage cases use fixture access-log records. The logger handoff
case uses two real proxy responses. No case contacts Connectivity.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "examples" / "access-log.jsonl"
NODE_IMAGE = "node:22.14.0-alpine3.21@sha256:9bef0ef1e268f60627da9ba7d7605e8831d5b56ad07487d24d1aa386336d1944"


def run(*args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"command failed {args!r}: {p.stderr[-2000:]}")
    return p


def config_text(source: str, db: str, spool: int, dbmax: int) -> str:
    return f'''[collector]
access_log = "{source}"
state_path = "{db}"
delivery = "spool"
spool_max_bytes = {spool}
db_max_bytes = {dbmax}
batch_max_events = 1
batch_max_bytes = 2048
max_line_bytes = 2048
max_files = 8
poll_ms = 50
backoff_min_ms = 50
backoff_max_ms = 200
request_timeout_ms = 1000
'''


def proxy_config_text() -> str:
    return '''name="cauce"
listen="0.0.0.0:8080"
admin="127.0.0.1:9090"
[http2]
enabled=false
[limits]
clients=32
requests=16
connections=8
idle_connections=4
connecting=4
acquire_ms=500
connect_ms=1000
headers_ms=2000
stream_idle_ms=3000
request_ms=5000
client_headers_ms=2000
idle_connection_ms=2000
max_connection_age_ms=10000
dns_cache_ms=1000
tcp_keepalive_secs=10
max_header_bytes=16384
max_headers=64
max_body_bytes=1048576
shutdown_ms=1000
[logs]
enabled=true
path="/sandbox/logs/access.jsonl"
queue=32
rotate_bytes=16777216
retained_files=1
[[upstreams]]
id="backend"
address="backend:8000"
host="backend:8000"
isolation="collector-log-repair"
connections=4
idle_connections=2
failure_backoff_ms=100
[[routes]]
id="main"
host="proxy:8080"
path_prefix="/"
balance="round_robin"
upstreams=["backend"]
'''


def collector_proxy_request(network: str, path: str, temp: Path) -> dict:
    js = ("fetch('http://proxy:8080' + " + json.dumps(path) + ").then(async r => {"
          "const body=await r.text(); console.log(JSON.stringify({status:r.status,body,request_id:r.headers.get('x-request-id')}))"
          "}).catch(e=>{console.error(String(e));process.exit(1)})")
    return json.loads(run("docker", "run", "--rm", "--network", network,
                          "--memory", "64m", "--cpus", ".25", "--pids-limit", "16",
                          "--log-opt", "max-size=128k", "--log-opt", "max-file=1",
                          NODE_IMAGE, "node", "-e", js, timeout=15).stdout)


def records(count: int) -> list[bytes]:
    template = json.loads(FIXTURE.read_text().splitlines()[0])
    result = []
    for i in range(count):
        item = dict(template)
        item["event_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cauce-increment9-fixture-{i}"))
        item["request_id"] = str(uuid.uuid5(uuid.NAMESPACE_OID, f"cauce-increment9-request-{i}"))
        item["path"] = f"/resilience-fixture/{i}"
        result.append((json.dumps(item, separators=(",", ":")) + "\n").encode())
    return result


class DockerCase:
    def __init__(self, image: str, work: Path, name: str, run_id: str,
                 tmpfs: str = "8m", network: str = "none", network_alias: str | None = None):
        self.image = image
        self.work = work
        self.name = name
        self.run_id = run_id
        self.cid = ""
        self.mount = tmpfs
        self.network = network
        self.network_alias = network_alias

    def start(self) -> None:
        alias_args = ["--network-alias", self.network_alias] if self.network_alias else []
        self.cid = run("docker", "run", "-d", "--name", self.name,
                       "--memory", "128m", "--cpus", "0.50", "--pids-limit", "32",
                       "--ulimit", "nofile=256:256", "--read-only", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges",
                       "--network", self.network, *alias_args,
                       "--label", "com.cauce.test=increment9-collector",
                       "--label", f"com.cauce.run={self.run_id}",
                       "--tmpfs", f"/sandbox:rw,size={self.mount},uid=10001,gid=10001,mode=0770,nr_inodes=1024",
                       "--entrypoint", "/bin/sh", self.image, "-c", "while :; do sleep 3600; done").stdout.strip()
        self.exec("/bin/sh", "-c", "mkdir -p /sandbox/logs /sandbox/state")

    def exec(self, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
        return run("docker", "exec", "--user", "10001:10001", self.cid, *args,
                   check=check, timeout=timeout)

    def copy_in(self, local: Path, remote: str) -> None:
        parent = remote.rsplit("/", 1)[0]
        self.exec("/bin/sh", "-c", f"mkdir -p {parent}")
        p = subprocess.run(["docker", "exec", "-i", "--user", "10001:10001", self.cid,
                            "/bin/sh", "-c", f"cat > {remote}"], input=local.read_bytes(),
                           capture_output=True, timeout=30)
        if p.returncode:
            raise RuntimeError(f"write fixture into tmpfs failed: {p.stderr.decode(errors='replace')}")
        self.exec("chmod", "600", remote)

    def collect(self, check: bool = True) -> subprocess.CompletedProcess:
        return self.exec("/usr/local/bin/cauce-collector", "--config", "/sandbox/collector.toml", "--once",
                         check=check)

    def status(self) -> dict:
        p = self.exec("/usr/local/bin/cauce-collector", "--config", "/sandbox/collector.toml", "--status")
        return json.loads(p.stdout)

    def fetch_db(self, local: Path) -> None:
        p = subprocess.run(["docker", "exec", self.cid, "/bin/sh", "-c",
                            "cat /sandbox/state/state.db"], capture_output=True, timeout=30)
        if p.returncode:
            raise RuntimeError(f"read collector state failed: {p.stderr.decode(errors='replace')}")
        local.write_bytes(p.stdout)

    def stop(self) -> None:
        if self.cid:
            p = subprocess.run(["docker", "rm", "-f", self.cid], capture_output=True, text=True, timeout=20)
            if p.returncode:
                still_there = subprocess.run(["docker", "inspect", self.cid], capture_output=True,
                                             text=True, timeout=20)
                if still_there.returncode == 0:
                    raise RuntimeError(f"container cleanup failed: {p.stderr[-500:]}")
            self.cid = ""


def configure(case: DockerCase, temp: Path, data: bytes, spool: int, dbmax: int) -> Path:
    source = temp / f"{case.name}-access.jsonl"
    cfg = temp / f"{case.name}-collector.toml"
    source.write_bytes(data)
    cfg.write_text(config_text("/sandbox/logs/access.jsonl", "/sandbox/state/state.db", spool, dbmax))
    case.copy_in(source, "/sandbox/logs/access.jsonl")
    case.copy_in(cfg, "/sandbox/collector.toml")
    return source


def inspect_db(case: DockerCase, temp: Path) -> tuple[list[str], list[dict], str]:
    local = temp / f"{case.name}-inspect.db"
    case.fetch_db(local)
    with sqlite3.connect(local) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        events = []
        for (body,) in db.execute("SELECT body FROM spool ORDER BY seq"):
            events.extend(e["event_id"] for e in json.loads(body)["events"])
        checkpoints = [dict(zip(("dev", "ino", "path", "offset", "observed_size"), row))
                       for row in db.execute("SELECT dev,ino,path,offset,observed_size FROM checkpoints")]
    return events, checkpoints, integrity


def inspect_batch_ids(case: DockerCase, temp: Path) -> list[str]:
    local = temp / f"{case.name}-batches.db"
    case.fetch_db(local)
    with sqlite3.connect(local) as db:
        return [row[0] for row in db.execute("SELECT batch_id FROM spool ORDER BY seq")]


def checkpoint_offset(checkpoints: list[dict]) -> int:
    return checkpoints[0]["offset"] if len(checkpoints) == 1 else -1


def container_limits(case: DockerCase) -> dict:
    obj = json.loads(run("docker", "inspect", case.cid).stdout)[0]
    hc = obj["HostConfig"]
    return {"image_id": obj["Image"], "state": obj["State"]["Status"],
            "memory_bytes": hc["Memory"], "nano_cpus": hc["NanoCpus"],
            "pids_limit": hc["PidsLimit"], "readonly_rootfs": hc["ReadonlyRootfs"],
            "tmpfs": hc["Tmpfs"], "network_mode": hc["NetworkMode"],
            "labels": obj["Config"]["Labels"]}


def ensure_docker() -> None:
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=20)
    if info.returncode:
        # User instruction: start Colima whenever Docker is unavailable.
        run("colima", "start", timeout=180)
        run("docker", "info", timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    ensure_docker()
    run_id = uuid.uuid4().hex
    image_id = run("docker", "image", "inspect", args.image, "--format", "{{.Id}}").stdout.strip()
    report = {"suite": "collector-storage-real-docker", "image": args.image,
              "image_id": image_id, "run_id": run_id,
              "fixture": "Storage: examples/access-log.jsonl with deterministic IDs; logger handoff: real proxy events",
              "host_disk_filled": False, "docker_config": "inherited from invocation environment",
              "cases": [], "failures": [], "cleanup_remaining": [], "cleanup_errors": []}
    temp_path = Path(tempfile.mkdtemp(prefix="cauce-i9-collector-"))
    cases: list[DockerCase] = []
    auxiliary_containers: list[str] = []
    owned_networks: list[str] = []
    try:
        all_records = records(24)
        all_ids = [json.loads(line)["event_id"] for line in all_records]

        # Quota pause survives process restart. Increasing configured quota is
        # the documented operator recovery; queued IDs remain stable.
        c = DockerCase(args.image, temp_path, "i9-coll-quota-" + run_id[:10], run_id)
        cases.append(c)
        c.start()
        source = configure(c, temp_path, b"".join(all_records), 4096, 131072)
        prior = None
        for _ in range(32):
            c.collect()
            first = c.status()
            queued1, checkpoints1, integrity1 = inspect_db(c, temp_path)
            offset1 = checkpoint_offset(checkpoints1)
            current = (first["pending_events"], first["pending_bytes"], offset1)
            if current == prior:
                break
            prior = current
        c.collect()
        second = c.status()
        queued2, checkpoints2, integrity2 = inspect_db(c, temp_path)
        offset2 = checkpoint_offset(checkpoints2)
        all_source_bytes = len(b"".join(all_records))
        next_record_bytes = len(all_records[first["pending_events"]]) - 1
        batch_ids1 = inspect_batch_ids(c, temp_path)
        batch_ids2 = inspect_batch_ids(c, temp_path)
        quota_stable = (first["pending_events"] == second["pending_events"]
                        and queued1 == queued2 and offset1 == offset2
                        and first["oldest_batch_id"] == second["oldest_batch_id"]
                        and 0 < first["pending_events"] < len(all_ids)
                        and offset1 < all_source_bytes
                        and first["pending_bytes"] <= first["spool_max_bytes"]
                        and first["pending_bytes"] + next_record_bytes > first["spool_max_bytes"]
                        and batch_ids1 == batch_ids2
                        and integrity1 == integrity2 == "ok"
                        and checkpoints1 == checkpoints2
                        and len(checkpoints1) == len(checkpoints2) == 1)
        if not quota_stable:
            raise AssertionError(f"spool quota/restart was not stable: {first=} {second=} {offset1=} {offset2=}")
        cfg_local = temp_path / f"{c.name}-collector.toml"
        cfg_local.write_text(config_text("/sandbox/logs/access.jsonl", "/sandbox/state/state.db", 131072, 262144))
        c.copy_in(cfg_local, "/sandbox/collector.toml")
        for _ in range(len(all_records) + 2):
            c.collect()
        recovered = c.status()
        queued3, checkpoints3, integrity3 = inspect_db(c, temp_path)
        offset3 = checkpoint_offset(checkpoints3)
        expected_queued = all_ids[:recovered["pending_events"]]
        if (recovered["pending_events"] != len(all_ids) or queued3 != all_ids
                or offset3 != len(b"".join(all_records)) or integrity3 != "ok"):
            raise AssertionError(f"quota increase did not recover exact events: {recovered=} {offset3=} {integrity3=}")
        report["cases"].append({"name": "spool quota pause survives restart and explicit quota increase resumes",
                                 "passed": True, "initial_status": first, "restart_status": second,
                                 "recovered_status": recovered, "event_ids_exact": queued3 == all_ids,
                                 "integrity_check": integrity3, "source_bytes": len(b"".join(all_records)),
                                 "checkpoint_after_recovery": offset3, "events": len(expected_queued),
                                 "checkpoint_at_quota": checkpoints2, "checkpoint_after_recovery_rows": checkpoints3,
                                 "integrity_at_quota_before_restart": integrity1,
                                 "integrity_at_quota_after_restart": integrity2,
                                 "stable_batch_ids": batch_ids2,
                                 "next_event_json_bytes_lower_bound": next_record_bytes,
                                 "pending_plus_next_event_lower_bound": first["pending_bytes"] + next_record_bytes,
                                 "container_limits": container_limits(c)})

        # SIGKILL the actual long-running collector after durable commits.
        k = DockerCase(args.image, temp_path, "i9-coll-kill-" + run_id[:10], run_id)
        cases.append(k)
        k.start()
        configure(k, temp_path, b"".join(all_records), 4096, 131072)
        launch = run("docker", "exec", "-d", "--user", "10001:10001", k.cid,
                     "/usr/local/bin/cauce-collector", "--config", "/sandbox/collector.toml")
        time.sleep(0.6)
        kill_script = "for p in /proc/[0-9]*; do [ -r $p/comm ] || continue; IFS= read -r n < $p/comm || continue; if [ $n = cauce-collector ]; then pid=${p#/proc/}; kill -9 $pid && echo $pid; exit 0; fi; done; exit 3"
        killed = k.exec("/bin/sh", "-c", kill_script, check=False)
        if killed.returncode or not killed.stdout.strip().isdigit():
            raise AssertionError(f"could not SIGKILL collector process: {killed.stdout!r} {killed.stderr!r}")
        killed_pid = int(killed.stdout.strip())
        dead = k.exec("/bin/sh", "-c",
                      f"test ! -r /proc/{killed_pid}/comm || ! grep -qx cauce-collector /proc/{killed_pid}/comm",
                      check=False)
        if dead.returncode:
            raise AssertionError(f"collector PID {killed_pid} still alive after SIGKILL")
        killed_status = k.status()
        killed_ids, killed_checkpoints, killed_integrity = inspect_db(k, temp_path)
        killed_offset = checkpoint_offset(killed_checkpoints)
        if not (0 < killed_status["pending_events"] < len(all_ids)
                and killed_ids == all_ids[:killed_status["pending_events"]]
                and killed_offset < len(b"".join(all_records)) and killed_integrity == "ok"):
            raise AssertionError(f"SIGKILL state not durable/consistent: {killed_status=} {killed_offset=} {killed_integrity=}")
        kcfg = temp_path / f"{k.name}-collector.toml"
        kcfg.write_text(config_text("/sandbox/logs/access.jsonl", "/sandbox/state/state.db", 131072, 262144))
        k.copy_in(kcfg, "/sandbox/collector.toml")
        for _ in range(len(all_records) + 2):
            k.collect()
        restart_status = k.status()
        restart_ids, restart_checkpoints, restart_integrity = inspect_db(k, temp_path)
        restart_offset = checkpoint_offset(restart_checkpoints)
        if (restart_status["pending_events"] != len(all_ids) or restart_ids != all_ids
                or restart_offset != len(b"".join(all_records)) or restart_integrity != "ok"):
            raise AssertionError(f"SIGKILL restart recovery failed: {restart_status=} {restart_offset=} {restart_integrity=}")
        report["cases"].append({"name": "SIGKILL of live collector preserves spool and restart resumes exact events",
                                 "passed": True, "kill_signal": "SIGKILL", "collector_pid": killed_pid,
                                 "detached_launch_returncode": launch.returncode, "state_after_kill": killed_status,
                                 "event_ids_after_kill": killed_ids, "checkpoint_rows_after_kill": killed_checkpoints,
                                 "integrity_after_kill": killed_integrity, "state_after_restart": restart_status,
                                 "event_ids_after_restart_exact": restart_ids == all_ids,
                                 "checkpoint_rows_after_restart": restart_checkpoints,
                                 "integrity_after_restart": restart_integrity,
                                 "container_limits": container_limits(k)})

        # Real proxy/collector interaction: the collector checkpoints one
        # complete proxy event plus an observed incomplete tail. On proxy
        # process restart, logger repair/rotation must preserve the checkpointed
        # inode so the collector can resume without a false rewrite alarm.
        repair_network = "i9-coll-repair-net-" + run_id[:10]
        run("docker", "network", "create", "--internal", "--label",
            f"com.cauce.run={run_id}", repair_network)
        owned_networks.append(repair_network)
        backend_name = "i9-coll-repair-backend-" + run_id[:10]
        backend_js = ("require('http').createServer((q,s)=>{q.resume();q.on('end',()=>{"
                      "s.writeHead(200,{'content-length':'2'});s.end('OK')})}).listen(8000,'0.0.0.0')")
        backend_id = run("docker", "run", "-d", "--name", backend_name, "--network", repair_network,
                         "--network-alias", "backend", "--label", "com.cauce.test=increment9-collector",
                         "--label", f"com.cauce.run={run_id}", "--memory", "64m", "--cpus", ".25",
                         "--pids-limit", "16", "--read-only", "--cap-drop", "ALL",
                         "--log-opt", "max-size=128k", "--log-opt", "max-file=1",
                         NODE_IMAGE, "node", "-e", backend_js).stdout.strip()
        auxiliary_containers.append(backend_id)
        pxy = DockerCase(args.image, temp_path, "i9-coll-repair-" + run_id[:10], run_id,
                         network=repair_network, network_alias="proxy")
        cases.append(pxy)
        pxy.start()
        proxy_cfg = temp_path / f"{pxy.name}-proxy.toml"
        proxy_cfg.write_text(proxy_config_text())
        pxy.copy_in(proxy_cfg, "/sandbox/proxy.toml")
        config_check = pxy.exec("/usr/local/bin/cauce", "check", "/sandbox/proxy.toml", check=False)
        if config_check.returncode:
            raise AssertionError(f"proxy integration config invalid: {config_check.stderr[-1200:]}")
        collector_cfg = temp_path / f"{pxy.name}-collector.toml"
        collector_cfg.write_text(config_text("/sandbox/logs/access.jsonl", "/sandbox/state/state.db",
                                             131072, 262144))
        pxy.copy_in(collector_cfg, "/sandbox/collector.toml")
        initial_proxy_launch = run("docker", "exec", "-d", "--user", "10001:10001", pxy.cid,
                                   "/bin/sh", "-c", "/usr/local/bin/cauce /sandbox/proxy.toml > /sandbox/proxy.log 2>&1")
        end = time.monotonic() + 12
        first_response = None
        request_error = None
        while time.monotonic() < end:
            try:
                first_response = collector_proxy_request(repair_network, "/before-repair", temp_path)
                break
            except Exception as exc:
                request_error = repr(exc)
                time.sleep(.1)
        if first_response is None or first_response["status"] != 200:
            proxy_logs = pxy.exec("/bin/sh", "-c", "cat /sandbox/proxy.log 2>/dev/null || true", check=False)
            backend_logs = subprocess.run(["docker", "logs", backend_id], capture_output=True,
                                          text=True, timeout=20)
            raise AssertionError(f"real proxy did not serve baseline request: {first_response=}; last_request_error={request_error}; "
                                 f"proxy_logs={(proxy_logs.stdout + proxy_logs.stderr)[-1500:]!r}; "
                                 f"backend_logs={(backend_logs.stdout + backend_logs.stderr)[-1000:]!r}")
        time.sleep(.2)
        original_log = pxy.exec("cat", "/sandbox/logs/access.jsonl").stdout.encode()
        original_lines = original_log.splitlines(keepends=True)
        if len(original_lines) != 1 or not original_lines[0].endswith(b"\n"):
            raise AssertionError(f"baseline proxy log was not one complete JSONL record: {original_log[-100:]!r}")
        baseline_event = json.loads(original_lines[0])
        partial_tail = b'{"schema_version":"1.0","event_id":"interrupted-tail-'
        partial_file = temp_path / f"{pxy.name}-partial-tail"
        partial_file.write_bytes(partial_tail)
        pxy.copy_in(partial_file, "/sandbox/logs/partial-tail")
        pxy.exec("/bin/sh", "-c", "cat /sandbox/logs/partial-tail >> /sandbox/logs/access.jsonl")
        pxy.collect()
        checkpointed_status = pxy.status()
        checkpointed_ids, checkpointed_rows, checkpointed_integrity = inspect_db(pxy, temp_path)
        if (checkpointed_status["pending_events"] != 1 or checkpointed_ids != [baseline_event["event_id"]]
                or len(checkpointed_rows) != 1
                or checkpointed_rows[0]["offset"] != len(original_lines[0])
                or checkpointed_rows[0]["observed_size"] != len(original_log) + len(partial_tail)
                or checkpointed_integrity != "ok"):
            raise AssertionError(f"collector did not checkpoint full line before partial tail: {checkpointed_status=} {checkpointed_rows=}")
        find_pid = "for p in /proc/[0-9]*; do [ -r $p/comm ] || continue; IFS= read -r n < $p/comm || continue; if [ $n = cauce ]; then pid=${p#/proc/}; echo $pid; kill -TERM $pid; exit 0; fi; done; exit 3"
        stop_proxy = pxy.exec("/bin/sh", "-c", find_pid, check=False)
        if stop_proxy.returncode or not stop_proxy.stdout.strip().isdigit():
            raise AssertionError(f"could not terminate real proxy process: {stop_proxy.stdout!r} {stop_proxy.stderr!r}")
        proxy_pid = int(stop_proxy.stdout.strip())
        wait_exit = pxy.exec("/bin/sh", "-c",
                             f"i=0; while [ $i -lt 100 ]; do if [ ! -r /proc/{proxy_pid}/comm ] || grep -q ' (cauce) Z ' /proc/{proxy_pid}/stat; then exit 0; fi; sleep .05; i=$((i+1)); done; exit 1",
                             check=False, timeout=10)
        if wait_exit.returncode:
            raise AssertionError(f"proxy process {proxy_pid} did not shut down before restart")
        proxy_launch = run("docker", "exec", "-d", "--user", "10001:10001", pxy.cid,
                           "/bin/sh", "-c", "/usr/local/bin/cauce /sandbox/proxy.toml > /sandbox/proxy.log 2>&1")
        end = time.monotonic() + 12
        second_response = None
        while time.monotonic() < end:
            try:
                second_response = collector_proxy_request(repair_network, "/after-repair", temp_path)
                break
            except Exception:
                time.sleep(.1)
        if second_response is None or second_response["status"] != 200:
            raise AssertionError(f"restarted proxy did not serve recovery request: {second_response=}")
        time.sleep(.2)
        rotated_path = "/sandbox/logs/access.jsonl.1"
        rotated_log = pxy.exec("cat", rotated_path).stdout.encode()
        active_log = pxy.exec("cat", "/sandbox/logs/access.jsonl").stdout.encode()
        rotated_events = [json.loads(line) for line in rotated_log.splitlines() if line]
        active_events = [json.loads(line) for line in active_log.splitlines() if line]
        if (rotated_log != original_log or len(rotated_events) != 1 or len(active_events) != 1
                or not active_log.endswith(b"\n")):
            raise AssertionError(f"logger repair did not preserve/rotate whole lines: {rotated_log[-100:]!r} {active_log[-100:]!r}")
        pxy.collect()
        repaired_status = pxy.status()
        repaired_ids, repaired_rows, repaired_integrity = inspect_db(pxy, temp_path)
        expected_event_ids = [baseline_event["event_id"], active_events[0]["event_id"]]
        if (repaired_status["pending_events"] != 2 or repaired_ids != expected_event_ids
                or len(repaired_rows) != 2 or repaired_integrity != "ok"
                or not any(row["path"].endswith("access.jsonl.1") and row["offset"] == len(rotated_log)
                           for row in repaired_rows)
                or not any(row["path"].endswith("access.jsonl") and row["offset"] == len(active_log)
                           for row in repaired_rows)):
            raise AssertionError(f"collector failed after logger repair/rotation: {repaired_status=} {repaired_rows=}")
        report["cases"].append({"name": "proxy logger tail repair rotates checkpointed inode and collector resumes",
                                 "passed": True, "proxy_launch_returncodes": [initial_proxy_launch.returncode,
                                                                            proxy_launch.returncode],
                                 "baseline_request": first_response, "recovery_request": second_response,
                                 "baseline_log_bytes": len(original_log), "injected_incomplete_tail_bytes": len(partial_tail),
                                 "collector_state_before_proxy_restart": checkpointed_status,
                                 "checkpoint_rows_before_proxy_restart": checkpointed_rows,
                                 "integrity_before_proxy_restart": checkpointed_integrity,
                                 "proxy_pid_restarted": proxy_pid, "rotated_log_bytes": len(rotated_log),
                                 "active_log_bytes_after_restart": len(active_log),
                                 "recovered_event_ids_exact": repaired_ids == expected_event_ids,
                                 "collector_state_after_repair": repaired_status,
                                 "checkpoint_rows_after_repair": repaired_rows,
                                 "integrity_after_repair": repaired_integrity,
                                 "container_limits": container_limits(pxy)})

        # Actual filesystem ENOSPC is confined to an 8 MiB tmpfs. First commit
        # one event/checkpoint, then append a second record, exhaust tmpfs, and
        # verify failed commit is atomic before freeing only the filler.
        d = DockerCase(args.image, temp_path, "i9-coll-enospc-" + run_id[:10], run_id)
        cases.append(d)
        d.start()
        first_line, second_line = all_records[:2]
        configure(d, temp_path, first_line, 524288, 1048576)
        d.collect()
        before = d.status()
        before_ids, before_checkpoints, before_integrity = inspect_db(d, temp_path)
        before_offset = checkpoint_offset(before_checkpoints)
        before_batch_ids = inspect_batch_ids(d, temp_path)
        if before["pending_events"] != 1 or before_ids != [all_ids[0]]:
            raise AssertionError(f"baseline spool commit failed: {before=} {before_ids=}")
        appended = temp_path / f"{d.name}-append.jsonl"
        appended.write_bytes(second_line)
        d.copy_in(appended, "/sandbox/logs/second.jsonl")
        # append under the collector's existing source inode; cat itself only
        # writes a few KiB before pressure is induced.
        d.exec("/bin/sh", "-c", "cat /sandbox/logs/second.jsonl >> /sandbox/logs/access.jsonl")
        fill = d.exec("/bin/sh", "-c", "dd if=/dev/zero of=/sandbox/filler bs=4096 status=none", check=False, timeout=30)
        fill_text = fill.stderr + fill.stdout
        full_df = d.exec("df", "-k", "/sandbox").stdout
        if fill.returncode == 0 or "No space left on device" not in fill_text:
            raise AssertionError(f"tmpfs did not report true ENOSPC: rc={fill.returncode}, {fill_text!r}, {full_df!r}")
        if "tmpfs" not in full_df.lower() or "8192" not in full_df:
            raise AssertionError(f"full filesystem did not identify the bounded 8 MiB tmpfs: {full_df!r}")
        failed = d.collect(check=False)
        after_failure = d.status()
        failed_ids, failed_checkpoints, failed_integrity = inspect_db(d, temp_path)
        failed_offset = checkpoint_offset(failed_checkpoints)
        failed_batch_ids = inspect_batch_ids(d, temp_path)
        if (failed.returncode == 0 or after_failure["pending_events"] != before["pending_events"]
                or failed_ids != before_ids or failed_batch_ids != before_batch_ids
                or failed_offset != before_offset or failed_integrity != "ok"):
            raise AssertionError("ENOSPC did not leave spool/checkpoint atomic: " + json.dumps({
                "stderr": failed.stderr[-500:], "before": before, "after": after_failure,
                "before_offset": before_offset, "after_offset": failed_offset,
                "before_ids": before_ids, "after_ids": failed_ids, "integrity": failed_integrity}))
        d.exec("rm", "-f", "/sandbox/filler")
        recovery = d.collect()
        after_recovery = d.status()
        recovered_ids, recovered_checkpoints, recovered_integrity = inspect_db(d, temp_path)
        recovered_offset = checkpoint_offset(recovered_checkpoints)
        recovered_batch_ids = inspect_batch_ids(d, temp_path)
        expected_bytes = len(first_line) + len(second_line)
        if (after_recovery["pending_events"] != 2 or recovered_ids != all_ids[:2]
                or recovered_offset != expected_bytes or recovered_integrity != "ok"):
            raise AssertionError(f"recovery after freeing bounded filler failed: {after_recovery=} {recovered_offset=} {recovered_integrity=}")
        report["cases"].append({"name": "real tmpfs ENOSPC rolls back checkpoint and recovers after filler removal",
                                 "passed": True, "tmpfs_limit": "8 MiB", "filler_exit": fill.returncode,
                                 "enospc_evidence": fill_text[-300:], "failed_collector_exit": failed.returncode,
                                 "tmpfs_full_df": full_df,
                                 "failed_collector_error": failed.stderr[-500:],
                                 "before_status": before, "status_after_failed_commit": after_failure,
                                 "checkpoint_before": before_offset, "checkpoint_after_failure": failed_offset,
                                 "checkpoint_rows_before": before_checkpoints,
                                 "checkpoint_rows_after_failure": failed_checkpoints,
                                 "batch_ids_before": before_batch_ids,
                                 "batch_ids_after_failure": failed_batch_ids,
                                 "batch_ids_after_recovery": recovered_batch_ids,
                                 "failed_commit_integrity": failed_integrity,
                                 "recovered_status": after_recovery, "recovered_event_ids_exact": recovered_ids == all_ids[:2],
                                 "checkpoint_after_recovery": recovered_offset,
                                 "recovered_integrity_check": recovered_integrity,
                                 "checkpoint_rows_after_recovery": recovered_checkpoints,
                                 "container_limits": container_limits(d)})
        report["passed"] = True
    except Exception as exc:
        report["failures"].append(str(exc))
        report["passed"] = False
    finally:
        for case in reversed(cases):
            try:
                case.stop()
            except Exception as exc:
                report["cleanup_errors"].append(str(exc))
        for container_id in reversed(auxiliary_containers):
            p = subprocess.run(["docker", "rm", "-f", container_id], capture_output=True,
                               text=True, timeout=20)
            if p.returncode:
                still_there = subprocess.run(["docker", "inspect", container_id], capture_output=True,
                                             text=True, timeout=20)
                if still_there.returncode == 0:
                    report["cleanup_errors"].append(f"auxiliary container cleanup failed: {p.stderr[-500:]}")
        for network in reversed(owned_networks):
            p = subprocess.run(["docker", "network", "rm", network], capture_output=True,
                               text=True, timeout=20)
            if p.returncode:
                still_there = subprocess.run(["docker", "network", "inspect", network], capture_output=True,
                                             text=True, timeout=20)
                if still_there.returncode == 0:
                    report["cleanup_errors"].append(f"network cleanup failed for {network}: {p.stderr[-500:]}")
        for case in cases:
            if case.cid:
                report["cleanup_remaining"].append(case.cid)
        try:
            shutil.rmtree(temp_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            report["cleanup_errors"].append(f"temporary directory cleanup failed: {exc}")
        report["temporary_directory_remaining"] = temp_path.exists()
        remaining = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.cauce.run={run_id}"],
                                   capture_output=True, text=True, timeout=20)
        report["cleanup_remaining"].extend(remaining.stdout.splitlines())
        report["cleanup_remaining_networks"] = subprocess.run(
            ["docker", "network", "ls", "-q", "--filter", f"label=com.cauce.run={run_id}"],
            capture_output=True, text=True, timeout=20).stdout.splitlines()
        if (report["cleanup_errors"] or report["cleanup_remaining"] or report["temporary_directory_remaining"]
                or report["cleanup_remaining_networks"]):
            report["passed"] = False
            report["failures"].append("cleanup incomplete")
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
