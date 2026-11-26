use super::{
    config::{Passthrough, TcpOptions},
    ms, proxy_encode,
    proxy_protocol::{self, Addresses},
    Counters, Group, Session,
};
use crate::admission::PeerGate;
use anyhow::{bail, ensure, Context, Result};
use rustls::server::Acceptor;
use socket2::{SockRef, TcpKeepalive};
use std::{
    collections::HashMap,
    io::{self, Cursor},
    pin::Pin,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    task::{Context as TaskContext, Poll},
    time::Duration,
};
use tokio::{
    io::{self as aio, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf},
    net::{TcpListener, TcpStream, UnixStream},
    sync::watch,
    task::JoinSet,
    time::{timeout_at, Instant},
};

type Match = (Option<String>, Option<Vec<u8>>, Arc<Group>);

struct Routes {
    rules: Vec<Match>,
    bytes: usize,
    deadline: Duration,
}

pub(super) struct Listener {
    options: TcpOptions,
    default: Option<Arc<Group>>,
    routes: Option<Routes>,
    downstream_tls: Option<tokio_rustls::TlsAcceptor>,
    handshake_timeout: Duration,
}

impl Listener {
    pub(super) fn plain(
        group: Arc<Group>,
        options: TcpOptions,
        downstream_tls: Option<tokio_rustls::TlsAcceptor>,
        handshake_timeout: Duration,
    ) -> Self {
        Self {
            options,
            default: Some(group),
            routes: None,
            downstream_tls,
            handshake_timeout,
        }
    }

    pub(super) fn tls(config: Passthrough, groups: &HashMap<&str, Arc<Group>>) -> Self {
        let rules = config
            .routes
            .into_iter()
            .map(|rule| {
                (
                    rule.sni.map(|s| s.to_ascii_lowercase()),
                    rule.alpn.map(String::into_bytes),
                    groups[rule.upstream.as_str()].clone(),
                )
            })
            .collect();
        Self {
            options: config.options,
            default: config
                .default_upstream
                .map(|name| groups[name.as_str()].clone()),
            downstream_tls: None,
            handshake_timeout: Duration::ZERO,
            routes: Some(Routes {
                rules,
                bytes: config.preread_bytes,
                deadline: ms(config.preread_timeout),
            }),
        }
    }

    pub(super) async fn serve(
        self,
        socket: TcpListener,
        mut shutdown: watch::Receiver<crate::listeners::Command>,
        counters: Arc<Counters>,
    ) -> Result<()> {
        let listener = Arc::new(self);
        let peers = listener
            .options
            .max_sessions_per_ip
            .map(|n| Arc::new(PeerGate::new(n)));
        let mut tasks = JoinSet::new();
        loop {
            tokio::select! {
                biased;
                _ = shutdown.changed() => break,
                complete = tasks.join_next(), if !tasks.is_empty() => {
                    complete.expect("active TCP task")?;
                }
                accepted = socket.accept() => {
                    let (client, peer) = match accepted {
                        Ok(pair) => pair,
                        Err(err) if matches!(err.kind(), io::ErrorKind::ConnectionAborted | io::ErrorKind::Interrupted) => continue,
                        Err(err) if matches!(err.raw_os_error(), Some(libc::EMFILE | libc::ENFILE | libc::ENOBUFS | libc::ENOMEM)) => {
                            tokio::select! {
                                _ = shutdown.changed() => break,
                                _ = tokio::time::sleep(ms(100)) => {}
                            }
                            continue;
                        }
                        Err(err) => return Err(err).context("stream TCP accept"),
                    };
                    if tasks.len() >= listener.options.max_sessions {
                        counters.rejected.fetch_add(1, Ordering::Relaxed);
                        continue;
                    }
                    let peer_slot = match &peers {
                        Some(peers) => match peers.enter(peer.ip()) {
                            Some(slot) => Some(slot),
                            None => {
                                counters.rejected.fetch_add(1, Ordering::Relaxed);
                                continue;
                            }
                        },
                        None => None,
                    };
                    let listener = listener.clone();
                    let accounting = Session::new(counters.clone());
                    let started = Instant::now();
                    tasks.spawn(async move {
                        let _peer_slot = peer_slot;
                        let _ = listener.forward(client, started, &accounting.0).await;
                    });
                }
            }
        }
        drop(socket);
        while let Some(result) = tasks.join_next().await {
            result?;
        }
        Ok(())
    }

