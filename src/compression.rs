//! Streaming response coding, with per-route quotas and bounded work per poll.
#[cfg(feature = "web")]
use crate::body::{self, BoxError};
use crate::{admission::RequestSlot, body::ProxyBody, config::bytes};
use anyhow::{ensure, Result};
#[cfg(feature = "web")]
use async_compression::{
    tokio::bufread::{BrotliEncoder, GzipEncoder},
    Level,
};
#[cfg(feature = "web")]
use bytes::Bytes;
#[cfg(feature = "web")]
use http_body_util::BodyExt;
#[cfg(feature = "web")]
use hyper::{
    body::{Body, Frame},
    StatusCode,
};
use hyper::{header::*, Method, Request, Response};
use serde::{Deserialize, Serialize};
#[cfg(feature = "web")]
use std::{
    io,
    pin::Pin,
    task::{Context, Poll},
};
#[cfg(feature = "web")]
use tokio::{
    io::{AsyncRead, BufReader, ReadBuf},
    sync::OwnedSemaphorePermit,
};

#[cfg(feature = "web")]
use std::sync::Arc;
#[cfg(feature = "web")]
use tokio::sync::Semaphore;

#[cfg(feature = "web")]
const BLOCK: usize = 16 * 1024;
#[cfg(feature = "web")]
const REPRESENTATION_INTEGRITY: &[&str] = &[
    "content-md5",
    "digest",
    "content-digest",
    "repr-digest",
    "signature",
    "signature-input",
];

