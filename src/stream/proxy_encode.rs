use super::proxy_protocol::{Addresses, SIGNATURE, V1_MAX};

pub(super) fn encode(
    version: super::config::ProxyVersion,
    addresses: Addresses,
) -> impl AsRef<[u8]> {
    use super::config::ProxyVersion;
    use bytes::{BufMut, BytesMut};
    use std::io::Write;
    use std::net::IpAddr;

    let mut buf = BytesMut::with_capacity(V1_MAX);
    let source = addresses.source;
    let destination = addresses.destination;
    match version {
        ProxyVersion::V1 => {
            let family = if source.is_ipv4() { 4 } else { 6 };
            write!(
                (&mut buf).writer(),
                "PROXY TCP{family} {} {} {} {}\r\n",
                source.ip(),
                destination.ip(),
                source.port(),
                destination.port()
            )
            .unwrap();
        }
        ProxyVersion::V2 => {
            buf.put_slice(SIGNATURE);
            buf.put_u8(0x21);
            match (source.ip(), destination.ip()) {
                (IpAddr::V4(src), IpAddr::V4(dst)) => {
                    buf.put_u8(0x11);
                    buf.put_u16(12);
                    buf.put_slice(&src.octets());
                    buf.put_slice(&dst.octets());
                }
                (IpAddr::V6(src), IpAddr::V6(dst)) => {
                    buf.put_u8(0x21);
                    buf.put_u16(36);
                    buf.put_slice(&src.octets());
                    buf.put_slice(&dst.octets());
                }
                _ => unreachable!("PROXY addresses have the same family"),
            }
            buf.put_u16(source.port());
            buf.put_u16(destination.port());
        }
    }
    buf
}
