use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Deserializer, Serialize};
use std::{collections::HashSet, net::SocketAddr, path::Path};
use yaml_serde::Value as YamlValue;

const MAX_CONFIG_BYTES: usize = 1_048_576;

pub const MAX_UPSTREAMS: usize = 1024;

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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) real_ip: Option<Box<crate::identity::Policy>>,
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub(crate) rate_zones: std::collections::BTreeMap<String, crate::admission::Spec>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_connections_per_peer: Option<usize>,
    pub upstreams: Vec<Upstream>,
    pub routes: Vec<Route>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct YamlConfig {
    version: u32,
    proxy: Config,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum QuantityInput {
    Number(u64),
    Unit(String),
}

fn duration<'de, D: Deserializer<'de>>(input: D, ms_per_unit: u64) -> Result<u64, D::Error> {
    use serde::de::Error;
    let value = match QuantityInput::deserialize(input)? {
        QuantityInput::Number(value) => return Ok(value),
        QuantityInput::Unit(value) => value,
    };
    let unit_start = value.bytes().take_while(u8::is_ascii_digit).count();
    let (number, suffix) = value.split_at(unit_start);
    let factor = match suffix {
        "ms" => 1,
        "s" => 1000,
        "m" => 60_000,
        "h" => 3_600_000,
        _ => return Err(D::Error::custom("duration must use ms, s, m or h")),
    };
    let ms = number
        .parse::<u64>()
        .ok()
        .and_then(|number| number.checked_mul(factor))
        .ok_or_else(|| D::Error::custom("invalid duration magnitude"))?;
    if ms % ms_per_unit != 0 {
        return Err(D::Error::custom("duration must represent whole seconds"));
    }
    Ok(ms / ms_per_unit)
}

pub(crate) fn milliseconds<'de, D: Deserializer<'de>>(input: D) -> Result<u64, D::Error> {
    duration(input, 1)
}

fn seconds<'de, D: Deserializer<'de>>(input: D) -> Result<u64, D::Error> {
    duration(input, 1000)
}

pub(crate) fn bytes<'de, D: Deserializer<'de>>(input: D) -> Result<u64, D::Error> {
    use serde::de::Error;
    let value = match QuantityInput::deserialize(input)? {
        QuantityInput::Number(value) => return Ok(value),
        QuantityInput::Unit(value) => value,
    };
    let unit_start = value.bytes().take_while(u8::is_ascii_digit).count();
    let (number, suffix) = value.split_at(unit_start);
    let factor = match suffix {
        "B" => 1,
        "KiB" => 1 << 10,
        "MiB" => 1 << 20,
        "GiB" => 1 << 30,
        _ => return Err(D::Error::custom("size must use B, KiB, MiB or GiB")),
    };
    number
        .parse::<u64>()
        .ok()
        .and_then(|n| n.checked_mul(factor))
        .ok_or_else(|| D::Error::custom("invalid size magnitude"))
}

fn bytes_usize<'de, D: Deserializer<'de>>(input: D) -> Result<usize, D::Error> {
    usize::try_from(bytes(input)?).map_err(|_| serde::de::Error::custom("size out of range"))
}

fn bytes_u32<'de, D: Deserializer<'de>>(input: D) -> Result<u32, D::Error> {
    u32::try_from(bytes(input)?).map_err(|_| serde::de::Error::custom("size out of range"))
}

fn expand_env(
    value: &mut YamlValue,
    env: &impl Fn(&str) -> Option<String>,
    remaining: &mut usize,
) -> Result<()> {
    match value {
        YamlValue::String(text) => {
            if let Some(name) = text.strip_prefix("${").and_then(|s| s.strip_suffix('}')) {
                ensure!(
                    name.starts_with(|c: char| c == '_' || c.is_ascii_alphabetic())
                        && name.bytes().all(|b| b == b'_' || b.is_ascii_alphanumeric()),
                    "invalid environment reference"
                );
                let resolved = env(name).context("referenced environment variable unavailable")?;
                *remaining = remaining
                    .checked_sub(resolved.len())
                    .context("environment expansion exceeds 1 MiB")?;
                *text = resolved;
            }
        }
        YamlValue::Sequence(items) => {
            for item in items {
                expand_env(item, env, remaining)?;
            }
        }
        YamlValue::Mapping(map) => {
            for (key, item) in map {
                ensure!(key.is_string(), "YAML configuration keys must be strings");
                expand_env(item, env, remaining)?;
            }
        }
        YamlValue::Tagged(_) => bail!("YAML configuration does not accept custom tags"),
        _ => {}
    }
    Ok(())
}

fn deserialize_yaml(data: &str, env: impl Fn(&str) -> Option<String>) -> Result<Config> {
    let mut doc: YamlValue = yaml_serde::from_str(data).map_err(|error| {
        if let Some(location) = error.location() {
            anyhow::anyhow!(
                "invalid YAML at line {}, column {}",
                location.line(),
                location.column()
            )
        } else {
            anyhow::anyhow!("invalid YAML document")
        }
    })?;
    let mut expansion_budget = MAX_CONFIG_BYTES;
    expand_env(&mut doc, &env, &mut expansion_budget)?;
    let config: YamlConfig = yaml_serde::from_value(doc)
        .map_err(|_| anyhow::anyhow!("invalid YAML configuration field, type or quantity"))?;
    ensure!(
        config.version == 1,
        "unsupported YAML configuration version"
    );
    Ok(config.proxy)
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct InboundTls {
    pub cert_file: String,
    pub key_file: String,
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub sni: std::collections::BTreeMap<String, CertificateFiles>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_ca_file: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub versions: Option<Vec<TlsVersion>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_resumption: Option<bool>,
}

#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct CertificateFiles {
    pub cert_file: String,
    pub key_file: String,
}

#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq, Debug)]
pub enum TlsVersion {
    #[serde(rename = "tls1.2")]
    Tls12,
    #[serde(rename = "tls1.3")]
    Tls13,
}

fn validate_tls_versions(versions: Option<&[TlsVersion]>) -> Result<()> {
    if let Some(versions) = versions {
        ensure!(
            matches!(versions, [_]) || matches!(versions, [first, second] if first != second),
            "TLS versions must list TLS 1.2, TLS 1.3 or both without duplicates"
        );
    }
    Ok(())
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(default, deny_unknown_fields)]
pub struct Http2Config {
    pub enabled: bool,
    pub max_concurrent_streams: u32,
    #[serde(deserialize_with = "bytes_u32")]
    pub initial_stream_window_bytes: u32,
    #[serde(deserialize_with = "bytes_u32")]
    pub initial_connection_window_bytes: u32,
    #[serde(deserialize_with = "bytes_usize")]
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
    #[serde(deserialize_with = "milliseconds")]
    pub acquire_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub connect_ms: u64,
    #[serde(default = "default_tls_handshake_ms")]
    #[serde(deserialize_with = "milliseconds")]
    pub tls_handshake_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub headers_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub stream_idle_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub request_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub client_headers_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub idle_connection_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub max_connection_age_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub dns_cache_ms: u64,
    #[serde(deserialize_with = "seconds")]
    pub tcp_keepalive_secs: u64,
    #[serde(deserialize_with = "bytes_usize")]
    pub max_header_bytes: usize,
    pub max_headers: usize,
    #[serde(deserialize_with = "bytes")]
    pub max_body_bytes: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub shutdown_ms: u64,
    #[serde(default = "default_websocket_max_ms")]
    #[serde(deserialize_with = "milliseconds")]
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
    #[serde(deserialize_with = "bytes")]
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
    #[serde(default = "default_weight")]
    pub weight: u16,
    pub connections: usize,
    pub idle_connections: usize,
    #[serde(deserialize_with = "milliseconds")]
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
    #[serde(
        default,
        deserialize_with = "nonempty_health_path",
        skip_serializing_if = "String::is_empty"
    )]
    pub path: String,
    #[serde(default)]
    pub mode: HealthMode,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub service: Option<String>,
    #[serde(deserialize_with = "milliseconds")]
    pub interval_ms: u64,
    #[serde(deserialize_with = "milliseconds")]
    pub timeout_ms: u64,
    pub rise: u32,
    pub fall: u32,
}
#[derive(Clone, Copy, Default, Deserialize, Serialize, PartialEq, Eq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum HealthMode {
    #[default]
    Http,
    Grpc,
}

