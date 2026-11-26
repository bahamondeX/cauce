//! Bounded address-set publication for configured recursive DNS.
use anyhow::{anyhow, ensure, Result};
use hickory_resolver::{
    config::{LookupIpStrategy, NameServerConfig, ResolveHosts, ResolverConfig},
    net::runtime::TokioRuntimeProvider,
    TokioResolver,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, HashMap},
    net::{IpAddr, SocketAddr},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::sync::{Mutex as AsyncMutex, Semaphore};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Settings {
    servers: Vec<SocketAddr>,
    max_hosts: usize,
    max_concurrent: usize,
    #[serde(deserialize_with = "crate::config::milliseconds")]
    timeout_ms: u64,
    #[serde(deserialize_with = "crate::config::milliseconds")]
    failure_cooldown_ms: u64,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            servers: Vec::new(),
            max_hosts: 1024,
            max_concurrent: 32,
            timeout_ms: 2000,
            failure_cooldown_ms: 250,
        }
    }
}

impl Settings {
    pub(crate) fn validate(&self) -> Result<()> {
        ensure!(
            (1..=8).contains(&self.servers.len())
                && self.servers.iter().all(|server| server.port() != 0
                    && !server.ip().is_unspecified()
                    && !server.ip().is_multicast())
                && (1..=4096).contains(&self.max_hosts)
                && (1..=128).contains(&self.max_concurrent)
                && (10..=30_000).contains(&self.timeout_ms)
                && (10..=30_000).contains(&self.failure_cooldown_ms),
            "invalid DNS servers, capacity or deadline"
        );
        Ok(())
    }
}

#[derive(Clone)]
pub(crate) struct Address(Arc<AtomicBool>);

impl Address {
    pub(crate) fn current(address: &Option<Self>) -> bool {
        address
            .as_ref()
            .is_none_or(|address| address.0.load(Ordering::Acquire))
    }
}

type Addresses = Arc<[(IpAddr, Address)]>;

struct Cached {
    addresses: Addresses,
    valid_until: Instant,
    failed: bool,
}

impl Cached {
    fn result(&self) -> Result<Addresses> {
        if self.failed {
            return Err(anyhow!("dns_resolution_failed"));
        }
        Ok(self.addresses.clone())
    }

    fn retire(&self) {
        for (_, address) in self.addresses.iter() {
            address.0.store(false, Ordering::Release);
        }
    }
}

#[derive(Default)]
struct Host {
    cached: AsyncMutex<Option<Cached>>,
}

pub(crate) struct Resolver {
    client: TokioResolver,
    hosts: Mutex<HashMap<String, Arc<Host>>>,
    queries: Semaphore,
    settings: Settings,
}

impl Resolver {
    pub(crate) fn new(settings: &Settings) -> Result<Arc<Self>> {
        let servers = settings
            .servers
            .iter()
            .map(|server| {
                let mut config = NameServerConfig::udp_and_tcp(server.ip());
                for connection in &mut config.connections {
                    connection.port = server.port();
                }
                config
            })
            .collect();
        let config = ResolverConfig::from_parts(None, Vec::new(), servers);
        let mut builder =
            TokioResolver::builder_with_config(config, TokioRuntimeProvider::default());
        let options = builder.options_mut();
        options.ip_strategy = LookupIpStrategy::Ipv4AndIpv6;
        options.use_hosts_file = ResolveHosts::Never;
        options.cache_size = (settings.max_hosts * 2) as u64;
        options.timeout = Duration::from_millis(settings.timeout_ms);
        options.num_concurrent_reqs = 1;
        options.attempts = 1;
        Ok(Arc::new(Self {
            client: builder.build()?,
            hosts: Mutex::new(HashMap::new()),
            queries: Semaphore::new(settings.max_concurrent),
            settings: settings.clone(),
        }))
    }

    pub(crate) async fn choose(
        &self,
        destination: &str,
        cursor: &std::sync::atomic::AtomicUsize,
    ) -> Result<(SocketAddr, Address)> {
        let (host, port) = host_port(destination).ok_or_else(|| anyhow!("invalid_dns_upstream"))?;
        let addresses = self.resolve(host).await?;
        let (ip, token) = &addresses[cursor.fetch_add(1, Ordering::Relaxed) % addresses.len()];
        Ok((SocketAddr::new(*ip, port), token.clone()))
    }

    pub(crate) async fn refresh(&self, destination: &str) -> Result<()> {
        let (host, _) = host_port(destination).ok_or_else(|| anyhow!("invalid_dns_upstream"))?;
        self.resolve(host).await?;
        Ok(())
    }

