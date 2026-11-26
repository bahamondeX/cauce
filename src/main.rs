mod actions;
mod admission;
mod application;
mod balance;
mod body;
mod cache;
mod compression;
mod config;
mod content;
mod diagnostics;
mod dns;
mod headers;
mod health;
mod http3;
mod http_tcp;
mod identity;
mod mail;
mod pool;
mod proxy;
mod resilience;
mod routing;
mod static_files;
mod stream;
mod telemetry;
mod tls;
mod transport;
mod websocket;

use anyhow::{Context, Result};
use config::Config;
use hyper::{server::conn::http1, service::service_fn};
use hyper_util::rt::TokioIo;
use pool::{Generation, Pool, PoolStrategy};
use proxy::State;
use std::{
    collections::HashMap,
    convert::Infallible,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, RwLock,
    },
    time::Duration,
};
use telemetry::{Logger, Metrics};
use tokio::{
    net::TcpListener,
    signal::unix::{signal, SignalKind},
    sync::{watch, Semaphore},
    task::JoinSet,
};
trait ClientIo: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send {}
impl<T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send> ClientIo for T {}

fn main() -> Result<()> {
    diagnostics::init();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .max_blocking_threads(16)
        .build()?;
    let result = runtime.block_on(run());
    // The OS DNS resolver may outlive a cancelled lookup. Do not let it hold shutdown.
    runtime.shutdown_timeout(Duration::from_millis(500));
    diagnostics::flush();
    result
}
async fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let check = args.get(1).is_some_and(|a| a == "check");
    let path = PathBuf::from(
        args.get(if check { 2 } else { 1 })
            .map(String::as_str)
            .unwrap_or("examples/cauce.yaml"),
    );
    let strategy = PoolStrategy::from_environment()?;
    let (config, routes) = Config::load(&path).await?;
    let config = Arc::new(config);
    let tls_clients = prepare_tls_clients(&config).await?;
    let mut server_tls = prepare_server_tls(config.tls.clone(), config.http2.enabled).await?;
    let extra_tls = prepare_explicit_listeners(&config).await?;
    let quic_config = http3::prepare(&config).await?;
    let resolver = config.dns.as_ref().map(dns::Resolver::new).transpose()?;
    let mail = mail::prepare(&config.mail, resolver.clone()).await?;
    let stream_tls = stream::prepare_tls(config.stream.as_deref()).await?;
    if check {
        println!("configuration valid");
        return Ok(());
    }
    let listener = if let Some(address) = config.listen {
        Some(TcpListener::bind(address).await.context("data listener")?)
    } else {
        None
    };
    let mut extra_listeners = Vec::with_capacity(config.listeners.len());
    for listener in &config.listeners {
        let bound = stream::socket(listener.listen, socket2::Type::STREAM, false)
            .context("HTTP TCP bind")?;
        bound.listen(1024)?;
        extra_listeners.push(TcpListener::from_std(bound.into())?);
    }
    let admin_listener = TcpListener::bind(config.admin)
        .await
        .context("admin listener")?;
    let stream = config
        .stream
        .as_ref()
        .map(|spec| stream::Listeners::bind((**spec).clone(), resolver.clone(), stream_tls))
        .transpose()?;
    let mail = mail
        .into_iter()
        .map(|runtime| {
            let listener = runtime.bind()?;
            Ok((runtime, listener))
        })
        .collect::<Result<Vec<_>>>()?;
    let endpoint = quic_config
        .map(|quic| http3::bind(config.http3.as_deref().unwrap(), quic))
        .transpose()?;
    let alt_svc = config.http3.as_ref().and_then(|spec| spec.alt_svc());
    let mut metrics = stream
        .as_ref()
        .map_or_else(Metrics::default, stream::Listeners::metrics);
    let mail_counters = (!mail.is_empty()).then(|| Arc::new(mail::Counters::default()));
    metrics.mail = mail_counters.clone();
    let metrics = Arc::new(metrics);
    let (logger, log_thread) = Logger::start(config.logs.clone(), metrics.clone());
    let pool = Pool::with_strategy(
        config.limits.clone(),
        config.http2.clone(),
        metrics,
        strategy,
    );
    let state = Arc::new(State {
        rate_zones: config
            .rate_zones
            .iter()
            .map(|(id, spec)| (id.clone(), Arc::new(admission::Zone::new(spec.clone()))))
            .collect(),
        peers: config
            .max_connections_per_peer
            .map(|cap| Arc::new(admission::PeerGate::new(cap))),
        generation: RwLock::new(Generation::with_strategy(
            (*config).clone(),
            routes,
            1,
            &tls_clients,
            strategy,
            resolver.clone(),
        )),
        pool,
        revalidators: Arc::new(Semaphore::new(32)),
        logger,
        requests: Arc::new(Semaphore::new(config.limits.requests)),
        clients: Arc::new(Semaphore::new(config.limits.clients)),
        ready: AtomicBool::new(true),
        instance_id: uuid::Uuid::new_v4().to_string(),
        admin_token: std::env::var("CAUCE_ADMIN_TOKEN")
            .ok()
            .filter(|s| s.len() >= 32),
        tunnels: websocket::Tunnels::new(),
    });
    let mut health = health::start(&state.generation.read().unwrap());
    let admin_clients = Arc::new(Semaphore::new(16));
    let mut hup = signal(SignalKind::hangup())?;
    let mut term = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let (generation_tx, generation_rx) = watch::channel(1);
    let maintenance = (listener.is_some() || endpoint.is_some() || !extra_listeners.is_empty())
        .then(|| tokio::spawn(maintain_pool(state.pool.clone(), generation_rx)));
    let mut tasks = JoinSet::new();
    let mut stream = stream
        .map(|stream| stream.start(&shutdown_rx, &state.logger.metrics))
        .unwrap_or_default();
    if let Some(counters) = mail_counters {
        for (runtime, listener) in mail {
            stream.spawn(runtime.serve(listener, shutdown_rx.clone(), counters.clone()));
        }
    }
    let web = http_tcp::Runtime {
        config: config.clone(),
        state: state.clone(),
        alt_svc,
    };
    let explicit = if extra_listeners.is_empty() {
        None
    } else {
        let certificates = Arc::new(RwLock::new(extra_tls));
        let runtime = Arc::new(http_tcp::Runtime {
            config: config.clone(),
            state: state.clone(),
            alt_svc: web.alt_svc.clone(),
        });
        for (index, listener) in extra_listeners.into_iter().enumerate() {
            stream.spawn(runtime.clone().serve(
                listener,
                certificates.clone(),
                index,
                shutdown_rx.clone(),
            ));
        }
        Some(certificates)
    };
    if let Some(endpoint) = &endpoint {
        stream.spawn(http3::serve(
            endpoint.clone(),
            config.http3.as_deref().unwrap().clone(),
            config.clone(),
            state.clone(),
            shutdown_rx.clone(),
        ));
    }
    let mut stream_failure = Ok(());
    let mut reload = JoinSet::new();
    eprintln!(
        "{{\"event\":\"started\",\"data\":\"{}\",\"admin\":\"{}\"}}",
        config
            .listen
            .map(|address| address.to_string())
            .unwrap_or_default(),
        config.admin
    );
    loop {
        tokio::select! {
            _ = term.recv() => break,
            _ = interrupt.recv() => break,
            ended = stream.join_next(), if !stream.is_empty() => {
                stream_failure = match ended {
                    Some(Ok(Err(error))) => Err(error.context("stream listener failed")),
                    Some(Err(error)) => Err(error.into()),
                    _ => Err(anyhow::anyhow!("stream listener ended before shutdown")),
                };
                break;
            },
            _ = tasks.join_next(), if !tasks.is_empty() => {},
            _ = hup.recv(), if reload.is_empty() => {
                let version = state.generation.read().unwrap().version + 1;
                reload.spawn(prepare_reload(path.clone(), config.clone(), version, strategy, resolver.clone()));
            },
            prepared = reload.join_next(), if !reload.is_empty() => {
                match prepared {
                    Some(Ok(Ok(Prepared { generation: next, legacy_tls, explicit_tls, quic }))) => {
                        let version = next.version;
                        {
                            let mut current = state.generation.write().unwrap();
                            current.draining.store(true, Ordering::Release);
                            for cache in current.caches.values() { cache.drain(); }
                            *current = next;
                        }
                        if let Some(health) = health.take() {
                            health.abort();
                        }
                        health = health::start(&state.generation.read().unwrap());
                        server_tls = legacy_tls;
                        if let Some(certificates) = &explicit {
                            *certificates.write().unwrap() = explicit_tls;
                        }
                        if let (Some(endpoint), Some(quic)) = (&endpoint, quic) {
                            endpoint.set_server_config(Some(quic));
                        }
                        generation_tx.send_replace(version);
                        eprintln!("{{\"event\":\"reloaded\",\"config_version\":{version}}}");
                    },
                    _ => eprintln!("{{\"event\":\"reload_rejected\",\"reason\":\"invalid_or_restart_required\"}}"),
                }
            },
            accepted = async { listener.as_ref().expect("HTTP listener enabled").accept().await }, if listener.is_some() => {
                let (socket, peer) = accepted?;
                web.accept(&mut tasks, (socket, peer), server_tls.clone(), &shutdown_rx);
            },
            accepted = admin_listener.accept() => {
                let (socket, _) = accepted?;
                if tasks.len() >= config.limits.clients + 16 { drop(socket); continue; }
                let slot = match admin_clients.clone().try_acquire_owned() { Ok(p) => p, Err(_) => { drop(socket); continue; }};
                let state = state.clone(); let mut shutdown = shutdown_rx.clone();
                tasks.spawn(async move {
                    let _slot = slot;
                    let service = service_fn(move |req| { let response = proxy::admin(req, &state); async move { Ok::<_, Infallible>(response) } });
                    let mut builder = http1::Builder::new();
                    builder.keep_alive(false).timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_secs(2)).max_headers(32).max_buf_size(8192);
                    let conn = builder.serve_connection(TokioIo::new(socket), service);
                    tokio::select! { _ = conn => {}, _ = shutdown.changed() => {} }
                });
            }
        }
    }
    reload.abort_all();
    if let Some(maintenance) = maintenance {
        maintenance.abort();
    }
    state.ready.store(false, Ordering::Release);
    state
        .generation
        .read()
        .unwrap()
        .draining
        .store(true, Ordering::Release);
    if let Some(health) = health {
        health.abort();
        let _ = health.await;
    }
    let _ = shutdown_tx.send(true);
    state.tunnels.close();
    let deadline = tokio::time::Instant::now() + Duration::from_millis(config.limits.shutdown_ms);
    state.tunnels.wait_until(deadline).await;
    let _ = tokio::time::timeout_at(deadline, async {
        tokio::join!(
            async { while tasks.join_next().await.is_some() {} },
            async { while stream.join_next().await.is_some() {} },
        );
    })
    .await;
    if let Some(endpoint) = endpoint {
        endpoint.close(0u32.into(), b"gateway shutdown deadline");
    }
    stream.shutdown().await;
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    {
        let generation = state.generation.read().unwrap();
        generation.draining.store(true, Ordering::Release);
        for cache in generation.caches.values() {
            cache.drain();
        }
    }
    state.pool.sweep(u64::MAX);
    drop(web);
    drop(state);
    if let Some(worker) = log_thread {
        let limit = tokio::time::Instant::now() + Duration::from_millis(500);
        while !worker.is_finished() && tokio::time::Instant::now() < limit {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        if worker.is_finished() {
            let _ = worker.join();
        }
    }
    stream_failure
}

