#!/usr/bin/env python3
"""Run an isolated, offline proxy -> JSONL -> collector spool demonstration.

Uses prebuilt images only. No host ports are published and the collector gets
Docker's none network. All Docker resources use a unique suffix and are removed
when the run ends.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def docker(*args: str, timeout: int = 30, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def ensure_engine() -> None:
    if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode == 0:
        return
    start = subprocess.run(["colima", "start"], capture_output=True, text=True,
                           timeout=180)
    if start.returncode or subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ).returncode:
        raise RuntimeError(
            "Docker Engine is unavailable; colima start did not make it ready: "
            + start.stderr.strip()
        )


def inspect_image(image: str) -> str:
    try:
        return docker("image", "inspect", image, "--format", "{{.Id}}")
    except RuntimeError as exc:
        raise RuntimeError(
            f"Required image {image!r} is not present locally. This demo does "
            "not build or pull images; prepare the Cauce runtime and "
            "cauce-increment7-backend fixture first."
        ) from exc


def wait_container(container: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = json.loads(docker("inspect", container))[0]
        if not item["State"]["Running"]:
            return item
        time.sleep(0.1)
    docker("kill", container, check=False)
    raise RuntimeError(f"collector container {container} did not finish")


def collector_run(image: str, name: str, logs: str, state: str,
                  config: Path, *, operation: str) -> tuple[str, dict | None]:
    cid = docker(
        "run", "-d", "--name", name, "--network", "none",
        "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "32",
        "--memory", "128m", "--volume", f"{logs}:/var/log/cauce:ro",
        "--volume", f"{state}:/var/lib/cauce",
        "--volume", f"{config}:/etc/cauce/collector.toml:ro",
        "--entrypoint", "/usr/local/bin/cauce-collector", image,
        "--config", "/etc/cauce/collector.toml", operation,
    )
    item = wait_container(cid)
    code = item["State"]["ExitCode"]
    stdout = docker("logs", cid, check=False)
    if code != 0:
        raise RuntimeError(f"collector {operation} exited {code}: {stdout}")
    config_info = item["Config"]
    host = item["HostConfig"]
    if host["NetworkMode"] != "none" or config_info["User"] != "10001:10001":
        raise RuntimeError("collector did not run isolated and as UID 10001")
    docker("rm", cid)
    parsed = json.loads(stdout) if operation == "--status" and stdout else None
    return stdout, parsed


def node_json(image: str, network: str, javascript: str) -> object:
    output = docker(
        "run", "--rm", "--network", network, "--entrypoint", "node", image,
        "-e", javascript, timeout=45,
    )
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"fixture client returned invalid JSON: {output!r}") from exc


def request_ids(image: str, network: str, count: int) -> list[str]:
    js = r"""
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ids=[];
for (let i=0; i<COUNT; i++) {
  let response;
  if (i === 0) {
    for (let attempt=0; attempt<80; attempt++) {
      try {
        response=await fetch('http://proxy:8080/demo/'+i,
          {signal:AbortSignal.timeout(4000)});
        break;
      } catch (e) { await sleep(100); }
    }
  } else {
    response=await fetch('http://proxy:8080/demo/'+i,
      {signal:AbortSignal.timeout(4000)});
  }
  if (!response || response.status !== 200)
    throw new Error('proxy request failed: '+(response?.status ?? 'not ready'));
  const id=response.headers.get('x-request-id');
  if (!id) throw new Error('proxy response lacks x-request-id');
  await response.arrayBuffer();
  ids.push(id);
}
console.log(JSON.stringify(ids));
""".replace("COUNT", str(count))
    value = node_json(image, network, js)
    if not isinstance(value, list) or len(value) != count or not all(
        isinstance(item, str) and item for item in value
    ):
        raise RuntimeError(f"fixture client returned unexpected request IDs: {value!r}")
    return value


def wait_logged(image: str, logs: str, expected: set[str], timeout: float = 12) -> None:
    js = """const fs=require('node:fs');
const p='/logs/access.jsonl';
let ids=[];
if(fs.existsSync(p)) ids=fs.readFileSync(p,'utf8').split('\\n').filter(Boolean)
 .map(line=>{try{return JSON.parse(line).request_id}catch{return null}}).filter(Boolean);
