#!/usr/bin/env python3
"""Generate a directive and module inventory from the pinned NGINX OSS tarball."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import urllib.request

VERSION = "1.30.8"
URL = f"https://nginx.org/download/nginx-{VERSION}.tar.gz"
SHA256 = "f867314ada32e717b949930859656c48a72dc6657aec598db58af2f98a40e1c1"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "compatibility/nginx-oss.yaml"
ADD_ONS = {
    "njs": {
        "version": "1.0.2",
        "commit": "72e6a980b9bf2a28ae2d568bbc3b6830a5d7c39e",
        "sha256": "93043e263131feadfd664356b989d86aa009a0e453e33c246ee08e39d96ca45c",
    },
    "nginx-acme": {
        "version": "0.4.2",
        "commit": "0e2edfb58fca6fa383510e8e274ac46008ef8dcd",
        "sha256": "c8b1d5abe878e8233a4cdc710614d8114fd94bc7ebf1f4ece720180f794ff836",
    },
}
TABLE = re.compile(r"^\s*(?:static\s+)?ngx_command_t\s+(\w+)\[\]\s*=\s*\{(.*?)^\};", re.M | re.S)
MODULE = re.compile(r"^\s*ngx_module_t\s+(ngx_\w+_module)\s*=\s*\{(.*?)^\};", re.M | re.S)
COMMAND = re.compile(r'\{\s*ngx_string\("([^"]+)"\)\s*,\s*([^,]+),')
FLAGS = re.compile(r"\bNGX_\w+\b")
COMMENTS = re.compile(r"/\*.*?\*/", re.S)
CONFIGURE_OPTION = re.compile(r"^[ \t]+(--[a-z][\w-]*(?:=[^)\s]*)?)\)", re.M)
ACME_TABLE = re.compile(r"static mut (NGX_HTTP_ACME_\w*COMMANDS): \[ngx_command_t; (\d+)\] = \[(.*?)^\];", re.M | re.S)
ACME_COMMAND = re.compile(r'ngx_string!\("([^"]+)"\),\s+type_: ([^,]+),')


def checked_archive(data, digest):
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Reference tarball SHA-256 mismatch")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def read_c_modules(body, source, modules, provenance=None):
    body = COMMENTS.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), body)
    tables = {match[1]: match for match in TABLE.finditer(body)}
    used = set()
    for name, fields in MODULE.findall(body):
        # NGX_MODULE_V1, module context, command table, module type.
        _, _, table, kind, *_ = (part.strip() for part in fields.split(","))
        assert kind in {"NGX_CORE_MODULE", "NGX_CONF_MODULE", "NGX_EVENT_MODULE",
                        "NGX_HTTP_MODULE", "NGX_STREAM_MODULE", "NGX_MAIL_MODULE"}, (name, kind)
        commands = []
        if table != "NULL":
            used.add(table)
            table_match = tables[table]
            content = table_match[2]
            entries = sorted(COMMAND.finditer(content),
                             key=lambda item: (item[1], list(dict.fromkeys(FLAGS.findall(item[2])))))
            assert len(entries) == content.count("ngx_string("), (name, table)
            commands = [(entry[1], list(dict.fromkeys(FLAGS.findall(entry[2])))) for entry in entries]
            if provenance is not None:
                for ordinal, entry in enumerate(entries):
                    offset = table_match.start(2) + entry.start()
                    setter = re.match(r"\s*(\w+)\s*,", content[entry.end():])
                    provenance[(source, name, ordinal)] = {
                        "line": body.count("\n", 0, offset) + 1,
                        "command_table": table,
                        "setter": setter[1] if setter else None,
                    }
        modules.setdefault(name, []).append((source, kind, commands))
    assert used == tables.keys(), (source, tables.keys() - used)


def inventory(data, provenance=None):
    modules = {}
    with checked_archive(data, SHA256) as archive:
        options = archive.extractfile(f"nginx-{VERSION}/auto/options").read().decode()
        configure_options = sorted(set(CONFIGURE_OPTION.findall(options)))
        if not {"--with-http_v3_module", "--with-mail=dynamic", "--add-dynamic-module=*"}.issubset(configure_options):
            raise ValueError("Incomplete configure option inventory")
        for item in sorted(archive.getmembers(), key=lambda item: item.name):
            source = item.name.removeprefix(f"nginx-{VERSION}/")
            if not item.isfile() or not source.startswith("src/") or not source.endswith(".c"):
                continue
            read_c_modules(archive.extractfile(item).read().decode("utf-8"), source, modules, provenance)
    return modules, configure_options


def add_on_inventory(name, path, provenance=None):
    reference = ADD_ONS[name].copy()
    commit = reference["commit"]
    reference["source"] = f"https://codeload.github.com/nginx/{name}/tar.gz/{commit}"
    raw = read_reference(path, reference["source"])
    modules = {}
    with checked_archive(raw, reference["sha256"]) as archive:
        if name == "njs":
            for kind in ("http", "stream"):
                source = f"nginx/ngx_{kind}_js_module.c"
                body = archive.extractfile(f"{name}-{commit}/{source}").read().decode()
                read_c_modules(body, source, modules, provenance)
        else:
            source = "src/conf.rs"
            body = archive.extractfile(f"{name}-{commit}/{source}").read().decode()
            tables = list(ACME_TABLE.finditer(body))
            assert len(tables) == 2, "Unexpected ACME command tables"
            commands = []
            for group in tables:
                table, count, content = group.groups()
                entries = list(ACME_COMMAND.finditer(content))
                assert len(entries) + 1 == int(count), table
                for entry in entries:
                    directive, flags = entry.groups()
                    if provenance is not None:
                        setter = re.match(r"\s*set:\s*Some\(([\w:]+)\)", content[entry.end():])
                        provenance[(source, "ngx_http_acme_module", len(commands))] = {
                            "line": body.count("\n", 0, group.start(3) + entry.start()) + 1,
                            "command_table": table,
                            "setter": setter[1] if setter else None,
                            "within": "acme_issuer" if table == "NGX_HTTP_ACME_ISSUER_COMMANDS" else None,
                        }
                    item = {"name": directive, "flags": FLAGS.findall(flags)}
                    if table == "NGX_HTTP_ACME_ISSUER_COMMANDS":
                        item["within"] = "acme_issuer"
                    commands.append(item)
            modules["ngx_http_acme_module"] = [{
                "source": source, "type": "NGX_HTTP_MODULE", "directives": commands
            }]
    if name == "njs":
        modules = {
            module: [
                {"source": source, "type": kind,
                 "directives": [{"name": directive, "flags": flags} for directive, flags in commands]}
                for source, kind, commands in variants
            ]
            for module, variants in modules.items()
        }
    assert modules, name
    return {**reference, "modules": {module: {"definitions": variants}
                                    for module, variants in modules.items()}}


def render(modules, configure_options, coverage, add_ons):
    lines = [
        "# Generated by scripts/nginx-inventory.py. Edit nginx-coverage.json for Cauce status.",
        "schema_version: 1", "reference:", f'  version: "{VERSION}"', f"  source: {URL}",
        f"  sha256: {SHA256}",
        "  scope: Module definitions and command tables shipped in the upstream source tarball.",
        "  availability: Commands include conditional builds and platform variants.",
        '  separately_versioned_add_ons: ["nginx-acme", "njs"]',
        "status_values:",
        "  unsupported: No mapped Cauce equivalent.",
        "  partial: Cauce covers a documented subset or uses different semantics.",
        "  implemented: Cauce implements the stated behavior without current verification.",
        "  verified: Cauce implements the stated standalone behavior with test evidence.",
        "defaults:", "  module_status: unsupported", "  directive_status: unsupported",
        f"module_count: {len(modules)}",
        f"directive_registrations: {sum(len(commands) for variants in modules.values() for _, _, commands in variants)}",
        "configure:",
        "  source: auto/options",
        f"  option_patterns_count: {len(configure_options)}",
        "  option_patterns:",
    ]
    lines.extend("    - " + json.dumps(option) for option in configure_options)
    lines.append("modules:")
    known = set()
    for name, variants in sorted(modules.items()):
        lines.append(f"  {name}:")
        module_key = name
        known.add(module_key)
        if module_key in coverage["mappings"]:
            lines.append("    coverage: " + json.dumps(coverage["mappings"][module_key]))
        lines.append("    definitions:")
        for source, kind, commands in variants:
            lines.extend((f"      - source: {source}", f"        type: {kind}"))
            if not commands:
                lines.append("        directives: []")
                continue
            lines.append("        directives:")
            for directive, flags in commands:
                key = f"{name}.{directive}"
                known.add(key)
                item = {"name": directive, "flags": flags}
                item.update(coverage["mappings"].get(key, {}))
                lines.append("          - " + json.dumps(item))
    unknown = coverage["mappings"].keys() - known
    assert not unknown, f"Unknown coverage keys: {sorted(unknown)}"
    lines.append("add_ons:")
    for name, add_on in add_ons.items():
        lines.append(f"  {name}:")
        for key in ("version", "commit", "sha256", "source"):
            lines.append(f"    {key}: " + json.dumps(add_on[key]))
        lines.append("    modules:")
        for module, record in sorted(add_on["modules"].items()):
            lines.extend((f"      {module}:", "        definitions:"))
            for variant in record["definitions"]:
                lines.extend((f'          - source: {variant["source"]}',
                              f'            type: {variant["type"]}', "            directives:"))
                lines.extend("              - " + json.dumps(row) for row in variant["directives"])
    lines.append("behaviors:")
    lines.extend("  - " + json.dumps(entry) for entry in coverage["behaviors"])
    lines.append("")
    return "\n".join(lines)


def read_reference(path, url):
    if path:
        return path.read_bytes()
    with urllib.request.urlopen(url, timeout=45) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarball", type=Path, help="Use a local copy of the pinned source")
    parser.add_argument("--njs-tarball", type=Path)
    parser.add_argument("--acme-tarball", type=Path)
    parser.add_argument("--check", action="store_true", help="Check committed output for drift")
    args = parser.parse_args()
    modules, configure_options = inventory(read_reference(args.tarball, URL))
    add_ons = {name: add_on_inventory(name, path)
               for name, path in (("njs", args.njs_tarball), ("nginx-acme", args.acme_tarball))}
    coverage = json.loads(OUTPUT.with_name("nginx-coverage.json").read_text())
    rendered = render(modules, configure_options, coverage, add_ons)
    if args.check:
        if OUTPUT.read_text() != rendered:
            parser.exit(1, "Registry drift. Regenerate compatibility/nginx-oss.yaml.\n")
    else:
        OUTPUT.write_text(rendered)
    addon_count = sum(len(variant["directives"]) for addon in add_ons.values()
                      for module in addon["modules"].values() for variant in module["definitions"])
    print(
        f"{len(modules)} modules, "
        f"{sum(len(cs) for variants in modules.values() for _, _, cs in variants)} directive registrations, "
        f"{len(configure_options)} configure option patterns, {addon_count} add-on directive registrations"
    )


if __name__ == "__main__":
    main()
