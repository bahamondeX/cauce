#!/usr/bin/env python3
"""Audit an NGINX config offline and emit a Cauce preview for a small HTTP subset."""

import argparse
from dataclasses import dataclass, field
import ipaddress
import json
import os
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
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9a-z-]{1,64}\Z")
RESERVED_HEADERS = {
    "host", "connection", "keep-alive", "proxy-connection", "transfer-encoding",
    "content-length", "content-type", "content-encoding", "trailer", "te", "upgrade",
    "proxy-authenticate", "proxy-authorization", "forwarded", "x-real-ip", "x-request-id",
    "traceparent", "tracestate", "baggage",
}


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


def header_preview(nodes):
    policy = {}
    seen = {"request": set(), "response": set()}
    for node in nodes:
        if node.name == "proxy_pass":
            continue
        if (node.children is not None or
                (node.name, len(node.args)) not in {("proxy_set_header", 2), ("proxy_hide_header", 1)}):
            raise ValueError("preview supports proxy_pass and literal proxy header policies only")
        direction = "request" if node.name == "proxy_set_header" else "response"
        name = node.args[0].lower()
        if (not HEADER_NAME.fullmatch(name) or name in RESERVED_HEADERS
                or name.startswith(("x-forwarded-", "grpc-", "sec-websocket-"))
                or name in seen[direction] or len(seen[direction]) == 16):
            raise ValueError("preview header name is invalid, protected, duplicate, or over budget")
        seen[direction].add(name)
        value = node.args[1] if direction == "request" else ""
        if any(not 32 <= ord(byte) <= 126 or byte in "$\\" for byte in value):
            raise ValueError("preview requires unescaped static proxy header values")
        edits = policy.setdefault(direction, {})
        if value:
            edits.setdefault("set", {})[name] = value
        else:
            edits.setdefault("remove", []).append(name)
    if sum(len(key) + len(value) + 4 for key, value in
           policy.get("request", {}).get("set", {}).items()) > 4096:
        raise ValueError("preview request header values exceed 4 KiB")
    return policy


def real_ip_preview(items):
    names = {"set_real_ip_from", "real_ip_header", "real_ip_recursive"}
    declarations = [item for item in items if item.name in names]
    if not declarations:
        return None
    trusted = []
    for item in declarations:
        if item.children is not None or len(item.args) != 1:
            raise ValueError("preview requires literal real-IP settings")
        if item.name == "set_real_ip_from":
            try:
                network = str(ipaddress.ip_network(item.args[0], strict=False))
            except ValueError:
                raise ValueError("preview requires literal trusted IPv4 or IPv6 addresses") from None
            if network not in trusted:
                trusted.append(network)
    if not 1 <= len(trusted) <= 256:
        raise ValueError("preview requires 1 to 256 trusted networks")
    headers = [item.args[0].lower() for item in declarations if item.name == "real_ip_header"]
    recursive = [item.args[0] for item in declarations if item.name == "real_ip_recursive"]
    if len(headers) > 1 or (headers and headers[0] not in {"x-forwarded-for", "x-real-ip"}):
        raise ValueError("preview requires X-Forwarded-For or X-Real-IP")
    if len(recursive) > 1 or (recursive and recursive[0] not in {"on", "off"}):
        raise ValueError("preview requires real_ip_recursive on or off")
    return {"trusted_proxies":trusted, "header":headers[0].replace("-", "_") if headers else "x_real_ip",
            "recursive": recursive == ["on"], "forwarding":"replace"}


def direct_preview(nodes):
    if len(nodes) != 1 or nodes[0].name != "return":
        return None
    node = nodes[0]
    if (node.children is None and len(node.args) == 2
            and node.args[0] == "200"
            and re.fullmatch(r"[ -~]{0,4096}", node.args[1])
            and "$" not in node.args[1]):
        return {"type": "direct", "status": 200, "body": node.args[1], "content_type": "text/plain"}
    return None


