#![no_main]

#[path = "../../src/transport.rs"]
#[allow(dead_code)]
mod transport;

use libfuzzer_sys::fuzz_target;

fn frame(kind: u8, flags: u8, stream: u32, payload: &[u8], declared_len: usize) -> Vec<u8> {
    let len = declared_len.min(0x00ff_ffff);
    let mut out = vec![
        ((len >> 16) & 0xff) as u8,
        ((len >> 8) & 0xff) as u8,
        (len & 0xff) as u8,
        kind,
        flags,
    ];
    out.extend_from_slice(&(stream & 0x7fff_ffff).to_be_bytes());
    out.extend_from_slice(payload);
    out
}

fn generated(mode: u8, bytes: &[u8]) -> (Vec<u8>, (bool, bool)) {
    let payload = bytes.get(1..).unwrap_or_default();
    let stream = u32::from(bytes.get(1).copied().unwrap_or(1)).max(1);
    match mode % 8 {
        0 => (payload.to_vec(), (false, false)), // arbitrary malformed/raw framing
        1 => (frame(1, 4, stream, &[0x82], 1), (false, false)), // complete HEADERS
        2 => {
            let mut wire = frame(1, 0, stream, &[0x82], 1);
            wire.extend(frame(9, 4, stream, &[0x84], 1));
            (wire, (false, false))
        }
        3 => (frame(1, 0, stream, &[0x82], 1), (true, true)), // open block
        4 => (frame(0, 0, stream, payload, payload.len()), (false, false)), // DATA
        5 => (frame(1, 0, stream, &[0x82], 5), (true, true)), // truncated payload
        6 => (frame(0, 0, stream, &[], 0)[..6].to_vec(), (false, true)), // partial frame header
        _ => {
            let mut wire = frame(1, 0, stream, &[0x82], 1);
            wire.extend(frame(9, 4, stream.wrapping_add(1), &[0x84], 1));
            (wire, (true, true)) // wrong-stream CONTINUATION
        }
    }
}

fuzz_target!(|data: &[u8]| {
    if data.is_empty() {
        return;
    }
    let (mut wire, expected) = generated(data[0], data);
    // Use the ordinary raw input path for case zero; generated cases include
    // a selector prefix so their structures stay intact during mutation.
    if data[0] % 8 == 0 {
        wire.insert(0, 0);
    } else {
        wire.insert(0, data[0]);
    }
    let actual = transport::fuzz_observer_fragmentation(&wire);
    if data[0] % 8 != 0 {
        assert_eq!(actual, expected, "structured frame model disagreement");
    }
    for boundary in [0, 999, 1_000, 1_001] {
        transport::fuzz_observer_time_advance(boundary);
    }
    transport::fuzz_observer_time_advance(u16::from(data[0]) * 5);
});
