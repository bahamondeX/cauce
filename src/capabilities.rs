use crate::{config::Config, content::Handler};
use anyhow::{ensure, Result};
use serde_json::{json, Value};

pub(crate) fn report() -> Value {
    json!({
        "version": env!("CARGO_PKG_VERSION"),
        "http1": true,
        "http2": true,
        "grpc": true,
        "tls": true,
        "metrics": true,
        "dns": true,
        "static_files": cfg!(feature = "web"),
        "compression": cfg!(feature = "web"),
        "memory_cache": cfg!(feature = "web"),
        "tcp": cfg!(feature = "stream"),
        "udp": cfg!(feature = "stream"),
        "stream_tls": cfg!(feature = "stream"),
        "http3": cfg!(feature = "quic"),
        "fastcgi": cfg!(feature = "application"),
        "uwsgi": cfg!(feature = "application"),
        "scgi": cfg!(feature = "application"),
        "mail": cfg!(feature = "mail"),
        "acme": cfg!(feature = "acme"),
        "disk_cache": cfg!(feature = "web"),
    })
}

pub(crate) fn validate(config: &Config) -> Result<()> {
    ensure!(
        cfg!(feature = "quic") || config.http3.is_none(),
        "http3 requires the quic build feature"
    );
    ensure!(
        cfg!(feature = "stream") || config.stream.is_none(),
        "stream requires the stream build feature"
    );
    ensure!(
        cfg!(feature = "mail") || config.mail.is_empty(),
        "mail requires the mail build feature"
    );
    ensure!(
        cfg!(feature = "application")
            || config
                .upstreams
                .iter()
                .all(|u| u.protocol.application().is_none()),
        "application gateway configuration requires the application build feature"
    );
    ensure!(
        cfg!(feature = "acme") || config.acme.is_none(),
        "ACME requires the acme build feature"
    );
    if !cfg!(feature = "web") {
        for route in &config.routes {
            ensure!(
                route.cache.is_none(),
                "HTTP cache requires the web build feature"
            );
            ensure!(
                route.compression.is_none(),
                "compression requires the web build feature"
            );
            ensure!(
                !matches!(route.content.as_deref(), Some(Handler::Static(_))),
                "static content requires the web build feature"
            );
        }
    }
    Ok(())
}

pub(crate) fn explain(config: &Config) -> Value {
    json!({
        "capabilities": report(),
        "http_tcp_listeners": usize::from(config.listen.is_some()) + config.listeners.len(),
        "http3_listeners": usize::from(config.http3.is_some()),
        "stream_tcp_listeners": config.stream.as_ref().map_or(0, |s| s.tcp.len() + s.tls_passthrough.len()),
        "stream_udp_listeners": config.stream.as_ref().map_or(0, |s| s.udp.len()),
        "mail_listeners": config.mail.len(),
        "http_routes": config.routes.len(),
        "upstreams": config.upstreams.len(),
        "disk_cache_routes": config.routes.iter().filter(|r|
            r.cache.as_ref().is_some_and(|cache| cache.disk.is_some())).count(),
        "acme_managed_groups": config.acme.as_ref().map_or(0, |acme| acme.certificates.len()),
        "upstream_dns": if config.dns.is_some() { "configured_recursive" } else { "system" },
    })
}
