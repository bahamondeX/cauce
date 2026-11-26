#!/usr/bin/env python3
"""Package verified Cauce ELFs over pinned Debian in repository-local OCI layouts."""
import argparse
from copy import deepcopy
import gzip
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MEDIA = "application/vnd.oci.image."
BASE = "https://registry-1.docker.io/v2/library/debian/"
ARCHITECTURES = ("arm64", "amd64")
PROFILES = ("full", "core")
FILES = ("cauce", "cauce-collector")


def inside(value):
    path = Path(value).resolve()
    if ROOT not in path.parents:
        raise argparse.ArgumentTypeError("inputs and outputs must be inside Cauce")
    return path


def git(*args):
    return subprocess.check_output(["git", "-C", ROOT, *args])


def digest(data):
    return "sha256:" + sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fetch(token, path, identifier, media):
    request = Request(BASE + path + "/" + identifier, headers={"Accept": media})
    request.add_unredirected_header("Authorization", "Bearer " + token)
    with urlopen(request, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or digest(data) != identifier:
        raise ValueError(f"Debian data failed verification for {identifier}")
    return data


def pull_base(base_digest):
    query = urlencode({"service": "registry.docker.io", "scope": "repository:library/debian:pull"})
    with urlopen("https://auth.docker.io/token?" + query, timeout=30) as response:
        token = json.load(response)["token"]
    index = json.loads(fetch(token, "manifests", base_digest, MEDIA + "index.v1+json"))
    result = {}
    for platform in ARCHITECTURES:
        descriptor, = [row for row in index["manifests"] if row.get("platform", {}).get("architecture") == platform
                       and row["platform"]["os"] == "linux"]
        manifest = json.loads(fetch(token, "manifests", descriptor["digest"], descriptor["mediaType"]))
        config = manifest["config"]
        raw_config = fetch(token, "blobs", config["digest"], config["mediaType"])
        if len(raw_config) != config["size"]:
            raise ValueError("Debian config size mismatch")
        data = [fetch(token, "blobs", item["digest"], item["mediaType"]) for item in manifest["layers"]]
        if any(len(raw) != item["size"] for raw, item in zip(data, manifest["layers"])):
            raise ValueError("Debian compressed layer size mismatch")
        metadata = json.loads(raw_config)
        expected = (platform, "linux", metadata["rootfs"]["diff_ids"])
        actual = (metadata["architecture"], metadata["os"], [digest(gzip.decompress(raw)) for raw in data])
        if actual != expected:
            raise ValueError("Debian platform or uncompressed layer digest mismatch")
        result[platform] = (descriptor, manifest, metadata, data)
    return result


def put(folder, data, media):
    identifier = digest(data)
    (folder / "blobs" / "sha256" / identifier.removeprefix("sha256:")).write_bytes(data)
    return {"mediaType": media, "digest": identifier, "size": len(data)}


def app_layer(source, executables):
    archive_data = git("archive", source, "examples/docker.toml", "LICENSE", "third-party")
    app = BytesIO()
    with tarfile.open(fileobj=BytesIO(archive_data), mode="r:") as archive, tarfile.open(fileobj=app, mode="w:") as layer:
        def entry(name, mode, data=None, owner=0):
            item = tarfile.TarInfo(name)
            item.mode, item.uid, item.gid = mode, owner, owner
            if data is None:
                item.type = tarfile.DIRTYPE
            else:
                item.size = len(data)
            layer.addfile(item, None if data is None else BytesIO(data))

        for name in ("etc/cauce", "usr/share/doc/cauce"):
            entry(name, 0o755)
        for name in ("var/lib/cauce", "var/log/cauce"):
            entry(name, 0o755, owner=10001)
        for name, data in executables.items():
            entry("usr/local/bin/" + name, 0o755, data)
        for item in archive:
            if item.name == "examples":
                continue
            if item.name == "examples/docker.toml":
                name = "etc/cauce/cauce.toml"
            else:
                name = "usr/share/doc/cauce/" + item.name
            if item.isdir():
                entry(name, 0o755)
            elif item.isfile():
                entry(name, 0o644, archive.extractfile(item).read())
            else:
                raise ValueError(f"unsupported app-layer entry {item.name}")
    return app.getvalue()


def verify_inputs(input_folder):
    artifacts = {}
    provenance = set()
    for profile in PROFILES:
        for arch in ARCHITECTURES:
            directory = input_folder / profile / ("linux_" + arch)
            raw_source = (directory / "SOURCE").read_bytes()
            checksums = {name: "sha256:" + identifier for identifier, name in
                         (line.split() for line in (directory / "SHA256SUMS").read_text().splitlines())}
            data = {name: (directory / name).read_bytes() for name in FILES}
            if any(checksums.get(name) != digest(raw) for name, raw in {**data, "SOURCE": raw_source}.items()):
                raise ValueError(f"source or executable checksum failed for {directory}")
            metadata = dict(line.split("=", 1) for line in raw_source.decode().splitlines())
            if metadata["profile"] != profile or metadata["platform"] != "linux/" + arch:
                raise ValueError(f"profile or platform mismatch for {directory}")
            artifacts[profile, arch] = data
            provenance.add((metadata["source_commit"], metadata["source_tree"], metadata["cargo_lock_sha256"]))
    (source, tree, lock), = provenance
    if not re.fullmatch("[0-9a-f]{40}", source) or git("rev-parse", source + "^{commit}").decode().strip() != source:
        raise ValueError("expected an exact Cauce source commit")
    if git("rev-parse", source + "^{tree}").decode().strip() != tree:
        raise ValueError("source tree does not match the recorded identity")
    if sha256(git("show", source + ":Cargo.lock")).hexdigest() != lock:
        raise ValueError("Cargo.lock does not match the recorded identity")
    return source, artifacts


def stage(destination, source, artifacts, bases):
    sizes = []
    for profile in PROFILES:
        folder = destination / profile
        (folder / "blobs" / "sha256").mkdir(parents=True)
        images = []
        for arch, (reference, manifest, base_config, layers) in bases.items():
            executables = artifacts[profile, arch]
            raw_layer = app_layer(source, executables)
            compressed = gzip.compress(raw_layer, compresslevel=9, mtime=0)
            app = put(folder, compressed, MEDIA + "layer.v1.tar+gzip")
            base_layers = [put(folder, raw, item["mediaType"]) for raw, item in zip(layers, manifest["layers"])]
            config = deepcopy(base_config)
            config["rootfs"]["diff_ids"].append(digest(raw_layer))
            config["history"].append({"created_by": "stage-oci-layout.py"})
            config["config"].update(
                User="10001:10001", ExposedPorts={"8080/tcp": {}}, StopSignal="SIGTERM",
                Entrypoint=["/usr/local/bin/cauce"], Cmd=["/etc/cauce/cauce.toml"],
                Labels={"org.opencontainers.image.revision": source, "io.cauce.distribution": profile,
                        "org.opencontainers.image.base.digest": reference["digest"]},
            )
            image_config = put(folder, encoded(config), MEDIA + "config.v1+json")
            image = {"schemaVersion": 2, "mediaType": MEDIA + "manifest.v1+json",
                     "config": image_config, "layers": [*base_layers, app]}
            descriptor = put(folder, encoded(image), image["mediaType"])
            images.append({**descriptor, "platform": reference["platform"]})
            sizes.append({
                "architecture": arch, "profile": profile, "manifest": descriptor["digest"],
                "base_manifest": reference["digest"], "layer_bytes": sum(item["size"] for item in image["layers"]),
                "cauce_layer_bytes": app["size"], "cauce_layer_digest": app["digest"],
                "executable_sha256": {name: digest(raw) for name, raw in executables.items()},
            })
        (folder / "oci-layout").write_bytes(encoded({"imageLayoutVersion": "1.0.0"}))
        index = encoded({"schemaVersion": 2, "mediaType": MEDIA + "index.v1+json", "manifests": images})
        (folder / "index.json").write_bytes(index)
        for image in sizes:
            if image["profile"] == profile:
                image["index_sha256"] = digest(index)
    return sizes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf_matrix", type=inside)
    parser.add_argument("output", type=inside)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new directory")
    source, artifacts = verify_inputs(args.elf_matrix)
    dockerfile = git("show", source + ":Dockerfile").decode()
    base_digest = re.search(r"^FROM debian:bookworm-slim@(sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE)[1]
    bases = pull_base(base_digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="cauce-oci-", dir=args.output.parent) as temporary:
        ready = Path(temporary) / "ready"
        images = stage(ready, source, artifacts, bases)
        evidence = {"source_commit": source, "base_index": base_digest, "images": images}
        (ready / "identities.json").write_text(json.dumps(evidence, indent=2) + "\n")
        ready.rename(args.output)
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
