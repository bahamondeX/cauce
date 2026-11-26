use crate::config::Limits;
use anyhow::{ensure, Context, Result};
use hyper::{
    header::{self, HeaderMap, HeaderName, HeaderValue},
    Request, Response, StatusCode,
};
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
        super::fastcgi::encode_length(name.len(), &mut encoded);
        super::fastcgi::encode_length(value.len(), &mut encoded);
        encoded.extend_from_slice(name);
        encoded.extend_from_slice(value);
        Ok(())
    };
    for (name, value) in [
        ("GATEWAY_INTERFACE", "CGI/1.1"),
        ("SCRIPT_FILENAME", &route.script_path),
        ("SCRIPT_NAME", &route.settings.script_name),
        ("DOCUMENT_ROOT", &route.settings.document_root),
        ("REQUEST_URI", request_uri),
        ("QUERY_STRING", request.uri().query().unwrap_or("")),
        ("REQUEST_METHOD", request.method().as_str()),
        ("SERVER_PROTOCOL", "HTTP/1.1"),
        ("SERVER_NAME", authority.host()),
        ("SERVER_PORT", &server_port),
        ("REMOTE_ADDR", &remote_addr),
        ("REQUEST_SCHEME", if secure { "https" } else { "http" }),
        ("HTTPS", if secure { "on" } else { "off" }),
        ("REDIRECT_STATUS", "200"),
        ("CONTENT_LENGTH", &length),
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

pub(super) fn header_end(bytes: &[u8], previous: usize) -> Option<usize> {
    let start = previous.saturating_sub(3);
    bytes[start..].iter().enumerate().find_map(|(i, byte)| {
        if *byte != b'\n' {
            return None;
        }
        let after = start + i + 1;
        bytes.get(after..).and_then(|tail| {
            if tail.starts_with(b"\r\n") {
                Some(after + 2)
            } else if tail.starts_with(b"\n") {
                Some(after + 1)
            } else {
                None
            }
        })
    })
}

pub(super) fn response(bytes: &[u8], limits: &Limits) -> Result<Response<()>> {
    let mut headers = HeaderMap::new();
    let mut status = None;
    let mut number = 0;
    for (index, line) in bytes.split(|byte| *byte == b'\n').enumerate() {
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        if line.is_empty() {
            break;
        }
        if index == 0 && line.starts_with(b"HTTP/") {
            let (version, code) = line
                .split_at_checked(9)
                .context("invalid CGI HTTP status line")?;
            ensure!(
                matches!(version, b"HTTP/1.0 " | b"HTTP/1.1 "),
                "invalid CGI HTTP version"
            );
            status = Some(status_code(code)?);
            continue;
        }
        number += 1;
        ensure!(
            number <= limits.max_headers && !matches!(line[0], b' ' | b'\t'),
            "invalid CGI header count or folding"
        );
        let colon = line
            .iter()
            .position(|b| *b == b':')
            .context("malformed CGI header")?;
        let name = HeaderName::from_bytes(&line[..colon])?;
        let input = trim_ows(&line[colon + 1..]);
        let value = HeaderValue::from_bytes(input)?;
        if name == "status" {
            ensure!(status.is_none(), "duplicate CGI status");
            status = Some(status_code(input)?);
        } else {
            ensure!(
                !matches!(name.as_str(), "transfer-encoding" | "upgrade" | "trailer"),
                "unsupported CGI response transfer framing"
            );
            headers.append(name, value);
        }
    }
    ensure!(
        number != 0 || status.is_some(),
        "empty CGI response headers"
    );
    content_length(&headers)?;
    let status = status.unwrap_or(if headers.contains_key(header::LOCATION) {
        StatusCode::FOUND
    } else {
        StatusCode::OK
    });
    let mut response = Response::new(());
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    Ok(response)
}

fn status_code(input: &[u8]) -> Result<StatusCode> {
    ensure!(
        input.len() >= 3 && input.get(3).is_none_or(|b| *b == b' '),
        "invalid CGI status"
    );
    let status = StatusCode::from_bytes(&input[..3])?;
    ensure!(
        (200..=599).contains(&status.as_u16()),
        "unsupported CGI status"
    );
    Ok(status)
}

pub(super) fn content_length(headers: &HeaderMap) -> Result<Option<u64>> {
    let mut lengths = headers.get_all(header::CONTENT_LENGTH).iter();
    let Some(first) = lengths.next() else {
        return Ok(None);
    };
    ensure!(
        lengths.next().is_none() && first.as_bytes().iter().all(u8::is_ascii_digit),
        "ambiguous CGI content length"
    );
    Ok(Some(first.to_str()?.parse()?))
}

fn trim_ows(mut input: &[u8]) -> &[u8] {
    while input
        .first()
        .is_some_and(|byte| matches!(byte, b' ' | b'\t'))
    {
        input = &input[1..];
    }
    while input
        .last()
        .is_some_and(|byte| matches!(byte, b' ' | b'\t'))
    {
        input = &input[..input.len() - 1];
    }
    input
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fragmented_head_boundary() {
        let wire = b"Status: 201 Created\r\nSet-Cookie: a=b\r\n\r\nbody";
        assert_eq!(header_end(wire, wire.len() - 8), Some(wire.len() - 4));
        let redirect = b"Location: /target\n\n";
        assert_eq!(
            header_end(redirect, redirect.len() - 1),
            Some(redirect.len())
        );
    }
}
