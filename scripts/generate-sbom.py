#!/usr/bin/env python3
"""Generate a deterministic CycloneDX inventory for the locked Rust resolution."""
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
lock = (ROOT / "Cargo.lock").read_bytes()
metadata = json.loads(subprocess.check_output(["cargo", "metadata", "--locked", "--format-version=1"], cwd=ROOT))
packages = {p["id"]: p for p in metadata["packages"]}
checksums = {(p["name"], p["version"]): p.get("checksum") for p in tomllib.loads(lock.decode())["package"]}


def reference(pkg):
    return f"pkg:cargo/{pkg['name']}@{pkg['version']}"


def component(pkg):
    ref = reference(pkg)
    item = {"type": "application" if pkg["name"] == "cauce" else "library", "name": pkg["name"],
            "version": pkg["version"], "bom-ref": ref, "purl": ref}
    if license_expression := pkg["license"]:
        item["licenses"] = [{"expression": license_expression}]
    if checksum := checksums.get((pkg["name"], pkg["version"])):
        item["hashes"] = [{"alg": "SHA-256", "content": checksum}]
    return item


root = metadata["resolve"]["root"]
linked = sorted(
    ({"ref": reference(packages[node["id"]]),
      "dependsOn": sorted(reference(packages[dep]) for dep in node["dependencies"])}
     for node in metadata["resolve"]["nodes"]),
    key=lambda item: item["ref"],
)
print(json.dumps({
    "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
    "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
    "metadata": {
        "component": component(packages[root]),
        "properties": [{"name": "cauce:cargo-lock:sha256", "value": hashlib.sha256(lock).hexdigest()},
                       {"name": "cauce:scope", "value": "Default Cargo resolution with dev and target-specific dependencies"}],
    },
    "components": sorted((component(p) for id, p in packages.items() if id != root), key=lambda p: p["bom-ref"]),
    "dependencies": linked,
}, indent=2))
