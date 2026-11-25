#!/usr/bin/env python3
"""Reproducible, isolated NGINX 1.30.6 vs Cauce HTTP/1.1 benchmark.

The only measured proxy is active at a time. Docker's one-shot Engine stats
endpoint supplies cgroup CPU/memory and throttling counters; a PID-sharing
sidecar records proxy RSS/threads/FDs without running probes inside the proxy.
"""
from __future__ import annotations
import argparse, datetime as dt, http.client, json, os, pathlib, platform, random, re, shlex
import shutil, socket, subprocess, sys, time, urllib.parse, uuid
from summarize import verify_image_architectures

HERE=pathlib.Path(__file__).resolve().parent
ROOT=HERE.parents[1]
SCENARIOS=('normal','delayed','saturation','tls-warm','tls-cold','sse','slow-reader','ws')
SCENARIO_RATES={'normal':500,'delayed':64,'saturation':128,'tls-warm':500,'tls-cold':100,'sse':40,'slow-reader':20,'ws':100}
STACKS=('cauce','nginx')
REQUIRED_STATS=('requests','active','peak_active','new_connections','reused_requests','connections','peak_connections','identities','run_id','tls_handshakes','tls_full_handshakes','tls_resumed_handshakes')

def run(cmd, *, env, check=True, timeout=60, capture=True):
    p=subprocess.run([str(x) for x in cmd],env=env,text=True,stdout=subprocess.PIPE if capture else None,
                     stderr=subprocess.STDOUT if capture else None,timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(map(str,cmd))}\n{p.stdout or ''}")
    return p

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self,path): super().__init__('localhost'); self.path=path
    def connect(self):
        s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(4); s.connect(self.path); self.sock=s

def docker_api(env, path):
    host=env.get('DOCKER_HOST','')
    if host.startswith('unix://'):
        c=UnixHTTPConnection(host[7:])
    else:
        raise RuntimeError('increment7 requires Docker Engine through a Unix DOCKER_HOST socket')
    c.request('GET',path); r=c.getresponse(); body=r.read(); c.close()
    if r.status!=200: raise RuntimeError(f'Docker Engine {path} returned HTTP {r.status}: {body[:400]!r}')
    return json.loads(body)

def inspect(env, target): return json.loads(run(['docker','inspect',target],env=env).stdout)[0]

def stats(env, cid):
    d=docker_api(env,f'/v1.41/containers/{cid}/stats?stream=false&one-shot=true')
    cpu=d.get('cpu_stats',{}); mem=d.get('memory_stats',{}); pids=d.get('pids_stats',{})
    cpuval=cpu.get('cpu_usage',{}).get('total_usage'); sysval=cpu.get('system_cpu_usage')
    cpus=len(cpu.get('cpu_usage',{}).get('percpu_usage') or []) or cpu.get('online_cpus',1)
    return {'container_id':cid,'cpu_total_ns':cpuval,'system_cpu_ns':sysval,'online_cpus':cpus,
            'memory_usage_bytes':mem.get('usage'),'memory_cache_bytes':mem.get('stats',{}).get('cache'),
            'memory_limit_bytes':mem.get('limit'),'pids':pids.get('current'),
            'throttling':cpu.get('throttling_data',{}), 'networks':d.get('networks',{}),
            'timestamp':d.get('read')}

def cpu_delta(baseline,final):
    def stamp(s):
        value=s.get('timestamp')
        if not value: return None
        return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    t0,t1=stamp(baseline),stamp(final)
    elapsed=(t1-t0) if t0 is not None and t1 is not None else None
    cpu=(final.get('cpu_total_ns') or 0)-(baseline.get('cpu_total_ns') or 0)
    throttling={}
    for k,v in final.get('throttling',{}).items():
        if isinstance(v,(int,float)) and isinstance(baseline.get('throttling',{}).get(k),(int,float)):
            throttling[k]=v-baseline['throttling'][k]
    return {'elapsed_from_engine_s':elapsed,'cpu_delta_ns':cpu,
            'cpu_cores':cpu/(elapsed*1e9) if elapsed and elapsed>0 else None,
            'cpu_percent_of_one_core':100*cpu/(elapsed*1e9) if elapsed and elapsed>0 else None,
            'throttling_delta':throttling}

