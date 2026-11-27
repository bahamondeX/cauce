#!/usr/bin/env python3
"""Describe shared implementation dependencies for the fixed NGINX inventory."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def main():
    definitions = json.loads((ROOT / "compatibility/semantic-foundations.json").read_text())
    rows = [json.loads(line) for line in (ROOT / "compatibility/nginx-gap-backlog.jsonl").read_text().splitlines()]

    def prerequisites(group, active=()):
        if group in active:
            raise ValueError(f"cyclic semantic dependency at {group}")
        direct = definitions[group]["depends_on"]
        return set(direct).union(*(prerequisites(item, (*active, group)) for item in direct))

    nodes = {name: {**spec, "transitive_dependencies": sorted(prerequisites(name)),
                    "direct_oss": 0, "transitive_oss": 0, "direct_add_on": 0}
             for name, spec in definitions.items()}
    rules = {name: tuple(re.compile(pattern) for pattern in spec["selectors"])
             for name, spec in definitions.items()}
    registrations = {}
    for row in rows:
        if row["kind"] == "configure":
            continue
        if row["kind"] != "directive":
            continue
        directive, module, reference = row["directive"], row["module"], row["reference"]
        direct = {group for group, selectors in rules.items() if any(
            pattern.match(module) or pattern.fullmatch(directive) for pattern in selectors)}
        if reference == "njs":
            direct.add("extensions")
        if reference == "nginx-acme":
            direct.add("tls")
        if not direct:
            direct.add("configuration" if row["module_type"] == "NGX_HTTP_MODULE" else "platform")
        all_groups = direct.union(*(prerequisites(group) for group in direct))
        for group in direct:
            nodes[group]["direct_oss" if reference == "oss" else "direct_add_on"] += 1
        if reference == "oss":
            for group in all_groups:
                nodes[group]["transitive_oss"] += 1
        registrations[row["task_id"]] = {
            "depends_on": sorted(direct), "source": row["source"], "line": row["registration"]["line"],
        }
    if len(registrations) != 824:
        raise ValueError("registration denominator drift")
    print(json.dumps({"version": 1, "reference": "NGINX OSS 1.30.8",
                      "groups": nodes, "registrations": registrations,
                      "configure_patterns": sorted(row["option"] for row in rows if row["kind"] == "configure")},
                     sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
