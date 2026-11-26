#![no_main]

use libfuzzer_sys::fuzz_target;

#[path = "../../src/stream/proxy_protocol.rs"]
mod proxy_protocol;

fuzz_target!(|input: &[u8]| {
    if input.len() <= 4096 {
        let _ = proxy_protocol::parse_v1(input);
        let _ = proxy_protocol::parse_v2(input);
    }
});
