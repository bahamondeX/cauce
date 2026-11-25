#!/usr/bin/env python3
"""Run the amd64 image under Colima's registered emulation, without publishing it."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
root=Path(__file__).resolve().parents[1]
name='cauce-amd64-test'
image=os.environ.get('CAUCE_AMD64_IMAGE','cauce-local-proxy:amd64')
check=subprocess.check_output(['docker','run','--rm','--platform','linux/amd64',image,'check','/etc/cauce/cauce.toml'],text=True)
try:
    subprocess.run(['docker','run','-d','--platform','linux/amd64','--name',name,'--network','cauce-local_default','-p','127.0.0.1:18081:8080',image],check=True,stdout=subprocess.DEVNULL)
    for _ in range(100):
        try:
            req=urllib.request.Request('http://127.0.0.1:18081/',headers={'Host':'localhost:8080'})
            with urllib.request.urlopen(req,timeout=.5) as r:
                r.read()
            break
        except OSError:
            time.sleep(.05)
    else:
        raise RuntimeError('amd64 proxy did not become ready')
    responses=[]
    for _ in range(6):
        req=urllib.request.Request('http://127.0.0.1:18081/',headers={'Host':'localhost:8080'})
        with urllib.request.urlopen(req,timeout=3) as r:
            responses.append({'status':r.status,'body':json.load(r)})
    assert {r['body']['server'] for r in responses}=={'node','uvicorn'}
    uid=subprocess.check_output(['docker','exec',name,'id'],text=True)
    arch=subprocess.check_output(['docker','image','inspect',image,'--format','{{.Architecture}}'],text=True).strip()
    assert arch=='amd64' and 'uid=10001' in uid
    report={'architecture':arch,'execution':'Colima ARM64 with amd64 emulation, not native amd64 hardware','config_check':check.strip(),'uid':uid.strip(),'responses':responses}
    (root/'docs'/os.environ.get('CAUCE_AMD64_REPORT','amd64-smoke.json')).write_text(json.dumps(report,indent=2)+'\n')
    print('PASS: amd64 config, non-root user and six proxied HTTP responses')
finally:
    subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
