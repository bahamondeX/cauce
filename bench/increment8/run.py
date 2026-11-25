#!/usr/bin/env python3
"""Bounded open-loop HTTP/2 and real gRPC comparison on native Docker CPUs."""
from __future__ import annotations
import argparse, datetime as dt, hashlib, importlib.util, json, os, pathlib, platform, random
import shlex, shutil, subprocess, sys, time, uuid
HERE=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('bench7',HERE.parent/'increment7/run.py')
b7=importlib.util.module_from_spec(spec);spec.loader.exec_module(b7)
SCENARIOS={
 'h2-clear':dict(mode='http2',tls=False,rate=400,delay=0),
 'h2-tls':dict(mode='http2',tls=True,rate=400,delay=0),
 'h2-delayed':dict(mode='http2',tls=False,rate=64,delay=100),
 'h2-saturation':dict(mode='http2',tls=False,rate=128,delay=500),
 'grpc-unary-clear':dict(mode='grpc',method='Unary',tls=False,rate=200,delay=0),
 'grpc-unary-tls':dict(mode='grpc',method='Unary',tls=True,rate=200,delay=0),
 'grpc-client-stream':dict(mode='grpc',method='ClientStream',tls=False,rate=80,delay=0),
 'grpc-server-stream':dict(mode='grpc',method='ServerStream',tls=False,rate=80,delay=0),
 'grpc-bidi':dict(mode='grpc',method='Bidi',tls=False,rate=80,delay=0),
}
NGINX='nginx:1.30.6@sha256:42c68eab6f7e22611786a9fe1d1c019e275c50185bd54b8f75dc2b9184322703'
NODE='node:22.14.0-alpine3.21@sha256:9bef0ef1e268f60627da9ba7d7605e8831d5b56ad07487d24d1aa386336d1944'
SAMPLER='python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740'
def save(path,data):path.write_text(json.dumps(data,indent=2)+'\n')
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def backend_get(env,cid,mode,path):
    url='http://127.0.0.1:'+('9001' if mode=='grpc' else '9000')+path
    if mode=='grpc':command=['python','-c',f'import urllib.request;print(urllib.request.urlopen({url!r}).read().decode())']
    else:command=['node','-e',f'fetch({json.dumps(url)}).then(r=>r.text()).then(console.log).catch(e=>{{console.error(e);process.exit(1)}})']
    return json.loads(b7.run(['docker','exec',cid]+command,env=env,timeout=15).stdout)
def waitfile(path,timeout,env,client):
    end=time.monotonic()+timeout
    while not path.exists() and time.monotonic()<end:
        time.sleep(.05)
    if not path.exists():
        raise RuntimeError(f'timed out waiting {path.name}: '+b7.run(['docker','logs',client],env=env,check=False).stdout[-4000:])