def static_preview(nodes, location_mode, location_path):
    fields = {node.name for node in nodes}
    base = fields.intersection({"root", "alias"})
    if not base:
        return None
    if len(base) != 1 or not fields.issubset({"root", "alias", "index", "try_files"}):
        raise ValueError("static preview covers a literal root or alias, index and try_files ending in =404")
    field, = base
    path = one_value(nodes, field)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("static preview requires an absolute literal directory")
    if field == "alias" and (location_mode != "literal_prefix" or not location_path.endswith("/") or not path.endswith("/")):
        raise ValueError("alias preview requires a literal prefix and directory with trailing slashes")
    content = {"type": "static", field: path}
    index = [node for node in nodes if node.name == "index"]
    if index:
        if (len(index) != 1 or index[0].children is not None or not index[0].args
                or len(index[0].args) > 16
                or any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}", value) for value in index[0].args)):
            raise ValueError("static preview requires literal index filenames")
        content["index"] = index[0].args
    tries = [node for node in nodes if node.name == "try_files"]
    if tries:
        if (len(tries) != 1 or tries[0].children is not None
                or tries[0].args not in (["$uri", "=404"], ["$uri/", "=404"], ["$uri", "$uri/", "=404"])):
            raise ValueError("try_files preview supports only $uri and $uri/ ending in =404")
        content["try_files"] = tries[0].args[:-1]
    return content


def redirect_preview(nodes):
    if len(nodes) != 1 or nodes[0].name != "return":
        return None
    node = nodes[0]
    if node.children is not None or len(node.args) != 2 or node.args[0] not in {"301", "302", "303", "307", "308"}:
        raise ValueError("preview supports only literal redirect return statements")
    target = node.args[1]
    if (not re.fullmatch(r"/[A-Za-z0-9_~./?&=-]{0,255}", target) or target.startswith("//")
            or "//" in target.split("?",1)[0]
            or any(p in {".",".."} for p in target.split("?",1)[0].split("/"))):
        raise ValueError("preview requires a static origin-relative redirect")
    return dict(kind="redirect", status=int(node.args[0]), target=target, query="replace")


TLS_PREVIEW_DIRECTIVES = {
    "ssl_certificate", "ssl_certificate_key", "ssl_protocols",
    "ssl_client_certificate", "ssl_verify_client",
}


def listen_preview(items):
    listeners = [node for node in items if node.name == "listen"]
    if len(listeners) != 1 or listeners[0].children is not None:
        raise ValueError("preview requires one explicit listener")
    if not listeners[0].args:
        raise ValueError("preview requires one explicit listener address")
    address, *flags = listeners[0].args
    if flags not in ([], ["ssl"]):
        raise ValueError("preview supports only a plain or ssl IPv4 listener")
    has_tls = any(node.name in TLS_PREVIEW_DIRECTIVES for node in items)
    if not flags:
        if has_tls:
            raise ValueError("TLS preview requires listen with the ssl flag")
        return ipv4_socket(address), None

    def absolute_file(directive):
        path = one_value(items, directive)
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
            raise ValueError(f"preview requires an absolute literal {directive} file path")
        return path

    tls = {
        "cert_file": absolute_file("ssl_certificate"),
        "key_file": absolute_file("ssl_certificate_key"),
    }
    protocols = [node for node in items if node.name == "ssl_protocols"]
    if protocols:
        versions = {"TLSv1.2": "tls1.2", "TLSv1.3": "tls1.3"}
        node = protocols[0]
        if (len(protocols) != 1 or node.children is not None or not node.args
                or len(set(node.args)) != len(node.args)
                or any(value not in versions for value in node.args)):
            raise ValueError("preview supports only TLSv1.2 and TLSv1.3 protocol policies")
        tls["versions"] = [versions[version] for version in node.args]
    verify = one_value(items, "ssl_verify_client") if any(
        node.name == "ssl_verify_client" for node in items
    ) else "off"
    if verify == "on":
        tls["client_ca_file"] = absolute_file("ssl_client_certificate")
    elif verify != "off" or any(node.name == "ssl_client_certificate" for node in items):
        raise ValueError("client authentication preview requires ssl_verify_client on and an explicit trust bundle")
    return ipv4_socket(address), tls


