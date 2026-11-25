#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import jsonschema
p=argparse.ArgumentParser()
p.add_argument('paths',nargs='+',type=Path)
a=p.parse_args()
schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/access-log.schema.json').read_text())
v=jsonschema.Draft202012Validator(schema,format_checker=jsonschema.FormatChecker())
count=0
for path in a.paths:
    for line in path.read_text().splitlines():
        v.validate(json.loads(line))
        count+=1
print(f'Validated {count} events')
