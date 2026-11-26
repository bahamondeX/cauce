use super::config::Protocol;
use anyhow::{ensure, Result};
use hyper::{header, Request};
use std::net::IpAddr;

pub(super) const MAX_PARAMS: usize = 32 * 1024;

pub(super) fn params<B>(
    route: &super::config::Route,
    request: &Request<B>,
    client_ip: IpAddr,
    secure: bool,
    authority: &hyper::http::uri::Authority,
    length: u64,
) -> Result<Vec<u8>> {
    let mut encoded = Vec::new();
    let length = length.to_string();
    let remote_addr = client_ip.to_string();
    let server_port = authority
        .port_u16()
        .unwrap_or(if secure { 443 } else { 80 })
        .to_string();
    let request_uri = request
        .uri()
        .path_and_query()
        .map_or("/", |part| part.as_str());
    let mut append = |name: &[u8], value: &[u8]| -> Result<()> {
        ensure!(
            !value.contains(&0)
                && value.len() <= 16 * 1024
                && encoded.len() + name.len() + value.len() + 8 <= MAX_PARAMS,
            "invalid or oversized CGI parameters"
        );
        match route.protocol {
            Protocol::Fastcgi => {
                super::fastcgi::encode_length(name.len(), &mut encoded);
                super::fastcgi::encode_length(value.len(), &mut encoded);
                encoded.extend_from_slice(name);
                encoded.extend_from_slice(value);
            }
            Protocol::Uwsgi => {
                for field in [name, value] {
                    encoded.extend_from_slice(&(field.len() as u16).to_le_bytes());
                    encoded.extend_from_slice(field);
                }
            }
            Protocol::Scgi => {
                for field in [name, value] {
                    encoded.extend_from_slice(field);
                    encoded.push(0);
                }
            }
        }
        Ok(())
    };
    append(b"CONTENT_LENGTH", length.as_bytes())?;
    if route.protocol == Protocol::Scgi {
        append(b"SCGI", b"1")?;
    }
    let script_name = route.settings.script_name.as_deref().unwrap_or("");
    if let Some(path) = &route.script_path {
        append(b"SCRIPT_FILENAME", path.as_bytes())?;
        append(
            b"DOCUMENT_ROOT",
            route.settings.document_root.as_deref().unwrap().as_bytes(),
        )?;
        append(b"REDIRECT_STATUS", b"200")?;
    } else {
        let uri_path = request.uri().path();
        let path_info = uri_path
            .strip_prefix(script_name)
            .filter(|path| path.is_empty() || path.starts_with('/'))
            .unwrap_or(uri_path);
        append(b"PATH_INFO", path_info.as_bytes())?;
    }
    for (name, value) in [
        ("GATEWAY_INTERFACE", "CGI/1.1"),
        ("SCRIPT_NAME", script_name),
        ("REQUEST_URI", request_uri),
        ("QUERY_STRING", request.uri().query().unwrap_or("")),
        ("REQUEST_METHOD", request.method().as_str()),
        ("SERVER_PROTOCOL", "HTTP/1.1"),
        ("SERVER_NAME", authority.host()),
        ("SERVER_PORT", &server_port),
        ("REMOTE_ADDR", &remote_addr),
        ("REQUEST_SCHEME", if secure { "https" } else { "http" }),
        ("HTTPS", if secure { "on" } else { "off" }),
    ] {
        append(name.as_bytes(), value.as_bytes())?;
    }
    if let Some(content_type) = request.headers().get(header::CONTENT_TYPE) {
        ensure!(
            request
                .headers()
                .get_all(header::CONTENT_TYPE)
                .iter()
                .count()
                == 1,
            "duplicate CGI content type"
        );
        append(b"CONTENT_TYPE", content_type.as_bytes())?;
    }
    for name in request.headers().keys() {
        // Reject ambiguous aliases and httpoxy's Proxy environment variable.
        if matches!(
            name.as_str(),
            "proxy" | "content-type" | "content-length" | "te" | "trailer" | "expect" | "host"
        ) || !name
            .as_str()
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        {
            continue;
        }
        let param = format!(
            "HTTP_{}",
            name.as_str().to_ascii_uppercase().replace('-', "_")
        );
        let mut values = request.headers().get_all(name).iter().peekable();
        let first = values.next().expect("header key has a value");
        if values.peek().is_none() {
            append(param.as_bytes(), first.as_bytes())?;
        } else {
            let separator: &[u8] = if name == header::COOKIE { b"; " } else { b", " };
            let mut joined = first.as_bytes().to_vec();
            for value in values {
                ensure!(
                    joined.len() + separator.len() + value.len() <= 16 * 1024,
                    "CGI header value too large"
                );
                joined.extend_from_slice(separator);
                joined.extend_from_slice(value.as_bytes());
            }
            append(param.as_bytes(), &joined)?;
        }
    }
    append(b"HTTP_HOST", authority.as_str().as_bytes())?;
    Ok(encoded)
}
