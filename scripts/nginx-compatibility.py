#!/usr/bin/env python3
"""Audit the pinned registry and run reusable NGINX loopback comparisons."""

import argparse
import base64
from collections import Counter
from contextlib import ExitStack, contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "compatibility/nginx-gap-backlog.jsonl"
MANIFEST = ROOT / "compatibility/verification-manifests.json"
sys.path.insert(0, str(ROOT / "tests/compatibility"))
sys.path.insert(1, str(ROOT / "tests"))
import fixtures


def digest(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def object_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def local_output(value):
    result = Path(value).resolve()
    if ROOT not in result.parents:
        raise argparse.ArgumentTypeError("output must be inside the Cauce repository")
    return result


def queue(rows):
    for row in rows:
        if row["kind"] != "directive" or row["verified_equivalent"]:
            continue
        fixture = fixtures.MODULES.get(row["module"])
        yield {
            "id": row.get("task_id"),
            "reference": row["reference"],
            "module": row["module"],
            "directive": row["directive"],
            "context": row["context"],
            "priority": row["priority"],
            "status": row["status"],
            "assessment": row.get("assessment", row["status"]),
            "source": row["source"],
            "steps": ["review_source", "compare_existing_behavior", "fill_missing_semantics",
                      "verify_differential_cases", "update_registry"],
            "test_arguments": ["test", "--module", row["module"]] if fixture else None,
        }


def report():
    rows = [json.loads(line) for line in REGISTRY.read_text().splitlines()]
    counts = Counter((r["reference"], r["kind"], r["status"]) for r in rows)
    assessments = Counter((r["reference"], r["kind"], r.get("assessment", r["status"])) for r in rows)
    return {
        "reference_version": "1.30.8",
        "registry_sha256": digest(REGISTRY),
        "counts": [{"reference": reference, "kind": kind, "status": status, "count": count}
                   for (reference, kind, status), count in sorted(counts.items())],
        "verified_equivalent": sum(r["verified_equivalent"] for r in rows),
        "assessments": [{"reference": ref, "kind": kind, "assessment": value, "count": count}
                        for (ref, kind, value), count in sorted(assessments.items())],
        "fixture_modules": sorted(fixtures.MODULES),
    }


def manifest(registration):
    reviewed = json.loads(MANIFEST.read_text())["registrations"]
    for line in REGISTRY.read_text().splitlines():
        row = json.loads(line)
        if row.get("task_id") != registration:
            continue
        contract = reviewed.get(registration)
        common = ("valid_arguments", "invalid_arguments", "defaults", "contexts",
                  "inheritance", "boundary", "interactions", "observable", "reload")
        return {
            "registration": registration, "module": row["module"], "source": row["source"],
            "reference_line": row["registration"]["line"], "context": row["context"],
            "arity": row["accepted_arity"], "argument_grammar": row["arguments"],
            "default": row["reference_default"], "inheritance": row["reference_inheritance"],
            "assessment": row["assessment"], "executed_evidence": row["evidence"],
            "known_differences": row["semantic_differences"], "contract": contract,
            "obligations_pending_review": [] if contract else list(common),
        }
    raise ValueError(f"unknown directive registration {registration}")


def available_ports(count):
    with ExitStack() as sockets:
        listeners = [sockets.enter_context(socket.socket()) for _ in range(count)]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners]


@contextmanager
def launch(binary, args, port, folder):
    stderr = folder / "stderr.log"
    began = time.monotonic()
    with stderr.open("wb") as log:
        process = subprocess.Popen([binary, *args], cwd=folder, stdout=log, stderr=log)
    lifecycle = {"started": False, "forced_kill": False}
    try:
        deadline = time.monotonic() + 8
        while process.poll() is None and time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 0.1):
                    lifecycle["started"] = True
                    lifecycle["ready_seconds"] = time.monotonic() - began
                    break
            except OSError:
                time.sleep(0.025)
        if not lifecycle["started"]:
            raise RuntimeError(f"{binary.name} startup failed\n{stderr.read_text()[-3000:]}")
        yield process, lifecycle
    finally:
        before = time.monotonic()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            lifecycle["forced_kill"] = True
            process.kill()
            process.wait()
        lifecycle.update(exit_status=process.returncode, shutdown_seconds=time.monotonic() - before)


