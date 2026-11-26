#!/usr/bin/env python3
"""Verify an existing collector database and real ENOSPC on a small disposable volume."""

import argparse
from contextlib import closing
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def snapshot(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        mode, = connection.execute("PRAGMA journal_mode").fetchone()
        integrity, = connection.execute("PRAGMA integrity_check").fetchone()
        offset, = connection.execute("SELECT offset FROM checkpoints").fetchone()
        batches = [json.loads(body) for (body,) in connection.execute("SELECT body FROM spool ORDER BY seq")]
    assert mode == "delete" and integrity == "ok", (mode, integrity)
    return {"offset": offset, "events": [event["event_id"] for batch in batches for event in batch["events"]],
            "journal_mode": mode, "integrity": integrity}


def fill(filler):
    seen = False
    with filler.open("xb", buffering=0) as output:
        for size in (1024 * 1024, 4096, 512):
            block = bytes(size)
            try:
                while output.write(block) > 0:
                    pass
            except OSError as error:
                if error.errno != errno.ENOSPC:
                    raise
                seen = True
    assert seen, "fixture did not reach ENOSPC"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    filesystem = os.statvfs(scratch)
    if (ROOT not in scratch.parents or not scratch.is_mount()
            or filesystem.f_frsize * filesystem.f_blocks > 32 * 1024 * 1024):
        parser.error("scratch must be a dedicated mount of at most 32 MiB inside Cauce")
    previous, collector = args.previous.resolve(), args.collector.resolve()
    fixture = json.loads((ROOT / "examples/access-log.jsonl").read_text().splitlines()[0])
    records = [{**fixture, "event_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4())} for _ in range(65)]
    lines = [json.dumps(event).encode() + b"\n" for event in records]
    with tempfile.TemporaryDirectory(prefix="sqlite-qualification-", dir=scratch) as folder:
        fixture_root = Path(folder)
        access, database, config = (fixture_root / name for name in ("access.jsonl", "state.db", "collector.toml"))
        config.write_text(f"""[collector]
access_log = {json.dumps(str(access))}
state_path = {json.dumps(str(database))}
spool_max_bytes = 131072
db_max_bytes = 262144
batch_max_events = 128
batch_max_bytes = 65536
max_line_bytes = 8192
delivery = "spool"
""")
        access.write_bytes(lines[0])

        def collect(binary, check=True):
            return subprocess.run([binary, "--config", config, "--once"], cwd=fixture_root,
                                  capture_output=True, text=True, check=check, timeout=10)

        collect(previous)
        baseline = snapshot(database)
        assert baseline["events"] == [records[0]["event_id"]] and baseline["offset"] == len(lines[0])
        collect(collector)
        assert snapshot(database) == baseline, "new collector changed the old durable state without new input"
        with access.open("ab") as log:
            log.writelines(lines[1:])
        filler = fixture_root / "filler"
        try:
            fill(filler)
            failed = collect(collector, check=False)
            assert failed.returncode != 0 and any(message in failed.stderr.lower() for message in (
                "database or disk is full", "unable to open database file",
            )), failed.stderr
        finally:
            filler.unlink(missing_ok=True)
        assert snapshot(database) == baseline, "ENOSPC advanced the durable checkpoint or spool"
        collect(collector)
        recovered = snapshot(database)
        assert recovered["offset"] == access.stat().st_size
        assert recovered["events"] == [record["event_id"] for record in records]
        print(json.dumps({"linked_sqlite": subprocess.check_output([collector, "--sqlite-version"], text=True).strip(),
                          "filesystem_capacity_bytes": filesystem.f_frsize * filesystem.f_blocks,
                          "events_before": len(baseline["events"]), "offset_before": baseline["offset"],
                          "collector_failure": failed.stderr.split("\n\nStack backtrace:")[0].strip(),
                          "enospc_confirmed_by_os": True, "durable_state_preserved": True,
                          "events_after_recovery": len(recovered["events"]), "offset_after": recovered["offset"],
                          "integrity": recovered["integrity"], "journal_mode": recovered["journal_mode"]}, indent=2))


if __name__ == "__main__":
    main()
