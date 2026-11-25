#!/usr/bin/env python3
"""Validate exported proposal envelopes and their complete access-log events."""
import argparse
import json
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('paths', nargs='+', type=Path)
args = parser.parse_args()
schemas = [json.loads((ROOT/'schemas'/name).read_text()) for name in
           ['access-log.schema.json', 'collector-batch.schema.json', 'collector-ack.schema.json']]
registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas)
for schema in schemas:
    jsonschema.Draft202012Validator.check_schema(schema)
validator = jsonschema.Draft202012Validator(schemas[1], registry=registry,
                                          format_checker=jsonschema.FormatChecker())
batches = events = 0
for path in args.paths:
    for line in path.read_text().splitlines():
        batch = json.loads(line)
        validator.validate(batch)
        batches += 1
        events += len(batch['events'])
assert batches, 'export contains no batches'
print(f'PASS: {batches} envelopes and {events} complete events validated against JSON Schema')
