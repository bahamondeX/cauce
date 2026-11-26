use crate::config::{InboundTls, UpstreamTls};
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::{collections::HashSet, net::SocketAddr};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Protocol {
    Smtp,
    Imap,
    Pop3,
}

impl Protocol {
    pub(super) fn name(self) -> &'static str {
        match self {
            Self::Smtp => "smtp",
            Self::Imap => "imap",
            Self::Pop3 => "pop3",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum TlsMode {
    Starttls,
    Implicit,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Mechanism {
    Plain,
    Login,
    UserPass,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Backend {
    pub address: SocketAddr,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dns_address: Option<String>,
    pub tls: Option<UpstreamTls>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub listen: SocketAddr,
    pub protocol: Protocol,
    pub tls: InboundTls,
    pub tls_mode: TlsMode,
    pub auth_http: String,
    pub auth_mechanisms: Vec<Mechanism>,
    pub backends: Vec<Backend>,
    #[serde(default = "default_connections")]
    pub max_connections: usize,
    #[serde(
        default = "default_auth_timeout",
        deserialize_with = "crate::config::milliseconds"
    )]
    pub auth_timeout_ms: u64,
    #[serde(
        default = "default_idle_timeout",
        deserialize_with = "crate::config::milliseconds"
    )]
    pub idle_timeout_ms: u64,
    #[serde(
        default = "default_session_timeout",
        deserialize_with = "crate::config::milliseconds"
    )]
    pub session_timeout_ms: u64,
}

fn default_connections() -> usize {
    256
}
fn default_auth_timeout() -> u64 {
    15_000
}
fn default_idle_timeout() -> u64 {
    300_000
}
fn default_session_timeout() -> u64 {
    3_600_000
}

impl Settings {
    pub(crate) fn auth_url(&self) -> Result<reqwest::Url> {
        ensure!(self.auth_http.len() <= 2048, "mail auth_http URL length");
        let url = reqwest::Url::parse(&self.auth_http)?;
        ensure!(
            !url.cannot_be_a_base()
                && url.username().is_empty()
                && url.password().is_none()
                && url.fragment().is_none()
                && url.host().is_some(),
            "invalid mail auth_http URL"
        );
        let loopback = url.host_str().is_some_and(|host| {
            host.trim_matches(['[', ']'])
                .parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback())
        });
        ensure!(
            url.scheme() == "https" || (url.scheme() == "http" && loopback),
            "mail auth_http requires HTTPS or a loopback HTTP address"
        );
        Ok(url)
    }

    pub(crate) fn validate(&self, dns_enabled: bool) -> Result<()> {
        self.tls.validate()?;
        self.auth_url()?;
        ensure!(
            (1..=4096).contains(&self.max_connections)
                && (100..=120_000).contains(&self.auth_timeout_ms)
                && (100..=86_400_000).contains(&self.idle_timeout_ms)
                && (self.auth_timeout_ms..=86_400_000).contains(&self.session_timeout_ms)
                && self.idle_timeout_ms <= self.session_timeout_ms,
            "invalid mail admission or deadline"
        );
        let mut mechanisms = HashSet::new();
        ensure!(
            !self.auth_mechanisms.is_empty()
                && self.auth_mechanisms.iter().all(|mechanism| {
                    mechanisms.insert(mechanism)
                        && matches!(
                            (self.protocol, mechanism),
                            (Protocol::Smtp, Mechanism::Plain | Mechanism::Login)
                                | (Protocol::Imap, Mechanism::Plain | Mechanism::Login)
                                | (Protocol::Pop3, Mechanism::UserPass)
                        )
                }),
            "invalid or duplicate mail authentication mechanism"
        );
        let mut addresses = HashSet::new();
        ensure!(
            (1..=64).contains(&self.backends.len())
                && self.backends.iter().all(|backend| {
                    let ip = backend.address.ip();
                    backend.address.port() != 0
                        && !ip.is_unspecified()
                        && !ip.is_multicast()
                        && (ip.is_loopback() || backend.tls.is_some())
                        && addresses.insert(backend.address)
                        && backend.dns_address.as_ref().is_none_or(|address|
                            dns_enabled && backend.tls.is_some() && crate::dns::valid_destination(address))
                }),
            "mail backends require distinct allowlist addresses and verified TLS for DNS or non-loopback destinations"
        );
        for backend in &self.backends {
            if let Some(tls) = &backend.tls {
                crate::config::validate_tls_versions(tls.versions.as_deref())?;
            }
        }
        Ok(())
    }
}
