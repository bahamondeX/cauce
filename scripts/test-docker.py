#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import time
import urllib.request
root=Path(__file__).resolve().parents[1]
results=[]
for _ in range(6):
    with urllib.request.urlopen('http://localhost:8080/',timeout=3) as r:
        results.append({'status':r.status,'body':json.load(r)})
assert {r['body']['server'] for r in results}=={'uvicorn','node'}
start=time.monotonic()
with urllib.request.urlopen('http://localhost:8080/sse',timeout=3) as r:
    first=r.readline().decode().strip()
    first_ms=(time.monotonic()-start)*1000
    rest=r.read().decode()
assert first=='data: 0' and 'data: 4' in rest
uid=subprocess.check_output(['docker','exec','cauce-local-proxy-1','id'],text=True).strip()
assert 'uid=10001' in uid
subprocess.run(['docker','kill','--signal','HUP','cauce-local-proxy-1'],check=True,stdout=subprocess.DEVNULL)
time.sleep(.1)
with urllib.request.urlopen('http://localhost:8080/',timeout=3) as r:
    assert r.status==200
    r.read()
image=json.loads(subprocess.check_output(['docker','image','inspect','cauce-local-proxy','--format','{"id":"{{.Id}}","architecture":"{{.Architecture}}","os":"{{.Os}}"}'],text=True))
report={'http':results,'sse':{'first_event':first,'first_event_ms':first_ms,'remainder':rest},'uid':uid,'image':image,'reload_signal':'HUP followed by HTTP 200'}
(root/'docs/docker-smoke.json').write_text(json.dumps(report,indent=2)+'\n')
print('PASS: Compose balancing, SSE, non-root and SIGHUP')
