#!/usr/bin/env python3
"""Run the real collector against an authorized, unchanged copy of Connectivity.

The adapter/authenticator/sink are real source; Moto simulates AWS services.
All execution is in one network-disabled Docker container with bounded tmpfs.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / 'tests/connectivity_real'
spec = importlib.util.spec_from_file_location('reference_snapshot', LAB / 'snapshot.py')
snapshot_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot_module)


def run(*args, check=True, timeout=30, data=None):
    p = subprocess.run(list(args), input=data, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        # Commands contain no credentials. Server logs and bodies are not dumped here.
        raise RuntimeError(f'{args[0]} failed, exit={p.returncode}: {p.stderr[-1000:]}')
    return p


def docker(*args, **kwargs):
    return run('docker', *args, **kwargs)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Lab:
    def __init__(self, args, work):
        self.args, self.work = args, work
        self.name = 'cauce-real-connectivity-' + uuid.uuid4().hex[:12]
        self.extract_name = self.name + '-extract'
        self.started = False
        self.requests = []

    def start(self):
        snapshot = self.work / 'reference'
        self.sources = snapshot_module.snapshot(self.args.reference, snapshot)
        docker('create', '--name', self.extract_name, self.args.image)
        try:
            self.binary = self.work / 'cauce-collector'
            docker('cp', self.extract_name + ':/usr/local/bin/cauce-collector', str(self.binary))
            self.binary.chmod(0o555)
        finally:
            docker('rm', self.extract_name, check=False)
        self.binary_hash = sha(self.binary)
        self.started = True  # Cleanup also covers a partially failed docker run.
        docker('run', '-d', '--name', self.name, '--network', 'none', '--read-only',
               '--user', '10001:10001', '--cpus', '1', '--memory', '512m', '--pids-limit', '96',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--log-driver', 'local', '--log-opt', 'max-size=1m', '--log-opt', 'max-file=2',
               '--tmpfs', '/sandbox:rw,size=32m,uid=10001,gid=10001,mode=0700,nr_inodes=2048',
               '--tmpfs', '/tmp:rw,size=16m,uid=10001,gid=10001,mode=0700,nr_inodes=1024',
               '-v', str(snapshot) + ':/reference:ro',
               '-v', str(LAB / 'server.py') + ':/lab/server.py:ro',
               '-v', str(self.binary) + ':/collector:ro',
               '-v', str(ROOT / 'examples/access-log.jsonl') + ':/fixture/event-template.jsonl:ro',
               self.args.lab_image, '--event-template', '/fixture/event-template.jsonl')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                health = self.api('health')
                if health.get('ready') is True:
                    break
            except (RuntimeError, ValueError):
                time.sleep(.2)
        else:
            state = json.loads(docker('inspect', self.name).stdout)[0]['State']
            logs = docker("logs", self.name).stdout + docker("logs", self.name).stderr
            self.assert_no_secrets(logs)
            raise RuntimeError(f'isolated adapter did not start: status={state["Status"]}, exit={state["ExitCode"]}, logs={logs[-2000:]}')
        inspection = json.loads(docker('inspect', self.name).stdout)[0]
        hc = inspection['HostConfig']
        assert hc['NetworkMode'] == 'none' and hc['ReadonlyRootfs']
        self.limits = {'network': hc['NetworkMode'], 'readonly_rootfs': hc['ReadonlyRootfs'],
                       'memory_bytes': hc['Memory'], 'nano_cpus': hc['NanoCpus'],
                       'pids_limit': hc['PidsLimit'], 'tmpfs': hc['Tmpfs'],
                       'user': inspection['Config']['User'], 'image_id': inspection['Image']}

    def api(self, path, body=None):
        code = '''import json,ssl,sys,urllib.request
p=json.load(sys.stdin)
body=None if p['body'] is None else json.dumps(p['body']).encode()
req=urllib.request.Request('https://127.0.0.1:9443/_lab/'+p['path'],data=body,headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req,context=ssl.create_default_context(cafile='/sandbox/ca.pem'),timeout=8) as r:
 print(r.read().decode())
'''
        p = docker('exec', '-i', self.name, 'python', '-c', code,
                   data=json.dumps({'path': path, 'body': body}), timeout=12)
        return json.loads(p.stdout)

    def control(self, mode):
        return self.api('control', {'action': mode})

    def prepare(self, name, count=2, delivery='proposal-http', spool=16384):
        # Secrets remain in tmpfs. Input consists only of synthetic log records and config.
        code = '''import datetime,json,pathlib,sys,uuid
p=json.load(sys.stdin); root=pathlib.Path('/sandbox/cases')/p['name'];root.mkdir(parents=True)
base=json.loads(pathlib.Path('/fixture/event-template.jsonl').read_text().splitlines()[0]); rows=[]
for i in range(p['count']):
 e=dict(base);e['event_id']=str(uuid.uuid4());e['request_id']=str(uuid.uuid4());e['timestamp']=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z');rows.append(e)
(root/'access.jsonl').write_text(''.join(json.dumps(e,separators=(',',':'))+'\\n' for e in rows))
c=dict(access_log=str(root/'access.jsonl'),state_path=str(root/'state.db'),delivery=p['delivery'],spool_max_bytes=p['spool'],db_max_bytes=262144,batch_max_events=2,batch_max_bytes=4096,max_line_bytes=4096,max_files=4,poll_ms=50,backoff_min_ms=100,backoff_max_ms=300,request_timeout_ms=4000)
if p['delivery']=='proposal-http':c.update(endpoint='https://127.0.0.1:9443/v1/ingest/cauce-proposal',token_file='/sandbox/token',ca_file='/sandbox/ca.pem')
(root/'collector.toml').write_text('[collector]\\n'+''.join(k+' = '+json.dumps(v)+'\\n' for k,v in c.items()))
print(json.dumps({'event_ids':[e['event_id'] for e in rows],'source_bytes':(root/'access.jsonl').stat().st_size}))
'''
        return json.loads(docker('exec', '-i', self.name, 'python', '-c', code,
                                data=json.dumps(dict(name=name, count=count, delivery=delivery, spool=spool))).stdout)

    def collect(self, name, success=True):
        result = docker('exec', self.name, '/collector', '--config',
                        f'/sandbox/cases/{name}/collector.toml', '--once', check=False, timeout=15)
        assert (result.returncode == 0) == success, f'{name}: unexpected collector exit={result.returncode}'
        # Error text is captured only after scanning against the actual ephemeral tokens.
        self.assert_no_secrets(result.stdout + result.stderr)
        return {'exit_code': result.returncode, 'stderr': result.stderr.strip()}

    def assert_no_secrets(self, text):
        # Check inside container; never return the secret bytes to the host.
        code = '''import pathlib,sys
text=sys.stdin.read(); secrets=pathlib.Path('/sandbox/secrets.json').read_text()
import json
assert all(s not in text for s in json.loads(secrets)), 'secret detected in collector output'
'''
        docker('exec', '-i', self.name, 'python', '-c', code, data=text)

    def state(self, name):
        code = '''import hashlib,json,pathlib,sqlite3,sys
root=pathlib.Path('/sandbox/cases')/sys.argv[1]
with sqlite3.connect('file:'+str(root/'state.db')+'?mode=ro',uri=True) as db:
 batches=[{'batch_id':b,'sha256':hashlib.sha256(body).hexdigest(),'events':[e['event_id'] for e in json.loads(body)['events']]} for b,body in db.execute('SELECT batch_id,body FROM spool ORDER BY seq')]
 offsets=[r[0] for r in db.execute('SELECT offset FROM checkpoints')]
 print(json.dumps({'batches':batches,'offsets':offsets,'integrity':db.execute('PRAGMA integrity_check').fetchone()[0]}))
'''
        return json.loads(docker('exec', self.name, 'python', '-c', code, name).stdout)

    def close(self):
        if self.started:
            docker('rm', '-f', self.name, check=False)
        docker('rm', '-f', self.extract_name, check=False)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--reference', type=Path, required=True, help='authorized source checkout, read-only')
    p.add_argument('--image', default='cauce:0.9.1-connectivity-arm64')
    p.add_argument('--lab-image', default='cauce-connectivity-real-lab:local')
    p.add_argument('--report-dir', type=Path, required=True)
    args = p.parse_args()
    if docker('info', check=False).returncode:
        run('colima', 'start', timeout=180)
        docker('info')
    args.report_dir.mkdir(parents=True, exist_ok=False)
    base = ROOT / 'work'; base.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='connectivity-real-', dir=base))
    lab = Lab(args, work)
    before = docker('ps', '--format', '{{.ID}} {{.Names}}').stdout.splitlines()
    report = {'result': 'INCOMPLETE', 'scope': 'real copied Connectivity adapter/authenticator/sink; Moto AWS simulation; no cloud deployment',
              'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'cases': []}
    try:
        lab.start()
        report.update(collector_image=args.image, collector_image_id=docker('image','inspect',args.image,'--format','{{.Id}}').stdout.strip(),
                      collector_binary_sha256=lab.binary_hash, reference_sha256=lab.sources, container=lab.limits)
        # Scenarios are finalized against the server control/report contract below.
        execute_cases(lab, report)
        report['server'] = lab.api('report')
        logs = docker('logs', lab.name).stdout + docker('logs', lab.name).stderr
        lab.assert_no_secrets(logs)
        report['server_logs_no_secrets'] = True
        report['reference_unchanged'] = snapshot_module.verify_original(args.reference, lab.sources)
        assert report['reference_unchanged']
        report['result'] = 'PASS'
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        lab.close()
        shutil.rmtree(work)
        report['completed_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report['owned_container_remaining'] = bool(docker('ps','-a','--filter','name='+lab.name,'--format','{{.Names}}').stdout.strip())
        after = docker('ps','--format','{{.ID}} {{.Names}}').stdout.splitlines()
        report['preexisting_running_containers_preserved'] = set(before) <= set(after)
        report['temporary_directory_removed'] = not work.exists()
        (args.report_dir/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        if report['result']=='PASS':
            assert not report['owned_container_remaining'] and report['preexisting_running_containers_preserved']
            print(json.dumps({'result':'PASS','cases':len(report['cases']),'report':str(args.report_dir/'report.json')}))


def execute_cases(lab, report):
    expected_ids = set()
    def drained(name, ids):
        state = lab.state(name)
        assert state['integrity'] == 'ok' and not state['batches'], state
        expected_ids.update(ids)
        storage = lab.api('report')['storage']
        assert set(storage['event_ids']) == expected_ids, storage
        return state, storage

    source = lab.prepare('normal')
    lab.collect('normal')
    state, storage = drained('normal', source['event_ids'])
    assert storage['receipt_count'] == 1 and storage['outbox_count'] == 1
    report['cases'].append({'name': 'delivery', 'state': state, 'storage': storage})

    lab.control('rotate_token')
    source = lab.prepare('lost-ack')
    lab.control('lose_ack_once')
    lab.collect('lost-ack', success=False)
    pending = lab.state('lost-ack')
    assert len(pending['batches']) == 1
    before = lab.api('report')['storage']
    lab.collect('lost-ack')
    state, after = drained('lost-ack', source['event_ids'])
    for key in ('receipt_count', 'outbox_count', 'object_version_count', 'cost_microusd'):
        assert before[key] == after[key], (key, before, after)
    requests = lab.api('report')['requests'][-2:]
    assert requests[0]['payload_sha256'] == requests[1]['payload_sha256']
    assert requests[0]['batch_id'] == requests[1]['batch_id']
    assert requests[0]['internal_status'] == 200 and requests[0]['wire_status'] == 503
    report['cases'].append({'name': 'lost_ack_replay', 'pending': pending, 'state': state,
                            'storage_unchanged_on_replay': True})

    for mode in ('expire_token_once', 'revoke_token_once', 'disable_namespace_once',
                 'unauthorized_namespace_once', 'revoke_token_before_commit_once',
                 'disable_namespace_before_commit_once'):
        lab.control('rotate_token')
        lab.control('restore_namespace')
        source = lab.prepare(mode)
        before = lab.api('report')['storage']
        lab.control(mode)
        lab.collect(mode, success=False)
        pending = lab.state(mode)
        assert len(pending['batches']) == 1 and pending['integrity'] == 'ok'
        denied = lab.api('report')
        request = denied['requests'][-1]
        assert request['wire_status'] != 200 and not request.get('handler_error'), request
        assert denied['storage']['receipt_count'] == before['receipt_count']
        assert denied['storage']['outbox_count'] == before['outbox_count']
        if 'before_commit' in mode:
            assert request['manifest_mutation_fired'], request
        lab.control('restore_namespace')
        lab.control('rotate_token')
        lab.collect(mode)
        state, after = drained(mode, source['event_ids'])
        recovery = lab.api('report')['requests'][-1]
        assert recovery['batch_id'] == pending['batches'][0]['batch_id']
        assert recovery['payload_sha256'] == pending['batches'][0]['sha256']
        report['cases'].append({'name': mode, 'denial_status': request['wire_status'],
                                'no_receipt_or_outbox_on_denial': True,
                                'manifest_mutation_fired': request['manifest_mutation_fired'],
                                'same_batch_recovered': True, 'state': state})


if __name__ == '__main__':
    main()
