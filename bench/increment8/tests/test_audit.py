import copy,importlib.util,json,pathlib,tempfile,unittest
SPEC=importlib.util.spec_from_file_location('summary',pathlib.Path(__file__).resolve().parents[1]/'summarize.py');m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)
class AuditTests(unittest.TestCase):
    def fixture(self,root):
        rows=[dict(seq=i,outcome='ok',session_id=0,stream_id=2*i+1,status=200,bytes=1024,scheduled_at_ms=0,started_at_ms=1,ended_at_ms=2+i,arrival_latency_ms=2+i,service_latency_ms=1+i,scheduler_lag_ms=0) for i in range(2)]
        (root/'raw.jsonl').write_text('\n'.join(map(json.dumps,rows)))
        resources=dict(cpu={k:dict(cpu_percent_of_one_core=2,throttling_delta={}) for k in ('proxy','client','backend')},samples=[{},{}],processes=[dict(run='r',processes=[dict(rss_bytes=1024)])],cgroup_peak_bytes=dict(proxy=4096))
        (root/'resources.json').write_text(json.dumps(resources))
        return dict(id='r',raw='raw.jsonl',resources='resources.json',initial_backend=dict(active=0,requests=0,completed=0,connections=0,peak_connections=0),backend=dict(requests=2,peak_active=1),generator=dict(offered=2,ok=2,errors=0,rejected=0,dropped=0,remote_max_concurrent_streams=[16],sessions=[dict(peak_inflight=1)],protocol='h2c',mode='http2',measurement_elapsed_s=1,warmup=dict(outcomes=dict(ok=1))))
    def test_good_and_missing_offer(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);r=self.fixture(root);self.assertEqual(m.audit_run(root,r)['raw_offers'],2)
            r['generator']['offered']=3
            with self.assertRaisesRegex(ValueError,'lost'):m.audit_run(root,r)
    def test_backend_and_protocol_evidence_required(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);r=self.fixture(root);r['backend']['requests']=1
            with self.assertRaisesRegex(ValueError,'backend'):m.audit_run(root,r)
            r=self.fixture(root);r['generator']['remote_max_concurrent_streams']=[128]
            with self.assertRaisesRegex(ValueError,'SETTINGS'):m.audit_run(root,r)
    def test_duplicate_stream_and_wrong_grpc_status(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);r=self.fixture(root);rows=[json.loads(x) for x in (root/'raw.jsonl').read_text().splitlines()];rows[1]['stream_id']=1
            (root/'raw.jsonl').write_text('\n'.join(map(json.dumps,rows)))
            with self.assertRaisesRegex(ValueError,'reused'):m.audit_run(root,r)
            r=self.fixture(root);r['generator'].update(mode='grpc',method='Unary')
            rows=[json.loads(x)|dict(grpc_status='13',grpc_message_count=1) for x in (root/'raw.jsonl').read_text().splitlines()]
            (root/'raw.jsonl').write_text('\n'.join(map(json.dumps,rows)))
            with self.assertRaisesRegex(ValueError,'gRPC'):m.audit_run(root,r)
    def test_gate_or_stale_pool_contamination_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d)
            for field in ('active','requests','completed','connections','peak_connections'):
                r=self.fixture(root);r['initial_backend'][field]=1
                with self.subTest(field=field),self.assertRaisesRegex(ValueError,'initial backend is contaminated'):
                    m.audit_run(root,r)
            r=self.fixture(root);del r['initial_backend']['peak_connections']
            with self.assertRaisesRegex(ValueError,'initial backend snapshot missing peak_connections'):
                m.audit_run(root,r)
    def test_raw_latencies_match_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);r=self.fixture(root)
            rows=[json.loads(x) for x in (root/'raw.jsonl').read_text().splitlines()]
            rows[0]['arrival_latency_ms']+=0.1
            (root/'raw.jsonl').write_text('\n'.join(map(json.dumps,rows)))
            with self.assertRaisesRegex(ValueError,'arrival latency does not match'):
                m.audit_run(root,r)
            r=self.fixture(root);rows=[json.loads(x) for x in (root/'raw.jsonl').read_text().splitlines()]
            rows[1]['service_latency_ms']+=0.1
            (root/'raw.jsonl').write_text('\n'.join(map(json.dumps,rows)))
            with self.assertRaisesRegex(ValueError,'service latency does not match'):
                m.audit_run(root,r)
if __name__=='__main__':unittest.main()
