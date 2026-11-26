#!/usr/bin/env python3
"""Build pinned Cauce revisions and run an unattended Linux HTTP/1.1 capacity campaign."""

import argparse
import csv
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile

from capacity import digest, write_json

PROGRAM = Path(__file__).resolve().parent
REPO = PROGRAM.parents[1]
ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}


def core(cpu):
    return (Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
            .joinpath("thread_siblings_list").read_text().strip())


def command(args, log, cwd=REPO, env=None):
    with log.open("wb") as output:
        with subprocess.Popen(args, cwd=cwd, env=env, stdout=output,
                              stderr=subprocess.STDOUT, start_new_session=True) as child:
            try:
                return child.wait()
            except BaseException:
                os.killpg(child.pid, signal.SIGINT)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                raise


def frequencies(cpus):
    values = {}
    for cpu in cpus:
        root = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq")
        values[cpu] = {p.name: p.read_text().strip() for p in root.glob("*")
                       if p.name in {"scaling_cur_freq", "scaling_min_freq", "scaling_max_freq",
                                     "scaling_governor", "cpuinfo_max_freq", "scaling_driver"}}
    return values


def build(env, source, args, log):
    cargo = ["cargo", f"+{args.toolchain}"]
    if command([*cargo, "build", "--release", "--locked", "--bins"], log, source, env):
        raise RuntimeError(f"build failed. See {log}")


def extract(revision, destination):
    destination.mkdir()
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "archive", revision], cwd=REPO, stdout=archive, check=True)
        archive.seek(0)
        subprocess.run(["tar", "-xf", "-", "-C", str(destination)], stdin=archive, check=True)


def curves(output, cells):
    fields = ["revision", "workers", "logs", "label", "rate", "seconds", "goodput_per_s",
              "p99_ns", "p999_ns", "scheduler_p99_ns", "errors", "invalid", "cpu_us_per_success",
              "rss_peak_sampled_bytes"]
    with (output / "capacity.csv").open("w") as file:
        writer = csv.DictWriter(file, fields)
        writer.writeheader()
        for cell in cells:
            for row in cell["report"].get("cells", []):
                latency = row.get("scheduled_latency_ns", {})
                writer.writerow({
                    **{key: cell[key] for key in ("revision", "workers", "logs")},
                    **{key: row.get(key) for key in ("label", "rate", "seconds", "goodput_per_s",
                                                   "scheduler_p99_ns", "cpu_us_per_success")},
                    "p99_ns": latency.get("p99"), "p999_ns": latency.get("p99.9"),
                    "errors": row["offered"] - row["successful"] if "offered" in row else None,
                    "invalid": json.dumps(row["invalid_reasons"]),
                    "rss_peak_sampled_bytes": row.get("process", {}).get("rss_peak_sampled_bytes"),
                })
    with (output / "scaling.csv").open("w") as file:
        writer = csv.writer(file)
        writer.writerow(["revision", "logs", "workers", "confirmed_goodput_per_s",
                         "bracketed", "scaling_efficiency"])
        for cell in cells:
            report = cell["report"]
            goodput = report.get("confirmed_goodput_per_s")
            baseline = next((c["report"] for c in cells if c["revision"] == cell["revision"]
                             and c["logs"] == cell["logs"] and c["workers"] == 1), {})
            bracketed = report.get("interpretation") == "confirmed_bracket"
            efficiency = (goodput / (baseline["confirmed_goodput_per_s"] * cell["workers"])
                          if bracketed and baseline.get("interpretation") == "confirmed_bracket"
                          else None)
            writer.writerow([cell["revision"], cell["logs"], cell["workers"], goodput,
                             bracketed, efficiency])


