//! Shared HTTP response cache policy, independent of the bounded memory store.
mod store;

use crate::{
    body::{self, BoxError, ProxyBody},
    config::{bytes, milliseconds},
};
use anyhow::{ensure, Result};
use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::{
    body::{Body, Frame, SizeHint},
    header::*,
    Method, Request, Response, StatusCode,
};
use serde::{Deserialize, Serialize};
use std::{
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::{Duration, Instant, SystemTime},
};
use store::{Read, Selection, Store, Ticket};

const STATUS: &str = "cache-status";
const MAX_KEY_BYTES: usize = 8192;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Spec {
    #[serde(deserialize_with = "bytes")]
    pub max_size: u64,
    #[serde(deserialize_with = "bytes")]
    max_entry_size: u64,
    max_entries: usize,
    max_fills: usize,
    max_waiters: usize,
    #[serde(deserialize_with = "milliseconds")]
    ttl: u64,
    #[serde(deserialize_with = "milliseconds")]
    stale_if_error: u64,
    #[serde(deserialize_with = "milliseconds")]
    stale_while_revalidate: u64,
    coalesce: bool,
    invalidate_on_write: bool,
    vary: Vec<String>,
    bypass_header: Option<String>,
    statuses: Vec<u16>,
}
impl Default for Spec {
    fn default() -> Self {
        Self {
            max_size: 64 * 1024 * 1024,
            max_entry_size: 256 * 1024,
            max_entries: 4096,
            max_fills: 16,
            max_waiters: 32,
            ttl: 60_000,
            stale_if_error: 0,
            stale_while_revalidate: 0,
            coalesce: true,
            invalidate_on_write: true,
            vary: vec!["accept-encoding".into()],
            bypass_header: None,
            statuses: vec![200],
        }
    }
}
impl Spec {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            (4096..=256 * 1024 * 1024).contains(&self.max_size)
                && (1..=4 * 1024 * 1024).contains(&self.max_entry_size)
                && self.max_entry_size <= self.max_size
                && (1..=32_768).contains(&self.max_entries)
                && (1..=128).contains(&self.max_fills)
                && self.max_fills as u64 * self.max_entry_size
                    <= self.max_size.min(64 * 1024 * 1024)
                && (1..=256).contains(&self.max_waiters)
                && self.ttl <= 7 * 86_400_000
                && self.stale_if_error <= 86_400_000
                && self.stale_while_revalidate <= 86_400_000
                && (1..=7).contains(&self.statuses.len())
                && self
                    .statuses
                    .iter()
                    .all(|s| matches!(s, 200 | 203 | 204 | 301 | 308 | 404 | 410))
                && self.vary.len() <= 8,
            "invalid HTTP cache limits or statuses"
        );
        for name in self.vary.iter().chain(&self.bypass_header) {
            let field = HeaderName::from_bytes(name.as_bytes())?;
            ensure!(
                name == field.as_str()
                    && name.len() <= 64
                    && !matches!(
                        name.as_str(),
                        "authorization"
                            | "cookie"
                            | "set-cookie"
                            | "x-forwarded-for"
                            | "forwarded"
                            | "x-real-ip"
                            | "connection"
                            | "host"
                            | "proxy-authorization"
                            | "te"
                            | "trailer"
                    ),
                "unsupported cache selector header"
            );
        }
        Ok(())
    }
}
pub(crate) struct Cache {
    spec: Spec,
    fields: Vec<HeaderName>,
    bypass: Option<HeaderName>,
    store: Arc<Store>,
}
impl Cache {
    pub fn new(spec: &Spec) -> Arc<Self> {
        let fields = spec
            .vary
            .iter()
            .map(|field| HeaderName::from_bytes(field.as_bytes()).expect("validated cache header"))
            .collect();
        let bypass = spec.bypass_header.as_ref().map(|field| {
            HeaderName::from_bytes(field.as_bytes()).expect("validated bypass header")
        });
        let store = Store::new(
            spec.max_size as usize,
            spec.max_entries,
            spec.max_fills,
            if spec.coalesce { spec.max_waiters } else { 0 },
        );
        Arc::new(Self {
            spec: spec.clone(),
            fields,
            bypass,
            store,
        })
    }

