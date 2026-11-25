#!/usr/bin/env python3
"""Black-box collector recovery/transport tests; native or real Docker containers."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kw):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kw)


def atomic(path, text):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
        temp = Path(f.name)
    temp.chmod(0o644)
    temp.replace(path)


def certs(path):
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(path/'ca.key'),
        '-out', str(path/'ca.crt'), '-days', '2', '-subj', '/CN=Cauce collector fixture CA',
        '-addext', 'basicConstraints=critical,CA:TRUE',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
        '-addext', 'subjectKeyIdentifier=hash',
        '-addext', 'authorityKeyIdentifier=keyid:always')
    run('openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(path/'server.key'),
        '-out', str(path/'server.csr'), '-subj', '/CN=sink.test')
    ext = path/'server.ext'
    ext.write_text('subjectAltName=DNS:sink.test,DNS:localhost\nextendedKeyUsage=serverAuth\n'
                   'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                   'authorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n')
    run('openssl', 'x509', '-req', '-in', str(path/'server.csr'), '-CA', str(path/'ca.crt'),
        '-CAkey', str(path/'ca.key'), '-CAcreateserial', '-out', str(path/'server.crt'),
        '-days', '2', '-sha256', '-extfile', str(ext))
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(path/'other.key'),
        '-out', str(path/'other.crt'), '-days', '2', '-subj', '/CN=Untrusted CA',
        '-addext', 'basicConstraints=critical,CA:TRUE',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
        '-addext', 'subjectKeyIdentifier=hash',
        '-addext', 'authorityKeyIdentifier=keyid:always')
    (path/'server.key').chmod(0o644)  # Disposable fixture, never packaged.


class Suite:
    def __init__(self, args):
        self.args = args
        base = Path(getattr(args, 'tempdir', ROOT/'work'))
        base.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='collector-tests-', dir=base))
        self.root.chmod(0o777)
        self.sink = self.root/'sink'
        self.sink.mkdir(mode=0o777)
        self.id = uuid.uuid4().hex[:10]
        self.network = 'cauce-collector-test-' + self.id
        self.sink_name = self.network + '-sink'
        self.cases = []
        self.proc = None
        self.active = []
        self.template = json.loads((ROOT/'examples/access-log.jsonl').read_text().splitlines()[0])
        certs(self.sink)
        self.control()
        self.port = 9443
        if args.docker:
            if subprocess.run(['docker','info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                run('colima', 'start')
            run('docker', 'network', 'create', '--internal', self.network)
            run('docker', 'run', '-d', '--name', self.sink_name, '--cpus=1', '--memory=256m',
                '--pids-limit=128', '--log-driver=local', '--log-opt=max-size=1m', '--log-opt=max-file=2',
                '--network', self.network,
                '--network-alias', 'sink.test', '-v', str(self.root)+':/fixtures',
                '-v', str(ROOT/'tests/collector/sink.py')+':/sink.py:ro',
                'python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740',
                'python', '/sink.py', '--dir', '/fixtures/sink')
            fixture_files = [self.sink/'ca.crt', self.sink/'server.crt',
                             self.sink/'server.key', self.sink/'control.json']
            expected_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in fixture_files}
            manifest_code = """import hashlib,json,pathlib,sys