fn nonempty_health_path<'de, D: Deserializer<'de>>(value: D) -> Result<String, D::Error> {
    let path = String::deserialize(value)?;
    if path.is_empty() {
        return Err(serde::de::Error::custom("health path cannot be empty"));
    }
    Ok(path)
}

fn default_weight() -> u16 {
    1
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_certificate: Option<CertificateFiles>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub versions: Option<Vec<TlsVersion>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_resumption: Option<bool>,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct Route {
    pub id: String,
    pub host: String,
    #[serde(
        default,
        deserialize_with = "nonempty_path_prefix",
        skip_serializing_if = "String::is_empty"
    )]
    pub path_prefix: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path_pattern: Option<String>,
    #[serde(default, skip_serializing_if = "PathMatch::is_prefix")]
    pub path_match: PathMatch,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub headers: Option<Box<crate::headers::Policy>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) action: Option<Box<crate::actions::Action>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) allow_clients: Option<crate::identity::Networks>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rate_zone: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) content: Option<Box<crate::content::Handler>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) compression: Option<Box<crate::compression::Policy>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) cache: Option<Box<crate::cache::Spec>>,
    #[serde(default)]
    pub balance: Balance,
    #[serde(default)]
    pub upstreams: Vec<String>,
}

fn nonempty_path_prefix<'de, D: Deserializer<'de>>(value: D) -> Result<String, D::Error> {
    let path = String::deserialize(value)?;
    if path.is_empty() {
        return Err(serde::de::Error::custom("path_prefix cannot be empty"));
    }
    Ok(path)
}