    pub fn invalidation<B>(
        &self,
        request: &Request<B>,
        authority: &str,
        secure: bool,
    ) -> Option<Family> {
        if !self.spec.invalidate_on_write
            || matches!(
                *request.method(),
                Method::GET | Method::HEAD | Method::OPTIONS | Method::TRACE
            )
        {
            return None;
        }
        Family::new(request, authority, secure)
    }
    pub fn drain(&self) {
        self.store.drain();
    }

    pub fn invalidate(&self, family: &Family) {
        self.store.invalidate(family);
    }

    pub async fn probe<B: Body>(
        self: &Arc<Self>,
        request: &Request<B>,
        authority: &str,
        secure: bool,
        deadline: tokio::time::Instant,
    ) -> Probe {
        let controls = Controls::parse(request.headers());
        let miss = || {
            if controls.only_if_cached {
                Probe::OnlyIfCached
            } else {
                Probe::Bypass
            }
        };
        if !matches!(*request.method(), Method::GET | Method::HEAD)
            || !request.body().is_end_stream()
            || controls.invalid
            || controls.no_store
            || [AUTHORIZATION, COOKIE, RANGE, IF_RANGE, UPGRADE]
                .iter()
                .any(|name| request.headers().contains_key(name))
            || self
                .bypass
                .as_ref()
                .is_some_and(|name| request.headers().contains_key(name))
        {
            return miss();
        }
        let Some(key) = Key::new(request, authority, secure, &self.fields) else {
            return miss();
        };
        let key = Arc::new(key);
        let force = controls.no_cache
            || controls.max_age == Some(0)
            || request.headers().get_all(PRAGMA).iter().any(|value| {
                value
                    .to_str()
                    .is_ok_and(|line| line.eq_ignore_ascii_case("no-cache"))
            });
        let conditional = [
            IF_MATCH,
            IF_NONE_MATCH,
            IF_MODIFIED_SINCE,
            IF_UNMODIFIED_SINCE,
        ]
        .iter()
        .any(|field| request.headers().contains_key(field));
        let could_fill =
            !controls.only_if_cached && request.method() == Method::GET && !conditional;
        loop {
            // The hit check and flight registration share the publication lock.
            let now = Instant::now();
            let result = self.store.select(&key, |entry, filling| {
                if let Some(entry) = entry {
                    let age = entry.age(now);
                    if !force
                        && age < entry.meta.fresh
                        && controls
                            .max_age
                            .is_none_or(|max| age <= Duration::from_secs(max))
                        && age.saturating_add(Duration::from_secs(controls.min_fresh.unwrap_or(0)))
                            < entry.meta.fresh
                    {
                        return Read::Hit;
                    }
                    if filling
                        && !force
                        && !conditional
                        && controls.min_fresh.is_none()
                        && controls.max_age.is_none()
                        && entry.within_stale(now, entry.meta.swr)
                    {
                        return Read::Hit;
                    }
                }
                if could_fill {
                    Read::Fill
                } else {
                    Read::Bypass
                }
            });
            match result {
                Selection::Hit(entry) => {
                    let age = entry.age(now);
                    let marker = if age < entry.meta.fresh {
                        "Cauce; hit"
                    } else {
                        "Cauce; hit; ttl=0"
                    };
                    let mut response =
                        entry.response(request.method() == Method::HEAD, now, marker);
                    if entry.meta.status.is_success() {
                        if let Some(status) = crate::static_files::precondition(
                            request.headers(),
                            header(&entry.meta.headers, ETAG),
                            date(&entry.meta.headers, LAST_MODIFIED),
                        ) {
                            *response.status_mut() = status;
                            response.headers_mut().remove(CONTENT_LENGTH);
                            *response.body_mut() = body::full("");
                        }
                    }
                    return Probe::Hit(response);
                }
                Selection::Fill(ticket, stale) => {
                    return Probe::Fill(Box::new(Fill {
                        cache: self.clone(),
                        ticket,
                        stale,
                        started: now,
                        can_serve_stale: !force
                            && controls.min_fresh.is_none()
                            && controls.max_age.is_none(),
                    }));
                }
                Selection::Wait(mut receiver) => {
                    if tokio::time::timeout_at(deadline, receiver.changed())
                        .await
                        .is_err()
                    {
                        return Probe::Deadline;
                    }
                }
                Selection::Unavailable => return miss(),
            }
        }
    }