def resource_sample(process):
    result = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(process.pid)],
                            capture_output=True, text=True, check=False)
    fields = result.stdout.split()
    if len(fields) != 2:
        return None
    resources = {"rss_kib": int(fields[0]), "cpu_time_ps": fields[1]}
    if sys.platform.startswith("linux"):
        proc = Path("/proc") / str(process.pid)
        per_process = {"Rss:": "smaps_rss_kib", "Pss:": "pss_kib", "Private_Dirty:": "private_dirty_kib"}
        try:
            for line in (proc / "smaps_rollup").read_text().splitlines():
                key, _, value = line.partition(" ")
                if key in per_process:
                    resources[per_process[key]] = int(value.split()[0])
            stats = (proc / "stat").read_text().rpartition(")")[2].split()
            ticks = int(stats[11]) + int(stats[12])
            resources["cpu_seconds"] = ticks / os.sysconf("SC_CLK_TCK")
        except OSError:
            pass
    return resources


def observe(port, case):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request(case.get("method", "GET"), case["target"], body=case.get("request_body"),
                     headers={"Host": case.get("host", "compat.test"), **case.get("request_headers", {})})
        result = conn.getresponse()
        body = result.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise RuntimeError("fixture response exceeds 1 MiB")
        headers = {}
        for name, value in result.getheaders():
            headers.setdefault(name.lower(), []).append(value)
        observation = {"status": result.status,
                       "headers": {name: headers.get(name, []) for name in case.get("headers", ())}}
        if case.get("compare_body", True):
            observation["body_base64"] = base64.b64encode(body).decode()
        raw = {"status": result.status, "headers": result.getheaders(),
               "body_base64": base64.b64encode(body).decode()}
        return observation, sorted(set(headers) - observation["headers"].keys()), raw
    finally:
        conn.close()