def check_limits(env,cid,cpuset,pids=128):
    d=b7.inspect(env,cid);h=d['HostConfig']
    for k,v in {'NanoCpus':1000000000,'Memory':268435456,'PidsLimit':pids,'CpusetCpus':cpuset}.items():
        if h.get(k)!=v:raise RuntimeError(f'{cid}: {k}={h.get(k)} expected {v}')
    nf=next(x for x in h['Ulimits'] if x['Name']=='nofile')
    if nf['Soft']!=1024 or nf['Hard']!=1024:raise RuntimeError('nofile mismatch')
    return {k:h[k] for k in ('NanoCpus','Memory','PidsLimit','CpusetCpus','Ulimits')}
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--image',default='cauce:0.7-final-arm64')
    ap.add_argument('--nginx-image',default=NGINX)
    ap.add_argument('--scenarios',nargs='+',choices=SCENARIOS,default=list(SCENARIOS))
    ap.add_argument('--repetitions',type=int,default=3)
    ap.add_argument('--seconds',type=float,default=8)
    ap.add_argument('--warmup',type=float,default=2)
    ap.add_argument('--seed',type=int,default=1592639711)
    ap.add_argument('--skip-build',action='store_true')
    ap.add_argument('--results-root',type=pathlib.Path,default=HERE/'results')
    a=ap.parse_args()
    if not 1<=a.repetitions<=20 or not 8<=a.seconds<=3600 or not 1<=a.warmup<=30:ap.error('repetitions 1..20, seconds 8..3600, warmup 1..30')
    a.results_root=a.results_root.resolve();a.results_root.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(a.results_root).free<3*1024**3:ap.error('requires 3 GiB free before starting')
    env=os.environ.copy();env.setdefault('DOCKER_HOST','unix://'+str(pathlib.Path.home()/'.colima/default/docker.sock' if sys.platform=='darwin' else pathlib.Path('/var/run/docker.sock')))
    engine=b7.engine_ready(env);compose=[shutil.which('docker-compose')] if shutil.which('docker-compose') else ['docker','compose']
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6]
    out=a.results_root/f'increment8-{stamp}';out.mkdir();(out/'raw').mkdir()
    certs=out/'certs';b7.create_certificates(certs)
    base=env|{'CAUCE_IMAGE':a.image,'NGINX_IMAGE':a.nginx_image,'CERT_DIR':str(certs),'RESULT_DIR':str(out),'CLIENT_IMAGE':NODE}
    def dc(args,envg=base,**kw):return b7.run(compose+['-f',str(HERE/'compose.yaml')]+list(args),env=envg,**kw)
    manifest={'status':'RUNNING','started_utc':now(),'id':stamp,'host_platform':platform.platform(),'engine':engine,
      'compose_version':b7.run(compose+['version','--short'],env=env).stdout.strip(),'python':platform.python_version(),
      'workload':{'scenarios':a.scenarios,'repetitions':a.repetitions,'seconds':a.seconds,'warmup_seconds':a.warmup,'seed':a.seed,'definitions':SCENARIOS,'logging':'off','load':'bounded open-loop; 2 persistent H2 sessions x 16 streams (saturation 4 x 16)'},
      'limits_contract':{'cpu_cores':1,'memory_bytes':268435456,'pids':128,'nofile':1024,'frontend_streams_per_connection':16,'global_active_requests':32,'upstream_active_connection_budget':32,'upstream_idle_cache_budget':32,'cauce_upstream_streams_per_connection':16,'worker_threads':1},
      'notes':['HTTP/2 on both hops, cleartext prior knowledge or TLS with verified temporary CA and ALPN h2.',
        'gRPC uses grpcio generic raw-byte Echo handlers, validates framing, payload and final grpc-status; no protobuf serialization cost.',
        'Equal admission/resource budgets do not imply identical pools: NGINX upstream max_conns limits active connections and keepalive limits idle cache separately.',
        'NGINX limit_conn counts active H2 requests after complete headers; Cauce additionally caps frontend sockets at 128.',
        'CPU 100% means one core. Engine interval includes short sampler margins around the load; serialization is outside it.',
        'RSS is sum of processes, not PSS; NGINX shared pages may be counted twice. Cgroup memory includes cache.',
        'Streaming RPCs use three messages per RPC; throughput unit is completed RPC/s. Sessions are warmed; this is not a cold TLS benchmark.'],
      'free_at_start_bytes':shutil.disk_usage(out).free,'runs':[],'gates':[],'errors':[]}
    save(out/'manifest.json',manifest)
    try:
        # Use pinned input images; all must match the Engine architecture, never QEMU measurements.
        for ref in (NODE,a.nginx_image,SAMPLER):
            b7.run(['docker','pull',ref],env=env,timeout=300)
        if not a.skip_build:dc(['build','backend-h2','backend-grpc'],timeout=900)
        refs={'cauce':a.image,'nginx':a.nginx_image,'client':NODE,'sampler':SAMPLER,'backend-h2':base.get('BACKEND_H2_IMAGE','cauce-increment8-h2:local'),'backend-grpc':base.get('BACKEND_GRPC_IMAGE','cauce-increment8-grpc:local')}
        arch=engine.split()[-1];arch={'aarch64':'arm64','x86_64':'amd64'}.get(arch,arch)
        lock={}
        for name,ref in refs.items():
            d=json.loads(b7.run(['docker','image','inspect',ref],env=env).stdout)[0]
            if d['Architecture']!=arch:raise RuntimeError(f'non-native image {ref}: {d["Architecture"]}, Engine {arch}')
            lock[name]={'reference':ref,'id':d['Id'],'architecture':d['Architecture'],'digests':d.get('RepoDigests',[])}
        manifest['images']=lock;save(out/'images.lock.json',lock)
        host=b7.run(['docker','info','--format','{{.NCPU}} {{.MemTotal}}'],env=env).stdout.split()
        if int(host[0])<4:raise RuntimeError('requires four Docker CPUs for isolated cpusets')
        manifest['engine_resources']={'cpus':int(host[0]),'memory_bytes':int(host[1])}
        manifest['source_sha256']={str(p.relative_to(HERE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in HERE.rglob('*') if p.is_file() and not any(x in p.parts for x in ('results','certs','__pycache__'))}
        manifest['shared_source_sha256']={str(p.relative_to(HERE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (HERE.parent/'increment7/run.py',HERE.parent/'increment7/proc_sampler.py')}
        gates=set()
        for rep in range(a.repetitions):
            order=list(a.scenarios);random.Random(a.seed+rep).shuffle(order)
            for scenario in order:
                cfg=SCENARIOS[scenario];mode=cfg['mode'];tls=cfg['tls'];backend='backend-grpc' if mode=='grpc' else 'backend-h2'
                for stack in (('cauce','nginx') if rep%2==0 else ('nginx','cauce')):
                    if shutil.disk_usage(out).free<2*1024**3:raise RuntimeError('less than 2 GiB free; refusing next load')
                    rid=f'r{rep+1}-{scenario}-{stack}';project=f'i8-{stamp[-6:]}-{len(manifest["runs"]):03d}'
                    variant=('grpc' if mode=='grpc' else 'h2')+('-tls' if tls else '-clear')
                    envg=base|{'CAUCE_CONFIG':str(HERE/'configs'/f'cauce-{variant}.toml'),'NGINX_CONFIG':str(HERE/'configs'/f'nginx-{variant}.conf')}
                    service='proxy-'+stack;sampler='sampler-'+stack;client=None
                    def start_stack():
                        dc(['-p',project,'up','-d','--wait','--no-build',backend],envg,timeout=90)
                        dc(['-p',project,'--profile',sampler,'up','-d','--no-build',service,sampler],envg,timeout=90)
                        proxy_id=dc(['-p',project,'ps','-q',service],envg).stdout.strip()
                        backend_id=dc(['-p',project,'ps','-q',backend],envg).stdout.strip()
                        if not proxy_id or not backend_id:raise RuntimeError('missing containers')
                        return proxy_id,backend_id
                    cleanup=['-p',project,'--profile','client','--profile',sampler,'--profile','http2','--profile','grpc','down','-v','--remove-orphans']
                    try:
                        cid,bid=start_stack()
                        url=f'{"https" if tls else "http"}://proxy:{8443 if tls else 8080}'
                        # This also proves real H2 upstream before accepting any throughput measurement.
                        if (stack,variant) not in gates:
                            g=dc(['-p',project,'--profile','client','run','--no-deps','--rm','-T','--entrypoint','node','client','/bench/gate_h2.mjs',url,mode,f'http://{backend}:{9001 if mode=="grpc" else 9000}'],envg,timeout=35)
                            gate=json.loads(g.stdout.strip().splitlines()[-1]);save(out/f'gate-{stack}-{variant}.json',gate)
                            manifest['gates'].append({'stack':stack,'variant':variant,'file':f'gate-{stack}-{variant}.json','status':gate['status']});gates.add((stack,variant))
                            # Admission gates fill pools and allocators. Discard that state so every
                            # repetition starts fresh and warms only with its measured workload.
                            dc(cleanup,envg,timeout=60)
                            cid,bid=start_stack()
                        limits=check_limits(env,cid,'0');check_limits(env,bid,'1',64)
                        initial_backend=backend_get(env,bid,mode,'/stats')
                        if any(initial_backend[k] for k in ('active','requests','completed','connections','peak_connections')):
                            raise RuntimeError('backend not pristine before workload warmup')
                        prefix=out/'raw'/rid;barrier=out/rid
                        cmd=['node','/bench/load_h2.mjs','--url',url,'--mode',mode,'--rate',str(cfg['rate']),'--seconds',str(a.seconds),'--warmup',str(a.warmup),'--connections',str(4 if scenario=='h2-saturation' else 2),'--streams','16','--timeout','5000','--out',f'/results/raw/{rid}','--barrier',f'/results/{rid}']
                        if scenario=='h2-saturation':cmd+=['--allow-503','true']
                        if mode=='grpc':cmd+=['--method',cfg['method']]
                        else:cmd+=['--path',f'/bytes?size=1024&delay_ms={cfg["delay"]}','--size','1024']
                        if tls:cmd+=['--ca','/certs/ca.crt']
                        shell=shlex.join(cmd)+f'; rc=$?; echo $rc > /results/{rid}.exit; sleep 3600; exit $rc'
                        client=dc(['-p',project,'--profile','client','run','--no-deps','-T','-d','--name',project+'-load','--entrypoint','/bin/sh','client','-c',shell],envg,timeout=30).stdout.strip().splitlines()[-1]
                        check_limits(env,client,'2')
                        waitfile(barrier/'warmup.done',a.warmup+45,env,client)
                        backend_get(env,bid,mode,'/reset');containers={'proxy':cid,'backend':bid,'client':client}
                        marker=out/'.sample';marker.write_text(rid+':baseline')
                        time.sleep(.5)
                        baseline={k:b7.stats(env,v) for k,v in containers.items()}
                        marker.write_text(rid)
                        start=time.monotonic();(barrier/'resume').write_text('go')
                        samples=[];deadline=start+a.seconds+30;next_sample=start
                        while not (barrier/'measurement.done').exists() and time.monotonic()<deadline:
                            if time.monotonic()>=next_sample:
                                samples.append({'elapsed_s':time.monotonic()-start,**{k:b7.stats(env,v) for k,v in containers.items()}})
                                next_sample=time.monotonic()+.7
                            time.sleep(.03)
                        waitfile(barrier/'measurement.done',1,env,client)
                        final={k:b7.stats(env,v) for k,v in containers.items()};marker.unlink(missing_ok=True)
                        (barrier/'release').write_text('sampled')
                        waitfile(pathlib.Path(str(barrier)+'.exit'),25,env,client)
                        clientlog=b7.run(['docker','logs',client],env=env,check=False).stdout;(out/'raw'/f'{rid}.client.log').write_text(clientlog)
                        if pathlib.Path(str(barrier)+'.exit').read_text().strip()!='0':raise RuntimeError('load client failed: '+clientlog[-3000:])
                        summary=json.loads(pathlib.Path(str(prefix)+'.summary.json').read_text());backend_end=backend_get(env,bid,mode,'/stats')
                        procs=[json.loads(x) for x in (out/'proc-samples.jsonl').read_text().splitlines()]
                        procs=[x for x in procs if x['run'] in (rid,rid+':baseline')]
                        resources={'baseline':baseline,'final':final,'samples':samples,'cpu':{k:b7.cpu_delta(baseline[k],final[k]) for k in containers},'processes':procs,
                          'cgroup_peak_bytes':{k:max(x[k]['memory_usage_bytes'] for x in samples+[final]) for k in containers}}
                        save(out/'raw'/f'{rid}.resources.json',resources)
                        # Preserve diagnostics before quality assertions.
                        row={'id':rid,'scenario':scenario,'stack':stack,'repetition':rep+1,'limits':limits,'initial_backend':initial_backend,'generator':summary,'backend':backend_end,'resources':f'raw/{rid}.resources.json','raw':f'raw/{rid}.jsonl'}
                        save(out/'raw'/f'{rid}.diagnostic.json',row)
                        if backend_end['active'] or backend_end['peak_active']>32:raise RuntimeError('backend active request budget/release failed')
                        if backend_end.get('peak_connections',0)>32:raise RuntimeError('observed physical connection budget exceeded')
                        sampler_id=dc(['-p',project,'ps','-q',sampler],envg).stdout.strip()
                        for k,v in (containers|{'sampler':sampler_id}).items():
                            d=b7.inspect(env,v)
                            if d['State'].get('OOMKilled') or d.get('RestartCount') or not d['State']['Running']:raise RuntimeError(f'{k} OOM/restarted/exited')
                        if not any(x['processes'] for x in procs if x['run']==rid) or len(samples)<2:raise RuntimeError('missing resource samples')
                        from summarize import audit_run
                        row['audit']=audit_run(out,row,saturation=scenario=='h2-saturation')
                        manifest['runs'].append(row);save(out/'manifest.json',manifest)
                        print(f'[{len(manifest["runs"])}] {rid} PASS',flush=True)
                    finally:
                        logs=dc(['-p',project,'logs','--no-color',service,backend],envg,check=False,timeout=20)
                        (out/'raw'/f'{rid}.services.log').write_text(logs.stdout or '')
                        dc(cleanup,envg,check=False,timeout=60)
        manifest['status']='COMPLETE';manifest['finished_utc']=now()
    except BaseException as e:
        manifest['status']='ABORTED' if isinstance(e,KeyboardInterrupt) else 'FAIL';manifest['finished_utc']=now();manifest['errors'].append({'type':type(e).__name__,'message':str(e)});raise
    finally:
        save(out/'manifest.json',manifest);print('Results: '+str(out),flush=True)
    return out
if __name__=='__main__':main()