    fn metadata(
        &self,
        status: StatusCode,
        headers: &HeaderMap,
        request_at: Instant,
    ) -> Option<Meta> {
        if !self.spec.statuses.contains(&status.as_u16())
            || [
                SET_COOKIE,
                SET_COOKIE2,
                CONTENT_RANGE,
                TRAILER,
                WWW_AUTHENTICATE,
            ]
            .iter()
            .any(|field| headers.contains_key(field))
            || headers.contains_key("signature")
            || headers.contains_key("signature-input")
            || [ETAG, LAST_MODIFIED, DATE, AGE, CONTENT_LENGTH, EXPIRES]
                .iter()
                .any(|field| headers.get_all(field).iter().nth(1).is_some())
        {
            return None;
        }
        for value in headers.get_all(VARY) {
            for name in value.to_str().ok()?.split(',') {
                let name = HeaderName::from_bytes(name.trim().as_bytes()).ok()?;
                if !self.fields.contains(&name) {
                    return None;
                }
            }
        }
        let control = Controls::parse(headers);
        // The operator enables caching per route, and the origin must signal
        // that this representation is shared. TTL settings limit that grant.
        if control.invalid
            || control.private
            || control.no_store
            || !(control.public || control.s_maxage.is_some())
        {
            return None;
        }
        let wall = SystemTime::now();
        let origin_date = date(headers, DATE).unwrap_or(wall);
        let expires = headers.contains_key(EXPIRES).then(|| {
            date(headers, EXPIRES)
                .and_then(|expires| expires.duration_since(origin_date).ok())
                .unwrap_or_default()
        });
        let configured = Duration::from_millis(self.spec.ttl);
        let freshness = if control.no_cache {
            Duration::ZERO
        } else {
            control
                .s_maxage
                .or(control.max_age)
                .map(Duration::from_secs)
                .or(expires)
                .unwrap_or(configured)
                .min(configured)
        };
        let now = Instant::now();
        let apparent = wall.duration_since(origin_date).unwrap_or_default();
        let received_age = if headers.contains_key(AGE) {
            header(headers, AGE)?.parse::<u64>().ok()?
        } else {
            0
        };
        let age = apparent.max(
            Duration::from_secs(received_age)
                .saturating_add(now.saturating_duration_since(request_at)),
        );
        let extra = |origin: Option<u64>, local: u64| {
            if control.no_cache || control.must_revalidate || control.s_maxage.is_some() {
                Duration::ZERO
            } else {
                origin
                    .map(Duration::from_secs)
                    .map_or(Duration::from_millis(local), |v| {
                        v.min(Duration::from_millis(local))
                    })
            }
        };
        let mut headers = headers.clone();
        // A tracer, backend diagnostic or previous cache may have used these
        // per-request fields. The gateway issues fresh correlation on every hit.
        for field in [
            "x-request-id",
            "traceparent",
            "tracestate",
            "x-cauce-transport",
            STATUS,
            "age",
        ] {
            headers.remove(field);
        }
        if !headers.contains_key(DATE) {
            headers.insert(
                DATE,
                HeaderValue::from_str(&httpdate::fmt_http_date(wall)).expect("date header"),
            );
        }
        Some(Meta {
            status,
            headers,
            fresh: freshness,
            age,
            received: now,
            swr: extra(
                control.stale_while_revalidate,
                self.spec.stale_while_revalidate,
            ),
            sie: extra(control.stale_if_error, self.spec.stale_if_error),
        })
    }
}
const SET_COOKIE2: HeaderName = HeaderName::from_static("set-cookie2");

