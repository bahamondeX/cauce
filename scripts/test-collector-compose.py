#!/usr/bin/env python3
"""Exercise the actual optional Compose profile against real proxy access logs."""
import http.client
import argparse
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
DC = ['docker','compose','-p','cauce-local','-f',str(ROOT/'compose.yaml'),'--profile','collector']


def run(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True).stdout


def request():
    connection = http.client.HTTPConnection('localhost',8080,timeout=3)
    connection.request('GET','/')
    response = connection.getresponse()
    status, request_id = response.status, response.getheader('x-request-id')
    response.read()
    connection.close()
    assert status==200 and request_id, (status,request_id)
    return request_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-prefix', default='collector-compose',
                        help='basename for the report and export under docs/')
    prefix = parser.parse_args().report_prefix
    if Path(prefix).name != prefix:
        parser.error('--report-prefix must be a basename')
    if subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        run('colima','start')
    run(*DC,'up','--build','-d')
    try:
        for _ in range(40):
            try:
                request()
                break
            except (OSError, AssertionError):
                time.sleep(.25)
        else:
            raise AssertionError('proxy did not become ready')
        expected = {request() for _ in range(3)}
        exported = ''
        for _ in range(10):
            time.sleep(.5)
            run(*DC,'stop','collector')
            exported=run(*DC,'run','--rm','--no-deps','collector',
                         '--config','/etc/cauce/collector.toml','--export','stdout')
            batches=[json.loads(line) for line in exported.splitlines() if line.strip()]
            observed={e['request_id'] for b in batches for e in b['events']}
            if expected <= observed:
                break
            run(*DC,'up','--no-build','-d','collector')
        else:
            raise AssertionError('current proxy requests were not found in collector spool')
        status=json.loads(run(*DC,'run','--rm','--no-deps','collector',
                              '--config','/etc/cauce/collector.toml','--status'))
        independent=request()  # Proxy still serves while collector is stopped.
        result={'result':'PASS','cases':[
            'real proxy JSONL volume consumed by separate optional collector',
            'three generated request IDs found intact in durable exported batches',
            'proxy HTTP 200 while collector is stopped',
            'collector offline with network_mode=none and non-root UID'],
            'request_ids':sorted(expected),'independent_request_id':independent,'status':status}
        (ROOT/'docs'/f'{prefix}-export.jsonl').write_text(exported)
    finally:
        run(*DC,'up','--no-build','-d','collector')
    cid=run(*DC,'ps','-q','collector').strip()
    inspect=json.loads(run('docker','inspect',cid))[0]
    assert inspect['State']['Running'], inspect['State']
    assert inspect['HostConfig']['NetworkMode']=='none'
    assert inspect['Config']['User']=='10001:10001'
    result['image_id']=inspect['Image']
    result['collector_network_mode']=inspect['HostConfig']['NetworkMode']
    result['collector_user']=inspect['Config']['User']
    (ROOT/'docs'/f'{prefix}.json').write_text(json.dumps(result,indent=2)+'\n')
    print('PASS: four real Compose pipeline checks')

if __name__=='__main__': main()
