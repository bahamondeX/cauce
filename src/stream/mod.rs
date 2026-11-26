//! Raw TCP and UDP listener lifecycle, independent of the HTTP service.
pub(crate) mod config;
mod proxy_encode;
mod proxy_protocol;
pub(crate) mod tcp;
mod udp;

use anyhow::{Context, Result};
use config::{Balance, Endpoint, StreamConfig, Upstream};
use socket2::{Domain, Socket, Type};
use std::{
    collections::HashMap,
    net::{IpAddr, SocketAddr},
    sync::{
        atomic::{AtomicU64, AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    net::{TcpListener, UdpSocket},
    sync::watch,
    task::JoinSet,
    time::Instant,
};

#[derive(Default)]
pub(crate) struct Counters {
    pub(crate) accepted: AtomicU64,
    pub(crate) active: AtomicU64,
    pub(crate) rejected: AtomicU64,
    pub(crate) upstream_failures: AtomicU64,
    pub(crate) datagrams_dropped: AtomicU64,
}

struct Session(Arc<Counters>);

impl Session {
    fn new(counters: Arc<Counters>) -> Self {
        counters.accepted.fetch_add(1, Ordering::Relaxed);
        counters.active.fetch_add(1, Ordering::Relaxed);
        Self(counters)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::Relaxed);
    }
}

struct Member {
    dns_cursor: AtomicUsize,
    endpoint: Endpoint,
    seed: u64,
    failed_until: AtomicU64,
}

struct Group {
    resolver: Option<Arc<crate::dns::Resolver>>,
    members: Vec<Member>,
    tickets: Vec<usize>,
    cursor: AtomicUsize,
    started: Instant,
    balance: Balance,
}

impl Group {
    fn compile(config: &Upstream, resolver: Option<Arc<crate::dns::Resolver>>) -> Self {
        use std::os::unix::ffi::OsStrExt;
        let members = config
            .endpoints
            .iter()
            .map(|endpoint| {
                let seed = if let Some(address) = endpoint.address {
                    crate::balance::hash(address.to_string().as_bytes())
                } else if let Some(destination) = &endpoint.dns_address {
                    crate::balance::hash(destination.as_bytes())
                } else {
                    crate::balance::hash(endpoint.unix.as_ref().unwrap().as_os_str().as_bytes())
                };
                Member {
                    dns_cursor: AtomicUsize::new(0),
                    endpoint: endpoint.clone(),
                    seed,
                    failed_until: AtomicU64::new(0),
                }
            })
            .collect();
        let tickets = if config.balance == Balance::WeightedRoundRobin {
            crate::balance::tickets(
                &config
                    .endpoints
                    .iter()
                    .map(|e| u64::from(e.weight))
                    .collect::<Vec<_>>(),
            )
        } else {
            Vec::new()
        };
        Self {
            resolver,
            members,
            tickets,
            cursor: AtomicUsize::new(0),
            started: Instant::now(),
            balance: config.balance,
        }
    }

    async fn address(&self, index: usize) -> std::io::Result<Option<SocketAddr>> {
        let member = &self.members[index];
        if let Some(destination) = &member.endpoint.dns_address {
            let (address, token) = self
                .resolver
                .as_ref()
                .expect("validated Stream DNS")
                .choose(destination, &member.dns_cursor)
                .await
                .map_err(std::io::Error::other)?;
            if !crate::dns::Address::current(&Some(token)) {
                return Err(std::io::Error::other("dns_address_retired"));
            }
            return Ok(Some(address));
        }
        Ok(member.endpoint.address)
    }

    fn select(&self, ip: IpAddr) -> Option<usize> {
        let now = self.started.elapsed().as_millis() as u64;
        let eligible = |i: usize| self.members[i].failed_until.load(Ordering::Relaxed) <= now;
        match self.balance {
            Balance::WeightedRoundRobin => {
                let start = self.cursor.fetch_add(1, Ordering::Relaxed) % self.tickets.len();
                let first = self.tickets[start];
                if eligible(first) {
                    Some(first)
                } else {
                    // Skip a failed member without repeatedly scanning its weight tickets.
                    (1..self.members.len())
                        .map(|i| (first + i) % self.members.len())
                        .find(|&i| eligible(i))
                }
            }
            Balance::ConsistentHash => {
                let key = crate::balance::key(ip);
                self.members
                    .iter()
                    .enumerate()
                    .filter(|(i, _)| eligible(*i))
                    .map(|(i, member)| {
                        let n = ((crate::balance::mix(key ^ member.seed) >> 11) + 1) as f64;
                        let uniform = n / (1u64 << 53) as f64;
                        (
                            i,
                            -uniform.ln() / f64::from(member.endpoint.weight),
                            member.seed,
                        )
                    })
                    .min_by(|a, b| a.1.total_cmp(&b.1).then_with(|| a.2.cmp(&b.2)))
                    .map(|(i, _, _)| i)
            }
        }
    }

    fn fail(&self, member: usize, cooldown_ms: u64, counters: &Counters) {
        let now = self.started.elapsed().as_millis() as u64;
        self.members[member]
            .failed_until
            .fetch_max(now + cooldown_ms, Ordering::Relaxed);
        counters.upstream_failures.fetch_add(1, Ordering::Relaxed);
    }
}

pub(crate) fn socket(address: SocketAddr, kind: Type, reuse_port: bool) -> std::io::Result<Socket> {
    let socket = Socket::new(Domain::for_address(address), kind, None)?;
    socket.set_nonblocking(true)?;
    if address.is_ipv6() {
        socket.set_only_v6(true)?;
    }
    if kind == Type::STREAM {
        socket.set_reuse_address(true)?;
    }
    if reuse_port {
        socket.set_reuse_port(true)?;
    }
    socket.bind(&address.into())?;
    Ok(socket)
}

pub(crate) struct Listeners {
    tcp: Vec<(TcpListener, tcp::Listener)>,
    udp: Vec<(Arc<UdpSocket>, Arc<Group>, config::UdpOptions)>,
}

impl Listeners {
    pub(crate) fn bind(
        config: StreamConfig,
        resolver: Option<Arc<crate::dns::Resolver>>,
    ) -> Result<Self> {
        let mut tcp_groups = HashMap::new();
        let mut udp_groups = HashMap::new();
        // Health and selection state are specific to the transport.
        for group in &config.upstreams {
            tcp_groups.insert(
                group.id.as_str(),
                Arc::new(Group::compile(group, resolver.clone())),
            );
        }
        let mut listeners = Self {
            tcp: Vec::new(),
            udp: Vec::new(),
        };
        let bind_tcp = |address, reuse_port| -> Result<TcpListener> {
            let socket = socket(address, Type::STREAM, reuse_port)
                .with_context(|| format!("stream TCP bind {address}"))?;
            socket.listen(1024)?;
            Ok(TcpListener::from_std(socket.into())?)
        };
        for entry in config.tcp {
            let bound = bind_tcp(entry.listen, entry.options.reuse_port)?;
            listeners.tcp.push((
                bound,
                tcp::Listener::plain(tcp_groups[entry.upstream.as_str()].clone(), entry.options),
            ));
        }
        for entry in config.tls_passthrough {
            let bound = bind_tcp(entry.listen, entry.options.reuse_port)?;
            listeners
                .tcp
                .push((bound, tcp::Listener::tls(entry, &tcp_groups)));
        }
        for entry in config.udp {
            let group = udp_groups.entry(entry.upstream.clone()).or_insert_with(|| {
                Arc::new(Group::compile(
                    config
                        .upstreams
                        .iter()
                        .find(|group| group.id == entry.upstream)
                        .unwrap(),
                    resolver.clone(),
                ))
            });
            let socket = socket(entry.listen, Type::DGRAM, entry.options.reuse_port)
                .with_context(|| format!("stream UDP bind {}", entry.listen))?;
            listeners.udp.push((
                Arc::new(UdpSocket::from_std(socket.into())?),
                group.clone(),
                entry.options,
            ));
        }
        Ok(listeners)
    }

    pub(crate) fn metrics(&self) -> crate::telemetry::Metrics {
        crate::telemetry::Metrics {
            tcp: (!self.tcp.is_empty()).then(Arc::default),
            udp: (!self.udp.is_empty()).then(Arc::default),
            ..Default::default()
        }
    }

    pub(crate) fn start(
        self,
        shutdown: &watch::Receiver<bool>,
        metrics: &crate::telemetry::Metrics,
    ) -> JoinSet<Result<()>> {
        let mut tasks = JoinSet::new();
        for (socket, listener) in self.tcp {
            tasks.spawn(listener.serve(
                socket,
                shutdown.clone(),
                metrics.tcp.as_ref().unwrap().clone(),
            ));
        }
        for (socket, group, options) in self.udp {
            tasks.spawn(udp::serve(
                socket,
                group,
                options,
                shutdown.clone(),
                metrics.udp.as_ref().unwrap().clone(),
            ));
        }
        tasks
    }
}

fn ms(value: u64) -> Duration {
    Duration::from_millis(value)
}
