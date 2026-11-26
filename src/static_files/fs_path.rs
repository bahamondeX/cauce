pub(crate) fn safe_part(name: &[u8]) -> bool {
    !name.is_empty()
        && name != b"."
        && name != b".."
        && name.len() <= 255
        && name
            .iter()
            .all(|byte| !matches!(*byte, 0..=31 | b'/' | b'\\' | 127))
}
pub(crate) fn decode_path(path: &str) -> Option<Vec<Vec<u8>>> {
    let mut parts = Vec::new();
    for component in path.split('/').filter(|s| !s.is_empty()) {
        if parts.len() == 32 {
            return None;
        }
        let mut bytes = Vec::with_capacity(component.len());
        let mut input = component.as_bytes().iter().copied();
        while let Some(byte) = input.next() {
            if byte == b'%' {
                let high = char::from(input.next()?).to_digit(16)?;
                let low = char::from(input.next()?).to_digit(16)?;
                bytes.push((high * 16 + low) as u8);
            } else {
                bytes.push(byte);
            }
        }
        if !safe_part(&bytes) {
            return None;
        }
        parts.push(bytes);
    }
    Some(parts)
}