def preview(directives):
    http = [d for d in directives if d.name == "http" and d.children is not None and not d.args]
    events = [d for d in directives if d.name == "events" and d.children == [] and not d.args]
    if len(http) != 1 or len(events) > 1 or len(http) + len(events) != len(directives):
        raise ValueError("preview supports one HTTP block and an optional empty events block")
    servers = http[0].children
    if len(servers) != 1 or servers[0].name != "server" or servers[0].args or servers[0].children is None:
        raise ValueError("preview supports one HTTP server block")
    items = servers[0].children
    listener, tls = listen_preview(items)
    hosts = server_names(items)
    locations = [node for node in items if node.name == "location"]
    allowed = {"listen", "server_name", "location", "set_real_ip_from", "real_ip_header", "real_ip_recursive"} | TLS_PREVIEW_DIRECTIVES
    if not locations or any(item.name not in allowed for item in items):
        raise ValueError("preview supports listen, server_name, flat locations and literal real-IP or TLS policy")
    config = yaml.safe_load((ROOT / "examples/cauce.yaml").read_text())
    proxy = config["proxy"]
    if listener == proxy["admin"]:
        raise ValueError("preview listener conflicts with the default admin socket")
    proxy.update(listen=listener, routes=[], upstreams=[])
    if tls is not None:
        proxy["tls"] = tls
    if identity := real_ip_preview(items):
        proxy["real_ip"] = identity
    endpoints = {}
    paths = set()
    for location in locations:
        mode = None
        if len(location.args) == 1:
            mode = "literal_prefix"
        elif len(location.args) == 2:
            mode = {"=": "exact", "^~": "prefix_no_regex"}.get(location.args[0])
        if (mode is None or location.children is None
                or not re.fullmatch(r"/[A-Za-z0-9_~./-]{0,255}", location.args[-1])):
            raise ValueError("preview requires a flat literal location")
        path = location.args[-1]
        key = (path, mode == "exact")
        if key in paths or "//" in path or any(p in {".", ".."} for p in path.split("/")):
            raise ValueError("preview requires unique and unambiguous paths")
        paths.add(key)
        content = direct_preview(location.children) or static_preview(location.children, mode, path)
        action = None if content else redirect_preview(location.children)
        policy = None
        upstreams = []
        if action is None and content is None:
            target = one_value(location.children, "proxy_pass")
            policy = header_preview(location.children)
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
            upstreams = [endpoints[address]]
        for host in hosts:
            for authority in (host, f"{host}:{listener.rsplit(':', 1)[1]}"):
                if len(proxy["routes"]) == 1024:
                    raise ValueError("preview exceeds Cauce route capacity")
                row = dict(id=f"route{len(proxy['routes']) + 1}", host=authority, path_prefix=path,
                           path_match=mode, balance="round_robin", upstreams=upstreams)
                if content:
                    row["content"] = content
                if action:
                    row["action"] = action
                if policy:
                    row["headers"] = policy
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
                  "Cauce routes on the raw URI path. NGINX normalizes URIs. Regex and named locations require manual migration.",
                  "The preview matches explicit server names with the listener port or no port. NGINX also has default-server behavior.",
                  "The preview supplies Cauce defaults for budgets, logging and timeouts. Uploads and responses stream.",
                  "Forwarded identity and upstream HTTP Host follow Cauce policy. The preview uses explicit local upstream addresses.",
                  "Static proxy header policies omit NGINX inheritance and implicit hide rules.",
                  "Static file previews bind literal directories with symlinks and hidden files denied by default. Index handling stays inside the selected document root.",
                  "Local return redirects use origin-relative Location. Check NGINX absolute_redirect behavior.",
                  "TLS previews use one server certificate and rustls defaults for chain verification and resumption. Review SNI mappings and required client certificates.",
                  "Review socket exposure and the loopback admin listener before deployment.",
              ]}
    if args.preview:
        try:
            config = preview(directives)
            fd = os.open(args.preview, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
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
