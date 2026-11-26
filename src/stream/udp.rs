use super::{
    config::{UdpOptions, UDP_QUEUE},
    ms, Counters, Group, Session,
};
use crate::admission::{PeerGate, PeerSlot};
use anyhow::{Context, Result};
use bytes::Bytes;
use std::{
    collections::HashMap,
    io,
    net::SocketAddr,
    sync::{atomic::Ordering, Arc},
};
use tokio::{
    net::UdpSocket,
    sync::{mpsc, watch},
    task::JoinSet,
    time::{sleep_until, Instant},
};

pub(super) async fn serve(
    socket: Arc<UdpSocket>,
    group: Arc<Group>,
    options: UdpOptions,
    mut shutdown: watch::Receiver<bool>,
    counters: Arc<Counters>,
) -> Result<()> {
    let mut buffer = vec![0; options.max_datagram_bytes + 1];
    let peers = Arc::new(PeerGate::new(options.max_sessions_per_ip));
    let options = Arc::new(options);
    let mut sessions = HashMap::<SocketAddr, mpsc::Sender<Bytes>>::new();
    let mut tasks = JoinSet::new();
    loop {
        tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            ended = tasks.join_next(), if !tasks.is_empty() => {
                sessions.remove(&ended.expect("active UDP session")?);
            }
            packet = socket.recv_from(&mut buffer) => {
                let (size, client) = match packet {
                    Ok(packet) => packet,
                    Err(err) if matches!(err.kind(), io::ErrorKind::ConnectionReset | io::ErrorKind::Interrupted) => continue,
                    Err(err) if err.raw_os_error() == Some(libc::EMSGSIZE) => {
                        dropped(&counters);
                        continue;
                    }
                    Err(err) => return Err(err).context("stream UDP receive"),
                };
                if size > options.max_datagram_bytes || client.port() == 0 || client.ip().is_unspecified() || client.ip().is_multicast() {
                    dropped(&counters);
                    continue;
                }
                if let Some(session) = sessions.get(&client) {
                    if let Ok(permit) = session.try_reserve() {
                        permit.send(Bytes::copy_from_slice(&buffer[..size]));
                    } else {
                        dropped(&counters);
                    }
                    continue;
                }
                let admitted = (sessions.len() < options.max_sessions)
                    .then(|| peers.enter(client.ip()))
                    .flatten()
                    .and_then(|peer| group.select(client.ip()).map(|member| (peer, member)));
                let Some((peer, member)) = admitted else {
                    counters.rejected.fetch_add(1, Ordering::Relaxed);
                    dropped(&counters);
                    continue;
                };
                let (sender, receiver) = mpsc::channel(UDP_QUEUE);
                sender.try_send(Bytes::copy_from_slice(&buffer[..size])).expect("empty UDP session queue");
                sessions.insert(client, sender);
                let relay = Relay { socket: socket.clone(), group: group.clone(), options: options.clone() };
                let accounting = Session::new(counters.clone());
                tasks.spawn(relay.run(client, member, receiver, accounting, peer));
            }
        }
    }
    // Closing all producers prevents new requests. Sessions may finish sending replies
    // for their queued requests until idle, their deadline, or the parent drain deadline.
    drop(sessions);
    while let Some(result) = tasks.join_next().await {
        result?;
    }
    Ok(())
}

fn dropped(counters: &Counters) {
    counters.datagrams_dropped.fetch_add(1, Ordering::Relaxed);
}

struct Relay {
    socket: Arc<UdpSocket>,
    group: Arc<Group>,
    options: Arc<UdpOptions>,
}

