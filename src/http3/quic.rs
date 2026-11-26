//! Quinn-backed h3 streams with early response cancellation and wire budgets.
use bytes::{Buf, Bytes};
use futures_util::{stream, Stream, StreamExt};
use h3::{
    error::Code,
    quic::{
        self, ConnectionErrorIncoming, RecvStream, SendStream, StreamErrorIncoming, StreamId,
        WriteBuf,
    },
};
use quinn::VarInt;
use std::{
    future::Future,
    io,
    pin::Pin,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    task::{ready, Context, Poll},
};

type Incoming = Result<(quinn::SendStream, quinn::RecvStream), quinn::ConnectionError>;
pub(super) type Stop = Pin<Box<dyn Future<Output = ()> + Send>>;
pub(super) type Accepted = Arc<Mutex<Option<Stop>>>;

fn upstream_error(error: quinn::ConnectionError) -> ConnectionErrorIncoming {
    match error {
        quinn::ConnectionError::ApplicationClosed(close) => {
            ConnectionErrorIncoming::ApplicationClose {
                error_code: close.error_code.into(),
            }
        }
        quinn::ConnectionError::TimedOut => ConnectionErrorIncoming::Timeout,
        error => ConnectionErrorIncoming::Undefined(Arc::new(error)),
    }
}

fn stream_error(error: quinn::ConnectionError) -> StreamErrorIncoming {
    StreamErrorIncoming::ConnectionErrorIncoming {
        connection_error: upstream_error(error),
    }
}

fn terminated(error_code: Code) -> StreamErrorIncoming {
    StreamErrorIncoming::StreamTerminated {
        error_code: error_code.value(),
    }
}

pub(super) struct Connection {
    control: h3_quinn::Connection,
    incoming: Pin<Box<dyn Stream<Item = Incoming> + Send>>,
    accepted: Accepted,
    max_frame: u64,
    max_body: u64,
}

impl Connection {
    pub(super) fn new(
        conn: quinn::Connection,
        max_headers: usize,
        max_body: u64,
        accepted: Accepted,
    ) -> Self {
        let incoming = Box::pin(stream::unfold(conn.clone(), |conn| async {
            Some((conn.accept_bi().await, conn))
        }));
        Self {
            control: h3_quinn::Connection::new(conn),
            incoming,
            accepted,
            // QPACK Huffman can increase wire size relative to the decoded form.
            max_frame: (max_headers as u64 * 2).max(4096),
            max_body,
        }
    }
}

impl quic::Connection<Bytes> for Connection {
    type RecvStream = ControlRecv;
    type OpenStreams = OpenControl;

