use super::{ms, quic, Settings};
use crate::{
    config::Config,
    proxy::{self, State},
};
use anyhow::{Context, Result};
use bytes::{Buf, Bytes};
use h3::{
    error::Code,
    server::{RequestResolver, RequestStream},
};
use http_body_util::BodyExt;
use hyper::{
    body::{Body, Frame},
    header::{HeaderValue, CONTENT_LENGTH},
    Version,
};
use quinn::{crypto::rustls::QuicServerConfig, Endpoint, ServerConfig, TransportConfig, VarInt};
use socket2::{Domain, Socket, Type};
use std::{
    io,
    net::IpAddr,
    pin::Pin,
    sync::{atomic::Ordering, Arc},
    task::{ready, Context as TaskContext, Poll, Waker},
    time::Duration,
};
use tokio::{
    sync::{watch, OwnedSemaphorePermit},
    task::JoinSet,
    time::{timeout_at, Instant},
};

pub(crate) async fn prepare(
    config: &Config,
    managed: crate::tls::ManagedCertificates,
) -> Result<Option<ServerConfig>> {
    let Some(settings) = config.http3.as_ref() else {
        return Ok(None);
    };
    let tls = settings
        .tls
        .clone()
        .or(config.tls.clone())
        .expect("validated QUIC TLS");
    let settings = (**settings).clone();
    tokio::task::spawn_blocking(move || {
        let tls = crate::tls::load_quic(&tls, managed)?;
        let crypto = QuicServerConfig::try_from(tls)?;
        let mut server = ServerConfig::with_crypto(Arc::new(crypto));
        server.migration(settings.path_migration);
        let mut transport = TransportConfig::default();
        transport
            .max_idle_timeout(Some(ms(settings.idle_timeout).try_into()?))
            .max_concurrent_bidi_streams(VarInt::from_u32(settings.max_streams))
            .max_concurrent_uni_streams(VarInt::from_u32(3))
            .stream_receive_window(VarInt::from_u32(128 * 1024))
            .receive_window(VarInt::from_u32(1024 * 1024))
            .send_window(1024 * 1024)
            .datagram_receive_buffer_size(None);
        server.transport_config(Arc::new(transport));
        Ok(Some(server))
    })
    .await?
}

pub(crate) fn bind(settings: &Settings, config: ServerConfig) -> Result<Endpoint> {
    let socket = Socket::new(Domain::for_address(settings.listen), Type::DGRAM, None)?;
    if settings.listen.is_ipv6() {
        socket.set_only_v6(true)?;
    }
    socket.set_nonblocking(true)?;
    socket
        .bind(&settings.listen.into())
        .context("HTTP/3 UDP listener")?;
    let runtime = quinn::default_runtime().context("QUIC async runtime unavailable")?;
    Ok(Endpoint::new(
        Default::default(),
        Some(config),
        socket.into(),
        runtime,
    )?)
}

pub(crate) async fn serve(
    endpoint: Endpoint,
    settings: Settings,
    config: Arc<Config>,
    state: Arc<State>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<()> {
    let mut tasks = JoinSet::new();
    let runtime = Arc::new(Runtime {
        settings,
        config,
        state,
    });
    loop {
        tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            ended = tasks.join_next(), if !tasks.is_empty() => {
                ended.expect("active QUIC connection")?;
            }
            next = endpoint.accept() => {
                let Some(incoming) = next else { break };
                if tasks.len() >= runtime.settings.max_connections {
                    incoming.refuse();
                    runtime.rejected();
                    continue;
                }
                if !incoming.remote_address_validated() {
                    let _ = incoming.retry();
                    continue;
                }
                let Ok(slot) = runtime.state.clients.clone().try_acquire_owned() else {
                    incoming.refuse();
                    runtime.rejected();
                    continue;
                };
                let peer = incoming.remote_address().ip();
                let peer_slot = if let Some(peers) = &runtime.state.peers {
                    let Some(slot) = peers.enter(peer) else {
                        incoming.refuse();
                        runtime.rejected();
                        continue;
                    };
                    Some(slot)
                } else { None };
                let started = Instant::now();
                let Ok(connecting) = incoming.accept() else { continue; };
                let runtime = runtime.clone();
                let shutdown = shutdown.clone();
                tasks.spawn(async move {
                    let _peer_slot = peer_slot;
                    runtime.connection(connecting, started, peer, Arc::new(slot), shutdown).await;
                });
            }
        }
    }
    endpoint.set_server_config(None);
    while let Some(ended) = tasks.join_next().await {
        ended?;
    }
    endpoint.close(qcode(Code::H3_NO_ERROR), b"gateway drain");
    endpoint.wait_idle().await;
    Ok(())
}