#[derive(PartialEq, Eq, Hash)]
pub(crate) struct Family {
    authority: String,
    uri: String,
    secure: bool,
}
impl Family {
    fn new<B>(request: &Request<B>, authority: &str, secure: bool) -> Option<Self> {
        let uri = request.uri().path_and_query()?.as_str();
        if uri.len() + authority.len() > MAX_KEY_BYTES {
            return None;
        }
        Some(Self {
            authority: authority.to_ascii_lowercase(),
            uri: uri.to_owned(),
            secure,
        })
    }
}
#[derive(PartialEq, Eq, Hash)]
struct Key {
    family: Family,
    selectors: Vec<u8>,
}
impl Key {
    fn new<B>(
        request: &Request<B>,
        host: &str,
        secure: bool,
        fields: &[HeaderName],
    ) -> Option<Self> {
        let family = Family::new(request, host, secure)?;
        let mut selectors = Vec::new();
        let budget = MAX_KEY_BYTES - family.authority.len() - family.uri.len();
        for field in fields {
            for value in request.headers().get_all(field) {
                if selectors.len() + value.as_bytes().len() + 4 > budget {
                    return None;
                }
                selectors.extend_from_slice(&(value.as_bytes().len() as u32).to_le_bytes());
                selectors.extend_from_slice(value.as_bytes());
            }
            if selectors.len() + 4 > budget {
                return None;
            }
            selectors.extend_from_slice(&u32::MAX.to_le_bytes());
        }
        Some(Self { family, selectors })
    }
    fn bytes(&self) -> usize {
        self.family.authority.capacity() + self.family.uri.capacity() + self.selectors.capacity()
    }
}

struct Meta {
    status: StatusCode,
    headers: HeaderMap,
    fresh: Duration,
    age: Duration,
    received: Instant,
    swr: Duration,
    sie: Duration,
}
struct Entry {
    meta: Meta,
    body: Bytes,
    capacity: usize,
}
impl Entry {
    fn bytes(&self) -> usize {
        self.capacity
            + 1024
            + self
                .meta
                .headers
                .iter()
                .map(|(name, value)| name.as_str().len() + value.as_bytes().len() + 96)
                .sum::<usize>()
    }
    fn age(&self, now: Instant) -> Duration {
        self.meta
            .age
            .saturating_add(now.saturating_duration_since(self.meta.received))
    }
    fn within_stale(&self, now: Instant, extension: Duration) -> bool {
        !extension.is_zero()
            && self.age(now) >= self.meta.fresh
            && self.age(now).saturating_sub(self.meta.fresh) < extension
    }
    fn response(
        &self,
        head: bool,
        now: Instant,
        cache_status: &'static str,
    ) -> Response<ProxyBody> {
        let mut response = Response::new(body::full(if head {
            Bytes::new()
        } else {
            self.body.clone()
        }));
        *response.status_mut() = self.meta.status;
        *response.headers_mut() = self.meta.headers.clone();
        if self.meta.status != StatusCode::NO_CONTENT {
            response
                .headers_mut()
                .insert(CONTENT_LENGTH, HeaderValue::from(self.body.len()));
        }
        response.headers_mut().insert(
            AGE,
            HeaderValue::from(self.age(now).as_secs().min(2_147_483_648)),
        );
        response
            .headers_mut()
            .insert(STATUS, HeaderValue::from_static(cache_status));
        response
    }
}

