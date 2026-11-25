use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{collections::HashSet, net::SocketAddr, path::Path};

#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub name: String,
    pub listen: SocketAddr,
    pub admin: SocketAddr,
    pub limits: Limits,
    pub logs: Logs,
    #[serde(default)]
    pub tls: Option<InboundTls>,
    #[serde(default)]
    pub http2: Http2Config,
    pub upstreams: Vec<Upstream>,
    pub routes: Vec<Route>,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct InboundTls {
    pub cert_file: String,
    pub key_file: String,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(default, deny_unknown_fields)]
pub struct Http2Config {
    pub enabled: bool,
    pub max_concurrent_streams: u32,
    pub initial_stream_window_bytes: u32,
    pub initial_connection_window_bytes: u32,
    pub max_send_buffer_bytes: usize,
    pub max_pending_accept_reset_streams: usize,
    pub max_local_error_reset_streams: usize,
}
impl Default for Http2Config {
    fn default() -> Self {
        Self {
            enabled: true,
            max_concurrent_streams: 128,
            initial_stream_window_bytes: 65_535,
            initial_connection_window_bytes: 1_048_576,
            max_send_buffer_bytes: 65_536,
            max_pending_accept_reset_streams: 32,
            max_local_error_reset_streams: 32,
        }
    }
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    pub clients: usize,
    pub requests: usize,
    pub connections: usize,
    pub idle_connections: usize,
    pub connecting: usize,
    pub acquire_ms: u64,
    pub connect_ms: u64,
    #[serde(default = "default_tls_handshake_ms")]
    pub tls_handshake_ms: u64,
    pub headers_ms: u64,
    pub stream_idle_ms: u64,
    pub request_ms: u64,
    pub client_headers_ms: u64,
    pub idle_connection_ms: u64,
    pub max_connection_age_ms: u64,
    pub dns_cache_ms: u64,
    pub tcp_keepalive_secs: u64,
    pub max_header_bytes: usize,
    pub max_headers: usize,
    pub max_body_bytes: u64,
    pub shutdown_ms: u64,
    #[serde(default = "default_websocket_max_ms")]
    pub websocket_max_ms: u64,
}
fn default_websocket_max_ms() -> u64 {
    3_600_000
}
fn default_tls_handshake_ms() -> u64 {
    5000
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct Logs {
    pub enabled: bool,
    pub path: String,
    pub queue: usize,
    pub rotate_bytes: u64,
    pub retained_files: usize,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct Upstream {
    pub id: String,
    /// Pool key is the entire immutable upstream plus config generation.
    pub address: String,
    pub host: String,
    pub isolation: String,
    pub connections: usize,
    pub idle_connections: usize,
    pub failure_backoff_ms: u64,
    #[serde(default)]
    pub health_check: Option<HealthCheck>,
    #[serde(default)]
    pub tls: Option<UpstreamTls>,
    #[serde(default)]
    pub protocol: UpstreamProtocol,
    #[serde(default = "default_streams_per_connection")]
    pub streams_per_connection: usize,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct HealthCheck {
    pub path: String,
    pub interval_ms: u64,
    pub timeout_ms: u64,
    pub rise: u32,
    pub fall: u32,
}
fn default_streams_per_connection() -> usize {
    128
}
#[derive(Clone, Copy, Default, Deserialize, Serialize, PartialEq, Eq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum UpstreamProtocol {
    #[default]
    Http1,
    Http2,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct UpstreamTls {
    pub server_name: String,
    pub ca_file: String,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct Route {
    pub id: String,
    pub host: String,
    pub path_prefix: String,
    pub balance: Balance,
    pub upstreams: Vec<String>,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Debug)]
#[serde(rename_all = "snake_case")]
pub enum Balance {
    RoundRobin,
    LeastInflight,
}
fn identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}
impl Config {
    pub async fn load(path: &Path) -> Result<Self> {
        use tokio::io::AsyncReadExt;
        let file = tokio::fs::File::open(path)
            .await
            .context("read configuration")?;
        let mut data = Vec::new();
        file.take(1_048_577).read_to_end(&mut data).await?;
        ensure!(data.len() <= 1_048_576, "configuration exceeds 1 MiB");
        tokio::task::spawn_blocking(move || {
            let config: Self = toml::from_str(std::str::from_utf8(&data)?)?;
            config.validate()?;
            Ok(config)
        })
        .await?
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(identifier(&self.name), "invalid product name");
        ensure!(
            self.admin.ip().is_loopback(),
            "admin must bind loopback; use an authenticated local tunnel"
        );
        ensure!(
            self.admin != self.listen,
            "separate admin and data listeners"
        );
        let l = &self.limits;
        let h = &self.http2;
        ensure!(
            (1..=10_000).contains(&h.max_concurrent_streams)
                && (16_384..=16_777_216).contains(&h.initial_stream_window_bytes)
                && (65_535..=67_108_864).contains(&h.initial_connection_window_bytes)
                && (16_384..=1_048_576).contains(&h.max_send_buffer_bytes)
                && (1..=1024).contains(&h.max_pending_accept_reset_streams)
                && (1..=1024).contains(&h.max_local_error_reset_streams),
            "invalid HTTP/2 stream, window or reset limits"
        );
        ensure!(
            l.clients > 0 && l.requests > 0 && l.connections > 0 && l.connecting > 0,
            "zero capacity"
        );
        ensure!(
            l.clients <= 100_000 && l.requests <= 100_000 && l.connections <= 100_000,
            "capacity too large"
        );
        ensure!(
            l.idle_connections <= l.connections,
            "idle capacity exceeds total"
        );
        ensure!(
            [
                l.acquire_ms,
                l.connect_ms,
                l.tls_handshake_ms,
                l.headers_ms,
                l.stream_idle_ms,
                l.request_ms,
                l.client_headers_ms,
                l.idle_connection_ms,
                l.max_connection_age_ms,
                l.dns_cache_ms,
                l.tcp_keepalive_secs,
                l.shutdown_ms,
                l.websocket_max_ms,
            ]
            .iter()
            .all(|x| *x > 0 && *x <= 86_400_000),
            "invalid timeout"
        );
        ensure!(
            l.max_header_bytes >= 8192
                && l.max_header_bytes <= 1_048_576
                && l.max_headers > 0
                && l.max_headers <= 1000
                && l.max_body_bytes > 0,
            "invalid message limits"
        );
        ensure!(
            self.logs.queue > 0
                && self.logs.queue <= 100_000
                && self.logs.rotate_bytes >= 4096
                && self.logs.retained_files <= 100,
            "invalid log limits"
        );
        ensure!(!self.logs.path.is_empty(), "missing log path");
        ensure!(
            !self.upstreams.is_empty()
                && self.upstreams.len() <= 1024
                && !self.routes.is_empty()
                && self.routes.len() <= 1024,
            "invalid routing size"
        );
        ensure!(
            self.upstreams
                .iter()
                .filter(|u| u.health_check.is_some())
                .count()
                <= 64,
            "at most 64 active health checks"
        );
        let mut ids = HashSet::new();
        for u in &self.upstreams {
            if let Some(h) = &u.health_check {
                ensure!(
                    u.protocol == UpstreamProtocol::Http1,
                    "active HTTP health requires HTTP/1 upstream"
                );
                let uri: hyper::Uri = h.path.parse().context("invalid health path")?;
                ensure!(
                    h.path.starts_with('/')
                        && !h.path.starts_with("//")
                        && h.path.len() <= 1024
                        && uri.scheme().is_none()
                        && uri.authority().is_none()
                        && !h.path.contains('#'),
                    "health path must be origin-form"
                );
                ensure!(
                    (100..=60_000).contains(&h.interval_ms)
                        && (10..=5_000).contains(&h.timeout_ms)
                        && h.timeout_ms <= h.interval_ms
                        && (1..=10).contains(&h.rise)
                        && (1..=10).contains(&h.fall),
                    "invalid health bounds"
                );
            }

            ensure!(
                (1..=10_000).contains(&u.streams_per_connection),
                "invalid upstream streams_per_connection"
            );
            ensure!(
                identifier(&u.id) && ids.insert(&u.id),
                "invalid/duplicate upstream id"
            );
            if let Some(tls) = &u.tls {
                ensure!(
                    !tls.server_name.is_empty() && tls.server_name.len() <= 253,
                    "invalid TLS server_name"
                );
                let _: rustls::pki_types::ServerName<'static> = tls
                    .server_name
                    .clone()
                    .try_into()
                    .context("invalid TLS server_name")?;
                ensure!(!tls.ca_file.is_empty(), "missing upstream CA file");
            }
            ensure!(
                u.connections > 0
                    && u.connections <= l.connections
                    && u.idle_connections <= u.connections,
                "invalid upstream capacity"
            );
            ensure!(
                u.failure_backoff_ms > 0 && u.failure_backoff_ms <= 60_000,
                "invalid backoff"
            );
            ensure!(
                !u.isolation.is_empty() && u.isolation.len() <= 64,
                "missing isolation boundary"
            );
            let h: hyper::http::uri::Authority = u.host.parse().context("invalid upstream Host")?;
            ensure!(!h.as_str().contains('@'), "credentials forbidden");
            if let Some(path) = u.address.strip_prefix("unix:") {
                ensure!(
                    u.tls.is_none(),
                    "TLS upstream requires TCP; Unix socket TLS is unsupported"
                );
                ensure!(
                    path.starts_with('/') && path.len() < 104,
                    "Unix socket must have an absolute short path"
                );
            } else {
                let a: hyper::http::uri::Authority =
                    u.address.parse().context("use host:port or unix:/path")?;
                ensure!(
                    a.port_u16().is_some() && !a.as_str().contains('@'),
                    "explicit upstream port required"
                );
            }
        }
        let mut route_ids = HashSet::new();
        let mut matches = HashSet::new();
        for r in &self.routes {
            ensure!(
                identifier(&r.id) && route_ids.insert(&r.id),
                "invalid/duplicate route id"
            );
            ensure!(
                !r.host.is_empty()
                    && r.host == r.host.to_ascii_lowercase()
                    && !r.host.contains('*'),
                "route host must be exact lowercase authority"
            );
            let _: hyper::http::uri::Authority = r.host.parse()?;
            ensure!(
                r.path_prefix.starts_with('/')
                    && !r.path_prefix.contains(['?', '#', '%'])
                    && r.path_prefix.len() <= 256,
                "invalid path prefix"
            );
            ensure!(
                matches.insert((&r.host, &r.path_prefix)),
                "duplicate route match"
            );
            ensure!(
                !r.upstreams.is_empty() && r.upstreams.iter().all(|id| ids.contains(id)),
                "unknown or empty route upstreams"
            );
            let unique: HashSet<_> = r.upstreams.iter().collect();
            ensure!(
                unique.len() == r.upstreams.len(),
                "duplicate upstream in route"
            );
        }
        Ok(())
    }
    pub fn reload_compatible(&self, next: &Self) -> Result<()> {
        if self.listen != next.listen
            || self.tls.is_some() != next.tls.is_some()
            || self.admin != next.admin
            || self.limits != next.limits
            || self.http2 != next.http2
            || self.logs != next.logs
            || self.name != next.name
        {
            bail!("reload supports routes/upstreams only; listener/limits/log/name changes require restart");
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn example_and_fail_closed() {
        let c: Config = toml::from_str(include_str!("../examples/cauce.toml")).unwrap();
        c.validate().unwrap();
        let mut bad = c.clone();
        bad.admin = "0.0.0.0:9090".parse().unwrap();
        assert!(bad.validate().is_err());
        let mut bad = c.clone();
        bad.routes[0].upstreams = vec!["unknown".into()];
        assert!(bad.validate().is_err());
        let mut bad = c.clone();
        bad.upstreams[0].address = "https://example.com".into();
        assert!(bad.validate().is_err());
        let mut bad = c.clone();
        bad.upstreams[0].address = "unix:/tmp/backend.sock".into();
        bad.upstreams[0].tls = Some(UpstreamTls {
            server_name: "backend.test".into(),
            ca_file: "/tmp/ca.pem".into(),
        });
        assert!(bad.validate().is_err());
        let mut bad = c.clone();
        bad.routes[0].host = "*".into();
        assert!(bad.validate().is_err());
        assert!(toml::from_str::<Config>(&format!(
            "unknown = true\n{}",
            include_str!("../examples/cauce.toml")
        ))
        .is_err());
        let mut inbound_toggle = c.clone();
        inbound_toggle.tls = Some(InboundTls {
            cert_file: "/tmp/cert.pem".into(),
            key_file: "/tmp/key.pem".into(),
        });
        assert!(c.reload_compatible(&inbound_toggle).is_err());
    }
    #[test]
    fn h2_defaults_caps_and_reload() {
        let c: Config = toml::from_str(include_str!("../examples/cauce.toml")).unwrap();
        assert!(c.http2.enabled);
        assert_eq!(c.upstreams[0].protocol, UpstreamProtocol::Http1);
        let mut changed = c.clone();
        changed.http2.max_concurrent_streams = 0;
        assert!(changed.validate().is_err());
        changed.http2.max_concurrent_streams = 4;
        changed.validate().unwrap();
        assert!(c.reload_compatible(&changed).is_err());
        let mut changed = c.clone();
        changed.upstreams[0].protocol = UpstreamProtocol::Http2;
        changed.upstreams[0].streams_per_connection = 4;
        changed.validate().unwrap();
        c.reload_compatible(&changed).unwrap();
        changed.upstreams[0].streams_per_connection = 0;
        assert!(changed.validate().is_err());
    }
}