    async fn forward(
        &self,
        mut client: TcpStream,
        started: Instant,
        counters: &Counters,
    ) -> Result<()> {
        let opts = &self.options;
        let deadline = started + ms(opts.session_timeout);
        apply_options(&client, opts)?;
        let socket_peer = client.peer_addr()?;
        let socket_addrs = Addresses {
            source: socket_peer,
            destination: client.local_addr()?,
        };
        let forwarded = if let Some(inbound) = &opts.proxy_in {
            ensure!(
                inbound.trusted_peers.contains(socket_peer.ip()),
                "untrusted PROXY peer"
            );
            timeout_at(
                deadline.min(started + ms(inbound.header_timeout)),
                read_proxy(&mut client, inbound.max_header_bytes),
            )
            .await??
        } else {
            None
        };
        let addresses = forwarded.unwrap_or(socket_addrs);
        let (group, preread) = if let Some(routes) = &self.routes {
            let (hello, original) = timeout_at(
                deadline.min(Instant::now() + routes.deadline),
                read_hello(&mut client, routes.bytes),
            )
            .await??;
            let hello = hello.client_hello();
            let group = routes
                .rules
                .iter()
                .find(|(name, alpn, _)| {
                    name.as_ref().is_none_or(|name| {
                        hello
                            .server_name()
                            .is_some_and(|sni| name.eq_ignore_ascii_case(sni))
                    }) && alpn.as_ref().is_none_or(|protocol| {
                        hello
                            .alpn()
                            .is_some_and(|mut list| list.any(|id| id == protocol))
                    })
                })
                .map(|(_, _, group)| group)
                .or(self.default.as_ref())
                .context("no TLS route")?;
            (group, original)
        } else {
            (self.default.as_ref().unwrap(), Vec::new())
        };
        let client = if let Some(acceptor) = &self.downstream_tls {
            let handshake = deadline.min(Instant::now() + self.handshake_timeout);
            Ok(timeout_at(handshake, acceptor.accept(client)).await??)
        } else {
            Err(client)
        };
        let member = group
            .select(addresses.source.ip())
            .context("no eligible TCP upstream")?;
        let endpoint = &group.members[member].endpoint;
        let connect = async {
            let mut upstream: Box<dyn crate::ClientIo> =
                if let Some(address) = group.address(member).await? {
                    let stream = TcpStream::connect(address).await?;
                    apply_options(&stream, opts)?;
                    Box::new(stream)
                } else {
                    Box::new(UnixStream::connect(endpoint.unix.as_ref().unwrap()).await?)
                };
            if let Some(version) = opts.proxy_out {
                upstream
                    .write_all(proxy_encode::encode(version, addresses).as_ref())
                    .await?;
            }
            if let Some((connector, name)) = &group.tls {
                let handshake = deadline.min(Instant::now() + self.handshake_timeout);
                upstream = Box::new(
                    timeout_at(handshake, connector.connect(name.clone(), upstream)).await??,
                );
            }
            upstream.write_all(&preread).await?;
            Ok::<_, io::Error>(upstream)
        };
        let connect_deadline = Instant::now() + ms(opts.connect_timeout);
        let upstream = match timeout_at(deadline.min(connect_deadline), connect).await {
            Ok(Ok(stream)) => stream,
            Ok(Err(error)) => {
                group.fail(member, opts.failure_cooldown, counters);
                return Err(error.into());
            }
            Err(error) => {
                if connect_deadline < deadline {
                    group.fail(member, opts.failure_cooldown, counters);
                }
                return Err(error.into());
            }
        };
        drop(preread);
        let idle = ms(opts.idle_timeout);
        let result = match client {
            Ok(secure) => relay(secure, upstream, opts.buffer_bytes, idle, deadline).await,
            Err(plain) => relay(plain, upstream, opts.buffer_bytes, idle, deadline).await,
        };
        if let Err(error) = result {
            if error.upstream {
                group.fail(member, opts.failure_cooldown, counters);
            }
            return Err(error.cause.into());
        }
        Ok(())
    }
}

fn apply_options(socket: &TcpStream, options: &TcpOptions) -> io::Result<()> {
    socket.set_nodelay(options.nodelay)?;
    let socket = SockRef::from(socket);
    if let Some(secs) = options.keepalive_secs {
        socket.set_tcp_keepalive(&TcpKeepalive::new().with_time(Duration::from_secs(secs)))?;
    }
    if let Some(bytes) = options.receive_buffer_bytes {
        socket.set_recv_buffer_size(bytes)?;
    }
    if let Some(bytes) = options.send_buffer_bytes {
        socket.set_send_buffer_size(bytes)?;
    }
    Ok(())
}

