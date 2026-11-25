#!/usr/bin/env python3
"""Real ENOSPC/rotation failure with a bounded Docker tmpfs and proxy recovery."""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NODE = 'node:22.14.0-alpine3.21@sha256:9bef0ef1e268f60627da9ba7d7605e8831d5b56ad07487d24d1aa386336d1944'
PYTHON = 'python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740'
TOKEN = 'increment9-local-logger-probe-token'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='cauce:0.9-final-arm64')
    parser.add_argument('--report', type=Path, default=ROOT/'docs/increment9-log-disk.json')
    args = parser.parse_args()
    report = args.report.resolve(); report.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    colima_socket = Path.home()/'.colima/default/docker.sock'
    if colima_socket.exists():
        env.setdefault('DOCKER_HOST', 'unix://' + str(colima_socket))
    def cmd(*parts, check=True, timeout=30):
        p = subprocess.run(['docker', *map(str, parts)], env=env, text=True,
                           capture_output=True, timeout=timeout)
        if check and p.returncode:
            raise RuntimeError(f'docker {parts[:3]} failed: {p.stderr or p.stdout}')
        return p
    if cmd('info', check=False).returncode:
        subprocess.run(['colima', 'start'], check=True, timeout=180)
        cmd('info')
    suffix = uuid.uuid4().hex[:10]
    prefix = 'i9-log-' + suffix
    work = ROOT/'work/increment9'/prefix; work.mkdir(parents=True)
    network = prefix + '-net'; containers = []
    result = {'result': 'RUNNING', 'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'run_id': prefix, 'cases': [], 'images': {}, 'temporary_filesystem_bytes': 4*1024**2,
              'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (Path(__file__), HERE/'log_client.py', HERE/'log_backend.mjs')},
              'published_ports': [], 'connectivity_contacted': False}
    def run(name, image, options, command):
        containers.append(name)
        cmd('run', '-d', '--name', name, '--label', 'cauce.increment9='+prefix,
            *([] if '--network' in options else ['--network', network]),
            '--memory', '256m', '--cpus', '1', '--pids-limit', '64',
            '--ulimit', 'nofile=1024:1024', '--log-driver', 'json-file',
            '--log-opt', 'max-size=1m', '--log-opt', 'max-file=2',
            *options, image, *command)
        return name
    try:
        for name, image in [('cauce', args.image), ('node', NODE), ('python', PYTHON)]:
            info = json.loads(cmd('image', 'inspect', image).stdout)[0]
            result['images'][name] = {'reference': image, 'id': info['Id'], 'architecture': info['Architecture']}
        cmd('network', 'create', '--internal', '--label', 'cauce.increment9='+prefix, network)
        run(prefix+'-backend', NODE, ['--network-alias', 'backend', '--read-only', '--cap-drop', 'ALL',
            '-v', str(HERE/'log_backend.mjs')+':/backend.mjs:ro'], ['node', '/backend.mjs'])
        for kind in ('enospc', 'rotation'):
            config = work/(kind+'.toml')
            text = (ROOT/'bench/increment8/configs/cauce-h2-clear.toml').read_text()
            text = text.replace('name="bench8-h2-clear"', 'name="cauce"')
            text = text.replace('address="backend-h2:8000"', 'address="backend:8000"')
            text = text.replace('protocol="http2"', 'protocol="http1"')
            text = text.replace('idle_connection_ms=15000', 'idle_connection_ms=250')
            text = text.replace('enabled=false\npath=', 'enabled=true\npath=')
            text = text.replace('queue=4096', 'queue=16')
            text = text.replace('rotate_bytes=67108864', 'rotate_bytes='+str(16*1024**2 if kind=='enospc' else 4096))
            text = text.replace('retained_files=3', 'retained_files=1')
            config.write_text(text)
            proxy = run(prefix+'-'+kind, args.image,
                ['--network-alias', 'proxy', '--read-only', '--cap-drop', 'ALL',
                 '--security-opt', 'no-new-privileges', '--pids-limit', '128',
                 '-e', 'CAUCE_ADMIN_TOKEN='+TOKEN, '-e', 'TOKIO_WORKER_THREADS=1',
                 '--tmpfs', '/var/log/cauce:rw,size=4m,uid=10001,gid=10001,mode=0770',
                 '-v', str(config)+':/etc/cauce/cauce.toml:ro'], ['/etc/cauce/cauce.toml'])
            client = run(prefix+'-'+kind+'-client', PYTHON,
                ['--network', 'container:'+proxy, '--pid', 'container:'+proxy, '--read-only',
                 '--user', '10001:10001', '--cap-drop', 'ALL',
                 '-e', 'CAUCE_ADMIN_TOKEN='+TOKEN, '-v', str(HERE/'log_client.py')+':/client.py:ro'],
                ['sleep', '600'])
            def probe(action, *extra):
                return json.loads(cmd('exec', client, 'python', '/client.py', action, *extra).stdout)
            until = time.monotonic()+10
            while True:
                try: before = probe('sample'); break
                except Exception:
                    if time.monotonic()>until: raise
                    time.sleep(.1)
            warmup = probe('batch', '2', '1'); time.sleep(.2)
            result['cases'].append({'name': kind, 'result': 'RUNNING',
                                    'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest()})
            if kind == 'enospc':
                fill = cmd('exec', proxy, 'dd', 'if=/dev/zero', 'of=/var/log/cauce/filler',
                           'bs=65536', 'count=128', check=False)
                assert fill.returncode != 0 and 'No space left on device' in fill.stderr, fill.stderr
                free = cmd('exec', proxy, 'stat', '-f', '-c', '%a %S', '/var/log/cauce').stdout.strip()
                assert int(free.split()[0]) == 0, free
                injected = {'errno': 'ENOSPC', 'fill_exit': fill.returncode, 'stderr': fill.stderr,
                            'available_blocks_and_block_bytes': free}
            else:
                cmd('exec', proxy, 'mkdir', '/var/log/cauce/access.jsonl.1')
                injected = {'rotation_destination': 'directory prevents rename over retained file'}
            traffic = probe('batch', '256', '8'); time.sleep(.25)
            during = probe('sample')
            result['cases'][-1].update({'injection': injected, 'before': before, 'during': during,
                                       'requests_during_failure': len(traffic)})
            raw = report.with_name(report.stem+'-'+kind+'-requests.jsonl')
            raw.write_text(''.join(json.dumps(r)+'\n' for r in warmup+traffic))
            assert during['metrics']['cauce_log_io_errors_total'] > before['metrics']['cauce_log_io_errors_total'], during
            assert len(traffic) == 256 and all(r['status']==200 for r in traffic)
            cmd('exec', proxy, 'rm' if kind=='enospc' else 'rmdir',
                '/var/log/cauce/filler' if kind=='enospc' else '/var/log/cauce/access.jsonl.1')
            # Low-rate recovery avoids overflowing the intentionally small logger queue.
            recovery = probe('batch', '2', '1'); time.sleep(.25)
            def read_events():
                paths = cmd('exec', proxy, 'find', '/var/log/cauce', '-maxdepth', '1', '-type', 'f',
                            '-name', 'access.jsonl*').stdout.splitlines()
                lines = []
                for p in paths:
                    lines.extend(cmd('exec', proxy, 'cat', p).stdout.splitlines())
                report.with_name(report.stem+'-'+kind+'-logs.jsonl').write_text('\n'.join(lines)+'\n')
                return [json.loads(line) for line in lines if line.strip()], paths
            events, paths = read_events()
            ids = {e['request_id'] for e in events}
            assert {r['request_id'] for r in recovery} <= ids, 'post-recovery log records missing'
            until = time.monotonic()+5
            while True:
                after = probe('sample')
                keys = ('cauce_requests_active','cauce_clients_active','cauce_upstream_streams_active',
                        'cauce_http2_tasks_active','cauce_upstream_connections_in_use')
                if all(after['metrics'][key]==0 for key in keys): break
                assert time.monotonic()<until, after
                time.sleep(.1)
            assert after['file_descriptors'] <= before['file_descriptors']+2, (before,after)
            inspect = json.loads(cmd('inspect', proxy).stdout)[0]
            assert inspect['State']['Running'] and not inspect['State']['OOMKilled'] and inspect['RestartCount']==0
            limits = inspect['HostConfig']
            assert limits['Memory']==256*1024**2 and limits['NanoCpus']==10**9
            assert inspect['Config']['User']=='10001:10001' and limits['ReadonlyRootfs']
            assert len(paths)<=2
            case = {'name':kind, 'result':'PASS', 'injection':injected, 'before':before,
                    'during':during, 'after':after, 'requests_during_failure':len(traffic),
                    'recovery_requests':recovery, 'recovery_events_present':True,
                    'valid_jsonl_events_retained':len(events), 'log_files':paths,
                    'max_request_ms':max(r['elapsed_ms'] for r in traffic),
                    'limits':{k:limits[k] for k in ('Memory','NanoCpus','PidsLimit','Tmpfs','ReadonlyRootfs')},
                    'restart_count':inspect['RestartCount'], 'oom_killed':inspect['State']['OOMKilled']}
            raw.write_text(''.join(json.dumps(r)+'\n' for r in warmup+traffic+recovery))
            report.with_name(report.stem+'-'+kind+'-logs.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            case['raw_requests']=raw.name
            case['config_sha256'] = result['cases'][-1]['config_sha256']
            result['cases'][-1] = case
            cmd('rm', '-f', client, proxy)
        result['result']='PASS'
    except BaseException as exc:
        result['result']='FAIL'; result['error']=repr(exc)
        raise
    finally:
        for container in reversed(containers):
            logs=cmd('logs', container, check=False)
            if logs.stdout or logs.stderr:
                (work/(container+'.log')).write_text(logs.stdout+logs.stderr)
            cmd('rm', '-f', container, check=False)
        cmd('network', 'rm', network, check=False)
        remaining=cmd('ps','-aq','--filter','label=cauce.increment9='+prefix).stdout.strip()
        result['cleanup_remaining_containers']=remaining.splitlines()
        result['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        report.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'result':result['result'],'report':str(report),'cases':len(result['cases'])}))


if __name__ == '__main__': main()