#[derive(Default)]
struct Controls {
    invalid: bool,
    no_cache: bool,
    no_store: bool,
    private: bool,
    public: bool,
    must_revalidate: bool,
    only_if_cached: bool,
    max_age: Option<u64>,
    s_maxage: Option<u64>,
    min_fresh: Option<u64>,
    stale_if_error: Option<u64>,
    stale_while_revalidate: Option<u64>,
}
impl Controls {
    fn parse(headers: &HeaderMap) -> Self {
        let mut control = Self::default();
        for value in headers.get_all(CACHE_CONTROL) {
            let Ok(value) = value.to_str() else {
                control.invalid = true;
                continue;
            };
            // The shared cache conservatively refuses quoted lists (where
            // commas could be part of a quoted extension or field list).
            if value.contains(['"', '\\']) {
                control.invalid = true;
            }
            for item in value.split(',') {
                let (name, value) = item
                    .split_once('=')
                    .map_or((item.trim(), None), |(n, v)| (n.trim(), Some(v.trim())));
                if name.eq_ignore_ascii_case("no-cache") {
                    control.no_cache = true;
                } else if name.eq_ignore_ascii_case("no-store") {
                    control.no_store = true;
                } else if name.eq_ignore_ascii_case("private") {
                    control.private = true;
                } else if name.eq_ignore_ascii_case("public") {
                    control.public = true;
                } else if name.eq_ignore_ascii_case("must-revalidate")
                    || name.eq_ignore_ascii_case("proxy-revalidate")
                {
                    control.must_revalidate = true;
                } else if name.eq_ignore_ascii_case("only-if-cached") {
                    control.only_if_cached = true;
                } else {
                    for (candidate, slot) in [
                        ("s-maxage", &mut control.s_maxage),
                        ("max-age", &mut control.max_age),
                        ("min-fresh", &mut control.min_fresh),
                        ("stale-if-error", &mut control.stale_if_error),
                        (
                            "stale-while-revalidate",
                            &mut control.stale_while_revalidate,
                        ),
                    ] {
                        if name.eq_ignore_ascii_case(candidate) {
                            let parsed = value
                                .filter(|value| {
                                    !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit())
                                })
                                .map(|value| value.parse::<u64>().unwrap_or(u64::MAX));
                            if slot.is_some() || parsed.is_none() {
                                control.invalid = true;
                            }
                            *slot = parsed;
                        }
                    }
                }
            }
        }
        control
    }
}

fn header(headers: &HeaderMap, field: HeaderName) -> Option<&str> {
    headers.get(field)?.to_str().ok()
}
fn date(headers: &HeaderMap, field: HeaderName) -> Option<SystemTime> {
    httpdate::parse_http_date(header(headers, field)?).ok()
}

pub(crate) enum Probe {
    Hit(Response<ProxyBody>),
    Fill(Box<Fill>),
    Bypass,
    OnlyIfCached,
    Deadline,
}
pub(crate) struct Fill {
    cache: Arc<Cache>,
    ticket: Ticket,
    stale: Option<Arc<Entry>>,
    started: Instant,
    can_serve_stale: bool,
}
impl Fill {
    pub fn conditional(&self, headers: &mut HeaderMap) -> bool {
        let Some(entry) = &self.stale else {
            return false;
        };
        if let Some(etag) = entry.meta.headers.get(ETAG) {
            headers.insert(IF_NONE_MATCH, etag.clone());
            true
        } else if let Some(last) = entry.meta.headers.get(LAST_MODIFIED) {
            headers.insert(IF_MODIFIED_SINCE, last.clone());
            true
        } else {
            false
        }
    }
    pub fn stale_on_error(&self, status: StatusCode) -> Option<Response<ProxyBody>> {
        let entry = self.stale.as_ref()?;
        let now = Instant::now();
        (self.can_serve_stale && self.ticket.current() && entry.within_stale(now, entry.meta.sie))
            .then(|| {
                let mut response = entry.response(false, now, "Cauce; fwd=stale");
                response.headers_mut().insert(
                    STATUS,
                    HeaderValue::from_str(&format!(
                        "Cauce; fwd=stale; fwd-status={}",
                        status.as_u16()
                    ))
                    .unwrap(),
                );
                response
            })
    }
    pub fn refreshed(self, headers: &HeaderMap) -> Option<Response<ProxyBody>> {
        let old = self.stale.as_ref()?;
        let previous = &old.meta.headers;
        if [SET_COOKIE, SET_COOKIE2, TRAILER]
            .iter()
            .any(|name| headers.contains_key(name))
            || headers
                .get(ETAG)
                .is_some_and(|new| previous.get(ETAG) != Some(new))
            || (headers.contains_key(VARY)
                && !headers
                    .get_all(VARY)
                    .iter()
                    .eq(previous.get_all(VARY).iter()))
            || headers
                .get(CONTENT_ENCODING)
                .is_some_and(|new| previous.get(CONTENT_ENCODING) != Some(new))
        {
            self.ticket.invalidate();
            return None;
        }
        let mut merged = previous.clone();
        merged.remove(AGE);
        merged.remove(DATE);
        for name in [DATE, AGE, CACHE_CONTROL, EXPIRES, LAST_MODIFIED, ETAG, VARY] {
            let values = headers.get_all(&name);
            if values.iter().next().is_some() {
                merged.remove(&name);
                for value in values {
                    merged.append(&name, value.clone());
                }
            }
        }
        let Some(meta) = self.cache.metadata(old.meta.status, &merged, self.started) else {
            self.ticket.invalidate();
            return None;
        };
        let entry = Arc::new(Entry {
            meta,
            body: old.body.clone(),
            capacity: old.capacity,
        });
        if !self.ticket.publish(entry.clone()) {
            return None;
        }
        Some(entry.response(false, Instant::now(), "Cauce; fwd=stale; fwd-status=304"))
    }

