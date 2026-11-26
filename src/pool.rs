//! HTTP/1 leases stay exclusive; HTTP/2 sockets are shared and stream slots live
//! until both the upload and downstream response have finished.
use crate::{
    body::ProxyBody,
    config::{Config, Http2Config, Limits, Route, Upstream, UpstreamProtocol, MAX_UPSTREAMS},
    diagnostics,
    routing::Routes,
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
        Arc, Mutex, MutexGuard, Weak,
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
        outcome: "acquired",
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
    pool_index: usize,
    pub config: Upstream,
    pub host: hyper::header::HeaderValue,
    pub slots: Arc<Semaphore>,
    idle_slots: Option<Arc<Semaphore>>,
    h1_opening: Option<Arc<Semaphore>>,
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
    routes: Routes,
    pub endpoints: HashMap<String, Arc<Endpoint>>,
    pub draining: AtomicBool,
    groups: HashMap<String, crate::balance::Group>,
    pub caches: HashMap<String, Arc<crate::cache::Cache>>,
    pub(crate) applications: HashMap<String, crate::application::config::Route>,
    pub(crate) application_buffers: Option<Arc<Semaphore>>,
}
impl Generation {
    #[cfg(test)]
    pub fn new(
        config: Config,
        version: u64,
        tls_clients: &HashMap<String, Arc<ClientConfig>>,
    ) -> Arc<Self> {
        let routes = Routes::compile(&config.routes).unwrap();
        Self::with_strategy(config, routes, version, tls_clients, PoolStrategy::Compact)
    }

