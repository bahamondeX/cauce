//! HAProxy PROXY v1/v2 TCP framing. Only the socket acceptor decides trust.
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

pub(super) const SIGNATURE: &[u8; 12] = b"\r\n\r\n\0\r\nQUIT\n";
pub(super) const V1_MAX: usize = 107;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct Addresses {
    pub source: SocketAddr,
    pub destination: SocketAddr,
}

#[derive(Debug)]
pub(super) struct Invalid;

fn port(text: &str) -> Result<u16, Invalid> {
    if text.is_empty()
        || !text.bytes().all(|b| b.is_ascii_digit())
        || (text.len() > 1 && text.starts_with('0'))
    {
        return Err(Invalid);
    }
    text.parse().map_err(|_| Invalid)
}

pub(super) fn parse_v1(header: &[u8]) -> Result<Option<Addresses>, Invalid> {
    if header.len() > V1_MAX {
        return Err(Invalid);
    }
    let line = header.strip_suffix(b"\r\n").ok_or(Invalid)?;
    if !line.iter().all(|b| (32..=126).contains(b)) {
        return Err(Invalid);
    }
    let text = std::str::from_utf8(line)
        .map_err(|_| Invalid)?
        .strip_prefix("PROXY ")
        .ok_or(Invalid)?;
    if text == "UNKNOWN" || text.starts_with("UNKNOWN ") {
        return Ok(None);
    }
    let mut fields = text.split(' ');
    let ipv4 = match fields.next() {
        Some("TCP4") => true,
        Some("TCP6") => false,
        _ => return Err(Invalid),
    };
    let source: IpAddr = fields.next().ok_or(Invalid)?.parse().map_err(|_| Invalid)?;
    let destination: IpAddr = fields.next().ok_or(Invalid)?.parse().map_err(|_| Invalid)?;
    let source = SocketAddr::new(source, port(fields.next().ok_or(Invalid)?)?);
    let destination = SocketAddr::new(destination, port(fields.next().ok_or(Invalid)?)?);
    if fields.next().is_some() || source.is_ipv4() != ipv4 || destination.is_ipv4() != ipv4 {
        return Err(Invalid);
    }
    Ok(Some(Addresses {
        source,
        destination,
    }))
}

pub(super) fn v2_length(prefix: &[u8]) -> Result<usize, Invalid> {
    let [sig @ .., command, _, hi, lo]: &[u8; 16] = prefix.try_into().map_err(|_| Invalid)?;
    if sig != SIGNATURE || !matches!(command, 0x20 | 0x21) {
        return Err(Invalid);
    }
    Ok(u16::from_be_bytes([*hi, *lo]) as usize)
}

pub(super) fn parse_v2(header: &[u8]) -> Result<Option<Addresses>, Invalid> {
    let (prefix, payload) = header.split_at_checked(16).ok_or(Invalid)?;
    if v2_length(prefix)? != payload.len() {
        return Err(Invalid);
    }
    // LOCAL ignores the address family, transport, and entire payload.
    if prefix[12] == 0x20 {
        return Ok(None);
    }
    let (addresses, tlvs) = match prefix[13] {
        0x00 => (None, payload),
        0x11 => {
            let (addresses, tlvs) = payload.split_at_checked(12).ok_or(Invalid)?;
            let [a, b, c, d, e, f, g, h, sp0, sp1, dp0, dp1] = addresses.try_into().unwrap();
            (
                Some(Addresses {
                    source: (Ipv4Addr::new(a, b, c, d), u16::from_be_bytes([sp0, sp1])).into(),
                    destination: (Ipv4Addr::new(e, f, g, h), u16::from_be_bytes([dp0, dp1])).into(),
                }),
                tlvs,
            )
        }
        0x21 => {
            let (addresses, tlvs) = payload.split_at_checked(36).ok_or(Invalid)?;
            let ip = |range: std::ops::Range<usize>| {
                Ipv6Addr::from(<[u8; 16]>::try_from(&addresses[range]).unwrap())
            };
            let port = |offset| u16::from_be_bytes([addresses[offset], addresses[offset + 1]]);
            (
                Some(Addresses {
                    source: (ip(0..16), port(32)).into(),
                    destination: (ip(16..32), port(34)).into(),
                }),
                tlvs,
            )
        }
        _ => return Err(Invalid),
    };
    validate_tlvs(header, tlvs)?;
    Ok(addresses)
}

fn take_tlv(input: &[u8]) -> Result<(u8, &[u8], &[u8]), Invalid> {
    let [kind, hi, lo, data @ ..] = input else {
        return Err(Invalid);
    };
    let (value, rest) = data
        .split_at_checked(u16::from_be_bytes([*hi, *lo]) as usize)
        .ok_or(Invalid)?;
    Ok((*kind, value, rest))
}

fn validate_tlvs(header: &[u8], mut tlvs: &[u8]) -> Result<(), Invalid> {
    let mut checksummed = false;
    while !tlvs.is_empty() {
        let offset = header.len() - tlvs.len() + 3;
        let (kind, value, rest) = take_tlv(tlvs)?;
        match kind {
            0x03 => {
                let expected = u32::from_be_bytes(value.try_into().map_err(|_| Invalid)?);
                if checksummed || crc32c(header, offset) != expected {
                    return Err(Invalid);
                }
                checksummed = true;
            }
            0x05 if value.len() > 128 => return Err(Invalid),
            0x20 => {
                let mut sub = value.get(5..).ok_or(Invalid)?;
                while !sub.is_empty() {
                    let (_, _, rest) = take_tlv(sub)?;
                    sub = rest;
                }
            }
            // Remaining types are opaque metadata, never an identity input.
            _ => {}
        }
        tlvs = rest;
    }
    Ok(())
}

fn crc32c(header: &[u8], checksum: usize) -> u32 {
    let mut crc = u32::MAX;
    for (index, byte) in header.iter().enumerate() {
        if !(checksum..checksum + 4).contains(&index) {
            crc ^= u32::from(*byte);
        }
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0x82f63b78 & 0u32.wrapping_sub(crc & 1));
        }
    }
    !crc
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validated_tlvs_and_checksum() {
        assert_eq!(crc32c(b"123456789", 100), 0xe3069283);
        let mut wire = SIGNATURE.to_vec();
        wire.extend_from_slice(&[
            0x21, 0x00, 0, 15, 0x20, 0, 5, 0, 0, 0, 0, 0, 3, 0, 4, 0, 0, 0, 0,
        ]);
        let checksum = crc32c(&wire, 27);
        wire[27..].copy_from_slice(&checksum.to_be_bytes());
        assert_eq!(parse_v2(&wire).unwrap(), None);
        wire[20] ^= 1;
        assert!(parse_v2(&wire).is_err());
        wire[18] = 4;
        assert!(parse_v2(&wire).is_err());
    }
}