struct Runtime {
    settings: Settings,
    config: Arc<Config>,
    state: Arc<State>,
}

impl Runtime {
    fn rejected(&self) {
        self.state
            .logger
            .metrics
            .rejected
            .fetch_add(1, Ordering::Relaxed);
    }

    async fn connection(
        self: Arc<Self>,
        connecting: quinn::Connecting,
        started: Instant,
        peer: IpAddr,
        slot: Arc<OwnedSemaphorePermit>,
        mut shutdown: watch::Receiver<bool>,
    ) {
        let limits = &self.config.limits;
        let deadline = started + ms(self.settings.connection_timeout);
        let handshake_deadline = (started + ms(limits.tls_handshake_ms)).min(deadline);
        let connection = tokio::select! {
            _ = shutdown.changed() => return,
            result = timeout_at(handshake_deadline, connecting) => match result {
                Ok(Ok(conn)) => conn,
                _ => return,
            },
        };
        let notifications = quic::Accepted::default();
        let transport = quic::Connection::new(
            connection.clone(),
            self.settings.max_field_bytes,
            limits.max_body_bytes,
            notifications.clone(),
        );
        let mut builder = h3::server::builder();
        builder
            .max_field_section_size(self.settings.max_field_bytes as u64)
            .send_grease(false);
        let Ok(Ok(mut server)) = timeout_at(handshake_deadline, builder.build(transport)).await
        else {
            connection.close(qcode(Code::H3_REQUEST_CANCELLED), b"HTTP/3 setup deadline");
            return;
        };
        let mut streams = JoinSet::new();
        let mut accepted_requests = 0;
        let peer_header = HeaderValue::from_str(&peer.to_string()).unwrap();
        let expiry = tokio::time::sleep_until(deadline);
        tokio::pin!(expiry);
        let drain = loop {
            if accepted_requests == self.settings.max_requests_per_connection {
                break true;
            }
            tokio::select! {
                biased;
                _ = shutdown.changed() => break true,
                _ = &mut expiry => break false,
                _ = streams.join_next(), if !streams.is_empty() => {},
                next = server.accept(), if streams.len() < self.settings.max_streams as usize => {
                    let Ok(Some(request)) = next else { break false; };
                    accepted_requests += 1;
                    let cancellation = notifications.lock().unwrap().take().expect("accepted QUIC stream has cancellation future");
                    let runtime = self.clone();
                    let slot = slot.clone();
                    let peer_header = peer_header.clone();
                    let accepted = Instant::now();
                    let deadlines = Deadlines {
                        headers: (accepted + ms(limits.client_headers_ms)).min(deadline),
                        absolute: (accepted + ms(limits.request_ms)).min(deadline),
                        idle: ms(limits.stream_idle_ms),
                    };
                    streams.spawn(async move {
                        runtime.request(request, cancellation, peer, peer_header, slot, deadlines).await;
                    });
                }
            }
        };
        if drain {
            let _ = timeout_at(deadline, server.shutdown(0)).await;
            // Drive control frames during drain. h3 rejects request IDs past
            // the GOAWAY cutoff while admitted requests finish.
            while !streams.is_empty() {
                tokio::select! {
                    _ = &mut expiry => break,
                    _ = streams.join_next() => {},
                    next = server.accept() => if !matches!(next, Ok(Some(_))) { break; },
                }
            }
        }
        connection.close(qcode(Code::H3_NO_ERROR), b"connection end");
    }

    async fn request(
        &self,
        incoming: RequestResolver<quic::Connection, Bytes>,
        mut cancellation: quic::Stop,
        peer: IpAddr,
        peer_header: HeaderValue,
        slot: Arc<OwnedSemaphorePermit>,
        deadlines: Deadlines,
    ) {
        let (mut req, stream) = tokio::select! {
            _ = &mut cancellation => return,
            resolved = timeout_at(deadlines.headers.min(deadlines.absolute), incoming.resolve_request()) => {
                let Ok(Ok(pair)) = resolved else { return; };
                pair
            }
        };
        let (mut send, recv) = stream.split();
        *req.version_mut() = Version::HTTP_3;
        let expected = req
            .headers()
            .get(CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok()?.parse().ok());
        let upload = Upload::new(recv, expected);
        let work = async {
            let result = proxy::handle(
                req.map(|_| upload),
                peer,
                peer_header,
                self.state.clone(),
                true,
                slot,
                crate::diagnostics::downstream_id(),
            )
            .await;
            let response = result.unwrap_or_else(|err| match err {});
            write_response(&mut send, response, &deadlines).await
        };
        let success = tokio::select! {
            _ = &mut cancellation => false,
            result = timeout_at(deadlines.absolute, work) => matches!(result, Ok(Ok(()))),
        };
        if success {
            // The stopped future resolves after FIN and data are acknowledged.
            // Keep the connection drain aware of pending delivery.
            let _ = timeout_at(deadlines.absolute, cancellation).await;
        } else {
            send.stop_stream(Code::H3_REQUEST_CANCELLED);
        }
    }
}

