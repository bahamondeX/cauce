//! Compiled local content handlers and response lifetime accounting.
use crate::{
    admission::RequestSlot,
    body::{self, BoxError, ProxyBody},
    config::{Limits, Route},
    pool::ms,
    static_files,
    telemetry::{Event, Logger},
};
use anyhow::{ensure, Result};
use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::{
    body::{Body, Frame, SizeHint},
    header::HeaderValue,
    Method, Request, Response, StatusCode,
};
use serde::{Deserialize, Serialize};
use std::{
    future::Future,
    pin::Pin,
    sync::atomic::Ordering,
    task::{Context, Poll},
    time::{Duration, Instant},
};
use tokio::time::{Instant as TokioInstant, Sleep};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Handler {
    Static(static_files::Policy),
    Direct(Direct),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "DirectSpec", into = "DirectSpec")]
pub(crate) struct Direct {
    spec: DirectSpec,
    text: Bytes,
    content_type: HeaderValue,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct DirectSpec {
    status: u16,
    #[serde(default)]
    body: String,
    #[serde(default)]
    content_type: Option<String>,
}

impl TryFrom<DirectSpec> for Direct {
    type Error = anyhow::Error;
    fn try_from(spec: DirectSpec) -> Result<Self> {
        ensure!(
            (200..=599).contains(&spec.status)
                && spec.status != 304
                && (!matches!(spec.status, 204 | 205) || spec.body.is_empty())
                && spec.body.len() <= 16384,
            "invalid direct response status or body"
        );
        let text = Bytes::copy_from_slice(spec.body.as_bytes());
        let content_type = HeaderValue::from_str(
            spec.content_type
                .as_deref()
                .unwrap_or("text/plain; charset=utf-8"),
        )?;
        Ok(Self {
            spec,
            text,
            content_type,
        })
    }
}
impl From<Direct> for DirectSpec {
    fn from(direct: Direct) -> Self {
        direct.spec
    }
}

impl Handler {
    pub fn validate(&self, route: &Route) -> Result<()> {
        if let Self::Static(policy) = self {
            policy.validate(route)?;
        }
        Ok(())
    }
    pub async fn response<B>(&self, request: &Request<B>, route: &Route) -> Response<ProxyBody> {
        match self {
            Self::Static(policy) => policy.response(request, route).await,
            Self::Direct(direct) => {
                let status =
                    StatusCode::from_u16(direct.spec.status).expect("validated direct status");
                let mut response = Response::builder().status(status);
                if !matches!(status.as_u16(), 204 | 205) {
                    response = response
                        .header("content-type", &direct.content_type)
                        .header("content-length", direct.text.len());
                }
                response
                    .body(body::full(if request.method() == Method::HEAD {
                        Bytes::new()
                    } else {
                        direct.text.clone()
                    }))
                    .unwrap()
            }
        }
    }
}

pub(crate) struct Completion {
    pub event: Option<Event>,
    pub logger: Logger,
    pub started: Instant,
    pub slot: RequestSlot,
}
impl Completion {
    pub fn wrap(
        mut self,
        response: Response<ProxyBody>,
        limits: &Limits,
        deadline: TokioInstant,
    ) -> Response<ProxyBody> {
        if let Some(event) = &mut self.event {
            event.status = response.status().as_u16();
        }
        if response.body().is_end_stream() {
            self.finish(0, "complete", Instant::now());
            return response;
        }
        let idle = Duration::from_millis(limits.stream_idle_ms);
        let expected = response
            .headers()
            .get("content-length")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse().ok());
        response.map(|inner| {
            LocalBody {
                inner,
                completion: Some(self),
                idle,
                deadline,
                timer: Box::pin(tokio::time::sleep_until(
                    deadline.min(TokioInstant::now() + idle),
                )),
                headers_at: Instant::now(),
                bytes: 0,
                expected,
            }
            .boxed_unsync()
        })
    }
    fn finish(self, bytes: u64, outcome: &'static str, headers_at: Instant) {
        let Self {
            event,
            logger,
            started,
            slot,
        } = self;
        let metrics = &logger.metrics;
        metrics.response_bytes.fetch_add(bytes, Ordering::Relaxed);
        if outcome == "complete" {
            metrics.responses.fetch_add(1, Ordering::Relaxed);
        } else {
            metrics.body_errors.fetch_add(1, Ordering::Relaxed);
        }
        if outcome == "cancelled" {
            metrics.cancellations.fetch_add(1, Ordering::Relaxed);
        }
        if let Some(mut event) = event {
            event.duration_ms = ms(started);
            event.transfer_ms = Some(ms(headers_at));
            event.response_bytes = bytes;
            event.outcome = outcome;
            if outcome != "complete" {
                event.error_direction = Some(if outcome == "cancelled" {
                    "downstream"
                } else {
                    "proxy"
                });
                event.error_category = Some("stream");
                event.error_phase = Some("transfer");
                event.reason = Some(outcome);
            }
            logger.emit(event);
        }
        drop(slot);
    }
}

struct LocalBody {
    inner: ProxyBody,
    completion: Option<Completion>,
    idle: Duration,
    deadline: TokioInstant,
    timer: Pin<Box<Sleep>>,
    headers_at: Instant,
    bytes: u64,
    expected: Option<u64>,
}
impl LocalBody {
    fn finish(&mut self, outcome: &'static str) {
        if let Some(state) = self.completion.take() {
            state.finish(self.bytes, outcome, self.headers_at);
        }
    }
}
impl Drop for LocalBody {
    fn drop(&mut self) {
        self.finish("cancelled");
    }
}
impl Body for LocalBody {
    type Data = Bytes;
    type Error = BoxError;
    fn is_end_stream(&self) -> bool {
        self.completion.is_none()
    }
    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        if self.completion.is_none() {
            return Poll::Ready(None);
        }
        if self.timer.as_mut().poll(cx).is_ready() {
            self.finish("stream_timeout");
            return Poll::Ready(Some(Err("stream_timeout".into())));
        }
        match Pin::new(&mut self.inner).poll_frame(cx) {
            Poll::Ready(Some(Ok(frame))) => {
                self.bytes += frame.data_ref().map_or(0, |data| data.len() as u64);
                if self.inner.is_end_stream() || self.expected == Some(self.bytes) {
                    self.finish("complete");
                } else {
                    let until = self.deadline.min(TokioInstant::now() + self.idle);
                    self.timer.as_mut().reset(until);
                }
                Poll::Ready(Some(Ok(frame)))
            }
            Poll::Ready(Some(Err(error))) => {
                self.finish("body_error");
                Poll::Ready(Some(Err(error)))
            }
            Poll::Ready(None) => {
                self.finish("complete");
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}
