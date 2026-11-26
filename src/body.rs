use crate::{
    config::Limits,
    diagnostics,
    pool::{ms, Lease, UploadGuard},
    telemetry::{Event, Logger},
};
use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::body::{Body, Frame, Incoming, SizeHint};
use std::{
    error::Error,
    future::Future,
    pin::Pin,
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicU8, Ordering},
        Arc,
    },
    task::{Context, Poll},
    time::{Duration, Instant},
};
use tokio::time::{sleep_until, Instant as TokioInstant, Sleep};

pub type BoxError = Box<dyn Error + Send + Sync>;
pub type ProxyBody = http_body_util::combinators::UnsyncBoxBody<Bytes, BoxError>;
pub fn full(text: impl Into<Bytes>) -> ProxyBody {
    http_body_util::Full::new(text.into())
        .map_err(|e| match e {})
        .boxed_unsync()
}
pub struct Completion {
    pub event: Option<Event>,
    pub started: Instant,
    pub headers_at: Instant,
    pub logger: Logger,
    pub lease: Lease,
    pub _request_slot: crate::admission::RequestSlot,
    pub uploaded: Arc<AtomicBool>,
    pub request_bytes: Arc<AtomicU64>,
    pub exchange: Option<diagnostics::Exchange>,
}
struct GuardBody {
    inner: Incoming,
    timer: Pin<Box<Sleep>>,
    idle: Duration,
    deadline: TokioInstant,
    limit: u64,
    count: Arc<AtomicU64>,
    upload_complete: Option<Arc<AtomicBool>>,
    upload_exchange: Option<diagnostics::Exchange>,
    upload_error: Option<Arc<AtomicU8>>,
    completion: Option<Completion>,
    upload_guard: Option<UploadGuard>,
    upload_request_slot: Option<crate::admission::RequestSlot>,
    max_headers: usize,
    max_header_bytes: usize,
    grpc: bool,
    finished: bool,
}
impl GuardBody {
    fn finish(&mut self, outcome: &'static str) {
        if self.finished {
            return;
        }
        self.finished = true;
        if let Some(exchange) = self.upload_exchange.take() {
            exchange.stage("upstream_upload_handoff", outcome);
        }
        self.upload_guard.take();
        self.upload_request_slot.take();
        if let Some(error) = &self.upload_error {
            let code = match outcome {
                "stream_timeout" => 2,
                "body_limit" => 3,
                "body_error" | "invalid_trailers" | "trailer_limit" => 4,
                _ => 0,
            };
            if code != 0 {
                error.store(code, Ordering::Release);
            }
        }
        if outcome == "complete" {
            if let Some(done) = &self.upload_complete {
                done.store(true, Ordering::Release);
            }
        }
        if let Some(mut c) = self.completion.take() {
            if let Some(exchange) = &c.exchange {
                exchange.stage("upstream_response_body_end", outcome);
                exchange.stage("downstream_response_handoff", outcome);
            }
            let response_bytes = self.count.load(Ordering::Relaxed);
            if let Some(event) = &mut c.event {
                event.duration_ms = ms(c.started);
                event.transfer_ms = Some(ms(c.headers_at));
                event.request_bytes = c.request_bytes.load(Ordering::Relaxed);
                event.response_bytes = response_bytes;
                event.outcome = outcome;
                if outcome != "complete" {
                    let (direction, category) = if outcome == "cancelled" {
                        ("downstream", "cancelled")
                    } else {
                        ("upstream_or_downstream", "stream")
                    };
                    event.error_direction = Some(direction);
                    event.error_category = Some(category);
                    event.error_phase = Some("transfer");
                    event.reason = Some(outcome);
                }
                if let Some(attempt) = event.attempts.last_mut() {
                    attempt.outcome = outcome;
                }
            }
            if outcome != "complete" {
                c.logger.metrics.body_errors.fetch_add(1, Ordering::Relaxed);
            }
            if outcome == "cancelled" {
                c.logger
                    .metrics
                    .cancellations
                    .fetch_add(1, Ordering::Relaxed);
            }
            if outcome == "complete" {
                c.lease.complete(c.uploaded.load(Ordering::Acquire));
                c.logger.metrics.responses.fetch_add(1, Ordering::Relaxed);
            }
            c.logger
                .metrics
                .response_bytes
                .fetch_add(response_bytes, Ordering::Relaxed);
            if let Some(event) = c.event {
                c.logger.emit(event);
            }
        }
    }
}
impl Drop for GuardBody {
    fn drop(&mut self) {
        self.finish("cancelled");
    }
}
impl Body for GuardBody {
    type Data = Bytes;
    type Error = BoxError;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        if self.finished {
            return Poll::Ready(None);
        }
        if self.timer.as_mut().poll(cx).is_ready() {
            self.finish("stream_timeout");
            if self.grpc {
                return Poll::Ready(Some(Ok(grpc_deadline_trailers())));
            }
            return Poll::Ready(Some(Err("stream_timeout".into())));
        }
        match Pin::new(&mut self.inner).poll_frame(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(None) => {
                self.finish("complete");
                Poll::Ready(None)
            }
            Poll::Ready(Some(Err(error))) => {
                diagnostics::emit("upstream_body_failed", || {
                    serde_json::json!({
                        "connection": self.completion.as_ref().map(|c| c.lease.connection_id()),
                        "direction": if self.completion.is_some() { "download" } else { "upload" },
                        "error": diagnostics::chain(&error)
                    })
                });
                self.finish("body_error");
                Poll::Ready(Some(Err("body_error".into())))
            }
            Poll::Ready(Some(Ok(frame))) => {
                if let Some(trailers) = frame.trailers_ref() {
                    if trailers.len() > self.max_headers
                        || trailers
                            .iter()
                            .map(|(name, value)| name.as_str().len() + value.as_bytes().len() + 32)
                            .sum::<usize>()
                            > self.max_header_bytes
                    {
                        self.finish("trailer_limit");
                        return Poll::Ready(Some(Err("trailer_limit".into())));
                    }
                    if trailers.keys().any(|name| {
                        matches!(
                            name.as_str(),
                            "connection"
                                | "keep-alive"
                                | "proxy-connection"
                                | "transfer-encoding"
                                | "upgrade"
                                | "content-length"
                                | "host"
                                | "te"
                                | "trailer"
                                | "proxy-authenticate"
                                | "proxy-authorization"
                        )
                    }) {
                        self.finish("invalid_trailers");
                        return Poll::Ready(Some(Err("invalid_trailers".into())));
                    }
                }
                if let Some(data) = frame.data_ref() {
                    let count = self.count.fetch_add(data.len() as u64, Ordering::Relaxed)
                        + data.len() as u64;
                    if count > self.limit {
                        self.finish("body_limit");
                        return Poll::Ready(Some(Err("body_limit".into())));
                    }
                }
                let next = (TokioInstant::now() + self.idle).min(self.deadline);
                self.timer.as_mut().reset(next);
                if self.inner.is_end_stream() {
                    self.finish("complete");
                }
                Poll::Ready(Some(Ok(frame)))
            }
        }
    }
    fn is_end_stream(&self) -> bool {
        self.finished
    }
    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}
