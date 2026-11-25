//! Bound stalled downstream writes even when Hyper stops polling the response body.
use std::{
    future::Future,
    io,
    pin::Pin,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    sync::watch,
    task::JoinSet,
    time::{sleep, Instant, Sleep},
};
tokio::task_local! {
    static STREAM_DEADLINE: watch::Sender<Instant>;
}
/// Only terminal gRPC status delivery may outlive the application deadline.
/// The independent hard timer still bounds peers with a closed receive window.
pub const GRPC_STATUS_GRACE: Duration = Duration::from_millis(100);
/// Each Hyper HTTP/2 stream runs under its own deadline, including periods when
/// a zero flow-control window prevents Hyper from polling the response body.
#[derive(Clone)]
pub struct StreamExecutor {
    limit: Duration,
    tasks: Arc<Mutex<StreamTasks>>,
    active: Arc<AtomicU64>,
}
#[derive(Default)]
struct StreamTasks {
    closed: bool,
    tasks: JoinSet<()>,
}
/// Owned by the connection task (including its abort path), never by Hyper.
/// Dropping the connection must also stop independently spawned stream futures.
pub struct StreamGuard(Arc<Mutex<StreamTasks>>);
impl Drop for StreamGuard {
    fn drop(&mut self) {
        let mut state = self.0.lock().unwrap();
        state.closed = true;
        state.tasks.abort_all();
    }
}
impl StreamExecutor {
    pub fn new(limit: Duration) -> (Self, StreamGuard) {
        let tasks = Arc::new(Mutex::new(StreamTasks::default()));
        (
            Self {
                limit,
                tasks: tasks.clone(),
                active: Arc::new(AtomicU64::new(0)),
            },
            StreamGuard(tasks),
        )
    }
}
impl StreamExecutor {
    pub fn with_active_counter(mut self, active: Arc<AtomicU64>) -> Self {
        self.active = active;
        self
    }
}
struct ActiveTask(Arc<AtomicU64>);
impl Drop for ActiveTask {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}
impl<F> hyper::rt::Executor<F> for StreamExecutor
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    fn execute(&self, future: F) {
        let (deadline_tx, mut deadline_rx) =
            watch::channel(Instant::now() + self.limit + GRPC_STATUS_GRACE);
        let mut state = self.tasks.lock().unwrap();
        if state.closed {
            return;
        }
        while state.tasks.try_join_next().is_some() {}
        self.active.fetch_add(1, Ordering::Relaxed);
        // Capture before spawning: even an unpolled, aborted task must decrement.
        let active = ActiveTask(self.active.clone());
        state
            .tasks
            .spawn(STREAM_DEADLINE.scope(deadline_tx, async move {
                let _active = active;
                tokio::pin!(future);
                loop {
                    let deadline = *deadline_rx.borrow();
                    tokio::select! {
                        _ = &mut future => break,
                        _ = tokio::time::sleep_until(deadline) => break,
                        changed = deadline_rx.changed() => { if changed.is_err() { break; } }
                    }
                }
            }));
    }
}
pub fn tighten_stream_deadline(deadline: Instant) {
    let _ = STREAM_DEADLINE.try_with(|sender| {
        sender.send_if_modified(|current| {
            if deadline < *current {
                *current = deadline;
                true
            } else {
                false
            }
        });
    });
}
/// Observe framing without decoding HPACK or retaining payloads. Hyper remains
/// responsible for protocol validation. One field block per connection is enough:
/// RFC 9113 requires HEADERS/CONTINUATION to be contiguous on the same stream.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum HeaderPhase {
    Preface,
    Http2,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct HeaderDeadline {
    at: Instant,
    phase: HeaderPhase,
}
enum Protocol {
    Preface(usize),
    Http1,
    Http2,
}
struct HeaderObserver {
    protocol: Protocol,
    limit: Duration,
    deadline: Option<HeaderDeadline>,
    header: [u8; 9],
    used: usize,
    remaining: usize,
    block: Option<u32>,
    ends_block: bool,
}
impl HeaderObserver {
    fn new(limit: Duration, now: Instant) -> Self {
        Self {
            protocol: Protocol::Preface(0),
            limit,
            deadline: Some(HeaderDeadline {
                at: now + limit,
                phase: HeaderPhase::Preface,
            }),
            header: [0; 9],
            used: 0,
            remaining: 0,
            block: None,
            ends_block: false,
        }
    }
    fn expired(&self, now: Instant) -> bool {
        self.deadline.is_some_and(|d| now >= d.at)
    }
    fn observe(&mut self, mut bytes: &[u8], now: Instant) -> io::Result<()> {
        if self.expired(now) {
            return Err(io::ErrorKind::TimedOut.into());
        }
        while !bytes.is_empty() {
            match self.protocol {
                Protocol::Http1 => return Ok(()),
                Protocol::Preface(ref mut matched) => {
                    const PREFACE: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";
                    if bytes[0] != PREFACE[*matched] {
                        self.protocol = Protocol::Http1;
                        self.deadline = None;
                    } else {
                        *matched += 1;
                        if *matched == PREFACE.len() {
                            self.protocol = Protocol::Http2;
                            self.deadline = None;
                        }
                    }
                    bytes = &bytes[1..];
                }
                Protocol::Http2 => {
                    if self.remaining > 0 {
                        let take = bytes.len().min(self.remaining);
                        self.remaining -= take;
                        bytes = &bytes[take..];
                        if self.remaining == 0 {
                            self.finish_frame();
                        }
                        continue;
                    }
                    // Even an unidentified partial frame header must be bounded.
                    // Once identified as DATA/other, its payload has no header timer.
                    self.deadline.get_or_insert(HeaderDeadline {
                        at: now + self.limit,
                        phase: HeaderPhase::Http2,
                    });
                    let take = bytes.len().min(9 - self.used);
                    self.header[self.used..self.used + take].copy_from_slice(&bytes[..take]);
                    self.used += take;
                    bytes = &bytes[take..];
                    if self.used < 9 {
                        continue;
                    }
                    self.used = 0;
                    self.remaining = ((self.header[0] as usize) << 16)
                        | ((self.header[1] as usize) << 8)
                        | self.header[2] as usize;
                    let kind = self.header[3];
                    let stream =
                        u32::from_be_bytes(self.header[5..9].try_into().unwrap()) & 0x7fff_ffff;
                    let begins = kind == 1 && self.block.is_none();
                    if begins {
                        self.block = Some(stream);
                    }
                    self.ends_block = (begins || (kind == 9 && self.block == Some(stream)))
                        && self.header[4] & 4 != 0;
                    if self.block.is_none() {
                        self.deadline = None;
                    }
                    if self.remaining == 0 {
                        self.finish_frame();
                    }
                }
            }
        }
        Ok(())
    }
    fn finish_frame(&mut self) {
        if self.ends_block {
            self.block = None;
            self.deadline = None;
        }
        self.ends_block = false;
    }
}

