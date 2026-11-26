use crate::{
    body::{self, Completion, ProxyBody},
    config::{Limits, UpstreamProtocol},
    diagnostics,
    pool::{ms, Generation, Pool},
    telemetry::{self, Attempt, Event, Logger},
};
use hyper::{
    body::{Body, Incoming},
    header::{
        HeaderMap, HeaderName, HeaderValue, CONNECTION, CONTENT_LENGTH, HOST, TRANSFER_ENCODING,
    },
    Request, Response, StatusCode,
};
use std::{
    borrow::Cow,
    convert::Infallible,
    net::IpAddr,
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicU8, Ordering},
        Arc, RwLock,
    },
    time::{Duration, Instant},
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

pub struct State {
    pub generation: RwLock<Arc<Generation>>,
    pub pool: Arc<Pool>,
    pub logger: Logger,
    pub requests: Arc<Semaphore>,
    pub clients: Arc<Semaphore>,
    pub ready: AtomicBool,
    pub instance_id: String,
    pub admin_token: Option<String>,
    pub tunnels: crate::websocket::Tunnels,
}
fn reply(status: StatusCode, reason: &'static str) -> Response<ProxyBody> {
    let mut r = Response::new(body::full(reason));
    *r.status_mut() = status;
    r
}
const HOP_HEADERS: [HeaderName; 7] = [
    CONNECTION,
    HeaderName::from_static("keep-alive"),
    hyper::header::PROXY_AUTHENTICATE,
    hyper::header::PROXY_AUTHORIZATION,
    TRANSFER_ENCODING,
    hyper::header::UPGRADE,
    HeaderName::from_static("proxy-connection"),
];
const TE: HeaderName = hyper::header::TE;
const CONTENT_TYPE: HeaderName = hyper::header::CONTENT_TYPE;
const X_REQUEST_ID: HeaderName = HeaderName::from_static("x-request-id");
const X_FORWARDED_FOR: HeaderName = HeaderName::from_static("x-forwarded-for");
const X_FORWARDED_PROTO: HeaderName = HeaderName::from_static("x-forwarded-proto");

fn headers_exceed_limits(headers: &HeaderMap, limits: &Limits) -> bool {
    headers.len() > limits.max_headers
        || headers
            .iter()
            .map(|(key, value)| key.as_str().len() + value.len() + 4)
            .sum::<usize>()
            > limits.max_header_bytes
}

