#!/usr/bin/env python3
"""Storage/checkpoint edge cases independent of the main collector HTTP suite."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CASES = []
FAILURES = []
spec = importlib.util.spec_from_file_location("collector_suite", ROOT / "scripts/test-collector.py")
suite_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = suite_module
spec.loader.exec_module(suite_module)


def check(name, condition, details=None):
    if not condition:
        FAILURES.append({"case": name, "details": details or "assertion failed"})
        print("FAIL:", name, json.dumps(details or "assertion failed"), flush=True)
        return
    CASES.append(name)
    print("PASS:", name, flush=True)


def event_line(suite, event):
    return json.dumps(event, separators=(",", ":")) + "\n"


def container_json(suite, code, path):
    result = suite_module.run("docker", "exec", suite.sink_name, "python", "-c", code,
                              suite.path(path))
    return json.loads(result.stdout)


def file_bytes(suite, path):
    if not suite.args.docker:
        return path.read_bytes()
    result = subprocess.run(["docker", "exec", suite.sink_name, "python", "-c",
                             "import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
                             suite.path(path)], capture_output=True, check=True)
    return result.stdout


def sync_docker_state(suite, path, expect_mode=False):
    if not suite.args.docker:
        return
    import hashlib
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    code = "import hashlib,json,os,pathlib,sys;p=pathlib.Path(sys.argv[1]);print(json.dumps([hashlib.sha256(p.read_bytes()).hexdigest(),os.stat(p).st_mode&0o777]))"
    for _ in range(100):
        seen = subprocess.run(["docker", "exec", suite.sink_name, "python", "-c", code,
                               suite.path(path)], capture_output=True, text=True)
        if seen.returncode == 0:
            try:
                actual_hash, actual_mode = json.loads(seen.stdout)
            except (ValueError, TypeError):
                actual_hash, actual_mode = None, None
        else:
            actual_hash, actual_mode = None, None
        mode_matches = not expect_mode or actual_mode == (path.stat().st_mode & 0o777)
        if actual_hash == expected and mode_matches:
            return
        time.sleep(0.05)
    raise AssertionError(f"Colima state file did not synchronize: {path.name}")


def db_rows(suite, path):
    if suite.args.docker:
        code = """import json,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
batches=[json.loads(r[0]) for r in db.execute('SELECT body FROM spool ORDER BY seq')]
checkpoints=list(db.execute('SELECT dev,ino,path,offset,observed_size FROM checkpoints'))
print(json.dumps([batches,checkpoints]))
"""
        return container_json(suite, code, path)
    with sqlite3.connect(path) as db:
        batches = [json.loads(row[0]) for row in db.execute("SELECT body FROM spool ORDER BY seq")]
        checkpoints = list(db.execute("SELECT dev,ino,path,offset,observed_size FROM checkpoints"))
    return batches, checkpoints


def file_identity(suite, path):
    if not suite.args.docker:
        stat = path.stat()
        return stat.st_dev, stat.st_ino
    value = container_json(suite,
                           "import json,os,sys;s=os.stat(sys.argv[1]);print(json.dumps([s.st_dev,s.st_ino]))",
                           path)
    return value[0], value[1]


def invoke(suite, config, *flags, sync=True, ok=True):
    if sync:
        suite.sync_inputs(config)
    p = subprocess.run(suite.command(config, *flags), capture_output=True, text=True, timeout=20)
    if ok and p.returncode:
        raise AssertionError(f"collector failed ({p.returncode}): {p.stderr}")
    return p


def test(suite):
    # A state database is bound to its first canonical source path, so changing
    # a config cannot silently reuse another file's checkpoint.
    d, config, cfgpath, first = suite.new("source-path", events=0)
    first_event = suite.event()
    first_event["upstream_id"] = "long-source-" + "x" * 200
    (d / "access.jsonl").write_text(event_line(suite, first_event))
    suite.invoke(cfgpath, "--once")
    original_cfgpath = cfgpath
    second_path = d / "other.jsonl"
    second_event = suite.event()
    second_path.write_text(event_line(suite, second_event))
    config["access_log"] = suite.path(second_path)
    cfgpath = suite.config(d, config)
    changed = suite.invoke(cfgpath, "--once", ok=False)
    exported = [e for batch in suite.batches(original_cfgpath) for e in batch["events"]]
    check("state database rejects a changed source path without consuming it",
          changed.returncode != 0 and {e["event_id"] for e in exported} == {first_event["event_id"]},
          {"returncode": changed.returncode, "stderr": changed.stderr[-400:], "queued": exported})

    d, config, cfgpath, _ = suite.new("state-symlink-alias", events=1)
    source = d / "access.jsonl"
    alias = d / "state-alias.db"
    alias.symlink_to(source)
    original_source = file_bytes(suite, source)
    config["state_path"] = suite.path(alias)
    alias_config = suite.config(d, config)
    p = suite.invoke(alias_config, "--once", ok=False)
    check("state path symlink to access log is rejected before opening SQLite",
          p.returncode != 0 and file_bytes(suite, source) == original_source,
          {"returncode": p.returncode, "stderr": p.stderr[-400:]})

    d, config, cfgpath, _ = suite.new("rotation-race", events=0, poll_ms=2, max_files=8)
    source = d / "access.jsonl"
    rotated = d / "access.jsonl.1"
    source.touch()
    suite.invoke(cfgpath, "--once")
    name = suite.network + "-rotation-race-" + uuid.uuid4().hex[:6]
    worker = subprocess.Popen(suite.command(cfgpath, name=name), stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
    if suite.args.docker:
        suite.active.append(name)
    try:
        if suite.args.docker:
            code = """import pathlib,sys,time
