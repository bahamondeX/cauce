//! Resolve a client address at the explicit socket-peer trust boundary.
use hyper::header::{HeaderMap, HeaderValue, CONNECTION};
use ipnet::IpNet;
use serde::{Deserialize, Serialize};
use std::{
    fmt::Write,
    net::{IpAddr, SocketAddr},
};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(try_from = "Vec<String>", into = "Vec<String>")]
pub(crate) struct Networks(Vec<IpNet>);

impl TryFrom<Vec<String>> for Networks {
    type Error = &'static str;

    fn try_from(lines: Vec<String>) -> Result<Self, Self::Error> {
        if !(1..=256).contains(&lines.len()) {
            return Err("a network list requires 1 to 256 CIDRs");
        }
        let mut nets = Vec::with_capacity(lines.len());
        for line in lines {
            let net: IpNet = line.parse().map_err(|_| "invalid IP CIDR")?;
            let net = net.trunc();
            if nets.contains(&net) {
                return Err("duplicate IP CIDR");
            }
            nets.push(net);
        }
        Ok(Self(nets))
    }
}

impl From<Networks> for Vec<String> {
    fn from(networks: Networks) -> Self {
        networks.0.iter().map(ToString::to_string).collect()
    }
}

impl Networks {
    pub fn contains(&self, ip: IpAddr) -> bool {
        self.0.iter().any(|net| net.contains(&ip.to_canonical()))
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Source {
    #[default]
    XForwardedFor,
    XRealIp,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Forwarding {
    #[default]
    Replace,
    PreserveTrustedChain,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Policy {
    pub trusted_proxies: Networks,
    #[serde(default)]
    header: Source,
    #[serde(default)]
    recursive: bool,
    #[serde(default)]
    forwarding: Forwarding,
}

pub(crate) struct Identity {
    pub client: IpAddr,
    pub forwarded: Option<HeaderValue>,
}

fn address(token: &str) -> Option<IpAddr> {
    let token = token.trim_matches([' ', '\t']);
    token
        .parse::<IpAddr>()
        .ok()
        .or_else(|| token.parse::<SocketAddr>().ok().map(|socket| socket.ip()))
        .map(|ip| ip.to_canonical())
}

impl Policy {
    pub fn resolve(&self, peer: IpAddr, headers: &HeaderMap) -> Identity {
        let fallback = Identity {
            client: peer,
            forwarded: None,
        };
        if !self.trusted_proxies.contains(peer) {
            return fallback;
        }
        let header = match self.header {
            Source::XForwardedFor => "x-forwarded-for",
            Source::XRealIp => "x-real-ip",
        };
        // A hop-by-hop nomination cannot supply routing or admission identity.
        if headers.get_all(CONNECTION).iter().any(|line| {
            line.to_str().map_or(true, |line| {
                line.split(',')
                    .any(|name| name.trim().eq_ignore_ascii_case(header))
            })
        }) {
            return fallback;
        }
        let mut chain = [peer; 32];
        let mut count = 0;
        for line in headers.get_all(header) {
            let Ok(line) = line.to_str() else {
                return fallback;
            };
            for part in line.split(',') {
                if count == chain.len() || (count != 0 && self.header == Source::XRealIp) {
                    return fallback;
                }
                let Some(ip) = address(part) else {
                    return fallback;
                };
                chain[count] = ip;
                count += 1;
            }
        }
        if count == 0 {
            return fallback;
        }
        let mut client_index = count - 1;
        if self.recursive && self.header == Source::XForwardedFor {
            while client_index > 0 && self.trusted_proxies.contains(chain[client_index]) {
                client_index -= 1;
            }
        }
        let client = chain[client_index];
        let forwarded = match self.forwarding {
            Forwarding::Replace => client.to_string(),
            Forwarding::PreserveTrustedChain => {
                let mut value = String::new();
                for hop in &chain[client_index..count] {
                    write!(value, "{hop}, ").expect("string formatting");
                }
                write!(value, "{peer}").expect("string formatting");
                value
            }
        };
        Identity {
            client,
            forwarded: Some(HeaderValue::from_str(&forwarded).expect("formatted IP chain")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recursive_trust_stops_at_first_untrusted_address() {
        let mut policy = Policy {
            trusted_proxies: vec!["127.0.0.0/8".into(), "2001:db8:1::/48".into()]
                .try_into()
                .unwrap(),
            header: Source::XForwardedFor,
            recursive: true,
            forwarding: Forwarding::PreserveTrustedChain,
        };
        let mut headers = HeaderMap::new();
        headers.append(
            "x-forwarded-for",
            HeaderValue::from_static("192.0.2.3, 198.51.100.2"),
        );
        headers.append(
            "x-forwarded-for",
            HeaderValue::from_static("[2001:db8:1::2]:443"),
        );
        let peer = "127.0.0.1".parse().unwrap();
        let resolved = policy.resolve(peer, &headers);
        assert_eq!(resolved.client.to_string(), "198.51.100.2");
        assert_eq!(
            resolved.forwarded.unwrap(),
            "198.51.100.2, 2001:db8:1::2, 127.0.0.1"
        );
        assert!(policy
            .resolve("192.0.2.5".parse().unwrap(), &headers)
            .forwarded
            .is_none());
        policy.recursive = false;
        assert_eq!(
            policy.resolve(peer, &headers).client.to_string(),
            "2001:db8:1::2"
        );
        headers.insert(CONNECTION, HeaderValue::from_static("X-FORWARDED-FOR"));
        assert_eq!(policy.resolve(peer, &headers).client, peer);
        policy.header = Source::XRealIp;
        headers.insert("x-real-ip", HeaderValue::from_static("192.0.2.42"));
        assert_eq!(
            policy.resolve(peer, &headers).client.to_string(),
            "192.0.2.42"
        );
        headers.append("x-real-ip", HeaderValue::from_static("192.0.2.43"));
        assert_eq!(policy.resolve(peer, &headers).client, peer);
    }
}
