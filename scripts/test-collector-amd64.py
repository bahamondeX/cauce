#!/usr/bin/env python3
"""Small real amd64 collector smoke on Colima ARM64 emulation."""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('collector_suite', ROOT/'scripts/test-collector.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
parser = argparse.ArgumentParser()
parser.add_argument('--image', default='cauce-collector:amd64')
args = parser.parse_args()
args.docker = True
args.binary = ROOT/'target/debug/cauce-collector'
suite = module.Suite(args)
try:
    architecture = module.run('docker','image','inspect',args.image,'--format','{{.Architecture}}').stdout.strip()
    assert architecture == 'amd64'
    d, config, path, data = suite.new('amd64-recovery', events=2)
    suite.invoke(path, '--once')
    suite.invoke(path, '--once')
    assert suite.status(path)['pending_events'] == 2
    batch = suite.batches(path)[0]
    assert {e['event_id'] for e in batch['events']} == {e['event_id'] for e in data}
    suite.passed('amd64 binary persists spool and resumes checkpoints without duplicate events')
    config.update(delivery='proposal-http', endpoint='https://sink.test:9443/ingest',
                  token_file=suite.path(d/'token'), ca_file=suite.path(suite.sink/'ca.crt'))
    path = suite.config(d, config)
    suite.control(wrong_id=True)
    assert suite.invoke(path, '--once', ok=False).returncode != 0
    assert suite.status(path)['pending_events'] == 2
    suite.passed('amd64 HTTPS rejects an incorrect ACK and retains its stable batch')
    suite.control()
    suite.invoke(path, '--once')
    assert suite.status(path)['pending_events'] == 0
    assert suite.received()[-1]['batch'] == batch
    suite.passed('amd64 HTTPS retries intact batch and commits a valid ACK')
    result = dict(result='PASS', architecture=architecture, cases=suite.cases,
                  execution='Colima ARM64 with amd64 emulation; not native amd64 hardware',
                  fixture='Local proposal-1 only; no Connectivity ingestion',
                  image_id=module.run('docker','image','inspect',args.image,'--format','{{.Id}}').stdout.strip())
    (ROOT/'docs/collector-amd64.json').write_text(json.dumps(result,indent=2)+'\n')
finally:
    suite.close()
