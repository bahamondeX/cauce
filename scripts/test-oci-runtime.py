#!/usr/bin/env python3
"""Run exact ARM64 OCI manifests through a local Docker runtime."""
import argparse
from hashlib import sha256
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import ssl
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import time
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.STDOUT, timeout=45).strip()


def docker(*args):
    return run("docker", *args)


def unpack_for_docker(layout, tag, output):
    index = json.loads((layout / "index.json").read_text())
    descriptor, = [row for row in index["manifests"] if row["platform"]["architecture"] == "arm64"]

    def blob(item):
        data = (layout / "blobs" / item["digest"].replace(":", "/")).read_bytes()
        if len(data) != item["size"] or "sha256:" + sha256(data).hexdigest() != item["digest"]:
            raise ValueError("OCI descriptor verification failed")
        return data

    manifest = json.loads(blob(descriptor))
    config = blob(manifest["config"])
    with tarfile.open(output, "w") as archive:
        def add(name, data):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, BytesIO(data))

        layers = []
        for item in manifest["layers"]:
            name = item["digest"].split(":")[1] + ".tar.gz"
            add(name, blob(item))
            layers.append(name)
        add("config.json", config)
        add("manifest.json", json.dumps([{"Config": "config.json", "RepoTags": [tag], "Layers": layers}]).encode())
    return descriptor["digest"], manifest["config"]["digest"], json.loads(config)


def check_runtime(layout, folder, profile, expected):
    suffix = uuid.uuid4().hex[:12]
    tag = f"cauce-m11-oci-proof:{profile}-{suffix}"
    name = f"cauce-m11-oci-{profile}-{suffix}"
    archive = folder / f"{profile}.tar"
    manifest, image_id, config = unpack_for_docker(layout, tag, archive)
    docker("load", "-i", archive)
    try:
        image, = json.loads(docker("image", "inspect", tag))
        assert image["Id"] == image_id and image["Architecture"] == "arm64"
        assert image["RootFS"]["Layers"] == config["rootfs"]["diff_ids"]
        assert image["Config"]["User"] == "10001:10001"
        assert image["Config"]["Entrypoint"] == ["/usr/local/bin/cauce"]
        assert image["Config"]["Cmd"] == ["/etc/cauce/cauce.toml"]
        assert image["Config"]["StopSignal"] == "SIGTERM"
        default_check = docker("run", "--pull=never", "--rm", "--network=none", tag,
                               "check", "/etc/cauce/cauce.toml")
        assert "configuration valid" in default_check
        try:
            docker("run", "--pull=never", "-d", "--name", name, "-p", "127.0.0.1::8080",
                   "--mount", f"type=bind,source={folder},target=/fixture,readonly", tag, "/fixture/probe.yaml")
            details, = json.loads(docker("inspect", name))
            port, = details["NetworkSettings"]["Ports"]["8080/tcp"]
            ctx = ssl.create_default_context(cafile=folder / "probe.pem")
            deadline = time.monotonic() + 8
            while True:
                client = http.client.HTTPSConnection("127.0.0.1", int(port["HostPort"]), context=ctx, timeout=1)
                try:
                    client.request("GET", "/proof", headers={"Host": "localhost"})
                    result = client.getresponse()
                    assert result.status == 200 and result.read() == b"oci-runtime"
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
                finally:
                    client.close()
            hashes = docker("exec", name, "sha256sum", "/usr/local/bin/cauce", "/usr/local/bin/cauce-collector")
            for line in hashes.splitlines():
                value, path = line.split()
                assert expected[Path(path).name] == "sha256:" + value
            permissions = docker("exec", name, "sh", "-ec",
                                 'test "$(id -u):$(id -g)" = 10001:10001; '
                                 'test -w /var/log/cauce && test -w /var/lib/cauce; '
                                 'test ! -w /usr/local/bin && test -r /fixture/probe-key.pem; '
                                 'grep "^Uid:" /proc/1/status')
            assert permissions.split()[1:] == ["10001"] * 4
            caps = json.loads(docker("exec", name, "/usr/local/bin/cauce", "capabilities", "--json"))
            assert caps["http1"] and caps["tls"] and caps["disk_cache"] == (profile == "full")
            sqlite = docker("exec", name, "/usr/local/bin/cauce-collector", "--sqlite-version")
            assert sqlite == "3.54.1"
            readiness = run("colima", "ssh", "--", "sudo", "-n", "nsenter", "-t",
                            str(details["State"]["Pid"]), "-n", "curl", "-fsS", "http://127.0.0.1:9090/ready")
            docker("stop", "--time", "5", name)
            status, = json.loads(docker("inspect", name))
            assert status["State"]["ExitCode"] == 0
            return {"profile": profile, "manifest": manifest, "config": image_id,
                    "source_revision": image["Config"]["Labels"]["org.opencontainers.image.revision"],
                    "entrypoint": image["Config"]["Entrypoint"], "user": image["Config"]["User"],
                    "default_config_check": default_check, "permissions": permissions, "executables": hashes,
                    "readiness": readiness, "https_status": 200, "capabilities": caps, "linked_sqlite": sqlite,
                    "sigterm_exit": status["State"]["ExitCode"]}
        finally:
            subprocess.run(["docker", "rm", "-f", name], cwd=ROOT, capture_output=True, timeout=20)
    finally:
        docker("image", "rm", tag)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layout", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    layout, work = args.layout.resolve(), args.work.resolve()
    if Path.cwd() != ROOT or any(ROOT not in path.parents for path in (layout, work)):
        parser.error("run from the Cauce root with repository-local paths")
    endpoint = os.environ.get("DOCKER_HOST") or docker(
        "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", docker("context", "show"))
    if not endpoint.startswith("unix://"):
        raise RuntimeError("OCI runtime proof requires a local Unix Docker socket")
    runtime = docker("info", "--format", "{{.OSType}}/{{.Architecture}}")
    if runtime != "linux/aarch64":
        raise RuntimeError(f"expected local Linux ARM64 runtime, got {runtime}")
    evidence = json.loads((layout / "identities.json").read_text())
    with TemporaryDirectory(prefix="oci-runtime-", dir=work) as path:
        folder = Path(path)
        folder.chmod(0o755)
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", folder / "probe-key.pem", "-out", folder / "probe.pem")
        (folder / "probe-key.pem").chmod(0o644)
        config = tomllib.loads((ROOT / "examples/cauce.toml").read_text())
        config.update(listen="0.0.0.0:8080", admin="127.0.0.1:9090", upstreams=[],
                      tls={"cert_file": "/fixture/probe.pem", "key_file": "/fixture/probe-key.pem"},
                      routes=[{"id": "probe", "host": "localhost", "path_prefix": "/proof",
                               "content": {"type": "direct", "status": 200, "body": "oci-runtime"}}])
        config["logs"]["enabled"] = False
        (folder / "probe.yaml").write_text(json.dumps({"version": 1, "proxy": config}))
        results = [check_runtime(layout / row["profile"], folder, row["profile"], row["executable_sha256"])
                   for row in evidence["images"] if row["architecture"] == "arm64"]
        print(json.dumps({"host": runtime, "images": results}, indent=2))


if __name__ == "__main__":
    main()
