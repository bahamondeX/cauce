#![no_main]

#[path = "../../src/static_files/fs_path.rs"]
mod fs_path;
#[path = "../../src/static_files/ranges.rs"]
mod ranges;
#[path = "../../src/cache/selectors.rs"]
mod selectors;

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    if let Ok(input) = std::str::from_utf8(data) {
        if let Some(parts) = fs_path::decode_path(input) {
            assert!(parts.len() <= 32);
            assert!(parts.iter().all(|name| fs_path::safe_part(name)));
        }
    }
    if let Some((size, spec)) = data.split_at_checked(8) {
        let size = u64::from_le_bytes(size.try_into().unwrap());
        if let Ok(spec) = std::str::from_utf8(spec) {
            if let ranges::Ranges::Parts(pairs) = ranges::ranges(spec, size) {
                assert!(!pairs.is_empty() && pairs.len() <= ranges::MAX_RANGES);
                for (index, &(start, end)) in pairs.iter().enumerate() {
                    assert!(start <= end && end < size);
                    assert!(pairs[..index].iter().all(|&(a, b)| b < start || end < a));
                }
            }
        }
    }
    let mut key = Vec::new();
    if selectors::append(&mut key, Some(data), 8192).is_some() {
        assert_eq!(key.len(), 4 + data.len());
        assert_eq!(&key[4..], data);
        let length = key.len();
        if selectors::append(&mut key, None, 8192).is_some() {
            assert_eq!(&key[length..], &u32::MAX.to_le_bytes());
        } else {
            assert_eq!(key.len(), length);
        }
    }
});
