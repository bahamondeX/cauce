#!/usr/bin/env python3
"""Short local HTTP smoke comparison; not a saturation or universal performance test."""
import json
from pathlib import Path
import subprocess
import sys
import time
root=Path(__file__).resolve().parents[1]
out=root/'bench/results'
out.mkdir(exist_ok=True)
env=subprocess.check_output(['docker','info','--format','{{.ServerVersion}} {{.Architecture}} CPUs={{.NCPU}} memory={{.MemTotal}} OS={{.OperatingSystem}}'],text=True)
(out/'environment.txt').write_text(env+subprocess.check_output(['docker','exec','cauce-local-nginx-1','nginx','-v'],stderr=subprocess.STDOUT,text=True))
for label,port,container in [('cauce-logs-off',8080,'cauce-local-proxy-1'),('nginx-logs-off',8081,'cauce-local-nginx-1')]:
    subprocess.run([sys.executable,str(root/'bench/load.py'),f'http://localhost:{port}/','--seconds','2','--output',str(out/f'{label}.warmup.jsonl')],check=True,stdout=subprocess.DEVNULL)
    run=subprocess.Popen([sys.executable,str(root/'bench/load.py'),f'http://localhost:{port}/','--seconds','5','--output',str(out/f'{label}.jsonl')])
    samples=[]
    for _ in range(2):
        time.sleep(1)
        samples.append(json.loads(subprocess.check_output(['docker','stats','--no-stream','--format','{{json .}}',container],text=True)))
    assert run.wait(timeout=10)==0
    (out/f'{label}.stats.json').write_text(json.dumps(samples,indent=2)+'\n')