source=pathlib.Path(sys.argv[1]);rotated=pathlib.Path(sys.argv[2])
for _ in range(180):
 if source.exists():
  try: source.replace(rotated)
  except FileNotFoundError: continue
  time.sleep(.002)
  rotated.replace(source)
 time.sleep(.002)
"""
            suite_module.run("docker", "exec", suite.sink_name, "python", "-c", code,
                             suite.path(source), suite.path(rotated))
        else:
            # The test exercises rotation while the tailer owns its state,
            # after startup has finished canonicalizing the configured path.
            import fcntl
            deadline = time.monotonic() + 6
            with (d / "state.lock").open("rb") as ownership:
                while True:
                    try:
                        fcntl.flock(ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        break
                    fcntl.flock(ownership, fcntl.LOCK_UN)
                    if worker.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError("collector did not acquire state ownership")
                    time.sleep(0.01)
            for _ in range(180):
                if source.exists():
                    try:
                        os.replace(source, rotated)
                    except FileNotFoundError:
                        continue
                    time.sleep(0.002)
                    os.replace(rotated, source)
                time.sleep(0.002)
        if suite.args.docker:
            suite_module.run("docker", "stop", "--time", "3", name)
        else:
            worker.terminate()
        _, stderr = worker.communicate(timeout=10)
        check("same-inode atomic rename race does not crash the tailer",
              worker.returncode == 0,
              {"returncode": worker.returncode, "stderr_start": (stderr or "")[:1000],
               "stderr_end": (stderr or "")[-400:]})
    finally:
        if worker.poll() is None:
            if suite.args.docker:
                suite_module.run("docker", "kill", name)
            else:
                worker.kill()
            worker.wait(timeout=10)

    # Commit each filler insert so one SQLITE_FULL attempt does not roll back
    # the whole padding transaction.
    d, config, cfgpath, _ = suite.new("physical-db-full", events=0, spool_max_bytes=524_288,
                                      db_max_bytes=1_048_576, batch_max_events=4,
                                      batch_max_bytes=32768, max_line_bytes=32768)
    suite.invoke(cfgpath, "--status")
    state = d / "state.db"
    fill_error = None
    with sqlite3.connect(state) as db:
        db.execute("PRAGMA max_page_count=256")
        db.execute("CREATE TABLE filler(value BLOB)")
        for _ in range(200):
            try:
                db.execute("INSERT INTO filler(value) VALUES(zeroblob(12000))")
                db.commit()
            except sqlite3.OperationalError as exc:
                fill_error = str(exc)
                db.rollback()
                break
        for _ in range(100):
            if db.execute("PRAGMA page_count").fetchone()[0] >= 256 and fill_error:
                break
            try:
                db.execute("INSERT INTO filler(value) VALUES(zeroblob(1000))")
                db.commit()
            except sqlite3.OperationalError as exc:
                fill_error = str(exc)
                db.rollback()
                break
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        max_pages = db.execute("PRAGMA max_page_count").fetchone()[0]
        check("disk-full fixture reached the configured SQLite page ceiling",
              pages == 256 and fill_error == "database or disk is full",
              {"page_count": pages, "max_page_count": max_pages,
                            "db_bytes": state.stat().st_size, "fill_error": fill_error})
    source = d / "access.jsonl"
    full_events = []
    for _ in range(4):
        large = suite.event()
        large["upstream_id"] = "u" * 256
        large["path"] = "/" + "p" * 255
        large["reason"] = "r" * 256
        full_events.append(large)
    source.write_text("".join(event_line(suite, event) for event in full_events))
    sync_docker_state(suite, state)
    p = suite.invoke(cfgpath, "--once", ok=False)
    status = suite.status(cfgpath)
    batches, checkpoints = db_rows(suite, state)
    check("SQLite FULL rolls back spool and source checkpoint together",
          p.returncode != 0 and status["pending_events"] == 0 and not batches and not checkpoints,
          {"returncode": p.returncode, "stderr": p.stderr[-400:], "status": status,
           "checkpoints": checkpoints})

    # Same-inode truncate followed by equal-or-larger replacement cannot be
    # caught by checking only current_size < byte_offset. A sample of the old
    # bytes/fingerprint or an equivalent strategy is needed to fail closed.
    d, config, cfgpath, _ = suite.new("copytruncate-grow", events=0)
    old_event = suite.event()
    old_event["upstream_id"] = "old-" + "x" * 200
    source = d / "access.jsonl"
    source.write_text(event_line(suite, old_event))
    suite.invoke(cfgpath, "--once")
    replacement = suite.event()
    replacement["upstream_id"] = "new-" + "y" * 200
    new_line = event_line(suite, replacement)
    old_size = source.stat().st_size
    with source.open("r+") as f:
        f.seek(0)
        f.write(new_line)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    checkpoint_rows = db_rows(suite, d / "state.db")[1]
    sync_docker_state(suite, source)
    _, source_ino = file_identity(suite, source)
    check("copytruncate fixture preserves inode and is at least the prior byte offset",
          source_ino == checkpoint_rows[0][1] and source.stat().st_size >= old_size,
          {"old_size": old_size, "new_size": source.stat().st_size})
    suite.sync_inputs(cfgpath)
    p = suite.invoke(cfgpath, "--once", ok=False)
    batches, _ = db_rows(suite, d / "state.db")
    found = {e["event_id"] for batch in batches for e in batch["events"]}
    check("copytruncate with same-size replacement is detected before loss",
          p.returncode != 0 and replacement["event_id"] not in found and old_event["event_id"] in found,
          {"returncode": p.returncode, "stderr": p.stderr[-400:], "queued": sorted(found)})

    # Spool fills while the active source grows. If rotation then evicts that
    # generation, loss must be reported rather than silently forgetting the inode.
    d, config, cfgpath, _ = suite.new("gap-on-full", events=0, spool_max_bytes=1024,
                                      db_max_bytes=262144, batch_max_events=1,
                                      batch_max_bytes=1024, max_line_bytes=1024, max_files=2)
    source = d / "access.jsonl"
    first_event, pending_event = suite.event(), suite.event()
    source.write_text(event_line(suite, first_event))
    suite.invoke(cfgpath, "--once")
    check("first batch is durable before quota test", suite.status(cfgpath)["pending_events"] == 1)
    pending_line = event_line(suite, pending_event)
    with source.open("a") as f:
        f.write(pending_line)
        f.flush()
        os.fsync(f.fileno())
    paused = suite.invoke(cfgpath, "--once")
    check("full spool pauses source at its prior durable checkpoint",
          suite.status(cfgpath)["pending_events"] == 1,
          {"returncode": paused.returncode, "stderr": paused.stderr[-400:]})
    # Simulate retention deleting this inode before a later collector poll.
    if suite.args.docker:
        code = "import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.unlink();p.write_text('')"
        suite_module.run("docker", "exec", suite.sink_name, "python", "-c", code,
                         suite.path(source))
        first_missing = invoke(suite, cfgpath, "--once", sync=False)
        time.sleep(0.075)
        p = invoke(suite, cfgpath, "--once", sync=False, ok=False)
    else:
        source.unlink()
        source.write_text("")
        first_missing = suite.invoke(cfgpath, "--once")
        time.sleep(0.075)
        p = suite.invoke(cfgpath, "--once", ok=False)
    check("evicted generation with observed but unspooled bytes fails closed after debounce",
          first_missing.returncode == 0 and p.returncode != 0
          and ("disappeared" in p.stderr or "gap" in p.stderr.lower()),
          {"first_missing_returncode": first_missing.returncode,
           "second_returncode": p.returncode, "stderr": p.stderr[-400:]})

    # Malformed existing DB must remain untouched; no create/repair/reseed path
    # may advance source state after corruption.
    d, _, cfgpath, _ = suite.new("corrupt-db", events=1)
    suite.invoke(cfgpath, "--once")
    state = d / "state.db"
    state.write_bytes(b"not a SQLite database\n")
    sync_docker_state(suite, state)
    damaged = file_bytes(suite, state)
    p = suite.invoke(cfgpath, "--status", ok=False)
    check("corrupt state fails closed and is left intact", p.returncode != 0 and file_bytes(suite, state) == damaged,
          {"returncode": p.returncode, "stderr": p.stderr[-400:]})

    # A read-only state database plus new source data must fail without moving
    # the durable checkpoint. The permission is restored in finally.
    d, _, cfgpath, _ = suite.new("readonly-db", events=1)
    suite.invoke(cfgpath, "--once")
    state = d / "state.db"
    source = d / "access.jsonl"
    source.write_text(source.read_text() + event_line(suite, suite.event()))
    before = file_bytes(suite, state)
    mode = state.stat().st_mode
    try:
        state.chmod(0o444)
        sync_docker_state(suite, state, expect_mode=True)
        p = suite.invoke(cfgpath, "--once", ok=False)
        check("read-only checkpoint storage fails without accepting new input",
              p.returncode != 0 and file_bytes(suite, state) == before,
              {"returncode": p.returncode, "stderr": p.stderr[-400:]})
    finally:
        state.chmod(mode)

    d, config, cfgpath, _ = suite.new("endpoint-pin", delivery="proposal-http")
    suite.invoke(cfgpath, "--once")
    received_before = len(suite.received())
    config["endpoint"] = config["endpoint"].replace("/ingest", "/other")
    changed_config = suite.config(d, config)
    p = suite.invoke(changed_config, "--status", ok=False)
    check("state database rejects endpoint changes before sending a request",
          p.returncode != 0 and len(suite.received()) == received_before,
          {"returncode": p.returncode, "stderr": p.stderr[-400:]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--image", default="cauce-collector:test")
    parser.add_argument("--binary", type=Path, default=ROOT / "target/debug/cauce-collector")
    args = parser.parse_args()
    suite = suite_module.Suite(args)
    try:
        test(suite)
        architecture = (suite_module.run("docker", "image", "inspect", args.image,
                                         "--format", "{{.Architecture}}").stdout.strip()
                        if args.docker else os.uname().machine)
        image_id = (suite_module.run("docker", "image", "inspect", args.image,
                                     "--format", "{{.Id}}").stdout.strip()
                    if args.docker else None)
        result = {"result": "PASS" if not FAILURES else "FAIL",
                  "mode": "docker" if args.docker else "native",
                  "architecture": architecture, "fixture": "Local proposal-1 only; no Connectivity ingestion",
                  "scope": f"{('Docker' if args.docker else 'native')} independent storage verification; local proposal-1 only; no Connectivity ingestion",
                  "cases": CASES, "failures": FAILURES}
        if args.docker:
            result["image_id"] = image_id
        else:
            result["binary_sha256"] = hashlib.sha256(args.binary.read_bytes()).hexdigest()
        output = ROOT / "docs" / f"collector-storage-{'docker' if args.docker else 'native'}.json"
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(f"{result['result']}: {len(CASES)} collector storage cases, "
              f"{len(FAILURES)} failures ({architecture})")
        if FAILURES:
            raise SystemExit(1)
    finally:
        suite.close()


if __name__ == "__main__":
    main()