/// Fuzz-only entry point for the production framing observer. The fuzz target
/// feeds both a complete buffer and arbitrary fragments at the same instant;
/// the resulting protocol state must be identical.
#[cfg(feature = "fuzzing")]
#[allow(dead_code)]
pub fn fuzz_observer_fragmentation(input: &[u8]) -> (bool, bool) {
    const PREFACE: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";
    const LIMIT: Duration = Duration::from_secs(1);
    if input.len() > 1_048_576 {
        return (false, false);
    }
    let now = Instant::now();
    let mut whole = HeaderObserver::new(LIMIT, now);
    let mut fragmented = HeaderObserver::new(LIMIT, now);
    whole.observe(PREFACE, now).expect("fixed preface");
    fragmented.observe(PREFACE, now).expect("fixed preface");
    let (mode, wire) = input.split_first().unwrap_or((&0, &[]));
    let whole_result = whole.observe(wire, now).is_ok();
    let mut offset = 0;
    let mut step = 1usize;
    while offset < wire.len() {
        let width = 1 + ((wire[offset] as usize ^ *mode as usize ^ step) % 31);
        let end = wire.len().min(offset + width);
        let result = fragmented.observe(&wire[offset..end], now).is_ok();
        assert_eq!(whole_result, result, "fragmentation changed parse result");
        offset = end;
        step = step.wrapping_add(1);
    }
    assert_eq!(observer_snapshot(&whole), observer_snapshot(&fragmented));
    (whole.block.is_some(), whole.deadline.is_some())
}

