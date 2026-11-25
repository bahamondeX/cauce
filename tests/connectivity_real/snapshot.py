"""Copy an explicit authorized, source-only reference; never execute in its checkout."""
from pathlib import Path
import hashlib
import json
import shutil

FILES = [
    'deploy/proxy_ingest_api.py',
    'copilot/__init__.py', 'copilot/identity.py', 'copilot/network_security.py',
    'copilot/tenant_queries.py', 'copilot/tenant_store.py', 'copilot/proxy_events.py',
    'copilot/proxy_ingest.py', 'copilot/proxy_store.py',
    'copilot/schemas/cauce-access-log-1.1.json', 'copilot/schemas/CAUCE-LICENSE',
    'copilot/schemas/CAUCE-NOTICE.md',
    'tests/conftest.py', 'tests/test_proxy_events.py',
    'tests/test_proxy_ingest.py', 'tests/test_proxy_store.py',
    'tests/test_proxy_ingest_api.py', 'tests/test_cauce_collector_integration.py',
    'tests/fixtures/cauce/http1-source.jsonl', 'tests/fixtures/cauce/http2-source.jsonl',
    'requirements-dev.lock', 'docs/PROXY_INGEST_CONTRACT.md',
    'docs/PHASE2B_DURABILITY.md', 'docs/PHASE2B_CONSUMER.md',
]


def snapshot(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for relative in FILES:
        original = source / relative
        if original.is_symlink() or not original.is_file():
            raise ValueError(f'reference must contain a regular source file: {relative}')
        before = original.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(before)
        target.chmod(0o444)
        digest = hashlib.sha256(before).hexdigest()
        if hashlib.sha256(original.read_bytes()).hexdigest() != digest:
            raise ValueError(f'reference changed during snapshot: {relative}')
        manifest[relative] = digest
    (destination / 'snapshot-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def verify_original(source: Path, manifest: dict) -> bool:
    return all(hashlib.sha256((source / relative).read_bytes()).hexdigest() == digest
               for relative, digest in manifest.items())
