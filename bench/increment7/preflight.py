#!/usr/bin/env python3
"""Concurrency gate: prove 32 simultaneous backend requests and reject the 33rd."""
import argparse, asyncio, json, ssl, time

p=argparse.ArgumentParser()
p.add_argument('url'); p.add_argument('--ca-file'); p.add_argument('--count',type=int,default=33)
a=p.parse_args()
if a.count != 33: p.error('the quota fixture requires exactly 33 clients')
from urllib.parse import urlsplit
u=urlsplit(a.url); secure=u.scheme=='https'; port=u.port or (443 if secure else 80)
ctx=ssl.create_default_context(cafile=a.ca_file) if secure else None
async def one(i, gate):
    await gate.wait()
    try:
        r,w=await asyncio.wait_for(asyncio.open_connection(u.hostname,port,ssl=ctx,server_hostname=u.hostname if secure else None),5)
        w.write(b'GET /hold?ms=1800 HTTP/1.1\r\nHost: '+u.netloc.encode()+b'\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
        await w.drain(); head=await asyncio.wait_for(r.readuntil(b'\r\n\r\n'),5)
        status=int(head.split(b' ',2)[1]); await r.read(); w.close(); await w.wait_closed()
        return {'client':i,'status':status}
    except Exception as e: return {'client':i,'error':type(e).__name__,'detail':str(e)}
async def backend_snapshot():
    r,w=await asyncio.open_connection('backend',9000)
    w.write(b'GET /stats HTTP/1.1\r\nHost: backend\r\nConnection: close\r\n\r\n'); await w.drain()
    head=await r.readuntil(b'\r\n\r\n'); headers={}
    for line in head.split(b'\r\n')[1:]:
        if b':' in line:
            k,v=line.split(b':',1);headers[k.strip().lower()]=v.strip().lower()
    if headers.get(b'transfer-encoding')==b'chunked':
        chunks=[]
        while True:
            size=int((await r.readuntil(b'\r\n')).split(b';',1)[0],16)
            if size==0:
                await r.readuntil(b'\r\n');break
            chunks.append(await r.readexactly(size));await r.readexactly(2)
        body=b''.join(chunks)
    elif b'content-length' in headers:
        body=await r.readexactly(int(headers[b'content-length']))
    else: body=await r.read()
    w.close(); await w.wait_closed(); return json.loads(body)
async def main():
    gate=asyncio.Event(); tasks=[asyncio.create_task(one(i,gate)) for i in range(a.count)]
    start=time.monotonic(); gate.set()
    snapshot=None; deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        await asyncio.sleep(.05); snapshot=await backend_snapshot()
        if snapshot['active']>=32: break
    rows=await asyncio.gather(*tasks); final=await backend_snapshot()
    statuses=[x.get('status') for x in rows]
    out={'url':a.url,'offered':a.count,'statuses':{str(s):statuses.count(s) for s in sorted(set(statuses),key=lambda x:(x is None,x or 0))},'errors':[x for x in rows if 'error' in x],'duration_s':time.monotonic()-start,'backend_during_hold':snapshot,'backend_after_release':final,'rows':rows}
    print(json.dumps(out));
    if out['errors'] or statuses.count(503)<1 or statuses.count(200)<32 or snapshot['active'] != 32 or snapshot['peak_active'] != 32 or snapshot['connections'] > 32 or final['active'] != 0 or final['connections'] > 32: raise SystemExit('capacity gate failed: 32 active requests, 33rd=503, connection cap, or recovery assertion failed')
asyncio.run(main())
