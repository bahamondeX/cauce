#!/usr/bin/env python3
"""Short Docker client conformance check against a synthetic HTTPS sink only."""
import argparse
import datetime
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector_fixture_suite", ROOT / "scripts/test-collector.py")
suite_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite_module)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--temp-dir', type=Path, default=Path('/tmp/colima'),
                        help='shared temporary directory mounted by the Docker VM')
    args = parser.parse_args()
    suite = suite_module.Suite(argparse.Namespace(docker=True, image=args.image,
                                                  binary=ROOT/'target/debug/cauce-collector',
                                                  tempdir=args.temp_dir))
    cases = []
    observations = []
    try:
        # Contract path and configured event/byte ceilings are explicit.
        received_before = len(suite.received())
        for status in (201, 202, 401, 403):
            d, c, config, _ = suite.new('http-' + str(status), delivery='proposal-http',
                                         spool_max_bytes=1048576, db_max_bytes=2097152,
                                         batch_max_events=128, batch_max_bytes=524288,
                                         max_line_bytes=262144)
            c['endpoint'] = f'https://sink.test:{suite.port}/v1/ingest/cauce-proposal'
            config = suite.config(d, c)
            suite.control(status=status)
            before = len(suite.received())
            result = suite.invoke(config, '--once', ok=False)
            assert result.returncode != 0, (status, result.stderr)
            assert suite.status(config)['pending_events'] == 1
            row = suite.received()[-1]
            assert len(suite.received()) == before + 1
            assert row['status'] == status and row['authorized'] is True
            assert row['path'] == '/v1/ingest/cauce-proposal'
            stable_id = row['batch']['batch_id']
            suite.control()
            suite.invoke(config, '--once')
            assert suite.status(config)['pending_events'] == 0
            assert suite.received()[-1]['batch']['batch_id'] == stable_id
            assert suite.received()[-1]['status'] == 200
            observations.append({'case': 'http-' + str(status), 'observed_status': row['status'],
                                 'authorized': row['authorized'], 'batch_id': stable_id,
                                 'pending_after_recovery': 0})
            cases.append(f'HTTP {status} rejected; same durable batch recovered on HTTP 200')

        # Invalid exact ACK must retain the batch and produce a fixed error with
        # neither the untrusted property name nor its reflected value.
        d, c, config, _ = suite.new('ack-reflection', delivery='proposal-http')
        c['endpoint'] = f'https://sink.test:{suite.port}/v1/ingest/cauce-proposal'
        config = suite.config(d, c)
        secret = 'Bearer-secret-that-must-not-enter-logs'
        suite.control(extra_ack=secret, extra_ack_value=secret)
        result = suite.invoke(config, '--once', ok=False)
        assert result.returncode != 0
        assert secret not in result.stderr
        assert secret not in result.stdout
        assert suite.received()[-1]['status'] == 200
        assert suite.received()[-1]['authorized'] is True
        assert suite.status(config)['pending_events'] == 1
        batch_id = suite.received()[-1]['batch']['batch_id']
        suite.control()
        suite.invoke(config, '--once')
        assert suite.status(config)['pending_events'] == 0
        assert suite.received()[-1]['batch']['batch_id'] == batch_id
        cases.append('extra ACK field rejected without logging untrusted names/values; retry recovered')

        d, c, config, data = suite.new('lost-ack-retry', delivery='proposal-http')
        c['endpoint'] = f'https://sink.test:{suite.port}/v1/ingest/cauce-proposal'
        config = suite.config(d, c)
        dedup_path = suite.sink/'dedup.json'
        dedup_before = set(json.loads(dedup_path.read_text())) if dedup_path.exists() else set()
        suite.control(drop_ack=True)
        result = suite.invoke(config, '--once', ok=False)
        assert result.returncode != 0
        assert suite.status(config)['pending_events'] == 1
        first = suite.received()[-1]['batch']
        assert suite.received()[-1]['status'] == 200
        assert suite.received()[-1]['authorized'] is True
        dedup_after_lost_ack = set(json.loads(dedup_path.read_text()))
        assert dedup_after_lost_ack - dedup_before == {data[0]['event_id']}
        suite.control()
        suite.invoke(config, '--once')
        assert suite.status(config)['pending_events'] == 0
        assert suite.received()[-1]['batch'] == first
        assert first['events'][0]['event_id'] == data[0]['event_id']
        dedup_after = set(json.loads(dedup_path.read_text()))
        assert dedup_after - dedup_before == {data[0]['event_id']}
        observations.append({'case': 'lost-ack', 'observed_status': 200, 'authorized': True,
                             'batch_id': first['batch_id'], 'event_id': data[0]['event_id'],
                             'unique_events_after_first_accept': 1,
                             'unique_events_after_retry': 1, 'pending_after_recovery': 0})
        assert sum(row['batch']['batch_id'] == first['batch_id'] for row in suite.received()) == 2
        cases.append('lost ACK retried with stable batch/event IDs and drained after synthetic sink recovery')

        d, c, config, data = suite.new('token-rotation', delivery='proposal-http')
        c['endpoint'] = f'https://sink.test:{suite.port}/v1/ingest/cauce-proposal'
        config = suite.config(d, c)
        suite.control(token='fixture-token-v2')
        result = suite.invoke(config, '--once', ok=False)
        assert result.returncode != 0
        assert suite.status(config)['pending_events'] == 1
        first = suite.received()[-1]['batch']
        assert suite.received()[-1]['status'] == 401
        assert suite.received()[-1]['authorized'] is False
        (d/'token').write_text('fixture-token-v2\n')
        suite.control(token='fixture-token-v2')
        suite.invoke(config, '--once')
        assert suite.status(config)['pending_events'] == 0
        assert suite.received()[-1]['batch'] == first
        assert suite.received()[-1]['status'] == 200
        assert suite.received()[-1]['authorized'] is True
        assert first['events'][0]['event_id'] == data[0]['event_id']
        cases.append('token file reread and rotated; retained batch/event IDs recovered on synthetic HTTP 200')

        for kind in ('untrusted-ca', 'hostname-mismatch'):
            d, c, config, _ = suite.new('tls-' + kind, delivery='proposal-http')
            if kind == 'untrusted-ca':
                c['ca_file'] = suite.path(suite.sink/'other.crt')
            else:
                ip = json.loads(suite_module.run('docker', 'inspect', suite.sink_name).stdout)[0][
                    'NetworkSettings']['Networks'][suite.network]['IPAddress']
                c['endpoint'] = f'https://{ip}:{suite.port}/v1/ingest/cauce-proposal'
            config = suite.config(d, c)
            before = len(suite.received())
            result = suite.invoke(config, '--once', ok=False)
            assert result.returncode != 0
            assert suite.status(config)['pending_events'] == 1
            assert len(suite.received()) == before, 'must fail TLS before sending the request'
            cases.append(f'{kind} rejected before HTTP request; pending batch preserved')

        result = {'result': 'PASS', 'scope': 'synthetic local HTTPS fixture; no Connectivity auth/service integration',
                  'completed_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'image': args.image, 'image_id': suite_module.run('docker', 'image', 'inspect', args.image,
                                                                    '--format', '{{.Id}}').stdout.strip(),
                  'batch_max_events': 128, 'batch_max_bytes': 524288,
                  'cases': cases, 'observations': observations,
                  'fixture_requests_before_cases': received_before}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        suite.close()
        shutil.rmtree(suite.root, ignore_errors=True)


if __name__ == '__main__':
    main()
