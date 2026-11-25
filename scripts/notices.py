#!/usr/bin/env python3
"""Capture dependency license metadata and original notice files; no license rewriting."""
import json
import os
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
meta = json.loads(subprocess.check_output(['cargo','metadata','--locked','--format-version','1','--filter-platform','aarch64-unknown-linux-gnu'], cwd=root))
dest = root/'third-party'
dest.mkdir(exist_ok=True)
rows = []
for p in meta['packages']:
    if p['name'] == 'cauce':
        continue
    directory = Path(p['manifest_path']).parent
    output = dest / f"{p['name']}-{p['version']}"
    output.mkdir(exist_ok=True)
    found = []
    for path in directory.iterdir():
        if path.is_file() and path.name.upper().startswith(('LICENSE','LICENCE','COPYING','NOTICE','COPYRIGHT')):
            shutil.copyfile(path, output/path.name)
            found.append(path.name)
    if p.get('license_file'):
        path = directory/p['license_file']
        if path.exists() and path.name not in found:
            shutil.copyfile(path, output/path.name)
            found.append(path.name)
    rows.append({'name':p['name'],'version':p['version'],'license':p.get('license'), 'notices':found, 'source':p.get('repository')})
(dest/'manifest.json').write_text(json.dumps(rows, indent=2)+'\n')
print(f'Captured {len(rows)} dependency license entries')
