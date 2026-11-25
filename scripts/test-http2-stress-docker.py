#!/usr/bin/env python3
"""Run reproducible Docker HTTP/2 stress and seeded wire-fuzz campaigns."""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TLS=ROOT/'work/http2-stress/tls'
RESULTS=ROOT/'work/http2-stress/results'
TOKEN='increment6-http2-stress-probe-token'

def run(args, **kwargs):
    kwargs.setdefault('check',True); return subprocess.run(args,**kwargs)

def issue(name, san, ca=False):
    key,csr,crt=(TLS/f'{name}.{x}' for x in ('key','csr','crt'))
    run(['openssl','req','-new','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(csr),'-subj',f'/CN={san}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    ext=("basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n" if ca else f"subjectAltName=DNS:{san},DNS:localhost\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n")
    cmd=['openssl','x509','-req','-in',str(csr),'-CA',str(crt if ca else TLS/'ca.crt'),'-CAkey',str(key if ca else TLS/'ca.key'),'-CAcreateserial','-out',str(crt if ca else crt),'-days','3','-sha256','-extfile','/dev/stdin']
    if ca:
        # Self-sign the test CA.
        cmd=['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(crt),'-days','3','-subj',f'/CN={san}','-addext','keyUsage=critical,keyCertSign,cRLSign']
        run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:
        run(cmd,input=ext,text=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    key.chmod(0o644)

def image_info(image):
    return subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}} {{.Architecture}}'],text=True).strip().split()

def cstate(container):
    raw=subprocess.check_output(['docker','inspect',container],text=True)
    item=json.loads(raw)[0]; state=item['State']
    return {'oom_killed':state.get('OOMKilled',False),'restarts':item.get('RestartCount',0),'running':state.get('Running',False)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--image',default='cauce:0.6-final-arm64')
    ap.add_argument('--platform',choices=('arm64','amd64'),default='arm64')
    ap.add_argument('--project',default=f'cauce-h2stress-{os.getpid()}')
    ap.add_argument('--waves',type=int,default=int(os.getenv('CAUCE_STRESS_WAVES','3')))
    ap.add_argument('--requests',type=int,default=int(os.getenv('CAUCE_STRESS_REQUESTS','32')))
    ap.add_argument('--seed',type=int,default=int(os.getenv('CAUCE_STRESS_SEED','1592639710')))
    ap.add_argument('--fuzz-cases',type=int,default=int(os.getenv('CAUCE_STRESS_FUZZ_CASES','24')))
    ap.add_argument('--keep',action='store_true')
    ap.add_argument('--report',type=Path)
    args=ap.parse_args()
    if not 1<=args.waves<=100 or not 16<=args.requests<=256 or not 1<=args.fuzz_cases<=512:
        ap.error('bounded campaign requires waves 1..100, requests 16..256, fuzz cases 1..512')
    TLS.mkdir(parents=True,exist_ok=True); RESULTS.mkdir(parents=True,exist_ok=True)
    os.environ.setdefault('DOCKER_CONFIG',str(ROOT/'work/docker'))
    os.environ.setdefault('DOCKER_HOST',f"unix://{Path.home()}/.colima/default/docker.sock")
    os.environ.setdefault('BUILDX_CONFIG',str(ROOT/'work/http2-stress/buildx'))
    if subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        run(['colima','start']); run(['docker','info'],stdout=subprocess.DEVNULL)
    (RESULTS/'http2-stress-clear.json').unlink(missing_ok=True);(RESULTS/'http2-stress-tls.json').unlink(missing_ok=True)
    issue('ca','Cauce Increment6 HTTP2 Stress CA',ca=True)
    issue('proxy','localhost'); issue('backend','backend.test')
    ident,arch=image_info(args.image)
    if arch!=args.platform: raise RuntimeError(f'{args.image} architecture is {arch}; expected {args.platform}')
    os.environ['CAUCE_STRESS_PROXY_CLEAR_IMAGE']=args.image;os.environ['CAUCE_STRESS_PROXY_TLS_IMAGE']=args.image
    os.environ['CAUCE_STRESS_ADMIN_TOKEN']=TOKEN;os.environ['CAUCE_STRESS_WAVES']=str(args.waves)
    os.environ['CAUCE_STRESS_REQUESTS']=str(args.requests);os.environ['CAUCE_STRESS_SEED']=str(args.seed)
    os.environ['CAUCE_STRESS_FUZZ_CASES']=str(args.fuzz_cases)
    compose=['docker','compose','-p',args.project,'-f',str(ROOT/'compose.http2-stress.yaml')]
    override=ROOT/'work/http2-stress/compose.platform.yaml'
    override.write_text('services:\n  proxy-clear:\n    platform: linux/'+arch+'\n  proxy-tls:\n    platform: linux/'+arch+'\n')
    compose += ['-f',str(override)]
    report={'result':'NOT_RUN','image':args.image,'image_id':ident,'architecture':arch,'project':args.project,'seed':args.seed,'waves':args.waves,'requests_per_wave':args.requests,'fuzz_cases_per_mode':args.fuzz_cases,'modes':{},'docker_samples':[]}
    out=args.report or ROOT/f'docs/increment6-http2-stress-{arch}.json'
    try:
        run([*compose,'build','backend','client-clear','client-tls'])
        run([*compose,'up','--no-build','-d','proxy-clear','proxy-tls','backend'])
        # Readiness gate uses real H2 client calls; retry only until bounded deadline.
        deadline=time.monotonic()+30
        while True:
            ready=subprocess.run([*compose,'exec','-T','backend','node','-e',"require('http').get('http://127.0.0.1:7003/stats',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if ready.returncode==0:break
            if time.monotonic()>deadline:raise TimeoutError('backend readiness timeout')
            time.sleep(.2)
        for mode in ('clear','tls'):
            service='proxy-'+mode; client='client-'+mode
            import tempfile
            client_output=tempfile.TemporaryFile(mode='w+t')
            cid=subprocess.check_output([*compose,'ps','-q',service],text=True).strip()
            try: memory_before=int(subprocess.check_output(['docker','exec',cid,'cat','/sys/fs/cgroup/memory.current'],text=True,stderr=subprocess.DEVNULL).strip())
            except Exception as e: raise RuntimeError(f'cannot read proxy cgroup memory.current: {e}')
            p=subprocess.Popen([*compose,'run','--rm',client],stdout=client_output,stderr=subprocess.STDOUT,text=True)
            resource_samples=[]
            while p.poll() is None:
                state=cstate(cid)
                assert state['running'] and not state['oom_killed'] and state['restarts']==0, state
                raw_stats=subprocess.check_output(['docker','stats','--no-stream','--format','{{json .}}',cid],text=True)
                try: docker_stats=json.loads(raw_stats)
                except json.JSONDecodeError:docker_stats={'raw':raw_stats.strip()}
                used=None
                try:
                    memory_current=subprocess.check_output(['docker','exec',cid,'cat','/sys/fs/cgroup/memory.current'],text=True,stderr=subprocess.DEVNULL).strip()
                    used=int(memory_current)
                except Exception:memory_current=None
                sample={'mode':mode,'at':time.time(),'proxy':state,'docker_stats':docker_stats,'memory_current_bytes':used}
                report['docker_samples'].append(sample);resource_samples.append(sample)
                time.sleep(.2)
            client_output.seek(0);stdout=client_output.read();client_output.close()
            if p.returncode:raise RuntimeError(f'{client} failed: {stdout[-8000:]}')
            result_path=RESULTS/f'http2-stress-{mode}.json'
            if not result_path.exists():raise RuntimeError(f'{client} did not persist report; output={stdout[-4000:]}')
            final_state=cstate(cid)
            assert final_state['running'] and not final_state['oom_killed'] and final_state['restarts']==0,final_state
            payload=json.loads(result_path.read_text())
            if payload.get('result')!='PASS':raise AssertionError(payload.get('error',payload))
            baseline=payload.get('resource_samples',[{}])[0].get('process',{})
            readings=[s['memory_current_bytes'] for s in resource_samples if isinstance(s.get('memory_current_bytes'),int)]
            if readings:
                assert max(readings)<=256*1024*1024,(max(readings),resource_samples[-1])
                # Cgroup accounting includes allocator/kernel cache; allow a 64 MiB post-run envelope.
                settled=int(subprocess.check_output(['docker','exec',cid,'cat','/sys/fs/cgroup/memory.current'],text=True).strip())
                report.setdefault('memory_checks',{})[mode]={'baseline_cgroup_bytes':memory_before,'baseline_process':baseline,'peak_cgroup_bytes':max([memory_before]+readings),'post_quiescence_cgroup_bytes':settled,'container_limit_bytes':256*1024*1024,'tolerance_note':'cgroup memory includes runtime and kernel accounting; bounded, not exact RSS leak proof'}
                assert settled<=memory_before+64*1024*1024,(memory_before,settled)
            else: raise RuntimeError('no cgroup memory.current readings captured during stress')
            report['modes'][mode]=payload
        report['result']='PASS'
    except Exception as e:
        report['result']='FAIL';report['error']=repr(e)
        raise
    finally:
        try:
            logs=subprocess.run([*compose,'logs','--no-color'],capture_output=True,text=True,timeout=15)
            (ROOT/'docs'/f'increment6-http2-stress-{arch}-compose.log').write_text(logs.stdout+logs.stderr)
        except Exception as e:print(f'log capture failed: {e}',file=sys.stderr)
        report['final_container_state']={}
        for svc in ('proxy-clear','proxy-tls'):
            cid=subprocess.run([*compose,'ps','-q',svc],capture_output=True,text=True).stdout.strip()
            if cid:
                try:report['final_container_state'][svc]=cstate(cid)
                except Exception:pass
        if not args.keep:subprocess.run([*compose,'down','--remove-orphans'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'result':report['result'],'report':str(out),'modes':list(report['modes'])},indent=2))

if __name__=='__main__':main()