impl Relay {
    async fn run(
        self,
        client: SocketAddr,
        member: usize,
        mut pending: mpsc::Receiver<Bytes>,
        accounting: Session,
        _peer: PeerSlot,
    ) -> SocketAddr {
        let counters = &accounting.0;
        let deadline = Instant::now() + ms(self.options.session_timeout);
        let result = async {
            let address = tokio::time::timeout_at(deadline, self.group.address(member))
                .await??
                .expect("validated UDP destination");
            let wildcard: SocketAddr = if address.is_ipv4() {
                (std::net::Ipv4Addr::UNSPECIFIED, 0).into()
            } else {
                (std::net::Ipv6Addr::UNSPECIFIED, 0).into()
            };
            let upstream = UdpSocket::bind(wildcard).await?;
            upstream.connect(address).await?;
            self.forward(&upstream, client, &mut pending, deadline, counters)
                .await
        }
        .await;
        if let Err(error) = result {
            // Local socket exhaustion cannot prove that an upstream is unhealthy.
            if matches!(
                error.kind(),
                io::ErrorKind::ConnectionRefused
                    | io::ErrorKind::ConnectionReset
                    | io::ErrorKind::HostUnreachable
                    | io::ErrorKind::NetworkUnreachable
            ) {
                self.group
                    .fail(member, self.options.failure_cooldown, counters);
            }
            dropped(counters);
        }
        client
    }

    async fn forward(
        &self,
        upstream: &UdpSocket,
        client: SocketAddr,
        pending: &mut mpsc::Receiver<Bytes>,
        deadline: Instant,
        counters: &Counters,
    ) -> io::Result<()> {
        let mut buffer = vec![0; self.options.max_datagram_bytes + 1];
        let mut credits = Credits::new(&self.options);
        let mut input_closed = false;
        let idle = ms(self.options.idle_timeout);
        let timer = sleep_until(deadline.min(Instant::now() + idle));
        tokio::pin!(timer);
        loop {
            if timer.is_elapsed() || (input_closed && credits.packets == 0) {
                break;
            }
            tokio::select! {
                _ = &mut timer => break,
                packet = pending.recv(), if !input_closed => {
                    let Some(packet) = packet else {
                        input_closed = true;
                        continue;
                    };
                    let send = tokio::select! {
                        _ = &mut timer => break,
                        result = upstream.send(&packet) => result,
                    };
                    match send {
                        Ok(n) if n == packet.len() => {
                            credits.request(n);
                            timer.as_mut().reset(deadline.min(Instant::now() + idle));
                        }
                        Err(err) if err.kind() == io::ErrorKind::WouldBlock => dropped(counters),
                        Err(err) => return Err(err),
                        Ok(_) => dropped(counters),
                    }
                }
                result = upstream.recv(&mut buffer) => {
                    let size = match result {
                        Ok(size) => size,
                        Err(err) if err.raw_os_error() == Some(libc::EMSGSIZE) => {
                            dropped(counters);
                            continue;
                        }
                        Err(err) => return Err(err),
                    };
                    if size <= self.options.max_datagram_bytes && credits.response(size) {
                        if tokio::select! {
                            _ = &mut timer => break,
                            result = self.socket.send_to(&buffer[..size], client) => result,
                        }.is_err() {
                            dropped(counters);
                        }
                    } else {
                        dropped(counters);
                    }
                }
            }
        }
        Ok(())
    }
}

// Charge a byte even for empty packets, and never bank more than a bounded
// burst. Both byte and packet limits apply across an entire session.
struct Credits {
    packets: usize,
    bytes: usize,
    ratio: usize,
    max_bytes: usize,
}

impl Credits {
    fn new(options: &UdpOptions) -> Self {
        let ratio = usize::from(options.max_response_ratio);
        Self {
            packets: 0,
            bytes: 0,
            ratio,
            max_bytes: 64 * options.max_datagram_bytes * ratio,
        }
    }

    fn request(&mut self, size: usize) {
        self.packets = (self.packets + 1).min(64);
        self.bytes = (self.bytes + size.max(1) * self.ratio).min(self.max_bytes);
    }

    fn response(&mut self, size: usize) -> bool {
        let bytes = size.max(1);
        if self.packets == 0 || self.bytes < bytes {
            return false;
        }
        self.packets -= 1;
        self.bytes -= bytes;
        if self.packets == 0 {
            self.bytes = 0;
        }
        true
    }
}
