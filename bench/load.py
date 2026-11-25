#!/usr/bin/env python3
"""Open-loop, bounded offered-rate generator. Latency starts at scheduled arrival.
Every offered request gets a raw record, including queue drops and errors.
"""
import argparse
import http.client
import json
import math
from pathlib import Path
import platform
import queue
import threading
import time
import urllib.parse

p = argparse.ArgumentParser()
p.add_argument('url')
p.add_argument('--host', default='localhost:8080')
p.add_argument('--rate', type=float, default=100)
p.add_argument('--seconds', type=float, default=10)
p.add_argument('--concurrency', type=int, default=16)
p.add_argument('--output', required=True)
a = p.parse_args()
if not (0 < a.rate <= 100000 and 0 < a.seconds <= 3600 and 0 < a.concurrency <= 1024):
    p.error('invalid load limits')
u = urllib.parse.urlsplit(a.url)
if u.scheme != 'http':
    p.error('HTTP only in increment 1')
work = queue.Queue(maxsize=a.concurrency * 2)
results = []
lock = threading.Lock()
start = time.monotonic()


def worker():
    c = None
    while True:
        item = work.get()
        if item is None:
            work.task_done()
            if c:
                c.close()
            return
        number, scheduled = item
        sent = time.monotonic()
        row = {'n': number, 'scheduled_s': scheduled-start, 'queue_ms': (sent-scheduled)*1000}
        try:
            if c is None:
                c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=3)
            c.request('GET', u.path or '/', headers={'Host': a.host})
            r = c.getresponse()
            row.update(status=r.status, bytes=len(r.read()), outcome='response')
        except Exception as error:
            row.update(status=None, bytes=0, outcome='error', error=type(error).__name__)
            if c:
                c.close()
            c = None
        row['latency_ms'] = (time.monotonic()-scheduled)*1000
        with lock:
            results.append(row)
        work.task_done()


threads = [threading.Thread(target=worker) for _ in range(a.concurrency)]
for t in threads:
    t.start()
count = int(a.rate*a.seconds)
for n in range(count):
    due = start+n/a.rate
    wait = due-time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        work.put_nowait((n, due))
    except queue.Full:
        results.append({'n':n, 'scheduled_s':due-start, 'outcome':'generator_queue_drop', 'status':None, 'latency_ms':None})
work.join()
end = time.monotonic()
for _ in threads:
    work.put(None)
for t in threads:
    t.join()
path = Path(a.output)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('w') as f:
    for row in sorted(results, key=lambda r:r['n']):
        f.write(json.dumps(row)+'\n')
latencies = sorted(r['latency_ms'] for r in results if r['outcome']=='response')
quantile = lambda q: latencies[max(0, math.ceil(len(latencies)*q)-1)] if latencies else None
summary = dict(url=a.url, host=a.host, offered_rate=a.rate, offered=count, wall_s=end-start,
               responses=len(latencies), errors=sum(r['outcome']=='error' for r in results),
               queue_drops=sum(r['outcome']=='generator_queue_drop' for r in results),
               http_errors=sum(r['status'] is not None and r['status']>=400 for r in results),
               throughput=len(latencies)/(end-start), p50_ms=quantile(.5), p95_ms=quantile(.95), p99_ms=quantile(.99),
               platform=platform.platform(), python=platform.python_version(), concurrency=a.concurrency,
               latency_origin='scheduled_arrival_including_generator_queue',
               caveat='short local run; no production or universal performance claim')
path.with_suffix('.summary.json').write_text(json.dumps(summary, indent=2)+'\n')
print(json.dumps(summary, indent=2))