def cpu_quotas():
    mount = Path("/sys/fs/cgroup")
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if not line.startswith("0::"):
            continue
        quotas = []
        group = mount / line[3:].lstrip("/")
        for path in (group, *group.parents):
            if not path.is_relative_to(mount):
                break
            limit = path / "cpu.max"
            if limit.is_file():
                quota, period = limit.read_text().split()
                if quota != "max":
                    quotas.append({"group": str(path.relative_to(mount)), "cpus": int(quota) / int(period)})
        return quotas
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--strategy", choices=("compact", "sharded"), default="compact")
    parser.add_argument("--endpoints", type=int, choices=range(1, 1025), metavar="1..1024", default=1)
    parser.add_argument("--revision", action="append", help="committed Cauce revision, repeat to compare")
    parser.add_argument("--toolchain", default="1.89.0")
    parser.add_argument("--proxy-cpus", type=int, nargs="+", required=True)
    parser.add_argument("--generator-cpus", type=int, nargs="+", required=True)
    parser.add_argument("--backend-cpu", type=int, required=True)
    parser.add_argument("--sampler-cpu", type=int, required=True)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--rates", type=int, nargs="+", default=[100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000])
    parser.add_argument("--seconds", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--confirm-seconds", type=int, default=60)
    parser.add_argument("--stability-seconds", type=int, default=0)
    parser.add_argument("--refine-steps", type=int, default=6)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--queue", type=int, default=64)
    parser.add_argument("--logs", choices=("off", "on", "both"), default="off")
    parser.add_argument("--smoke", action="store_true", help="classify as a smoke run and permit fewer than eight CPUs")
    parser.add_argument("--proxy-bin", type=Path, help="use an already built proxy (smoke only)")
    parser.add_argument("--generator-bin", type=Path, help="use an already built generator (smoke only)")
    args = parser.parse_args()
    arch = ARCHES.get(platform.machine())
    cpus = [*args.proxy_cpus, *args.generator_cpus, args.backend_cpu, args.sampler_cpu]
    if (sys.platform != "linux" or arch is None or len(cpus) != len(set(cpus))
            or not set(cpus) <= os.sched_getaffinity(0)):
        parser.error("a Linux ARM64 or AMD64 host and disjoint available CPUs are required")
    if (not args.smoke and (len(cpus) < 8 or args.confirm_seconds < 60)
            or not 1 <= min(args.workers) <= max(args.workers) <= len(args.proxy_cpus)
            or len(set(args.workers)) != len(args.workers)):
        parser.error("provide CPUs for each worker and at least eight CPUs and 60s confirmations")
    role_cores = [{core(cpu) for cpu in role} for role in
                  (args.proxy_cpus, args.generator_cpus, [args.backend_cpu], [args.sampler_cpu])]
    if not args.smoke and sum(map(len, role_cores)) != len(set.union(*role_cores)):
        parser.error("SMT siblings cannot be assigned to different benchmark roles")
    quotas = cpu_quotas()
    if not args.smoke and any(limit["cpus"] < len(cpus) for limit in quotas or []):
        parser.error("effective cgroup CPU quota does not cover the configured roles")
    if (args.proxy_bin or args.generator_bin) and not args.smoke:
        parser.error("prebuilt override is available for smoke runs")
    if args.proxy_bin and args.revision:
        parser.error("cannot assign a source revision to an override binary")
    args.output.mkdir(parents=True)
    output = args.output.resolve()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("CARGO_PROFILE_RELEASE_", "CARGO_TARGET_")) or key in {
            "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CAUCE_TRANSPORT_DIAGNOSTICS"
        }:
            del env[key]
    env["CARGO_INCREMENTAL"] = "0"
    state = {"arch": arch, "smoke": args.smoke, "strategy": args.strategy, "endpoints": args.endpoints, "roles": {
        "proxy": args.proxy_cpus, "generator": args.generator_cpus, "backend": args.backend_cpu,
        "sampler": args.sampler_cpu}, "toolchain": args.toolchain,
        "frequency_before": frequencies(cpus), "cgroup_v2_cpu_quotas": quotas}
    cells = []
    try:
        if command(["sh", str(PROGRAM / "linux-environment.sh")], output / "environment.txt", env=env):
            raise RuntimeError("could not record Linux environment")
        with tempfile.TemporaryDirectory(prefix="cauce-bench-build-") as workspace:
            workspace = Path(workspace)
            if args.generator_bin:
                generator = args.generator_bin.resolve()
            else:
                source = workspace / "load"
                shutil.copytree(PROGRAM / "load", source, ignore=shutil.ignore_patterns("target"))
                build(env, source, args, output / "generator-build.log")
                generator = source / "target/release/cauce-load"
            state["generator_sha256"] = digest(generator)
            state["generator_lock_sha256"] = digest(PROGRAM / "load/Cargo.lock")
            state["generator_source_sha256"] = digest(PROGRAM / "load/src/main.rs")
            for rev in args.revision or ["HEAD"]:
                if args.proxy_bin:
                    revision, proxy = "prebuilt", args.proxy_bin.resolve()
                else:
                    revision = subprocess.check_output(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
                                                       cwd=REPO, text=True).strip()
                    source = workspace / revision
                    extract(revision, source)
                    if args.strategy == "sharded" and "CAUCE_POOL_STRATEGY" not in (source / "src/pool.rs").read_text():
                        raise RuntimeError("selected revision has no sharded pool")
                    build(env, source, args, output / f"{revision}-build.log")
                    proxy = source / "target/release/cauce"
                    state.setdefault("proxy_locks", {})[revision] = digest(source / "Cargo.lock")
                for workers in args.workers:
                    for logs in ("on", "off") if args.logs == "both" else (args.logs,):
                        key = f"{revision[:12]}-{workers}w-{logs}"
                        dest = output / key
                        rc = command([
                            sys.executable,
                            str(PROGRAM / "capacity-workload.py"), str(proxy), str(generator), str(dest),
                            "--protocol", "http1", "--strategy", args.strategy, "--endpoints", str(args.endpoints),
                            "--workers", str(workers), "--proxy-cpus",
                            *map(str, args.proxy_cpus[:workers]), "--client-cpu", *map(str, args.generator_cpus),
                            "--backend-cpu", str(args.backend_cpu), "--sampler-cpu", str(args.sampler_cpu),
                            "--seconds", str(args.seconds), "--warmup", str(args.warmup),
                            "--confirm-seconds", str(args.confirm_seconds), "--p99-ms", "20", "--p999-ms", "100",
                            "--stability-seconds", str(args.stability_seconds),
                            "--refine-steps", str(args.refine_steps), "--concurrency", str(args.concurrency),
                            "--queue", str(args.queue), "--rates", *map(str, args.rates),
                            *(["--logs"] if logs == "on" else []),
                        ], output / f"{key}.log", env=env)
                        result = dest / "measurements/campaign.json"
                        report = json.loads(result.read_text()) if result.is_file() else {}
                        cells.append({"revision": revision, "workers": workers, "logs": logs,
                                      "exit_status": rc, "report": report})
        return 0 if all(c["exit_status"] == 0 for c in cells) else 2
    finally:
        state.update(frequency_after=frequencies(cpus), cells=cells)
        write_json(output / "native-campaign.json", state)
        curves(output, cells)
        files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
        (output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.relative_to(output)}\n" for path in files))


if __name__ == "__main__":
    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