#[derive(Clone, Copy, Default, Deserialize, Serialize, PartialEq, Eq, Hash, Debug)]
#[serde(rename_all = "snake_case")]
pub enum PathMatch {
    #[default]
    Prefix,
    Exact,
    LiteralPrefix,
    PrefixNoRegex,
    Regex,
    RegexInsensitive,
    Named,
}

impl PathMatch {
    fn is_prefix(&self) -> bool {
        *self == Self::Prefix
    }

    fn is_regex(self) -> bool {
        matches!(self, Self::Regex | Self::RegexInsensitive)
    }
}
#[derive(Clone, Copy, Default, Deserialize, Serialize, PartialEq, Debug)]
#[serde(rename_all = "snake_case")]
pub enum Balance {
    #[default]
    RoundRobin,
    LeastInflight,
    WeightedRoundRobin,
    LeastConnections,
    WeightedLeastConnections,
    IpHash,
    ConsistentHash,
    PowerOfTwoChoices,
}
pub(crate) fn identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}
impl Config {
    pub async fn load(path: &Path) -> Result<(Self, crate::routing::Routes)> {
        use tokio::io::AsyncReadExt;
        let file = tokio::fs::File::open(path)
            .await
            .context("read configuration")?;
        let mut data = Vec::new();
        file.take((MAX_CONFIG_BYTES + 1) as u64)
            .read_to_end(&mut data)
            .await?;
        ensure!(
            data.len() <= MAX_CONFIG_BYTES,
            "configuration exceeds 1 MiB"
        );
        let yaml = path
            .extension()
            .is_some_and(|ext| ext == "yaml" || ext == "yml");
        tokio::task::spawn_blocking(move || {
            let text = std::str::from_utf8(&data)?;
            let config: Self = if yaml {
                deserialize_yaml(text, |name| std::env::var(name).ok())?
            } else {
                toml::from_str(text)?
            };
            config.validate()?;
            let routes = crate::routing::Routes::compile(&config.routes)?;
            Ok((config, routes))
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
        if let Some(tls) = &self.tls {
            ensure!(tls.sni.len() <= 32, "at most 32 SNI certificate entries");
            let mut names = HashSet::new();
            for name in tls.sni.keys() {
                ensure!(
                    !name.ends_with('.')
                        && !name.contains('_')
                        && matches!(
                            rustls::pki_types::ServerName::try_from(name.as_str()),
                            Ok(rustls::pki_types::ServerName::DnsName(_))
                        )
                        && names.insert(name.to_ascii_lowercase()),
                    "invalid or duplicate TLS SNI DNS name"
                );
            }
            validate_tls_versions(tls.versions.as_deref())?;
        }
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
            self.upstreams.len() <= MAX_UPSTREAMS
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
            ensure!(
                (1..=64).contains(&u.weight),
                "upstream weight must be 1 to 64"
            );
            if let Some(h) = &u.health_check {
                match h.mode {
                    HealthMode::Http => {
                        let uri: hyper::Uri = h.path.parse().context("invalid health path")?;
                        ensure!(
                            h.service.is_none()
                                && h.path.starts_with('/')
                                && !h.path.starts_with("//")
                                && h.path.len() <= 1024
                                && uri.scheme().is_none()
                                && uri.authority().is_none()
                                && !h.path.contains('#'),
                            "HTTP health requires an origin-form path and no service"
                        );
                    }
                    HealthMode::Grpc => {
                        ensure!(
                            u.protocol == UpstreamProtocol::Http2 && h.path.is_empty(),
                            "gRPC health requires an HTTP/2 upstream and uses the standard Check path"
                        );
                        ensure!(
                            h.service.as_ref().is_none_or(|service| {
                                service.len() <= 256 && !service.chars().any(char::is_control)
                            }),
                            "invalid gRPC health service"
                        );
                    }
                }
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
                validate_tls_versions(tls.versions.as_deref())?;
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
        ensure!(
            self.routes
                .iter()
                .filter(|route| route
                    .action
                    .as_ref()
                    .is_some_and(|action| !action.unconditional()))
                .count()
                <= 128,
            "at most 128 action patterns"
        );
        let weights: std::collections::HashMap<_, _> = self
            .upstreams
            .iter()
            .map(|upstream| (upstream.id.as_str(), usize::from(upstream.weight)))
            .collect();
        ensure!(
            self.routes
                .iter()
                .filter_map(|route| route.compression.as_ref())
                .map(|policy| policy.max_inflight())
                .sum::<usize>()
                <= 128,
            "compression generation quota exceeded"
        );
        let mut cache_budget = 0;
        let mut ticket_budget = 0;
        ensure!(
            self.rate_zones.len() <= 32
                && self
                    .rate_zones
                    .iter()
                    .all(|(id, zone)| identifier(id) && zone.valid())
                && self
                    .rate_zones
                    .values()
                    .map(|zone| zone.max_keys)
                    .sum::<usize>()
                    <= 262144,
            "invalid rate zone or total key budget"
        );
        ensure!(
            self.max_connections_per_peer
                .is_none_or(|cap| (1..=l.clients).contains(&cap)),
            "invalid per-peer connection cap"
        );
        let mut route_ids = HashSet::new();
        let mut matches = HashSet::new();
        for r in &self.routes {
            ensure!(
                r.rate_zone
                    .as_ref()
                    .is_none_or(|name| self.rate_zones.contains_key(name)),
                "unknown rate zone"
            );
            ensure!(
                identifier(&r.id) && route_ids.insert(&r.id),
                "invalid/duplicate route id"
            );
            if r.host.contains('*') {
                ensure!(
                    crate::routing::wildcard(&r.host).is_some(),
                    "route host must use a wildcard on one edge of a lowercase DNS name"
                );
            } else {
                ensure!(
                    !r.host.is_empty() && r.host == r.host.to_ascii_lowercase(),
                    "route host must be a lowercase authority"
                );
                let _: hyper::http::uri::Authority = r.host.parse()?;
            }
            let (path, match_kind) = if r.path_match == PathMatch::Named {
                let name = r
                    .path_pattern
                    .as_deref()
                    .filter(|name| identifier(name))
                    .context("named route requires a location identifier")?;
                ensure!(
                    r.path_prefix.is_empty(),
                    "named route requires path_pattern only"
                );
                (name, PathMatch::Named)
            } else if r.path_match.is_regex() {
                ensure!(
                    r.path_prefix.is_empty(),
                    "regex route requires path_pattern only"
                );
                let pattern = r
                    .path_pattern
                    .as_deref()
                    .filter(|pattern| !pattern.is_empty() && pattern.len() <= 256)
                    .context("regex route requires path_pattern of at most 256 bytes")?;
                (pattern, r.path_match)
            } else {
                ensure!(
                    r.path_pattern.is_none()
                        && r.path_prefix.starts_with('/')
                        && !r.path_prefix.contains(['?', '#', '%'])
                        && r.path_prefix.len() <= 256,
                    "literal route requires a valid path_prefix only"
                );
                let unique_kind = if r.path_match == PathMatch::Exact {
                    PathMatch::Exact
                } else {
                    PathMatch::Prefix
                };
                (r.path_prefix.as_str(), unique_kind)
            };
            ensure!(
                matches.insert((&r.host, path, match_kind)),
                "duplicate route match"
            );
            if let Some(spec) = &r.cache {
                spec.validate()?;
                ensure!(
                    r.content.is_none() && !r.upstreams.is_empty(),
                    "cache requires a proxy route"
                );
                if let Some(headers) = &r.headers {
                    ensure!(!headers.request.touches(&["cookie", "authorization", "range", "if-range", "if-match", "if-none-match", "if-modified-since", "if-unmodified-since"])
                        && !headers.response.touches(&["cache-control", "vary", "set-cookie", "set-cookie2", "expires", "etag", "last-modified", "age", "cache-status"]),
                        "cache route headers cannot change authentication, conditionals or cache controls");
                }
                cache_budget += spec.max_size;
                ensure!(
                    cache_budget <= 512 * 1024 * 1024,
                    "generation cache byte quota exceeded"
                );
            }
            if let Some(handler) = &r.content {
                handler.validate(r)?;
                ensure!(
                    r.upstreams.is_empty(),
                    "local content cannot have upstreams"
                );
            }
            ensure!(
                (r.content.is_some()
                    || !r.upstreams.is_empty()
                    || r.action
                        .as_ref()
                        .is_some_and(|action| action.unconditional()))
                    && r.upstreams.iter().all(|id| ids.contains(id)),
                "unknown or empty route upstreams"
            );
            if matches!(r.balance, Balance::WeightedRoundRobin | Balance::IpHash) {
                let tickets: usize = r.upstreams.iter().map(|id| weights[id.as_str()]).sum();
                ticket_budget += tickets;
                ensure!(
                    tickets <= 4096 && ticket_budget <= 65536,
                    "weighted route ticket budget exceeded"
                );
            }
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
            || self.rate_zones != next.rate_zones
            || self.max_connections_per_peer != next.max_connections_per_peer
            || self.name != next.name
        {
            bail!("reload supports routes/upstreams only; listener/limits/log/name and admission zone changes require restart");
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn yaml_and_toml_compile_to_the_same_configuration() {
        let yaml = include_str!("../examples/cauce.yaml");
        let toml = include_str!("../examples/cauce.toml");
        let yaml = deserialize_yaml(yaml, |_| None).unwrap();
        let toml = toml::from_str::<Config>(toml).unwrap();
        assert_eq!(yaml, toml);
        yaml.validate().unwrap();
    }

    #[test]
    fn yaml_rejects_invalid_fields_versions_and_durations() {
        let example = include_str!("../examples/cauce.yaml");
        for (old, new) in [
            ("version: 1", "version: 2"),
            ("version: 1", "version: 1\nversion: 1"),
            ("version: 1", "version: 1\nunexpected: true"),
            ("    clients: 1024", "    clients: 1024\n    typo: 2"),
            ("    clients: 1024", "    clients: 1024\n    clients: 2"),
            ("    clients: 1024", "    clients: !custom 1024"),
            ("250ms", "250us"),
            ("250ms", "18446744073709551615s"),
            ("30s", "1ms"),
        ] {
            assert!(
                deserialize_yaml(&example.replace(old, new), |_| None).is_err(),
                "{new}"
            );
        }
    }

    #[test]
    fn byte_units_compile_to_native_limits_and_reject_overflow() {
        let original = include_str!("../examples/cauce.yaml");
        let configure = |size: &str| {
            let input = original.replace("16MiB", "${CAUCE_BYTES}");
            deserialize_yaml(&input, |_| Some(size.to_owned()))
        };
        let valid = configure("16384KiB").unwrap();
        assert_eq!(valid.limits.max_body_bytes, 16 * 1024 * 1024);
        assert_eq!(valid.logs.rotate_bytes, valid.limits.max_body_bytes);
        valid.validate().unwrap();
        for bad in ["1.5MiB", "1KB", "1 KiB", "18446744073709551615GiB", "-1B"] {
            assert!(configure(bad).is_err(), "{bad}");
        }
        assert!(configure("0B").unwrap().validate().is_err());
        let input = format!("{original}\n  http2:\n    initial_stream_window_bytes: 4GiB\n");
        assert!(deserialize_yaml(&input, |_| None).is_err());
        let toml = include_str!("../examples/cauce.toml")
            .replace("max_header_bytes = 32768", "max_header_bytes = \"32KiB\"");
        assert_eq!(
            toml::from_str::<Config>(&toml)
                .unwrap()
                .limits
                .max_header_bytes,
            32768
        );
    }

    #[test]
    fn yaml_expands_whole_values_and_redacts_failed_substitutions() {
        let example = include_str!("../examples/cauce.yaml")
            .replace("logs/access.jsonl", "${CAUCE_LOG}")
            .replace("250ms", "${CAUCE_WAIT}");
        let valid = deserialize_yaml(&example, |name| match name {
            "CAUCE_LOG" => Some("test: value # remains a scalar".into()),
            "CAUCE_WAIT" => Some("2s".into()),
            _ => None,
        })
        .unwrap();
        assert_eq!(valid.logs.path, "test: value # remains a scalar");
        assert_eq!(valid.limits.acquire_ms, 2000);
        for env in [Some("operator-secret-value"), None] {
            let result = deserialize_yaml(&example, |_| env.map(str::to_owned));
            let diagnostic = format!("{:#}", result.unwrap_err());
            assert!(!diagnostic.contains("operator-secret-value"));
        }
    }
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
            client_certificate: None,
            versions: None,
            session_resumption: None,
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
            sni: Default::default(),
            client_ca_file: None,
            versions: None,
            session_resumption: None,
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
