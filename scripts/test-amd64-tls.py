#!/usr/bin/env python3
"""Small amd64 TLS/WSS smoke under Colima emulation; requires TLS fixture profile."""
import asyncio,json,ssl,subprocess,uuid
from pathlib import Path
import websockets
ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'work/tls'
NAME='cauce-amd64-tls'
async def main():
    config=WORK/f'amd64-{uuid.uuid4().hex}.toml'
    config.write_text((ROOT/'examples/tls/cauce.toml').read_text().replace('localhost:8443','localhost:18445'))
    try:
        subprocess.run(['docker','run','-d','--platform','linux/amd64','--name',NAME,'--network','cauce-tls_default','-p','127.0.0.1:18445:8080','--read-only','--cap-drop','ALL','--tmpfs','/var/log/cauce:uid=10001,gid=10001,mode=0700','-v',f'{WORK}:/run/cauce-tls:ro','cauce-tls-proxy:amd64','/run/cauce-tls/'+config.name],check=True,stdout=subprocess.DEVNULL)
        ctx=ssl.create_default_context(cafile=str(WORK/'ca.crt'))
        for i in range(80):
            try:
                async with websockets.connect('wss://localhost:18445/ws',ssl=ctx,proxy=None,open_timeout=1) as ws:
                    await ws.send('amd64 verified TLS echo');assert await ws.recv()=='amd64 verified TLS echo'
                    await ws.close();assert ws.close_code==1000
                break
            except (OSError,TimeoutError):await asyncio.sleep(.1)
        else:raise AssertionError('amd64 TLS proxy not ready')
        result={'architecture':'amd64','execution':'Colima ARM64 emulation; not native x86 hardware','wss_echo':True,'normal_close':1000,'image':subprocess.check_output(['docker','image','inspect','cauce-tls-proxy:amd64','--format','{{.Id}}'],text=True).strip()}
        (ROOT/'docs/increment2-amd64-tls.json').write_text(json.dumps(result,indent=2)+'\n')
        print('PASS: amd64 emulated, verified TLS on both sides, WSS echo and normal close')
    finally:subprocess.run(['docker','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
asyncio.run(main())
