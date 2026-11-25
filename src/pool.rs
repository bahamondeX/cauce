//! HTTP/1 leases stay exclusive; HTTP/2 sockets are shared and stream slots live
//! until both the upload and downstream response have finished.
use crate::{
    body::ProxyBody,
    config::{Balance, Config, Http2Config, Limits, Route, Upstream, UpstreamProtocol},
    telemetry::{Attempt, Metrics},
    tls,
};
use anyhow::{anyhow, Result};
use hyper::{
    body::Incoming,
    client::conn::{http1::SendRequest, http2::SendRequest as H2SendRequest},
    rt::Executor,
    Request, Response,
};
use hyper_util::rt::{TokioIo, TokioTimer};
use rustls::ClientConfig;
use std::{
    collections::HashMap,
    future::Future,
    net::SocketAddr,
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
        Arc, Mutex, Weak,
    },
    time::{Duration, Instant},
};
use tokio::{
    net::{TcpStream, UnixStream},
    sync::{Notify, OwnedSemaphorePermit, Semaphore},
    task::{AbortHandle, JoinSet},
    time::timeout,
};
trait PoolIo: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send {}
impl<T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send> PoolIo for T {}

pub fn ms(t: Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1000.0
}
fn empty_attempt() -> Attempt {
    Attempt {
        number: 1,
        outcome: "acquired".into(),
        connection_id: None,
        reused: None,
        pool_wait_ms: None,
        connect_gate_ms: None,
        dns_ms: None,
        tcp_ms: None,
        tls_ms: None,
        headers_ms: None,
    }
}
pub struct Endpoint {
    pub config: Upstream,
    pub slots: Arc<Semaphore>,
    pub inflight: AtomicUsize,
    pub active_healthy: AtomicBool,
    pub failed_until: Mutex<Option<Instant>>,
    dns: Mutex<Option<(Instant, Vec<SocketAddr>)>>,
    // A semaphore limits DNS refresh concurrency; no mutex is held over network IO.
    resolving: Arc<Semaphore>,
    // Serialize cold HTTP/2 connection creation for this endpoint, while allowing
    // subsequent connections once the first is saturated at its stream limit.
    h2_opening: Arc<Semaphore>,
    dns_cursor: AtomicUsize,
    pub tls_client: Option<Arc<ClientConfig>>,
}
pub struct Generation {
    pub version: u64,
    pub config: Config,
    pub endpoints: HashMap<String, Arc<Endpoint>>,
    pub draining: AtomicBool,
    cursor: AtomicUsize,
}
impl Generation {
    pub fn new(
        config: Config,
        version: u64,
        tls_clients: &HashMap<String, Arc<ClientConfig>>,
    ) -> Arc<Self> {
        let endpoints = config
            .upstreams
            .iter()
            .map(|u| {
                (
                    u.id.clone(),
                    Arc::new(Endpoint {
                        config: u.clone(),
                        slots: Arc::new(Semaphore::new(u.connections)),
                        inflight: AtomicUsize::new(0),
                        active_healthy: AtomicBool::new(u.health_check.is_none()),
                        failed_until: Mutex::new(None),
                        dns: Mutex::new(None),
                        resolving: Arc::new(Semaphore::new(1)),
                        h2_opening: Arc::new(Semaphore::new(1)),
                        dns_cursor: AtomicUsize::new(0),
                        tls_client: tls_clients.get(&u.id).cloned(),
                    }),
                )
            })
            .collect();
        Arc::new(Self {
            version,
            config,
            endpoints,
            draining: AtomicBool::new(false),
            cursor: AtomicUsize::new(0),
        })
    }
    pub fn select(&self, route: &Route) -> Option<Arc<Endpoint>> {
        let start = self.cursor.fetch_add(1, Ordering::Relaxed);
        let mut candidates = (0..route.upstreams.len()).filter_map(|i| {
            let e = self.endpoints[&route.upstreams[(start + i) % route.upstreams.len()]].clone();
            let healthy = e
                .failed_until
                .lock()
                .unwrap()
                .is_none_or(|until| until <= Instant::now());
            (healthy && e.active_healthy.load(Ordering::Acquire)).then_some(e)
        });
        match route.balance {
            Balance::RoundRobin => candidates.next(),
            Balance::LeastInflight => candidates.min_by_key(|e| e.inflight.load(Ordering::Relaxed)),
        }
    }
}
pub struct Connection {
    pub sender: SendRequest<ProxyBody>,
    pub id: String,
    born: Instant,
    idle_since: Instant,
    pub driver: AbortHandle,
    _capacity: Arc<DriverCapacity>,
}
struct H2Connection {
    sender: H2SendRequest<ProxyBody>,
    id: String,
    born: Instant,
    idle_since: Mutex<Instant>,
    streams: Arc<Semaphore>,
    capacity: usize,
    idle_limit: usize,
    key: Key,
    live: AtomicUsize,
    draining: AtomicBool,
    driver: AbortHandle,
    executor: H2TaskExecutor,
}
impl Drop for H2Connection {
    fn drop(&mut self) {
        self.executor.abort_all();
        self.driver.abort();
    }
}
impl Drop for Connection {
    fn drop(&mut self) {
        self.driver.abort();
    }
}
#[derive(Hash, PartialEq, Eq, Clone)]
struct Key {
    generation: u64,
    upstream: String,
}
struct Idle {
    connections: HashMap<Key, Vec<Connection>>,
    h2: HashMap<Key, Vec<Arc<H2Connection>>>,
}
pub struct Pool {
    idle: Mutex<Idle>,
    global: Arc<Semaphore>,
    connecting: Arc<Semaphore>,
    notify: Arc<Notify>,
    limits: Limits,
    http2: Http2Config,
    pub metrics: Arc<Metrics>,
    next_id: AtomicU64,
}
struct DriverCapacity {
    global: Option<OwnedSemaphorePermit>,
    endpoint: Option<OwnedSemaphorePermit>,
    notify: Arc<Notify>,
}
/// Tracks every task Hyper schedules for this HTTP/2 connection. The handshake
/// schedules the actual IO ConnTask separately from its dispatcher, so aborting
/// only the dispatcher can leave the socket and uploads alive.
#[derive(Clone)]
struct H2TaskExecutor {
    tasks: Arc<Mutex<JoinSet<()>>>,
    capacity: Arc<DriverCapacity>,
}
impl H2TaskExecutor {
    fn new(capacity: Arc<DriverCapacity>) -> Self {
        Self {
            tasks: Arc::new(Mutex::new(JoinSet::new())),
            capacity,
        }
    }

