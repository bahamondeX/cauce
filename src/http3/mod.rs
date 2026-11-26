//! QUIC settings stay deserializable so slim builds can reject them explicitly.
#[cfg(feature = "quic")]
mod quic;
#[cfg(feature = "quic")]
mod runtime;
#[cfg(feature = "quic")]
pub(crate) use runtime::{bind, prepare, serve};

use crate::config::{milliseconds, Config, InboundTls, TlsVersion};
use anyhow::{ensure, Context, Result};
use hyper::header::HeaderValue;
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
#[cfg(feature = "quic")]
use std::time::Duration;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub(crate) listen: SocketAddr,
    #[serde(default)]
    pub(crate) tls: Option<InboundTls>,
    #[serde(default)]
    advertise: bool,
    #[serde(default)]
    path_migration: bool,
    #[serde(default = "connections")]
    max_connections: usize,
    #[serde(default = "streams")]
    max_streams: u32,
    #[serde(default = "requests_per_connection")]
    max_requests_per_connection: u32,
    #[serde(default = "field_bytes")]
    max_field_bytes: usize,
    #[serde(default = "idle_ms", deserialize_with = "milliseconds")]
    idle_timeout: u64,
    #[serde(default = "lifetime_ms", deserialize_with = "milliseconds")]
    connection_timeout: u64,
}

fn connections() -> usize {
    128
}
fn requests_per_connection() -> u32 {
    4096
}
fn streams() -> u32 {
    32
}
fn field_bytes() -> usize {
    16 * 1024
}
fn idle_ms() -> u64 {
    30_000
}
fn lifetime_ms() -> u64 {
    3_600_000
}
#[cfg(feature = "quic")]
fn ms(value: u64) -> Duration {
    Duration::from_millis(value)
}

impl Settings {
    pub(crate) fn reload_compatible(&self, other: &Self) -> bool {
        self.listen == other.listen
            && self.advertise == other.advertise
            && self.path_migration == other.path_migration
            && self.max_connections == other.max_connections
            && self.max_streams == other.max_streams
            && self.max_requests_per_connection == other.max_requests_per_connection
            && self.max_field_bytes == other.max_field_bytes
            && self.idle_timeout == other.idle_timeout
            && self.connection_timeout == other.connection_timeout
    }

    pub(crate) fn validate(&self, config: &Config) -> Result<()> {
        let mut addresses: Vec<_> = config
            .stream
            .as_ref()
            .into_iter()
            .flat_map(|s| s.udp.iter().map(|l| l.listen))
            .collect();
        crate::stream::config::add_address(&mut addresses, self.listen)?;
        let tls = self
            .tls
            .as_ref()
            .or(config.tls.as_ref())
            .context("HTTP/3 requires TLS")?;
        tls.validate()?;
        ensure!(
            tls.versions
                .as_ref()
                .is_none_or(|versions| versions.contains(&TlsVersion::Tls13)),
            "HTTP/3 requires TLS 1.3"
        );
        ensure!(
            (1..=1024).contains(&self.max_connections)
                && (1..=256).contains(&self.max_streams)
                && (self.max_streams..=16384).contains(&self.max_requests_per_connection)
                && (1024..=65_536).contains(&self.max_field_bytes)
                && (1..=86_400_000).contains(&self.connection_timeout)
                && (1..=self.connection_timeout).contains(&self.idle_timeout),
            "invalid HTTP/3 resource or deadline bounds"
        );
        let concurrency = self.max_connections.min(config.limits.clients) as u64;
        let visible_memory = concurrency
            * (2 * 1024 * 1024 + 2 * u64::from(self.max_streams) * self.max_field_bytes as u64);
        ensure!(
            visible_memory <= 1 << 30,
            "aggregate HTTP/3 flow window and header budget exceeds 1 GiB"
        );
        ensure!(
            !self.advertise
                || config.listen.is_some() && config.tls.is_some()
                || config
                    .listeners
                    .iter()
                    .any(|listener| listener.tls.is_some()),
            "HTTP/3 Alt-Svc advertisement requires a TCP TLS listener"
        );
        Ok(())
    }

    pub(crate) fn alt_svc(&self) -> Option<HeaderValue> {
        self.advertise.then(|| {
            HeaderValue::from_str(&format!("h3=\":{}\"; ma=300", self.listen.port())).unwrap()
        })
    }
}