    pub fn with_strategy(
        config: Config,
        routes: Routes,
        version: u64,
        tls_clients: &HashMap<String, Arc<ClientConfig>>,
        strategy: PoolStrategy,
    ) -> Arc<Self> {
        let endpoints: HashMap<_, _> = config
            .upstreams
            .iter()
            .enumerate()
            .map(|(pool_index, u)| {
                (
                    u.id.clone(),
                    Arc::new(Endpoint {
                        pool_index,
                        config: u.clone(),
                        host: hyper::header::HeaderValue::from_str(&u.host)
                            .expect("validated upstream authority"),
                        slots: Arc::new(Semaphore::new(u.connections)),
                        idle_slots: (strategy == PoolStrategy::Sharded)
                            .then(|| Arc::new(Semaphore::new(u.idle_connections))),
                        h1_opening: (strategy == PoolStrategy::Sharded)
                            .then(|| Arc::new(Semaphore::new(1))),
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
        let groups = config
            .routes
            .iter()
            .filter(|route| route.upstreams.len() > 1)
            .map(|route| {
                (
                    route.id.clone(),
                    crate::balance::Group::compile(route, &endpoints),
                )
            })
            .collect();
        let caches = config
            .routes
            .iter()
            .filter_map(|route| {
                route
                    .cache
                    .as_ref()
                    .map(|spec| (route.id.clone(), crate::cache::Cache::new(spec)))
            })
            .collect();
        let applications: HashMap<_, _> = config
            .routes
            .iter()
            .filter_map(|r| {
                r.application.as_ref().map(|settings| {
                    (
                        r.id.clone(),
                        crate::application::config::Route::compile(
                            endpoints[&r.upstreams[0]]
                                .config
                                .protocol
                                .application()
                                .expect("validated application protocol"),
                            settings,
                        ),
                    )
                })
            })
            .collect();
        let application_buffers = (!applications.is_empty())
            .then(|| Arc::new(Semaphore::new(crate::application::TOTAL_BUFFER_BYTES)));
        Arc::new(Self {
            version,
            routes,
            config,
            endpoints,
            draining: AtomicBool::new(false),
            groups,
            caches,
            applications,
            application_buffers,
        })
    }
    pub fn route(&self, host: &str, path: &str) -> Option<&Route> {
        self.routes.find(&self.config.routes, host, path)
    }
    pub fn named_route(&self, host: &str, name: &str) -> Option<&Route> {
        self.routes.named(&self.config.routes, host, name)
    }
    pub fn select(&self, route: &Route, client: std::net::IpAddr) -> Option<Arc<Endpoint>> {
        if let [id] = route.upstreams.as_slice() {
            let endpoint = &self.endpoints[id];
            return endpoint.eligible().then(|| endpoint.clone());
        }
        self.groups.get(&route.id)?.select(route.balance, client)
    }
}
impl Endpoint {
    pub(crate) fn eligible(&self) -> bool {
        self.active_healthy.load(Ordering::Acquire)
            && self
                .failed_until
                .lock()
                .unwrap()
                .is_none_or(|until| until <= Instant::now())
    }
}

enum Exclusive {
    Http1 {
        sender: SendRequest<ProxyBody>,
        driver: AbortHandle,
    },
    Application {
        socket: Option<crate::application::Socket>,
        returned: Option<crate::application::Returned>,
    },
}

impl Exclusive {
    fn is_closed(&self) -> bool {
        match self {
            Self::Http1 { sender, .. } => sender.is_closed(),
            Self::Application { socket, .. } => socket.is_none(),
        }
    }
}

pub struct Connection {
    transport: Exclusive,
    id: u64,
    born: Instant,
    idle_since: Instant,
    _capacity: Arc<DriverCapacity>,
    idle_credit: Option<IdleCredit>,
}
struct H2Connection {
    sender: H2SendRequest<ProxyBody>,
    id: u64,
    born: Instant,
    idle: Mutex<H2Idle>,
    streams: Arc<Semaphore>,
    capacity: usize,
    idle_limit: usize,
    key: Key,
    live: AtomicUsize,
    draining: AtomicBool,
    driver: AbortHandle,
    executor: H2TaskExecutor,
}
struct IdleCredit {
    _global: OwnedSemaphorePermit,
    _endpoint: OwnedSemaphorePermit,
}

struct H2Idle {
    since: Instant,
    credit: Option<IdleCredit>,
}

impl Drop for H2Connection {
    fn drop(&mut self) {
        self.executor.abort_all();
        self.driver.abort();
    }
}
impl Drop for Connection {
    fn drop(&mut self) {
        if let Exclusive::Http1 { driver, .. } = &self.transport {
            driver.abort();
        }
    }
}
#[derive(Hash, PartialEq, Eq, Clone, Copy)]
struct Key {
    generation: u64,
    // Assigned once within each immutable generation, including distinct
    // logical upstreams that happen to share a network address or TLS policy.
    upstream: usize,
}
#[derive(Default)]
struct Idle {
    connections: HashMap<Key, Vec<Connection>>,
    h1_count: usize,
    h2: HashMap<Key, Vec<Arc<H2Connection>>>,
}
impl Idle {
    fn pop_any_h1(&mut self, protected: Option<Key>) -> Option<Connection> {
        let socket = self
            .connections
            .iter_mut()
            .filter(|(key, _)| Some(*key) != protected.as_ref())
            .find_map(|(_, sockets)| sockets.pop())?;
        self.h1_count -= 1;
        Some(socket)
    }

    fn reclaim_h2(&mut self, protected: Option<Key>) -> bool {
        let Some((key, index)) = self
            .h2
            .iter()
            .filter(|(key, _)| Some(*key) != protected.as_ref())
            .find_map(|(key, sockets)| {
                sockets
                    .iter()
                    .position(|socket| socket.streams.available_permits() == socket.capacity)
                    .map(|index| (*key, index))
            })
        else {
            return false;
        };
        let sockets = self.h2.get_mut(&key).unwrap();
        let removed = sockets.remove(index);
        removed.idle.lock().unwrap().credit.take();
        if sockets.is_empty() {
            self.h2.remove(&key);
        }
        true
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PoolStrategy {
    Compact,
    Sharded,
}

impl PoolStrategy {
    pub fn from_environment() -> Result<Self> {
        match std::env::var("CAUCE_POOL_STRATEGY") {
            Err(std::env::VarError::NotPresent) => Ok(Self::Compact),
            Ok(value) if value == "compact" => Ok(Self::Compact),
            Ok(value) if value == "sharded" => Ok(Self::Sharded),
            _ => Err(anyhow!("CAUCE_POOL_STRATEGY must be compact or sharded")),
        }
    }
}

struct ShardedIdle {
    buckets: Box<[Mutex<Idle>]>,
    budget: Arc<Semaphore>,
    next_victim: AtomicUsize,
    used_buckets: AtomicUsize,
}

struct IdleGuard<'a> {
    guard: Option<MutexGuard<'a, Idle>>,
    times: Option<(Instant, Instant)>,
    key: Key,
    operation: &'static str,
}

impl std::ops::Deref for IdleGuard<'_> {
    type Target = Idle;
    fn deref(&self) -> &Idle {
        self.guard.as_deref().unwrap()
    }
}

impl std::ops::DerefMut for IdleGuard<'_> {
    fn deref_mut(&mut self) -> &mut Idle {
        self.guard.as_deref_mut().unwrap()
    }
}

impl Drop for IdleGuard<'_> {
    fn drop(&mut self) {
        self.guard.take();
        if let Some((start, acquired)) = self.times {
            let hold = acquired.elapsed().as_nanos() as u64;
            diagnostics::emit("pool_lock", || {
                serde_json::json!({
                    "generation": self.key.generation, "endpoint_index": self.key.upstream,
                    "operation": self.operation, "wait_ns": (acquired-start).as_nanos() as u64,
                    "hold_ns": hold,
                })
            });
        }
    }
}

struct Opening<'a> {
    guard: Option<OwnedSemaphorePermit>,
    notify: &'a Notify,
}

impl Drop for Opening<'_> {
    fn drop(&mut self) {
        self.guard.take();
        self.notify.notify_waiters();
    }
}

pub struct Pool {
    idle: Mutex<Idle>,
    sharded: Option<ShardedIdle>,
    global: Arc<Semaphore>,
    connecting: Arc<Semaphore>,
    notify: Arc<Notify>,
    limits: Limits,
    http2: Http2Config,
    pub metrics: Arc<Metrics>,
    next_id: AtomicU64,
}
// Keep the socket future and its permits together even if the task is
// cancelled before its first poll. Tuple fields drop in declaration order.
fn spawn_driver(
    connection: impl Future<Output = hyper::Result<()>> + Send + 'static,
    capacity: Arc<DriverCapacity>,
    kind: &'static str,
) -> AbortHandle {
    let lifecycle = diagnostics::Task::new(kind, diagnostics::enabled().then_some(capacity.id));
    let transport = (connection, capacity, lifecycle);
    tokio::spawn(async move {
        let (connection, _capacity, mut lifecycle) = transport;
        let result = connection.await;
        if let Some(task) = &mut lifecycle {
            task.finish("connection_end", result.as_ref().err().map(|e| e as _));
        }
    })
    .abort_handle()
}

struct DriverCapacity {
    id: u64,
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
        diagnostics::emit(
            "upstream_permits_released",
            || serde_json::json!({"connection": self.id}),
        );
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
            let mut idle = pool.lock_idle(self.connection.key, "retire");
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
        if let Some(pool) = self.pool.upgrade().filter(|pool| pool.sharded.is_some()) {
            pool.release_sharded_stream(&self.connection, &self.endpoint, &mut self.permit);
            self.endpoint.inflight.fetch_sub(1, Ordering::Relaxed);
            self.notify.notify_waiters();
            return;
        }
        self.permit.take();
        self.endpoint.inflight.fetch_sub(1, Ordering::Relaxed);
        if !self.connection.draining.load(Ordering::Acquire) {
            self.connection.idle.lock().unwrap().since = Instant::now();
        }
        let global_idle_disabled = self
            .pool
            .upgrade()
            .is_some_and(|pool| pool.limits.idle_connections == 0);
        if self.connection.streams.available_permits() == self.connection.capacity
            && (self.connection.idle_limit == 0 || global_idle_disabled)
        {
            if let Some(pool) = self.pool.upgrade() {
                let mut idle = pool.lock_idle(self.connection.key, "release");
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
        if let Some(connection) = self.connection.take() {
            diagnostics::emit(
                "upstream_lease_discarded",
                || serde_json::json!({"connection": connection.id, "reason": "incomplete"}),
            );
            drop(connection);
        }
        if self.h2.is_none() {
            self.endpoint.inflight.fetch_sub(1, Ordering::Relaxed);
        }
    }
}
impl Lease {
    pub fn connection_id(&self) -> u64 {
        self.h2.as_ref().map_or_else(
            || self.connection.as_ref().expect("HTTP/1 lease").id,
            |hold| hold.connection.id,
        )
    }

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
        exchange: Option<&diagnostics::Exchange>,
    ) -> hyper::Result<Response<Incoming>> {
        if let Some(exchange) = exchange {
            exchange.stage("upstream_sender_wait", "started");
        }
        if let Some(h2) = &self.h2 {
            let mut sender = h2.connection.sender.clone();
            sender.ready().await?;
            if let Some(exchange) = exchange {
                exchange.stage("upstream_sender_ready", "ready");
                exchange.stage("upstream_request_submitted", "hyper_dispatch");
            }
            sender.send_request(req).await
        } else {
            let Exclusive::Http1 { sender, .. } =
                &mut self.connection.as_mut().expect("HTTP/1 lease").transport
            else {
                unreachable!("HTTP request on application lease")
            };
            // Receiving the last body frame does not synchronize with the
            // driver returning to its keep-alive state on another worker.
            sender.ready().await?;
            if let Some(exchange) = exchange {
                exchange.stage("upstream_sender_ready", "ready");
                exchange.stage("upstream_request_submitted", "hyper_dispatch");
            }
            sender.send_request(req).await
        }
    }
    pub(crate) async fn application_request(
        &mut self,
        request: Request<ProxyBody>,
        context: crate::application::Contextual<'_>,
    ) -> Result<Response<crate::application::ResponseBody>> {
        let Exclusive::Application { socket, returned } = &mut self
            .connection
            .as_mut()
            .expect("application lease")
            .transport
        else {
            unreachable!("application request on HTTP lease")
        };
        let output = Arc::new(Mutex::new(None));
        *returned = Some(output.clone());
        crate::application::exchange(
            socket.take().expect("leased application socket"),
            output,
            request,
            context,
        )
        .await
    }

