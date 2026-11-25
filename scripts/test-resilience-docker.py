#!/usr/bin/env python3
"""Run isolated failure/recovery suites and preserve a reproducible Docker manifest."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
SUITES = ('proxy_failures', 'log_disk', 'collector_storage')


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='cauce:0.9-final-arm64')
    parser.add_argument('--report-dir', type=Path)
    args = parser.parse_args()
    colima_socket = Path.home()/'.colima/default/docker.sock'
    if colima_socket.exists():
        os.environ.setdefault('DOCKER_HOST', 'unix://' + str(colima_socket))
    def docker(*parts, check=True):
        p = subprocess.run(['docker', *parts], text=True, capture_output=True, timeout=30)
        if check and p.returncode:
            raise RuntimeError(f'docker {parts[:2]}: {p.stderr}')
        return p
    if docker('info', check=False).returncode:
        subprocess.run(['colima', 'start'], check=True, timeout=180)
        docker('info')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6]
    out = (args.report_dir or ROOT/'tests/resilience/results'/('increment9-'+stamp)).resolve()
    out.mkdir(parents=True, exist_ok=False)
    if shutil.disk_usage(out).free < 2*1024**3:
        raise RuntimeError('At least 2 GiB free required; no files were removed.')
    def containers():
        ids = docker('ps', '-aq').stdout.split()
        entries = json.loads(docker('inspect', *ids).stdout) if ids else []
        return {e['Id']: {'name': e['Name'].lstrip('/'), 'running': e['State']['Running'],
                         'started_at': e['State']['StartedAt']} for e in entries}
    before = containers()
    inputs = [ROOT/'Cargo.toml', ROOT/'Cargo.lock', ROOT/'Dockerfile', Path(__file__).resolve(),
              ROOT/'examples/access-log.jsonl', ROOT/'bench/increment8/configs/cauce-h2-clear.toml']
    inputs += sorted((ROOT/'src').rglob('*.rs'))
    inputs += sorted(p for p in (ROOT/'tests/resilience').glob('*') if p.suffix in ('.py', '.mjs'))
    manifest = {'increment': 9, 'status': 'RUNNING', 'started_utc': now(), 'suites': [],
                'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in inputs},
                'containers_before': before, 'host_free_bytes_before': shutil.disk_usage(out).free,
                'host_disk_filled': False, 'connectivity_contacted': False,
                'limits': 'Bounded 4/8 MiB tmpfs only. No host ports. No global cleanup.',
                'errors': []}
    def save():
        (out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    try:
        engine = json.loads(docker('info', '--format', '{{json .}}').stdout)
        manifest['engine'] = {k: engine[k] for k in ('ServerVersion', 'Architecture', 'NCPU', 'MemTotal', 'OSType')}
        image = json.loads(docker('image', 'inspect', args.image).stdout)[0]
        manifest['image'] = {k: image[k] for k in ('Id', 'Architecture', 'Os', 'RepoDigests')}
        manifest['image']['reference'] = args.image
        native = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(engine['Architecture'], engine['Architecture'])
        assert image['Architecture'] == native, 'Use a native runtime image for this campaign'
        save()
        for suite in SUITES:
            report = out/(suite+'.json')
            command = [sys.executable, str(ROOT/'tests/resilience'/(suite+'.py')),
                       '--image', image['Id'], '--report', str(report)]
            print(f'RUN {suite}', flush=True)
            with (out/(suite+'.stdout.log')).open('w') as log:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=600)
            result = json.loads(report.read_text()) if report.exists() else {}
            passed = completed.returncode == 0 and (result.get('result') == 'PASS' or result.get('passed') is True)
            manifest['suites'].append({'name': suite, 'result': 'PASS' if passed else 'FAIL',
                                       'exit_code': completed.returncode, 'report': report.name})
            save()
            assert passed, f'{suite} failed; inspect {suite}.stdout.log and report'
        assert all(digest(ROOT/p) == h for p, h in manifest['source_sha256'].items()), 'Inputs changed during campaign'
        manifest['status'] = 'COMPLETE'
    except BaseException as exc:
        manifest['status'] = 'FAIL'
        manifest['errors'].append(repr(exc))
    finally:
        after = containers()
        manifest['containers_after'] = after
        preserved = all(after.get(cid) == entry for cid, entry in before.items() if entry['running'])
        leftovers = [e for cid, e in after.items() if cid not in before and e['name'].startswith(('i9-', 'cauce-i9-'))]
        manifest['preexisting_running_containers_preserved'] = preserved
        manifest['remaining_test_containers'] = leftovers
        manifest['remaining_test_networks'] = docker('network', 'ls', '--format', '{{.Name}}').stdout.splitlines()
        manifest['remaining_test_networks'] = [n for n in manifest['remaining_test_networks'] if n.startswith(('i9-', 'cauce-i9-'))]
        if not preserved or leftovers or manifest['remaining_test_networks']:
            manifest['status'] = 'FAIL'
            manifest['errors'].append('Container preservation or suite cleanup failed')
        manifest['finished_utc'] = now()
        manifest['host_free_bytes_after'] = shutil.disk_usage(out).free
        manifest['artifacts_sha256'] = {p.name: digest(p) for p in sorted(out.iterdir()) if p.is_file() and p.name != 'manifest.json'}
        save()
    print(json.dumps({'status': manifest['status'], 'report_dir': str(out), 'errors': manifest['errors']}))
    return 0 if manifest['status'] == 'COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
