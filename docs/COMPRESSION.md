# Response compression

Add `compression` to a route to enable gzip and Brotli. Set
`algorithms` in server preference order for equal client quality
values. `min_size` accepts bytes or a size such as `1KiB`. `mime` accepts
specific content types and type wildcards. Sizes are taken from known
response lengths. A minimum of zero allows filtering unknown sizes.
Gzip and Brotli levels and `max_inflight` bound the encoder profile.
The response passes through a streaming encoder that yields between
bounded blocks. [Configuration example](../examples/content-and-cache.yaml).

The filter handles Accept-Encoding preferences and emits
Vary: Accept-Encoding when negotiation can change the representation.
Dynamic compression removes the identity Content-Length and
Accept-Ranges, and weakens strong validators. Precompressed static
serving uses the stored size of a fresh sibling `.gz` or `.br` file and
serves the original MIME type. Set `content.precompressed: true` on
the static route to enable this. A Range request uses the identity
representation and follows the static range rules.

The filter bypasses gRPC, upgrades, credential-bearing and cookie
requests, private/no-store/no-transform responses, existing encodings,
integrity headers and declared trailers. Unknown integrity trailers
cannot be applied to a transformed body. Other trailers are preserved
by the reader. Keep compression disabled on secret-bearing responses
that reflect attacker-controlled input even when credentials arrive
through a separate mechanism. Origin or intermediary cache hits
represent the upstream entity before Cauce's downstream encoding.
Zstandard and dedicated codec workers remain separate experiments.