struct Deadlines {
    headers: Instant,
    absolute: Instant,
    idle: Duration,
}

async fn write_response(
    send: &mut RequestStream<quic::ResponseSend, Bytes>,
    response: hyper::Response<crate::body::ProxyBody>,
    deadlines: &Deadlines,
) -> Result<()> {
    let (mut parts, mut body) = response.into_parts();
    parts.version = Version::HTTP_3;
    parts.headers.remove(hyper::header::TRANSFER_ENCODING);
    timeout_at(
        Instant::now() + deadlines.idle,
        send.send_response(hyper::Response::from_parts(parts, ())),
    )
    .await??;
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(anyhow::Error::msg)?;
        match frame.into_data() {
            Ok(data) if !data.is_empty() => {
                timeout_at(Instant::now() + deadlines.idle, send.send_data(data)).await??
            }
            Err(frame) => {
                if let Ok(trailers) = frame.into_trailers() {
                    timeout_at(
                        Instant::now() + deadlines.idle,
                        send.send_trailers(trailers),
                    )
                    .await??;
                }
            }
            _ => {}
        }
    }
    timeout_at(Instant::now() + deadlines.idle, send.finish()).await??;
    Ok(())
}

fn qcode(code: Code) -> VarInt {
    VarInt::from_u64(code.value()).unwrap()
}

#[derive(PartialEq)]
enum Phase {
    Data,
    Trailers,
    Done,
}

pub(crate) struct Upload {
    recv: RequestStream<quic::RequestRecv, Bytes>,
    phase: Phase,
    expected: Option<u64>,
    bytes: u64,
    prefetched: Option<Result<Frame<Bytes>, io::Error>>,
}

impl Upload {
    fn new(recv: RequestStream<quic::RequestRecv, Bytes>, expected: Option<u64>) -> Self {
        let mut this = Self {
            recv,
            phase: Phase::Data,
            expected,
            bytes: 0,
            prefetched: None,
        };
        // Empty requests can use the existing cache and known-empty upstream
        // path when FIN and HEADERS were received together.
        let cx = &mut TaskContext::from_waker(Waker::noop());
        if let Poll::Ready(Some(frame)) = Pin::new(&mut this).poll_frame(cx) {
            this.prefetched = Some(frame);
        }
        this
    }
}

impl Body for Upload {
    type Data = Bytes;
    type Error = io::Error;

    fn is_end_stream(&self) -> bool {
        self.phase == Phase::Done && self.prefetched.is_none()
    }

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, io::Error>>> {
        if let Some(frame) = self.prefetched.take() {
            return Poll::Ready(Some(frame));
        }
        if self.phase == Phase::Data {
            let next = ready!(self.recv.poll_recv_data(cx)).map(|item| {
                item.map(|mut buf| {
                    let size = buf.remaining();
                    buf.copy_to_bytes(size)
                })
            });
            match next {
                Ok(Some(data)) => {
                    self.bytes += data.len() as u64;
                    if self.expected.is_some_and(|length| self.bytes > length) {
                        self.phase = Phase::Done;
                        return Poll::Ready(Some(Err(io::Error::other(
                            "HTTP/3 request length exceeded",
                        ))));
                    }
                    return Poll::Ready(Some(Ok(Frame::data(data))));
                }
                Ok(None) => {
                    self.phase = Phase::Trailers;
                    if self.expected.is_some_and(|length| self.bytes != length) {
                        self.phase = Phase::Done;
                        return Poll::Ready(Some(Err(io::Error::other(
                            "incomplete HTTP/3 request body",
                        ))));
                    }
                }
                Err(error) => {
                    self.phase = Phase::Done;
                    return Poll::Ready(Some(Err(io::Error::other(error))));
                }
            }
        }
        if self.phase == Phase::Trailers {
            let trailers = ready!(self.recv.poll_recv_trailers(cx));
            self.phase = Phase::Done;
            return Poll::Ready(
                trailers
                    .map_err(io::Error::other)
                    .transpose()
                    .map(|frame| frame.map(Frame::trailers)),
            );
        }
        Poll::Ready(None)
    }
}

impl crate::proxy::RequestBody for Upload {
    const HTTP3: bool = true;
}