    fn abort_all(&self) {
        self.tasks.lock().unwrap().abort_all();
    }
}
impl<F> Executor<F> for H2TaskExecutor
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    fn execute(&self, future: F) {
        let mut tasks = self.tasks.lock().unwrap();
        while tasks.try_join_next().is_some() {}
        let capacity = self.capacity.clone();
        tasks.spawn(async move {
            let _capacity = capacity;
            let _ = future.await;
        });
    }
}
impl Drop for DriverCapacity {
    fn drop(&mut self) {
        self.global.take();
        self.endpoint.take();
        self.notify.notify_waiters();
    }
}
pub struct Lease {
    pub connection: Option<Connection>,
    pool: Arc<Pool>,
    generation: Arc<Generation>,
    endpoint: Arc<Endpoint>,
    h2: Option<Arc<StreamHold>>,
}
struct StreamHold {
    metrics: Arc<Metrics>,
    permit: Option<OwnedSemaphorePermit>,
    connection: Arc<H2Connection>,
    endpoint: Arc<Endpoint>,
    notify: Arc<Notify>,
    pool: Weak<Pool>,
    counted_live: AtomicBool,
    watchdog: Mutex<Option<AbortHandle>>,
}
impl StreamHold {
    fn release_live(&self) {
        if self.counted_live.swap(false, Ordering::AcqRel)
            && self.connection.live.fetch_sub(1, Ordering::AcqRel) == 1
            && self.connection.draining.load(Ordering::Acquire)
        {
            // Hyper does not expose the per-request SendStream reset handle.
            // Once all live streams are gone, aborting this retired connection
            // wakes any stuck PipeToSendStream so its request body can be dropped.
            self.connection.executor.abort_all();
            self.connection.driver.abort();
        }
    }
    fn expire(&self) {
        self.connection.draining.store(true, Ordering::Release);
        if let Some(pool) = self.pool.upgrade() {
            let mut idle = pool.idle.lock().unwrap();
            if let Some(list) = idle.h2.get_mut(&self.connection.key) {
                list.retain(|candidate| !Arc::ptr_eq(candidate, &self.connection));
                if list.is_empty() {
                    idle.h2.remove(&self.connection.key);
                }
            }
        }
        self.release_live();
        if self.connection.live.load(Ordering::Acquire) == 0 {
            self.connection.executor.abort_all();
            self.connection.driver.abort();
        }
        self.notify.notify_waiters();
    }
}
impl Drop for StreamHold {
    fn drop(&mut self) {
        self.metrics
            .upstream_streams_active
            .fetch_sub(1, Ordering::Relaxed);
        self.release_live();
        if let Some(handle) = self.watchdog.get_mut().unwrap().take() {
            handle.abort();
        }
        self.permit.take();
        self.endpoint.inflight.fetch_sub(1, Ordering::Relaxed);
        if !self.connection.draining.load(Ordering::Acquire) {
            *self.connection.idle_since.lock().unwrap() = Instant::now();
        }
        let global_idle_disabled = self
            .pool
            .upgrade()
            .is_some_and(|pool| pool.limits.idle_connections == 0);
        if self.connection.streams.available_permits() == self.connection.capacity
            && (self.connection.idle_limit == 0 || global_idle_disabled)
        {
            if let Some(pool) = self.pool.upgrade() {
                let mut idle = pool.idle.lock().unwrap();
                if let Some(list) = idle.h2.get_mut(&self.connection.key).filter(|_| {
                    self.connection.streams.available_permits() == self.connection.capacity
                }) {
                    list.retain(|candidate| !Arc::ptr_eq(candidate, &self.connection));
                    if list.is_empty() {
                        idle.h2.remove(&self.connection.key);
                    }
                }
            }
        }
        self.notify.notify_waiters();
    }
}
#[derive(Clone)]
pub struct UploadGuard {
    _hold: Arc<StreamHold>,
}
impl Drop for Lease {
    fn drop(&mut self) {
        // Dropping an incomplete response aborts the driver before releasing capacity.
        self.connection.take();
        if self.h2.is_none() {
            self.endpoint.inflight.fetch_sub(1, Ordering::Relaxed);
        }
        self.pool.notify.notify_waiters();
    }
}
impl Lease {
    pub fn arm_deadline(&self, deadline: tokio::time::Instant) {
        let Some(hold) = &self.h2 else {
            return;
        };
        let weak = Arc::downgrade(hold);
        let task = tokio::spawn(async move {
            tokio::time::sleep_until(deadline).await;
            if let Some(hold) = weak.upgrade() {
                hold.expire();
            }
        });
        let handle = task.abort_handle();
        let mut watchdog = hold.watchdog.lock().unwrap();
        if hold.counted_live.load(Ordering::Acquire) {
            if let Some(old) = watchdog.replace(handle) {
                old.abort();
            }
        } else {
            handle.abort();
        }
    }
    pub fn upload_guard(&self) -> Option<UploadGuard> {
        self.h2.as_ref().map(|h| UploadGuard { _hold: h.clone() })
    }
    pub async fn send_request(
        &mut self,
        req: Request<ProxyBody>,
    ) -> hyper::Result<Response<Incoming>> {
        if let Some(h2) = &self.h2 {
            let mut sender = h2.connection.sender.clone();
            sender.ready().await?;
            sender.send_request(req).await
        } else {
            self.connection
                .as_mut()
                .expect("HTTP/1 lease")
                .sender
                .send_request(req)
                .await
        }
    }
    pub fn complete(&mut self, upload_complete: bool) {
        if let Some(mut c) = self.connection.take() {
            if !upload_complete
                || self.generation.draining.load(Ordering::Acquire)
                || c.sender.is_closed()
                || c.born.elapsed().as_millis() >= self.pool.limits.max_connection_age_ms as u128
            {
                return;
            }
            c.idle_since = Instant::now();
            let mut idle = self.pool.idle.lock().unwrap();
            let count: usize = idle.connections.values().map(Vec::len).sum();
            let key = Key {
                generation: self.generation.version,
                upstream: self.endpoint.config.id.clone(),
            };
            let list = idle.connections.entry(key).or_default();
            if count < self.pool.limits.idle_connections
                && list.len() < self.endpoint.config.idle_connections
            {
                list.push(c);
            }
        }
        self.pool.notify.notify_waiters();
    }
}
impl Pool {
    pub fn new(limits: Limits, http2: Http2Config, metrics: Arc<Metrics>) -> Arc<Self> {
        Arc::new(Self {
            idle: Mutex::new(Idle {
                connections: HashMap::new(),
                h2: HashMap::new(),
            }),
            global: Arc::new(Semaphore::new(limits.connections)),
            connecting: Arc::new(Semaphore::new(limits.connecting)),
            notify: Arc::new(Notify::new()),
            limits,
            http2,
            metrics,
            next_id: AtomicU64::new(1),
        })
    }
    pub fn sweep(&self, keep_generation: u64) {
        let mut idle = self.idle.lock().unwrap();
        idle.connections.retain(|k, list| {
            if k.generation != keep_generation {
                return false;
            }
            list.retain(|c| {
                !c.sender.is_closed()
                    && c.idle_since.elapsed().as_millis() < self.limits.idle_connection_ms as u128
                    && c.born.elapsed().as_millis() < self.limits.max_connection_age_ms as u128
            });
            !list.is_empty()
        });
        idle.h2.retain(|k, list| {
            if k.generation != keep_generation {
                return false;
            }
            list.retain(|c| {
                !c.sender.is_closed()
                    && c.born.elapsed().as_millis() < self.limits.max_connection_age_ms as u128
                    && (c.streams.available_permits() < c.capacity
                        || (c.idle_since.lock().unwrap().elapsed().as_millis()
                            < self.limits.idle_connection_ms as u128))
            });
            let endpoint_idle_limit = list.first().map_or(0, |c| c.idle_limit);
            list.sort_by_key(|c| c.idle_since.lock().unwrap().elapsed());
            let mut idle_kept = 0usize;
            list.retain(|c| {
                if c.streams.available_permits() < c.capacity {
                    true
                } else if idle_kept < endpoint_idle_limit {
                    idle_kept += 1;
                    true
                } else {
                    false
                }
            });
            !list.is_empty()
        });
        let mut idle_total: usize = idle.connections.values().map(Vec::len).sum();
        idle_total += idle
            .h2
            .values()
            .map(|xs| {
                xs.iter()
                    .filter(|c| c.streams.available_permits() == c.capacity)
                    .count()
            })
            .sum::<usize>();
        while idle_total > self.limits.idle_connections {
            if let Some((key, idx)) = idle.h2.iter().find_map(|(k, xs)| {
                xs.iter()
                    .position(|c| c.streams.available_permits() == c.capacity)
                    .map(|i| (k.clone(), i))
            }) {
                let xs = idle.h2.get_mut(&key).unwrap();
                xs.remove(idx);
                if xs.is_empty() {
                    idle.h2.remove(&key);
                }
                idle_total -= 1;
            } else if let Some(xs) = idle.connections.values_mut().find(|xs| !xs.is_empty()) {
                xs.pop();
                idle_total -= 1;
            } else {
                break;
            }
        }
        self.notify.notify_waiters();
    }
    pub fn available(&self) -> usize {
        self.global.available_permits()
    }
    pub fn idle_count(&self) -> usize {
        let idle = self.idle.lock().unwrap();
        idle.connections.values().map(Vec::len).sum::<usize>()
            + idle
                .h2
                .values()
                .map(|xs| {
                    xs.iter()
                        .filter(|c| c.streams.available_permits() == c.capacity)
                        .count()
                })
                .sum::<usize>()
    }
    pub async fn acquire(
        self: &Arc<Self>,
        generation: Arc<Generation>,
        endpoint: Arc<Endpoint>,
    ) -> Result<(Lease, Attempt)> {
        let begin = Instant::now();
        let key = Key {
            generation: generation.version,
            upstream: endpoint.config.id.clone(),
        };
        if endpoint.config.protocol == UpstreamProtocol::Http2 {
            return self.acquire_h2(generation, endpoint, key, begin).await;
        }
        let acquired = timeout(Duration::from_millis(self.limits.acquire_ms), async {
            loop {
                let notified = self.notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                {
                    let mut idle = self.idle.lock().unwrap();
                    if let Some(list) = idle.connections.get_mut(&key) {
                        while let Some(c) = list.pop() {
                            if !c.sender.is_closed()
                                && c.idle_since.elapsed().as_millis()
                                    < self.limits.idle_connection_ms as u128
                                && c.born.elapsed().as_millis()
                                    < self.limits.max_connection_age_ms as u128
                            {
                                return (Some(c), None);
                            }
                        }
                    }
                    if let Ok(ep) = endpoint.slots.clone().try_acquire_owned() {
                        // Reclaim idle capacity from other keys rather than waiting forever behind it.
                        if self.global.available_permits() == 0 {
                            if let Some(list) =
                                idle.connections.values_mut().find(|v| !v.is_empty())
                            {
                                list.pop();
                            } else if let Some((key, idx)) = idle.h2.iter().find_map(|(k, xs)| {
                                xs.iter()
                                    .position(|c| c.streams.available_permits() == c.capacity)
                                    .map(|i| (k.clone(), i))
                            }) {
                                let xs = idle.h2.get_mut(&key).unwrap();
                                xs.remove(idx);
                                if xs.is_empty() {
                                    idle.h2.remove(&key);
                                }
                            }
                        }
                        if let Ok(global) = self.global.clone().try_acquire_owned() {
                            return (None, Some((global, ep)));
                        }
                    }
                }
                notified.await;
            }
        })
        .await
        .map_err(|_| anyhow!("pool_capacity_timeout"))?;
        let wait = ms(begin);
        self.metrics
            .pool_wait_us
            .fetch_add((wait * 1000.0) as u64, Ordering::Relaxed);
        let mut attempt = Attempt {
            number: 1,
            outcome: "acquired".into(),
            connection_id: None,
            reused: Some(acquired.0.is_some()),
            pool_wait_ms: Some(wait),
            connect_gate_ms: None,
            dns_ms: None,
            tcp_ms: None,
            tls_ms: None,
            headers_ms: None,
        };
        let connection = if let Some(c) = acquired.0 {
            self.metrics.reused.fetch_add(1, Ordering::Relaxed);
            c
        } else {
            let (global, ep) = acquired.1.unwrap();
            let gate_started = Instant::now();
            let _gate = timeout(
                Duration::from_millis(self.limits.acquire_ms),
                self.connecting.clone().acquire_owned(),
            )
            .await
            .map_err(|_| anyhow!("connect_capacity_timeout"))??;
            attempt.connect_gate_ms = Some(ms(gate_started));
            let connected = self.connect(&endpoint, global, ep, &mut attempt).await;
            if connected.is_err() {
                *endpoint.failed_until.lock().unwrap() = Some(
                    Instant::now() + Duration::from_millis(endpoint.config.failure_backoff_ms),
                );
                self.metrics.failures.fetch_add(1, Ordering::Relaxed);
                self.notify.notify_waiters();
            }
            connected?
        };
        attempt.connection_id = Some(connection.id.clone());
        endpoint.inflight.fetch_add(1, Ordering::Relaxed);
        Ok((
            Lease {
                connection: Some(connection),
                pool: self.clone(),
                generation,
                endpoint,
                h2: None,
            },
            attempt,
        ))
    }
    async fn acquire_h2(
        self: &Arc<Self>,
        generation: Arc<Generation>,
        endpoint: Arc<Endpoint>,
        key: Key,
        begin: Instant,
    ) -> Result<(Lease, Attempt)> {
        let deadline = tokio::time::Instant::now() + Duration::from_millis(self.limits.acquire_ms);
        let mut reserved: Option<(OwnedSemaphorePermit, OwnedSemaphorePermit)> = None;
        let (connection, stream_permit, newly_connected, gate_ms, mut attempt) = loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            let mut selected = None;
            {
                let mut idle = self.idle.lock().unwrap();
                if let Some(list) = idle.h2.get_mut(&key) {
                    list.retain(|c| {
                        !c.draining.load(Ordering::Acquire)
                            && !c.sender.is_closed()
                            && c.born.elapsed().as_millis()
                                < self.limits.max_connection_age_ms as u128
                            && (c.streams.available_permits() < c.capacity
                                || c.idle_since.lock().unwrap().elapsed().as_millis()
                                    < self.limits.idle_connection_ms as u128)
                    });
                    for c in list.iter() {
                        if c.born.elapsed().as_millis() >= self.limits.max_connection_age_ms as u128
                        {
                            continue;
                        }
                        if let Ok(p) = c.streams.clone().try_acquire_owned() {
                            c.live.fetch_add(1, Ordering::AcqRel);
                            selected = Some((c.clone(), p));
                            break;
                        }
                    }
                }
            }
            if let Some((c, p)) = selected {
                break (c, p, false, None, empty_attempt());
            }
            if reserved.is_none() {
                let ep = endpoint.slots.clone().try_acquire_owned();
                let global = self.global.clone().try_acquire_owned();
                match (ep, global) {
                    (Ok(ep), Ok(global)) => reserved = Some((global, ep)),
                    (ep, global) => {
                        drop(ep.ok());
                        drop(global.ok());
                        if self.global.available_permits() == 0 {
                            self.reclaim_idle_connection();
                        }
                    }
                }
            }
            if let Some((global, ep)) = reserved.take() {
                let open_gate = match tokio::time::timeout_at(
                    deadline,
                    endpoint.h2_opening.clone().acquire_owned(),
                )
                .await
                {
                    Ok(Ok(g)) => g,
                    Ok(Err(e)) => return Err(e.into()),
                    Err(_) => return Err(anyhow!("connect_capacity_timeout")),
                };
                let started = Instant::now();
                let gate = match tokio::time::timeout_at(
                    deadline,
                    self.connecting.clone().acquire_owned(),
                )
                .await
                {
                    Ok(Ok(g)) => g,
                    Ok(Err(e)) => return Err(e.into()),
                    Err(_) => return Err(anyhow!("connect_capacity_timeout")),
                };
                let gate_ms = ms(started);
                // A concurrent opener may have created a usable socket while we waited.
                let existing = {
                    let idle = self.idle.lock().unwrap();
                    idle.h2.get(&key).and_then(|xs| {
                        xs.iter()
                            .find(|c| {
                                !c.draining.load(Ordering::Acquire)
                                    && !c.sender.is_closed()
                                    && c.born.elapsed().as_millis()
                                        < self.limits.max_connection_age_ms as u128
                            })
                            .and_then(|c| {
                                c.streams.clone().try_acquire_owned().ok().map(|p| {
                                    c.live.fetch_add(1, Ordering::AcqRel);
                                    (c.clone(), p)
                                })
                            })
                    })
                };
                if let Some((c, p)) = existing {
                    drop(gate);
                    drop(open_gate);
                    drop(global);
                    drop(ep);
                    break (c, p, false, Some(gate_ms), empty_attempt());
                }
                let cap = Arc::new(DriverCapacity {
                    global: Some(global),
                    endpoint: Some(ep),
                    notify: self.notify.clone(),
                });
                let mut a = empty_attempt();
                a.connect_gate_ms = Some(gate_ms);
                match self.connect_h2(&endpoint, key.clone(), cap, &mut a).await {
                    Ok(c) => {
                        let p = c
                            .streams
                            .clone()
                            .try_acquire_owned()
                            .map_err(|_| anyhow!("h2_stream_capacity_race"))?;
                        c.live.fetch_add(1, Ordering::AcqRel);
                        self.idle
                            .lock()
                            .unwrap()
                            .h2
                            .entry(key.clone())
                            .or_default()
                            .push(c.clone());
                        drop(gate);
                        drop(open_gate);
                        break (c, p, true, Some(gate_ms), a);
                    }
                    Err(e) => {
                        drop(gate);
                        drop(open_gate);
                        *endpoint.failed_until.lock().unwrap() = Some(
                            Instant::now()
                                + Duration::from_millis(endpoint.config.failure_backoff_ms),
                        );
                        self.metrics.failures.fetch_add(1, Ordering::Relaxed);
                        return Err(e);
                    }
                }
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(anyhow!("pool_capacity_timeout"));
            }
            tokio::select! { _ = notified => {}, _ = tokio::time::sleep_until(deadline) => return Err(anyhow!("pool_capacity_timeout")) }
        };
        attempt.number = 1;
        attempt.outcome = "acquired".into();
        attempt.connection_id = Some(connection.id.clone());
        attempt.reused = Some(!newly_connected);
        attempt.pool_wait_ms = Some(ms(begin));
        self.metrics.pool_wait_us.fetch_add(
            (attempt.pool_wait_ms.unwrap_or_default() * 1000.0) as u64,
            Ordering::Relaxed,
        );
        if let Some(v) = gate_ms {
            attempt.connect_gate_ms = Some(v);
        }
        if !newly_connected {
            self.metrics.reused.fetch_add(1, Ordering::Relaxed);
        }
        endpoint.inflight.fetch_add(1, Ordering::Relaxed);
        self.metrics
            .upstream_streams_active
            .fetch_add(1, Ordering::Relaxed);
        let hold = Arc::new(StreamHold {
            metrics: self.metrics.clone(),
            permit: Some(stream_permit),
            connection,
            endpoint: endpoint.clone(),
            notify: self.notify.clone(),
            pool: Arc::downgrade(self),
            counted_live: AtomicBool::new(true),
            watchdog: Mutex::new(None),
        });
        Ok((
            Lease {
                connection: None,
                pool: self.clone(),
                generation,
                endpoint,
                h2: Some(hold),
            },
            attempt,
        ))
    }
    async fn connect_h2(
        &self,
        e: &Endpoint,
        key: Key,
        capacity: Arc<DriverCapacity>,
        a: &mut Attempt,
    ) -> Result<Arc<H2Connection>> {
        let duration = Duration::from_millis(self.limits.connect_ms);
        let tcp_start = Instant::now();
        let io: Box<dyn PoolIo> = if let Some(path) = e.config.address.strip_prefix("unix:") {
            Box::new(
                timeout(duration, UnixStream::connect(path))
                    .await
                    .map_err(|_| anyhow!("unix_connect_timeout"))??,
            )
        } else {
            let dns_start = Instant::now();
            let _refresh = timeout(duration, e.resolving.acquire())
                .await
                .map_err(|_| anyhow!("dns_timeout"))??;
            let cached = e
                .dns
                .lock()
                .unwrap()
                .as_ref()
                .filter(|(t, _)| t.elapsed().as_millis() < self.limits.dns_cache_ms as u128)
                .map(|(_, ips)| ips.clone());
            let addresses = match cached {
                Some(ips) => ips,
                None => {
                    let ips: Vec<_> = timeout(duration, tokio::net::lookup_host(&e.config.address))
                        .await
                        .map_err(|_| anyhow!("dns_timeout"))??
                        .take(64)
                        .collect();
                    if ips.is_empty() {
                        return Err(anyhow!("dns_no_answers"));
                    }
                    *e.dns.lock().unwrap() = Some((Instant::now(), ips.clone()));
                    ips
                }
            };
            drop(_refresh);
            a.dns_ms = Some(ms(dns_start));
            let addr = addresses[e.dns_cursor.fetch_add(1, Ordering::Relaxed) % addresses.len()];
            let socket = timeout(duration, TcpStream::connect(addr))
                .await
                .map_err(|_| anyhow!("tcp_connect_timeout"))??;
            socket.set_nodelay(true)?;
            socket2::SockRef::from(&socket).set_tcp_keepalive(
                &socket2::TcpKeepalive::new()
                    .with_time(Duration::from_secs(self.limits.tcp_keepalive_secs)),
            )?;
            a.tcp_ms = Some(ms(tcp_start));
            if let Some(tls_config) = &e.config.tls {
                let client = e
                    .tls_client
                    .clone()
                    .ok_or_else(|| anyhow!("upstream_tls_not_prepared"))?;
                let name = tls::server_name(tls_config)?;
                let tls_start = Instant::now();
                let stream = timeout(
                    Duration::from_millis(self.limits.tls_handshake_ms),
                    tokio_rustls::TlsConnector::from(client).connect(name, socket),
                )
                .await
                .map_err(|_| anyhow!("tls_handshake_timeout"))?
                .map_err(|_| anyhow!("tls_handshake_error"))?;
                a.tls_ms = Some(ms(tls_start));
                if stream.get_ref().1.alpn_protocol() != Some(b"h2") {
                    return Err(anyhow!("http2_alpn_required"));
                }
                Box::new(stream)
            } else {
                Box::new(socket)
            }
        };
        let executor = H2TaskExecutor::new(capacity.clone());
        let mut builder = hyper::client::conn::http2::Builder::new(executor.clone());
        builder
            .timer(TokioTimer::new())
            .initial_stream_window_size(self.http2.initial_stream_window_bytes)
            .initial_connection_window_size(self.http2.initial_connection_window_bytes)
            // Do not assume the configured pool limit is also the peer's
            // MAX_CONCURRENT_STREAMS. h2 holds the first local open until it
            // receives initial peer SETTINGS, then replaces this zero with the
            // advertised limit (or unlimited if the setting is absent).
            .initial_max_send_streams(Some(0))
            .max_header_list_size(self.limits.max_header_bytes as u32)
            .max_send_buf_size(self.http2.max_send_buffer_bytes)
            .max_pending_accept_reset_streams(Some(self.http2.max_pending_accept_reset_streams))
            .max_local_error_reset_streams(Some(self.http2.max_local_error_reset_streams));
        let (sender, conn) = timeout(
            duration,
            builder.handshake::<_, ProxyBody>(TokioIo::new(io)),
        )
        .await
        .map_err(|_| anyhow!("http2_handshake_timeout"))??;
        let driver = tokio::spawn(async move {
            let _capacity = capacity;
            let _ = conn.await;
        })
        .abort_handle();
        self.metrics.new_connections.fetch_add(1, Ordering::Relaxed);
        Ok(Arc::new(H2Connection {
            sender,
            id: format!("c{}", self.next_id.fetch_add(1, Ordering::Relaxed)),
            born: Instant::now(),
            idle_since: Mutex::new(Instant::now()),
            streams: Arc::new(Semaphore::new(e.config.streams_per_connection)),
            capacity: e.config.streams_per_connection,
            idle_limit: e.config.idle_connections,
            key,
            live: AtomicUsize::new(0),
            draining: AtomicBool::new(false),
            driver,
            executor,
        }))
    }
    fn reclaim_idle_connection(&self) {
        let mut idle = self.idle.lock().unwrap();
        if let Some((key, idx)) = idle.h2.iter().find_map(|(k, xs)| {
            xs.iter()
                .position(|c| c.streams.available_permits() == c.capacity)
                .map(|i| (k.clone(), i))
        }) {
            let xs = idle.h2.get_mut(&key).unwrap();
            xs.remove(idx);
            if xs.is_empty() {
                idle.h2.remove(&key);
            }
            return;
        }
        if let Some(xs) = idle.connections.values_mut().find(|xs| !xs.is_empty()) {
            xs.pop();
        }
    }
    async fn connect(
        &self,
        e: &Endpoint,
        global: OwnedSemaphorePermit,
        ep: OwnedSemaphorePermit,
        a: &mut Attempt,
    ) -> Result<Connection> {
        // Drivers retain capacity until their sockets actually drop, including abort cleanup.
        let capacity = Arc::new(DriverCapacity {
            global: Some(global),
            endpoint: Some(ep),
            notify: self.notify.clone(),
        });
        let duration = Duration::from_millis(self.limits.connect_ms);
        let mut http = hyper::client::conn::http1::Builder::new();
        http.max_headers(self.limits.max_headers)
            .max_header_size(self.limits.max_header_bytes)
            .max_buf_size(self.limits.max_header_bytes);
        let (sender, driver) = if let Some(path) = e.config.address.strip_prefix("unix:") {
            let socket = timeout(duration, UnixStream::connect(path))
                .await
                .map_err(|_| anyhow!("unix_connect_timeout"))??;
            let io = TokioIo::new(socket);
            let (sender, conn) = timeout(duration, http.handshake(io)).await??;
            let driver_capacity = capacity.clone();
            (
                sender,
                tokio::spawn(async move {
                    let _capacity = driver_capacity;
                    let _ = conn.with_upgrades().await;
                })
                .abort_handle(),
            )
        } else {
            let dns_start = Instant::now();
            let _refresh = timeout(duration, e.resolving.acquire())
                .await
                .map_err(|_| anyhow!("dns_timeout"))??;
            let cached = e
                .dns
                .lock()
                .unwrap()
                .as_ref()
                .filter(|(t, _)| t.elapsed().as_millis() < self.limits.dns_cache_ms as u128)
                .map(|(_, ips)| ips.clone());
            let addresses = match cached {
                Some(ips) => ips,
                None => {
                    let ips: Vec<_> = timeout(duration, tokio::net::lookup_host(&e.config.address))
                        .await
                        .map_err(|_| anyhow!("dns_timeout"))??
                        .take(64)
                        .collect();
                    if ips.is_empty() {
                        return Err(anyhow!("dns_no_answers"));
                    }
                    *e.dns.lock().unwrap() = Some((Instant::now(), ips.clone()));
                    ips
                }
            };
            drop(_refresh);
            a.dns_ms = Some(ms(dns_start));
            let chosen = addresses[e.dns_cursor.fetch_add(1, Ordering::Relaxed) % addresses.len()];
            let tcp_start = Instant::now();
            let socket = timeout(duration, TcpStream::connect(chosen))
                .await
                .map_err(|_| anyhow!("tcp_connect_timeout"))??;
            socket.set_nodelay(true)?;
            socket2::SockRef::from(&socket).set_tcp_keepalive(
                &socket2::TcpKeepalive::new()
                    .with_time(Duration::from_secs(self.limits.tcp_keepalive_secs)),
            )?;
            a.tcp_ms = Some(ms(tcp_start));
            let io: Box<dyn PoolIo> = if let Some(tls_config) = &e.config.tls {
                let client = e
                    .tls_client
                    .clone()
                    .ok_or_else(|| anyhow!("upstream_tls_not_prepared"))?;
                let name = tls::server_name(tls_config)?;
                let tls_start = Instant::now();
                let stream = timeout(
                    Duration::from_millis(self.limits.tls_handshake_ms),
                    tokio_rustls::TlsConnector::from(client).connect(name, socket),
                )
                .await
                .map_err(|_| anyhow!("tls_handshake_timeout"))?
                .map_err(|_| anyhow!("tls_handshake_error"))?;
                a.tls_ms = Some(ms(tls_start));
                Box::new(stream)
            } else {
                Box::new(socket)
            };
            let (sender, conn) = timeout(duration, http.handshake(TokioIo::new(io))).await??;
            let driver_capacity = capacity.clone();
            (
                sender,
                tokio::spawn(async move {
                    let _capacity = driver_capacity;
                    let _ = conn.with_upgrades().await;
                })
                .abort_handle(),
            )
        };
        self.metrics.new_connections.fetch_add(1, Ordering::Relaxed);
        Ok(Connection {
            sender,
            driver,
            id: format!("c{}", self.next_id.fetch_add(1, Ordering::Relaxed)),
            born: Instant::now(),
            idle_since: Instant::now(),
            _capacity: capacity,
        })
    }
    pub fn mark_failed(&self, e: &Endpoint) {
        *e.failed_until.lock().unwrap() =
            Some(Instant::now() + Duration::from_millis(e.config.failure_backoff_ms));
        self.metrics.failures.fetch_add(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod h2_pool_tests {
    use super::*;
    use crate::config::{Http2Config, Logs};
    use bytes::Bytes;
    use http_body_util::{BodyExt, Full};
    use hyper::{body::Incoming, service::service_fn, Request, Response};
    use hyper_util::rt::TokioExecutor;
    use std::{convert::Infallible, sync::atomic::AtomicUsize};
    use std::{
        pin::Pin,
        task::{Context, Poll},
    };
    use tokio::{net::TcpListener, time::sleep};

    struct StalledUploadBody {
        sent: Arc<AtomicBool>,
        dropped: Arc<AtomicBool>,
        _guard: Option<UploadGuard>,
    }
    impl hyper::body::Body for StalledUploadBody {
        type Data = Bytes;
        type Error = crate::body::BoxError;
        fn poll_frame(
            self: Pin<&mut Self>,
            _: &mut Context<'_>,
        ) -> Poll<Option<Result<hyper::body::Frame<Bytes>, Self::Error>>> {
            let this = self.get_mut();
            if !this.sent.swap(true, Ordering::AcqRel) {
                Poll::Ready(Some(Ok(hyper::body::Frame::data(Bytes::from(
                    vec![0; 1024 * 1024],
                )))))
            } else {
                Poll::Pending
            }
        }
        fn is_end_stream(&self) -> bool {
            false
        }
        fn size_hint(&self) -> hyper::body::SizeHint {
            hyper::body::SizeHint::default()
        }
    }
    impl Drop for StalledUploadBody {
        fn drop(&mut self) {
            self.dropped.store(true, Ordering::Release);
        }
    }

    fn config(address: String) -> Config {
        Config {
            name: "pooltest".into(),
            listen: "127.0.0.1:19080".parse().unwrap(),
            admin: "127.0.0.1:19081".parse().unwrap(),
            limits: Limits {
                clients: 8,
                requests: 8,
                connections: 2,
                idle_connections: 2,
                connecting: 2,
                acquire_ms: 2_000,
                connect_ms: 2_000,
                tls_handshake_ms: 2_000,
                headers_ms: 2_000,
                stream_idle_ms: 2_000,
                request_ms: 5_000,
                client_headers_ms: 2_000,
                idle_connection_ms: 30_000,
                max_connection_age_ms: 60_000,
                dns_cache_ms: 30_000,
                tcp_keepalive_secs: 30,
                max_header_bytes: 16_384,
                max_headers: 64,
                max_body_bytes: 1_048_576,
                shutdown_ms: 2_000,
                websocket_max_ms: 5_000,
            },
            logs: Logs {
                enabled: false,
                path: "/tmp/cauce-test.jsonl".into(),
                queue: 1,
                rotate_bytes: 4096,
                retained_files: 1,
            },
            tls: None,
            http2: Http2Config::default(),
            upstreams: vec![Upstream {
                health_check: None,
                id: "backend".into(),
                address,
                host: "backend.test".into(),
                isolation: "test".into(),
                connections: 2,
                idle_connections: 2,
                failure_backoff_ms: 100,
                tls: None,
                protocol: UpstreamProtocol::Http2,
                streams_per_connection: 8,
            }],
            routes: vec![Route {
                id: "route".into(),
                host: "example.test".into(),
                path_prefix: "/".into(),
                balance: Balance::RoundRobin,
                upstreams: vec!["backend".into()],
            }],
        }
    }

    async fn peer_settings_burst(peer_limit: Option<u32>) -> usize {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let accepted = Arc::new(AtomicUsize::new(0));
        let active = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let accepted_task = accepted.clone();
        let active_task = active.clone();
        let peak_task = peak.clone();
        let server = tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    break;
                };
                accepted_task.fetch_add(1, Ordering::Relaxed);
                let active = active_task.clone();
                let peak = peak_task.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |_request: Request<Incoming>| {
                        let active = active.clone();
                        let peak = peak.clone();
                        async move {
                            let now = active.fetch_add(1, Ordering::SeqCst) + 1;
                            peak.fetch_max(now, Ordering::SeqCst);
                            sleep(Duration::from_millis(100)).await;
                            active.fetch_sub(1, Ordering::SeqCst);
                            Ok::<_, Infallible>(Response::new(Full::new(Bytes::from_static(b"ok"))))
                        }
                    });
                    let mut builder =
                        hyper::server::conn::http2::Builder::new(TokioExecutor::new());
                    builder.max_concurrent_streams(peer_limit);
                    let _ = builder
                        .serve_connection(TokioIo::new(socket), service)
                        .await;
                });
            }
        });

        let mut config = config(address.to_string());
        config.limits.connections = 1;
        config.upstreams[0].connections = 1;
        config.upstreams[0].streams_per_connection = 8;
        let generation = Generation::new(config.clone(), 1, &HashMap::new());
        let endpoint = generation.endpoints["backend"].clone();
        let pool = Pool::new(
            config.limits.clone(),
            config.http2.clone(),
            Arc::new(Metrics::default()),
        );
        let mut leases = Vec::new();
        let mut connection_id = None;
        for _ in 0..8 {
            let (lease, attempt) = pool
                .acquire(generation.clone(), endpoint.clone())
                .await
                .unwrap();
            if let Some(id) = &connection_id {
                assert_eq!(attempt.connection_id.as_ref(), Some(id));
            } else {
                connection_id = attempt.connection_id.clone();
            }
            leases.push(lease);
        }
        let mut requests = Vec::new();
        for mut lease in leases {
            requests.push(tokio::spawn(async move {
                let request = Request::builder()
                    .uri("/")
                    .header("host", "backend.test")
                    .body(crate::body::full(Bytes::new()))
                    .unwrap();
                let response = lease.send_request(request).await.unwrap();
                assert_eq!(response.status(), hyper::StatusCode::OK);
                assert_eq!(
                    response.into_body().collect().await.unwrap().to_bytes(),
                    Bytes::from_static(b"ok")
                );
                lease.complete(true);
            }));
        }
        for request in requests {
            request.await.unwrap();
        }
        assert_eq!(accepted.load(Ordering::Relaxed), 1);
        let observed_peak = peak.load(Ordering::SeqCst);
        server.abort();
        observed_peak
    }

    #[tokio::test]
    async fn initial_peer_settings_bound_cold_start_streams() {
        assert_eq!(peer_settings_burst(Some(4)).await, 4);
    }

    #[tokio::test]
    async fn absent_peer_stream_limit_releases_initial_zero_gate() {
        assert_eq!(peer_settings_burst(None).await, 8);
    }

    #[tokio::test]
    async fn multiplexes_two_leases_over_one_h2_socket() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let accepted = Arc::new(AtomicUsize::new(0));
        let active = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let accepted_task = accepted.clone();
        let active_task = active.clone();
        let peak_task = peak.clone();
        let server = tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    break;
                };
                accepted_task.fetch_add(1, Ordering::Relaxed);
                let active = active_task.clone();
                let peak = peak_task.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |_req: Request<Incoming>| {
                        let active = active.clone();
                        let peak = peak.clone();
                        async move {
                            let now = active.fetch_add(1, Ordering::SeqCst) + 1;
                            peak.fetch_max(now, Ordering::SeqCst);
                            sleep(Duration::from_millis(60)).await;
                            active.fetch_sub(1, Ordering::SeqCst);
                            Ok::<_, Infallible>(Response::new(Full::new(Bytes::from_static(b"ok"))))
                        }
                    });
                    let builder = hyper::server::conn::http2::Builder::new(TokioExecutor::new());
                    let _ = builder
                        .serve_connection(TokioIo::new(socket), service)
                        .await;
                });
            }
        });
        let config = config(address.to_string());
        let generation = Generation::new(config.clone(), 1, &HashMap::new());
        let endpoint = generation.endpoints["backend"].clone();
        let metrics = Arc::new(Metrics::default());
        let pool = Pool::new(config.limits, config.http2, metrics.clone());
        let (first_result, second_result) = tokio::join!(
            pool.acquire(generation.clone(), endpoint.clone()),
            pool.acquire(generation, endpoint),
        );
        let (mut first, first_attempt) = first_result.unwrap();
        let (mut second, second_attempt) = second_result.unwrap();
        assert_eq!(first_attempt.connection_id, second_attempt.connection_id);
        assert_eq!(metrics.upstream_streams_active.load(Ordering::Relaxed), 2);
        let request = || {
            Request::builder()
                .uri("/")
                .header("host", "backend.test")
                .body(crate::body::full(Bytes::new()))
                .unwrap()
        };
        let (a, b) = tokio::join!(
            first.send_request(request()),
            second.send_request(request())
        );
        a.unwrap().into_body().collect().await.unwrap();
        b.unwrap().into_body().collect().await.unwrap();
        first.complete(true);
        second.complete(true);
        drop((first, second));
        assert_eq!(metrics.upstream_streams_active.load(Ordering::Relaxed), 0);
        assert_eq!(
            accepted.load(Ordering::Relaxed),
            1,
            "expected a shared TCP connection"
        );
        assert!(
            peak.load(Ordering::SeqCst) >= 2,
            "expected overlapping HTTP/2 streams"
        );
        server.abort();
    }

    #[tokio::test]
    async fn watchdog_retires_stalled_early_response_without_leaking_pool_capacity() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let held_requests = Arc::new(Mutex::new(Vec::<Incoming>::new()));
        let server = tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    break;
                };
                let held_requests = held_requests.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |request: Request<Incoming>| {
                        let slow = request.uri().path() == "/slow";
                        held_requests.lock().unwrap().push(request.into_body());
                        async move {
                            if slow {
                                sleep(Duration::from_millis(400)).await;
                                Ok::<_, Infallible>(Response::new(Full::new(Bytes::from_static(
                                    b"slow",
                                ))))
                            } else {
                                Ok::<_, Infallible>(Response::new(Full::new(Bytes::new())))
                            }
                        }
                    });
                    let builder = hyper::server::conn::http2::Builder::new(TokioExecutor::new());
                    let _ = builder
                        .serve_connection(TokioIo::new(socket), service)
                        .await;
                });
            }
        });
        let mut config = config(address.to_string());
        config.limits.connections = 1;
        config.limits.idle_connections = 0;
        config.upstreams[0].connections = 1;
        config.upstreams[0].idle_connections = 0;
        config.http2.initial_stream_window_bytes = 1_024;
        let generation = Generation::new(config.clone(), 1, &HashMap::new());
        let endpoint = generation.endpoints["backend"].clone();
        let metrics = Arc::new(Metrics::default());
        let pool = Pool::new(config.limits.clone(), config.http2.clone(), metrics.clone());
        let (mut lease, _) = pool
            .acquire(generation.clone(), endpoint.clone())
            .await
            .unwrap();
        let (mut sibling, _) = pool.acquire(generation, endpoint).await.unwrap();
        let deadline = tokio::time::Instant::now() + Duration::from_millis(250);
        lease.arm_deadline(deadline);
        sibling.arm_deadline(tokio::time::Instant::now() + Duration::from_secs(1));
        let sent = Arc::new(AtomicBool::new(false));
        let dropped = Arc::new(AtomicBool::new(false));
        let request_body = StalledUploadBody {
            sent: sent.clone(),
            dropped: dropped.clone(),
            _guard: lease.upload_guard(),
        }
        .boxed_unsync();
        let request = Request::builder()
            .uri("/upload")
            .header("host", "backend.test")
            .body(request_body)
            .unwrap();
        let sibling_request = Request::builder()
            .uri("/slow")
            .header("host", "backend.test")
            .body(crate::body::full(Bytes::new()))
            .unwrap();
        let (response, sibling_response) = tokio::join!(
            lease.send_request(request),
            sibling.send_request(sibling_request)
        );
        let sibling_response = sibling_response.unwrap();
        assert_eq!(sibling_response.status(), hyper::StatusCode::OK);
        assert_eq!(
            sibling_response
                .into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes(),
            Bytes::from_static(b"slow"),
            "watchdog must leave an unexpired sibling stream operational"
        );
        let response = response.unwrap();
        assert_eq!(response.status(), hyper::StatusCode::OK);
        response.into_body().collect().await.unwrap();
        lease.complete(false);
        sibling.complete(true);
        drop((lease, sibling));
        tokio::time::timeout(Duration::from_secs(1), async {
            while !sent.load(Ordering::Acquire) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(
            !dropped.load(Ordering::Acquire),
            "upload should still be flow-control blocked after early response"
        );
        assert_eq!(
            metrics.upstream_streams_active.load(Ordering::Relaxed),
            1,
            "a detached upload still owns a stream after its response completes"
        );
        tokio::time::timeout(Duration::from_secs(2), async {
            while !dropped.load(Ordering::Acquire) || pool.available() != 1 {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("watchdog should abort the retired H2 driver and release the stuck upload");
        assert_eq!(metrics.upstream_streams_active.load(Ordering::Relaxed), 0);
        assert_eq!(
            pool.idle_count(),
            0,
            "expired connection must not return to admission"
        );
        server.abort();
    }
}