async fn maintain_pool(pool: Arc<Pool>, mut generations: watch::Receiver<u64>) {
    let mut interval = tokio::time::interval(Duration::from_millis(250));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _ = interval.tick() => {},
            changed = generations.changed() => {
                if changed.is_err() {
                    break;
                }
            }
        }
        let generation = *generations.borrow_and_update();
        let pool = pool.clone();
        if tokio::task::spawn_blocking(move || pool.sweep(generation))
            .await
            .is_err()
        {
            break;
        }
    }
}

struct Prepared {
    generation: Arc<Generation>,
    legacy_tls: Option<tokio_rustls::TlsAcceptor>,
    explicit_tls: Vec<Option<tokio_rustls::TlsAcceptor>>,
    quic: Option<quinn::ServerConfig>,
}

async fn prepare_explicit_listeners(
    config: &Config,
) -> Result<Vec<Option<tokio_rustls::TlsAcceptor>>> {
    if config.listeners.is_empty() {
        return Ok(Vec::new());
    }
    let tls: Vec<_> = config
        .listeners
        .iter()
        .map(|listener| listener.tls.clone())
        .collect();
    let h2 = config.http2.enabled;
    tokio::task::spawn_blocking(move || {
        tls.iter()
            .map(|config| {
                tls::load_server(config.as_ref(), h2)
                    .map(|server| server.map(tokio_rustls::TlsAcceptor::from))
            })
            .collect()
    })
    .await?
}