    pub fn capture(
        self,
        status: StatusCode,
        headers: &HeaderMap,
        already_empty: bool,
    ) -> Option<Capture> {
        let Some(meta) = self.cache.metadata(status, headers, self.started) else {
            // A definitive replacement that is not cacheable revokes the
            // previous representation. Server errors may retain stale data.
            if !status.is_server_error() && self.stale.is_some() {
                self.ticket.invalidate();
            }
            return None;
        };
        let limit = self.cache.spec.max_entry_size as usize;
        let expected =
            header(headers, CONTENT_LENGTH).map(|value| value.parse::<u64>().unwrap_or(u64::MAX));
        if expected.is_some_and(|size| size > limit as u64) {
            if self.stale.is_some() {
                self.ticket.invalidate();
            }
            return None;
        }
        let capture = Capture {
            meta,
            ticket: self.ticket,
            bytes: Vec::new(),
            limit,
            expected,
        };
        if already_empty {
            capture.publish();
            None
        } else {
            Some(capture)
        }
    }
}
pub(crate) struct Capture {
    meta: Meta,
    ticket: Ticket,
    bytes: Vec<u8>,
    limit: usize,
    expected: Option<u64>,
}
impl Capture {
    fn publish(self) {
        if self
            .expected
            .is_none_or(|length| length == self.bytes.len() as u64)
        {
            self.ticket.publish(Arc::new(Entry {
                meta: self.meta,
                capacity: self.bytes.capacity(),
                body: Bytes::from(self.bytes),
            }));
        }
    }
    pub fn wrap(self, body: ProxyBody) -> ProxyBody {
        CaptureBody {
            body,
            capture: Some(self),
        }
        .boxed_unsync()
    }
}
struct CaptureBody {
    body: ProxyBody,
    capture: Option<Capture>,
}
impl Body for CaptureBody {
    type Data = Bytes;
    type Error = BoxError;
    fn size_hint(&self) -> SizeHint {
        self.body.size_hint()
    }
    fn is_end_stream(&self) -> bool {
        self.capture.is_none() && self.body.is_end_stream()
    }
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        let frame = Pin::new(&mut self.body).poll_frame(cx);
        if let Some(capture) = &mut self.capture {
            match &frame {
                Poll::Ready(Some(Ok(ready))) => {
                    if let Some(bytes) = ready
                        .data_ref()
                        .filter(|bytes| bytes.len() <= capture.limit - capture.bytes.len())
                    {
                        // Exact reservation prevents capacity growth above the
                        // per-fill byte budget when the last frame is large.
                        capture.bytes.reserve_exact(bytes.len());
                        capture.bytes.extend_from_slice(bytes);
                    } else {
                        // Trailer responses and oversized responses remain
                        // forward-only. Waiters can start another fill.
                        self.capture.take();
                    }
                }
                Poll::Ready(Some(Err(_))) => {
                    self.capture.take();
                }
                _ => {}
            }
            if matches!(frame, Poll::Ready(None))
                || self.body.is_end_stream() && matches!(frame, Poll::Ready(Some(Ok(_))))
            {
                if let Some(capture) = self.capture.take() {
                    capture.publish();
                }
            }
        }
        frame
    }
}