def create_certificates(certdir):
    certdir.mkdir(parents=True,exist_ok=True)
    # One ephemeral local CA signs both ingress and backend certificates.
    commands=[
      ['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=Cauce Increment7 Test CA','-keyout',str(certdir/'ca.key'),'-out',str(certdir/'ca.crt')],
    ]
    for name,cn,dns in [('proxy','proxy.test','DNS:proxy,DNS:proxy.test'),('backend','backend.test','DNS:backend,DNS:backend.test')]:
        conf=certdir/f'{name}.cnf'
        conf.write_text(f'[req]\ndistinguished_name=dn\nreq_extensions=v3_req\nprompt=no\n[dn]\nCN={cn}\n[v3_req]\nsubjectAltName={dns}\nextendedKeyUsage=serverAuth\n')
        commands += [
          ['openssl','req','-new','-newkey','rsa:2048','-nodes','-keyout',str(certdir/f'{name}.key'),'-out',str(certdir/f'{name}.csr'),'-config',str(conf)],
          ['openssl','x509','-req','-in',str(certdir/f'{name}.csr'),'-CA',str(certdir/'ca.crt'),'-CAkey',str(certdir/'ca.key'),'-CAcreateserial','-days','2','-out',str(certdir/f'{name}.crt'),'-extensions','v3_req','-extfile',str(conf)]
        ]
    for c in commands: subprocess.run(c,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    # This short-lived lab CA is not a deployable credential; remove the CA private key after signing.
    (certdir/'ca.key').unlink(missing_ok=True)
    for name in ('ca.crt','proxy.key','proxy.crt','backend.key','backend.crt'): (certdir/name).chmod(0o644)
    for f in certdir.glob('*.csr'): f.unlink(missing_ok=True)
    for f in certdir.glob('*.cnf'): f.unlink(missing_ok=True)
    (certdir/'ca.srl').unlink(missing_ok=True)

def render_nginx(template,dest, tls, logs, log_path):
    s=template.read_text()
    s=s.replace('__ACCESS_LOG__',f'access_log {log_path} increment7;' if logs else 'access_log off;')
    s=s.replace('__BACKEND_PORT__','8443' if tls else '8000')
    s=s.replace('__LISTEN__','8443 ssl' if tls else '8080')
    s=s.replace('__TLS__','ssl_certificate /certs/proxy.crt;\n    ssl_certificate_key /certs/proxy.key;\n    ssl_protocols TLSv1.2 TLSv1.3;' if tls else '')
    s=s.replace('__UPSTREAM_SCHEME__','https' if tls else 'http')
    s=s.replace('__UPSTREAM_TLS__','proxy_ssl_protocols TLSv1.2;\n      proxy_ssl_session_reuse off;\n      proxy_ssl_verify on;\n      proxy_ssl_trusted_certificate /certs/ca.crt;\n      proxy_ssl_name backend.test;\n      proxy_ssl_server_name on;' if tls else '')
    if '__' in s: raise RuntimeError(f'unrendered NGINX template placeholder in {dest}')
    dest.write_text(s)

def make_cauce_config(logs_dir, group, tls, logs):
    suffix='-logs' if logs else ''
    src=HERE/'configs'/f'cauce-{"tls" if tls else "clear"}{suffix}.toml'
    dest=logs_dir.parent/f'{group}-cauce.toml'
    content=src.read_text().replace('/var/log/cauce/access.jsonl',f'/proxylogs/{group}-cauce-access.jsonl').replace('rotate_bytes=16777216','rotate_bytes=67108864')
    dest.write_text(content)
    return dest

def switch_app(dc_run, env, project, stack, service, sampler, tls, logs, group, template, logpath):
    profile=f'sampler-{stack}'
    # The PID/network-sharing sampler must be recreated after its service container changes.
    dc_run(['-p',project,'--profile',profile,'stop',sampler],env=env,check=False,timeout=20)
    dc_run(['-p',project,'--profile',profile,'rm','-f',sampler],env=env,check=False,timeout=20)
    dc_run(['-p',project,'stop',service],env=env,check=False,timeout=20)
    dc_run(['-p',project,'rm','-f',service],env=env,check=False,timeout=20)
    out=env.copy()
    if stack=='cauce': out['CAUCE_CONFIG']=str(make_cauce_config(pathlib.Path(out['LOG_DIR']),group,tls,logs))
    else:
        nconf=pathlib.Path(out['RESULT_DIR'])/f'{group}-nginx-{"tls" if tls else "clear"}.conf'
        render_nginx(template,nconf,tls,logs,logpath)
        out['NGINX_CONFIG']=str(nconf)
    dc_run(['-p',project,'up','-d','--no-build',service],env=out,timeout=60)
    dc_run(['-p',project,'--profile',profile,'up','-d','--no-build',sampler],env=out,timeout=40)
    return out

def backend_stats(env, cid):
    js="fetch('http://127.0.0.1:9000/stats').then(r=>r.text()).then(console.log).catch(e=>{console.error(e);process.exit(2)})"
    p=run(['docker','exec',cid,'node','-e',js],env=env,timeout=8)
    d=json.loads(p.stdout.strip().splitlines()[-1])
    missing=[k for k in REQUIRED_STATS if k not in d]
    if missing: raise RuntimeError(f'backend stats missing fields: {missing}')
    return d

def reset_backend(env,cid,run_id):
    js=f"fetch('http://127.0.0.1:9000/reset?run={run_id}').then(r=>r.text()).then(console.log).catch(e=>{{console.error(e);process.exit(2)}})"
    run(['docker','exec',cid,'node','-e',js],env=env,timeout=8)

def wait_backend(env,cid, predicate, timeout=10):
    deadline=time.monotonic()+timeout; latest=None
    while time.monotonic()<deadline:
        latest=backend_stats(env,cid)
        if predicate(latest): return latest
        time.sleep(.1)
    raise RuntimeError(f'backend recovery barrier timed out; last stats={latest}')

def resource_limits(env, cid, service):
    d=inspect(env,cid); h=d['HostConfig']
    nofile=next((x for x in h.get('Ulimits',[]) if x['Name']=='nofile'),{})
    wanted={'NanoCpus':1_000_000_000,'Memory':256*1024*1024,'PidsLimit':128}
    for key,value in wanted.items():
        if h.get(key)!=value: raise RuntimeError(f'{service} container limit mismatch {key}: {h.get(key)} != {value}')
    if nofile.get('Soft')!=1024 or nofile.get('Hard')!=1024: raise RuntimeError(f'{service} nofile limit mismatch: {nofile}')
    cpus=h.get('CpusetCpus')
    if cpus not in ('0','1'): raise RuntimeError(f'{service} is not pinned to an isolated proxy/backend CPU: CpusetCpus={cpus}')
    return {'image_id':d.get('Image'),'image_ref':d.get('Config',{}).get('Image'),'limits':wanted|{'nofile_soft':nofile.get('Soft'),'nofile_hard':nofile.get('Hard'),'cpuset':cpus},'container_id':cid}

def engine_ready(env):
    p=subprocess.run(['docker','info','--format','{{.ServerVersion}} {{.Architecture}}'],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    if p.returncode:
        print('Docker unavailable; starting Colima as requested.',flush=True)
        subprocess.run(['colima','start'],env=env,check=True,timeout=180)
        p=run(['docker','info','--format','{{.ServerVersion}} {{.Architecture}}'],env=env,timeout=30)
    return p.stdout.strip()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',default='cauce:0.7-final-arm64')
    p.add_argument('--nginx-image',default=os.environ.get('NGINX_IMAGE','nginx:1.30.6@sha256:42c68eab6f7e22611786a9fe1d1c019e275c50185bd54b8f75dc2b9184322703'))
    p.add_argument('--scenarios',nargs='+',choices=SCENARIOS,default=list(SCENARIOS))
    p.add_argument('--repetitions',type=int,default=3)
    p.add_argument('--seconds',type=float,default=8)
    p.add_argument('--warmup',type=float,default=2)
    p.add_argument('--concurrency',type=int,default=32)
    p.add_argument('--rate',type=float,default=None,help='override every scenario rate; default uses SCENARIO_RATES')
    p.add_argument('--seed',type=int,default=1592639710)
    p.add_argument('--results-root',type=pathlib.Path,default=HERE/'results')
    p.add_argument('--skip-build',action='store_true',help='Use already-built local fixture images')
    p.add_argument('--skip-preflight',action='store_true',help='Debug only; final campaigns should retain the capacity gate')
    a=p.parse_args()
    if not 1<=a.repetitions<=20 or not 8<=a.seconds<=3600 or not 0<=a.warmup<=30 or a.concurrency!=32 or (a.rate is not None and not 1<=a.rate<=10000): p.error('invalid benchmark limits; baseline concurrency is fixed at 32')
    a.results_root=a.results_root.resolve()
    a.results_root.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(a.results_root).free < 2*1024**3:
        p.error('at least 2 GiB free on the results filesystem is required before starting')
    if not a.skip_preflight and 'saturation' not in a.scenarios: print('Preflight gate still exercises saturation for each stack/log configuration.')
    env=os.environ.copy()
    default_socket=pathlib.Path.home()/'.colima/default/docker.sock' if sys.platform=='darwin' else pathlib.Path('/var/run/docker.sock')
    env.setdefault('DOCKER_HOST',f'unix://{default_socket}')
    docker_context=engine_ready(env)
    dc=shutil.which('docker-compose')
    if dc: compose=[dc]
    else: compose=['docker','compose']
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6]
    outdir=a.results_root/f'increment7-{stamp}'; outdir.mkdir(parents=True,exist_ok=False)
    certdir=outdir/'certs'; create_certificates(certdir)
    template=HERE/'configs/nginx.conf.template'
    logdir=outdir/'proxy-logs';logdir.mkdir();logdir.chmod(0o777)
    baseenv=env.copy(); baseenv['CAUCE_IMAGE']=a.image; baseenv['NGINX_IMAGE']=a.nginx_image
    baseenv['BACKEND_IMAGE']='cauce-increment7-backend:local';baseenv['CLIENT_IMAGE']='cauce-increment7-client:local'
    baseenv['CERT_DIR']=str(certdir); baseenv['RESULT_DIR']=str(outdir);baseenv['LOG_DIR']=str(logdir)
    baseenv['COMPOSE_PROJECT_NAME']='increment7'
    baseenv['COMPOSE_FILE']=str(HERE/'compose.yaml')
    def dc_run(args, **kwargs): return run(compose+['-f',str(HERE/'compose.yaml')]+list(args),env=kwargs.pop('env',baseenv),**kwargs)
    manifest={'status':'RUNNING','started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'run_id':stamp,
      'docker_context':docker_context,'host_platform':platform.platform(),'python':platform.python_version(),
      'compose_version':run(compose+['version','--short'],env=env).stdout.strip(),
      'image_requested':a.image,'image_id':run(['docker','image','inspect',a.image,'--format','{{.Id}}'],env=env).stdout.strip(),
      'workload':{'scenarios':a.scenarios,'repetitions':a.repetitions,'seconds':a.seconds,'warmup_seconds':a.warmup,
                  'concurrency':a.concurrency,'rate_override':a.rate,'scenario_rates_per_second':SCENARIO_RATES,
                  'scenario_concurrency':{'saturation':64,'default':a.concurrency},'seed':a.seed,'http_version':'HTTP/1.1 on both hops',
                  'logging_variants':['off','on'],'stack_order':'alternates by repetition'},
      'limits_contract':{'proxy_cpu_cores':1,'proxy_memory_bytes':268435456,'pids':128,'nofile':1024,
                         'requests':32,'upstream_physical_connections':32,'idle_upstream_connections':32,
                         'client_connections':128,'backend_max_body_bytes':16777216,
                         'nginx_workers':1,'tokio_worker_threads':1},
      'method_notes':['Cauce and NGINX each run alone against the same fixture on HTTP/1.1.',
       'TLS scenarios verify a temporary local CA at both client-to-proxy and proxy-to-backend legs.',
       'TLS cold creates a new downstream client TLS connection and closes each backend response connection; tls-warm reuses warmed connections.',
       'Normal/delayed/saturation/SSE/slow-reader/WebSocket use cleartext on both hops.',
       'Client connection cap is 128 for Cauce. NGINX has a global active-request limit of 32; idle client sockets are not equivalent.',
       'NGINX JSON access logs store duration in seconds and set pool_wait_ms to null; Cauce provides duration_ms and measured upstream pool_wait_ms.',
       'Both proxies write access logs to a Docker-local named volume; the runner exports the complete directory to the result folder after measurement. Logs include capacity preflight and excluded warmup requests as well as measured requests. Log schema and flush behavior differ.'],
      'host_disk_free_at_start_bytes':shutil.disk_usage(outdir).free,
      'runs':[],'capacity_gates':[],'access_log_checks':[],'errors':[]}
    (outdir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    try:
        run(['docker','pull',a.nginx_image],env=env,timeout=900)
        sampler_image='python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740'
        run(['docker','pull',sampler_image],env=env,timeout=900)
        run(['docker','run','--rm',sampler_image,'python','-c',"from pathlib import Path; p=Path('/tmp/write-probe'); p.write_bytes(b'ok'); p.unlink()"],env=env,timeout=30)
        if not a.skip_build:
            dc_run(['--profile','client','build','backend','client'],timeout=900)
        image_lock={}
        for name,ref in [('cauce',a.image),('nginx',a.nginx_image),('backend','cauce-increment7-backend:local'),('client','cauce-increment7-client:local'),('sampler',sampler_image)]:
            info=json.loads(run(['docker','image','inspect',ref],env=env).stdout)[0]
            image_lock[name]={'reference':ref,'id':info.get('Id'),'architecture':info.get('Architecture'),'os':info.get('Os'),
                              'repo_digests':info.get('RepoDigests',[])}
        verify_image_architectures(docker_context.split()[-1], image_lock)
        (outdir/'images.lock.json').write_text(json.dumps(image_lock,indent=2)+'\n')
        manifest['images']=image_lock
        manifest['method_notes'].append('images.lock.json records the immutable image IDs and repository digests used by this run.')
        host_info=run(['docker','info','--format','{{.NCPU}} {{.MemTotal}}'],env=env).stdout.strip().split()
        manifest['host_resources']={'docker_cpus':int(host_info[0]),'docker_memory_bytes':int(host_info[1]),
          'cpu_pinning':{'proxy':baseenv.get('CPUSET_PROXY','0'),'backend':baseenv.get('CPUSET_BACKEND','1'),
                         'client':baseenv.get('CPUSET_CLIENT','2'),'sampler':baseenv.get('CPUSET_SAMPLER','3')},
          'runtime_versions':{'nginx':run(['docker','run','--rm','--entrypoint','nginx',a.nginx_image,'-V'],env=env).stdout.strip(),
            'node':run(['docker','run','--rm','cauce-increment7-backend:local','node','--version'],env=env,check=False).stdout.strip(),
            'python':run(['docker','run','--rm','--entrypoint','python','cauce-increment7-client:local','--version'],env=env,check=False).stdout.strip()}}
        manifest['cert_fingerprints']={name:run(['openssl','x509','-in',str(certdir/f'{name}.crt'),'-noout','-fingerprint','-sha256'],env=env).stdout.strip() for name in ('ca','proxy','backend')}
        rng=random.Random(a.seed)
        for rep in range(a.repetitions):
            scenario_order=list(a.scenarios); rng.shuffle(scenario_order)
            stack_order=list(STACKS) if rep%2==0 else list(reversed(STACKS))
            for stack in stack_order:
                for logs in ('off','on'):
                    group=f'r{rep+1}-{stack}-logs-{logs}'
                    logpath=f'/proxylogs/{group}-nginx-access.jsonl'
                    envg=baseenv.copy()
                    if stack=='cauce': envg['CAUCE_CONFIG']=str(make_cauce_config(logdir,group,False,logs=='on'))
                    else:
                        nconf=outdir/f'{group}-nginx-clear.conf';render_nginx(template,nconf,False,logs=='on',logpath);envg['NGINX_CONFIG']=str(nconf)
                    service='proxy' if stack=='cauce' else 'nginx';sampler=f'sampler-{stack}'
                    project=f'i7-{stamp[-6:]}-{rep}-{stack}-{logs}'
                    envg['LOG_VOLUME']=project+'-proxylogs'
                    try:
                        dc_run(['-p',project,'--profile',f'sampler-{stack}','up','-d','--no-build','backend',service,sampler],env=envg,timeout=90)
                        app_cid=dc_run(['-p',project,'ps','-q',service],env=envg).stdout.strip()
                        backend_cid=dc_run(['-p',project,'ps','-q','backend'],env=envg).stdout.strip()
                        if not app_cid or not backend_cid: raise RuntimeError('Compose failed to create app/backend containers')
                        limits=resource_limits(env,app_cid,service)
                        if not a.skip_preflight:
                            gate_id=f'{group}-gate';reset_backend(env,backend_cid,gate_id)
                            gate=dc_run(['-p',project,'--profile','client','run','--no-deps','-T','--rm','--entrypoint','python','client','/bench/preflight.py','http://proxy:8080','--count','33'],env=envg,timeout=30)
                            gate_doc=json.loads(gate.stdout.strip().splitlines()[-1])
                            (outdir/f'{gate_id}.json').write_text(json.dumps(gate_doc,indent=2)+'\n')
                            hold=gate_doc['backend_during_hold'];released=gate_doc['backend_after_release']
                            if hold['active']!=32 or hold['peak_active']!=32 or hold['connections']>32 or released['active']!=0 or released['connections']>32 or not gate_doc['statuses'].get('503'):
                                raise RuntimeError(f'capacity gate invariant failed: {gate_doc}')
                            manifest['capacity_gates'].append({'id':gate_id,'stack':stack,'logs':logs,'url':'http://proxy:8080','evidence':gate_doc})
                            wait_backend(env,backend_cid,lambda s:s['active']==0 and s['connections']<=32,timeout=8)
                        current_tls=False
                        for scenario in scenario_order:
                            if shutil.disk_usage(outdir).free < 1024**3:
                                raise RuntimeError('less than 1 GiB free on results filesystem; aborting before next measured load')
                            tls=scenario.startswith('tls-')
                            if tls!=current_tls:
                                envg=switch_app(dc_run,envg,project,stack,service,sampler,tls,logs=='on',group,template,logpath)
                                app_cid=dc_run(['-p',project,'ps','-q',service],env=envg).stdout.strip();current_tls=tls
                            if not app_cid: raise RuntimeError(f'{service} not running before scenario {scenario}')
                            run_id=f'{group}-{scenario}-n{len(manifest["runs"])+1:03d}'
                            run_dir=outdir/'raw';run_dir.mkdir(exist_ok=True)
                            raw=run_dir/f'{run_id}.jsonl';summary=run_dir/f'{run_id}.summary.json';clog=run_dir/f'{run_id}.client.log'
                            done=outdir/f'{run_id}.done';exitf=outdir/f'{run_id}.exit';readyf=outdir/f'{run_id}.ready';warmf=outdir/f'{run_id}.warm';resumef=outdir/f'{run_id}.resume';measurement=outdir/f'{run_id}.measurement';release=outdir/f'{run_id}.measurement-release';marker=outdir/'.sample'
                            for f in (done,exitf,readyf,warmf,resumef,measurement,release,marker): f.unlink(missing_ok=True)
                            wait_backend(env,backend_cid,lambda s:s['active']==0,timeout=10)
                            rate=a.rate or SCENARIO_RATES[scenario];concurrency=64 if scenario=='saturation' else a.concurrency
                            baseurl=f'{"https" if tls else "http"}://proxy:{8443 if tls else 8080}/'
                            cmdload=['--url',baseurl,'--scenario',scenario,'--seconds',str(a.seconds),'--warmup',str(a.warmup),
                                     '--concurrency',str(concurrency),'--rate',str(rate),'--seed',str(a.seed+len(manifest['runs'])),
                                     '--output',f'/results/raw/{raw.name}','--summary',f'/results/raw/{summary.name}',
                                     '--ready-file',f'/results/{readyf.name}','--warmup-done-file',f'/results/{warmf.name}',
                                     '--resume-file',f'/results/{resumef.name}','--measurement-done-file',f'/results/{measurement.name}',
                                     '--measurement-release-file',f'/results/{release.name}']
                            if scenario in ('normal','delayed','saturation','tls-warm','tls-cold'): cmdload += ['--expect-bytes','1024']
                            if tls: cmdload += ['--ca-file','/certs/ca.crt']
                            shell=' '.join(shlex.quote(x) for x in ['python','/bench/load.py']+cmdload)
                            oneoff=f'i7-{stamp[-6:]}-{len(manifest["runs"]):03d}'
                            cmd=['-p',project,'--profile','client','run','--no-deps','-T','-d','--name',oneoff,'--entrypoint','/bin/sh','client','-c',f'{shell}; rc=$?; echo $rc > /results/{exitf.name}; touch /results/{done.name}; sleep 3600; exit $rc']
                            started=dc_run(cmd,env=envg,timeout=30);client_id=started.stdout.strip().splitlines()[-1]
                            deadline=time.monotonic()+max(60,a.warmup+45)
                            while time.monotonic()<deadline and not warmf.exists():
                                if not inspect(env,client_id)['State'].get('Running'): break
                                time.sleep(.05)
                            if not warmf.exists():
                                log=run(['docker','logs',client_id],env=env,check=False).stdout or ''
                                raise RuntimeError(f'generator did not reach warmup barrier: {log[-3000:]}')
                            reset_backend(env,backend_cid,run_id)
                            limits_run=resource_limits(env,app_cid,service)
                            baseline={name:stats(env,cid) for name,cid in [('proxy',app_cid),('backend',backend_cid),('client',client_id)]}
                            marker.write_text(run_id+':baseline\n')
                            baseline_deadline=time.monotonic()+8
                            while time.monotonic()<baseline_deadline:
                                observed=False
                                if psfile:=outdir/'proc-samples.jsonl':
                                    if psfile.exists():
                                        for line in psfile.read_text().splitlines()[-20:]:
                                            try:q=json.loads(line)
                                            except json.JSONDecodeError:continue
                                            if q.get('run')==run_id+':baseline' and q.get('processes'):
                                                observed=True;break
                                if observed:break
                                time.sleep(.05)
                            if not observed: raise RuntimeError(f'process sampler did not capture a proxy baseline for {run_id}')
                            marker.write_text(run_id+'\n');t_start=time.monotonic();resumef.write_text('go\n')
                            samples=[];next_sample=0
                            deadline=t_start+a.seconds+90
                            while time.monotonic()<deadline and not measurement.exists():
                                now=time.monotonic()
                                if now>=next_sample:
                                    row={'elapsed_s':now-t_start}
                                    for name,cid in [('proxy',app_cid),('backend',backend_cid),('client',client_id)]: row[name]=stats(env,cid)
                                    samples.append(row);next_sample=now+.8
                                time.sleep(.05)
                            if not measurement.exists(): raise RuntimeError(f'generator did not signal measured-load completion: {run_id}')
                            # Take the endpoint sample before raw-result serialization and Docker post-processing.
                            final={name:stats(env,cid) for name,cid in [('proxy',app_cid),('backend',backend_cid),('client',client_id)]}
                            t_end=time.monotonic();marker.unlink(missing_ok=True);release.write_text('stats captured\n')
                            post_deadline=time.monotonic()+30
                            while time.monotonic()<post_deadline and not done.exists(): time.sleep(.05)
                            if not done.exists(): raise RuntimeError(f'generator did not finish output serialization: {run_id}')
                            if not exitf.exists() or exitf.read_text().strip()!='0':
                                log=run(['docker','logs',client_id],env=env,check=False).stdout or '';raise RuntimeError(f'load process failed: {log[-3000:]}')
                            proc=inspect(env,client_id);client_log=run(['docker','logs',client_id],env=env,check=False).stdout or '';clog.write_text(client_log)
                            if not summary.exists(): raise RuntimeError(f'generator did not write summary {summary}')
                            load_summary=json.loads(summary.read_text())
                            backend_end=wait_backend(env,backend_cid,lambda s:s['active']==0,timeout=15)
                            rows=[json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
                            psfile=outdir/'proc-samples.jsonl';diag_proc=[];diag_baseline=[]
                            if psfile.exists():
                                for line in psfile.read_text().splitlines():
                                    try:q=json.loads(line)
                                    except json.JSONDecodeError:continue
                                    if q.get('run')==run_id:diag_proc.append(q)
                                    elif q.get('run')==run_id+':baseline':diag_baseline.append(q)
                            cgroup_peak={k:max((x[k].get('memory_usage_bytes') or 0 for x in samples),default=0) for k in ('proxy','backend','client')}
                            diagnostic={'run_id':run_id,'status':'captured_before_assertions','baseline':baseline,'final':final,
                                'cpu_summary':{name:cpu_delta(baseline[name],final[name]) for name in baseline},
                                'docker_samples':samples,'proc_baseline':diag_baseline,'proc_samples':diag_proc,
                                'cgroup_memory_peak_bytes':cgroup_peak,'backend_final':backend_end,
                                'generator_summary':load_summary,'raw_rows':rows}
                            (run_dir/f'{run_id}.diagnostic.json').write_text(json.dumps(diagnostic,indent=2)+'\n')
                            required=('offered','successful_responses','errors','http_errors','rejections','drops','connection_new','connection_reused','percentiles_ms','total_goodput_per_second')
                            miss=[k for k in required if k not in load_summary]
                            if miss: raise RuntimeError(f'load summary missing fields: {miss}')
                            if backend_end['active']!=0 or backend_end['peak_active']>32 or backend_end['connections']>32 or backend_end['peak_connections']>32:
                                raise RuntimeError(f'backend cap/recovery assertion failed: {backend_end}')
                            if scenario=='saturation' and (backend_end['peak_active']!=32 or load_summary['http_errors']<1):
                                raise RuntimeError(f'saturation failed to prove 32-active cap and excess rejection: {backend_end} {load_summary}')
                            if not load_summary['successful_responses']:
                                raise RuntimeError(f'workload returned zero successful responses: {load_summary}')
                            if scenario!='saturation' and (load_summary['errors'] or load_summary['timeouts'] or load_summary['drops'] or load_summary['rejections'] or load_summary['http_errors'] or load_summary['successful_responses']!=load_summary['offered']):
                                raise RuntimeError(f'non-saturation workload had generator errors or queue loss: {load_summary}')
                            if scenario=='saturation' and (load_summary['errors'] or load_summary['timeouts'] or load_summary['drops'] or load_summary['rejections']):
                                raise RuntimeError(f'saturation had transport failures, timeout, or generator queue loss: {load_summary}')
                            if load_summary.get('warmup_errors',0) or load_summary.get('warmup_rejections',0): raise RuntimeError(f'warmup failed or was rejected: {load_summary}')
                            if scenario=='saturation' and (load_summary['successful_responses']<1 or any(r.get('outcome')=='http_error' and r.get('status')!=503 for r in rows)):
                                raise RuntimeError(f'saturation produced no successes or a non-503 HTTP error: {load_summary}')
                            if scenario=='tls-cold' and (backend_end['tls_full_handshakes']<1 or backend_end['tls_resumed_handshakes']!=0):
                                raise RuntimeError(f'tls-cold backend did not show full non-resumed TLS handshakes: {backend_end}')
                            if scenario=='tls-warm' and backend_end['reused_requests']<1:
                                raise RuntimeError(f'tls-warm did not reuse upstream connections: {backend_end}')
                            if scenario=='sse' and any(r.get('outcome')!='success' or r.get('sse_events')!=5 for r in rows): raise RuntimeError('SSE validation did not produce five complete events for every offered request')
                            if scenario=='ws' and any(r.get('outcome')!='success' or not r.get('ws_echo_ok') for r in rows): raise RuntimeError('WebSocket echo validation failed')
                            if scenario=='slow-reader' and any(r.get('outcome')!='success' or r.get('bytes')!=1048576 for r in rows): raise RuntimeError('slow-reader payload validation failed')
                            if cgroup_peak['proxy']>256*1024*1024: raise RuntimeError(f'proxy exceeded memory limit: {cgroup_peak}')
                            procs=diag_proc;proc_baseline=diag_baseline
                            if not procs or not proc_baseline or len(samples)<2: raise RuntimeError(f'missing resource sampling for {run_id}')
                            expected_processes=1 if stack=='cauce' else 2
                            if not any(len(x['processes'])>=expected_processes for x in procs): raise RuntimeError(f'process sampler missed proxy processes: {procs[:1]}')
                            cpu_summary={name:cpu_delta(baseline[name],final[name]) for name in baseline}
                            jitter=load_summary['percentiles_ms'].get('scheduler_lag',{}).get('all',{}).get('p99')
                            generator_limited=bool(load_summary['drops'] or load_summary['rejections'] or (jitter is not None and jitter>50))
                            resource_doc={'run_id':run_id,'measurement_elapsed_s':t_end-t_start,'baseline':baseline,'final':final,
                                'cpu_summary':cpu_summary,'docker_samples':samples,'proc_baseline':proc_baseline,
                                'proc_samples':procs,'cgroup_memory_peak_bytes':cgroup_peak,
                                'scheduler_lag_p99_ms':jitter,'generator_comparison_limited':generator_limited}
                            respath=run_dir/f'{run_id}.resources.json';respath.write_text(json.dumps(resource_doc,indent=2)+'\n')
                            state_rows={}
                            for name,cid in [('proxy',app_cid),('backend',backend_cid),('client',client_id)]:
                                insp=inspect(env,cid);state_rows[name]={'oom_killed':insp['State'].get('OOMKilled'),'restart_count':insp.get('RestartCount',0),'running':insp['State'].get('Running')}
                                if insp['State'].get('OOMKilled') or insp.get('RestartCount',0): raise RuntimeError(f'OOM/restart: {name} {state_rows[name]}')
                            rss=max((sum(x['rss_bytes'] for x in sample['processes']) for sample in procs),default=None)
                            threads=max((sum(x['threads'] for x in sample['processes']) for sample in procs),default=None)
                            fds=max((sum(x['fds'] for x in sample['processes'] if x['fds'] is not None) for sample in procs),default=None)
                            fds_missing=any(any(x['fds'] is None for x in sample['processes']) for sample in procs)
                            row={'id':run_id,'stack':stack,'logs':logs,'scenario':scenario,'repetition':rep+1,
                              'url':baseurl,'rate_per_second':rate,'concurrency':concurrency,'proxy':limits_run,
                              'generator':load_summary,'backend':backend_end,'resource_file':str(respath.relative_to(outdir)),
                              'container_states':state_rows,'proxy_process_rss_peak_bytes':rss,'proxy_process_threads_peak':threads,
                              'proxy_process_fds_peak':fds,'proxy_process_fds_missing':fds_missing,'proxy_process_count':expected_processes,
                              'access_log_file':str(logdir/f'{group}-cauce-access.jsonl') if stack=='cauce' and logs=='on' else (str(logdir/f'{group}-nginx-access.jsonl') if logs=='on' else None)}
                            manifest['runs'].append(row);(outdir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
                            print(f"[{len(manifest['runs'])}] {stack} logs={logs} {scenario} offered={load_summary['offered']} goodput_with_drain={load_summary['total_goodput_per_second']:.2f}/s p95={load_summary['percentiles_ms']['scheduled_latency']['success']['p95']}",flush=True)
                            run(['docker','rm','-f',client_id],env=env,check=False)
                        if logs=='on':
                            dc_run(['-p',project,'stop',service,sampler],env=envg,timeout=30)
                            copied=run(['docker','cp',f'{app_cid}:/proxylogs/.',str(logdir)],env=env,check=False)
                            if copied.returncode: raise RuntimeError(f'failed to export proxy logs from local Docker volume: {copied.stdout or ""}')
                            files=list(logdir.glob(f'{group}-*-access.jsonl*'))
                            expected=sum(r['generator']['offered']+r['generator']['warmup_requests_excluded'] for r in manifest['runs'] if r['id'].startswith(group+'-'))+(0 if a.skip_preflight else 33)
                            count=0
                            for file in files:
                                with file.open() as stream:
                                    for line in stream:
                                        if not line.strip(): continue
                                        json.loads(line);count+=1
                            if not files or count!=expected:
                                raise RuntimeError(f'access log verification failed for {group}: files={files} valid_rows={count} expected={expected}')
                            manifest['access_log_checks'].append({'group':group,'expected':expected,'valid_rows':count,'files':[str(f.relative_to(outdir)) for f in files]})
                    finally:
                        dc_run(['-p',project,'--profile','client','--profile',f'sampler-{stack}','down','-v','--remove-orphans'],env=envg,check=False,timeout=60)
        manifest['status']='COMPLETE';manifest['finished_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
    except KeyboardInterrupt:
        manifest['status']='ABORTED';manifest['finished_utc']=dt.datetime.now(dt.timezone.utc).isoformat();manifest['errors'].append({'error':'KeyboardInterrupt','message':'campaign interrupted; partial runs are not a complete comparison'})
        raise
    except Exception as e:
        manifest['status']='FAIL';manifest['finished_utc']=dt.datetime.now(dt.timezone.utc).isoformat();manifest['errors'].append({'error':type(e).__name__,'message':str(e)})
        raise
    finally:
        (outdir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        print(f"Results: {outdir}",flush=True)
    return 0

if __name__=='__main__':
    try: raise SystemExit(main())
    except KeyboardInterrupt: raise SystemExit(130)