/// Exercise the production deadline using two different read timestamps. A
/// progressing field block must retain its original deadline; it cannot renew
/// itself by dribbling payload bytes.
#[cfg(feature = "fuzzing")]
#[allow(dead_code)]
pub fn fuzz_observer_time_advance(elapsed_ms: u16) {
    const PREFACE: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";
    let limit = Duration::from_secs(1);
    let start = Instant::now();
    let mut observer = HeaderObserver::new(limit, start);
    observer.observe(PREFACE, start).expect("fixed preface");
    let mut frame = [0u8; 9];
    frame[2] = 2; // two payload bytes
    frame[3] = 1; // HEADERS
    frame[8] = 1; // stream 1
    observer.observe(&frame, start).expect("frame header");
    let deadline = observer.deadline.expect("incomplete HEADERS deadline");
    let later = start + Duration::from_millis(u64::from(elapsed_ms));
    if later >= deadline.at {
        assert!(observer.observe(&[0x82], later).is_err());
        assert_eq!(observer.deadline, Some(deadline));
    } else {
        observer.observe(&[0x82], later).expect("in-time payload");
        assert_eq!(observer.deadline, Some(deadline));
        assert!(observer.block.is_some());
    }

    // Preface deadline expires at the exact boundary; once HTTP/1 is selected,
    // it is removed and subsequent traffic remains passthrough.
    let mut preface = HeaderObserver::new(limit, start);
    preface.observe(b"P", start).expect("preface fragment");
    if elapsed_ms >= 1_000 {
        assert!(preface.observe(b"R", start + limit).is_err());
    } else {
        let mismatch = start + Duration::from_millis(u64::from(elapsed_ms));
        preface.observe(b"O", mismatch).expect("HTTP/1 selection");
        preface
            .observe(b"ST /", start + limit + Duration::from_millis(1))
            .expect("HTTP/1 passthrough");
        assert!(preface.deadline.is_none());
    }

    // Once a complete DATA header is identified, only its payload remains;
    // elapsed time beyond the header limit does not create a field-block timer.
    let mut data = HeaderObserver::new(limit, start);
    data.observe(PREFACE, start).expect("fixed preface");
    let mut data_header = [0u8; 9];
    data_header[2] = 2;
    data_header[8] = 1;
    data.observe(&data_header, start).expect("DATA header");
    assert!(data.deadline.is_none());
    data.observe(&[0, 1], start + limit + Duration::from_millis(1))
        .expect("DATA payload has no HEADERS deadline");
}

#[cfg(feature = "fuzzing")]
#[allow(dead_code)]
#[derive(Debug, PartialEq, Eq)]
struct ObserverSnapshot {
    protocol: (u8, usize),
    deadline: Option<HeaderDeadline>,
    header: [u8; 9],
    used: usize,
    remaining: usize,
    block: Option<u32>,
    ends_block: bool,
}

#[cfg(feature = "fuzzing")]
fn observer_snapshot(observer: &HeaderObserver) -> ObserverSnapshot {
    let protocol = match observer.protocol {
        Protocol::Preface(matched) => (0, matched),
        Protocol::Http1 => (1, 0),
        Protocol::Http2 => (2, 0),
    };
    ObserverSnapshot {
        protocol,
        deadline: observer.deadline,
        header: observer.header,
        used: observer.used,
        remaining: observer.remaining,
        block: observer.block,
        ends_block: observer.ends_block,
    }
}