def run_module(module, nginx, cauce, folder, proof=None):
    cauce_port, nginx_port, admin = available_ports(3)
    config = tomllib.loads((ROOT / "examples/cauce.toml").read_text())
    config.update(listen=f"127.0.0.1:{cauce_port}", admin=f"127.0.0.1:{admin}", upstreams=[], routes=[])
    config["logs"]["enabled"] = False
    with ExitStack() as resources:
        factory = fixtures.PROOF_FIXTURES[proof["fixture"]] if proof else fixtures.MODULES[module]
        reference, cases, rejected, *reload = factory(config, nginx_port, resources, folder)
        folders = {key: folder / key for key in ("nginx", "cauce")}
        for runtime in folders.values():
            runtime.mkdir(parents=True)
        (folders["nginx"] / "logs").mkdir()
        nginx_config = folders["nginx"] / "nginx.conf"
        process_mode = "master_process on;\nworker_processes 1;\n" if reload else "master_process off;\n"
        nginx_prologue = (process_mode + "daemon off;\nerror_log stderr;\npid nginx.pid;\n"
                          "events { worker_connections 64; }\nhttp { access_log off;\n"
                          "client_body_temp_path client_body; proxy_temp_path proxy;\n"
                          "fastcgi_temp_path fastcgi; uwsgi_temp_path uwsgi; scgi_temp_path scgi;\n")
        cauce_config = folders["cauce"] / "cauce.yaml"
        def configurations(reference, proxy):
            return {"nginx": nginx_prologue + reference + "\n}\n",
                    "cauce": json.dumps({"version": 1, "proxy": proxy}, indent=2) + "\n"}

        initial = configurations(reference, config)
        nginx_config.write_text(initial["nginx"])
        cauce_config.write_text(initial["cauce"])
        rejection_results = []
        for case in rejected:
            invalid_cauce = folders["cauce"] / "invalid.yaml"
            invalid = json.dumps({"version": 1, "proxy": {
                **config, "routes": case["routes"], **case.get("proxy_edits", {})}})
            if field := case.get("duplicate_cauce"):
                item = json.dumps({field: case["routes"][0][field]})[1:-1]
                invalid = invalid.replace(item, item + ", " + item, 1)
            invalid_cauce.write_text(invalid)
            commands = {"cauce": [cauce, "check", invalid_cauce]}
            rejected_configurations = {"cauce": invalid}
            if "reference" in case:
                invalid_nginx = folders["nginx"] / "invalid.conf"
                rejected_configurations["nginx"] = nginx_prologue + case["reference"] + "\n}\n"
                invalid_nginx.write_text(rejected_configurations["nginx"])
                commands["nginx"] = [nginx, "-t", "-p", str(folders["nginx"]) + "/", "-c", "invalid.conf"]
            outputs = {}
            for key, command in commands.items():
                probe = subprocess.run(command, cwd=folders[key], capture_output=True, text=True, timeout=5)
                outputs[key] = {"exit_status": probe.returncode,
                                "expected_diagnostic": case["error"][key] in probe.stderr,
                                "stderr": probe.stderr, "stdout": probe.stdout}
            passed = all(item["exit_status"] != 0 and item["expected_diagnostic"] for item in outputs.values())
            rejection_results.append({"case": case["name"], "observations": outputs, "passed": passed,
                                      "input": case,
                                      "configurations": rejected_configurations,
                                      "reason": "expected rejection" if passed else "unexpected configuration verdict"})
        with ExitStack() as children:
            runs = {
                "nginx": children.enter_context(launch(nginx, ["-p", str(folders["nginx"]) + "/",
                                                                 "-c", "nginx.conf"], nginx_port, folders["nginx"])),
                "cauce": children.enter_context(launch(cauce, [cauce_config], cauce_port, folders["cauce"])),
            }
            before = {key: resource_sample(value[0]) for key, value in runs.items()}
            results = []
            updated = configurations(*reload[0]) if reload else None
            for case in cases:
                if case.get("after_reload") and not any(item.get("input", {}).get("after_reload") for item in results):
                    for key, path in (("nginx", nginx_config), ("cauce", cauce_config)):
                        candidate = path.with_suffix(".candidate")
                        candidate.write_text(updated[key])
                        candidate.replace(path)
                        runs[key][0].send_signal(signal.SIGHUP)
                deadline = time.monotonic() + (4 if case.get("after_reload") else 0)
                transient_errors = []
                while True:
                    try:
                        left, ignored_left, left_raw = observe(nginx_port, case)
                        right, ignored_right, right_raw = observe(cauce_port, case)
                    except OSError as error:
                        if time.monotonic() >= deadline:
                            raise
                        transient_errors.append(str(error))
                    else:
                        if left["status"] == right["status"] == case["status"] or time.monotonic() >= deadline:
                            break
                    time.sleep(0.03)
                expected_body = case.get("expected_body")
                expected_match = left["status"] == case["status"] and (
                    expected_body is None or left.get("body_base64") == base64.b64encode(expected_body.encode()).decode())
                expected_match = expected_match and all(left["headers"].get(name) == values
                    for name, values in case.get("expected_headers", {}).items())
                passed = left == right and expected_match
                results.append({"case": case["name"], "target": case["target"],
                                "input": case, "raw": {"nginx": left_raw, "cauce": right_raw},
                                "transient_errors": transient_errors,
                                "passed": passed, "reason": "observables match" if passed else "observables differ",
                                "nginx": left, "cauce": right,
                                "uncompared_headers": sorted(set(ignored_left + ignored_right)),
                                "compared_body": case.get("compare_body", True)})
            after = {key: resource_sample(value[0]) for key, value in runs.items()}
        return {"module": module, "cases": results, "configuration_rejections": rejection_results,
                "configurations": {"initial": initial, "reload": updated},
                "resources_before": before, "resources_after": after,
                "lifecycle": {key: value[1] for key, value in runs.items()}}