    pub fn complete(&mut self, upload_complete: bool) {
        if let Some(mut c) = self.connection.take() {
            if let Exclusive::Application { socket, returned } = &mut c.transport {
                *socket = returned
                    .take()
                    .and_then(|output| output.lock().unwrap().take());
            }
            if !upload_complete
                || c.transport.is_closed()
                || c.born.elapsed().as_millis() >= self.pool.limits.max_connection_age_ms as u128
            {
                diagnostics::emit(
                    "upstream_lease_discarded",
                    || serde_json::json!({"connection": c.id, "reason": "not_reusable"}),
                );
                return;
            }
            c.idle_since = Instant::now();
            let key = Key {
                generation: self.generation.version,
                upstream: self.endpoint.pool_index,
            };
            let mut idle = self.pool.lock_idle(key, "return_http1");
            // A sweep may have already removed this generation while the
            // response was finishing. Never publish an idle socket after it.
            if self.generation.draining.load(Ordering::Acquire) {
                drop(idle);
                diagnostics::emit(
                    "upstream_lease_discarded",
                    || serde_json::json!({"connection": c.id, "reason": "generation_draining"}),
                );
                return;
            }
            let retain = if self.pool.sharded.is_some() {
                c.idle_credit = self.pool.try_idle_credit(&self.endpoint);
                c.idle_credit.is_some()
            } else {
                idle.h1_count < self.pool.limits.idle_connections
                    && idle.connections.get(&key).map_or(0, Vec::len)
                        < self.endpoint.config.idle_connections
            };
            if retain {
                let connection = c.id;
                idle.connections.entry(key).or_default().push(c);
                idle.h1_count += 1;
                drop(idle);
                self.pool.notify.notify_waiters();
                diagnostics::emit(
                    "upstream_idle_return",
                    || serde_json::json!({"connection": connection}),
                );
            } else {
                drop(idle);
                diagnostics::emit(
                    "upstream_lease_discarded",
                    || serde_json::json!({"connection": c.id, "reason": "idle_limit"}),
                );
            }
        }
    }
}
impl Pool {
    #[cfg(test)]
    fn new(limits: Limits, http2: Http2Config, metrics: Arc<Metrics>) -> Arc<Self> {
        Self::with_strategy(limits, http2, metrics, PoolStrategy::Compact)
    }