console.log(JSON.stringify(ids));"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # A helper with network=none can still read the Docker log volume.
        raw = docker(
            "run", "--rm", "--network", "none", "--volume",
            f"{logs}:/logs:ro", "--entrypoint", "node", image, "-e", js,
        )
        observed = set(json.loads(raw))
        if expected <= observed:
            return
        time.sleep(0.15)
    raise RuntimeError("proxy responses did not appear in its JSONL access log")


def run_demo(args: argparse.Namespace) -> dict:
    ensure_engine()
    proxy_id = inspect_image(args.proxy_image)
    backend_id = inspect_image(args.backend_image)

    suffix = uuid.uuid4().hex[:10]
    network = f"cauce-demo-net-{suffix}"
    logs = f"cauce-demo-logs-{suffix}"
    state = f"cauce-demo-state-{suffix}"
    backend = f"cauce-demo-backend-{suffix}"
    proxy = f"cauce-demo-proxy-{suffix}"
    collectors: list[str] = []
    created_network = False
    created_volumes: list[str] = []
    temp_dir = ROOT / "work" / "offline-demo" / suffix
    temp_dir.mkdir(parents=True, exist_ok=False)
    config = temp_dir / "cauce.toml"
    collector_config = temp_dir / "collector.toml"
    template = (ROOT / "examples/docker.toml").read_text()
    template = template.replace('host = "localhost:8080"', 'host = "proxy:8080"')
    template = template.replace('address = "python:8000"', 'address = "backend:8000"')
    template = template.replace('host = "localhost:8000"', 'host = "backend:8000"')
    template = template.replace('address = "node:8001"', 'address = "backend:8000"')
    template = template.replace('host = "localhost:8001"', 'host = "backend:8000"')
    config.write_text(template)
    collector_text = (ROOT / "examples/collector.toml").read_text()
    collector_text = collector_text.replace(
        'access_log = "/var/log/cauce/access.jsonl"',
        'access_log = "/var/log/cauce/access.jsonl"',
    ).replace('state_path = "/var/lib/cauce/collector/state.db"',
              'state_path = "/var/lib/cauce/collector/state.db"')
    collector_config.write_text(collector_text)

    checks: dict[str, bool] = {}
    ids_first: list[str] = []
    ids_while_collector_absent: list[str] = []
    try:
        docker("network", "create", network)
        created_network = True
        for volume in (logs, state):
            docker("volume", "create", volume)
            created_volumes.append(volume)
        # Named volumes start root-owned. The existing Node fixture image has
        # BusyBox chown; no helper image is built or pulled.
        docker("run", "--rm", "--network", "none", "--user", "0:0",
               "--volume", f"{logs}:/logs", "--volume", f"{state}:/state",
               "--entrypoint", "chown", args.backend_image,
               "10001:10001", "/logs", "/state")
        docker("run", "-d", "--name", backend, "--network", network,
               "--network-alias", "backend", "--memory", "128m",
               "--pids-limit", "64", "--env", "PORT=8000",
               args.backend_image, "node", "/app/server.mjs")
        docker("run", "-d", "--name", proxy, "--network", network,
               "--network-alias", "proxy", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--pids-limit", "128",
               "--memory", "256m", "--cpus", "1", "--volume",
               f"{config}:/etc/cauce/cauce.toml:ro", "--volume",
               f"{logs}:/var/log/cauce", args.proxy_image,
               "/etc/cauce/cauce.toml")

        ids_first = request_ids(args.backend_image, network, 3)
        wait_logged(args.backend_image, logs, set(ids_first))
        first_name = f"cauce-demo-collector-a-{suffix}"
        collectors.append(first_name)
        collector_run(args.proxy_image, first_name, logs, state,
                      collector_config, operation="--once")
        checks["proxy_requests_captured_in_jsonl"] = True
        checks["collector_runs_without_network"] = True

        # The proxy must keep serving after the one-shot collector exits.
        ids_while_collector_absent = request_ids(args.backend_image, network, 1)
        wait_logged(args.backend_image, logs, set(ids_while_collector_absent))
        second_name = f"cauce-demo-collector-b-{suffix}"
        collectors.append(second_name)
        collector_run(args.proxy_image, second_name, logs, state,
                      collector_config, operation="--once")
        checks["proxy_serves_while_collector_stopped"] = True

        # A further process restart with no new source lines must not duplicate
        # the previously checkpointed events.
        third_name = f"cauce-demo-collector-c-{suffix}"
        collectors.append(third_name)
        collector_run(args.proxy_image, third_name, logs, state,
                      collector_config, operation="--once")
        status_name = f"cauce-demo-status-{suffix}"
        collectors.append(status_name)
        _, status = collector_run(
            args.proxy_image, status_name, logs, state,
            collector_config, operation="--status",
        )
        if not status or status.get("pending_events") != 4:
            raise RuntimeError(f"unexpected spool status: {status!r}")
        # --export requires a destination; run it directly with stdout and retain
        # the container inspection checks performed by collector_run.
        export_name = f"cauce-demo-export-{suffix}"
        collectors.append(export_name)
        docker("run", "-d", "--name", export_name, "--network", "none",
               "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--pids-limit", "32",
               "--memory", "128m", "--volume", f"{logs}:/var/log/cauce:ro",
               "--volume", f"{state}:/var/lib/cauce", "--volume",
               f"{collector_config}:/etc/cauce/collector.toml:ro", "--entrypoint",
               "/usr/local/bin/cauce-collector", args.proxy_image,
               "--config", "/etc/cauce/collector.toml", "--export", "stdout")
        item = wait_container(export_name)
        if item["State"]["ExitCode"] != 0:
            raise RuntimeError("collector export failed")
        if (item["HostConfig"]["NetworkMode"] != "none" or
                item["Config"]["User"] != "10001:10001"):
            raise RuntimeError("collector export did not run isolated")
        exported = docker("logs", export_name)
        docker("rm", export_name)
        batches = [json.loads(line) for line in exported.splitlines() if line.strip()]
        events = [event for batch in batches for event in batch.get("events", [])]
        event_request_ids = [event.get("request_id") for event in events]
        expected = ids_first + ids_while_collector_absent
        if len(events) != 4 or set(event_request_ids) != set(expected):
            raise RuntimeError(
                f"export did not contain each expected request exactly once: "
                f"count={len(events)}, ids={event_request_ids!r}"
            )
        if len(event_request_ids) != len(set(event_request_ids)):
            raise RuntimeError("collector restart produced duplicate request events")
        checks["checkpoint_restart_no_duplicates"] = True
        checks["export_matches_all_proxy_responses"] = True
        checks["spool_status_matches_export"] = status["pending_events"] == len(events)

        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(exported)
        return {
            "result": "PASS",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "images": {"proxy": args.proxy_image, "proxy_id": proxy_id,
                       "backend_fixture": args.backend_image,
                       "backend_id": backend_id},
            "network_published_ports": [],
            "collector_network_mode": "none",
            "collector_user": "10001:10001",
            "delivery": "offline spool",
            "checks": checks,
            "requests": len(expected),
            "events_exported": len(events),
            "unique_request_ids": len(set(event_request_ids)),
            "pending_events": status["pending_events"],
            "checkpoints": status["checkpoints"],
            "export_file": str(args.export),
            "connectivity_contacted": False,
        }
    finally:
        for name in collectors + [proxy, backend]:
            docker("rm", "-f", name, check=False)
        for volume in created_volumes:
            docker("volume", "rm", volume, check=False)
        if created_network:
            docker("network", "rm", network, check=False)
        try:
            config.unlink(missing_ok=True)
            collector_config.unlink(missing_ok=True)
            temp_dir.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-image", default="cauce:0.7-final-arm64")
    parser.add_argument("--backend-image", default="cauce-increment7-backend:local")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/increment8-demo.json")
    parser.add_argument("--export", type=Path, default=ROOT / "docs/increment8-demo-export.jsonl")
    args = parser.parse_args()
    if not args.report.is_absolute():
        args.report = ROOT / args.report
    if not args.export.is_absolute():
        args.export = ROOT / args.export
    try:
        result = run_demo(args)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n")
        print(f"PASS: offline Docker demo; report={args.report}")
        return 0
    except Exception as exc:
        failure = {
            "result": "FAIL",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
            "connectivity_contacted": False,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(failure, indent=2) + "\n")
        print(f"FAIL: {exc}; report={args.report}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
