//! RFC6455 opening handshake validation and bounded, transparent upgraded streams.
//! Frame parsing, extensions, ping/pong and close frames remain end-to-end.
use crate::{body::Completion, pool::ms};
use base64::{engine::general_purpose::STANDARD, Engine};
use hyper::{header::HeaderMap, Request, Response};
use hyper_util::rt::TokioIo;
use sha1::{Digest, Sha1};
use std::{
    future::Future,
    io,
    pin::Pin,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    sync::OwnedSemaphorePermit,
    task::JoinSet,
};

pub struct Handshake {
    accept: String,
    protocols: Vec<String>,
}
fn single<'a>(h: &'a HeaderMap, key: &str) -> Option<&'a str> {
    if h.get_all(key).iter().count() != 1 {
        return None;
    }
    h.get(key)?.to_str().ok()
}
fn token(h: &HeaderMap, key: &str, value: &str) -> bool {
    h.get_all(key)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .any(|v| v.trim().eq_ignore_ascii_case(value))
}
pub fn request<B>(req: &Request<B>) -> Result<Option<Handshake>, &'static str> {
    if !req.headers().contains_key("upgrade") {
        return if token(req.headers(), "connection", "upgrade") {
            Err("invalid_websocket_handshake")
        } else {
            Ok(None)
        };
    }
    let h = req.headers();
    if !single(h, "upgrade").is_some_and(|s| s.eq_ignore_ascii_case("websocket")) {
        return Err("unsupported_upgrade");
    }
    if req.method() != hyper::Method::GET
        || !token(h, "connection", "upgrade")
        || single(h, "sec-websocket-version") != Some("13")
        || h.contains_key("transfer-encoding")
        || h.get("content-length").is_some_and(|v| v != "0")
    {
        return Err("invalid_websocket_handshake");
    }
    let key = single(h, "sec-websocket-key").ok_or("invalid_websocket_key")?;
    if STANDARD.decode(key).map_or(true, |v| v.len() != 16) {
        return Err("invalid_websocket_key");
    }
    // Refuse Connection-nominated handshake fields instead of forwarding a mutated handshake.
    for field in [
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-protocol",
        "sec-websocket-extensions",
    ] {
        if token(h, "connection", field) {
            return Err("invalid_websocket_handshake");
        }
    }
    let protocols = h
        .get_all("sec-websocket-protocol")
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(',').map(|s| s.trim().to_owned()))
        .collect();
    let mut digest = Sha1::new();
    digest.update(key.as_bytes());
    digest.update(b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
    Ok(Some(Handshake {
        accept: STANDARD.encode(digest.finalize()),
        protocols,
    }))
}
impl Handshake {
    pub fn validate<B>(&self, response: &Response<B>) -> bool {
        let h = response.headers();
        token(h, "connection", "upgrade")
            && single(h, "upgrade").is_some_and(|v| v.eq_ignore_ascii_case("websocket"))
            && single(h, "sec-websocket-accept") == Some(self.accept.as_str())
            && !h.contains_key("content-length")
            && !h.contains_key("transfer-encoding")
            && !h.contains_key("sec-websocket-extensions")
            && !token(h, "connection", "sec-websocket-accept")
            && !token(h, "connection", "sec-websocket-protocol")
            && !token(h, "connection", "sec-websocket-extensions")
            && (!h.contains_key("sec-websocket-protocol")
                || single(h, "sec-websocket-protocol")
                    .is_some_and(|p| self.protocols.iter().any(|v| v == p)))
    }
}
struct Registry {
    closed: bool,
    tasks: JoinSet<()>,
}
pub struct Tunnels(Mutex<Registry>);
impl Tunnels {
    pub fn new() -> Self {
        Self(Mutex::new(Registry {
            closed: false,
            tasks: JoinSet::new(),
        }))
    }
    pub fn spawn(&self, task: impl Future<Output = ()> + Send + 'static) -> bool {
        let mut r = self.0.lock().unwrap();
        if r.closed {
            return false;
        }
        while r.tasks.try_join_next().is_some() {}
        r.tasks.spawn(task);
        true
    }
    pub fn close(&self) {
        self.0.lock().unwrap().closed = true;
    }
    pub async fn wait_until(&self, deadline: tokio::time::Instant) {
        let mut tasks = {
            let mut r = self.0.lock().unwrap();
            r.closed = true;
            std::mem::take(&mut r.tasks)
        };
        if tokio::time::timeout_at(deadline, async {
            while tasks.join_next().await.is_some() {}
        })
        .await
        .is_err()
        {
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
        }
    }
}
struct Activity<T> {
    io: T,
    last: Arc<Mutex<Instant>>,
    written: Arc<AtomicU64>,
}
impl<T: AsyncRead + Unpin> AsyncRead for Activity<T> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let before = buf.filled().len();
        let result = Pin::new(&mut self.io).poll_read(cx, buf);
        if buf.filled().len() > before {
            *self.last.lock().unwrap() = Instant::now();
        }
        result
    }
}
impl<T: AsyncWrite + Unpin> AsyncWrite for Activity<T> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let result = Pin::new(&mut self.io).poll_write(cx, buf);
        if let Poll::Ready(Ok(n)) = &result {
            if *n > 0 {
                *self.last.lock().unwrap() = Instant::now();
                self.written.fetch_add(*n as u64, Ordering::Relaxed);
            }
        }
        result
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_shutdown(cx)
    }
}
struct TunnelLog {
    completion: Completion,
    down: Arc<AtomicU64>,
    up: Arc<AtomicU64>,
    outcome: &'static str,
    reason: Option<&'static str>,
}
impl Drop for TunnelLog {
    fn drop(&mut self) {
        let c = &mut self.completion;
        let response_bytes = self.down.load(Ordering::Relaxed);
        if let Some(event) = &mut c.event {
            event.duration_ms = ms(c.started);
            event.transfer_ms = Some(ms(c.headers_at));
            event.request_bytes = self.up.load(Ordering::Relaxed);
            event.response_bytes = response_bytes;
            event.outcome = self.outcome;
            if let Some(attempt) = event.attempts.last_mut() {
                attempt.outcome = self.outcome;
            }
            if self.outcome != "complete" {
                event.error_direction = Some("upstream_or_downstream");
                event.error_category = Some("websocket");
                event.error_phase = Some("tunnel");
                event.reason = self.reason;
            }
        }
        if self.outcome != "complete" {
            c.logger.metrics.body_errors.fetch_add(1, Ordering::Relaxed);
        }
        if self.outcome == "cancelled" {
            c.logger
                .metrics
                .cancellations
                .fetch_add(1, Ordering::Relaxed);
        }
        c.logger
            .metrics
            .response_bytes
            .fetch_add(response_bytes, Ordering::Relaxed);
        if let Some(event) = c.event.take() {
            c.logger.emit(event);
        }
        // The lease is discarded. An upgraded socket can never reenter the HTTP pool.
    }
}
pub struct Tunnel {
    pub downstream: hyper::upgrade::OnUpgrade,
    pub upstream: hyper::upgrade::OnUpgrade,
    pub completion: Completion,
    pub client_slot: Arc<OwnedSemaphorePermit>,
    pub handshake_deadline: tokio::time::Instant,
    pub idle: Duration,
    pub max_age: Duration,
}
impl Tunnel {
    pub async fn run(self) {
        let _client_slot = self.client_slot;
        let mut log = TunnelLog {
            completion: self.completion,
            up: Arc::new(AtomicU64::new(0)),
            down: Arc::new(AtomicU64::new(0)),
            outcome: "cancelled",
            reason: Some("tunnel_cancelled"),
        };
        let upgraded = tokio::time::timeout_at(self.handshake_deadline, async {
            tokio::try_join!(self.downstream, self.upstream)
        })
        .await;
        let (down, up) = match upgraded {
            Ok(Ok(pair)) => pair,
            _ => {
                log.reason = Some("upgrade_failed");
                return;
            }
        };
        log.completion
            .logger
            .metrics
            .responses
            .fetch_add(1, Ordering::Relaxed);
        let last = Arc::new(Mutex::new(Instant::now()));
        let mut down = Activity {
            io: TokioIo::new(down),
            last: last.clone(),
            written: log.down.clone(),
        };
        let mut up = Activity {
            io: TokioIo::new(up),
            last: last.clone(),
            written: log.up.clone(),
        };
        let idle = async {
            loop {
                let until = *last.lock().unwrap() + self.idle;
                tokio::time::sleep_until(until.into()).await;
                if last.lock().unwrap().elapsed() >= self.idle {
                    break;
                }
            }
        };
        tokio::select! {
            result = tokio::io::copy_bidirectional_with_sizes(&mut down,&mut up,8192,8192) => {
                if result.is_ok() { log.outcome = "complete"; log.reason = None; }
                else { log.outcome = "body_error"; log.reason = Some("tunnel_io_error"); }
            },
            _ = idle => { log.outcome = "stream_timeout"; log.reason = Some("websocket_idle_timeout"); },
            _ = tokio::time::sleep(self.max_age) => { log.outcome = "stream_timeout"; log.reason = Some("websocket_max_age"); }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn valid() -> Request<()> {
        Request::builder()
            .header("upgrade", "websocket")
            .header("connection", "keep-alive, Upgrade")
            .header("sec-websocket-version", "13")
            .header("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ==")
            .body(())
            .unwrap()
    }
    #[test]
    fn rfc6455_handshake_and_rejections() {
        let mut req = valid();
        let h = request(&req).unwrap().unwrap();
        let mut r = Response::builder()
            .status(101)
            .header("upgrade", "websocket")
            .header("connection", "Upgrade")
            .header("sec-websocket-accept", "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
            .body(())
            .unwrap();
        assert!(h.validate(&r));
        r.headers_mut()
            .insert("sec-websocket-protocol", "unoffered".parse().unwrap());
        assert!(!h.validate(&r));
        req.headers_mut().append(
            "sec-websocket-key",
            "dGhlIHNhbXBsZSBub25jZQ==".parse().unwrap(),
        );
        assert!(request(&req).is_err());
        let mut req = valid();
        req.headers_mut()
            .insert("content-length", "2".parse().unwrap());
        assert!(request(&req).is_err());
        let mut req = valid();
        req.headers_mut()
            .insert("connection", "Upgrade, sec-websocket-key".parse().unwrap());
        assert!(request(&req).is_err());
    }
}
