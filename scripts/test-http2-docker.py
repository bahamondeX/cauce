#!/usr/bin/env python3
"""Build isolated Docker fixtures and run real HTTP/2 + gRPC integration clients."""
import argparse, json, os, re, select, subprocess, sys, time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
os.environ.setdefault('BUILDX_CONFIG',str(ROOT/'work/http2/buildx'))
TLS=ROOT/'work/http2/tls'

def run(args, **kw):
    kw.setdefault('check',True)
    return subprocess.run(args, **kw)

def issue(name, san):
    key=TLS/f'{name}.key'; csr=TLS/f'{name}.csr'; crt=TLS/f'{name}.crt'
    run(['openssl','req','-new','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(csr),'-subj',f'/CN={san}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    alt='DNS:localhost,DNS:proxy-tls' if name=='proxy' else f'DNS:{san}'
    ext=f'subjectAltName={alt}\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n'
    run(['openssl','x509','-req','-in',str(csr),'-CA',str(TLS/'ca.crt'),'-CAkey',str(TLS/'ca.key'),'-CAcreateserial','-out',str(crt),'-days','3','-sha256','-extfile','/dev/stdin'],input=ext,text=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--keep',action='store_true');ap.add_argument('--report-prefix',default='increment4-http2',help='prefix under docs/ for evidence files');ap.add_argument('--project',default=f'cauce-http2-{os.getpid()}');ap.add_argument('--image',help='reuse a prebuilt proxy image for both proxy services');ap.add_argument('--platform',choices=('amd64','arm64'),help='target only proxy services; fixtures remain native');args=ap.parse_args()
    TLS.mkdir(parents=True,exist_ok=True)
    if subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        run(['colima','start'])
        run(['docker','info'],stdout=subprocess.DEVNULL)
    run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(TLS/'ca.key'),'-out',str(TLS/'ca.crt'),'-days','3','-subj','/CN=Cauce HTTP2 Integration CA','-addext','keyUsage=critical,keyCertSign,cRLSign'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    issue('proxy','localhost');issue('backend','backend.test');issue('grpc','backend.test')
    for name in ('proxy','backend','grpc'): (TLS/f'{name}.key').chmod(0o644)
    os.environ['CAUCE_HTTP2_PROXY_CLEAR_IMAGE']=args.image or f'{args.project}-proxy-clear'
    os.environ['CAUCE_HTTP2_PROXY_TLS_IMAGE']=args.image or f'{args.project}-proxy-tls'
    compose=['docker','compose','-p',args.project,'-f',str(ROOT/'compose.http2.yaml')]
    if args.platform:
        override=ROOT/'work/http2/compose.platform.yaml'
        override.write_text(f'services:\n  proxy-clear:\n    platform: linux/{args.platform}\n  proxy-tls:\n    platform: linux/{args.platform}\n')
        compose += ['-f',str(override)]
    suffix=args.platform or 'native'
    results_path=ROOT/f'docs/{args.report_prefix}-{suffix}.json'; log_path=ROOT/f'docs/{args.report_prefix}-{suffix}-compose.log'
    access_path=ROOT/f'docs/{args.report_prefix}-{suffix}-access.jsonl'
    result={'project':args.project,'cases':[],'image':args.image or 'native build','platform':args.platform or 'native'}
    exported=False
    try:
        if args.image:
            run([*compose,'build','node','grpc','h2early','client','lifecycle'])
            run([*compose,'up','--no-build','-d'])
        else:
            run([*compose,'up','--build','-d'])
            run([*compose,'build','client','lifecycle'])
        client=run([*compose,'run','--rm','client'],capture_output=True,text=True,timeout=240,check=False)
        if client.returncode: raise RuntimeError(f'HTTP/2 Docker client failed: {client.stdout}\n{client.stderr}')
        output=client.stdout
        # The client emits one final machine-readable object.
        parsed=None
        for match in reversed(list(re.finditer(r'\{\s*"result"\s*:',output))):
            try:
                candidate,_=json.JSONDecoder().raw_decode(output[match.start():])
                if isinstance(candidate,dict) and candidate.get('result')=='PASS':
                    parsed=candidate;break
            except json.JSONDecodeError:
                continue
        if parsed is None:raise AssertionError(f'client did not emit a parseable PASS record:\n{output}')
        if parsed.get('result')!='PASS': raise AssertionError(output)
        result.update(parsed);result['architecture']=subprocess.check_output(['docker','image','inspect',os.environ['CAUCE_HTTP2_PROXY_CLEAR_IMAGE'],'--format','{{.Architecture}}'],text=True).strip()
        result['result']='PASS'
        print('PASS: '+'; '.join(parsed['checks']))
        records=[]
        for service in ('proxy-clear','proxy-tls'):
            captured=subprocess.run([*compose,'exec','-T',service,'cat','/var/log/cauce/access.jsonl'],capture_output=True,text=True,timeout=20)
            if captured.returncode==0:records.extend(line for line in captured.stdout.splitlines() if line.strip())
        if records:access_path.write_text('\n'.join(records)+'\n')
        result['access_log_records']=len(records)
        result['image_ids']={service:subprocess.check_output(['docker','image','inspect',os.environ[f'CAUCE_HTTP2_{service.upper().replace("-","_")}_IMAGE'],'--format','{{.Id}}'],text=True).strip() for service in ('proxy-clear','proxy-tls')}
        exported=True
        for signal_name,mode in [('HUP','hup'),('TERM','term')]:
            proc=subprocess.Popen([*compose,'run','--no-deps','--rm','lifecycle','node','lifecycle.mjs',mode],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            ready=False
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                available,_,_=select.select([proc.stdout],[],[],max(0,deadline-time.monotonic()))
                if not available:break
                line=proc.stdout.readline()
                if not line:break
                if line.strip()=='ACTIVE':ready=True;break
            if not ready:
                stdout,stderr=proc.communicate(timeout=10)
                raise RuntimeError(f'{signal_name} lifecycle client failed to start: {stdout} {stderr}')
            run(['docker','kill','--signal='+signal_name,f'{args.project}-proxy-clear-1'],stdout=subprocess.DEVNULL)
            stdout,stderr=proc.communicate(timeout=15)
            if proc.returncode:raise RuntimeError(f'{signal_name} lifecycle client failed: {stdout} {stderr}')
            lifecycle=json.loads(stdout.splitlines()[-1])
            if signal_name=='TERM' and not lifecycle.get('goaway'):raise AssertionError('TERM did not produce GOAWAY')
            if signal_name=='HUP':
                logs=subprocess.check_output(['docker','logs',f'{args.project}-proxy-clear-1'],stderr=subprocess.STDOUT,text=True)
                assert '"event":"reloaded"' in logs,'HUP did not reload configuration'
            result['cases'].append(f'{signal_name} during active request: response completed; GOAWAY={lifecycle.get("goaway")}')
    except Exception as e:
        result.update({'result':'FAIL','error':str(e)})
        raise
    finally:
        try:
            logs=subprocess.run([*compose,'logs','--no-color'],capture_output=True,text=True,timeout=30)
            log_path.write_text(logs.stdout+logs.stderr)
            if not exported:
                records=[]
                for service in ('proxy-clear','proxy-tls'):
                    captured=subprocess.run([*compose,'exec','-T',service,'cat','/var/log/cauce/access.jsonl'],capture_output=True,text=True,timeout=20)
                    if captured.returncode==0:records.extend(line for line in captured.stdout.splitlines() if line.strip())
                if records:access_path.write_text('\n'.join(records)+'\n')
                result['access_log_records']=len(records)
        except Exception as e:
            log_path.write_text(f'compose logs or access capture unavailable: {e}\n')
        if not args.keep:
            subprocess.run([*compose,'down','--remove-orphans'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        results_path.write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__': main()