def test(args):
    nginx, cauce = args.nginx.resolve(), args.cauce.resolve()
    check = subprocess.run([nginx, "-V"], capture_output=True, text=True, check=True, timeout=5, cwd=ROOT)
    build = check.stdout + check.stderr
    if not re.search(r"^nginx version: nginx/1\.30\.8$", build, re.MULTILINE):
        raise ValueError("the differential runner requires NGINX OSS 1.30.8")
    args.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nginx-differential-", dir=args.work) as tmp:
        modules = []
        proof = json.loads(MANIFEST.read_text())["registrations"][args.registration] if args.registration else None
        targets = [proof["module"]] if proof else args.module or sorted(fixtures.MODULES)
        for module in targets:
            try:
                modules.append(run_module(module, nginx, cauce, Path(tmp) / module, proof))
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                modules.append({"module": module, "fixture_error": str(error), "cases": []})
    passed = all("fixture_error" not in module
                 and all(case["passed"] for case in module["cases"] + module["configuration_rejections"])
                 and all(side["started"] and side["exit_status"] == 0 and not side["forced_kill"]
                         for side in module["lifecycle"].values()) for module in modules)
    identity = {}
    if proof:
        identity = {"registration": args.registration, "fixture": proof["fixture"],
                    "fixture_sha256": digest(Path(fixtures.__file__)), "manifest_sha256": object_digest(proof),
                    "harness_sha256": digest(Path(__file__)), "cauce_revision": args.source,
                    "harness_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    "reference_source_sha256": "f867314ada32e717b949930859656c48a72dc6657aec598db58af2f98a40e1c1"}
    return {**identity, "passed": passed, "nginx_version": build.strip(), "nginx_sha256": digest(nginx),
            "cauce_sha256": digest(cauce), "modules": modules}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit")
    for name in ("tarball", "njs-tarball", "acme-tarball"):
        audit.add_argument("--" + name, required=True, type=Path)
    output = commands.add_parser("report")
    output.add_argument("--format", choices=["json"], default="json")
    plan = commands.add_parser("manifest")
    plan.add_argument("registration")
    task = commands.add_parser("queue")
    task.add_argument("--module")
    compare = commands.add_parser("test")
    compare.add_argument("--module", choices=sorted(fixtures.MODULES), action="append")
    compare.add_argument("--registration", choices=sorted(json.loads(MANIFEST.read_text())["registrations"]))
    compare.add_argument("--source")
    compare.add_argument("--nginx", required=True, type=Path)
    compare.add_argument("--cauce", required=True, type=Path)
    compare.add_argument("--work", type=local_output, default=ROOT / "work/compatibility")
    args = cli.parse_args()
    if args.command == "test" and args.registration:
        if args.module or not args.source or not re.fullmatch("[0-9a-f]{40}", args.source):
            cli.error("registration proof requires a full Cauce source revision and no module override")
        subprocess.run(["git", "cat-file", "-e", f"{args.source}^{{commit}}"], cwd=ROOT, check=True)
    if args.command == "audit":
        archive_args = ["--tarball", args.tarball.resolve(), "--njs-tarball", args.njs_tarball.resolve(),
                        "--acme-tarball", args.acme_tarball.resolve()]
        for script in ("nginx-inventory.py", "nginx-gap-audit.py"):
            status = subprocess.run([sys.executable, "-IB", ROOT / "scripts" / script, "--check", *archive_args],
                                    cwd=ROOT, check=False).returncode
            if status:
                return status
        return 0
    if args.command == "queue":
        tasks = queue(json.loads(line) for line in REGISTRY.read_text().splitlines())
        for item in sorted(tasks, key=lambda item: (item["priority"], item["module"], item["directive"])):
            if args.module is None or args.module == item["module"]:
                print(json.dumps(item, sort_keys=True))
        return 0
    if args.command == "manifest":
        print(json.dumps(manifest(args.registration), indent=2, sort_keys=True))
        return 0
    result = test(args) if args.command == "test" else report()
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(not result.get("passed", True))


if __name__ == "__main__":
    sys.exit(main())
