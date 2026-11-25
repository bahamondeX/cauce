#!/usr/bin/env python3
"""Build and exercise the TLS + WebSocket profile using real Docker sockets."""
import os
import json
import argparse
import http.client
import hashlib
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TLS = ROOT / 'work/tls'
TLS.mkdir(parents=True, exist_ok=True)


def run(*args, **kw):
    return subprocess.run(args, check=True, **kw)


def cert(cn, san, stem):
    key, csr, crt = (TLS / f'{stem}.{x}' for x in ('key', 'csr', 'crt'))
    run('openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
        '-out', str(csr), '-subj', f'/CN={cn}')
    run('openssl', 'x509', '-req', '-in', str(csr), '-CA', str(TLS / 'ca.crt'),
        '-CAkey', str(TLS / 'ca.key'), '-CAcreateserial', '-out', str(crt), '-days', '3',
        '-sha256', '-extfile', '/dev/stdin', input=f'subjectAltName=DNS:{san}\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n', text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--keep', action='store_true', help='leave profile containers running for inspection')
    keep = parser.parse_args().keep
    if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run("colima", "start")
    # Disposable local CA and leaf certificates. Private keys stay under ignored work/.
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(TLS/'ca.key'),
        '-out', str(TLS/'ca.crt'), '-days', '3', '-subj', '/CN=Cauce test CA', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    cert('localhost', 'localhost', 'proxy')
    cert('backend.test', 'backend.test', 'backend')
    cert('untrusted.test', 'untrusted.test', 'untrusted')
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(TLS/'other-ca.key'),
        '-out', str(TLS/'other-ca.crt'), '-days', '3', '-subj', '/CN=Untrusted test CA', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    os.chmod(TLS/'ca.key', 0o600)
    os.chmod(TLS/'proxy.key', 0o644)
    os.chmod(TLS/'backend.key', 0o644)

    dc = ['docker', 'compose', '-p', 'cauce-tls', '-f', str(ROOT/'compose.tls.yaml')]
    config_path = ROOT/'examples/tls/cauce.toml'
    original_config = config_path.read_text()
    results = {'cases': []}

    def write_config(text):
        # Replace atomically so the bind-mounted file is never observed truncated.
        with tempfile.NamedTemporaryFile('w', dir=config_path.parent, delete=False) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            temporary = Path(f.name)
        temporary.chmod(0o644)
        temporary.replace(config_path)

    def restart_with(text):
        # Existing bind directory observes atomic replacement; synchronize the VM view
        # before requesting application reload, and require an actual generation change.
        write_config(text)
        expected=hashlib.sha256(text.encode()).hexdigest()
        for _ in range(100):
            seen=subprocess.check_output(['docker','exec','cauce-tls-proxy-1','sha256sum','/etc/cauce/tls/cauce.toml'],text=True).split()[0]
            if seen==expected: break
            time.sleep(.05)
        else: raise AssertionError('shared configuration did not become visible')
        previous=subprocess.run(['docker','logs','cauce-tls-proxy-1'],capture_output=True,text=True).stderr.count('"event":"reloaded"')
        run('docker','kill','--signal','HUP','cauce-tls-proxy-1',stdout=subprocess.DEVNULL)
        for _ in range(100):
            logs=subprocess.run(['docker','logs','cauce-tls-proxy-1'],capture_output=True,text=True).stderr
            if logs.count('"event":"reloaded"')>previous:return
            time.sleep(.05)
        raise AssertionError('proxy rejected configuration: '+logs)
    run(*dc, 'up', '--build', '--force-recreate', '-d', 'proxy', 'websocket')
    try:
        import ssl
        import asyncio
        import websockets

        async def exercise():
            ctx = ssl.create_default_context(cafile=str(TLS/'ca.crt'))
            uri = 'wss://localhost:8443/ws'
            ready = False
            for _ in range(40):
                try:
                    async with websockets.connect(uri, ssl=ctx, open_timeout=1) as probe:
                        await probe.close()
                    ready = True
                    break
                except Exception:
                    await asyncio.sleep(.5)
            assert ready, 'TLS proxy and backend did not become ready'
            results['cases'].append('trusted inbound certificate and TLS upstream')
            async with websockets.connect(uri, ssl=ctx, max_size=8*1024*1024, ping_interval=5) as ws:
                payload = b'x' * (4 * 1024 * 1024)
                await ws.send([payload[:1024*1024], payload[1024*1024:2*1024*1024],
                                payload[2*1024*1024:3*1024*1024], payload[3*1024*1024:]])
                assert await ws.recv() == payload
                pong = await ws.ping()
                await pong
            results['cases'].append('4 MiB fragmented binary echo, ping/pong and normal close')
            try:
                async with websockets.connect('wss://127.0.0.1:8443/ws', ssl=ctx):
                    raise AssertionError('wrong inbound hostname was accepted')
            except ssl.SSLCertVerificationError:
                pass
            results['cases'].append('inbound hostname mismatch rejected')

            async def one(i):
                async with websockets.connect(uri, ssl=ctx, max_size=1024*1024) as ws:
                    msg = f'client-{i}'
                    await ws.send(msg)
                    assert await ws.recv() == msg
            await asyncio.gather(*(one(i) for i in range(8)))
            results['cases'].append('eight concurrent bidirectional sessions')

            # Two downstream HTTPS requests should reuse one upstream TLS socket.
            h = http.client.HTTPSConnection('localhost', 8443, context=ctx, timeout=4)
            upstream_ids = []
            request_ids = []
            for _ in range(2):
                h.request('GET', '/http', headers={'Host': 'localhost:8443'})
                response = h.getresponse()
                assert response.status == 200, response.status
                request_ids.append(response.getheader('x-request-id'))
                upstream_ids.append(json.loads(response.read())['connection_id'])
            h.close()
            assert upstream_ids[0] == upstream_ids[1], upstream_ids
            events = []
            for _ in range(20):
                raw = subprocess.check_output(['docker', 'exec', 'cauce-tls-proxy-1',
                    'cat', '/var/log/cauce/access.jsonl'], text=True)
                events = [json.loads(line) for line in raw.splitlines()
                          if json.loads(line).get('request_id') in request_ids]
                if len(events) >= 2:
                    break
                await asyncio.sleep(.1)
            assert len(events) >= 2, events
            first, second = events[-2:]
            a1, a2 = first['attempts'][0], second['attempts'][0]
            assert a1['reused'] is False and a1['tls_ms'] is not None, a1
            assert a2['reused'] is True and a2['tls_ms'] is None, a2
            assert a1['connection_id'] == a2['connection_id']
            results['cases'].append('HTTPS upstream socket reused; second tls_ms is null')

            async def denied():
                try:
                    async with websockets.connect(uri, ssl=ctx, open_timeout=4):
                        raise AssertionError('untrusted upstream TLS unexpectedly upgraded')
                except websockets.exceptions.InvalidStatus as exc:
                    assert exc.response.status_code == 502, exc.response.status_code
                    return
                raise AssertionError('expected HTTP 502 for backend TLS failure')

            # The proxy must reject a trusted CA with the wrong DNS name.
            restart_with(original_config.replace('server_name = "backend.test"',
                                                  'server_name = "wrong.test"'))
            await denied()
            results['cases'].append('upstream hostname mismatch rejected with HTTP 502')

            # A matching DNS name with a CA that didn't issue the server cert is also rejected.
            restart_with(original_config.replace('/run/cauce-tls/ca.crt',
                                                  '/run/cauce-tls/other-ca.crt'))
            await denied()
            results['cases'].append('untrusted upstream CA rejected with HTTP 502')
        asyncio.run(exercise())
        results['architecture'] = subprocess.check_output(['docker', 'image', 'inspect', 'cauce-tls-proxy', '--format', '{{.Architecture}}'], text=True).strip()
        results['result'] = 'PASS'
        (ROOT/'docs/docker-tls-websocket.jsonl').write_text(subprocess.check_output(['docker','exec','cauce-tls-proxy-1','cat','/var/log/cauce/access.jsonl'],text=True))
        (ROOT/'docs/docker-tls-websocket.json').write_text(json.dumps(results, indent=2)+'\n')
        print('PASS: ' + '; '.join(results['cases']))
    finally:
        if keep:
            restart_with(original_config)
        else:
            write_config(original_config)
        try:
            logs = subprocess.check_output([*dc, 'logs', '--no-color'], text=True, stderr=subprocess.STDOUT)
            (ROOT/'docs/docker-tls-websocket-compose.log').write_text(logs)
        except subprocess.CalledProcessError:
            pass
        if not keep:
            run(*dc, 'down', '--remove-orphans')


if __name__ == '__main__':
    main()
