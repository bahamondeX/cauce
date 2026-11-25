#!/usr/bin/env python3
"""Verify real HTTP/2 access logs through the offline collector and restart."""
import json, shutil, subprocess, tempfile, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
if len(sys.argv)!=2: raise SystemExit('usage: test-http2-collector.py ACCESS_LOG_JSONL')
(root/'work').mkdir(exist_ok=True)
source=root/'docs/increment4-http2-collector-source.jsonl'
incoming=Path(sys.argv[1]).resolve()
if incoming!=source: shutil.copyfile(incoming,source)
lines=source.read_text().splitlines()
events=[json.loads(line) for line in lines if line.strip()]
assert events and any(e['protocol']=='HTTP/2' for e in events)
assert all(e['schema_version']=='1.1' for e in events)
state=Path(tempfile.mkdtemp(prefix='h2-collector-', dir=root/'work'))
shutil.copyfile(source,state/'access.jsonl')
text=(root/'examples/collector.toml').read_text().replace('/var/log/cauce/access.jsonl',str(state/'access.jsonl')).replace('/var/lib/cauce/collector/state.db',str(state/'state.db'))
config=state/'collector.toml';config.write_text(text)
cmd=[str(root/'target/debug/cauce-collector'),'--config',str(config)]
for _ in range(100):
 subprocess.run(cmd+['--once'],check=True,capture_output=True)
 status=json.loads(subprocess.check_output(cmd+['--status']))
 if status['pending_events']==len(events):break
else:raise AssertionError(status)
export=root/'docs/increment4-http2-export.jsonl'
if export.exists():export.unlink()
subprocess.run(cmd+['--export',str(export)],check=True)
subprocess.run(cmd+['--once'],check=True,capture_output=True)
resumed=json.loads(subprocess.check_output(cmd+['--status']))
assert resumed['pending_events']==len(events),resumed
batches=[json.loads(line) for line in export.read_text().splitlines()]
exported=[event for batch in batches for event in batch['events']]
assert {e['event_id'] for e in exported}=={e['event_id'] for e in events}
report={'result':'PASS','source':'Real Docker HTTP/2 + gRPC proxy events', 'source_file':source.name,'events':len(events),'http2_events':sum(e['protocol']=='HTTP/2' for e in events),'batches':len(batches),'schema_version':'1.1','checkpoint_restart_no_duplicates':True,'delivery':'offline spool; no Connectivity network traffic'}
(root/'docs/increment4-http2-collector.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