    pub fn with_strategy(
        limits: Limits,
        http2: Http2Config,
        metrics: Arc<Metrics>,
        strategy: PoolStrategy,
    ) -> Arc<Self> {
        Arc::new(Self {
            idle: Mutex::default(),
            sharded: (strategy == PoolStrategy::Sharded).then(|| ShardedIdle {
                buckets: (0..MAX_UPSTREAMS).map(|_| Mutex::default()).collect(),
                budget: Arc::new(Semaphore::new(limits.idle_connections)),
                next_victim: AtomicUsize::new(0),
                used_buckets: AtomicUsize::new(0),
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
    fn lock_idle(&self, key: Key, operation: &'static str) -> IdleGuard<'_> {
        let lock = self.sharded.as_ref().map_or(&self.idle, |shards| {
            if shards.used_buckets.load(Ordering::Relaxed) <= key.upstream {
                shards
                    .used_buckets
                    .fetch_max(key.upstream + 1, Ordering::Release);
            }
            &shards.buckets[key.upstream]
        });
        let start = diagnostics::enabled().then(Instant::now);
        let guard = lock.lock().unwrap();
        IdleGuard {
            guard: Some(guard),
            times: start.map(|begin| (begin, Instant::now())),
            key,
            operation,
        }
    }

    fn try_idle_credit(&self, endpoint: &Endpoint) -> Option<IdleCredit> {
        let ep = endpoint
            .idle_slots
            .as_ref()?
            .clone()
            .try_acquire_owned()
            .ok()?;
        let global = self
            .sharded
            .as_ref()?
            .budget
            .clone()
            .try_acquire_owned()
            .ok()?;
        Some(IdleCredit {
            _global: global,
            _endpoint: ep,
        })
    }

    fn release_sharded_stream(
        &self,
        connection: &Arc<H2Connection>,
        endpoint: &Endpoint,
        permit: &mut Option<OwnedSemaphorePermit>,
    ) {
        let mut bucket = self.lock_idle(connection.key, "return_http2");
        // Serialize the final permit return and idle credit publication with admission.
        permit.take();
        if connection.streams.available_permits() != connection.capacity {
            return;
        }
        let Some(list) = bucket.h2.get_mut(&connection.key) else {
            return;
        };
        let Some(index) = list
            .iter()
            .position(|member| Arc::ptr_eq(member, connection))
        else {
            return;
        };
        if !connection.draining.load(Ordering::Acquire) && !connection.sender.is_closed() {
            if let Some(credit) = self.try_idle_credit(endpoint) {
                let mut idle = connection.idle.lock().unwrap();
                idle.since = Instant::now();
                idle.credit = Some(credit);
                return;
            }
        }
        list.swap_remove(index);
        if list.is_empty() {
            bucket.h2.remove(&connection.key);
        }
    }

    fn reserve(
        &self,
        endpoint: &Endpoint,
        global: OwnedSemaphorePermit,
        ep: OwnedSemaphorePermit,
    ) -> Arc<DriverCapacity> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        diagnostics::emit("upstream_permits_reserved", || {
            serde_json::json!({
                "connection": id, "endpoint": endpoint.config.id,
                "global_available": self.global.available_permits(),
                "endpoint_available": endpoint.slots.available_permits(),
            })
        });
        Arc::new(DriverCapacity {
            id,
            global: Some(global),
            endpoint: Some(ep),
            notify: self.notify.clone(),
        })
    }

    pub fn sweep(&self, oldest_generation: u64) {
        if let Some(shards) = &self.sharded {
            for bucket in &shards.buckets[..shards.used_buckets.load(Ordering::Acquire)] {
                self.sweep_bucket(&mut bucket.lock().unwrap(), oldest_generation);
            }
        } else {
            self.sweep_bucket(&mut self.idle.lock().unwrap(), oldest_generation);
        }
        self.notify.notify_waiters();
    }

    fn sweep_bucket(&self, idle: &mut Idle, oldest_generation: u64) {
        // A sweep scheduled before reload can finish after publication. It may
        // retire older generations, while new sockets keep their own lifetime.
        idle.connections.retain(|k, list| {
            if k.generation < oldest_generation {
                return false;
            }
            list.retain(|c| {
                !c.transport.is_closed()
                    && c.idle_since.elapsed().as_millis() < self.limits.idle_connection_ms as u128
                    && c.born.elapsed().as_millis() < self.limits.max_connection_age_ms as u128
            });
            !list.is_empty()
        });
        idle.h1_count = idle.connections.values().map(Vec::len).sum();
        idle.h2.retain(|k, list| {
            if k.generation < oldest_generation {
                return false;
            }
            list.retain(|c| {
                !c.sender.is_closed()
                    && c.born.elapsed().as_millis() < self.limits.max_connection_age_ms as u128
                    && (c.streams.available_permits() < c.capacity
                        || (c.idle.lock().unwrap().since.elapsed().as_millis()
                            < self.limits.idle_connection_ms as u128))
            });
            if self.sharded.is_some() {
                return !list.is_empty();
            }
            let endpoint_idle_limit = list.first().map_or(0, |c| c.idle_limit);
            list.sort_by_key(|c| c.idle.lock().unwrap().since.elapsed());
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
        if self.sharded.is_some() {
            return;
        }
        let mut idle_total = idle.h1_count;
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
            if !idle.reclaim_h2(None) && idle.pop_any_h1(None).is_none() {
                break;
            }
            idle_total -= 1;
        }
    }
    pub fn available(&self) -> usize {
        self.global.available_permits()
    }
    pub fn idle_count(&self) -> usize {
        if let Some(shards) = &self.sharded {
            return self.limits.idle_connections - shards.budget.available_permits();
        }
        let idle = self.idle.lock().unwrap();
        idle.h1_count
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
        log_attempt: bool,
    ) -> Result<(Lease, Option<Attempt>)> {
        let begin = Instant::now();
        let key = Key {
            generation: generation.version,
            upstream: endpoint.pool_index,
        };
        if endpoint.config.protocol == UpstreamProtocol::Http2 {
            return self
                .acquire_h2(generation, endpoint, key, begin, log_attempt)
                .await;
        }
        let acquired = timeout(Duration::from_millis(self.limits.acquire_ms), async {
            loop {
                let notified = self.notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                {
                    let mut idle = self.lock_idle(key, "acquire_http1");
                    let Idle {
                        connections,
                        h1_count,
                        ..
                    } = &mut *idle;
                    if let Some(list) = connections.get_mut(&key) {
                        while let Some(mut c) = list.pop() {
                            *h1_count -= 1;
                            c.idle_credit.take();
                            if !c.transport.is_closed()
                                && c.idle_since.elapsed().as_millis()
                                    < self.limits.idle_connection_ms as u128
                                && c.born.elapsed().as_millis()
                                    < self.limits.max_connection_age_ms as u128
                            {
                                return (Some(c), None);
                            }
                        }
                    }
                    let opening = match &endpoint.h1_opening {
                        Some(gate) => gate.clone().try_acquire_owned().ok(),
                        None => None,
                    };
                    if endpoint.h1_opening.is_none() || opening.is_some() {
                        if let Ok(ep) = endpoint.slots.clone().try_acquire_owned() {
                            if self.sharded.is_none()
                                && self.global.available_permits() == 0
                                && idle.pop_any_h1(None).is_none()
                            {
                                idle.reclaim_h2(None);
                            }
                            if let Ok(global) = self.global.clone().try_acquire_owned() {
                                return (None, Some((global, ep, opening)));
                            }
                            drop(ep);
                            drop(idle);
                            if self.sharded.is_some() && self.global.available_permits() == 0 {
                                self.reclaim_idle_connection(key);
                            }
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
        let mut attempt = log_attempt.then(|| Attempt {
            reused: Some(acquired.0.is_some()),
            pool_wait_ms: Some(wait),
            ..empty_attempt()
        });
        let connection = if let Some(c) = acquired.0 {
            self.metrics.reused.fetch_add(1, Ordering::Relaxed);
            c
        } else {
            let (global, ep, opening) = acquired.1.unwrap();
            let _opening = opening.map(|guard| Opening {
                guard: Some(guard),
                notify: &self.notify,
            });
            let capacity = self.reserve(&endpoint, global, ep);
            let gate_started = log_attempt.then(Instant::now);
            let _gate = timeout(
                Duration::from_millis(self.limits.acquire_ms),
                self.connecting.clone().acquire_owned(),
            )
            .await
            .map_err(|_| anyhow!("connect_capacity_timeout"))??;
            diagnostics::emit(
                "upstream_connect_gate_acquired",
                || serde_json::json!({"connection": capacity.id, "available": self.connecting.available_permits()}),
            );
            if let Some(attempt) = &mut attempt {
                attempt.connect_gate_ms = gate_started.map(ms);
            }
            let connected = self.connect(&endpoint, capacity, &mut attempt).await;
            if connected.is_err() {
                *endpoint.failed_until.lock().unwrap() = Some(
                    Instant::now() + Duration::from_millis(endpoint.config.failure_backoff_ms),
                );
                self.metrics.failures.fetch_add(1, Ordering::Relaxed);
                self.notify.notify_waiters();
            }
            connected?
        };
        if let Some(attempt) = &mut attempt {
            attempt.connection_id = Some(format!("c{}", connection.id));
        }
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
        log_attempt: bool,
    ) -> Result<(Lease, Option<Attempt>)> {
        let deadline = tokio::time::Instant::now() + Duration::from_millis(self.limits.acquire_ms);
        let mut reserved = None;
        let mut attempt = log_attempt.then(empty_attempt);
        let (connection, stream_permit, newly_connected) = loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            let mut selected = None;
            {
                let mut idle = self.lock_idle(key, "acquire_http2");
                if let Some(list) = idle.h2.get_mut(&key) {
                    list.retain(|c| {
                        !c.draining.load(Ordering::Acquire)
                            && !c.sender.is_closed()
                            && c.born.elapsed().as_millis()
                                < self.limits.max_connection_age_ms as u128
                            && (c.streams.available_permits() < c.capacity
                                || c.idle.lock().unwrap().since.elapsed().as_millis()
                                    < self.limits.idle_connection_ms as u128)
                    });
                    for c in list.iter() {
                        if c.born.elapsed().as_millis() >= self.limits.max_connection_age_ms as u128
                        {
                            continue;
                        }
                        if let Ok(p) = c.streams.clone().try_acquire_owned() {
                            if self.sharded.is_some() {
                                c.idle.lock().unwrap().credit.take();
                            }
                            c.live.fetch_add(1, Ordering::AcqRel);
                            selected = Some((c.clone(), p));
                            break;
                        }
                    }
                }
            }
            if let Some((c, p)) = selected {
                break (c, p, false);
            }
            if reserved.is_none() {
                let blocked_by_global = match endpoint.slots.clone().try_acquire_owned() {
                    Ok(ep) => match self.global.clone().try_acquire_owned() {
                        Ok(global) => {
                            reserved = Some(self.reserve(&endpoint, global, ep));
                            false
                        }
                        Err(_) => true,
                    },
                    Err(_) => false,
                };
                if (blocked_by_global || self.sharded.is_none())
                    && reserved.is_none()
                    && self.global.available_permits() == 0
                {
                    self.reclaim_idle_connection(key);
                }
            }
            if let Some(capacity) = reserved.take() {
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
                let started = log_attempt.then(Instant::now);
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
                diagnostics::emit(
                    "upstream_connect_gate_acquired",
                    || serde_json::json!({"connection": capacity.id, "available": self.connecting.available_permits()}),
                );
                if let Some(attempt) = &mut attempt {
                    attempt.connect_gate_ms = started.map(ms);
                }
                // A concurrent opener may have created a usable socket while we waited.
                let existing = {
                    let idle = self.lock_idle(key, "recheck_http2");
                    idle.h2.get(&key).and_then(|xs| {
                        xs.iter()
                            .filter(|c| {
                                !c.draining.load(Ordering::Acquire)
                                    && !c.sender.is_closed()
                                    && c.born.elapsed().as_millis()
                                        < self.limits.max_connection_age_ms as u128
                            })
                            .find_map(|c| {
                                c.streams.clone().try_acquire_owned().ok().map(|p| {
                                    if self.sharded.is_some() {
                                        c.idle.lock().unwrap().credit.take();
                                    }
                                    c.live.fetch_add(1, Ordering::AcqRel);
                                    (c.clone(), p)
                                })
                            })
                    })
                };
                if let Some((c, p)) = existing {
                    drop(gate);
                    drop(open_gate);
                    drop(capacity);
                    break (c, p, false);
                }
                match self
                    .connect_h2(&endpoint, key, capacity, &mut attempt)
                    .await
                {
                    Ok(c) => {
                        let p = c
                            .streams
                            .clone()
                            .try_acquire_owned()
                            .map_err(|_| anyhow!("h2_stream_capacity_race"))?;
                        c.live.fetch_add(1, Ordering::AcqRel);
                        {
                            let mut idle = self.lock_idle(key, "publish_http2");
                            if !generation.draining.load(Ordering::Acquire) {
                                idle.h2.entry(key).or_default().push(c.clone());
                            }
                        }
                        drop(gate);
                        drop(open_gate);
                        break (c, p, true);
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
        let wait = ms(begin);
        self.metrics
            .pool_wait_us
            .fetch_add((wait * 1000.0) as u64, Ordering::Relaxed);
        if let Some(attempt) = &mut attempt {
            attempt.connection_id = Some(format!("c{}", connection.id));
            attempt.reused = Some(!newly_connected);
            attempt.pool_wait_ms = Some(wait);
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
        attempt: &mut Option<Attempt>,
    ) -> Result<Arc<H2Connection>> {
        let duration = Duration::from_millis(self.limits.connect_ms);
        let tcp_start = attempt.is_some().then(Instant::now);
        let io: Box<dyn PoolIo> = if let Some(path) = e.config.address.strip_prefix("unix:") {
            Box::new(
                timeout(duration, UnixStream::connect(path))
                    .await
                    .map_err(|_| anyhow!("unix_connect_timeout"))??,
            )
        } else {
            let dns_start = attempt.is_some().then(Instant::now);
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
            if let Some(attempt) = attempt {
                attempt.dns_ms = dns_start.map(ms);
            }
            let addr = addresses[e.dns_cursor.fetch_add(1, Ordering::Relaxed) % addresses.len()];
            let socket = timeout(duration, TcpStream::connect(addr))
                .await
                .map_err(|_| anyhow!("tcp_connect_timeout"))??;
            socket.set_nodelay(true)?;
            socket2::SockRef::from(&socket).set_tcp_keepalive(
                &socket2::TcpKeepalive::new()
                    .with_time(Duration::from_secs(self.limits.tcp_keepalive_secs)),
            )?;
            if let Some(attempt) = attempt {
                attempt.tcp_ms = tcp_start.map(ms);
            }
            if let Some(tls_config) = &e.config.tls {
                let client = e
                    .tls_client
                    .clone()
                    .ok_or_else(|| anyhow!("upstream_tls_not_prepared"))?;
                let name = tls::server_name(tls_config)?;
                let tls_start = attempt.is_some().then(Instant::now);
                let stream = timeout(
                    Duration::from_millis(self.limits.tls_handshake_ms),
                    tokio_rustls::TlsConnector::from(client).connect(name, socket),
                )
                .await
                .map_err(|_| anyhow!("tls_handshake_timeout"))?
                .map_err(|_| anyhow!("tls_handshake_error"))?;
                if let Some(attempt) = attempt {
                    attempt.tls_ms = tls_start.map(ms);
                }
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
            builder.handshake::<_, ProxyBody>(TokioIo::new(diagnostics::Io::new(
                io,
                "upstream",
                Some(capacity.id),
            ))),
        )
        .await
        .map_err(|_| anyhow!("http2_handshake_timeout"))??;
        let id = capacity.id;
        let driver = spawn_driver(conn, capacity, "upstream_http2");
        self.metrics.new_connections.fetch_add(1, Ordering::Relaxed);
        Ok(Arc::new(H2Connection {
            sender,
            id,
            born: Instant::now(),
            idle: Mutex::new(H2Idle {
                since: Instant::now(),
                credit: None,
            }),
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
    fn reclaim_idle_connection(&self, requester: Key) {
        if let Some(shards) = &self.sharded {
            let first = shards.next_victim.fetch_add(1, Ordering::Relaxed);
            let used = shards.used_buckets.load(Ordering::Acquire);
            for offset in 0..used {
                let index = first.wrapping_add(offset) % used;
                let mut bucket = shards.buckets[index].lock().unwrap();
                // An idle return can win the race after acquisition checked
                // its bucket. Leave that socket available to the waiter.
                let protected = (index == requester.upstream).then_some(requester);
                if bucket.pop_any_h1(protected).is_some() || bucket.reclaim_h2(protected) {
                    break;
                }
            }
            return;
        }
        let mut idle = self.idle.lock().unwrap();
        if !idle.reclaim_h2(None) {
            idle.pop_any_h1(None);
        }
    }
    async fn connect(
        &self,
        e: &Endpoint,
        capacity: Arc<DriverCapacity>,
        attempt: &mut Option<Attempt>,
    ) -> Result<Connection> {
        let duration = Duration::from_millis(self.limits.connect_ms);
        let transport = if e.config.protocol.application().is_some() {
            let socket: crate::application::Socket =
                if let Some(path) = e.config.address.strip_prefix("unix:") {
                    Box::new(
                        timeout(duration, UnixStream::connect(path))
                            .await
                            .map_err(|_| anyhow!("unix_connect_timeout"))??,
                    )
                } else {
                    Box::new(self.connect_tcp(e, attempt, duration).await?)
                };
            Exclusive::Application {
                socket: Some(socket),
                returned: None,
            }
        } else {
            let mut http = hyper::client::conn::http1::Builder::new();
            http.max_headers(self.limits.max_headers)
                .max_header_size(self.limits.max_header_bytes)
                .max_buf_size(self.limits.max_header_bytes);
            let (sender, driver) = if let Some(path) = e.config.address.strip_prefix("unix:") {
                let socket = timeout(duration, UnixStream::connect(path))
                    .await
                    .map_err(|_| anyhow!("unix_connect_timeout"))??;
                let io = TokioIo::new(diagnostics::Io::new(socket, "upstream", Some(capacity.id)));
                let (sender, conn) = timeout(duration, http.handshake(io)).await??;
                (
                    sender,
                    spawn_driver(conn.with_upgrades(), capacity.clone(), "upstream_http1"),
                )
            } else {
                let socket = self.connect_tcp(e, attempt, duration).await?;
                let io: Box<dyn PoolIo> = if let Some(tls_config) = &e.config.tls {
                    let client = e
                        .tls_client
                        .clone()
                        .ok_or_else(|| anyhow!("upstream_tls_not_prepared"))?;
                    let name = tls::server_name(tls_config)?;
                    let tls_start = attempt.is_some().then(Instant::now);
                    let stream = timeout(
                        Duration::from_millis(self.limits.tls_handshake_ms),
                        tokio_rustls::TlsConnector::from(client).connect(name, socket),
                    )
                    .await
                    .map_err(|_| anyhow!("tls_handshake_timeout"))?
                    .map_err(|_| anyhow!("tls_handshake_error"))?;
                    if let Some(attempt) = attempt {
                        attempt.tls_ms = tls_start.map(ms);
                    }
                    Box::new(stream)
                } else {
                    Box::new(socket)
                };
                let (sender, conn) = timeout(
                    duration,
                    http.handshake(TokioIo::new(diagnostics::Io::new(
                        io,
                        "upstream",
                        Some(capacity.id),
                    ))),
                )
                .await??;
                (
                    sender,
                    spawn_driver(conn.with_upgrades(), capacity.clone(), "upstream_http1"),
                )
            };
            Exclusive::Http1 { sender, driver }
        };
        self.metrics.new_connections.fetch_add(1, Ordering::Relaxed);
        Ok(Connection {
            transport,
            id: capacity.id,
            born: Instant::now(),
            idle_since: Instant::now(),
            _capacity: capacity,
            idle_credit: None,
        })
    }
    async fn connect_tcp(
        &self,
        e: &Endpoint,
        attempt: &mut Option<Attempt>,
        duration: Duration,
    ) -> Result<TcpStream> {
        let dns_start = attempt.is_some().then(Instant::now);
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
        if let Some(attempt) = attempt {
            attempt.dns_ms = dns_start.map(ms);
        }
        let chosen = addresses[e.dns_cursor.fetch_add(1, Ordering::Relaxed) % addresses.len()];
        let tcp_start = attempt.is_some().then(Instant::now);
        let socket = timeout(duration, TcpStream::connect(chosen))
            .await
            .map_err(|_| anyhow!("tcp_connect_timeout"))??;
        socket.set_nodelay(true)?;
        socket2::SockRef::from(&socket).set_tcp_keepalive(
            &socket2::TcpKeepalive::new()
                .with_time(Duration::from_secs(self.limits.tcp_keepalive_secs)),
        )?;
        if let Some(attempt) = attempt {
            attempt.tcp_ms = tcp_start.map(ms);
        }
        Ok(socket)
    }

    pub fn mark_failed(&self, e: &Endpoint) {
        *e.failed_until.lock().unwrap() =
            Some(Instant::now() + Duration::from_millis(e.config.failure_backoff_ms));
        self.metrics.failures.fetch_add(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod pool_tests {
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
            stream: None,
            http3: None,
            listeners: Vec::new(),
            listen: Some("127.0.0.1:19080".parse().unwrap()),
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
            real_ip: None,
            rate_zones: Default::default(),
            max_connections_per_peer: None,
            upstreams: vec![Upstream {
                health_check: None,
                id: "backend".into(),
                address,
                host: "backend.test".into(),
                isolation: "test".into(),
                weight: 1,
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
                path_match: crate::config::PathMatch::Prefix,
                path_pattern: None,
                headers: None,
                action: None,
                content: None,
                compression: None,
                cache: None,
                application: None,
                allow_clients: None,
                rate_zone: None,
                balance: crate::config::Balance::RoundRobin,
                upstreams: vec!["backend".into()],
            }],
        }
    }

    #[test]
    fn weighted_groups_balance_concurrently_and_preserve_hashes_on_recovery() {
        let mut cfg = config("127.0.0.1:8000".into());
        let mut second = cfg.upstreams[0].clone();
        second.id = "second".into();
        cfg.upstreams.push(second);
        cfg.upstreams[0].weight = 3;
        cfg.routes[0].upstreams.push("second".into());
        cfg.routes[0].balance = crate::config::Balance::WeightedRoundRobin;
        let mut hashed = cfg.routes[0].clone();
        hashed.id = "hashed".into();
        hashed.path_prefix = "/hash".into();
        hashed.balance = crate::config::Balance::ConsistentHash;
        cfg.routes.push(hashed);
        let generation = Generation::new(cfg, 1, &HashMap::new());
        let route = &generation.config.routes[0];
        let client = "192.0.2.1".parse().unwrap();
        let first = AtomicUsize::new(0);
        std::thread::scope(|scope| {
            for _ in 0..8 {
                scope.spawn(|| {
                    for _ in 0..100 {
                        if generation.select(route, client).unwrap().config.id == "backend" {
                            first.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                });
            }
        });
        assert_eq!(first.into_inner(), 600);
        let route = &generation.config.routes[1];
        let selected = generation.select(route, client).unwrap();
        *selected.failed_until.lock().unwrap() = Some(Instant::now() + Duration::from_secs(2));
        assert_ne!(
            generation.select(route, client).unwrap().config.id,
            selected.config.id
        );
        *selected.failed_until.lock().unwrap() = None;
        assert_eq!(
            generation.select(route, client).unwrap().config.id,
            selected.config.id
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancelled_opening_wakes_capacity_waiters() {
        for strategy in [PoolStrategy::Compact, PoolStrategy::Sharded] {
            for protocol in [UpstreamProtocol::Http1, UpstreamProtocol::Http2] {
                let mut cfg = config("127.0.0.1:1".into());
                cfg.limits.connections = 1;
                cfg.limits.connecting = 1;
                cfg.upstreams[0].connections = 1;
                cfg.upstreams[0].protocol = protocol;
                let routes = Routes::compile(&cfg.routes).unwrap();
                let generation =
                    Generation::with_strategy(cfg, routes, 1, &HashMap::new(), strategy);
                let pool = Pool::with_strategy(
                    generation.config.limits.clone(),
                    generation.config.http2.clone(),
                    Arc::new(Metrics::default()),
                    strategy,
                );
                let endpoint = generation.endpoints["backend"].clone();
                let _gate = pool.connecting.acquire().await.unwrap();
                let mut opening = Box::pin(pool.acquire(generation, endpoint.clone(), false));
                std::future::poll_fn(|cx| {
                    assert!(opening.as_mut().poll(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
                assert_eq!(pool.available(), 0);
                assert_eq!(endpoint.slots.available_permits(), 0);

                let notified = pool.notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                drop(opening);
                timeout(Duration::from_millis(200), notified).await.unwrap();
                assert_eq!(pool.available(), 1);
                assert_eq!(endpoint.slots.available_permits(), 1);
                if let Some(opening) = &endpoint.h1_opening {
                    assert_eq!(opening.available_permits(), 1);
                }
            }
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn sharded_eviction_preserves_a_live_lease_and_endpoint_identity() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            while let Ok((socket, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let service = service_fn(|req: Request<Incoming>| async move {
                        let host = req.headers().get("host").unwrap().as_bytes();
                        Ok::<_, Infallible>(Response::new(Full::new(Bytes::copy_from_slice(host))))
                    });
                    let _ = hyper_util::server::conn::auto::Builder::new(TokioExecutor::new())
                        .serve_connection(TokioIo::new(socket), service)
                        .await;
                });
            }
        });
        for protocol in [UpstreamProtocol::Http1, UpstreamProtocol::Http2] {
            let mut cfg = config(address.to_string());
            cfg.limits.connections = 1;
            cfg.limits.idle_connections = 1;
            cfg.upstreams[0].protocol = protocol;
            cfg.upstreams[0].connections = 1;
            cfg.upstreams[0].idle_connections = 1;
            let mut second = cfg.upstreams[0].clone();
            second.id = "second".into();
            second.host = "second.test".into();
            cfg.upstreams.push(second);

            let pool = Pool::with_strategy(
                cfg.limits.clone(),
                cfg.http2.clone(),
                Arc::new(Metrics::default()),
                PoolStrategy::Sharded,
            );
            let routes = Routes::compile(&cfg.routes).unwrap();
            let generation =
                Generation::with_strategy(cfg, routes, 1, &HashMap::new(), PoolStrategy::Sharded);
            let a = generation.endpoints["backend"].clone();
            let b = generation.endpoints["second"].clone();
            let (mut first, _) = pool
                .acquire(generation.clone(), a.clone(), false)
                .await
                .unwrap();
            let id = first.connection_id();
            assert_eq!(pool.available(), 0);
            assert_eq!(pool.idle_count(), 0);
            let mut pending = Box::pin(pool.acquire(generation.clone(), b.clone(), false));
            std::future::poll_fn(|cx| {
                assert!(pending.as_mut().poll(cx).is_pending());
                Poll::Ready(())
            })
            .await;
            let request = Request::builder()
                .uri("http://backend.test/")
                .header("host", "backend.test")
                .body(crate::body::full(""))
                .unwrap();
            let response = first.send_request(request, None).await.unwrap();
            assert_eq!(
                response.into_body().collect().await.unwrap().to_bytes(),
                "backend.test"
            );
            first.complete(true);
            drop(first);
            assert_eq!(pool.idle_count(), 1);
            let (mut replacement, _) = timeout(Duration::from_secs(2), pending)
                .await
                .unwrap()
                .unwrap();
            assert_ne!(id, replacement.connection_id());
            assert_eq!(pool.idle_count(), 0);
            let request = Request::builder()
                .uri("http://second.test/")
                .header("host", "second.test")
                .body(crate::body::full(""))
                .unwrap();
            let response = replacement.send_request(request, None).await.unwrap();
            assert_eq!(
                response.into_body().collect().await.unwrap().to_bytes(),
                "second.test"
            );
            replacement.complete(true);
            drop(replacement);
            assert_eq!(pool.idle_count(), 1);
            // Reclaim after a same-generation return reproduces the
            // interleaving between a cold lookup and a pressure scan.
            pool.reclaim_idle_connection(Key {
                generation: generation.version,
                upstream: b.pool_index,
            });
            assert_eq!(pool.idle_count(), 1);
            pool.sweep(2);
            assert_eq!(pool.idle_count(), 0);
            drop(
                timeout(Duration::from_secs(2), pool.global.acquire())
                    .await
                    .unwrap()
                    .unwrap(),
            );
        }
        server.abort();
    }

    struct ParkAfterReply {
        socket: tokio::io::DuplexStream,
        reads_after_reply: Option<usize>,
        pause: Option<(
            tokio::sync::oneshot::Sender<()>,
            std::sync::mpsc::Receiver<()>,
        )>,
    }

    impl tokio::io::AsyncRead for ParkAfterReply {
        fn poll_read(
            mut self: Pin<&mut Self>,
            cx: &mut Context<'_>,
            buf: &mut tokio::io::ReadBuf<'_>,
        ) -> Poll<std::io::Result<()>> {
            if let Some(reads) = &mut self.reads_after_reply {
                *reads += 1;
                // Hyper probes the socket once before delivering headers.
                // Park on its next read, before it polls the sender again.
                if *reads == 2 {
                    let (parked, proceed) = self.pause.take().unwrap();
                    parked.send(()).unwrap();
                    let _ = proceed.recv_timeout(Duration::from_secs(3));
                }
            }
            let before = buf.filled().len();
            let result = Pin::new(&mut self.socket).poll_read(cx, buf);
            if self.reads_after_reply.is_none() && buf.filled().len() > before {
                self.reads_after_reply = Some(0);
            }
            result
        }
    }

    impl tokio::io::AsyncWrite for ParkAfterReply {
        fn poll_write(
            mut self: Pin<&mut Self>,
            cx: &mut Context<'_>,
            data: &[u8],
        ) -> Poll<std::io::Result<usize>> {
            Pin::new(&mut self.socket).poll_write(cx, data)
        }

        fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<std::io::Result<()>> {
            Poll::Ready(Ok(()))
        }

        fn poll_shutdown(
            mut self: Pin<&mut Self>,
            cx: &mut Context<'_>,
        ) -> Poll<std::io::Result<()>> {
            Pin::new(&mut self.socket).poll_shutdown(cx)
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn reused_http1_sender_waits_for_the_driver_after_complete_response() {
        use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
        let mut cfg = config("127.0.0.1:1".into());
        cfg.upstreams[0].protocol = UpstreamProtocol::Http1;
        let generation = Generation::new(cfg, 1, &HashMap::new());
        let pool = Pool::new(
            generation.config.limits.clone(),
            generation.config.http2.clone(),
            Arc::new(Metrics::default()),
        );
        let endpoint = generation.endpoints["backend"].clone();
        let (socket, peer) = tokio::io::duplex(4096);
        let (parked, driver_parked) = tokio::sync::oneshot::channel();
        let (resume_driver, proceed) = std::sync::mpsc::channel();
        let io = TokioIo::new(ParkAfterReply {
            socket,
            reads_after_reply: None,
            pause: Some((parked, proceed)),
        });
        let (sender, driver) = hyper::client::conn::http1::handshake(io).await.unwrap();
        let capacity = pool.reserve(
            &endpoint,
            pool.global.clone().try_acquire_owned().unwrap(),
            endpoint.slots.clone().try_acquire_owned().unwrap(),
        );
        let mut lease = Lease {
            connection: Some(Connection {
                transport: Exclusive::Http1 {
                    sender,
                    driver: spawn_driver(driver, capacity.clone(), "upstream_http1"),
                },
                id: capacity.id,
                born: Instant::now(),
                idle_since: Instant::now(),
                _capacity: capacity,
                idle_credit: None,
            }),
            pool,
            generation,
            endpoint,
            h2: None,
        };
        let backend = tokio::spawn(async move {
            let mut peer = BufReader::new(peer);
            for _ in 0..2 {
                loop {
                    let mut line = Vec::new();
                    assert_ne!(peer.read_until(b'\n', &mut line).await.unwrap(), 0);
                    if line == b"\r\n" {
                        break;
                    }
                }
                peer.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    .await
                    .unwrap();
            }
        });
        let request = || {
            Request::builder()
                .uri("/")
                .header("host", "backend.test")
                .body(crate::body::full(""))
                .unwrap()
        };
        let first = lease.send_request(request(), None).await.unwrap();
        assert!(first
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .is_empty());
        timeout(Duration::from_secs(1), driver_parked)
            .await
            .unwrap()
            .unwrap();
        let Exclusive::Http1 { sender, .. } = &lease.connection.as_ref().unwrap().transport else {
            panic!("expected HTTP/1")
        };
        assert!(!sender.is_ready());
        lease.complete(true);
        assert_eq!(lease.pool.idle_count(), 1);
        let (mut lease, attempt) = lease
            .pool
            .acquire(lease.generation.clone(), lease.endpoint.clone(), true)
            .await
            .unwrap();
        assert_eq!(attempt.unwrap().reused, Some(true));
        assert_eq!(lease.pool.idle_count(), 0);
        let mut second = Box::pin(lease.send_request(request(), None));
        let second_state = std::future::poll_fn(|cx| Poll::Ready(second.as_mut().poll(cx))).await;
        resume_driver.send(()).unwrap();
        assert!(
            second_state.is_pending(),
            "dispatch tried to send while driver was parked"
        );
        assert_eq!(
            timeout(Duration::from_secs(1), second)
                .await
                .unwrap()
                .unwrap()
                .status(),
            200
        );
        backend.await.unwrap();
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
                .acquire(generation.clone(), endpoint.clone(), true)
                .await
                .unwrap();
            let attempt = attempt.unwrap();
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
                let response = lease.send_request(request, None).await.unwrap();
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
            pool.acquire(generation.clone(), endpoint.clone(), true),
            pool.acquire(generation, endpoint, true),
        );
        let (mut first, first_attempt) = first_result.unwrap();
        let (mut second, second_attempt) = second_result.unwrap();
        assert_eq!(
            first_attempt.unwrap().connection_id,
            second_attempt.unwrap().connection_id
        );
        assert_eq!(metrics.upstream_streams_active.load(Ordering::Relaxed), 2);
        let request = || {
            Request::builder()
                .uri("/")
                .header("host", "backend.test")
                .body(crate::body::full(Bytes::new()))
                .unwrap()
        };
        let (a, b) = tokio::join!(
            first.send_request(request(), None),
            second.send_request(request(), None)
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
            .acquire(generation.clone(), endpoint.clone(), true)
            .await
            .unwrap();
        let (mut sibling, _) = pool.acquire(generation, endpoint, true).await.unwrap();
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
            lease.send_request(request, None),
            sibling.send_request(sibling_request, None)
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
