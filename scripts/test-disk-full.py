#!/usr/bin/env python3
"""Linux ENOSPC via /dev/full; does not fill or damage a filesystem."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

root=Path(__file__).resolve().parents[1]
docker=['docker']
name='cauce-diskfull-test'
config=(root/'examples/docker.toml').read_text().replace('/var/log/cauce/access.jsonl','/dev/full').replace('upstreams = ["python", "node"]','upstreams = ["node"]')
path=root/'work/diskfull.toml'
path.parent.mkdir(exist_ok=True)
path.write_text(config)
try:
    subprocess.run(docker+['run','-d','--name',name,'--network','cauce-local_default','-p','127.0.0.1:18080:8080',
                          '-e','CAUCE_ADMIN_TOKEN=diskfull-test-token-at-least-32-bytes','-v',f'{path}:/etc/cauce.toml:ro',
                          'cauce-local-proxy','/etc/cauce.toml'],check=True,stdout=subprocess.DEVNULL)
    for _ in range(100):
        try:
            request=urllib.request.Request('http://127.0.0.1:18080/',headers={'Host':'localhost:8080'})
            with urllib.request.urlopen(request,timeout=.5) as response:
                response.read()
            break
        except OSError:
            time.sleep(.05)
    else:
        print(subprocess.check_output(docker+['logs',name],text=True,stderr=subprocess.STDOUT))
        raise RuntimeError('container readiness timeout')
    statuses=[]
    for _ in range(20):
        request=urllib.request.Request('http://127.0.0.1:18080/',headers={'Host':'localhost:8080'})
        with urllib.request.urlopen(request,timeout=3) as response:
            statuses.append(response.status)
            response.read()
    time.sleep(.2)
    code="import urllib.request; r=urllib.request.Request('http://127.0.0.1:9090/metrics',headers={'Authorization':'Bearer diskfull-test-token-at-least-32-bytes'}); print(urllib.request.urlopen(r).read().decode())"
    metrics=subprocess.check_output(docker+['run','--rm','--network',f'container:{name}','--entrypoint','python','cauce-local-python','-c',code],text=True)
    errors=int(next(s.split()[-1] for s in metrics.splitlines() if s.startswith('cauce_log_io_errors_total ')))
    assert errors>0 and statuses==[200]*20
    report={'mechanism':'Linux /dev/full ENOSPC','responses':statuses,'log_io_errors':errors,'metrics':metrics}
    (root/'docs/disk-full.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'PASS: {len(statuses)} HTTP 200 responses despite {errors} log IO errors')
finally:
    subprocess.run(docker+['rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