    async fn resolve(&self, host: &str) -> Result<Addresses> {
        let host = format!("{}.", host.trim_end_matches('.').to_ascii_lowercase());
        let slot = {
            let mut hosts = self.hosts.lock().unwrap();
            if let Some(slot) = hosts.get(&host) {
                slot.clone()
            } else {
                if hosts.len() >= self.settings.max_hosts {
                    let evict = hosts.iter().find_map(|(name, slot)| {
                        // The map owns the only reference while the entry is idle.
                        (Arc::strong_count(slot) == 1).then(|| name.clone())
                    });
                    let Some(evict) = evict else {
                        return Err(anyhow!("dns_host_capacity"));
                    };
                    let removed = hosts.remove(&evict).unwrap();
                    if let Some(cached) = removed.cached.try_lock().unwrap().as_ref() {
                        cached.retire();
                    };
                }
                let slot = Arc::new(Host::default());
                hosts.insert(host.clone(), slot.clone());
                slot
            }
        };
        let deadline =
            tokio::time::Instant::now() + Duration::from_millis(self.settings.timeout_ms);
        let mut cached = tokio::time::timeout_at(deadline, slot.cached.lock())
            .await
            .map_err(|_| anyhow!("dns_timeout"))?;
        if let Some(previous) = cached
            .as_ref()
            .filter(|entry| entry.valid_until > Instant::now())
        {
            return previous.result();
        }
        let permit = tokio::time::timeout_at(deadline, self.queries.acquire())
            .await
            .map_err(|_| anyhow!("dns_capacity_timeout"))??;
        let resolved = tokio::time::timeout_at(deadline, self.client.lookup_ip(host)).await;
        drop(permit);
        let (mut addresses, valid_until, failed) = match resolved {
            Ok(Ok(answer)) => (
                answer
                    .iter()
                    .filter(|ip| !ip.is_unspecified() && !ip.is_multicast())
                    .take(64)
                    .collect::<Vec<_>>(),
                answer.valid_until(),
                false,
            ),
            _ => (
                Vec::new(),
                Instant::now() + Duration::from_millis(self.settings.failure_cooldown_ms),
                true,
            ),
        };
        addresses.sort_unstable();
        addresses.dedup();
        let mut previous: BTreeMap<_, _> = cached
            .as_ref()
            .into_iter()
            .flat_map(|entry| entry.addresses.iter().cloned())
            .collect();
        let next = addresses
            .into_iter()
            .map(|ip| {
                let address = previous
                    .remove(&ip)
                    .unwrap_or_else(|| Address(Arc::new(AtomicBool::new(true))));
                (ip, address)
            })
            .collect::<Vec<_>>();
        for (_, address) in previous {
            address.0.store(false, Ordering::Release);
        }
        let state = cached.insert(Cached {
            failed: failed || next.is_empty(),
            addresses: next.into(),
            valid_until,
        });
        state.result()
    }
}

pub(crate) fn host_port(address: &str) -> Option<(&str, u16)> {
    let (host, port) = address.rsplit_once(':')?;
    if host.is_empty() || host.contains(':') {
        return None;
    }
    Some((host, port.parse().ok().filter(|port| *port != 0)?))
}

pub(crate) struct HttpResolver(pub Arc<Resolver>);

impl reqwest::dns::Resolve for HttpResolver {
    fn resolve(&self, name: reqwest::dns::Name) -> reqwest::dns::Resolving {
        let resolver = self.0.clone();
        Box::pin(async move {
            let addresses = resolver.resolve(name.as_str()).await?;
            let ips: Vec<_> = addresses
                .iter()
                .map(|(ip, _)| SocketAddr::new(*ip, 0))
                .collect();
            Ok(Box::new(ips.into_iter()) as reqwest::dns::Addrs)
        })
    }
}

