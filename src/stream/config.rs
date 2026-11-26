use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::{collections::HashSet, net::SocketAddr, path::PathBuf};

use crate::config::{identifier, milliseconds, Config};

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct StreamConfig {
    pub upstreams: Vec<Upstream>,
    pub tcp: Vec<Tcp>,
    pub tls_passthrough: Vec<Passthrough>,
    pub udp: Vec<Udp>,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(super) enum Balance {
    #[default]
    WeightedRoundRobin,
    ConsistentHash,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Upstream {
    pub id: String,
    #[serde(default)]
    pub(super) balance: Balance,
    pub endpoints: Vec<Endpoint>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Endpoint {
    pub address: Option<SocketAddr>,
    pub unix: Option<PathBuf>,
    #[serde(default = "weight")]
    pub weight: u16,
}

fn weight() -> u16 {
    1
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Tcp {
    pub listen: SocketAddr,
    pub upstream: String,
    #[serde(default)]
    pub options: TcpOptions,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Passthrough {
    pub listen: SocketAddr,
    pub default_upstream: Option<String>,
    pub routes: Vec<TlsRoute>,
    #[serde(default)]
    pub options: TcpOptions,
    #[serde(default = "preread_bytes")]
    pub preread_bytes: usize,
    #[serde(default = "header_ms", deserialize_with = "milliseconds")]
    pub preread_timeout: u64,
}

fn preread_bytes() -> usize {
    32 * 1024
}

fn header_ms() -> u64 {
    2_000
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct TlsRoute {
    pub sni: Option<String>,
    pub alpn: Option<String>,
    pub upstream: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct TcpOptions {
    #[serde(deserialize_with = "milliseconds")]
    pub connect_timeout: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub idle_timeout: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub session_timeout: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub failure_cooldown: u64,
    pub max_sessions: usize,
    pub max_sessions_per_ip: Option<usize>,
    pub buffer_bytes: usize,
    pub receive_buffer_bytes: Option<usize>,
    pub send_buffer_bytes: Option<usize>,
    pub nodelay: bool,
    pub keepalive_secs: Option<u64>,
    pub reuse_port: bool,
    pub proxy_in: Option<ProxyIn>,
    pub proxy_out: Option<ProxyVersion>,
}

impl Default for TcpOptions {
    fn default() -> Self {
        Self {
            connect_timeout: 5_000,
            idle_timeout: 60_000,
            session_timeout: 3_600_000,
            failure_cooldown: 1_000,
            max_sessions: 1_024,
            max_sessions_per_ip: None,
            buffer_bytes: 8_192,
            receive_buffer_bytes: None,
            send_buffer_bytes: None,
            nodelay: true,
            keepalive_secs: None,
            reuse_port: false,
            proxy_in: None,
            proxy_out: None,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProxyIn {
    pub trusted_peers: crate::identity::Networks,
    #[serde(default = "header_ms", deserialize_with = "milliseconds")]
    pub header_timeout: u64,
    #[serde(default = "proxy_bytes")]
    pub max_header_bytes: usize,
}

fn proxy_bytes() -> usize {
    512
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ProxyVersion {
    V1,
    V2,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct UdpOptions {
    #[serde(deserialize_with = "milliseconds")]
    pub idle_timeout: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub session_timeout: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub failure_cooldown: u64,
    pub max_sessions: usize,
    pub max_sessions_per_ip: usize,
    pub max_datagram_bytes: usize,
    pub max_response_ratio: u16,
    pub reuse_port: bool,
}

impl Default for UdpOptions {
    fn default() -> Self {
        Self {
            idle_timeout: 10_000,
            session_timeout: 60_000,
            failure_cooldown: 1_000,
            max_sessions: 1_024,
            max_sessions_per_ip: 64,
            max_datagram_bytes: 1_232,
            max_response_ratio: 3,
            reuse_port: false,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Udp {
    pub listen: SocketAddr,
    pub upstream: String,
    #[serde(default)]
    pub options: UdpOptions,
}

pub(super) const UDP_QUEUE: usize = 4;

fn valid_duration(ms: u64) -> bool {
    (1..=86_400_000).contains(&ms)
}

impl TcpOptions {
    fn validate(&self) -> Result<()> {
        ensure!(
            [
                self.connect_timeout,
                self.idle_timeout,
                self.session_timeout,
                self.failure_cooldown
            ]
            .into_iter()
            .all(valid_duration),
            "invalid TCP timeout"
        );
        ensure!(
            (1..=100_000).contains(&self.max_sessions),
            "invalid TCP admission limit"
        );
        ensure!(
            self.max_sessions_per_ip
                .is_none_or(|n| (1..=self.max_sessions).contains(&n)),
            "invalid TCP peer admission limit"
        );
        ensure!(
            (512..=65_536).contains(&self.buffer_bytes),
            "invalid TCP copy buffer size"
        );
        ensure!(
            [self.receive_buffer_bytes, self.send_buffer_bytes]
                .into_iter()
                .flatten()
                .all(|n| (1024..=4_194_304).contains(&n)),
            "invalid TCP socket buffer size"
        );
        ensure!(
            self.keepalive_secs
                .is_none_or(|n| (1..=86_400).contains(&n)),
            "invalid TCP keepalive"
        );
        if let Some(header) = &self.proxy_in {
            ensure!(
                (16..=4096).contains(&header.max_header_bytes)
                    && valid_duration(header.header_timeout),
                "invalid PROXY input limit"
            );
        }
        Ok(())
    }
}

impl StreamConfig {
    pub(crate) fn has_listeners(&self) -> bool {
        !self.tcp.is_empty() || !self.tls_passthrough.is_empty() || !self.udp.is_empty()
    }

    pub(crate) fn validate(&self, config: &Config) -> Result<()> {
        let count = self.tcp.len() + self.tls_passthrough.len() + self.udp.len();
        ensure!(
            count <= 64 && self.upstreams.len() <= 256,
            "too many stream listeners or upstreams"
        );
        let mut groups = std::collections::HashMap::new();
        for upstream in &self.upstreams {
            ensure!(
                identifier(&upstream.id) && (1..=64).contains(&upstream.endpoints.len()),
                "invalid stream upstream"
            );
            ensure!(
                groups.insert(upstream.id.as_str(), upstream).is_none(),
                "duplicate stream upstream"
            );
            let mut seen = HashSet::new();
            for endpoint in &upstream.endpoints {
                ensure!(
                    match (&endpoint.address, &endpoint.unix) {
                        (Some(addr), None) =>
                            addr.port() != 0
                                && !addr.ip().is_unspecified()
                                && !addr.ip().is_multicast(),
                        (None, Some(path)) =>
                            path.is_absolute()
                                && !path.as_os_str().as_encoded_bytes().contains(&0)
                                && path.as_os_str().len() <= 103,
                        _ => false,
                    } && (1..=100).contains(&endpoint.weight)
                        && seen.insert((&endpoint.address, &endpoint.unix)),
                    "invalid or duplicate stream endpoint"
                );
            }
        }
        let mut tcp_addrs = vec![config.admin];
        tcp_addrs.extend(config.listen);
        let mut udp_addrs = Vec::new();
        let mut memory = 0u64;
        let mut tcp = |address, options: &TcpOptions, preread: usize| -> Result<()> {
            add_address(&mut tcp_addrs, address)?;
            options.validate()?;
            memory += options.max_sessions as u64
                * (2 * options.buffer_bytes
                    + preread
                    + options.proxy_in.as_ref().map_or(0, |p| p.max_header_bytes))
                    as u64;
            Ok(())
        };
        for listener in &self.tcp {
            tcp(listener.listen, &listener.options, 0)?;
            ensure!(
                groups.contains_key(listener.upstream.as_str()),
                "unknown TCP upstream"
            );
        }
        for listener in &self.tls_passthrough {
            ensure!(
                (512..=65_536).contains(&listener.preread_bytes)
                    && valid_duration(listener.preread_timeout),
                "invalid TLS preread limit"
            );
            // Include the saved wire bytes and the incremental parser's copy.
            tcp(
                listener.listen,
                &listener.options,
                2 * listener.preread_bytes,
            )?;
            ensure!(
                listener.routes.len() <= 128
                    && (!listener.routes.is_empty() || listener.default_upstream.is_some()),
                "invalid TLS preread route count"
            );
            let mut routes = HashSet::new();
            if let Some(default) = &listener.default_upstream {
                ensure!(
                    groups.contains_key(default.as_str()),
                    "unknown default TLS upstream"
                );
            }
            for route in &listener.routes {
                let sni = route.sni.as_ref().map(|s| s.to_ascii_lowercase());
                ensure!(
                    sni.as_ref().is_none_or(|s| !s.ends_with('.')
                        && !s.contains('_')
                        && matches!(
                            rustls::pki_types::ServerName::try_from(s.as_str()),
                            Ok(rustls::pki_types::ServerName::DnsName(_))
                        ))
                        && route
                            .alpn
                            .as_ref()
                            .is_none_or(|p| !p.is_empty() && p.len() <= 255)
                        && (sni.is_some() || route.alpn.is_some())
                        && groups.contains_key(route.upstream.as_str())
                        && routes.insert((sni, &route.alpn)),
                    "invalid or duplicate TLS preread route"
                );
            }
        }
        for listener in &self.udp {
            add_address(&mut udp_addrs, listener.listen)?;
            let opts = &listener.options;
            ensure!(
                (1..=100_000).contains(&opts.max_sessions)
                    && (1..=opts.max_sessions).contains(&opts.max_sessions_per_ip)
                    && (1..=65_507).contains(&opts.max_datagram_bytes)
                    && (1..=3).contains(&opts.max_response_ratio)
                    && [
                        opts.idle_timeout,
                        opts.session_timeout,
                        opts.failure_cooldown
                    ]
                    .into_iter()
                    .all(valid_duration),
                "invalid UDP limits"
            );
            ensure!(
                groups
                    .get(listener.upstream.as_str())
                    .is_some_and(|group| group
                        .endpoints
                        .iter()
                        .all(|endpoint| endpoint.address.is_some())),
                "UDP requires an IP upstream group"
            );
            memory +=
                opts.max_sessions as u64 * ((UDP_QUEUE + 2) * (opts.max_datagram_bytes + 1)) as u64;
        }
        ensure!(
            memory <= 1 << 30,
            "aggregate stream buffer budget exceeds 1 GiB"
        );
        Ok(())
    }
}

pub(crate) fn add_address(used: &mut Vec<SocketAddr>, address: SocketAddr) -> Result<()> {
    ensure!(
        address.port() != 0 && address.ip().to_canonical() == address.ip(),
        "stream listeners require a fixed port and native IP family"
    );
    ensure!(
        !used.iter().any(|other| other.port() == address.port()
            && (other.ip() == address.ip()
                || (other.is_ipv4() == address.is_ipv4()
                    && (other.ip().is_unspecified() || address.ip().is_unspecified())))),
        "overlapping stream listeners"
    );
    used.push(address);
    Ok(())
}
