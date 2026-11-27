#!/usr/bin/env python3
"""Audit an NGINX config offline and emit a Cauce preview for a bounded HTTP and Stream subset."""

import argparse
from dataclasses import dataclass, field
import glob
import ipaddress
import itertools
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1_048_576
MAX_INCLUDE_FILES = 128
MAX_INCLUDE_DEPTH = 16
VARIABLE = re.compile(r"(?<!\\)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*|[0-9]+))")
TOKEN = re.compile(
    r"""(?P<space>\s+|\#[^\n]*)|(?P<quoted>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    r"""|(?P<punct>[{};])|(?P<word>(?:\\.|\$\{[A-Za-z0-9_]+\}|[^\s{};\#'"\\])+)""",
    re.S,
)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9-]*\Z")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
STRUCTURE = {("http",), ("events",), ("http", "server"), ("stream",), ("stream", "server"), ("mail",), ("mail", "server")}
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
    source: str | None = None


def parse(text, source=None):
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
        blocks[-1].append(Directive(args[0], start, args[1:], children, source))
        if children is not None:
            if len(blocks) >= 64:
                raise ValueError(f"nesting exceeds limit at line {token_line}")
            blocks.append(children)
        args = []
    if args or len(blocks) != 1:
        raise ValueError(f"incomplete directive or block at line {line}")
    return root


def expanded_config(path, include_root):
    root = include_root.resolve(strict=True) if include_root else None
    if root and not root.is_dir():
        raise ValueError("include root must be a directory")
    total_bytes = 0
    files = []
    includes = []
    active = set()

    def read(file):
        nonlocal total_bytes
        if file in active:
            raise ValueError("include cycle")
        if len(active) >= MAX_INCLUDE_DEPTH or len(files) >= MAX_INCLUDE_FILES:
            raise ValueError("configuration include depth or file budget exceeded")
        location = str(file.relative_to(root)) if root and file.is_relative_to(root) else file.name
        fd = os.open(file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("configuration input must be a regular file")
            data = stream.read(MAX_BYTES - total_bytes + 1)
        total_bytes += len(data)
        if total_bytes > MAX_BYTES:
            raise ValueError("configuration exceeds 1 MiB")
        files.append(location)
        active.add(file)
        try:
            items = parse(data.decode("utf-8"), source=location)
            return expand(items) if root else items
        except ValueError as error:
            raise ValueError(f"{location}: {error}") from None
        finally:
            active.remove(file)

    def expand(nodes):
        result = []
        for node in nodes:
            if node.name != "include":
                if node.children is not None:
                    node.children = expand(node.children)
                result.append(node)
                continue
            if len(node.args) != 1 or node.children is not None:
                raise ValueError(f"invalid include at line {node.line}")
            value = node.args[0]
            if "$" in value or "\\" in value or "**" in value or any(part == ".." for part in Path(value).parts):
                raise ValueError(f"unsupported include pattern at line {node.line}")
            pattern = Path(value)
            if pattern.is_absolute():
                if not pattern.is_relative_to(root):
                    raise ValueError(f"include outside root at line {node.line}")
            else:
                pattern = root / pattern
            matches = list(itertools.islice(glob.iglob(str(pattern)), MAX_INCLUDE_FILES + 1))
            if not matches and not glob.has_magic(value):
                raise ValueError(f"include file missing at line {node.line}")
            if len(matches) + len(files) > MAX_INCLUDE_FILES:
                raise ValueError("configuration include file budget exceeded")
            targets = []
            for entry in sorted(matches):
                included = Path(entry).resolve(strict=True)
                if not included.is_relative_to(root):
                    raise ValueError(f"include outside root at line {node.line}")
                targets.append(str(included.relative_to(root)))
                result.extend(read(included))
            includes.append({"source": node.source, "line": node.line, "files": targets})
        return result

    items = read(path.resolve(strict=True))
    return items, files, includes


def variable_uses(nodes):
    for node in nodes:
        names = sorted({match[1] or match[2] for value in node.args for match in VARIABLE.finditer(value)})
        if names:
            yield {"source": node.source, "line": node.line, "variables": names,
                   "status": "manual_review"}
        yield from variable_uses(node.children or [])


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
        row = {"directive": name, "source": node.source, "line": node.line, "context": list(prefix),
               "status": status, "reference_variants": variants}
        if name == "include":
            row["notes"] = "Provide --include-root to expand this include."
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


def http_preview(directives, route_mappings):
    http = [d for d in directives if d.name == "http" and d.children is not None and not d.args]
    events = [d for d in directives if d.name == "events" and d.children == [] and not d.args]
    if len(http) != 1 or len(events) > 1 or len(http) + len(events) != len(directives):
        raise ValueError("preview supports one HTTP block and an optional empty events block")
    inherited_names = {"set_real_ip_from", "real_ip_header", "real_ip_recursive"} | TLS_PREVIEW_DIRECTIVES
    inherited = [node for node in http[0].children if node.name in inherited_names]
    servers = [node for node in http[0].children if node.name not in inherited_names]
    if len(servers) != 1 or servers[0].name != "server" or servers[0].args or servers[0].children is None:
        raise ValueError("preview supports one HTTP server and literal inherited TLS or real-IP policy")
    local = {node.name for node in servers[0].children}
    items = [node for node in inherited if node.name not in local] + servers[0].children
    listener, tls = listen_preview(items)
    hosts = server_names(items)
    locations = [node for node in items if node.name == "location"]
    allowed = {"listen", "server_name", "location", "set_real_ip_from", "real_ip_header", "real_ip_recursive"} | TLS_PREVIEW_DIRECTIVES
    if not locations or any(item.name not in allowed for item in items):
        raise ValueError("preview supports listen, server_name, literal locations and real-IP or TLS policy")
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
    def translate(location, parents, parent_path, depth, inherited_headers, inherited_additions, inherit_mode):
        if depth > 16:
            raise ValueError("preview exceeds the Cauce location nesting limit")
        mode = None
        if len(location.args) == 1:
            mode = "literal_prefix"
        elif len(location.args) == 2:
            mode = {"=": "exact", "^~": "prefix_no_regex"}.get(location.args[0])
        if (mode is None or location.children is None
                or not re.fullmatch(r"/[A-Za-z0-9_~./-]{0,255}", location.args[-1])):
            raise ValueError(f"preview requires a literal location at line {location.line}")
        path = location.args[-1]
        key = (tuple(parents.values()), path, mode == "exact")
        if (key in paths or "//" in path or any(p in {".", ".."} for p in path.split("/"))
                or (parent_path is not None and not path.startswith(parent_path))):
            raise ValueError("preview requires unique and unambiguous paths")
        paths.add(key)
        children = [node for node in location.children if node.name == "location"]
        if children and mode == "exact":
            raise ValueError("exact locations cannot contain nested locations")
        private = [node for node in location.children if node.name == "internal"]
        if len(private) > 1 or any(node.args or node.children is not None for node in private):
            raise ValueError("internal must appear once without arguments in a location")
        directives = [node for node in location.children if node.name not in
                      {"location", "add_header", "add_header_inherit", "internal"}]
        mode_nodes = [node for node in location.children if node.name == "add_header_inherit"]
        if mode_nodes:
            inherit_mode = one_value(mode_nodes, "add_header_inherit")
            if inherit_mode not in {"on", "off"}:
                raise ValueError("literal response header preview supports inheritance on or off")
        additions = []
        for node in location.children:
            if node.name != "add_header":
                continue
            if node.children is not None or len(node.args) not in (2, 3) or node.args[2:] not in ([], ["always"]):
                raise ValueError("preview supports literal add_header fields with optional always")
            name, value = node.args[:2]
            name = name.lower()
            if (not HEADER_NAME.fullmatch(name) or name in RESERVED_HEADERS
                    or name.startswith(("x-forwarded-", "grpc-", "sec-websocket-"))
                    or "$" in value or any(not 0x20 <= ord(character) <= 0x7e for character in value)):
                raise ValueError("unsafe or dynamic response header addition requires manual mapping")
            additions.append(dict(name=name, value=value, always=len(node.args) == 3))
        if len(additions) > 16 or sum(len(row["name"]) + len(row["value"]) + 4 for row in additions) > 4096:
            raise ValueError("preview response header additions exceed the Cauce edit or byte budget")
        effective_additions = additions or (inherited_additions if inherit_mode == "on" else [])
        content = direct_preview(directives) or static_preview(directives, mode, path)
        action = None if content else redirect_preview(directives)
        if action is not None and effective_additions:
            raise ValueError("response header additions on redirect actions require manual mapping")
        policy = None
        upstreams = []
        if action is None and content is None:
            if any(node.name in {"fastcgi_pass", "uwsgi_pass", "scgi_pass"} for node in directives):
                raise ValueError("application gateway preview requires manual endpoint and parameter mapping")
            target = one_value(directives, "proxy_pass")
            policy = header_preview(directives)
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
        effective_headers = {**inherited_headers, **(policy or {})}
        current = {}
        for host in hosts:
            for authority in (host, f"{host}:{listener.rsplit(':', 1)[1]}"):
                if len(proxy["routes"]) == 1024:
                    raise ValueError("preview exceeds Cauce route capacity")
                row = dict(id=f"route{len(proxy['routes']) + 1}", host=authority, path_prefix=path,
                           path_match=mode, balance="round_robin", upstreams=upstreams)
                if authority in parents:
                    row["parent"] = parents[authority]
                if private:
                    row["internal"] = True
                if content:
                    row["content"] = content
                if action:
                    row["action"] = action
                if effective_headers:
                    row["headers"] = effective_headers if upstreams else {}
                if effective_additions or inherited_additions:
                    row["headers"] = {**row.get("headers", {}), "response_add": effective_additions}
                proxy["routes"].append(row)
                current[authority] = row["id"]
        route_mappings.append({"source": location.source, "line": location.line,
                               "target_routes": list(current.values())})
        for child in children:
            translate(child, current, path, depth + 1, effective_headers, effective_additions, inherit_mode)

    for location in locations:
        translate(location, {}, None, 0, {}, [], "on")
    return config


def stream_address(value):
    if value.startswith("["):
        match = re.fullmatch(r"\[([0-9a-fA-F:]+)\]:([0-9]{1,5})", value)
        if match and 1 <= int(match[2]) <= 65535:
            return f"[{ipaddress.IPv6Address(match[1])}]:{int(match[2])}"
        raise ValueError("preview requires an explicit IPv6 socket address")
    return ipv4_socket(value)


def stream_preview(block, proxy):
    if not 1 <= len(block.children) <= 64:
        raise ValueError("stream preview requires one to 64 server blocks")
    listeners = {"upstreams": [], "tcp": [], "udp": [], "tls_passthrough": []}
    for server in block.children:
        if server.name != "server" or server.args or server.children is None:
            raise ValueError("stream preview requires literal server blocks")
        entries = server.children
        found = [node for node in entries if node.name == "listen"]
        if len(found) != 1 or not found[0].args or found[0].children is not None:
            raise ValueError("stream preview requires one literal listen per server")
        address, *flags = found[0].args
        allowed_flags = {"udp", "proxy_protocol", "reuseport"}
        if set(flags) - allowed_flags or len(set(flags)) != len(flags):
            raise ValueError("unsupported stream listener option")
        listen = stream_address(address)
        is_udp = "udp" in flags
        options = {}
        if "reuseport" in flags:
            options["reuse_port"] = True
        target = one_value(entries, "proxy_pass")
        if target.startswith("unix:") and not is_udp:
            socket_path = target.removeprefix("unix:")
            if not re.fullmatch(r"/[a-zA-Z0-9/._-]{1,102}", socket_path):
                raise ValueError("stream preview requires an absolute Unix path")
            endpoint = {"unix": socket_path}
        else:
            endpoint = {"address": stream_address(target)}
        group = f"stream{len(listeners['upstreams']) + 1}"
        listeners["upstreams"].append({"id": group, "endpoints": [endpoint]})
        row = {"listen": listen, "upstream": group, "options": options}
        allowed = {"listen", "proxy_pass", "proxy_timeout"}
        if any(node.name == "proxy_timeout" for node in entries):
            options["idle_timeout"] = migration_duration(one_value(entries, "proxy_timeout"))
        if is_udp:
            allowed.add("proxy_responses")
            if "proxy_protocol" in flags or one_value(entries, "proxy_responses") != "1":
                raise ValueError("UDP preview requires the explicit one-response subset")
            destination = "udp"
        else:
            allowed.update({"proxy_connect_timeout", "proxy_half_close", "proxy_protocol",
                            "proxy_protocol_timeout", "set_real_ip_from", "ssl_preread", "preread_timeout"})
            if any(node.name == "proxy_connect_timeout" for node in entries):
                options["connect_timeout"] = migration_duration(one_value(entries, "proxy_connect_timeout"))
            if any(node.name == "proxy_half_close" for node in entries):
                if one_value(entries, "proxy_half_close") != "on":
                    raise ValueError("TCP preview requires half-close behavior enabled")
            if any(node.name == "proxy_protocol" for node in entries):
                if one_value(entries, "proxy_protocol") != "on":
                    raise ValueError("TCP preview supports explicit outbound PROXY v1")
                options["proxy_out"] = "v1"
            trust = [node for node in entries if node.name == "set_real_ip_from"]
            header_timeout = any(node.name == "proxy_protocol_timeout" for node in entries)
            if "proxy_protocol" in flags:
                if not 1 <= len(trust) <= 256 or any(node.children is not None or len(node.args) != 1 for node in trust):
                    raise ValueError("inbound PROXY preview requires explicit trusted IP networks")
                try:
                    networks = list(dict.fromkeys(str(ipaddress.ip_network(node.args[0], strict=False)) for node in trust))
                except ValueError:
                    raise ValueError("inbound PROXY preview requires literal IPv4 or IPv6 networks") from None
                options["proxy_in"] = {"trusted_peers": networks}
                if header_timeout:
                    options["proxy_in"]["header_timeout"] = migration_duration(one_value(entries, "proxy_protocol_timeout"))
            elif trust or header_timeout:
                raise ValueError("PROXY trust and header timeout require inbound PROXY")
            preread = any(node.name == "ssl_preread" for node in entries)
            preread_timeout = any(node.name == "preread_timeout" for node in entries)
            if preread:
                if one_value(entries, "ssl_preread") != "on":
                    raise ValueError("TLS preread preview supports explicit ssl_preread on")
                row.pop("upstream")
                row.update(default_upstream=group, routes=[])
                if preread_timeout:
                    row["preread_timeout"] = migration_duration(one_value(entries, "preread_timeout"))
            elif preread_timeout:
                raise ValueError("preread_timeout requires TLS preread")
            destination = "tls_passthrough" if preread else "tcp"
        if any(node.name not in allowed or node.children is not None for node in entries):
            raise ValueError("stream preview contains an unsupported directive")
        listeners[destination].append(row)
    proxy["stream"] = {key: rows for key, rows in listeners.items() if rows}


def migration_duration(value):
    match = re.fullmatch(r"([0-9]{1,8})(ms|s|m|h)?", value)
    if not match:
        raise ValueError("stream preview requires a positive duration")
    amount, unit = int(match[1]), match[2] or "s"
    if not 1 <= amount * {"ms":1, "s":1000, "m":60_000, "h":3_600_000}[unit] <= 86_400_000:
        raise ValueError("stream preview duration is out of range")
    return f"{amount}{unit}"


def preview(directives, route_mappings):
    if any(node.name == "mail" for node in directives):
        raise ValueError("mail preview requires a manually verified authentication service and backend allowlist")
    streams = [node for node in directives if node.name == "stream"]
    if not streams:
        return http_preview(directives, route_mappings)
    if len(streams) != 1 or streams[0].args or streams[0].children is None:
        raise ValueError("preview requires one Stream block")
    other = [node for node in directives if node is not streams[0]]
    if all(node.name == "events" and node.children == [] and not node.args for node in other) and len(other) <= 1:
        config = yaml.safe_load((ROOT / "examples/cauce.yaml").read_text())
        for key in ("listen", "upstreams", "routes"):
            config["proxy"].pop(key)
    else:
        config = http_preview(other, route_mappings)
    stream_preview(streams[0], config["proxy"])
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--preview", type=Path, help="Write a new review-only Cauce YAML file for the supported subset")
    parser.add_argument("--include-root", type=Path, help="Expand local includes rooted at the NGINX configuration prefix")
    parser.add_argument("--validate-bin", type=Path, help="Validate generated YAML with this Cauce binary before publishing")
    args = parser.parse_args()
    try:
        directives, files, includes = expanded_config(args.config, args.include_root)
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, UnicodeError):
            diagnostic = "invalid UTF-8"
        elif isinstance(error, OSError):
            diagnostic = "cannot read configuration input"
        else:
            diagnostic = str(error)
        print(json.dumps({"status": "invalid", "diagnostic": diagnostic}, indent=2))
        return 2
    matrix = yaml.safe_load((ROOT / "compatibility/nginx-oss.yaml").read_text())
    report = {"nginx_reference": matrix["reference"]["version"],
              "directives": list(audit(directives, inventory(matrix))),
              "files": files, "includes": includes, "variable_uses": list(variable_uses(directives)),
              "semantic_differences": [
                  "Cauce routes on the raw URI path. NGINX normalizes URIs. Regex and named locations require manual migration.",
                  "The preview matches explicit server names with the listener port or no port. NGINX also has default-server behavior.",
                  "The preview supplies Cauce defaults for budgets, logging and timeouts. Uploads and responses stream.",
                  "Application gateways use bounded fixed CGI mapping. Verify script paths, modifiers, request lengths and protocol-specific pooling manually.",
                  "Mail preview requires explicit TLS and allowlisted origins. Review auth_http escaping, supported authentication mechanisms and plaintext policy.",
                  "Stream previews use literal origins, TCP half-close, and UDP response credit. Review session limits and restart requirements.",
                  "Forwarded identity and upstream HTTP Host follow Cauce policy. The preview uses explicit local upstream addresses.",
                  "Static proxy header policies omit implicit hide rules and accept location-local settings.",
                  "Preview inheritance covers literal real-IP and TLS policy and ordinary proxy header rules across literal children. Other inherited settings require review.",
                  "Static file previews bind literal directories with symlinks and hidden files denied by default. Index handling stays inside the selected document root.",
                  "Local return redirects use origin-relative Location. Check NGINX absolute_redirect behavior.",
                  "TLS previews use one server certificate and rustls defaults for chain verification and resumption. Review SNI mappings and required client certificates.",
                  "Review socket exposure and the loopback admin listener before deployment.",
              ]}
    if args.preview:
        try:
            route_mappings = []
            config = preview(directives, route_mappings)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.preview.parent,
                                             prefix=".cauce-preview-", suffix=".yaml") as output:
                yaml.safe_dump(config, output, sort_keys=False)
                output.flush()
                if args.validate_bin:
                    validation = subprocess.run([str(args.validate_bin.resolve()), "check", output.name],
                                                capture_output=True, timeout=15, check=False)
                    if validation.returncode:
                        raise ValueError("generated configuration was rejected by cauce check")
                os.link(output.name, args.preview)
            proxy = config["proxy"]
            web = any(route.get("content", {}).get("type") == "static" for route in proxy.get("routes", []))
            stream = "stream" in proxy
            profile = "full" if web and stream else "network" if stream else "web" if web else "core"
            report["preview"] = {"status": "generated_for_review", "confidence": "manual_review",
                                 "minimum_distribution_profile": profile,
                                 "route_mappings": route_mappings,
                                 "validation": ["cauce", "check", str(args.preview)],
                                 "validated": args.validate_bin is not None}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            if isinstance(error, FileExistsError):
                reason = "output path already exists"
            elif isinstance(error, OSError):
                reason = "cannot write preview or run validation"
            elif isinstance(error, subprocess.TimeoutExpired):
                reason = "Cauce validation timed out"
            else:
                reason = str(error)
            report["preview"] = {"status": "unavailable", "confidence": "unsupported_subset",
                                 "diagnostic": reason}
    print(json.dumps(report, indent=2))
    if args.preview and report["preview"]["status"] != "generated_for_review":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