p=pathlib.Path('/fixtures/sink')
print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in json.loads(sys.argv[1])},sort_keys=True))
"""
            last_manifest = None
            for _ in range(100):
                seen = subprocess.run(['docker','exec',self.sink_name,'python','-c',manifest_code,
                                       json.dumps(sorted(expected_hashes))],
                                      capture_output=True,text=True,timeout=2)
                last_manifest = seen.stderr or seen.stdout
                if seen.returncode == 0 and json.loads(seen.stdout) == expected_hashes:
                    break
                time.sleep(.05)
            else:
                sink_logs = subprocess.run(['docker','logs',self.sink_name],capture_output=True,text=True)
                subprocess.run(['docker','rm','-f',self.sink_name],capture_output=True)
                subprocess.run(['docker','network','rm',self.network],capture_output=True)
                shutil.rmtree(self.root, ignore_errors=True)
                raise AssertionError(f'Docker TLS fixture inputs not synchronized: {last_manifest}; '
                                     f'sink logs: {sink_logs.stderr[-1000:]}{sink_logs.stdout[-1000:]}')
            last_probe = None
            for _ in range(100):
                try:
                    probe = subprocess.run(['docker', 'exec', self.sink_name, 'python', '-c',
                        "import ssl,urllib.request; print(urllib.request.urlopen('https://localhost:9443/health',context=ssl.create_default_context(cafile='/fixtures/sink/ca.crt')).status)"],
                        capture_output=True, text=True, timeout=2)
                    last_probe = probe.stderr or probe.stdout
                except subprocess.TimeoutExpired as exc:
                    last_probe = f'probe timed out: {exc}'
                    time.sleep(.1)
                    continue
                if probe.returncode == 0:
                    break
                time.sleep(.1)
            else:
                subprocess.run(['docker', 'rm', '-f', self.sink_name], capture_output=True)
                subprocess.run(['docker', 'network', 'rm', self.network], capture_output=True)
                raise AssertionError(f'Docker TLS fixture not ready: {last_probe}')
        else:
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                self.port = s.getsockname()[1]
            self.proc = subprocess.Popen([sys.executable, str(ROOT/'tests/collector/sink.py'),
                                         '--dir', str(self.sink), '--port', str(self.port)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.create_default_context(cafile=str(self.sink/'ca.crt'))
            for _ in range(100):
                try:
                    urllib.request.urlopen(f'https://localhost:{self.port}/health',context=context,timeout=.2).close()
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(.05)
            else:
                raise AssertionError('native TLS fixture not ready')

    def close(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        if self.args.docker:
            for name in self.active + [self.sink_name]:
                subprocess.run(['docker','rm','-f',name], capture_output=True)
            subprocess.run(['docker','network','rm',self.network], capture_output=True)

    def control(self, **kwargs):
        atomic(self.sink/'control.json', json.dumps(kwargs))
        if self.args.docker and hasattr(self, 'port'):
            # Synchronize Colima's shared filesystem view before triggering an attempt.
            expected = (self.sink/'control.json').read_text()
            for _ in range(100):
                seen = subprocess.run(['docker','exec',self.sink_name,'cat','/fixtures/sink/control.json'],text=True,capture_output=True)
                if seen.stdout == expected:
                    break
                time.sleep(.03)
            else:
                raise AssertionError('fixture control file not synchronized')

    def path(self, path):
        return '/fixtures/' + str(path.relative_to(self.root)) if self.args.docker else str(path)

    def event(self):
        e = copy.deepcopy(self.template)
        e['event_id'] = str(uuid.uuid4())
        e['request_id'] = str(uuid.uuid4())
        return e

    def new(self, name, events=1, delivery='spool', **overrides):
        d = self.root/name
        d.mkdir(mode=0o777)
        data = [self.event() for _ in range(events)]
        (d/'access.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in data))
        (d/'token').write_text('fixture-token-v1\n')
        c = dict(access_log=self.path(d/'access.jsonl'), state_path=self.path(d/'state.db'),
                 spool_max_bytes=32768, db_max_bytes=262144, batch_max_events=4,
                 batch_max_bytes=16384, max_line_bytes=8192, max_files=8, poll_ms=50,
                 backoff_min_ms=100, backoff_max_ms=200, delivery=delivery,
                 request_timeout_ms=1000)
        if delivery == 'proposal-http':
            c.update(endpoint=f'https://{"sink.test" if self.args.docker else "localhost"}:{self.port}/ingest',
                     token_file=self.path(d/'token'), ca_file=self.path(self.sink/'ca.crt'))
        c.update(overrides)
        config = self.config(d, c)
        return d, c, config, data

    def config(self, d, c):
        config = d/('config-'+uuid.uuid4().hex+'.toml')
        config.write_text('[collector]\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k,v in c.items()))
        return config

    def command(self, config, *flags, name=None):
        tail = ['--config', self.path(config), *flags]
        if not self.args.docker:
            return [str(self.args.binary), *tail]
        cmd = ['docker','run','--rm','--network',self.network,'--read-only','--cap-drop=ALL',
               '--cpus=1','--memory=256m','--pids-limit=128','--log-driver=local',
               '--log-opt=max-size=1m','--log-opt=max-file=2',
               '--security-opt=no-new-privileges:true', '-v', str(self.root)+':/fixtures',
               '--entrypoint','/usr/local/bin/cauce-collector']
        if name:
            cmd += ['--name',name]
        return cmd + [self.args.image, *tail]

    def sync_inputs(self, config):
        if not self.args.docker:
            return
        # The test host writes through Colima's shared directory. Wait for the VM
        # to observe that fixture revision before asserting collector behavior.
        code = """import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1])