async fn prepare_reload(
    path: PathBuf,
    baseline: Arc<Config>,
    version: u64,
    strategy: PoolStrategy,
    resolver: Option<Arc<dns::Resolver>>,
) -> Result<Prepared> {
    let (candidate, routes) = Config::load(&path).await?;
    baseline.reload_compatible(&candidate)?;
    let clients = prepare_tls_clients(&candidate).await?;
    let legacy_tls = prepare_server_tls(candidate.tls.clone(), candidate.http2.enabled).await?;
    let explicit_tls = prepare_explicit_listeners(&candidate).await?;
    let quic = http3::prepare(&candidate).await?;
    let generation = tokio::task::spawn_blocking(move || {
        Generation::with_strategy(candidate, routes, version, &clients, strategy, resolver)
    })
    .await?;
    Ok(Prepared {
        generation,
        legacy_tls,
        explicit_tls,
        quic,
    })
}

async fn prepare_tls_clients(
    config: &Config,
) -> Result<HashMap<String, Arc<rustls::ClientConfig>>> {
    let upstreams: Vec<_> = config
        .upstreams
        .iter()
        .filter_map(|u| u.tls.clone().map(|t| (u.id.clone(), t, u.protocol)))
        .collect();
    tokio::task::spawn_blocking(move || {
        upstreams
            .into_iter()
            .map(|(id, tls_config, protocol)| Ok((id, tls::load_client(&tls_config, protocol)?)))
            .collect()
    })
    .await?
}

async fn prepare_server_tls(
    config: Option<config::InboundTls>,
    http2: bool,
) -> Result<Option<tokio_rustls::TlsAcceptor>> {
    tokio::task::spawn_blocking(move || {
        tls::load_server(config.as_ref(), http2)
            .map(|server| server.map(tokio_rustls::TlsAcceptor::from))
    })
    .await?
}
