# ADR 0010 · Flat locations and route headers

Cauce resolves a virtual host before it applies flat location precedence.
An exact path wins. The longest prefix marked `prefix_no_regex` suppresses
regex matching. Otherwise a matching regex takes precedence over the
longest applicable prefix. Explicit `literal_prefix` follows the NGINX
byte-prefix rule. The existing `prefix` mode keeps its segment boundary.

Regex policies use a bounded byte-regex engine and compile inside
configuration preparation. A rejected generation never replaces the
active index. The validated raw path remains the lookup input. Nested
location rules, PCRE-only syntax, URI normalization, named routes and
internal redirects require separate semantics.

Route header policies compile into typed names and values. They run after
hop filtering and preserve proxy-owned framing, identity, forwarding,
tracing, and protocol headers. Limits apply to configured edits and the
resulting header maps. Explicit replacement collapses prior values.
Configuration literals are marked sensitive for compression. The migration
preview accepts only static edits and writes its output with owner-only
permissions.

Both features are opt-in. The single existing prefix route retains direct
lookup. No regexes are evaluated for a host without compiled patterns.
[Configuration guide](../CONFIGURATION.md).
