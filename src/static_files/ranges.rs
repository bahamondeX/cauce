pub(crate) const MAX_RANGES: usize = 8;

pub(crate) enum Ranges {
    Ignore,
    Unsatisfiable,
    Parts(Vec<(u64, u64)>),
}
pub(crate) fn ranges(value: &str, length: u64) -> Ranges {
    let Some(value) = value.strip_prefix("bytes=") else {
        return Ranges::Ignore;
    };
    let mut ranges = Vec::new();
    let parse = |s: &str| {
        (!s.is_empty() && s.bytes().all(|byte| byte.is_ascii_digit()))
            .then(|| s.parse::<u64>().unwrap_or(u64::MAX))
    };
    for (index, item) in value.split(',').enumerate() {
        if index == MAX_RANGES {
            return Ranges::Ignore;
        }
        let Some((low, high)) = item.trim().split_once('-') else {
            return Ranges::Ignore;
        };
        let pair = if low.is_empty() {
            let Some(suffix) = parse(high) else {
                return Ranges::Ignore;
            };
            if length == 0 || suffix == 0 {
                continue;
            }
            (length.saturating_sub(suffix), length - 1)
        } else {
            let Some(start) = parse(low) else {
                return Ranges::Ignore;
            };
            let end = if high.is_empty() {
                u64::MAX
            } else if let Some(end) = parse(high) {
                end
            } else {
                return Ranges::Ignore;
            };
            if end < start {
                return Ranges::Ignore;
            }
            if start >= length {
                continue;
            }
            (start, end.min(length - 1))
        };
        // Overlapping ranges can amplify work. Treat them as an ordinary GET.
        if ranges
            .iter()
            .any(|&(first, last)| pair.0 <= last && first <= pair.1)
        {
            return Ranges::Ignore;
        }
        ranges.push(pair);
    }
    if ranges.is_empty() {
        Ranges::Unsatisfiable
    } else {
        Ranges::Parts(ranges)
    }
}
