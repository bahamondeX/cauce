//! Application request prefixes for native uWSGI and SCGI.
pub(super) fn uwsgi_prefix(length: usize, modifier1: u8, modifier2: u8) -> Option<[u8; 4]> {
    let [low, high] = u16::try_from(length).ok()?.to_le_bytes();
    Some([modifier1, low, high, modifier2])
}

pub(super) fn scgi_prefix(length: usize) -> Option<String> {
    (length != 0 && length <= 32 * 1024).then(|| format!("{length}:"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_prefix_and_netstring_bounds() {
        assert_eq!(uwsgi_prefix(1024, 5, 22), Some([5, 0, 4, 22]));
        assert!(uwsgi_prefix(65_536, 0, 0).is_none());
        assert_eq!(scgi_prefix(17).as_deref(), Some("17:"));
        assert!(scgi_prefix(0).is_none());
        assert!(scgi_prefix(32 * 1024 + 1).is_none());
    }
}
