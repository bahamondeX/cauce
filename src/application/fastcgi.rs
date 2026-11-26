//! FastCGI v1 wire framing. One responder request is active per socket.
pub(super) const BEGIN: u8 = 1;
pub(super) const END: u8 = 3;
pub(super) const PARAMS: u8 = 4;
pub(super) const STDIN: u8 = 5;
pub(super) const STDOUT: u8 = 6;
pub(super) const STDERR: u8 = 7;

#[derive(Debug, PartialEq, Eq)]
pub(super) struct Record {
    pub kind: u8,
    pub length: usize,
    pub padding: usize,
}

pub(super) fn parse_header(bytes: [u8; 8]) -> Option<Record> {
    let [version, kind, hi, lo, length_hi, length_lo, padding, _] = bytes;
    if (version, hi, lo) != (1, 0, 1) || !matches!(kind, STDOUT | STDERR | END) {
        return None;
    }
    let length = usize::from(u16::from_be_bytes([length_hi, length_lo]));
    if kind == END && length != 8 {
        return None;
    }
    Some(Record {
        kind,
        length,
        padding: usize::from(padding),
    })
}

pub(super) fn header(kind: u8, len: u16) -> [u8; 8] {
    let [hi, lo] = len.to_be_bytes();
    [1, kind, 0, 1, hi, lo, 0, 0]
}

pub(super) fn encode_length(length: usize, encoded: &mut Vec<u8>) {
    if length < 128 {
        encoded.push(length as u8);
    } else {
        encoded.extend_from_slice(&((length as u32) | (1 << 31)).to_be_bytes());
    }
}

pub(super) fn ended(bytes: [u8; 8]) -> bool {
    // applicationStatus is diagnostic. protocolStatus must be REQUEST_COMPLETE.
    bytes[4] == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_direction_request_id_and_lengths() {
        assert_eq!(parse_header(header(STDOUT, 65_535)).unwrap().length, 65_535);
        assert!(parse_header(header(STDIN, 1)).is_none());
        assert!(parse_header(header(END, 7)).is_none());
        let mut malformed = header(STDOUT, 0);
        malformed[3] = 1 + 1;
        assert!(parse_header(malformed).is_none());
        let mut encoded = Vec::new();
        encode_length(127, &mut encoded);
        encode_length(128, &mut encoded);
        assert_eq!(encoded, [127, 128, 0, 0, 128]);
    }
}
