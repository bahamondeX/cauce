//! Opt-in, bounded transport event capture for diagnostic runs.
use serde_json::{json, Value};
use std::{
    error::Error,
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc::{self, SyncSender},
        Arc, OnceLock,
    },
    time::{Duration, Instant},
};

enum CaptureMessage {
    Event(Value),
    Flush(SyncSender<()>),
}

struct Capture {
    origin: Instant,
    queue: SyncSender<CaptureMessage>,
}

static CAPTURE: OnceLock<Capture> = OnceLock::new();
static DROPPED: AtomicU64 = AtomicU64::new(0);
static SEQUENCE: AtomicU64 = AtomicU64::new(0);
static NEXT_DOWNSTREAM: AtomicU64 = AtomicU64::new(1);
const EVENT_LIMIT: u64 = 250_000;

pub fn init() {
    if std::env::var("CAUCE_TRANSPORT_DIAGNOSTICS").as_deref() != Ok("1") {
        return;
    }
    let (queue, receiver) = mpsc::sync_channel(8192);
    let _ = CAPTURE.set(Capture {
        origin: Instant::now(),
        queue,
    });
    std::thread::spawn(move || {
        for message in receiver {
            match message {
                CaptureMessage::Event(value) => eprintln!("{value}"),
                CaptureMessage::Flush(done) => {
                    eprintln!(
                        "{}",
                        json!({"event":"transport_diagnostics_end",
                            "attempted": SEQUENCE.load(Ordering::Relaxed),
                            "dropped": DROPPED.load(Ordering::Relaxed),
                            "limit": EVENT_LIMIT,
                        })
                    );
                    let _ = done.send(());
                }
            }
        }
    });
}

pub fn flush() {
    if let Some(capture) = CAPTURE.get() {
        let (done, wait) = mpsc::sync_channel(1);
        if capture.queue.try_send(CaptureMessage::Flush(done)).is_ok() {
            let _ = wait.recv_timeout(Duration::from_secs(1));
        } else {
            eprintln!(r#"{{"event":"transport_diagnostics_flush_lost"}}"#);
        }
    }
}

pub fn enabled() -> bool {
    CAPTURE.get().is_some()
}

pub fn downstream_id() -> Option<u64> {
    enabled().then(|| NEXT_DOWNSTREAM.fetch_add(1, Ordering::Relaxed))
}

pub fn emit(event: &'static str, fields: impl FnOnce() -> Value) {
    let Some(capture) = CAPTURE.get() else {
        return;
    };
    let seq = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    if seq >= EVENT_LIMIT {
        return;
    }
    let value = json!({
        "event": event, "seq": seq, "elapsed_us": capture.origin.elapsed().as_micros() as u64,
        "worker": format!("{:?}", std::thread::current().id()), "fields": fields()
    });
    if capture
        .queue
        .try_send(CaptureMessage::Event(value))
        .is_err()
    {
        DROPPED.fetch_add(1, Ordering::Relaxed);
    }
}

#[derive(Clone)]
pub struct Exchange {
    pub request_id: Arc<str>,
    pub downstream: Option<u64>,
    pub upstream: u64,
}

impl Exchange {
    pub fn stage(&self, event: &'static str, outcome: &'static str) {
        emit(event, || {
            json!({"request_id": self.request_id.as_ref(),
            "downstream": self.downstream, "connection": self.upstream, "outcome": outcome})
        });
    }
}

pub fn chain(error: &(dyn Error + 'static)) -> Vec<String> {
    let mut chain = vec![error.to_string()];
    let mut next = error.source();
    while let Some(cause) = next {
        chain.push(cause.to_string());
        next = cause.source();
    }
    chain
}

pub struct Task {
    kind: &'static str,
    id: u64,
    terminal: bool,
}

impl Task {
    pub fn new(kind: &'static str, id: Option<u64>) -> Option<Self> {
        id.map(|id| Self {
            kind,
            id,
            terminal: false,
        })
    }

    pub fn finish(&mut self, reason: &'static str, error: Option<&(dyn Error + 'static)>) {
        self.terminal = true;
        emit("transport_task_end", || {
            json!({
                "kind": self.kind, "connection": self.id,
                "reason": reason, "error": error.map(chain),
            })
        });
    }
}

impl Drop for Task {
    fn drop(&mut self) {
        if !self.terminal {
            self.finish("future_dropped", None);
        }
    }
}

/// Records connection-level I/O below Hyper's framing. TLS calls report
/// plaintext boundaries, and multiplexed writes can include several streams.
pub struct Io<T> {
    inner: T,
    id: Option<u64>,
    side: &'static str,
    unflushed: bool,
    eof: bool,
}

impl<T> Io<T> {
    pub fn new(inner: T, side: &'static str, id: Option<u64>) -> Self {
        Self {
            inner,
            id: id.filter(|_| enabled()),
            side,
            unflushed: false,
            eof: false,
        }
    }

    fn report(&self, event: &'static str, bytes: Option<usize>, error: Option<&std::io::Error>) {
        if let Some(id) = self.id {
            emit(event, || {
                json!({"connection": id, "side": self.side, "bytes": bytes,
                    "errno": error.and_then(std::io::Error::raw_os_error),
                    "error": error.map(|error| error.kind().to_string()),
                })
            });
        }
    }

    fn wrote(&mut self, result: &Poll<std::io::Result<usize>>) {
        if self.id.is_none() {
            return;
        }
        match result {
            Poll::Ready(Ok(bytes)) if *bytes != 0 => self.unflushed = true,
            Poll::Ready(Err(error)) => self.report("socket_write_error", None, Some(error)),
            _ => {}
        }
    }
}

use std::{
    pin::Pin,
    task::{Context, Poll},
};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

impl<T: AsyncRead + Unpin> AsyncRead for Io<T> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let before = buf.filled().len();
        let space = buf.remaining();
        let result = Pin::new(&mut self.inner).poll_read(cx, buf);
        if self.id.is_some() {
            match &result {
                Poll::Ready(Ok(())) if buf.filled().len() > before => {
                    self.report("socket_read", Some(buf.filled().len() - before), None);
                }
                Poll::Ready(Ok(())) if space > 0 && !self.eof => {
                    self.eof = true;
                    self.report("socket_read_eof", None, None);
                }
                Poll::Ready(Err(error)) => self.report("socket_read_error", None, Some(error)),
                _ => {}
            }
        }
        result
    }
}

impl<T: AsyncWrite + Unpin> AsyncWrite for Io<T> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        let result = Pin::new(&mut self.inner).poll_write(cx, bytes);
        self.wrote(&result);
        result
    }

    fn poll_write_vectored(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[std::io::IoSlice<'_>],
    ) -> Poll<std::io::Result<usize>> {
        let result = Pin::new(&mut self.inner).poll_write_vectored(cx, bytes);
        self.wrote(&result);
        result
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        let result = Pin::new(&mut self.inner).poll_flush(cx);
        match &result {
            Poll::Ready(Ok(())) if self.unflushed => {
                self.unflushed = false;
                self.report("socket_write_flushed", None, None);
            }
            Poll::Ready(Err(error)) => self.report("socket_write_error", None, Some(error)),
            _ => {}
        }
        result
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}