r={}
for name in json.loads(sys.argv[2]):
 f=p/name
 r[name]=hashlib.sha256(f.read_bytes()).hexdigest()
print(json.dumps(r,sort_keys=True))
"""
        import hashlib
        for _ in range(100):
            expected={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in config.parent.iterdir()
                      if f.is_file() and ('.jsonl' in f.name or f.suffix=='.toml' or f.name=='token')}
            # Look up each known path directly. Colima can cache readdir entries
            # even while opening a newly created configuration path succeeds.
            seen=subprocess.run(['docker','exec',self.sink_name,'python','-c',code,self.path(config.parent),
                                 json.dumps(sorted(expected))],
                                capture_output=True,text=True)
            if seen.returncode==0 and json.loads(seen.stdout)==expected:
                return
            time.sleep(.05)
        raise AssertionError(f'Colima fixture inputs did not converge in {config.parent.name}: '
                             f'code={seen.returncode}; stdout={seen.stdout[-1200:]}; '
                             f'stderr={seen.stderr[-1200:]}; expected={expected}')

    def rename(self, source, destination):
        if self.args.docker:
            # Rename through the guest filesystem: host-side Colima/SSHFS
            # renames can reassign guest inodes by path, unlike a Linux writer.
            run('docker','exec',self.sink_name,'python','-c',
                'import os,sys;os.rename(sys.argv[1],sys.argv[2])',
                self.path(source),self.path(destination))
        else:
            source.rename(destination)

    def invoke(self, config, *flags, ok=True):
        self.sync_inputs(config)
        p = subprocess.run(self.command(config, *flags), capture_output=True, text=True, timeout=20)
        if ok and p.returncode:
            raise AssertionError(f'collector failed ({p.returncode}): {p.stderr}')
        return p

    def status(self, config):
        return json.loads(self.invoke(config, '--status').stdout)

    def batches(self, config):
        output = self.invoke(config, '--export', 'stdout').stdout
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def received(self):
        p = self.sink/'received.jsonl'
        return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

    def passed(self, name):
        self.cases.append(name)
        print('PASS:', name, flush=True)

    def test(self):
        d,c,config,data = self.new('partial', events=2)
        (d/'access.jsonl').write_text(json.dumps(data[0])+'\n'+json.dumps(data[1]))
        self.invoke(config, '--once')
        assert self.status(config)['pending_events'] == 1
        self.invoke(config, '--once')
        assert self.status(config)['pending_events'] == 1
        with (d/'access.jsonl').open('a') as f:
            f.write('\n')
        self.invoke(config, '--once')
        assert self.status(config)['pending_events'] == 2
        batches = self.batches(config)
        assert {e['event_id'] for b in batches for e in b['events']} == {e['event_id'] for e in data}
        assert self.status(config)['pending_events'] == 2
        self.passed('complete-line tail, durable restart checkpoints and non-destructive export')

        self.rename(d/'access.jsonl', d/'access.jsonl.1')
        third = self.event()
        (d/'access.jsonl').write_text(json.dumps(third)+'\n')
        for _ in range(3): self.invoke(config, '--once')
        assert self.status(config)['pending_events'] == 3
        self.passed('rename rotation preserves inode checkpoint and discovers new generation')

        # A separate source with multiple retained generations is read oldest first.
        d,c,config,data = self.new('retained', events=1)
        old1,old2=self.event(),self.event()
        (d/'access.jsonl.2').write_text(json.dumps(old2)+'\n')
        (d/'access.jsonl.1').write_text(json.dumps(old1)+'\n')
        for _ in range(5): self.invoke(config,'--once')
        exported = [e for b in self.batches(config) for e in b['events']]
        assert [e['event_id'] for e in exported] == [old2['event_id'],old1['event_id'],data[0]['event_id']]
        self.passed('startup discovers retained rotated files oldest first')

        d,c,config,data = self.new('quota', events=100, spool_max_bytes=8192,
                                  batch_max_bytes=8192,max_line_bytes=4096)
        for _ in range(12): self.invoke(config,'--once')
        s = self.status(config)
        assert 0 < s['pending_events'] < 100 and s['pending_bytes'] <= 8192, s
        assert s['db_bytes'] <= c['db_max_bytes'], s
        self.invoke(config,'--once')
        assert self.status(config)['pending_events'] == s['pending_events']
        self.passed('bounded spool pauses input without advancing checkpoint past durable events')

        for case,text in [('invalid','{"event_id":"bad"}\n'),('oversized','x'*20000+'\n')]:
            d,c,config,_ = self.new(case,events=0)
            (d/'access.jsonl').write_text(text)
            p=self.invoke(config,'--once',ok=False)
            assert self.status(config)['pending_events'] == 0
            assert p.returncode or p.stderr, 'invalid input must be observable'
            self.passed(case+' source is rejected without checkpointing or uploading')

        d,c,config,data = self.new('https', delivery='proposal-http')
        self.control()
        self.invoke(config,'--once')
        assert self.status(config)['pending_events'] == 0
        assert self.received()[-1]['batch']['events'][0]['event_id'] == data[0]['event_id']
        self.passed('HTTPS with trusted CA sends proposal batch and validates ACK')

        for status in [401,403,429,500,307]:
            d,c,config,data = self.new('http-'+str(status),delivery='proposal-http')
            self.control(status=status)
            before=len(self.received())
            self.invoke(config,'--once',ok=False)
            assert self.status(config)['pending_events'] == 1
            assert len(self.received())==before+1, 'one attempt, no redirects or hidden retries'
            first=self.batches(config)[0]['batch_id']
            self.control()
            self.invoke(config,'--once')
            assert self.status(config)['pending_events']==0
            assert self.received()[-1]['batch']['batch_id']==first
            self.passed(f'HTTP {status} retains stable batch; retry succeeds; redirects never followed')

        for mode in ['wrong_id','wrong_count','large_ack','drop_ack','delay_ms']:
            d,c,config,data=self.new('ack-'+mode,delivery='proposal-http')
            self.control(**{mode: 1500 if mode=='delay_ms' else True})
            self.invoke(config,'--once',ok=False)
            assert self.status(config)['pending_events']==1
            first=self.batches(config)[0]['batch_id']
            self.control()
            self.invoke(config,'--once')
            assert self.status(config)['pending_events']==0
            assert self.received()[-1]['batch']['batch_id']==first
            self.passed(mode+' retains durable batch and stable event IDs for retry/dedup')

        for kind in ['ca','hostname']:
            d,c,config,_=self.new('tls-'+kind,delivery='proposal-http')
            if kind=='ca': c['ca_file']=self.path(self.sink/'other.crt')
            else: c['endpoint']=c['endpoint'].replace('sink.test','127.0.0.1') if self.args.docker else c['endpoint'].replace('localhost','127.0.0.1')
            if self.args.docker and kind=='hostname':
                ip=json.loads(run('docker','inspect',self.sink_name).stdout)[0]['NetworkSettings']['Networks'][self.network]['IPAddress']
                c['endpoint']=f'https://{ip}:9443/ingest'
            config=self.config(d,c)
            before=len(self.received())
            self.invoke(config,'--once',ok=False)
            assert self.status(config)['pending_events']==1
            assert len(self.received())==before
            self.passed('TLS rejects untrusted '+kind+' before sending credentials/events')

        d,c,config,_=self.new('credential-rotation',delivery='proposal-http')
        self.control(token='fixture-token-v2')
        self.invoke(config,'--once',ok=False)
        assert self.status(config)['pending_events']==1
        atomic(d/'token','fixture-token-v2\n')
        self.invoke(config,'--once')
        assert self.status(config)['pending_events']==0
        self.passed('credential file is reread on retry; rotated token accepted')

        # Inject an ACK transaction failure after the receiver has returned success.
        # SQLite is only modified while the collector process is stopped.
        d,c,config,_=self.new('ack-commit-failure',delivery='proposal-http')
        self.control(status=500)
        self.invoke(config,'--once',ok=False)
        first=self.batches(config)[0]
        with sqlite3.connect(d/'state.db') as db:
            db.execute("CREATE TRIGGER fail_ack BEFORE DELETE ON spool BEGIN SELECT RAISE(ABORT,'injected ACK persistence failure'); END")
        self.control()
        p=self.invoke(config,'--once',ok=False)
        assert p.returncode != 0
        assert self.received()[-1]['status']==200
        assert self.status(config)['pending_events']==1
        with sqlite3.connect(d/'state.db') as db:
            db.execute('DROP TRIGGER fail_ack')
        self.invoke(config,'--once')
        assert self.status(config)['pending_events']==0
        assert self.received()[-1]['batch']==first
        self.passed('ACK received but dequeue transaction fails: durable batch is retried intact')

        # Real kill while receiver accepted a batch but deliberately withholds ACK.
        d,c,config,data=self.new('kill-recovery',delivery='proposal-http',request_timeout_ms=10000)
        self.control(delay_ms=5000)
        before=len(self.received())
        name=self.network+'-kill'
        self.sync_inputs(config)
        p=subprocess.Popen(self.command(config,'--once',name=name),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        if self.args.docker: self.active.append(name)
        for _ in range(100):
            if len(self.received())>before: break
            time.sleep(.05)
        else: raise AssertionError('collector never delivered pending batch before kill')
        first=self.received()[-1]['batch']
        if self.args.docker:
            run('docker','kill','--signal','KILL',name)
        else: p.kill()
        p.communicate(timeout=10)
        assert self.status(config)['pending_events']==1
        self.control()
        self.invoke(config,'--once')
        assert self.status(config)['pending_events']==0
        assert self.received()[-1]['batch']==first
        self.passed('SIGKILL after receiver acceptance: restart resends identical durable batch')

        # Run loop, lock exclusion, actual exponential retry spacing, then clean termination.
        d,c,config,_=self.new('loop',delivery='proposal-http')
        self.control(status=500)
        before=len(self.received())
        name=self.network+'-loop'
        self.sync_inputs(config)
        p=subprocess.Popen(self.command(config,name=name),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        if self.args.docker: self.active.append(name)
        for _ in range(100):
            if len(self.received())>=before+3: break
            time.sleep(.05)
        else: raise AssertionError('loop did not retry')
        concurrent=self.invoke(config,'--once',ok=False)
        assert concurrent.returncode != 0, 'two collector writers must not own same state'
        times=[r['time'] for r in self.received()[before:]]
        assert min(b-a for a,b in zip(times,times[1:])) >= .04, times
        if self.args.docker: run('docker','stop','--time','3',name)
        else: p.terminate()
        p.communicate(timeout=10)
        assert p.returncode == 0, 'SIGTERM must close the collector cleanly'
        self.passed('bounded retry backoff, exclusive state ownership, clean SIGTERM')

    def report(self):
        arch=run('docker','image','inspect',self.args.image,'--format','{{.Architecture}}').stdout.strip() if self.args.docker else os.uname().machine
        result=dict(result='PASS', mode='docker' if self.args.docker else 'native', architecture=arch,
                    fixture='Local proposal-1 only; no Connectivity ingestion', cases=self.cases)
        if self.args.docker:
            result['image_id']=run('docker','image','inspect',self.args.image,'--format','{{.Id}}').stdout.strip()
        (ROOT/'docs'/f'collector-{"docker" if self.args.docker else "native"}.json').write_text(json.dumps(result,indent=2)+'\n')
        print(f'PASS: {len(self.cases)} collector cases ({arch})')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--docker',action='store_true')
    p.add_argument('--image',default='cauce-collector:test')
    p.add_argument('--binary',type=Path,default=ROOT/'target/debug/cauce-collector')
    args=p.parse_args()
    suite=Suite(args)
    try:
        suite.test()
        suite.report()
    finally:
        suite.close()

if __name__=='__main__': main()