async fn read_proxy(client: &mut TcpStream, limit: usize) -> Result<Option<Addresses>> {
    let first = client.read_u8().await?;
    if first == b'P' {
        let mut header = [0; proxy_protocol::V1_MAX];
        header[0] = first;
        for i in 1..limit.min(header.len()) {
            header[i] = client.read_u8().await?;
            if header[i - 1..=i] == *b"\r\n" {
                return proxy_protocol::parse_v1(&header[..=i])
                    .map_err(|_| anyhow::anyhow!("invalid PROXY v1 header"));
            }
        }
        bail!("oversized PROXY v1 header");
    }
    ensure!(first == b'\r', "invalid PROXY signature");
    let mut prefix = [0; 16];
    prefix[0] = first;
    client.read_exact(&mut prefix[1..]).await?;
    let length = proxy_protocol::v2_length(&prefix)
        .map_err(|_| anyhow::anyhow!("invalid PROXY v2 prefix"))?;
    ensure!(length <= limit - prefix.len(), "oversized PROXY v2 header");
    let mut header = vec![0; prefix.len() + length];
    header[..prefix.len()].copy_from_slice(&prefix);
    client.read_exact(&mut header[prefix.len()..]).await?;
    proxy_protocol::parse_v2(&header).map_err(|_| anyhow::anyhow!("invalid PROXY v2 header"))
}

async fn read_hello(
    client: &mut TcpStream,
    limit: usize,
) -> Result<(rustls::server::Accepted, Vec<u8>)> {
    let mut acceptor = Acceptor::default();
    let mut original = Vec::with_capacity(limit);
    let mut buffer = [0u8; 4096];
    while original.len() < limit {
        let chunk = (limit - original.len()).min(buffer.len());
        let count = client.read(&mut buffer[..chunk]).await?;
        ensure!(count != 0, "truncated TLS ClientHello");
        original.extend_from_slice(&buffer[..count]);
        let mut input = Cursor::new(&buffer[..count]);
        while input.position() < count as u64 {
            ensure!(
                acceptor.read_tls(&mut input)? != 0,
                "invalid TLS record stream"
            );
            match acceptor.accept() {
                Ok(Some(hello)) => return Ok((hello, original)),
                Ok(None) => {}
                Err((err, _)) => return Err(err.into()),
            }
        }
    }
    bail!("TLS preread byte limit exceeded");
}

struct Progress<'a, Io> {
    io: Io,
    epoch: Instant,
    last: &'a AtomicU64,
    upstream_error: Option<&'a AtomicBool>,
}

impl<Io> Progress<'_, Io> {
    fn wrote(&self) {
        self.last
            .store(self.epoch.elapsed().as_micros() as u64, Ordering::Relaxed);
    }

    fn error(&self) {
        if let Some(error) = self.upstream_error {
            error.store(true, Ordering::Relaxed);
        }
    }
}

impl<Io: AsyncRead + Unpin> AsyncRead for Progress<'_, Io> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let result = Pin::new(&mut self.io).poll_read(cx, buf);
        if matches!(result, Poll::Ready(Err(_))) {
            self.error();
        }
        result
    }
}

impl<Io: AsyncWrite + Unpin> AsyncWrite for Progress<'_, Io> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let result = Pin::new(&mut self.io).poll_write(cx, buf);
        match &result {
            Poll::Ready(Ok(count)) if *count > 0 => self.wrote(),
            Poll::Ready(Err(_)) => self.error(),
            _ => {}
        }
        result
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<io::Result<()>> {
        let result = Pin::new(&mut self.io).poll_flush(cx);
        if matches!(result, Poll::Ready(Err(_))) {
            self.error();
        }
        result
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<io::Result<()>> {
        let result = Pin::new(&mut self.io).poll_shutdown(cx);
        match &result {
            Poll::Ready(Ok(())) => self.wrote(),
            Poll::Ready(Err(_)) => self.error(),
            _ => {}
        }
        result
    }
}

pub(crate) struct RelayError {
    cause: io::Error,
    upstream: bool,
}

pub(crate) async fn relay(
    client: impl crate::ClientIo,
    upstream: impl crate::ClientIo,
    buffer: usize,
    idle: Duration,
    deadline: Instant,
) -> std::result::Result<(), RelayError> {
    let epoch = Instant::now();
    let last = AtomicU64::new(0);
    let upstream_error = AtomicBool::new(false);
    let mut client = Progress {
        io: client,
        epoch,
        last: &last,
        upstream_error: None,
    };
    let mut upstream = Progress {
        io: upstream,
        epoch,
        last: &last,
        upstream_error: Some(&upstream_error),
    };
    let expired = async {
        loop {
            let recently = epoch + Duration::from_micros(last.load(Ordering::Relaxed)) + idle;
            tokio::time::sleep_until(recently.min(deadline)).await;
            if Instant::now() >= deadline
                || recently == epoch + Duration::from_micros(last.load(Ordering::Relaxed)) + idle
            {
                break;
            }
        }
    };
    let result = tokio::select! {
        biased;
        _ = expired => Err(io::Error::new(io::ErrorKind::TimedOut, "stream deadline")),
        result = aio::copy_bidirectional_with_sizes(&mut client, &mut upstream, buffer, buffer) => result.map(|_| ())
    };
    result.map_err(|cause| RelayError {
        cause,
        upstream: upstream_error.load(Ordering::Relaxed),
    })
}