pub struct UploadState {
    pub count: Arc<AtomicU64>,
    pub done: Arc<AtomicBool>,
    pub error: Arc<AtomicU8>,
    pub upload_guard: Option<UploadGuard>,
    pub request_slot: crate::admission::RequestSlot,
    pub exchange: Option<diagnostics::Exchange>,
}
pub fn upload(
    inner: Incoming,
    limits: &Limits,
    deadline: TokioInstant,
    state: UploadState,
) -> ProxyBody {
    let UploadState {
        count,
        done,
        error,
        upload_guard,
        request_slot,
        exchange,
    } = state;
    let idle = Duration::from_millis(limits.stream_idle_ms);
    let already_complete = inner.is_end_stream();
    if already_complete {
        done.store(true, Ordering::Release);
        if let Some(exchange) = &exchange {
            exchange.stage("upstream_upload_handoff", "empty");
        }
    }
    GuardBody {
        inner,
        timer: Box::pin(sleep_until((TokioInstant::now() + idle).min(deadline))),
        idle,
        deadline,
        limit: limits.max_body_bytes,
        count,
        upload_complete: Some(done),
        upload_exchange: if already_complete { None } else { exchange },
        upload_error: Some(error),
        completion: None,
        upload_guard,
        upload_request_slot: Some(request_slot),
        max_headers: limits.max_headers,
        max_header_bytes: limits.max_header_bytes,
        grpc: false,
        finished: false,
    }
    .boxed_unsync()
}
pub fn download(
    inner: Incoming,
    limits: &Limits,
    deadline: TokioInstant,
    completion: Completion,
    grpc: bool,
) -> ProxyBody {
    let empty = inner.is_end_stream();
    let idle = Duration::from_millis(limits.stream_idle_ms);
    let mut body = GuardBody {
        inner,
        timer: Box::pin(sleep_until((TokioInstant::now() + idle).min(deadline))),
        idle,
        deadline,
        limit: limits.max_body_bytes,
        count: Arc::new(AtomicU64::new(0)),
        upload_complete: None,
        upload_exchange: None,
        upload_error: None,
        completion: Some(completion),
        upload_guard: None,
        upload_request_slot: None,
        max_headers: limits.max_headers,
        max_header_bytes: limits.max_header_bytes,
        grpc,
        finished: false,
    };
    if empty {
        body.finish("complete");
    }
    body.boxed_unsync()
}

fn grpc_deadline_trailers() -> Frame<Bytes> {
    let mut trailers = hyper::HeaderMap::new();
    trailers.insert("grpc-status", hyper::header::HeaderValue::from_static("4"));
    trailers.insert(
        "grpc-message",
        hyper::header::HeaderValue::from_static("proxy deadline exceeded"),
    );
    Frame::trailers(trailers)
}