fn strip_hop(headers: &mut HeaderMap) {
    let named: Vec<_> = headers
        .get_all(CONNECTION)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .filter_map(|v| HeaderName::from_bytes(v.trim().as_bytes()).ok())
        .collect();
    for name in named {
        headers.remove(name);
    }
    for name in &HOP_HEADERS {
        headers.remove(name);
    }
    // TE is the sole connection-specific field permitted by HTTP/2, and only
    // with the trailers value. Retain it for gRPC and HTTP/1 trailer forwarding.
    if headers.get_all(&TE).iter().count() != 1
        || !headers
            .get(&TE)
            .is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"trailers"))
    {
        headers.remove(&TE);
    }
}
fn grpc_request(headers: &HeaderMap) -> bool {
    headers
        .get(&CONTENT_TYPE)
        .and_then(|h| h.to_str().ok())
        .is_some_and(|s| {
            let s = s.split(';').next().unwrap_or("").trim();
            s == "application/grpc" || s.starts_with("application/grpc+")
        })
}
fn grpc_timeout(value: &str) -> Option<Duration> {
    if !(2..=9).contains(&value.len()) || !value.is_ascii() {
        return None;
    }
    let (digits, unit) = value.split_at(value.len() - 1);
    if !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let n: u64 = digits.parse().ok()?;
    Some(match unit {
        "H" => Duration::from_secs(n.checked_mul(3600)?),
        "M" => Duration::from_secs(n.checked_mul(60)?),
        "S" => Duration::from_secs(n),
        "m" => Duration::from_millis(n),
        "u" => Duration::from_micros(n),
        "n" => Duration::from_nanos(n),
        _ => return None,
    })
}
fn remaining_grpc_timeout(duration: Duration) -> String {
    // Round up to avoid shortening a caller's deadline through unit conversion.
    let nanos = duration.as_nanos();
    for (unit, size) in [
        ("n", 1),
        ("u", 1000),
        ("m", 1_000_000),
        ("S", 1_000_000_000),
        ("M", 60_000_000_000),
        ("H", 3_600_000_000_000),
    ] {
        let value = nanos.div_ceil(size);
        if value <= 99_999_999 {
            return format!("{value}{unit}");
        }
    }
    "99999999H".into()
}
fn invalid_framing(headers: &HeaderMap) -> bool {
    (headers.contains_key(CONTENT_LENGTH) && headers.contains_key(TRANSFER_ENCODING))
        || headers.get_all(CONTENT_LENGTH).iter().count() > 1
        || headers.get_all(TRANSFER_ENCODING).iter().count() > 1
        || headers
            .get(TRANSFER_ENCODING)
            .is_some_and(|v| v.as_bytes() != b"chunked")
        || headers.get_all(CONNECTION).iter().any(|v| {
            v.to_str().is_err()
                || v.to_str().unwrap().split(',').any(|x| {
                    ["host", "content-length", "transfer-encoding"]
                        .contains(&x.trim().to_ascii_lowercase().as_str())
                })
        })
}
pub(crate) fn normal_path(path: &str) -> bool {
    // Routing rejects ambiguous encodings rather than guessing backend normalization.
    if path.contains(['\\', '#']) || path.contains("//") {
        return false;
    }
    if path.split('%').skip(1).any(|encoded| {
        let Some([high, low]) = encoded.as_bytes().get(..2) else {
            return false;
        };
        matches!(
            (*high, low.to_ascii_lowercase()),
            (b'2', b'f' | b'e' | b'5') | (b'5', b'c') | (b'0', b'0' | b'a' | b'd')
        )
    }) {
        return false;
    }
    !path
        .split('/')
        .any(|segment| segment == "." || segment == "..")
}
fn record_error(
    state: &State,
    event: Option<Event>,
    started: Instant,
    status: StatusCode,
    reason: &'static str,
) -> Response<ProxyBody> {
    state
        .logger
        .metrics
        .rejected
        .fetch_add(1, Ordering::Relaxed);
    if let Some(mut event) = event {
        event.status = status.as_u16();
        event.duration_ms = ms(started);
        event.outcome = "rejected";
        event.error_direction = Some(if reason == "headers_timeout" {
            "upstream_or_downstream"
        } else if reason.starts_with("request_body_") {
            "downstream"
        } else if status == StatusCode::BAD_GATEWAY || status == StatusCode::GATEWAY_TIMEOUT {
            "upstream"
        } else {
            "proxy"
        });
        event.error_category = Some(if status == StatusCode::SERVICE_UNAVAILABLE {
            "capacity"
        } else {
            "request_or_transport"
        });
        event.error_phase = Some(reason);
        event.reason = Some(reason);
        state.logger.emit(event);
    }
    reply(status, reason)
}
fn record_grpc_deadline(
    state: &State,
    event: Option<Event>,
    started: Instant,
) -> Response<ProxyBody> {
    // A trailers-only gRPC response carries status in the initial HEADERS with
    // END_STREAM, and must use HTTP 200 rather than an HTTP gateway error.
    let mut response = record_error(state, event, started, StatusCode::OK, "request_deadline");
    *response.body_mut() = body::full("");
    *response.version_mut() = hyper::Version::HTTP_2;
    response
        .headers_mut()
        .insert("content-type", HeaderValue::from_static("application/grpc"));
    response
        .headers_mut()
        .insert("grpc-status", HeaderValue::from_static("4"));
    response.headers_mut().insert(
        "grpc-message",
        HeaderValue::from_static("proxy deadline exceeded"),
    );
    response
}
pub async fn handle(
    mut req: Request<Incoming>,
    peer_ip: IpAddr,
    peer: HeaderValue,
    state: Arc<State>,
    secure: bool,
    client_slot: Arc<OwnedSemaphorePermit>,
    downstream: Option<u64>,
) -> Result<Response<ProxyBody>, Infallible> {
    let started = Instant::now();
    let generation = state.generation.read().unwrap().clone();
    let limits = &generation.config.limits;
    let mut deadline = tokio::time::Instant::now() + Duration::from_millis(limits.request_ms);
    let inbound_version = req.version();
    let is_h2 = inbound_version == hyper::Version::HTTP_2;
    let grpc = grpc_request(req.headers());
    let id = uuid::Uuid::new_v4().to_string();
    diagnostics::emit("request_started", || {
        let offer = req
            .headers()
            .get("x-cauce-bench-offer")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<u64>().ok());
        serde_json::json!({
            "downstream": downstream, "request_id": id, "offer": offer,
            "generation": generation.version, "protocol": if is_h2 { "http2" } else { "http1" },
        })
    });
    let trace = req
        .headers()
        .get("traceparent")
        .filter(|h| h.to_str().is_ok_and(telemetry::traceparent))
        .cloned();
    let mut event = state.logger.enabled().then(|| Event {
        schema_version: "1.1",
        event_id: uuid::Uuid::new_v4(),
        timestamp: time::OffsetDateTime::now_utc(),
        instance_id: state.instance_id.clone(),
        config_version: generation.version,
        request_id: id.clone(),
        traceparent: trace
            .as_ref()
            .and_then(|h| h.to_str().ok())
            .map(str::to_owned),
        route_id: None,
        upstream_id: None,
        method: match *req.method() {
            hyper::Method::GET => "GET",
            hyper::Method::POST => "POST",
            hyper::Method::PUT => "PUT",
            hyper::Method::DELETE => "DELETE",
            hyper::Method::PATCH => "PATCH",
            hyper::Method::HEAD => "HEAD",
            hyper::Method::OPTIONS => "OPTIONS",
            _ => "OTHER",
        },
        path: None,
        protocol: if is_h2 { "HTTP/2" } else { "HTTP/1.1" },
        status: 0,
        duration_ms: 0.0,
        transfer_ms: None,
        request_bytes: 0,
        response_bytes: 0,
        outcome: "pending",
        error_direction: None,
        error_category: None,
        error_phase: None,
        reason: None,
        backpressure_observations: None,
        attempts: vec![],
    });
    state
        .logger
        .metrics
        .requests
        .fetch_add(1, Ordering::Relaxed);
    macro_rules! reject {
        ($status:ident, $reason:literal) => {
            return Ok(record_error(
                &state,
                event,
                started,
                StatusCode::$status,
                $reason,
            ))
        };
    }
    if !state.ready.load(Ordering::Acquire) {
        reject!(SERVICE_UNAVAILABLE, "draining");
    }
    let slot = match state.requests.clone().try_acquire_owned() {
        Ok(p) => Arc::new(p),
        Err(_) => {
            reject!(SERVICE_UNAVAILABLE, "request_capacity");
        }
    };
    if req.method() == hyper::Method::CONNECT
        || (!is_h2 && (req.uri().authority().is_some() || req.uri().scheme().is_some()))
    {
        reject!(BAD_REQUEST, "origin_form_required");
    }
    if inbound_version != hyper::Version::HTTP_11 && !is_h2 {
        reject!(HTTP_VERSION_NOT_SUPPORTED, "http_11_or_2_required");
    }
    if is_h2
        && (req.uri().scheme_str() != Some(if secure { "https" } else { "http" })
            || [
                "connection",
                "keep-alive",
                "proxy-connection",
                "transfer-encoding",
                "upgrade",
            ]
            .iter()
            .any(|h| req.headers().contains_key(*h))
            || req
                .headers()
                .get_all("te")
                .iter()
                .any(|h| !h.as_bytes().eq_ignore_ascii_case(b"trailers")))
    {
        reject!(BAD_REQUEST, "invalid_http2_headers");
    }
    if grpc {
        if !is_h2 {
            reject!(HTTP_VERSION_NOT_SUPPORTED, "grpc_requires_http2");
        }
        if req.headers().get_all("grpc-timeout").iter().count() > 1 {
            reject!(BAD_REQUEST, "invalid_grpc_timeout");
        }
        if let Some(value) = req.headers().get("grpc-timeout") {
            let Some(timeout) = value.to_str().ok().and_then(grpc_timeout) else {
                reject!(BAD_REQUEST, "invalid_grpc_timeout");
            };
            deadline = deadline.min(
                tokio::time::Instant::now() + timeout.min(Duration::from_millis(limits.request_ms)),
            );
        }
    }
    if invalid_framing(req.headers()) {
        reject!(BAD_REQUEST, "ambiguous_framing");
    }
    let hard_deadline = deadline
        + if grpc {
            crate::transport::GRPC_STATUS_GRACE
        } else {
            Duration::ZERO
        };
    crate::transport::tighten_stream_deadline(hard_deadline);
    if headers_exceed_limits(req.headers(), limits) {
        reject!(REQUEST_HEADER_FIELDS_TOO_LARGE, "header_limit");
    }
    let host_count = req.headers().get_all(HOST).iter().count();
    if (!is_h2 && host_count != 1) || (is_h2 && host_count > 1) {
        reject!(BAD_REQUEST, "single_host_required");
    }
    let header_host = req.headers().get(HOST).and_then(|h| h.to_str().ok());
    let authority = if is_h2 {
        req.uri().authority().map(|a| a.as_str())
    } else {
        None
    };
    if authority
        .zip(header_host)
        .is_some_and(|(a, h)| !a.eq_ignore_ascii_case(h))
    {
        reject!(BAD_REQUEST, "conflicting_authority");
    }
    let host = authority.or(header_host).unwrap_or("");
    if host.is_empty() || host.contains('@') {
        reject!(BAD_REQUEST, "single_host_required");
    }
    let host = if host.bytes().any(|byte| byte.is_ascii_uppercase()) {
        Cow::Owned(host.to_ascii_lowercase())
    } else {
        Cow::Borrowed(host)
    };
    let websocket = match crate::websocket::request(&req) {
        Ok(h) => h,
        Err(reason) => {
            return Ok(record_error(
                &state,
                event,
                started,
                if reason == "unsupported_upgrade" {
                    StatusCode::NOT_IMPLEMENTED
                } else {
                    StatusCode::BAD_REQUEST
                },
                reason,
            ))
        }
    };
    if !normal_path(req.uri().path()) {
        reject!(BAD_REQUEST, "ambiguous_path");
    }
    let identity = generation
        .config
        .real_ip
        .as_ref()
        .map(|policy| policy.resolve(peer_ip, req.headers()));
    if let Some(identity) = &identity {
        diagnostics::emit(
            "client_identity",
            || serde_json::json!({"peer": peer_ip, "client": identity.client, "request_id": id}),
        );
    }
    let mut route = match generation.route(&host, req.uri().path()) {
        Some(route) => route,
        None => {
            reject!(NOT_FOUND, "route_not_found");
        }
    };
    if route.action.is_some() {
        let mut rewritten: Option<hyper::http::uri::PathAndQuery> = None;
        for hop in 0..=10 {
            let Some(action) = &route.action else { break };
            let (path, args) = rewritten.as_ref().map_or_else(
                || (req.uri().path(), req.uri().query()),
                |uri| (uri.path(), uri.query()),
            );
            match action.apply(path, args) {
                Err(()) => reject!(BAD_REQUEST, "invalid_rewrite_destination"),
                Ok(None) => break,
                Ok(Some(crate::actions::Outcome::Redirect(status, location))) => {
                    if let Some(mut event) = event {
                        event.route_id = Some(route.id.clone());
                        event.status = status.as_u16();
                        event.outcome = "success";
                        event.duration_ms = ms(started);
                        state.logger.emit(event);
                    }
                    state
                        .logger
                        .metrics
                        .responses
                        .fetch_add(1, Ordering::Relaxed);
                    let mut response = reply(status, "");
                    response
                        .headers_mut()
                        .insert(hyper::header::LOCATION, location);
                    response
                        .headers_mut()
                        .insert(X_REQUEST_ID, HeaderValue::from_str(&id).unwrap());
                    return Ok(response);
                }
                Ok(Some(outcome)) => {
                    if hop == 10 {
                        reject!(INTERNAL_SERVER_ERROR, "internal_redirect_limit");
                    }
                    let next = match outcome {
                        crate::actions::Outcome::Rewrite(target) => {
                            let next = generation.route(&host, target.path());
                            rewritten = Some(target);
                            next
                        }
                        crate::actions::Outcome::Named(name) => {
                            generation.named_route(&route.host, name)
                        }
                        crate::actions::Outcome::Redirect(..) => unreachable!(),
                    };
                    let Some(next) = next else {
                        reject!(NOT_FOUND, "route_not_found")
                    };
                    route = next;
                }
            }
        }
        if let Some(target) = rewritten {
            let mut parts = req.uri().clone().into_parts();
            parts.path_and_query = Some(target);
            *req.uri_mut() = hyper::Uri::from_parts(parts).expect("validated internal URI");
        }
    }
    if let Some(event) = &mut event {
        event.route_id = Some(route.id.clone());
        event.path = (!route.path_prefix.is_empty()).then(|| route.path_prefix.clone());
    }
    let downstream_upgrade = websocket.as_ref().map(|_| hyper::upgrade::on(&mut req));
    if req
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.parse::<u64>().ok())
        .is_some_and(|n| n > limits.max_body_bytes)
    {
        reject!(PAYLOAD_TOO_LARGE, "body_limit");
    }
    let endpoint = match generation.select(route) {
        Some(e) => e,
        None => {
            reject!(SERVICE_UNAVAILABLE, "no_healthy_upstream");
        }
    };
    diagnostics::emit("route_selected", || {
        serde_json::json!({
            "request_id": id, "route": route.id, "endpoint": endpoint.config.id,
            "downstream": downstream,
        })
    });
    if let Some(event) = &mut event {
        event.upstream_id = Some(endpoint.config.id.clone());
    }
    let upstream_h2 = endpoint.config.protocol == UpstreamProtocol::Http2;
    if grpc && !upstream_h2 {
        reject!(BAD_GATEWAY, "grpc_upstream_requires_http2");
    }
    if websocket.is_some() && upstream_h2 {
        reject!(NOT_IMPLEMENTED, "websocket_requires_http1_upstream");
    }
    diagnostics::emit("upstream_acquire_started", || {
        serde_json::json!({
            "request_id": id, "endpoint": endpoint.config.id,
            "remaining_us": deadline.saturating_duration_since(tokio::time::Instant::now()).as_micros() as u64,
            "acquire_ms": limits.acquire_ms,
        })
    });
    let acquired = tokio::time::timeout_at(
        deadline,
        state
            .pool
            .acquire(generation.clone(), endpoint.clone(), event.is_some()),
    )
    .await;
    let (mut lease, attempt) = match acquired {
        Ok(Ok(pair)) => pair,
        failure => {
            diagnostics::emit("upstream_acquire_failed", || {
                serde_json::json!({
                    "request_id": id, "endpoint": endpoint.config.id,
                    "error": match &failure {
                        Ok(Err(error)) => diagnostics::chain(error.as_ref()),
                        Err(error) => diagnostics::chain(error),
                        Ok(Ok(_)) => unreachable!(),
                    },
                })
            });
            let reason = match failure {
                Err(_) => "request_deadline",
                Ok(Err(error)) => match error.to_string().as_str() {
                    "pool_capacity_timeout" => "pool_capacity_timeout",
                    "connect_capacity_timeout" => "connect_capacity_timeout",
                    "dns_timeout" => "dns_timeout",
                    "dns_no_answers" => "dns_no_answers",
                    "tcp_connect_timeout" => "tcp_connect_timeout",
                    "unix_connect_timeout" => "unix_connect_timeout",
                    "tls_handshake_timeout" => "tls_handshake_timeout",
                    "tls_handshake_error" => "tls_handshake_error",
                    _ => "upstream_connect_error",
                },
                _ => unreachable!(),
            };
            if let Some(event) = &mut event {
                event.attempts.push(Attempt {
                    number: 1,
                    outcome: reason,
                    connection_id: None,
                    reused: None,
                    pool_wait_ms: None,
                    connect_gate_ms: None,
                    dns_ms: None,
                    tcp_ms: None,
                    tls_ms: None,
                    headers_ms: None,
                });
            }
            let status = if ["pool_capacity_timeout", "connect_capacity_timeout"].contains(&reason)
            {
                StatusCode::SERVICE_UNAVAILABLE
            } else if reason.ends_with("timeout") || reason == "request_deadline" {
                StatusCode::GATEWAY_TIMEOUT
            } else {
                StatusCode::BAD_GATEWAY
            };
            if grpc && reason == "request_deadline" {
                return Ok(record_grpc_deadline(&state, event, started));
            }
            return Ok(record_error(&state, event, started, status, reason));
        }
    };
    diagnostics::emit("upstream_lease_acquired", || {
        serde_json::json!({
            "request_id": id, "connection": lease.connection_id(), "endpoint": endpoint.config.id,
        })
    });
    lease.arm_deadline(hard_deadline);
    if let Some(event) = &mut event {
        event.attempts.extend(attempt);
    }
    let path = req
        .uri()
        .path_and_query()
        .map(|p| p.as_str())
        .unwrap_or("/");
    let uri = if upstream_h2 {
        format!(
            "{}://{}{}",
            if endpoint.config.tls.is_some() {
                "https"
            } else {
                "http"
            },
            endpoint.config.host,
            path
        )
    } else {
        path.to_owned()
    };
    *req.uri_mut() = uri.parse().expect("validated authority and request path");
    *req.version_mut() = if upstream_h2 {
        hyper::Version::HTTP_2
    } else {
        hyper::Version::HTTP_11
    };
    strip_hop(req.headers_mut());
    if websocket.is_some() {
        req.headers_mut()
            .insert(CONNECTION, HeaderValue::from_static("upgrade"));
        req.headers_mut()
            .insert("upgrade", HeaderValue::from_static("websocket"));
        // Extensions aren't negotiated by this increment; endpoints use base RFC6455 frames.
        req.headers_mut().remove("sec-websocket-extensions");
        req.headers_mut().remove(CONTENT_LENGTH);
    }
    // Convert trusted input to a canonical chain after stripping forwarding fields.
    let forwarded: Vec<_> = req
        .headers()
        .keys()
        .filter(|k| k.as_str() == "forwarded" || k.as_str().starts_with("x-forwarded-"))
        .cloned()
        .collect();
    for k in forwarded {
        req.headers_mut().remove(k);
    }
    req.headers_mut().remove("traceparent");
    req.headers_mut().remove("tracestate");
    if let Some(t) = trace {
        req.headers_mut().insert("traceparent", t);
    }
    req.headers_mut().insert(HOST, endpoint.host.clone());
    req.headers_mut().remove("x-real-ip");
    req.headers_mut().insert(
        X_FORWARDED_FOR,
        identity.and_then(|value| value.forwarded).unwrap_or(peer),
    );
    req.headers_mut().insert(
        X_FORWARDED_PROTO,
        HeaderValue::from_static(if secure { "https" } else { "http" }),
    );
    let request_id_header = HeaderValue::from_str(&id).unwrap();
    req.headers_mut()
        .insert(X_REQUEST_ID, request_id_header.clone());
    if grpc {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Ok(record_grpc_deadline(&state, event, started));
        }
        req.headers_mut().insert(
            "grpc-timeout",
            HeaderValue::from_str(&remaining_grpc_timeout(remaining)).unwrap(),
        );
        req.headers_mut()
            .insert("te", HeaderValue::from_static("trailers"));
    }
    if let Some(policy) = &route.headers {
        if policy.request.apply(req.headers_mut()) && headers_exceed_limits(req.headers(), limits) {
            reject!(REQUEST_HEADER_FIELDS_TOO_LARGE, "header_policy_limit");
        }
    }
    let uploaded = Arc::new(AtomicBool::new(false));
    let upload_error = Arc::new(AtomicU8::new(0));
    let request_bytes = Arc::new(AtomicU64::new(0));
    let exchange = diagnostics::enabled().then(|| diagnostics::Exchange {
        request_id: Arc::from(id.as_str()),
        downstream,
        upstream: lease.connection_id(),
    });
    let upload_guard = lease.upload_guard();
    let upstream_req = req.map(|b| {
        if websocket.is_some() || (upstream_h2 && b.is_end_stream()) {
            uploaded.store(true, Ordering::Release);
            if let Some(exchange) = &exchange {
                exchange.stage("upstream_upload_handoff", "empty");
            }
            return body::full("");
        }
        body::upload(
            b,
            limits,
            deadline,
            body::UploadState {
                count: request_bytes.clone(),
                done: uploaded.clone(),
                error: upload_error.clone(),
                upload_guard,
                request_slot: slot.clone(),
                exchange: exchange.clone(),
            },
        )
    });
    let headers_at = Instant::now();
    let header_start = tokio::time::Instant::now();
    let header_deadline = (header_start + Duration::from_millis(limits.headers_ms)).min(deadline);
    let response = tokio::time::timeout_at(
        header_deadline,
        lease.send_request(upstream_req, exchange.as_ref()),
    )
    .await;
    let mut response = match response {
        Ok(Ok(r)) => r,
        other => {
            diagnostics::emit("upstream_headers_failed", || {
                serde_json::json!({
                    "request_id": id, "connection": lease.connection_id(),
                    "budget_us": header_deadline.duration_since(header_start).as_micros() as u64,
                    "error": match &other {
                        Ok(Err(error)) => diagnostics::chain(error),
                        Err(error) => diagnostics::chain(error),
                        Ok(Ok(_)) => unreachable!(),
                    },
                })
            });
            let client_error = upload_error.load(Ordering::Acquire);
            if let Some(event) = &mut event {
                event.request_bytes = request_bytes.load(Ordering::Relaxed);
            }
            if grpc && (tokio::time::Instant::now() >= deadline || client_error == 2) {
                if let Some(attempt) = event.as_mut().and_then(|e| e.attempts.last_mut()) {
                    attempt.outcome = "request_deadline";
                }
                return Ok(record_grpc_deadline(&state, event, started));
            }
            if client_error != 0 {
                let (status, reason) = match client_error {
                    2 => (StatusCode::REQUEST_TIMEOUT, "request_body_timeout"),
                    3 => (StatusCode::PAYLOAD_TOO_LARGE, "request_body_limit"),
                    _ => (StatusCode::BAD_REQUEST, "request_body_error"),
                };
                if let Some(attempt) = event.as_mut().and_then(|e| e.attempts.last_mut()) {
                    attempt.outcome = reason;
                }
                return Ok(record_error(&state, event, started, status, reason));
            }
            if uploaded.load(Ordering::Acquire) && !upstream_h2 {
                state.pool.mark_failed(&endpoint);
            }
            if let Some(attempt) = event.as_mut().and_then(|e| e.attempts.last_mut()) {
                attempt.outcome = "headers_failed";
            }
            let timed_out = other.is_err();
            return Ok(record_error(
                &state,
                event,
                started,
                if timed_out {
                    StatusCode::GATEWAY_TIMEOUT
                } else {
                    StatusCode::BAD_GATEWAY
                },
                if timed_out {
                    "headers_timeout"
                } else {
                    "upstream_headers_error"
                },
            ));
        }
    };
    if let Some(exchange) = &exchange {
        exchange.stage("upstream_response_headers", "parsed");
    }
    if let Some(event) = &mut event {
        if let Some(attempt) = event.attempts.last_mut() {
            attempt.headers_ms = Some(ms(headers_at));
        }
        event.status = response.status().as_u16();
    }
    if invalid_framing(response.headers()) || headers_exceed_limits(response.headers(), limits) {
        reject!(BAD_GATEWAY, "invalid_upstream_headers");
    }
    if response.status() == StatusCode::SWITCHING_PROTOCOLS {
        let Some(handshake) = websocket else {
            reject!(BAD_GATEWAY, "unexpected_upgrade");
        };
        if !handshake.validate(&response) {
            reject!(BAD_GATEWAY, "invalid_websocket_response");
        }
        let upstream_upgrade = hyper::upgrade::on(&mut response);
        strip_hop(response.headers_mut());
        response
            .headers_mut()
            .insert(CONNECTION, HeaderValue::from_static("upgrade"));
        response
            .headers_mut()
            .insert("upgrade", HeaderValue::from_static("websocket"));
        response
            .headers_mut()
            .insert(X_REQUEST_ID, request_id_header);
        let tunnel = crate::websocket::Tunnel {
            downstream: downstream_upgrade.unwrap(),
            upstream: upstream_upgrade,
            completion: Completion {
                event: event.clone(),
                started,
                headers_at: Instant::now(),
                logger: state.logger.clone(),
                lease,
                _request_slot: slot,
                uploaded,
                request_bytes,
                exchange,
            },
            client_slot,
            handshake_deadline: header_deadline,
            idle: Duration::from_millis(limits.stream_idle_ms),
            max_age: Duration::from_millis(limits.websocket_max_ms),
        };
        if !state.tunnels.spawn(tunnel.run()) {
            reject!(SERVICE_UNAVAILABLE, "draining");
        }
        return Ok(response.map(|_| body::full("")));
    }
    strip_hop(response.headers_mut());
    *response.version_mut() = inbound_version;
    response
        .headers_mut()
        .insert(X_REQUEST_ID, request_id_header);
    if let Some(policy) = &route.headers {
        if policy.response.apply(response.headers_mut())
            && headers_exceed_limits(response.headers(), limits)
        {
            reject!(BAD_GATEWAY, "upstream_header_policy_limit");
        }
    }
    let completion = Completion {
        event,
        started,
        headers_at: Instant::now(),
        logger: state.logger.clone(),
        lease,
        _request_slot: slot,
        uploaded,
        request_bytes,
        exchange,
    };
    Ok(response.map(|b| body::download(b, limits, deadline, completion, grpc)))
}
pub fn admin(req: Request<Incoming>, state: &State) -> Response<ProxyBody> {
    if req.method() != hyper::Method::GET {
        return reply(StatusCode::METHOD_NOT_ALLOWED, "GET required");
    }
    match req.uri().path() {
        "/live" => reply(StatusCode::OK, "live\n"),
        "/ready" => {
            if state.ready.load(Ordering::Acquire) {
                reply(StatusCode::OK, "ready\n")
            } else {
                reply(StatusCode::SERVICE_UNAVAILABLE, "draining\n")
            }
        }
        "/metrics" => {
            let expected = state.admin_token.as_ref().map(|s| format!("Bearer {s}"));
            if expected.is_none()
                || req
                    .headers()
                    .get("authorization")
                    .and_then(|h| h.to_str().ok())
                    != expected.as_deref()
            {
                return reply(StatusCode::FORBIDDEN, "local admin token required\n");
            }
            let generation = state.generation.read().unwrap();
            let prefix = generation.config.name.replace('-', "_");
            let mut text = state.logger.metrics.prometheus(&prefix);
            text.push_str(&format!("{prefix}_upstream_connections_in_use {}\n{prefix}_upstream_connections_idle {}\n{prefix}_request_slots_available {}\n{prefix}_config_version {}\n",
                generation.config.limits.connections - state.pool.available(), state.pool.idle_count(), state.requests.available_permits(), generation.version));
            text.push_str(&format!("# TYPE {prefix}_clients_active gauge\n{prefix}_clients_active {}\n# TYPE {prefix}_requests_active gauge\n{prefix}_requests_active {}\n",
                generation.config.limits.clients - state.clients.available_permits(),
                generation.config.limits.requests - state.requests.available_permits()));
            let mut response = Response::new(body::full(text));
            response.headers_mut().insert(
                "content-type",
                HeaderValue::from_static("text/plain; version=0.0.4"),
            );
            response
        }
        _ => reply(StatusCode::NOT_FOUND, "not found\n"),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_ambiguous_paths() {
        assert!(!normal_path("/api/%2fsecret"));
        assert!(!normal_path("/a/../b"));
    }
    #[test]
    fn percent_encoded_path_filter_is_ascii_case_insensitive() {
        let blocked = [0x2f, 0x5c, 0x2e, 0x25, 0x00, 0x0a, 0x0d];
        for octet in 0..=255 {
            for path in [
                format!("/ok/%{octet:02x}/path"),
                format!("/ok/%{octet:02X}/path"),
            ] {
                assert_eq!(normal_path(&path), !blocked.contains(&octet), "{path}");
            }
        }
        assert!(!normal_path("/ok/%aa%2E"));
        assert!(!normal_path("/ok/%%5C"));
        for path in ["/ok/a%2", "/ok/%%", "/café", "/a.b/.c"] {
            assert!(normal_path(path), "{path}");
        }
    }
    #[test]
    fn framing_and_hop_headers() {
        let mut h = HeaderMap::new();
        h.append(CONNECTION, HeaderValue::from_static("X-Secret, TE"));
        h.append(CONNECTION, HeaderValue::from_static("x-other"));
        h.insert("x-secret", HeaderValue::from_static("value"));
        h.insert("x-other", HeaderValue::from_static("value"));
        h.insert(TE, HeaderValue::from_static("trailers"));
        h.insert("keep-alive", HeaderValue::from_static("timeout=1"));
        strip_hop(&mut h);
        assert!(h.is_empty());
        h.insert(CONTENT_LENGTH, HeaderValue::from_static("2"));
        h.insert(TRANSFER_ENCODING, HeaderValue::from_static("chunked"));
        assert!(invalid_framing(&h));
    }
    #[test]
    fn grpc_deadlines_and_trailer_header() {
        assert_eq!(grpc_timeout("1m"), Some(Duration::from_millis(1)));
        assert_eq!(
            grpc_timeout("99999999H"),
            Some(Duration::from_secs(359999996400))
        );
        for invalid in ["", "1", "100000000S", "1s", "-1S", "1.5S", "１２S"] {
            assert_eq!(grpc_timeout(invalid), None, "{invalid}");
        }
        for duration in [
            Duration::from_nanos(1),
            Duration::from_millis(1234),
            Duration::from_secs(3600),
        ] {
            let encoded = remaining_grpc_timeout(duration);
            assert!(encoded.len() <= 9);
            assert!(grpc_timeout(&encoded).unwrap() >= duration);
        }
        let mut headers = HeaderMap::new();
        headers.insert("te", HeaderValue::from_static("trailers"));
        headers.insert("trailer", HeaderValue::from_static("grpc-status"));
        strip_hop(&mut headers);
        assert_eq!(headers["te"], "trailers");
        assert_eq!(headers["trailer"], "grpc-status");
        headers.insert("te", HeaderValue::from_static("gzip, trailers"));
        strip_hop(&mut headers);
        assert!(!headers.contains_key("te"));
    }
}
