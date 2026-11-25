#!/usr/bin/env python3
"""Docker TLS rotation, certificate expiry, and timeout regression tests. Requires TLS profile --keep."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'work/tls'
NAME='cauce-tls-lifecycle'
IMAGE=os.environ.get('CAUCE_TEST_IMAGE','cauce-tls-proxy')
CA=WORK/'ca.crt'
results=[]
def docker(*args): return subprocess.check_output(['docker',*args],text=True).strip()
def passed(case,**detail):
    results.append({'case':case,'passed':True,**detail}); print('PASS:',case,flush=True)
def request(expected=200):
    c=http.client.HTTPSConnection('localhost',18444,context=ssl.create_default_context(cafile=str(CA)),timeout=3)
    try:
        c.request('GET','/http');r=c.getresponse();data=r.read();assert r.status==expected,(r.status,data);return data
    finally: c.close()
def fingerprint():
    with ssl.create_default_context(cafile=str(CA)).wrap_socket(socket.create_connection(('localhost',18444),timeout=2),server_hostname='localhost') as s:
        return hashlib.sha256(s.getpeercert(True)).hexdigest()
def remove(name): subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def hup(event):
    # Wait for Colima's shared filesystem to expose each completed host write.
    for file in [WORK/'lifecycle.toml',WORK/'lifecycle.crt',WORK/'lifecycle-ca.crt']:
        expected=hashlib.sha256(file.read_bytes()).hexdigest()
        for _ in range(50):
            remote=docker('exec',NAME,'sha256sum','/run/cauce-tls/'+file.name).split()[0]
            if remote==expected:break
            time.sleep(.05)
        else:raise AssertionError('container did not observe completed '+file.name)
    before=subprocess.run(['docker','logs',NAME],capture_output=True,text=True).stderr.count(event)
    docker('kill','--signal','HUP',NAME)
    for _ in range(100):
        logs=subprocess.run(['docker','logs',NAME],capture_output=True,text=True).stderr
        if logs.count(event)>before:return
        time.sleep(.03)
    raise AssertionError('missing '+event)
def openssl(*args):subprocess.run(['openssl',*args],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

try:
    base=(ROOT/'examples/tls/cauce.toml').read_text().replace('localhost:8443','localhost:18444')
    base=base.replace('/run/cauce-tls/proxy.crt','/run/cauce-tls/lifecycle.crt').replace('/run/cauce-tls/ca.crt','/run/cauce-tls/lifecycle-ca.crt')
    base=base.replace('[limits]','[limits]\ntls_handshake_ms = 300')
    # This fixture asserts an HTTP/1.1-only ALPN offer. The HTTP/2 suite
    # separately verifies negotiation with the default HTTP/2 setting.
    base += '\n[http2]\nenabled = false\n'
    config=WORK/'lifecycle.toml';config.write_text(base)
    shutil.copyfile(WORK/'proxy.crt',WORK/'lifecycle.crt');shutil.copyfile(CA,WORK/'lifecycle-ca.crt')
    remove(NAME)
    docker('run','-d','--name',NAME,'--network','cauce-tls_default','-p','127.0.0.1:18444:8080','--read-only','--cap-drop','ALL',
           '--tmpfs','/var/log/cauce:uid=10001,gid=10001,mode=0700','-v',f'{WORK}:/run/cauce-tls:ro',IMAGE,'/run/cauce-tls/lifecycle.toml')
    for _ in range(80):
        try:request();break
        except OSError as error:
            if _ == 30: print(repr(error),flush=True)
            time.sleep(.1)
    else:raise AssertionError('lifecycle listener not ready')
    for version in [ssl.TLSVersion.TLSv1_2,ssl.TLSVersion.TLSv1_3]:
        ctx=ssl.create_default_context(cafile=str(CA));ctx.minimum_version=version;ctx.maximum_version=version;ctx.set_alpn_protocols(['http/1.1'])
        with ctx.wrap_socket(socket.create_connection(('localhost',18444),timeout=2),server_hostname='localhost') as s:
            assert s.selected_alpn_protocol()=='http/1.1'
            passed('inbound '+s.version()+' with ALPN http/1.1')
    ctx=ssl.create_default_context(cafile=str(CA));ctx.set_alpn_protocols(['h2'])
    try:
        with ctx.wrap_socket(socket.create_connection(('localhost',18444),timeout=2),server_hostname='localhost'):pass
        raise AssertionError('h2-only ALPN accepted')
    except ssl.SSLError:passed('unsupported h2-only ALPN rejected')
    with socket.create_connection(('localhost',18444),timeout=2) as raw:
        started=time.monotonic();raw.settimeout(2);assert raw.recv(1)==b''
        assert time.monotonic()-started<1.5
    passed('stalled incoming TLS handshake times out')
    initial=fingerprint()
    (WORK/'lifecycle.crt').write_text('not a certificate')
    hup('reload_rejected');request();assert fingerprint()==initial
    passed('invalid inbound certificate rejects reload and preserves working TLS')
    (WORK/'proxy-rotate.ext').write_text('subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n')
    openssl('x509','-req','-in',str(WORK/'proxy.csr'),'-CA',str(CA),'-CAkey',str(WORK/'ca.key'),'-set_serial','912345678','-days','3','-sha256','-extfile',str(WORK/'proxy-rotate.ext'),'-out',str(WORK/'lifecycle.crt'))
    hup('reloaded');request();assert fingerprint()!=initial
    passed('valid certificate rotation takes effect on new connections')
    (WORK/'lifecycle-ca.crt').write_text('bad CA')
    hup('reload_rejected');request()
    passed('invalid upstream CA file rejects entire reload')
    shutil.copyfile(WORK/'other-ca.crt',WORK/'lifecycle-ca.crt');hup('reloaded');request(502)
    passed('changing trust drains old HTTPS pool; untrusted backend gives 502')
    shutil.copyfile(CA,WORK/'lifecycle-ca.crt');hup('reloaded');request()
    passed('restored upstream trust establishes a working new generation')
    (WORK/'expired.ext').write_text('subjectAltName=DNS:backend.test\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n')
    openssl('x509','-req','-in',str(WORK/'backend.csr'),'-CA',str(CA),'-CAkey',str(WORK/'ca.key'),'-set_serial','912345679','-not_before','20200101000000Z','-not_after','20200102000000Z','-sha256','-extfile',str(WORK/'expired.ext'),'-out',str(WORK/'expired.crt'))
    remove('cauce-tls-expired')
    docker('run','-d','--name','cauce-tls-expired','--network','cauce-tls_default','-v',f'{WORK}:/run/cauce-tls:ro',
           '-e','TLS_CERT=/run/cauce-tls/expired.crt','-e','TLS_KEY=/run/cauce-tls/backend.key','cauce-tls-websocket')
    time.sleep(.5)
    config.write_text(base.replace('websocket:8766','cauce-tls-expired:8766'));hup('reloaded');request(502)
    logs=docker('exec',NAME,'cat','/var/log/cauce/access.jsonl')
    assert any(json.loads(line).get('reason')=='tls_handshake_error' for line in logs.splitlines())
    passed('expired upstream certificate rejected with 502')
    remove('cauce-tls-stall')
    code='import socket,time; s=socket.socket();s.bind(("0.0.0.0",8767));s.listen(); c,a=s.accept();time.sleep(30)'
    docker('run','-d','--name','cauce-tls-stall','--network','cauce-tls_default','--entrypoint','python','cauce-tls-websocket','-c',code)
    time.sleep(.3)
    config.write_text(base.replace('websocket:8766','cauce-tls-stall:8767'));hup('reloaded')
    started=time.monotonic();request(504);elapsed=time.monotonic()-started;assert elapsed<1.5
    passed('stalled upstream TLS handshake returns 504 within TLS deadline',elapsed_ms=round(elapsed*1000,2))
    time.sleep(.15)
    logs=docker('exec',NAME,'cat','/var/log/cauce/access.jsonl')
    (ROOT/'docs/increment2-tls-lifecycle.jsonl').write_text(logs+'\n')
    assert any(json.loads(line).get('reason')=='tls_handshake_timeout' for line in logs.splitlines())
    (ROOT/'docs/increment2-tls-lifecycle.json').write_text(json.dumps({'image':IMAGE,'cases':results,'passed':len(results)},indent=2)+'\n')
finally:
    (ROOT/'docs/lifecycle-last-container.log').write_text(subprocess.run(['docker','logs',NAME],capture_output=True,text=True).stderr)
    for name in [NAME,'cauce-tls-expired','cauce-tls-stall']:remove(name)
