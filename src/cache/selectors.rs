//! Length-prefixed header values and explicit field boundaries for cache keys.
pub(crate) fn append(out: &mut Vec<u8>, value: Option<&[u8]>, budget: usize) -> Option<()> {
    let length = value.map_or(0, |bytes| bytes.len());
    let room = budget.checked_sub(out.len())?.checked_sub(4)?;
    if length > room || length >= u32::MAX as usize {
        return None;
    }
    if let Some(bytes) = value {
        out.extend_from_slice(&(length as u32).to_le_bytes());
        out.extend_from_slice(bytes);
    } else {
        out.extend_from_slice(&u32::MAX.to_le_bytes());
    }
    Some(())
}
