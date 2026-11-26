#!/usr/bin/env python3
"""Audit an NGINX config offline and emit a Cauce preview for a small HTTP subset."""

import argparse
from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1_048_576
TOKEN = re.compile(
    r"""(?P<space>\s+|\#[^\n]*)|(?P<quoted>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    r"""|(?P<punct>[{};])|(?P<word>(?:\\.|\$\{[A-Za-z0-9_]+\}|[^\s{};\#'"\\])+)""",
    re.S,
)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9-]*\Z")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
STRUCTURE = {("http",), ("events",), ("http", "server")}
VALUE_BLOCKS = {"charset_map", "geo", "map", "split_clients", "types"}


@dataclass
class Directive:
    name: str
    line: int
    args: list[str]
    children: list["Directive"] | None = field(default=None)


def parse(text):
    root = []
    blocks = [root]
    args = []
    line = start = 1
    offset = 0
    while offset < len(text):
        token = TOKEN.match(text, offset)
        if token is None:
            raise ValueError(f"cannot tokenize configuration at line {line}")
        kind = token.lastgroup
        value = token.group()
        token_line = line
        line += value.count("\n")
        offset = token.end()
        if kind == "space":
            continue
        if kind != "punct":
            if not args:
                start = token_line
            args.append(value[1:-1] if kind == "quoted" else value)
            continue
        if value == "}":
            if len(blocks) == 1 or args:
                raise ValueError(f"unexpected closing brace at line {token_line}")
            blocks.pop()
            continue
        if not args:
            raise ValueError(f"expected a directive at line {token_line}")
        children = [] if value == "{" else None
        blocks[-1].append(Directive(args[0], start, args[1:], children))
        if children is not None:
            if len(blocks) >= 64:
                raise ValueError(f"nesting exceeds limit at line {token_line}")
            blocks.append(children)
        args = []
    if args or len(blocks) != 1:
        raise ValueError(f"incomplete directive or block at line {line}")
    return root


def inventory(matrix):
    by_name = {}
    collections = [matrix["modules"]]
    collections.extend(add_on["modules"] for add_on in matrix.get("add_ons", {}).values())
    for modules in collections:
        for module, record in modules.items():
            for definition in record["definitions"]:
                for directive in definition["directives"]:
                    by_name.setdefault(directive["name"], {})[module] = directive
    return by_name


def audit(directives, registry, prefix=()):
    for node in directives:
        # nginx's types and geo blocks contain value rows. Keep arbitrary data and
        # directive arguments out of the diagnostics.
        is_value = VALUE_BLOCKS.intersection(prefix) or not IDENTIFIER.fullmatch(node.name)
        name = "<value>" if is_value else node.name
        path = (*prefix, name)
        options = registry.get(name, {})
        variants = [
            {"module": module, "status": row.get("status", "unsupported"),
             **({"notes": row["notes"]} if "notes" in row else {})}
            for module, row in sorted(options.items())
            if "within" not in row or row["within"] in prefix
        ]
        status = "structure" if path in STRUCTURE else "unsupported"
        scope = next((level for level in path[:-1] if level in {"http", "stream", "mail"}), "main")
        relevant = [
            variant for variant in variants
            if variant["module"].startswith(f"ngx_{scope}_")
        ]
        if any(variant["status"] == "partial" for variant in relevant):
            status = "manual_review"
        row = {"directive": name, "line": node.line, "context": list(prefix),
               "status": status, "reference_variants": variants}
        if name == "include":
            row["notes"] = "Analyze included files individually. The inventory does not expand them."
        yield row
        yield from audit(node.children or [], registry, path)


def one_value(items, name):
    matches = [node for node in items if node.name == name]
    if len(matches) != 1 or len(matches[0].args) != 1 or matches[0].children is not None:
        raise ValueError(f"preview requires one {name} with a single argument")
    return matches[0].args[0]


def ipv4_socket(value):
    host, separator, port = value.partition(":")
    if not separator or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError("preview requires explicit IPv4:port addresses")
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError("preview requires explicit IPv4:port addresses") from None
    return f"{address}:{int(port)}"


def server_names(items):
    directives = [node for node in items if node.name == "server_name"]
    if len(directives) != 1 or not directives[0].args or directives[0].children is not None:
        raise ValueError("preview requires one server_name directive with explicit DNS names")
    names = []
    for value in directives[0].args:
        name = value.lower()
        domain = name[2:] if name.startswith("*.") else name.removesuffix(".*")
        if not value.isascii() or len(domain) > 253 or not all(
            DNS_LABEL.fullmatch(label) for label in domain.split(".")
        ):
            raise ValueError("preview supports DNS names and complete leading or trailing wildcard labels")
        if name not in names:
            names.append(name)
    return names


def preview(directives):
    http = [d for d in directives if d.name == "http" and d.children is not None and not d.args]
    events = [d for d in directives if d.name == "events" and d.children == [] and not d.args]
    if len(http) != 1 or len(events) > 1 or len(http) + len(events) != len(directives):
        raise ValueError("preview supports one HTTP block and an optional empty events block")
    servers = http[0].children
    if len(servers) != 1 or servers[0].name != "server" or servers[0].args or servers[0].children is None:
        raise ValueError("preview supports one HTTP server block")
    items = servers[0].children
    listener = ipv4_socket(one_value(items, "listen"))
    hosts = server_names(items)
    locations = [node for node in items if node.name == "location"]
    if not locations or len(locations) + 2 != len(items):
        raise ValueError("preview supports only listen, server_name, and location in the server")
    config = yaml.safe_load((ROOT / "examples/cauce.yaml").read_text())
    proxy = config["proxy"]
    if listener == proxy["admin"]:
        raise ValueError("preview listener conflicts with the default admin socket")
    proxy.update(listen=listener, routes=[], upstreams=[])
    endpoints = {}
    paths = set()
    for location in locations:
        exact = len(location.args) == 2 and location.args[0] == "="
        if (location.children is None or len(location.children) != 1 or len(location.args) != (2 if exact else 1)
                or not re.fullmatch(r"/[A-Za-z0-9_~./-]{0,255}", location.args[-1])):
            raise ValueError("preview requires a literal prefix or exact location with a single proxy_pass")
        path = location.args[-1]
        key = (path, exact)
        if key in paths or "//" in path or any(p in {".", ".."} for p in path.split("/")):
            raise ValueError("preview requires unique and unambiguous paths")
        paths.add(key)
        target = one_value(location.children, "proxy_pass")
        if not target.startswith("http://"):
            raise ValueError("preview requires clear HTTP IPv4 upstreams")
        address = ipv4_socket(target.removeprefix("http://"))
        if address not in endpoints:
            endpoint = f"origin{len(endpoints) + 1}"
            endpoints[address] = endpoint
            proxy["upstreams"].append(
                dict(id=endpoint, address=address, host=address, isolation="migration",
                     connections=32, idle_connections=16, failure_backoff_ms="500ms")
            )
        for host in hosts:
            for authority in (host, f"{host}:{listener.rsplit(':', 1)[1]}"):
                if len(proxy["routes"]) == 1024:
                    raise ValueError("preview exceeds Cauce route capacity")
                row = dict(id=f"route{len(proxy['routes']) + 1}", host=authority, path_prefix=path,
                           balance="round_robin", upstreams=[endpoints[address]])
                if exact:
                    row["path_match"] = "exact"
                proxy["routes"].append(row)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--preview", type=Path, help="Write a new review-only Cauce YAML file for the supported subset")
    args = parser.parse_args()
    with args.config.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    try:
        if len(data) > MAX_BYTES:
            raise ValueError("configuration exceeds 1 MiB")
        directives = parse(data.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        diagnostic = "invalid UTF-8" if isinstance(error, UnicodeError) else str(error)
        print(json.dumps({"status": "invalid", "diagnostic": diagnostic}, indent=2))
        return 2
    matrix = yaml.safe_load((ROOT / "compatibility/nginx-oss.yaml").read_text())
    report = {"nginx_reference": matrix["reference"]["version"],
              "directives": list(audit(directives, inventory(matrix))),
              "semantic_differences": [
                  "Cauce matches literal paths or segment-bounded prefixes. NGINX normalizes URIs and supports additional location modes.",
                  "The preview matches explicit server names with the listener port or no port. NGINX also has default-server behavior.",
                  "The preview supplies Cauce defaults for budgets, logging and timeouts. Uploads and responses stream.",
                  "Forwarded identity and upstream HTTP Host follow Cauce policy. The preview uses explicit local upstream addresses.",
                  "Review socket exposure and the loopback admin listener before deployment.",
              ]}
    if args.preview:
        try:
            config = preview(directives)
            with args.preview.open("x", encoding="utf-8") as output:
                yaml.safe_dump(config, output, sort_keys=False)
            report["preview"] = {"status": "generated_for_review", "confidence": "manual_review",
                                 "validation": ["cauce", "check", str(args.preview)]}
        except (ValueError, FileExistsError) as error:
            reason = "output path already exists" if isinstance(error, FileExistsError) else str(error)
            report["preview"] = {"status": "unavailable", "confidence": "unsupported_subset",
                                 "diagnostic": reason}
    print(json.dumps(report, indent=2))
    if args.preview and report["preview"]["status"] != "generated_for_review":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
