#!/usr/bin/env python3
"""Local HTTPS lab around Connectivity's real injected ingest app and sink.

The runner mounts copied source read-only at /reference and this file imports
only the real adapter/storage implementation from that snapshot. Moto backs
all AWS APIs; no AWS endpoint, credential, or external route is configured.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
from botocore.config import Config
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from moto import mock_aws

from copilot.identity import Principal
from copilot.proxy_events import MAX_BODY, NamespaceMapping
from copilot.proxy_store import ProxyStore, mapping_hash
from copilot.tenant_store import TenantStore
from proxy_ingest_api import make_ingest_app


MODES = {
    "normal",
    "lose_ack_once",
    "expire_token_once",
    "revoke_token_once",
    "disable_namespace_once",
    "unauthorized_namespace_once",
    "revoke_token_before_commit_once",
    "disable_namespace_before_commit_once",
}
CONTROL_ACTIONS = {"normal", "rotate_token", "restore_namespace", *MODES}
BUCKET = "connectivity-cauce-local-lab"


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _write_json(path: Path, value) -> None:
    _write_atomic(path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), 0o600)


def _tls_material(directory: Path):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Cauce local Connectivity test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(hours=4))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server-key.pem"
    _write_atomic(ca_path, ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    _write_atomic(cert_path, leaf.public_bytes(serialization.Encoding.PEM), 0o644)
    _write_atomic(
        key_path,
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o600,
    )
    return ca_path, cert_path, key_path


def _create_tables(session):
    db = session.resource("dynamodb", region_name="us-east-1")
    for name, keys in (
        ("users", ("user_id",)),
        ("hosts", ("user_id", "host_id")),
        ("control", ("pk", "sk")),
    ):
        db.create_table(
            TableName="connectivity-copilot-" + name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in keys],
            KeySchema=[
                {"AttributeName": key, "KeyType": "HASH" if index == 0 else "RANGE"}
                for index, key in enumerate(keys)
            ],
        )
    return TenantStore(db)


class Lab:
    def __init__(self, sandbox: Path, event_template: Path):
        self.sandbox = sandbox
        self.sandbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.sandbox, 0o700)
        template = None
        for line in event_template.read_bytes().splitlines():
            if line.strip():
                template = json.loads(line)
                break
        if not isinstance(template, dict):
            raise ValueError("event template must contain a JSON object line")

        self.clock = datetime.now(timezone.utc).replace(microsecond=0)
        self.epoch = int(self.clock.timestamp())
        self._mock = mock_aws()
        self._mock.start()
        try:
            # Deliberately non-secret placeholders are scoped to Moto only.
            session = boto3.Session(
                aws_access_key_id="moto-local-only",
                aws_secret_access_key="moto-local-only",
                region_name="us-east-1",
            )
            self.store = _create_tables(session)
            principal = Principal(str(uuid.uuid4()), "cauce-lab@example.invalid")
            self.store.ensure_user(principal)
            host = self.store.create_host(principal, "https://example.invalid/")
            host["verification"] = {"status": "verified", "expires_at": self.epoch + 86_400}
            self.store.hosts.put_item(Item=host)
            self.namespace_id = str(uuid.uuid4())
            self.mapping = NamespaceMapping(
                principal.user_id,
                self.namespace_id,
                host["host_id"],
                template["route_id"],
                template.get("upstream_id"),
            )
            self.namespace_key = {"pk": "INGEST_NS#" + self.namespace_id, "sk": "META"}
            self.namespace_item = {
                **self.namespace_key,
                "mapping": {
                    "user_id": self.mapping.user_id,
                    "namespace_id": self.mapping.namespace_id,
                    "host_id": self.mapping.host_id,
                    "route_id": self.mapping.route_id,
                    "upstream_id": self.mapping.upstream_id,
                    "transport": self.mapping.transport,
                },
                "mapping_hash": mapping_hash(self.mapping),
                "host_config_version": int(host["config_version"]),
                "enabled": True,
                "deleted": False,
            }
            self.store.control.put_item(Item=self.namespace_item)
            s3 = session.client(
                "s3",
                region_name="us-east-1",
                config=Config(retries={"total_max_attempts": 1}),
            )
            s3.create_bucket(Bucket=BUCKET)
            s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
            self.bucket = BUCKET
            self.s3 = s3
            self.sink = ProxyStore(self.store, s3, self.bucket, clock=lambda: self.epoch)
            self.token_path = self.sandbox / "token"
            self.token_generation = 0
            self.token_id = ""
            self.token_hash = ""
            self.secret_values = []
            self._issue_token()
            self.original_namespace = dict(self.namespace_item)
            self.mode = "normal"
            self.requests = []
            self.app = make_ingest_app(self.sink.authorize, self.sink, lambda: self.clock)
            self.client = TestClient(self.app)
        except BaseException:
            self._mock.stop()
            raise

    def _issue_token(self):
        token_id = str(uuid.uuid4())
        token = f"cc_ingest_v1.{token_id}.{secrets.token_urlsafe(32)}"
        self.secret_values.append(token)
        _write_json(self.sandbox / "secrets.json", self.secret_values)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.store.control.put_item(
            Item={
                "pk": "INGEST_TOKEN#" + token_id,
                "sk": "META",
                "token_hash": token_hash,
                "user_id": self.mapping.user_id,
                "namespace_id": self.mapping.namespace_id,
                "scope": "proxy:ingest",
                "enabled": True,
                "deleted": False,
                "expires_at": self.epoch + 86_400,
            }
        )
        self.token_id = token_id
        self.token_hash = token_hash
        self.token_generation += 1
        _write_atomic(self.token_path, token.encode(), 0o600)

    def rotate_token(self):
        old = self._token_item()
        old["enabled"] = False
        old["deleted"] = True
        self.store.control.put_item(Item=old)
        self._issue_token()

    def _token_key(self):
        return {"pk": "INGEST_TOKEN#" + self.token_id, "sk": "META"}

    def _token_item(self):
        return self.store.control.get_item(Key=self._token_key(), ConsistentRead=True)["Item"]

    def restore_namespace(self):
        self.store.control.put_item(Item=dict(self.original_namespace))

    def _inactivate(self, mode):
        if mode in {"expire_token_once", "revoke_token_once", "revoke_token_before_commit_once"}:
            item = self._token_item()
            if mode == "expire_token_once":
                item["expires_at"] = self.epoch - 1
            else:
                item["enabled"] = False
            self.store.control.put_item(Item=item)
        elif mode in {"disable_namespace_once", "disable_namespace_before_commit_once"}:
            item = self.store.control.get_item(Key=self.namespace_key, ConsistentRead=True)["Item"]
            item["enabled"] = False
            self.store.control.put_item(Item=item)
        elif mode == "unauthorized_namespace_once":
            item = self.store.control.get_item(Key=self.namespace_key, ConsistentRead=True)["Item"]
            mapping = dict(item["mapping"])
            mapping["namespace_id"] = str(uuid.uuid4())
            item["mapping"] = mapping
            # Keep the original hash: its mismatch is the real authorization guard.
            self.store.control.put_item(Item=item)

    def set_control(self, action):
        if action not in CONTROL_ACTIONS:
            raise ValueError("unsupported control action")
        if action == "rotate_token":
            self.rotate_token()
            return {"ok": True, "action": action, "token_generation": self.token_generation}
        if action == "restore_namespace":
            self.restore_namespace()
            return {"ok": True, "action": action}
        self.mode = action
        return {"ok": True, "action": action}

    def _counts(self):
        rows = self.store.control.scan().get("Items", [])
        receipts = [
            item
            for item in rows
            if item.get("pk", "").startswith("INGEST#") and item.get("sk", "").startswith("BATCH#")
        ]
        jobs = [item for item in rows if item.get("pk", "").startswith("INGEST_JOB#")]
        costs = [item for item in rows if item.get("sk", "").startswith("COST#")]
        versions = []
        paginator = self.s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket):
            for value in page.get("Versions", []):
                versions.append({"key": value["Key"], "version_id": value["VersionId"], "etag": value["ETag"]})
        keys = sorted({value["key"] for value in versions})
        accepted = []
        for receipt in receipts:
            try:
                accepted.extend(self.sink._read_accepted(self.mapping, receipt["sk"].removeprefix("BATCH#")))
            except LookupError:
                continue
        return {
            "receipt_count": len(receipts),
            "outbox_count": len(jobs),
            "outbox": [
                {k: job.get(k) for k in ("pk", "batch_id", "status", "dispatch_state")}
                for job in jobs
            ],
            "object_key_count": len(keys),
            "object_version_count": len(versions),
            "object_versions": versions,
            "cost_microusd": sum(int(item.get("used_microusd", 0)) for item in costs),
            "event_ids": sorted({row["event_id"] for row in accepted}),
        }

    def report(self):
        return {"requests": list(self.requests), "storage": self._counts()}

    def close(self):
        self.client.close()
        self._mock.stop()


def serve(lab: Lab, host: str, port: int, cert: Path, key: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *args):
            pass

        def _json(self, status, payload):
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _loopback_only(self):
            try:
                return ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                return False

        def do_GET(self):
            if not self._loopback_only():
                return self._json(404, {"error": "not_found"})
            if self.path == "/_lab/health":
                return self._json(200, {"ready": True})
            if self.path == "/_lab/report":
                return self._json(200, lab.report())
            return self._json(404, {"error": "not_found"})

        def do_POST(self):
            if not self._loopback_only():
                return self._json(404, {"error": "not_found"})
            if self.path == "/_lab/control":
                length = self.headers.get("Content-Length", "")
                if not length.isdigit() or int(length) > 4096:
                    return self._json(413, {"error": "control_size"})
                try:
                    payload = json.loads(self.rfile.read(int(length)))
                    if not isinstance(payload, dict) or set(payload) != {"action"}:
                        raise ValueError
                    result = lab.set_control(payload["action"])
                except (ValueError, TypeError, json.JSONDecodeError):
                    return self._json(400, {"error": "invalid_control"})
                return self._json(200, result)
            if self.path != "/v1/ingest/cauce-proposal":
                return self._json(404, {"error": "not_found"})
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit() or int(raw_length) > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"error": "body_size"})
            body = self.rfile.read(int(raw_length))
            try:
                envelope = json.loads(body)
                batch_id = envelope.get("batch_id") if isinstance(envelope, dict) else None
                event_ids = [event.get("event_id") for event in envelope.get("events", [])]
            except (ValueError, TypeError):
                batch_id, event_ids = None, []
            mode = lab.mode
            lab.mode = "normal"
            original_put = lab.sink._put_immutable
            precommit_modes = {"revoke_token_before_commit_once", "disable_namespace_before_commit_once"}
            mutation_record = {"fired": False}
            try:
                if mode in {
                    "expire_token_once",
                    "revoke_token_once",
                    "disable_namespace_once",
                    "unauthorized_namespace_once",
                }:
                    lab._inactivate(mode)
                elif mode in precommit_modes:
                    def mutate_after_manifest(object_key, value):
                        result = original_put(object_key, value)
                        if "/batches/" in object_key and not mutation_record["fired"]:
                            lab._inactivate(mode)
                            mutation_record["fired"] = True
                        return result

                    lab.sink._put_immutable = mutate_after_manifest

                response = lab.client.post(
                    self.path,
                    content=body,
                    headers={
                        "Authorization": self.headers.get("Authorization", ""),
                        "Content-Type": self.headers.get("Content-Type", ""),
                        "Content-Encoding": self.headers.get("Content-Encoding", "identity"),
                    },
                )
                wire_status = 503 if mode == "lose_ack_once" and response.status_code == 200 else response.status_code
                lab.requests.append(
                    {
                        "mode": mode,
                        "batch_id": batch_id,
                        "payload_sha256": hashlib.sha256(body).hexdigest(),
                        "event_ids": event_ids,
                        "internal_status": response.status_code,
                        "wire_status": wire_status,
                        "manifest_mutation_fired": mutation_record["fired"],
                        "storage_after": lab._counts(),
                    }
                )
                if wire_status == 503:
                    return self._json(503, {"code": "lab_lost_ack"})
                self.send_response(wire_status)
                for name, value in response.headers.items():
                    if name.lower() in {"content-type", "cache-control", "x-content-type-options"}:
                        self.send_header(name, value)
                content = response.content
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception:
                # Do not serialize exception details or headers into logs/reports.
                lab.requests.append({"mode": mode, "batch_id": batch_id,
                                     "payload_sha256": hashlib.sha256(body).hexdigest(),
                                     "event_ids": event_ids, "wire_status": 503,
                                     "handler_error": True, "storage_after": lab._counts()})
                return self._json(503, {"code": "lab_handler_failure"})
            finally:
                lab.sink._put_immutable = original_put

    server = HTTPServer((host, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path, default=Path("/sandbox"))
    parser.add_argument("--event-template", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9443)
    args = parser.parse_args()
    lab = Lab(args.sandbox, args.event_template)
    ca, cert, key = _tls_material(args.sandbox)
    server = serve(lab, args.host, args.port, cert, key)
    try:
        print("READY", flush=True)
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        lab.close()


if __name__ == "__main__":
    main()
