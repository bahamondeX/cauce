"""HTTP and process probes inside the isolated proxy namespaces."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

TOKEN = os.environ['CAUCE_ADMIN_TOKEN']


def metrics():
    req = urllib.request.Request('http://127.0.0.1:9090/metrics',
                                 headers={'Authorization': 'Bearer ' + TOKEN})
    with urllib.request.urlopen(req, timeout=2) as response:
        text = response.read().decode()
    return {line.split()[0]: float(line.split()[1]) for line in text.splitlines()
            if line and not line.startswith('#')}


def request(i):
    started = time.monotonic()
    client = http.client.HTTPConnection('127.0.0.1', 8080, timeout=3)
    try:
        client.request('GET', '/test?sequence=' + str(i), headers={'Host': 'proxy:8080'})
        response = client.getresponse()
        body = response.read()
        assert response.status == 200 and body == b'resilience-ok\n', (response.status, body)
        identity = response.getheader('x-request-id')
        assert identity
        return {'request_id': identity, 'status': response.status,
                'elapsed_ms': (time.monotonic() - started) * 1000}
    finally:
        client.close()


def sample():
    fields = {}
    for line in Path('/proc/1/status').read_text().splitlines():
        if line.startswith(('VmRSS:', 'Threads:')):
            fields[line.split(':')[0]] = int(line.split()[1])
    return {'metrics': metrics(), 'process': fields,
            'file_descriptors': len(list(Path('/proc/1/fd').iterdir()))}


if __name__ == '__main__':
    if sys.argv[1] == 'sample':
        result = sample()
    elif sys.argv[1] == 'batch':
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(sys.argv[3])) as executor:
            result = list(executor.map(request, range(int(sys.argv[2]))))
    else:
        raise ValueError('unknown action')
    print(json.dumps(result))
