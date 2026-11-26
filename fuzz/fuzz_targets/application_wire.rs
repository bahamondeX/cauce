#![no_main]

#[allow(dead_code)]
#[path = "../../src/application/envelope.rs"]
mod envelope;
#[allow(dead_code)]
#[path = "../../src/application/fastcgi.rs"]
mod fastcgi;
#[path = "../../src/application/response.rs"]
mod response;

use libfuzzer_sys::fuzz_target;

fuzz_target!(|input: &[u8]| {
    if let Some(prefix) = input.get(..8).and_then(|bytes| bytes.try_into().ok()) {
        if let Some(record) = fastcgi::parse_header(prefix) {
            assert!(record.length <= usize::from(u16::MAX));
            assert!(record.padding <= usize::from(u8::MAX));
        }
        fastcgi::ended(prefix);
    }
    if let [one, two, low, high, ..] = input {
        let length = u16::from_le_bytes([*low, *high]);
        assert_eq!(
            envelope::uwsgi_prefix(length.into(), *one, *two),
            Some([*one, *low, *high, *two])
        );
        if let Some(prefix) = envelope::scgi_prefix(length.into()) {
            assert!((1..=32 * 1024).contains(&length));
            assert!(prefix.ends_with(':'));
        }
    }
    if let Some(end) = response::header_end(input, 0) {
        let _ = response::response(&input[..end], 64);
    }
});