/// The outer connection task polls this independently of Hyper's read/write
/// readiness. A stalled writer must not suspend the input header deadline.
pub struct HeaderWatch(watch::Receiver<Option<HeaderDeadline>>);
impl HeaderWatch {
    pub async fn expired(&mut self) {
        loop {
            let deadline = *self.0.borrow_and_update();
            match deadline {
                Some(d) => tokio::select! {
                    biased;
                    changed = self.0.changed() => {
                        if changed.is_err() { std::future::pending::<()>().await; }
                    }
                    _ = tokio::time::sleep_until(d.at) => return,
                },
                None => {
                    if self.0.changed().await.is_err() {
                        std::future::pending::<()>().await;
                    }
                }
            }
        }
    }
    pub fn http2_expired(&self) -> bool {
        self.0
            .borrow()
            .is_some_and(|d| d.phase == HeaderPhase::Http2 && Instant::now() >= d.at)
    }
}
/// The same observer covers clear prior knowledge and the decrypted TLS stream.
/// HTTP/1 switches to passthrough at its first non-preface byte.
pub struct PrefaceTimeout<T> {
    inner: T,
    observer: HeaderObserver,
    deadline_tx: watch::Sender<Option<HeaderDeadline>>,
}
impl<T> PrefaceTimeout<T> {
    pub fn new(inner: T, limit: Duration) -> (Self, HeaderWatch) {
        let observer = HeaderObserver::new(limit, Instant::now());
        let (deadline_tx, deadline_rx) = watch::channel(observer.deadline);
        (
            Self {
                inner,
                observer,
                deadline_tx,
            },
            HeaderWatch(deadline_rx),
        )
    }
}
impl<T: AsyncRead + Unpin> AsyncRead for PrefaceTimeout<T> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        if self.observer.expired(Instant::now()) {
            return Poll::Ready(Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "client_headers_timeout",
            )));
        }
        let before = buf.filled().len();
        let result = Pin::new(&mut self.inner).poll_read(cx, buf);
        if matches!(result, Poll::Ready(Ok(()))) {
            self.observer
                .observe(&buf.filled()[before..], Instant::now())?;
            let deadline = self.observer.deadline;
            self.deadline_tx.send_if_modified(|current| {
                if *current == deadline {
                    false
                } else {
                    *current = deadline;
                    true
                }
            });
        }
        result
    }
}
impl<T: AsyncWrite + Unpin> AsyncWrite for PrefaceTimeout<T> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write(cx, data)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}
pub struct WriteTimeout<T> {
    inner: T,
    idle: Duration,
    timer: Pin<Box<Sleep>>,
    waiting: bool,
}
impl<T> WriteTimeout<T> {
    pub fn new(inner: T, idle: Duration) -> Self {
        Self {
            inner,
            idle,
            timer: Box::pin(sleep(idle)),
            waiting: false,
        }
    }
    fn stalled(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        if !self.waiting {
            self.timer.as_mut().reset(Instant::now() + self.idle);
            self.waiting = true;
        }
        if self.timer.as_mut().poll(cx).is_ready() {
            Poll::Ready(Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "downstream_write_idle_timeout",
            )))
        } else {
            Poll::Pending
        }
    }
}
impl<T: AsyncRead + Unpin> AsyncRead for WriteTimeout<T> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_read(cx, buf)
    }
}
impl<T: AsyncWrite + Unpin> AsyncWrite for WriteTimeout<T> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        match Pin::new(&mut self.inner).poll_write(cx, data) {
            Poll::Ready(r) => {
                self.waiting = false;
                Poll::Ready(r)
            }
            Poll::Pending => self.stalled(cx).map_ok(|_| 0),
        }
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match Pin::new(&mut self.inner).poll_flush(cx) {
            Poll::Ready(r) => {
                self.waiting = false;
                Poll::Ready(r)
            }
            Poll::Pending => self.stalled(cx),
        }
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const LIMIT: Duration = Duration::from_millis(40);
    const PREFACE: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";
    fn frame(len: u32, kind: u8, flags: u8, stream: u32) -> Vec<u8> {
        let mut bytes = len.to_be_bytes()[1..].to_vec();
        bytes.extend([kind, flags]);
        bytes.extend(stream.to_be_bytes());
        bytes
    }
    fn observer(now: Instant) -> HeaderObserver {
        let mut o = HeaderObserver::new(LIMIT, now);
        o.observe(PREFACE, now).unwrap();
        o
    }
    #[test]
    fn absolute_deadline_covers_fragments_and_continuation_gaps() {
        let now = Instant::now();
        let mut o = observer(now);
        let mut bytes = frame(2, 1, 1, 1);
        bytes.extend([0x82, 0x84]);
        for (i, byte) in bytes.iter().enumerate() {
            o.observe(&[*byte], now + Duration::from_millis(i as u64))
                .unwrap();
            assert_eq!(o.deadline.unwrap().at, now + LIMIT);
        }
        o.observe(&frame(1, 9, 4, 1)[..8], now + LIMIT / 2).unwrap();
        assert!(o.observe(&[1, 0x86], now + LIMIT).is_err());
        assert!(o.block.is_some());
    }
    #[test]
    fn end_headers_waits_for_all_payload_including_padding() {
        let now = Instant::now();
        for kind in [1, 9] {
            let mut o = observer(now);
            if kind == 9 {
                o.observe(&frame(0, 1, 0, 1), now).unwrap();
            }
            o.observe(&frame(3, kind, 4, 1), now).unwrap();
            o.observe(&[0, 0], now + LIMIT / 2).unwrap();
            assert_eq!(o.deadline.unwrap().at, now + LIMIT);
            o.observe(&[0], now + LIMIT / 2).unwrap();
            assert!(o.deadline.is_none());
        }
    }
    #[test]
    fn invalid_interleaving_does_not_disarm_or_reset_deadline() {
        let now = Instant::now();
        for (kind, stream) in [(9, 3), (1, 3), (4, 0)] {
            let mut o = observer(now);
            o.observe(&frame(0, 1, 0, 1), now).unwrap();
            o.observe(&frame(0, kind, 4, stream), now + LIMIT / 2)
                .unwrap();
            assert_eq!(o.deadline.unwrap().at, now + LIMIT);
            assert_eq!(o.block, Some(1));
        }
    }
    #[test]
    fn data_and_idle_are_not_header_timed_but_partial_frame_headers_are() {
        let now = Instant::now();
        let mut o = observer(now);
        o.observe(&frame(0, 1, 4, 1), now).unwrap();
        assert!(o.deadline.is_none());
        let later = now + LIMIT * 10;
        o.observe(&frame(2, 0, 0, 1), later).unwrap();
        assert!(o.deadline.is_none());
        o.observe(&[0], later + LIMIT * 10).unwrap();
        o.observe(&[0], later + LIMIT * 20).unwrap();
        o.observe(&[0, 0], later + LIMIT * 30).unwrap();
        assert_eq!(o.deadline.unwrap().at, later + LIMIT * 31);
    }
    #[test]
    fn coalesced_frames_and_fragmented_preface_keep_next_block_deadline() {
        let now = Instant::now();
        let mut o = HeaderObserver::new(LIMIT, now);
        for byte in PREFACE {
            o.observe(&[*byte], now).unwrap();
        }
        let mut bytes = frame(0, 4, 0, 0);
        bytes.extend(frame(1, 1, 4, 1));
        bytes.push(0x82);
        bytes.extend(frame(0, 1, 0, 3));
        o.observe(&bytes, now).unwrap();
        assert_eq!(o.block, Some(3));
        assert_eq!(o.deadline.unwrap().at, now + LIMIT);
        o.observe(&frame(0, 9, 4, 3), now + LIMIT / 2).unwrap();
        assert!(o.deadline.is_none());
    }
    #[test]
    fn http1_passthrough_and_preface_have_separate_lifetimes() {
        let now = Instant::now();
        let mut o = HeaderObserver::new(LIMIT, now);
        o.observe(b"P", now).unwrap();
        assert!(o.deadline.is_some());
        o.observe(b"OST / HTTP/1.1\r\n\r\n", now + LIMIT / 2)
            .unwrap();
        o.observe(PREFACE, now + LIMIT * 10).unwrap();
        assert!(o.deadline.is_none());
        let mut stalled = HeaderObserver::new(LIMIT, now);
        assert!(stalled.observe(PREFACE, now + LIMIT).is_err());
    }
    #[tokio::test]
    async fn header_watch_expires_without_further_io_polls() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let (mut writer, reader) = tokio::io::duplex(128);
        let (mut io, mut watch) = PrefaceTimeout::new(reader, LIMIT);
        let mut bytes = PREFACE.to_vec();
        bytes.extend(frame(1, 1, 4, 1));
        writer.write_all(&bytes).await.unwrap();
        let mut read = vec![0; bytes.len()];
        io.read_exact(&mut read).await.unwrap();
        assert_eq!(read, bytes);
        tokio::time::timeout(Duration::from_secs(1), watch.expired())
            .await
            .unwrap();
        assert!(watch.http2_expired());
    }
    #[tokio::test]
    async fn connection_guard_cancels_stream_tasks_and_rejects_late_tasks() {
        use hyper::rt::Executor;
        struct Dropped(Option<tokio::sync::oneshot::Sender<()>>);
        impl Drop for Dropped {
            fn drop(&mut self) {
                let _ = self.0.take().unwrap().send(());
            }
        }
        let active = Arc::new(AtomicU64::new(0));
        let (executor, guard) = StreamExecutor::new(Duration::from_secs(60));
        let executor = executor.with_active_counter(active.clone());
        let (tx, rx) = tokio::sync::oneshot::channel();
        let captured = Dropped(Some(tx));
        executor.execute(async move {
            let _captured = captured;
            std::future::pending::<()>().await;
        });
        assert_eq!(active.load(Ordering::Relaxed), 1);
        drop(guard);
        tokio::time::timeout(Duration::from_secs(1), rx)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(active.load(Ordering::Relaxed), 0);
        let (tx, rx) = tokio::sync::oneshot::channel();
        let captured = Dropped(Some(tx));
        executor.execute(async move {
            let _captured = captured;
            std::future::pending::<()>().await;
        });
        rx.await.unwrap();
        assert_eq!(active.load(Ordering::Relaxed), 0);
    }
}
