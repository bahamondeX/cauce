use anyhow::{ensure, Context, Result};
use hyper::{
    header::{self, HeaderMap, HeaderName, HeaderValue},
    Response, StatusCode,
};

pub(super) fn header_end(bytes: &[u8], previous: usize) -> Option<usize> {
    let start = previous.min(bytes.len()).saturating_sub(3);
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

pub(super) fn response(bytes: &[u8], max_headers: usize) -> Result<Response<()>> {
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
            number <= max_headers && !matches!(line[0], b' ' | b'\t'),
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
    fn response_rejects_ambiguous_framing_and_origins() {
        for headers in [
            &b"Status: 101 Switching Protocols\r\n\r\n"[..],
            &b"HTTP/1.1 200 OK\r\nStatus: 201 Created\r\n\r\n"[..],
            &b"Content-Length: 1\r\nContent-Length: 1\r\n\r\n"[..],
            &b"X-Test: valid\r\n continuation\r\n\r\n"[..],
            &b"Content-Length: 1,2\r\n\r\n"[..],
            &b"Transfer-Encoding: chunked\r\n\r\n"[..],
        ] {
            assert!(response(headers, 64).is_err());
        }
        let reply = response(b"Status: 307 Temporary Redirect\nLocation: /a\n\n", 2).unwrap();
        assert_eq!(reply.status(), StatusCode::TEMPORARY_REDIRECT);
    }

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