pub(crate) fn valid_destination(destination: &str) -> bool {
    host_port(destination).is_some_and(|(host, _)| {
        !host.contains('*')
            && !host.contains('@')
            && hickory_resolver::proto::rr::Name::from_ascii(host).is_ok_and(|name| !name.is_root())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use hickory_resolver::proto::{
        op::{Message, ResponseCode},
        rr::{
            rdata::{A, AAAA, CNAME, SOA},
            Name, RData, Record, RecordType,
        },
    };
    use std::sync::atomic::AtomicUsize;
    use tokio::{net::UdpSocket, task::JoinSet};

    #[tokio::test]
    async fn resolves_dual_stack_aliases_rotates_and_recovers_from_negative_answers() {
        let udp = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let settings = Settings {
            servers: vec![udp.local_addr().unwrap()],
            max_concurrent: 1,
            timeout_ms: 500,
            failure_cooldown_ms: 30,
            ..Settings::default()
        };
        let resolver = Resolver::new(&settings).unwrap();
        let mode = Arc::new(AtomicUsize::new(0));
        let packets = Arc::new(AtomicUsize::new(0));
        let fixture = tokio::spawn({
            let mode = mode.clone();
            let packets = packets.clone();
            async move {
                let mut buf = [0; 4096];
                loop {
                    let (len, peer) = udp.recv_from(&mut buf).await.unwrap();
                    let request = Message::from_vec(&buf[..len]).unwrap();
                    let query = &request.queries[0];
                    packets.fetch_add(1, Ordering::Relaxed);
                    let mut reply =
                        Message::response(request.metadata.id, request.metadata.op_code);
                    reply.add_query(query.clone());
                    reply.metadata.recursion_available = true;
                    match mode.load(Ordering::Acquire) {
                        2 => {
                            reply.metadata.response_code = ResponseCode::NXDomain;
                            let zone = Name::from_ascii("test.").unwrap();
                            reply.add_authority(Record::from_rdata(
                                zone.clone(),
                                1,
                                RData::SOA(SOA::new(zone.clone(), zone, 1, 60, 60, 60, 1)),
                            ));
                        }
                        3 => {
                            reply.metadata.response_code = ResponseCode::ServFail;
                        }
                        4 => continue,
                        current => {
                            let canonical = Name::from_ascii("origin.test.").unwrap();
                            if query.name != canonical {
                                reply.add_answer(Record::from_rdata(
                                    query.name.clone(),
                                    1,
                                    RData::CNAME(CNAME(canonical.clone())),
                                ));
                            }
                            let data = match query.query_type {
                                RecordType::A => {
                                    RData::A(A::new(127, 0, 0, if current == 0 { 1 } else { 2 }))
                                }
                                RecordType::AAAA => RData::AAAA(AAAA("::1".parse().unwrap())),
                                other => panic!("unexpected query {other}"),
                            };
                            reply.add_answer(Record::from_rdata(canonical, 1, data));
                        }
                    }
                    udp.send_to(&reply.to_vec().unwrap(), peer).await.unwrap();
                }
            }
        });
        let mut tasks = JoinSet::new();
        for _ in 0..24 {
            let resolver = resolver.clone();
            tasks.spawn(async move { resolver.resolve("alias.test").await.unwrap() });
        }
        let first = tasks.join_next().await.unwrap().unwrap();
        assert_eq!(
            first
                .iter()
                .map(|(ip, _)| ip.to_string())
                .collect::<Vec<_>>(),
            ["127.0.0.1", "::1"]
        );
        while let Some(result) = tasks.join_next().await {
            assert!(Arc::ptr_eq(&first, &result.unwrap()));
        }
        assert_eq!(packets.load(Ordering::Relaxed), 2);
        mode.store(1, Ordering::Release);
        tokio::time::sleep(Duration::from_millis(1050)).await;
        let next = resolver.resolve("alias.test.").await.unwrap();
        assert_eq!(next[0].0.to_string(), "127.0.0.2");
        assert!(!Address::current(&Some(first[0].1.clone())));
        assert!(Address::current(&Some(first[1].1.clone())));
        mode.store(2, Ordering::Release);
        tokio::time::sleep(Duration::from_millis(1050)).await;
        assert!(resolver.resolve("alias.test").await.is_err());
        assert!(!Address::current(&Some(next[0].1.clone())));
        let after_nxdomain = packets.load(Ordering::Relaxed);
        tokio::time::sleep(Duration::from_millis(45)).await;
        assert!(resolver.resolve("alias.test").await.is_err());
        assert_eq!(packets.load(Ordering::Relaxed), after_nxdomain);
        mode.store(3, Ordering::Release);
        assert!(resolver.resolve("bad.test").await.is_err());
        let after_servfail = packets.load(Ordering::Relaxed);
        assert!(resolver.resolve("bad.test").await.is_err());
        assert_eq!(packets.load(Ordering::Relaxed), after_servfail);
        mode.store(4, Ordering::Release);
        let before = packets.load(Ordering::Relaxed);
        let cancelled = tokio::spawn({
            let resolver = resolver.clone();
            async move { resolver.resolve("cancel.test").await }
        });
        tokio::time::timeout(Duration::from_millis(200), async {
            while packets.load(Ordering::Relaxed) == before {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        cancelled.abort();
        assert!(matches!(cancelled.await, Err(error) if error.is_cancelled()));
        mode.store(1, Ordering::Release);
        assert!(resolver.resolve("cancel.test").await.is_ok());
        tokio::time::sleep(Duration::from_millis(45)).await;
        assert!(resolver.resolve("bad.test").await.is_ok());
        fixture.abort();
    }
}