#[derive(Clone, Copy, Debug, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Coding {
    Gzip,
    Br,
}
impl Coding {
    pub fn name(self) -> &'static str {
        match self {
            Self::Gzip => "gzip",
            Self::Br => "br",
        }
    }
    pub fn suffix(self) -> &'static str {
        match self {
            Self::Gzip => ".gz",
            Self::Br => ".br",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Spec {
    algorithms: Vec<Coding>,
    #[serde(deserialize_with = "bytes")]
    min_size: u64,
    gzip_level: u32,
    brotli_level: u32,
    max_inflight: usize,
    mime: Vec<String>,
}
impl Default for Spec {
    fn default() -> Self {
        Self {
            algorithms: vec![Coding::Br, Coding::Gzip],
            min_size: 1024,
            gzip_level: 3,
            brotli_level: 4,
            max_inflight: 8,
            mime: [
                "text/*",
                "application/json",
                "application/javascript",
                "application/xml",
                "image/svg+xml",
            ]
            .map(str::to_owned)
            .into(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(try_from = "Spec", into = "Spec")]
pub(crate) struct Policy {
    spec: Spec,
    #[cfg(feature = "web")]
    quota: Arc<Semaphore>,
}
impl From<Policy> for Spec {
    fn from(policy: Policy) -> Self {
        policy.spec
    }
}
impl PartialEq for Policy {
    fn eq(&self, other: &Self) -> bool {
        self.spec == other.spec
    }
}
impl TryFrom<Spec> for Policy {
    type Error = anyhow::Error;
    fn try_from(spec: Spec) -> Result<Self> {
        ensure!(
            (1..=2).contains(&spec.algorithms.len())
                && (spec.algorithms.len() == 1 || spec.algorithms[0] != spec.algorithms[1])
                && spec.gzip_level <= 9
                && spec.brotli_level <= 6
                && (1..=32).contains(&spec.max_inflight)
                && (1..=32).contains(&spec.mime.len())
                && spec.mime.iter().all(|value| {
                    value.len() <= 80
                        && value
                            .split_once('/')
                            .is_some_and(|(a, b)| !a.is_empty() && !b.is_empty())
                        && value.bytes().all(|b| {
                            b.is_ascii_lowercase() || b.is_ascii_digit() || b"/-+.*".contains(&b)
                        })
                        && (!value.contains('*') || value.ends_with("/*"))
                }),
            "invalid compression algorithms, levels or limits"
        );
        Ok(Self {
            #[cfg(feature = "web")]
            quota: Arc::new(Semaphore::new(spec.max_inflight)),
            spec,
        })
    }
}

#[derive(Clone, Copy)]
pub(crate) struct Choice {
    pub coding: Option<Coding>,
    #[cfg(feature = "web")]
    eligible: bool,
    #[cfg(feature = "web")]
    identity_allowed: bool,
}

impl Policy {
    pub fn max_inflight(&self) -> usize {
        self.spec.max_inflight
    }

    pub fn choice<B>(&self, request: &Request<B>, grpc: bool, upgrade: bool) -> Choice {
        let headers = request.headers();
        let eligible = !(grpc
            || upgrade
            || !matches!(*request.method(), Method::GET | Method::HEAD)
            || [AUTHORIZATION, COOKIE, RANGE]
                .iter()
                .any(|name| headers.contains_key(name))
            || directive(headers, &["no-transform"]));
        let weight = |coding: &str| coding_quality(headers, coding);
        let wildcard = weight("*");
        let identity = weight("identity");
        #[cfg(feature = "web")]
        let identity_allowed = identity
            .or(wildcard.filter(|quality| *quality == 0))
            .unwrap_or(1000)
            > 0;
        let candidate = self
            .spec
            .algorithms
            .iter()
            .copied()
            .map(|codec| (codec, weight(codec.name()).or(wildcard).unwrap_or(0)))
            .filter(|(_, q)| *q > 0 && identity.is_none_or(|identity| *q >= identity))
            // Equal weights preserve server configuration order.
            .reduce(|preferred, next| {
                if preferred.1 < next.1 {
                    next
                } else {
                    preferred
                }
            });
        Choice {
            coding: eligible
                .then_some(candidate.map(|(coding, _)| coding))
                .flatten(),
            #[cfg(feature = "web")]
            eligible,
            #[cfg(feature = "web")]
            identity_allowed,
        }
    }

    pub fn precompressed(&self, choice: Choice, response_size: u64, mime: &str) -> Option<Coding> {
        if response_size < self.spec.min_size || !self.mime(mime) {
            return None;
        }
        choice.coding
    }

    fn mime(&self, mime: &str) -> bool {
        self.spec.mime.iter().any(|value| {
            if let Some(prefix) = value.strip_suffix('*') {
                mime.get(..prefix.len())
                    .is_some_and(|start| start.eq_ignore_ascii_case(prefix))
            } else {
                mime.eq_ignore_ascii_case(value)
            }
        })
    }

    #[cfg(not(feature = "web"))]
    pub fn apply(
        &self,
        _choice: Choice,
        response: Response<ProxyBody>,
        _slot: &RequestSlot,
        _head: bool,
    ) -> Response<ProxyBody> {
        response
    }

    #[cfg(feature = "web")]
    pub fn apply(
        &self,
        choice: Choice,
        mut response: Response<ProxyBody>,
        slot: &RequestSlot,
        head: bool,
    ) -> Response<ProxyBody> {
        if choice.eligible && response.status() == StatusCode::NOT_MODIFIED {
            vary(response.headers_mut());
        }
        if !choice.eligible || response.status() != StatusCode::OK {
            return response;
        }
        let length = response
            .headers()
            .get(CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<u64>().ok())
            .or_else(|| response.body().size_hint().exact());
        let headers = response.headers_mut();
        if headers.contains_key(CONTENT_ENCODING)
            || headers.contains_key(CONTENT_RANGE)
            || headers.contains_key(SET_COOKIE)
            || headers.contains_key(TRAILER)
            || REPRESENTATION_INTEGRITY
                .iter()
                .any(|name| headers.contains_key(*name))
            || directive(headers, &["private", "no-store", "no-transform"])
        {
            return response;
        }
        let mimetype = headers
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.split(';').next())
            .map(str::trim);
        if length.is_none_or(|len| len >= self.spec.min_size)
            && mimetype.is_some_and(|mime| self.mime(mime))
            && (length.is_some() || self.spec.min_size == 0)
        {
            vary(headers);
            if let Some(coding) = choice.coding {
                if head {
                    encode_headers(headers, coding);
                    return response;
                }
                if let Ok(permit) = self.quota.clone().try_acquire_owned() {
                    encode_headers(headers, coding);
                    let level = match coding {
                        Coding::Gzip => self.spec.gzip_level,
                        Coding::Br => self.spec.brotli_level,
                    };
                    return response.map(|body| {
                        EncoderBody::new(body, coding, level, permit, slot.clone()).boxed_unsync()
                    });
                }
            }
        }
        if choice.identity_allowed {
            response
        } else {
            *response.status_mut() = StatusCode::NOT_ACCEPTABLE;
            let headers = response.headers_mut();
            for name in [
                CONTENT_LENGTH,
                CONTENT_TYPE,
                ETAG,
                LAST_MODIFIED,
                ACCEPT_RANGES,
            ] {
                headers.remove(name);
            }
            for name in REPRESENTATION_INTEGRITY {
                headers.remove(*name);
            }
            headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
            vary(headers);
            response.map(|_| body::full(""))
        }
    }
}
pub(crate) fn vary(headers: &mut HeaderMap) {
    if !headers
        .get_all(VARY)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|v| v.split(','))
        .any(|item| item.trim() == "*" || item.trim().eq_ignore_ascii_case("accept-encoding"))
    {
        headers.append(VARY, HeaderValue::from_static("Accept-Encoding"));
    }
}
#[cfg(feature = "web")]
fn encode_headers(headers: &mut HeaderMap, coding: Coding) {
    headers.insert(CONTENT_ENCODING, HeaderValue::from_static(coding.name()));
    headers.remove(CONTENT_LENGTH);
    headers.remove(ACCEPT_RANGES);
    if let Some(tag) = headers.get(ETAG).and_then(|value| value.to_str().ok()) {
        if tag.starts_with('"') {
            if let Ok(weak) = HeaderValue::from_str(&format!("W/{tag}")) {
                headers.insert(ETAG, weak);
            }
        }
    }
}
fn directive(headers: &HeaderMap, names: &[&str]) -> bool {
    headers.get_all(CACHE_CONTROL).iter().any(|value| {
        value.to_str().map_or(true, |value| {
            value.split(',').any(|field| {
                let name = field.split('=').next().unwrap_or_default().trim();
                names
                    .iter()
                    .any(|candidate| name.eq_ignore_ascii_case(candidate))
            })
        })
    })
}
fn coding_quality(headers: &HeaderMap, coding: &str) -> Option<u16> {
    headers
        .get_all(ACCEPT_ENCODING)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|item| {
            let (name, quality) = item
                .trim()
                .split_once(';')
                .map_or((item.trim(), None), |(name, q)| (name.trim(), Some(q)));
            if !name.eq_ignore_ascii_case(coding) {
                return None;
            }
            Some(quality.map_or(1000, |q| quality_value(q.trim()).unwrap_or(0)))
        })
        .min()
}
fn quality_value(parameter: &str) -> Option<u16> {
    let (name, value) = parameter.split_once('=')?;
    if !name.trim().eq_ignore_ascii_case("q") {
        return None;
    }
    let (whole, fraction) = value.trim().split_once('.').unwrap_or((value.trim(), ""));
    if fraction.len() > 3 || !fraction.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let rest = fraction
        .bytes()
        .zip([100, 10, 1])
        .map(|(b, scale)| u16::from(b - b'0') * scale)
        .sum::<u16>();
    match whole {
        "0" => Some(rest),
        "1" if rest == 0 => Some(1000),
        _ => None,
    }
}

#[cfg(feature = "web")]
struct Source {
    body: ProxyBody,
    pending: Bytes,
    trailers: Option<HeaderMap>,
    budget: usize,
}
#[cfg(feature = "web")]
impl AsyncRead for Source {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        if buf.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        for _ in 0..16 {
            if !self.pending.is_empty() {
                let amount = self.pending.len().min(buf.remaining()).min(self.budget);
                if amount == 0 {
                    cx.waker().wake_by_ref();
                    return Poll::Pending;
                }
                buf.put_slice(&self.pending.split_to(amount));
                self.budget -= amount;
                return Poll::Ready(Ok(()));
            }
            match Pin::new(&mut self.body).poll_frame(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(None) => return Poll::Ready(Ok(())),
                Poll::Ready(Some(Err(error))) => return Poll::Ready(Err(io::Error::other(error))),
                Poll::Ready(Some(Ok(frame))) => match frame.into_data() {
                    Ok(data) => self.pending = data,
                    Err(frame) => {
                        if let Ok(mut trailers) = frame.into_trailers() {
                            for name in REPRESENTATION_INTEGRITY {
                                trailers.remove(*name);
                            }
                            self.trailers = (!trailers.is_empty()).then_some(trailers);
                        }
                    }
                },
            }
        }
        cx.waker().wake_by_ref();
        Poll::Pending
    }
}
#[cfg(feature = "web")]
type Input = BufReader<Source>;
#[cfg(feature = "web")]
enum Encoder {
    Gzip(Box<GzipEncoder<Input>>),
    Br(Box<BrotliEncoder<Input>>),
}
#[cfg(feature = "web")]
impl Encoder {
    fn source(&mut self) -> &mut Source {
        match self {
            Self::Gzip(codec) => codec.get_mut().get_mut(),
            Self::Br(codec) => codec.get_mut().get_mut(),
        }
    }
}
#[cfg(feature = "web")]
struct EncoderBody {
    encoder: Encoder,
    scratch: Box<[u8; BLOCK]>,
    hold: Option<(OwnedSemaphorePermit, RequestSlot)>,
}
#[cfg(feature = "web")]
impl EncoderBody {
    fn new(
        body: ProxyBody,
        coding: Coding,
        level: u32,
        permit: OwnedSemaphorePermit,
        slot: RequestSlot,
    ) -> Self {
        let source = BufReader::with_capacity(
            BLOCK,
            Source {
                body,
                pending: Bytes::new(),
                trailers: None,
                budget: 0,
            },
        );
        let level = Level::Precise(level as i32);
        let encoder = match coding {
            Coding::Gzip => Encoder::Gzip(Box::new(GzipEncoder::with_quality(source, level))),
            Coding::Br => Encoder::Br(Box::new(BrotliEncoder::with_quality(source, level))),
        };
        Self {
            encoder,
            scratch: Box::new([0; BLOCK]),
            hold: Some((permit, slot)),
        }
    }
}
#[cfg(feature = "web")]
impl Body for EncoderBody {
    type Data = Bytes;
    type Error = BoxError;
    fn is_end_stream(&self) -> bool {
        self.hold.is_none()
    }
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        let Self {
            encoder,
            scratch,
            hold,
        } = &mut *self;
        if hold.is_none() {
            return Poll::Ready(None);
        }
        encoder.source().budget = BLOCK;
        let mut buffer = ReadBuf::new(&mut scratch[..]);
        let poll = match encoder {
            Encoder::Gzip(codec) => Pin::new(codec.as_mut()).poll_read(cx, &mut buffer),
            Encoder::Br(codec) => Pin::new(codec.as_mut()).poll_read(cx, &mut buffer),
        };
        match poll {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Ok(())) if !buffer.filled().is_empty() => Poll::Ready(Some(Ok(
                Frame::data(Bytes::copy_from_slice(buffer.filled())),
            ))),
            Poll::Ready(Ok(())) => {
                hold.take();
                Poll::Ready(
                    encoder
                        .source()
                        .trailers
                        .take()
                        .map(|headers| Ok(Frame::trailers(headers))),
                )
            }
            Poll::Ready(Err(error)) => {
                hold.take();
                Poll::Ready(Some(Err(error.into())))
            }
        }
    }
}
