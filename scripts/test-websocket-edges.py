#!/usr/bin/env python3
"""Run after test-tls-docker.py --keep: real Docker tunnel lifecycle and quota tests."""
import asyncio
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import time
import uuid
import websockets

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT/'work/tls'
NAME = 'cauce-ws-edges'
REPORT = ROOT/'docs/increment2-websocket-edges.json'
results = []

def docker(*args, **kw):
    return subprocess.run(['docker', *args], check=True, text=True, capture_output=True, **kw)

def passed(name, **detail):
    results.append({'case':name,'passed':True,**detail})
    print('PASS:',name,flush=True)

async def main():
    base=(ROOT/'examples/tls/cauce.toml').read_text().replace('localhost:8443','localhost:18443')
    for key,value in [('connections',1),('idle_connections',0),('stream_idle_ms',450),('websocket_max_ms',1800),('shutdown_ms',500),('acquire_ms',150)]:
        base=re.sub(rf'(?m)^{key} = \d+$',f'{key} = {value}',base)
    ctx=ssl.create_default_context(cafile=str(WORK/'ca.crt'))
    async def start(text, secure=True):
        config=WORK/f"edges-{uuid.uuid4().hex}.toml"
        with config.open("w") as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        await asyncio.sleep(.5)
        subprocess.run(['docker','rm','-f',NAME],capture_output=True)
        docker('run','-d','--name',NAME,'--network','cauce-tls_default','-p','127.0.0.1:18443:8080',
               '--read-only','--cap-drop','ALL','--tmpfs','/var/log/cauce:uid=10001,gid=10001,mode=0700',
               '-v',f'{WORK}:/run/cauce-tls:ro',os.environ.get('CAUCE_TEST_IMAGE','cauce-tls-proxy'),'/run/cauce-tls/'+config.name)
        uri=('wss' if secure else 'ws')+'://localhost:18443/ws'
        kwargs={'ssl':ctx} if secure else {}
        for _ in range(60):
            try:
                async with websockets.connect(uri,proxy=None,ping_interval=None,open_timeout=.5,**kwargs): pass
                return uri,kwargs
            except (OSError,TimeoutError,websockets.exceptions.WebSocketException) as error:
                if _ == 30: print(repr(error),flush=True)
                await asyncio.sleep(.1)
        raise AssertionError('edge proxy did not become ready')
    async def connect(uri,kwargs):
        return await websockets.connect(uri,proxy=None,ping_interval=None,open_timeout=2,**kwargs)
    try:
        uri,kwargs=await start(base)
        ws=await connect(uri,kwargs)
        await ws.send('first'); assert await ws.recv()=='first'
        try:
            await connect(uri,kwargs)
            raise AssertionError('upgraded pool permit released early')
        except websockets.exceptions.InvalidStatus as e:
            assert e.response.status_code==503,e.response
        await ws.send('still alive'); assert await ws.recv()=='still alive'
        passed('upstream capacity retained after 101; second upgrade returns 503')
        ws.transport.abort()
        await asyncio.sleep(.15)
        ws=await connect(uri,kwargs)
        await ws.send('replacement');assert await ws.recv()=='replacement'
        await ws.close(code=1000,reason='normal');assert ws.close_code==1000
        passed('abrupt disconnect releases capacity; next tunnel closes normally with 1000')
        ws=await connect(uri,kwargs)
        started=time.monotonic()
        await asyncio.wait_for(ws.wait_closed(),2)
        assert time.monotonic()-started < 1.7
        passed('idle tunnel closes within configured timeout',elapsed_ms=round((time.monotonic()-started)*1000,2))
        ws=await connect(uri,kwargs)
        started=time.monotonic(); exchanges=0
        try:
            while True:
                await ws.send('activity'); assert await ws.recv()=='activity';exchanges+=1
                await asyncio.sleep(.1)
        except websockets.exceptions.ConnectionClosed:
            elapsed=time.monotonic()-started
            assert elapsed>1 and elapsed<3
            assert exchanges>=8
        passed('bidirectional activity resets idle; absolute websocket lifetime still closes',exchanges=exchanges)
        # Capture logs before replacing the container; opaque transport EOF/cancel semantics documented.
        logs=docker('exec',NAME,'cat','/var/log/cauce/access.jsonl').stdout
        (ROOT/'docs/increment2-websocket-edges.jsonl').write_text(logs)
        events=[json.loads(x) for x in logs.splitlines()]
        assert any(e['reason']=='websocket_idle_timeout' for e in events)
        assert any(e['reason']=='websocket_max_age' for e in events)
        assert any(e['status']==503 for e in events)
        passed('tunnel outcomes recorded as versioned JSONL')
        # Long-lived tunnel must be cut by shutdown_ms, not left detached until max age.
        text=base.replace('stream_idle_ms = 450','stream_idle_ms = 30000').replace('websocket_max_ms = 1800','websocket_max_ms = 300000')
        uri,kwargs=await start(text)
        ws=await connect(uri,kwargs)
        start_at=time.monotonic();docker('kill','--signal','TERM',NAME)
        await asyncio.wait_for(ws.wait_closed(),2)
        await asyncio.to_thread(docker,'wait',NAME)
        elapsed=time.monotonic()-start_at
        assert elapsed<2
        passed('SIGTERM drains then aborts active tunnel within shared shutdown deadline',elapsed_ms=round(elapsed*1000,2))
        # ws input can tunnel to a verified WSS upstream; no incoming TLS is inferred from Upgrade.
        plain=re.sub(r'(?ms)^\[tls\]\n.*?(?=^\[limits\])','',base)
        uri,kwargs=await start(plain,secure=False)
        ws=await connect(uri,kwargs)
        await ws.send('plain ws to secure backend');assert await ws.recv()=='plain ws to secure backend'
        await ws.close();assert ws.close_code==1000
        passed('plain ws listener to verified TLS WebSocket backend')
        # Exercise the reverse and fully cleartext combinations against a plain WS fixture.
        no_upstream_tls=re.sub(r'(?m)^tls = \{.*\}\n','',base).replace('websocket:8765','websocket:8764')
        uri,kwargs=await start(no_upstream_tls)
        ws=await connect(uri,kwargs);await ws.send('secure input plain upstream');assert await ws.recv()=='secure input plain upstream';await ws.close()
        passed('verified wss listener to plain WebSocket backend')
        plain_both=re.sub(r'(?ms)^\[tls\]\n.*?(?=^\[limits\])','',no_upstream_tls)
        uri,kwargs=await start(plain_both,secure=False)
        ws=await connect(uri,kwargs);await ws.send('plain both');assert await ws.recv()=='plain both';await ws.close()
        passed('plain ws listener to plain WebSocket backend')
        REPORT.write_text(json.dumps({'platform':'Linux Docker ARM64','cases':results,'passed':len(results)},indent=2)+'\n')
    finally:
        (ROOT/'docs/edge-last-container.log').write_text(subprocess.run(['docker','logs',NAME],capture_output=True,text=True).stderr)
        subprocess.run(['docker','rm','-f',NAME],capture_output=True)

if __name__=='__main__': asyncio.run(main())