    fn poll_accept_recv(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<ControlRecv, ConnectionErrorIncoming>> {
        quic::Connection::<Bytes>::poll_accept_recv(&mut self.control, cx).map_ok(|recv| {
            ControlRecv {
                recv,
                remaining: 64 * 1024,
            }
        })
    }

    fn poll_accept_bidi(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<Bidi, ConnectionErrorIncoming>> {
        let (send, recv) = ready!(self.incoming.poll_next_unpin(cx))
            .expect("QUIC accept stream is never exhausted")
            .map_err(upstream_error)?;
        let stopped = send.stopped();
        // h3 accepts at most one request per call to accept. A later stream can
        // replace this slot only when h3 itself rejects a stream during GOAWAY.
        *self.accepted.lock().unwrap() = Some(Box::pin(async {
            let _ = stopped.await;
        }));
        let id = stream_id(recv.id());
        let reset_code = Arc::new(AtomicU64::new(Code::H3_REQUEST_INCOMPLETE.value()));
        let pending = Box::pin(stream::unfold(recv, |mut recv| async {
            Some((recv.read_chunk(16 * 1024, true).await, recv))
        }));
        Poll::Ready(Ok(Bidi {
            send: ResponseSend {
                stream: send,
                writing: None,
                finished: false,
                reset_code: reset_code.clone(),
            },
            recv: RequestRecv {
                reset_code,
                pending: Some(pending),
                id,
                frame: FrameBudget::default(),
                max_frame: self.max_frame,
                max_body: self.max_body,
                remaining: self.max_body.saturating_add(4 * self.max_frame + 64 * 1024),
            },
        }))
    }

    fn opener(&self) -> OpenControl {
        OpenControl(quic::Connection::<Bytes>::opener(&self.control))
    }
}

fn unsupported() -> Poll<Result<Bidi, StreamErrorIncoming>> {
    Poll::Ready(Err(terminated(Code::H3_STREAM_CREATION_ERROR)))
}

impl quic::OpenStreams<Bytes> for Connection {
    type SendStream = h3_quinn::SendStream<Bytes>;
    type BidiStream = Bidi;

    fn poll_open_bidi(&mut self, _: &mut Context<'_>) -> Poll<Result<Bidi, StreamErrorIncoming>> {
        unsupported()
    }

    fn poll_open_send(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<Self::SendStream, StreamErrorIncoming>> {
        self.control.poll_open_send(cx)
    }

    fn close(&mut self, code: Code, reason: &[u8]) {
        quic::OpenStreams::<Bytes>::close(&mut self.control, code, reason);
    }
}

pub(super) struct OpenControl(h3_quinn::OpenStreams);

impl quic::OpenStreams<Bytes> for OpenControl {
    type SendStream = h3_quinn::SendStream<Bytes>;
    type BidiStream = Bidi;

    fn poll_open_bidi(&mut self, _: &mut Context<'_>) -> Poll<Result<Bidi, StreamErrorIncoming>> {
        unsupported()
    }

    fn poll_open_send(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<Self::SendStream, StreamErrorIncoming>> {
        self.0.poll_open_send(cx)
    }

    fn close(&mut self, code: Code, reason: &[u8]) {
        quic::OpenStreams::<Bytes>::close(&mut self.0, code, reason);
    }
}

pub(super) struct ControlRecv {
    recv: h3_quinn::RecvStream,
    remaining: usize,
}

impl RecvStream for ControlRecv {
    type Buf = Bytes;

    fn poll_data(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<Option<Bytes>, StreamErrorIncoming>> {
        let bytes = ready!(self.recv.poll_data(cx))?;
        if let Some(bytes) = &bytes {
            self.remaining = self
                .remaining
                .checked_sub(bytes.len())
                .ok_or_else(|| terminated(Code::H3_EXCESSIVE_LOAD))?;
        }
        Poll::Ready(Ok(bytes))
    }

    fn stop_sending(&mut self, code: u64) {
        self.recv.stop_sending(code);
    }

    fn recv_id(&self) -> StreamId {
        self.recv.recv_id()
    }
}

type ReadChunk = Result<Option<quinn::Chunk>, quinn::ReadError>;
type ReadChunks = Pin<Box<dyn Stream<Item = ReadChunk> + Send + Sync>>;

pub(super) struct RequestRecv {
    reset_code: Arc<AtomicU64>,
    pending: Option<ReadChunks>,
    id: StreamId,
    frame: FrameBudget,
    max_frame: u64,
    max_body: u64,
    remaining: u64,
}

impl RecvStream for RequestRecv {
    type Buf = Bytes;

    fn poll_data(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<Option<Bytes>, StreamErrorIncoming>> {
        let Some(read) = &mut self.pending else {
            return Poll::Ready(Ok(None));
        };
        let next = ready!(read.poll_next_unpin(cx)).expect("QUIC read stream yields a result");
        match next {
            Ok(Some(chunk)) => {
                let remaining = self.remaining.checked_sub(chunk.bytes.len() as u64);
                if let Some(remaining) = remaining.filter(|_| {
                    self.frame
                        .check(&chunk.bytes, self.max_frame, self.max_body)
                }) {
                    self.remaining = remaining;
                } else {
                    self.reset_code
                        .store(Code::H3_EXCESSIVE_LOAD.value(), Ordering::Relaxed);
                    self.pending = None;
                    return Poll::Ready(Err(terminated(Code::H3_EXCESSIVE_LOAD)));
                }
                Poll::Ready(Ok(Some(chunk.bytes)))
            }
            Ok(None) => {
                self.pending = None;
                Poll::Ready(Ok(None))
            }
            Err(error) => {
                self.pending = None;
                let error = match error {
                    quinn::ReadError::Reset(code) => StreamErrorIncoming::StreamTerminated {
                        error_code: code.into(),
                    },
                    quinn::ReadError::ConnectionLost(error) => stream_error(error),
                    error => StreamErrorIncoming::Unknown(Box::new(error)),
                };
                Poll::Ready(Err(error))
            }
        }
    }

    fn stop_sending(&mut self, _: u64) {
        // Dropping the h3 receiver drops Quinn's pending read and RecvStream.
        // Quinn stops reception even if the read future was mid-poll.
        self.pending = None;
    }

    fn recv_id(&self) -> StreamId {
        self.id
    }
}

#[derive(Default)]
struct FrameBudget {
    kind: Option<u64>,
    partial: u64,
    width: u8,
    remaining: u64,
}

impl FrameBudget {
    // QUIC varints are at most eight bytes. Track only frame lengths here and
    // let h3 parse frame types, payloads and QPACK.
    fn check(&mut self, mut wire: &[u8], max_frame: u64, max_body: u64) -> bool {
        while !wire.is_empty() {
            if self.remaining > 0 {
                let skip = self.remaining.min(wire.len() as u64);
                self.remaining -= skip;
                wire = &wire[skip as usize..];
                continue;
            }
            let byte = wire[0];
            wire = &wire[1..];
            if self.width == 0 {
                self.width = 1 << (byte >> 6);
                self.partial = u64::from(byte & 0x3f);
            } else {
                self.partial = (self.partial << 8) | u64::from(byte);
            }
            self.width -= 1;
            if self.width == 0 {
                if let Some(kind) = self.kind.take() {
                    self.remaining = self.partial;
                    let limit = if kind == 0 { max_body } else { max_frame };
                    if self.remaining > limit {
                        return false;
                    }
                } else {
                    self.kind = Some(self.partial);
                }
            }
        }
        true
    }
}

pub(super) struct ResponseSend {
    stream: quinn::SendStream,
    writing: Option<WriteBuf<Bytes>>,
    finished: bool,
    reset_code: Arc<AtomicU64>,
}

impl Drop for ResponseSend {
    fn drop(&mut self) {
        if !self.finished {
            self.reset(self.reset_code.load(Ordering::Relaxed));
        }
    }
}

impl SendStream<Bytes> for ResponseSend {
    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), StreamErrorIncoming>> {
        if let Some(buf) = &mut self.writing {
            while buf.has_remaining() {
                let n = ready!(Pin::new(&mut self.stream).poll_write(cx, buf.chunk())).map_err(
                    |error| match error {
                        quinn::WriteError::Stopped(code) => StreamErrorIncoming::StreamTerminated {
                            error_code: code.into(),
                        },
                        quinn::WriteError::ConnectionLost(error) => stream_error(error),
                        error => StreamErrorIncoming::Unknown(Box::new(error)),
                    },
                )?;
                buf.advance(n);
            }
        }
        self.writing = None;
        Poll::Ready(Ok(()))
    }

    fn send_data<D: Into<WriteBuf<Bytes>>>(&mut self, data: D) -> Result<(), StreamErrorIncoming> {
        if self.writing.is_some() {
            return Err(StreamErrorIncoming::Unknown(Box::new(io::Error::other(
                "pending HTTP/3 send",
            ))));
        }
        self.writing = Some(data.into());
        Ok(())
    }

    fn poll_finish(&mut self, _: &mut Context<'_>) -> Poll<Result<(), StreamErrorIncoming>> {
        self.finished = true;
        Poll::Ready(
            self.stream
                .finish()
                .map_err(|e| StreamErrorIncoming::Unknown(Box::new(e))),
        )
    }

    fn reset(&mut self, code: u64) {
        self.finished = true;
        let _ = self
            .stream
            .reset(VarInt::from_u64(code).expect("HTTP/3 code fits QUIC varint"));
    }

    fn send_id(&self) -> StreamId {
        stream_id(self.stream.id())
    }
}

fn stream_id(id: quinn::StreamId) -> StreamId {
    u64::from(id).try_into().expect("Quinn stream ID is valid")
}

pub(super) struct Bidi {
    send: ResponseSend,
    recv: RequestRecv,
}

impl RecvStream for Bidi {
    type Buf = Bytes;

    fn poll_data(
        &mut self,
        cx: &mut Context<'_>,
    ) -> Poll<Result<Option<Bytes>, StreamErrorIncoming>> {
        self.recv.poll_data(cx)
    }

    fn stop_sending(&mut self, code: u64) {
        self.recv.stop_sending(code)
    }

    fn recv_id(&self) -> StreamId {
        self.recv.recv_id()
    }
}

impl SendStream<Bytes> for Bidi {
    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), StreamErrorIncoming>> {
        self.send.poll_ready(cx)
    }

    fn send_data<D: Into<WriteBuf<Bytes>>>(&mut self, data: D) -> Result<(), StreamErrorIncoming> {
        self.send.send_data(data)
    }

    fn poll_finish(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), StreamErrorIncoming>> {
        self.send.poll_finish(cx)
    }

    fn reset(&mut self, code: u64) {
        self.send.reset(code)
    }

    fn send_id(&self) -> StreamId {
        self.send.send_id()
    }
}

impl quic::BidiStream<Bytes> for Bidi {
    type SendStream = ResponseSend;
    type RecvStream = RequestRecv;

    fn split(self) -> (Self::SendStream, Self::RecvStream) {
        (self.send, self.recv)
    }
}
