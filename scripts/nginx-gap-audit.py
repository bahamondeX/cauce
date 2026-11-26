#!/usr/bin/env python3
"""Export every pinned NGINX registration into a reviewable JSONL backlog."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import runpy

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = runpy.run_path(str(ROOT / "scripts/nginx-inventory.py"))
OUTPUT = ROOT / "compatibility/nginx-gap-backlog.jsonl"

# These are bounded preview translations. Scope and argument restrictions
# continue to be enforced by nginx-migration-report.py before emitting YAML.
TRANSLATIONS = {
    "ngx_http_core_module": {"listen", "server", "server_name", "location", "root", "alias",
                             "try_files"},
    "ngx_http_index_module": {"index"},
    "ngx_http_proxy_module": {"proxy_pass", "proxy_set_header", "proxy_hide_header"},
    "ngx_http_realip_module": {"set_real_ip_from", "real_ip_header", "real_ip_recursive"},
    "ngx_stream_core_module": {"listen", "server"},
    "ngx_stream_proxy_module": {"proxy_pass", "proxy_connect_timeout", "proxy_timeout",
                               "proxy_half_close", "proxy_buffer_size"},
    "ngx_stream_upstream_module": {"upstream", "server"},
}

# Track behavior-level evidence independently of whole directive equivalence.
EVIDENCE = {
    "routing": ("ngx_http_core_module", "listen server server_name location"),
    "headers": ("ngx_http_proxy_module", "proxy_set_header proxy_hide_header"),
    "content": ("ngx_http_core_module", "root alias try_files"),
    "dns": ("ngx_http_core_module", "resolver resolver_timeout"),
    "stream-dns": ("ngx_stream_core_module", "resolver resolver_timeout"),
    "stream-tls": ("ngx_stream_ssl_module", "ssl_certificate ssl_certificate_key ssl_client_certificate ssl_verify_client"),
    "stream-backend-tls": ("ngx_stream_proxy_module", "proxy_ssl proxy_ssl_name proxy_ssl_server_name proxy_ssl_verify proxy_ssl_trusted_certificate proxy_ssl_certificate proxy_ssl_certificate_key"),
    "cache": ("ngx_http_proxy_module", "proxy_cache proxy_cache_background_update proxy_cache_revalidate"),
    "disk-cache": ("ngx_http_proxy_module", "proxy_cache_path"),
    "nested-locations": ("ngx_http_core_module", "location"),
    "header-inheritance": ("ngx_http_headers_filter_module", "add_header add_header_inherit"),
}
FILES = {
    "routing": ("shared cases", "bench/program/evidence/milestone-4/nginx-differential.jsonl"),
    "headers": ("shared cases", "bench/program/evidence/milestone-4/nginx-differential.jsonl"),
    "content": ("shared cases", "bench/program/evidence/milestone-6/nginx-content.jsonl"),
    "dns": ("Cauce fixture", "bench/program/evidence/milestone-9/9a-dns-sockets.log"),
    "stream-dns": ("Cauce fixture", "bench/program/evidence/milestone-9/9a-dns-sockets.log"),
    "stream-tls": ("shared cases", "bench/program/evidence/milestone-9/stream-tls-sockets.log"),
    "stream-backend-tls": ("shared cases", "bench/program/evidence/milestone-9/stream-tls-sockets.log"),
    "cache": ("Cauce fixture", "bench/program/evidence/milestone-9/swr-sockets.log"),
    "disk-cache": ("Cauce fixture", "bench/program/evidence/milestone-9/disk-sockets.log"),
    "nested-locations": ("shared cases", "bench/program/evidence/milestone-10/differential-nested.json"),
    "header-inheritance": ("shared cases", "bench/program/evidence/milestone-10/differential-nested.json"),
}

REVIEWED = {
    ("ngx_http_core_module", "location"): {
        "arguments": ["optional =, ^~, ~ or ~* modifier", "URI prefix, regex or named location"],
        "default": "The server's root location configuration is used when no location matches.",
        "inheritance": "Location matching selects a scope. Individual modules merge configuration through their own callbacks.",
        "source_functions": ["ngx_http_core_location", "ngx_http_core_find_location"],
    },
    ("ngx_http_headers_filter_module", "add_header"): {
        "arguments": ["field", "value (literal or expression)", "optional always"],
        "default": "No additional field.",
        "inheritance": "Inherit the parent collection without local entries when add_header_inherit is on. The merge and off modes change this behavior.",
        "source_functions": ["ngx_http_headers_filter", "ngx_http_headers_merge_conf"],
    },
    ("ngx_http_headers_filter_module", "add_header_inherit"): {
        "arguments": ["on, off or merge"], "default": "on",
        "inheritance": "An unset mode inherits the nearest parent mode.",
        "source_functions": ["ngx_http_headers_merge_conf"],
    },
    ("ngx_http_proxy_module", "proxy_set_header"): {
        "arguments": ["field", "value (literal or variable expression)"],
        "default": ["Host $proxy_internal_host", "Connection empty", "Content-Length $proxy_internal_body_length",
                    "Transfer-Encoding $proxy_internal_chunked", "TE empty", "Keep-Alive empty", "Expect empty",
                    "Upgrade empty"],
        "inheritance": "Inherit the parent collection only when this context declares no proxy_set_header entries. Implicit proxy headers are added during initialization.",
        "source_functions": ["ngx_http_proxy_merge_loc_conf", "ngx_http_proxy_init_headers"],
    },
    ("ngx_http_proxy_module", "proxy_hide_header"): {
        "arguments": ["field"],
        "default": ["Date", "Server", "X-Pad", "X-Accel-Expires", "X-Accel-Redirect", "X-Accel-Limit-Rate",
                    "X-Accel-Buffering", "X-Accel-Charset"],
        "inheritance": "An undeclared custom hide list inherits from the parent. The effective hide set also incorporates built-in defaults and proxy_pass_header exceptions.",
        "source_functions": ["ngx_http_proxy_merge_loc_conf", "ngx_http_upstream_hide_headers_hash"],
    },
}

PLATFORMS = {
    "ngx_epoll_module": "Linux epoll",
    "ngx_kqueue_module": "BSD and Darwin kqueue",
    "ngx_eventport_module": "illumos/Solaris event ports",
    "ngx_devpoll_module": "illumos/Solaris /dev/poll",
    "ngx_iocp_module": "Windows IOCP",
    "ngx_quic_bpf_module": "Linux eBPF",
    "ngx_win32_select_module": "Windows select",
    "ngx_win32_poll_module": "Windows WSAPoll",
}
LIFECYCLE = {
    "--build": "Cargo package metadata and build artifact provenance",
    "--builddir": "Cargo --target-dir",
    "--conf-path": "Cauce CLI configuration path",
    "--crossbuild": "Cargo target triples and the checksummed multiarch build runner",
    "--error-log-path": "Process stderr and service-manager redirection",
    "--group": "Operating-system service or container identity",
    "--help": "Cargo or build tool help",
    "--http-log-path": "proxy.logs.path",
    "--prefix": "Installation packaging and working directory",
    "--sbin-path": "Installation packaging",
    "--user": "Operating-system service or container identity",
    "--with-cc": "Target linker and native build CC configuration",
    "--with-cc-opt": "Native build CFLAGS and rustc target flags",
    "--with-cpu-opt": "rustc target-cpu and target-feature",
    "--with-ld-opt": "Target linker flags",
    "--with-threads": "Tokio workers and bounded blocking operations",
    "--with-ipv6": "OS IPv6 sockets and dual-stack configuration",
    "--with-debug": "Cargo development profile, RUST_LOG-independent diagnostics",
}
PLATFORM_FLAGS = {
    "--test-build-devpoll": "illumos/Solaris",
    "--test-build-epoll": "Linux",
    "--test-build-eventport": "illumos/Solaris",
    "--test-build-solaris-sendfilev": "illumos/Solaris",
    "--with-select_module": "platform-specific select implementation",
    "--without-select_module": "platform-specific select implementation",
    "--with-poll_module": "platform-specific poll implementation",
    "--without-poll_module": "platform-specific poll implementation",
    "--without-quic_bpf_module": "Linux eBPF",
}
SUBSTITUTE = {
    "--with-openssl": "rustls with ring",
    "--with-openssl-opt": "rustls compile and runtime TLS policy",
    "--with-pcre": "bounded Rust regex subset",
    "--with-pcre-jit": "bounded Rust regex without PCRE JIT",
    "--with-pcre-opt": "bounded Rust regex subset",
    "--without-pcre": "Regex evaluation is part of routing",
    "--without-pcre2": "Regex evaluation is part of routing",
    "--with-zlib": "async-compression and zlib-rs",
    "--with-zlib-opt": "Cargo compressor dependency features",
    "--with-zlib-asm": "Compressor crate architecture selection",
}
MODULE_ALIASES = {
    "--with-mail": "ngx_mail_core_module",
    "--with-imap": "ngx_mail_imap_module",
    "--with-imap_ssl_module": "ngx_mail_ssl_module",
    "--with-stream": "ngx_stream_core_module",
    "--without-http-cache": "ngx_http_proxy_module",
    "--without-http": "ngx_http_core_module",
    "--without-http_upstream_sticky": "ngx_http_upstream_sticky_module",
}


def priority(module, directive=""):
    if directive in {"resolver", "resolver_timeout", "proxy_cache_path", "proxy_cache_background_update",
                     "proxy_next_upstream", "include"} or module in {"ngx_stream_ssl_module", "ngx_http_ssl_module"}:
        return "P0"
    if module.startswith(("ngx_http_core", "ngx_http_proxy", "ngx_http_upstream", "ngx_stream", "ngx_mail")):
        return "P1"
    return "P2"


def configure(option, modules, mappings):
    name = option.split("=", 1)[0]
    module = MODULE_ALIASES.get(name)
    if module is None:
        candidate = re.sub(r"^--(?:with|without)-", "ngx_", name)
        if candidate in modules:
            module = candidate
    if option.endswith("=dynamic") or name in {"--add-dynamic-module", "--add-module", "--with-compat", "--modules-path"}:
        category, equivalent = "module packaging", None
    elif name in PLATFORM_FLAGS:
        category, equivalent = "platform build", "Tokio and OS-specific runtime support" if "epoll" in name else None
    elif name in SUBSTITUTE:
        category, equivalent = "dependency selection", SUBSTITUTE[name]
    elif module:
        category, equivalent = "module selection", mappings.get(module, {}).get("notes")
    elif name in LIFECYCLE:
        category, equivalent = "build or deployment", LIFECYCLE[name]
    elif name.endswith("-temp-path"):
        category, equivalent = "storage layout", None
    elif name.startswith(("--with-md5", "--with-sha1", "--with-libatomic", "--with-perl")):
        category, equivalent = "dependency selection", None
    else:
        category, equivalent = "build or deployment", None
    return {
        "kind": "configure", "reference": "oss", "option": option, "source": "auto/options",
        "category": category, "module": module, "cauce_equivalent": equivalent,
        "status": "mapped" if equivalent else "unmapped",
        "migration": "deployment review", "platform": PLATFORM_FLAGS.get(name),
        "semantic_differences": "Build-time option. Runtime compatibility is tracked in module and directive rows.",
        "verified_equivalent": False, "evidence": [], "priority": priority(module or ""),
    }


def coverage(module, directive, mapping, reference):
    notes = mapping.get("notes")
    migrations = ("bounded preview" if reference == "oss" and directive in TRANSLATIONS.get(module, ())
                  else "manual" if notes else "unsupported")
    sources = [
        {"scope": FILES[key][0], "path": FILES[key][1]}
        for key, (name, directives) in EVIDENCE.items()
        if module == name and (directive is None or directive in directives.split())
        and (ROOT / FILES[key][1]).is_file()
    ]
    status = mapping.get("status", "unmapped")
    # Existing inventory uses "verified" for subsets. Never elevate the whole
    # registration on that basis.
    if status in {"verified", "implemented"}:
        status = "partial"
    return {
        "status": status, "cauce_equivalent": notes,
        "semantic_differences": notes or "No Cauce behavior mapping established.",
        "migration": migrations, "evidence": sources, "verified_equivalent": False,
        "platform": PLATFORMS.get(module),
        "priority": priority(module, directive or "") if reference == "oss" else "add-on",
    }


def accepted_arity(flags):
    counts = set()
    for flag in flags:
        if match := re.fullmatch(r"NGX_CONF_TAKE([1-7]+)", flag):
            counts.update(int(n) for n in match[1])
    if "NGX_CONF_NOARGS" in flags:
        counts.add(0)
    if "NGX_CONF_FLAG" in flags:
        counts.add(1)
    minimum = next((count for symbol, count in (("ANY", 0), ("1MORE", 1), ("2MORE", 2))
                    if "NGX_CONF_" + symbol in flags), None)
    return {"accepted_counts": sorted(counts), "variadic_minimum": minimum,
            "block": "NGX_CONF_BLOCK" in flags}


def semantic_rows(modules, mappings, reference, provenance, build_options):
    for module, definitions in sorted(modules.items()):
        yield {
            "kind": "module", "reference": reference, "module": module,
            "sources": [source for source, _, _ in definitions],
            **coverage(module, None, mappings.get(module, {}), reference),
        }
        for source, module_type, directives in definitions:
            for order, (directive, flags) in enumerate(directives):
                known = REVIEWED.get((module, directive), {}) if reference == "oss" else {}
                yield {
                    "kind": "directive", "reference": reference, "module": module,
                    "directive": directive, "definition_ordinal": order,
                    "source": source, "module_type": module_type,
                    "task_id": f"{reference}/{source}/{module}/{directive}/{order}",
                    "registration": provenance[(source, module, order)],
                    "accepted_arity": accepted_arity(flags),
                    "arguments": known.get("arguments"),
                    "reference_default": known.get("default"),
                    "reference_inheritance": known.get("inheritance"),
                    "reference_functions": known.get("source_functions", []),
                    "semantic_audit": "reviewed_subset" if known else "pending",
                    "build_options": build_options.get(module, []),
                    "context": [flag for flag in flags if flag.endswith("_CONF") or flag == "NGX_DIRECT_CONF"],
                    "parser_flags": flags,
                    **coverage(module, directive, mappings.get(f"{module}.{directive}", {}), reference),
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarball", type=Path)
    parser.add_argument("--njs-tarball", type=Path)
    parser.add_argument("--acme-tarball", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = INVENTORY["read_reference"](args.tarball, INVENTORY["URL"])
    provenance = {}
    modules, configure_options = INVENTORY["inventory"](data, provenance)
    mappings = json.loads((OUTPUT.parent / "nginx-coverage.json").read_text())["mappings"]
    options = [configure(option, modules, mappings) for option in configure_options]
    build_options = {}
    for option in options:
        if option["module"] is not None:
            build_options.setdefault(option["module"], []).append(option["option"])
    rows = list(semantic_rows(modules, mappings, "oss", provenance, build_options))
    rows.extend(options)
    for name, path in (("njs", args.njs_tarball), ("nginx-acme", args.acme_tarball)):
        provenance = {}
        addon = INVENTORY["add_on_inventory"](name, path, provenance)
        table = {module: [(item["source"], item["type"],
                           [(row["name"], row["flags"]) for row in item["directives"]])
                          for item in row["definitions"]]
                 for module, row in addon["modules"].items()}
        addon_mappings = {}
        if name == "nginx-acme":
            for module in table:
                addon_mappings[module] = {"status": "partial", "notes":
                    "Cauce manages HTTP-01 issuance and renewal with a shared HTTPS/QUIC resolver. The add-on's configuration and challenge variants require separate verification."}
        rows.extend(semantic_rows(table, addon_mappings, name, provenance, {}))
    counts = Counter((row["reference"], row["kind"]) for row in rows)
    assert counts == {("oss", "module"): 120, ("oss", "directive"): 754, ("oss", "configure"): 149,
                      ("njs", "module"): 4, ("njs", "directive"): 55,
                      ("nginx-acme", "module"): 1, ("nginx-acme", "directive"): 15}, counts
    rendered = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
    if args.check:
        if rendered != OUTPUT.read_text():
            parser.exit(1, "Compatibility backlog drift\n")
    else:
        OUTPUT.write_text(rendered)
    for key, value in sorted(Counter((r["reference"], r["kind"], r["status"]) for r in rows).items()):
        print(*key, value)
    print("sha256", sha256(rendered.encode()).hexdigest())


if __name__ == "__main__":
    main()
