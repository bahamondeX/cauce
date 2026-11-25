mod body;
mod config;
mod health;
mod pool;
mod proxy;
mod routing;
mod telemetry;
mod tls;
mod transport;
mod websocket;

use anyhow::{Context, Result};
use config::Config;
use hyper::{server::conn::http1, service::service_fn};
use hyper_util::{rt::TokioIo, server::conn::auto};
use pool::{Generation, Pool};
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
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .max_blocking_threads(16)
        .build()?;
    let result = runtime.block_on(run());
    // The OS DNS resolver may outlive a cancelled lookup. Do not let it hold shutdown.
    runtime.shutdown_timeout(Duration::from_millis(500));
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
    let config = Arc::new(Config::load(&path).await?);
    let tls_clients = prepare_tls_clients(&config).await?;
    let mut server_tls = prepare_server_tls(config.tls.clone(), config.http2.enabled).await?;
    if check {
        println!("configuration valid");
        return Ok(());
    }
    let listener = TcpListener::bind(config.listen)
        .await
        .context("data listener")?;
    let admin_listener = TcpListener::bind(config.admin)
        .await
        .context("admin listener")?;
    let metrics = Arc::new(Metrics::default());
    let (logger, log_thread) = Logger::start(config.logs.clone(), metrics.clone());
    let pool = Pool::new(config.limits.clone(), config.http2.clone(), metrics);
    let clients = Arc::new(Semaphore::new(config.limits.clients));
    let state = Arc::new(State {
        generation: RwLock::new(Generation::new((*config).clone(), 1, &tls_clients)),
        pool,
        logger,
        requests: Arc::new(Semaphore::new(config.limits.requests)),
        clients: clients.clone(),
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
    let maintenance = tokio::spawn(maintain_pool(state.pool.clone(), generation_rx));
    let mut tasks = JoinSet::new();
    let mut reload = JoinSet::new();
    eprintln!(
        "{{\"event\":\"started\",\"data\":\"{}\",\"admin\":\"{}\"}}",
        config.listen, config.admin
    );
    loop {
        tokio::select! {
            _ = term.recv() => break,
            _ = interrupt.recv() => break,
            _ = tasks.join_next(), if !tasks.is_empty() => {},
            _ = hup.recv(), if reload.is_empty() => {
                let version = state.generation.read().unwrap().version + 1;
                reload.spawn(prepare_reload(path.clone(), config.clone(), version));
            },
            prepared = reload.join_next(), if !reload.is_empty() => {
                match prepared {
                    Some(Ok(Ok((next, server)))) => {
                        let version = next.version;
                        {
                            let mut current = state.generation.write().unwrap();
                            current.draining.store(true, Ordering::Release);
                            *current = next;
                        }
                        health.abort();
                        health = health::start(&state.generation.read().unwrap());
                        server_tls = server;
                        generation_tx.send_replace(version);
                        eprintln!("{{\"event\":\"reloaded\",\"config_version\":{version}}}");
                    },
                    _ => eprintln!("{{\"event\":\"reload_rejected\",\"reason\":\"invalid_or_restart_required\"}}"),
                }
            },
            accepted = listener.accept() => {
                let (socket, peer) = accepted?;
                if tasks.len() >= config.limits.clients + 16 {
                    state.logger.metrics.rejected.fetch_add(1, Ordering::Relaxed); drop(socket); continue;
                }
                let slot = match clients.clone().try_acquire_owned() { Ok(p) => Arc::new(p), Err(_) => {
                    state.logger.metrics.rejected.fetch_add(1, Ordering::Relaxed); drop(socket); continue;
                }};
                socket.set_nodelay(true)?;
                let state = state.clone(); let mut shutdown = shutdown_rx.clone(); let l = config.limits.clone();
                let h2 = config.http2.clone();
                let acceptor = server_tls.clone();
                tasks.spawn(async move {
                    let _slot = slot;
                    let secure = acceptor.is_some();
                    let mut negotiated_h2 = false;
                    let socket: Box<dyn ClientIo> = if let Some(acceptor) = acceptor {
                        match tokio::time::timeout(Duration::from_millis(l.tls_handshake_ms), acceptor.accept(socket)).await {
                            Ok(Ok(stream)) => {
                                negotiated_h2 = stream.get_ref().1.alpn_protocol() == Some(b"h2");
                                Box::new(transport::WriteTimeout::new(stream, Duration::from_millis(l.stream_idle_ms)))
                            },
                            _ => return,
                        }
                    } else { Box::new(transport::WriteTimeout::new(socket, Duration::from_millis(l.stream_idle_ms))) };
                    let metrics = state.logger.metrics.clone();
                    let service_slot = _slot.clone();
                    let service = service_fn(move |req| proxy::handle(req, peer, state.clone(), secure, service_slot.clone()));
                    let (socket, mut headers) = transport::PrefaceTimeout::new(socket, Duration::from_millis(l.client_headers_ms));
                    let (executor, _stream_guard) = transport::StreamExecutor::new(Duration::from_millis(l.request_ms));
                    let mut builder = auto::Builder::new(executor.with_active_counter(metrics.http2_tasks_active.clone()));
                    builder.http1().timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_millis(l.client_headers_ms))
                        .max_headers(l.max_headers).max_buf_size(l.max_header_bytes).half_close(false);
                    builder.http2().timer(hyper_util::rt::TokioTimer::new())
                        .max_concurrent_streams(h2.max_concurrent_streams)
                        .initial_stream_window_size(h2.initial_stream_window_bytes)
                        .initial_connection_window_size(h2.initial_connection_window_bytes)
                        .max_send_buf_size(h2.max_send_buffer_bytes)
                        .max_header_list_size(l.max_header_bytes as u32)
                        .max_pending_accept_reset_streams(h2.max_pending_accept_reset_streams)
                        .max_local_error_reset_streams(h2.max_local_error_reset_streams)
                        .adaptive_window(false);
                    if secure {
                        builder = if negotiated_h2 { builder.http2_only() } else { builder.http1_only() };
                    } else if !h2.enabled {
                        builder = builder.http1_only();
                    }
                    let connection = builder.serve_connection_with_upgrades(TokioIo::new(socket), service);
                    tokio::pin!(connection);
                    tokio::select! {
                        _ = &mut connection => {},
                        _ = headers.expired() => {},
                        _ = shutdown.changed() => {
                            connection.as_mut().graceful_shutdown();
                            tokio::select! {
                                _ = tokio::time::timeout(Duration::from_millis(l.shutdown_ms), &mut connection) => {},
                                _ = headers.expired() => {},
                            }
                        }
                    }
                    if headers.http2_expired() {
                        metrics.http2_header_timeouts.fetch_add(1, Ordering::Relaxed);
                    }
                });
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
    maintenance.abort();
    state.ready.store(false, Ordering::Release);
    state
        .generation
        .read()
        .unwrap()
        .draining
        .store(true, Ordering::Release);
    health.abort();
    let _ = health.await;
    let _ = shutdown_tx.send(true);
    state.tunnels.close();
    let deadline = tokio::time::Instant::now() + Duration::from_millis(config.limits.shutdown_ms);
    state.tunnels.wait_until(deadline).await;
    let _ = tokio::time::timeout_at(deadline, async {
        while tasks.join_next().await.is_some() {}
    })
    .await;
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    state.pool.sweep(u64::MAX);
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
    Ok(())
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

async fn prepare_reload(
    path: PathBuf,
    baseline: Arc<Config>,
    version: u64,
) -> Result<(Arc<Generation>, Option<tokio_rustls::TlsAcceptor>)> {
    let candidate = Config::load(&path).await?;
    baseline.reload_compatible(&candidate)?;
    let clients = prepare_tls_clients(&candidate).await?;
    let server = prepare_server_tls(candidate.tls.clone(), candidate.http2.enabled).await?;
    let generation =
        tokio::task::spawn_blocking(move || Generation::new(candidate, version, &clients)).await?;
    Ok((generation, server))
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
