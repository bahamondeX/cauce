#!/usr/bin/env python3
"""Run bounded Docker tests for the incomplete HTTP/2 HEADERS deadline."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TLS = ROOT / "work/http2-headers/tls"
os.environ.setdefault("BUILDX_CONFIG", str(ROOT / "work/http2-headers/buildx"))


def run(args, **kwargs):
    kwargs.setdefault("check", True)
    return subprocess.run(args, **kwargs)


def issue(name, san):
    key, csr, crt = (TLS / f"{name}.{ext}" for ext in ("key", "csr", "crt"))
    run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={san}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    alt = "DNS:localhost,DNS:proxy-tls" if name == "proxy" else f"DNS:{san}"
    ext = f"subjectAltName={alt}\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n"
    run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(TLS / "ca.crt"), "-CAkey", str(TLS / "ca.key"), "-CAcreateserial", "-out", str(crt), "-days", "3", "-sha256", "-extfile", "/dev/stdin"], input=ext, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def image_info(image):
    return subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}} {{.Architecture}}"], text=True).strip().split()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="already built cauce:0.5.0 image tag (do not build the runtime here)")
    ap.add_argument("--platform", choices=("arm64", "amd64"), required=True, help="image architecture; names the report file")
    ap.add_argument("--project", default=f"cauce-headers-{os.getpid()}")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--report-path", type=Path, help="override the normal per-architecture report path")
    args = ap.parse_args()

    TLS.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run(["colima", "start"])
        run(["docker", "info"], stdout=subprocess.DEVNULL)
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(TLS / "ca.key"), "-out", str(TLS / "ca.crt"), "-days", "3", "-subj", "/CN=Cauce HTTP2 Headers Integration CA", "-addext", "keyUsage=critical,keyCertSign,cRLSign"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    issue("proxy", "localhost")
    issue("backend", "backend.test")
    for name in ("proxy", "backend"):
        (TLS / f"{name}.key").chmod(0o644)

    image_id, arch = image_info(args.image)
    if arch != args.platform:
        raise RuntimeError(f"image {args.image} architecture is {arch}; requested --platform {args.platform}")
    os.environ["CAUCE_HEADERS_PROXY_CLEAR_IMAGE"] = args.image
    os.environ["CAUCE_HEADERS_PROXY_TLS_IMAGE"] = args.image
    compose = ["docker", "compose", "-p", args.project, "-f", str(ROOT / "compose.http2-headers.yaml")]
    override = ROOT / "work/http2-headers/compose.platform.yaml"
    override.write_text(f"services:\n  proxy-clear:\n    platform: linux/{arch}\n  proxy-tls:\n    platform: linux/{arch}\n")
    compose += ["-f", str(override)]
    suffix = args.platform
    report_path = args.report_path or ROOT / f"docs/increment5-http2-headers-{suffix}.json"
    report_stem = report_path.with_suffix("")
    logs_path = report_stem.with_name(report_stem.name + "-compose.log")
    access_path = report_stem.with_name(report_stem.name + "-access.jsonl")
    result = {"result": "NOT_RUN", "project": args.project, "platform": arch, "runtime_image": args.image, "runtime_image_id": image_id, "runtime_architecture": arch, "cases": []}
    try:
        run([*compose, "build", "node", "client"])
        run([*compose, "up", "--no-build", "-d"])
        client = run([*compose, "run", "--rm", "client"], capture_output=True, text=True, timeout=180, check=False)
        if client.returncode:
            raise RuntimeError(f"Docker HTTP/2 headers client failed:\n{client.stdout}\n{client.stderr}")
        parsed = None
        for match in reversed(list(re.finditer(r'\{\s*"result"\s*:', client.stdout))):
            try:
                candidate, _ = json.JSONDecoder().raw_decode(client.stdout[match.start():])
                if isinstance(candidate, dict) and candidate.get("result") == "PASS":
                    parsed = candidate
                    break
            except json.JSONDecodeError:
                continue
        if parsed is None:
            raise AssertionError(f"client did not emit a PASS report:\n{client.stdout}")
        result.update(parsed)
        result["result"] = "PASS"
        result["proxy_images"] = {"proxy-clear": image_info(args.image)[0], "proxy-tls": image_info(args.image)[0]}
        result["checks"] = parsed.get("checks", [])
        access_records = []
        for service in ("proxy-clear", "proxy-tls"):
            request_id = parsed["sibling_request_ids"][service]
            deadline = time.monotonic() + 2
            while True:
                captured = run([*compose, "exec", "-T", service, "cat", "/var/log/cauce/access.jsonl"], capture_output=True, text=True, check=False)
                if captured.returncode:
                    raise RuntimeError(f"could not collect {service} access log: {captured.stderr}")
                records = [json.loads(line) for line in captured.stdout.splitlines() if line.strip()]
                sibling_cancel = [r for r in records if r.get("request_id") == request_id
                                  and r.get("outcome") == "cancelled" and r.get("error_phase") == "transfer"
                                  and r.get("method") == "GET" and r.get("response_bytes") == 0
                                  and 700 <= r.get("duration_ms", 0) <= 1200]
                if sibling_cancel or time.monotonic() >= deadline: break
                time.sleep(.1)
            if len(sibling_cancel) != 1:
                raise AssertionError(f"{service} has no unique cancellation for active sibling {request_id}: {records}")
            # Keep only this process's records if a caller reuses a Compose project.
            access_records.extend(r for r in records if r.get("instance_id") == sibling_cancel[0]["instance_id"])
            result.setdefault("sibling_cancellations", {})[service] = [
                {"request_id": r["request_id"], "traceparent": r["traceparent"], "request_bytes": r["request_bytes"], "response_bytes": r["response_bytes"], "duration_ms": r["duration_ms"], "outcome": r["outcome"], "error_phase": r["error_phase"]}
                for r in sibling_cancel
            ]
        access_path.write_text("".join(json.dumps(record, separators=(",", ":")) + "\n" for record in access_records))
        result["access_log_records"] = len(access_records)
        result["access_log_path"] = access_path.name
        print("PASS: " + "; ".join(result["checks"]))
    except Exception as exc:
        result.update({"result": "FAIL", "error": str(exc)})
        raise
    finally:
        try:
            logs = subprocess.run([*compose, "logs", "--no-color"], capture_output=True, text=True, timeout=20)
            logs_path.write_text(logs.stdout + logs.stderr)
        except Exception as exc:
            logs_path.write_text(f"Compose log capture failed: {exc}\n")
        if not args.keep:
            subprocess.run([*compose, "down", "--remove-orphans"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        report_path.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
