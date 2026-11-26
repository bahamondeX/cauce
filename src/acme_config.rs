use crate::config::Config;
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::{collections::HashSet, path::PathBuf};

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub directory: String,
    pub directory_ca_file: Option<PathBuf>,
    pub state_dir: PathBuf,
    pub accept_terms: bool,
    pub contact: Option<String>,
    pub certificates: Vec<Vec<String>>,
    #[serde(default = "renew_before")]
    pub renew_before_seconds: u64,
    #[serde(default = "interval")]
    pub check_interval_seconds: u64,
}

fn renew_before() -> u64 {
    30 * 86_400
}
fn interval() -> u64 {
    3600
}

impl Settings {
    pub(crate) fn validate(&self, config: &Config) -> Result<()> {
        let url = reqwest::Url::parse(&self.directory)
            .map_err(|_| anyhow::anyhow!("invalid ACME directory URL"))?;
        ensure!(
            self.directory.len() <= 2048
                && url.scheme() == "https"
                && url.host().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.fragment().is_none()
                && self.accept_terms
                && self.state_dir.is_absolute()
                && self
                    .contact
                    .as_ref()
                    .is_none_or(|s| s.starts_with("mailto:")
                        && s.len() <= 256
                        && !s.chars().any(char::is_control))
                && (1..=90 * 86_400).contains(&self.renew_before_seconds)
                && (1..=86_400).contains(&self.check_interval_seconds),
            "invalid ACME directory, storage, acceptance or schedule"
        );
        ensure!(
            (1..=16).contains(&self.certificates.len())
                && self.certificates.iter().all(|group| {
                    let mut unique = HashSet::new();
                    (1..=16).contains(&group.len())
                        && group.iter().all(|name| {
                            name == &name.to_ascii_lowercase()
                                && !name.ends_with('.')
                                && name.contains('.')
                                && !name.contains('_')
                                && matches!(
                                    rustls::pki_types::ServerName::try_from(name.as_str()),
                                    Ok(rustls::pki_types::ServerName::DnsName(_))
                                )
                                && unique.insert(name)
                        })
                }),
            "ACME HTTP-01 requires bounded explicit DNS hostnames without wildcards"
        );
        let clear = config.listen.is_some() && config.tls.is_none()
            || config
                .listeners
                .iter()
                .any(|listener| listener.tls.is_none());
        let secure = config.tls.is_some()
            || config.http3.is_some()
            || config
                .listeners
                .iter()
                .any(|listener| listener.tls.is_some());
        ensure!(
            clear && secure,
            "ACME HTTP-01 requires both cleartext HTTP and HTTPS or QUIC"
        );
        Ok(())
    }
}
