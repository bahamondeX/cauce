use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::path::{Component, Path};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub document_root: String,
    pub script: String,
    pub script_name: String,
    #[serde(default)]
    pub request_buffer_bytes: Option<u32>,
    #[serde(default)]
    pub keep_connection: bool,
}

impl Settings {
    pub(crate) fn validate(&self) -> Result<()> {
        let root = Path::new(&self.document_root);
        let script = Path::new(&self.script);
        ensure!(
            root.is_absolute()
                && self.document_root.len() <= 1024
                && !self.document_root.bytes().any(|b| b.is_ascii_control())
                && !root.components().any(|c| c == Component::ParentDir),
            "application document_root must be a literal absolute path"
        );
        ensure!(
            !self.script.is_empty()
                && self.script.len() <= 256
                && self.script.ends_with(".php")
                && self.script.split('/').all(|part| {
                    !part.is_empty()
                        && !matches!(part, "." | "..")
                        && part
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
                })
                && script
                    .components()
                    .all(|c| matches!(c, Component::Normal(_))),
            "FastCGI script must be a fixed relative PHP filename"
        );
        ensure!(
            self.script_name.starts_with('/')
                && self.script_name.len() <= 256
                && self
                    .script_name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b))
                && crate::proxy::normal_path(&self.script_name),
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
    pub(crate) settings: Settings,
    pub(super) script_path: String,
}

impl Route {
    pub(crate) fn compile(settings: &Settings) -> Self {
        let script = Path::new(&settings.document_root).join(&settings.script);
        Self {
            settings: settings.clone(),
            script_path: script
                .into_os_string()
                .into_string()
                .expect("validated UTF-8 script path"),
        }
    }
}
