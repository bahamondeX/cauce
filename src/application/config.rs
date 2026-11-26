use anyhow::{ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Component, Path};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Protocol {
    Fastcgi,
    Uwsgi,
    Scgi,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub document_root: Option<String>,
    pub script: Option<String>,
    pub script_name: Option<String>,
    pub request_buffer_bytes: Option<u32>,
    #[serde(default)]
    pub keep_connection: bool,
    pub modifier1: Option<u8>,
    pub modifier2: Option<u8>,
}

impl Settings {
    pub(crate) fn validate(&self, protocol: Protocol) -> Result<()> {
        if protocol == Protocol::Fastcgi {
            let root = self
                .document_root
                .as_deref()
                .context("FastCGI requires document_root")?;
            let path = Path::new(root);
            let script = self.script.as_deref().context("FastCGI requires script")?;
            ensure!(
                path.is_absolute()
                    && root.len() <= 1024
                    && !root.bytes().any(|b| b.is_ascii_control())
                    && !path.components().any(|c| c == Component::ParentDir),
                "FastCGI document_root must be a literal absolute path"
            );
            ensure!(
                !script.is_empty()
                    && script.len() <= 256
                    && script.ends_with(".php")
                    && script.split('/').all(|part| {
                        !part.is_empty()
                            && !matches!(part, "." | "..")
                            && part
                                .bytes()
                                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
                    })
                    && Path::new(script)
                        .components()
                        .all(|c| matches!(c, Component::Normal(_))),
                "FastCGI script must be a fixed relative PHP filename"
            );
            ensure!(
                self.script_name.is_some(),
                "FastCGI requires a fixed script_name"
            );
        } else {
            ensure!(
                self.document_root.is_none() && self.script.is_none() && !self.keep_connection,
                "script mapping and keep_connection require FastCGI"
            );
        }
        ensure!(
            protocol == Protocol::Uwsgi || (self.modifier1.is_none() && self.modifier2.is_none()),
            "modifier1 and modifier2 require uWSGI"
        );
        ensure!(
            self.script_name
                .as_ref()
                .is_none_or(|name| name.starts_with('/')
                    && name.len() <= 256
                    && name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b))
                    && crate::proxy::normal_path(name)),
            "invalid fixed CGI script_name"
        );
        ensure!(
            self.request_buffer_bytes
                .is_none_or(|n| (1..=8 * 1024 * 1024).contains(&n)),
            "application request buffer must be 1 byte to 8 MiB"
        );
        Ok(())
    }
}

pub(crate) struct Route {
    pub(super) protocol: Protocol,
    pub(crate) settings: Settings,
    pub(super) script_path: Option<String>,
}

impl Route {
    pub(crate) fn compile(protocol: Protocol, settings: &Settings) -> Self {
        let script_path = settings
            .document_root
            .as_ref()
            .zip(settings.script.as_ref())
            .map(|(root, script)| {
                Path::new(root)
                    .join(script)
                    .into_os_string()
                    .into_string()
                    .expect("validated UTF-8 script path")
            });
        Self {
            protocol,
            settings: settings.clone(),
            script_path,
        }
    }
}
