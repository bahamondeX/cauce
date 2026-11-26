//! HTTP/1.1 and HTTP/2 TCP transport, shared by legacy and explicit listeners.
use crate::{
    config::{Config, Http2Config, InboundTls, Limits},
    diagnostics,
    proxy::{self, State},
    transport, ClientIo,
};
use anyhow::{Context, Result};
use hyper::{header::HeaderValue, service::service_fn};
use hyper_util::{rt::TokioIo, server::conn::auto};
use serde::{Deserialize, Serialize};
use std::{
    convert::Infallible,
    net::{IpAddr, SocketAddr},
    sync::{atomic::Ordering, Arc, RwLock},
    time::Duration,
};
use tokio::{
    net::{TcpListener, TcpStream},
    sync::{watch, OwnedSemaphorePermit},
    task::JoinSet,
};
use tokio_rustls::TlsAcceptor;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub(crate) listen: SocketAddr,
    pub(crate) tls: Option<InboundTls>,
}

pub(crate) struct Runtime {
    pub(crate) config: Arc<Config>,
    pub(crate) state: Arc<State>,
    pub(crate) alt_svc: Option<HeaderValue>,
}

impl Runtime {
    pub(crate) async fn serve(
        self: Arc<Self>,
        listener: TcpListener,
        certificates: Arc<RwLock<Vec<Option<TlsAcceptor>>>>,
        index: usize,
        mut shutdown: watch::Receiver<bool>,
    ) -> Result<()> {
        let mut tasks = JoinSet::new();
        loop {
            tokio::select! {
                biased;
                _ = shutdown.changed() => break,
                _ = tasks.join_next(), if !tasks.is_empty() => {},
                socket = listener.accept() => {
                    let socket = socket.context("HTTP TCP listener accept")?;
                    let acceptor = certificates.read().unwrap()[index].clone();
                    self.accept(&mut tasks, socket, acceptor, &shutdown);
                }
            }
        }
        drop(listener);
        while tasks.join_next().await.is_some() {}
        Ok(())
    }

    pub(crate) fn accept(
        &self,
        tasks: &mut JoinSet<()>,
        (socket, peer): (TcpStream, SocketAddr),
        server_tls: Option<TlsAcceptor>,
        shutdown_rx: &watch::Receiver<bool>,
    ) {
        let state = &self.state;
        let clients = &state.clients;
        let downstream = diagnostics::downstream_id();
        diagnostics::emit("downstream_accept", || {
            serde_json::json!({
                "connection": downstream, "peer_port": peer.port(), "tasks": tasks.len(),
                "client_permits_available": clients.available_permits(),
            })
        });
        if tasks.len() >= self.config.limits.clients + 16 {
            state
                .logger
                .metrics
                .rejected
                .fetch_add(1, Ordering::Relaxed);
            diagnostics::emit("downstream_rejected", || {
                serde_json::json!({
                    "connection": downstream, "tasks": tasks.len(),
                    "client_permits_available": clients.available_permits(),
                })
            });
            drop(socket);
            return;
        }
        let slot = match clients.clone().try_acquire_owned() {
            Ok(p) => Arc::new(p),
            Err(_) => {
                state
                    .logger
                    .metrics
                    .rejected
                    .fetch_add(1, Ordering::Relaxed);
                diagnostics::emit("downstream_rejected", || {
                    serde_json::json!({
                        "connection": downstream, "tasks": tasks.len(),
                        "client_permits_available": clients.available_permits(),
                    })
                });
                drop(socket);
                return;
            }
        };
        let peer_slot = if let Some(gate) = &state.peers {
            let Some(slot) = gate.enter(peer.ip()) else {
                state
                    .logger
                    .metrics
                    .rejected
                    .fetch_add(1, Ordering::Relaxed);
                return;
            };
            Some(slot)
        } else {
            None
        };
        if socket.set_nodelay(true).is_err() {
            return;
        }
        let state = state.clone();
        let shutdown = shutdown_rx.clone();
        let l = self.config.limits.clone();
        let h2 = self.config.http2.clone();
        let acceptor = server_tls;
        let alt_svc = self
            .alt_svc
            .as_ref()
            .filter(|_| acceptor.is_some())
            .cloned();
        let mut lifecycle = diagnostics::Task::new("downstream", downstream);
        tasks.spawn(async move {
            let _peer_slot = peer_slot;
            let session_slot = slot;
            let secure = acceptor.is_some();
            let mut negotiated_h2 = false;
            let socket: Box<dyn ClientIo> = if let Some(acceptor) = acceptor {
                match tokio::time::timeout(
                    Duration::from_millis(l.tls_handshake_ms),
                    acceptor.accept(socket),
                )
                .await
                {
                    Ok(Ok(stream)) => {
                        negotiated_h2 = stream.get_ref().1.alpn_protocol() == Some(b"h2");
                        Box::new(transport::WriteTimeout::new(
                            stream,
                            Duration::from_millis(l.stream_idle_ms),
                        ))
                    }
                    error => {
                        if let Some(task) = &mut lifecycle {
                            match error {
                                Ok(Err(error)) => task.finish("tls_error", Some(&error)),
                                Err(error) => task.finish("tls_timeout", Some(&error)),
                                Ok(Ok(_)) => unreachable!(),
                            }
                        }
                        return;
                    }
                }
            } else {
                Box::new(transport::WriteTimeout::new(
                    socket,
                    Duration::from_millis(l.stream_idle_ms),
                ))
            };
            let client = Client {
                peer: peer.ip(),
                state,
                slot: session_slot,
                secure,
                negotiated_h2,
                downstream,
                lifecycle,
                shutdown,
            };
            if alt_svc.is_some() {
                client.serve::<true>(socket, l, h2, alt_svc).await;
            } else {
                client.serve::<false>(socket, l, h2, None).await;
            }
        });
    }
}

struct Client {
    peer: IpAddr,
    state: Arc<State>,
    slot: Arc<OwnedSemaphorePermit>,
    secure: bool,
    negotiated_h2: bool,
    downstream: Option<u64>,
    lifecycle: Option<diagnostics::Task>,
    shutdown: watch::Receiver<bool>,
}

impl Client {
    async fn serve<const ADVERTISE: bool>(
        self,
        socket: Box<dyn ClientIo>,
        l: Limits,
        h2: Http2Config,
        alt_svc: Option<HeaderValue>,
    ) {
        let Self {
            peer,
            state,
            slot,
            secure,
            negotiated_h2,
            downstream,
            mut lifecycle,
            mut shutdown,
        } = self;
        let metrics = state.logger.metrics.clone();
        let forwarded_for = hyper::header::HeaderValue::from_str(&peer.to_string()).unwrap();
        let service = service_fn(move |req| {
            let response = proxy::handle(
                req,
                peer,
                forwarded_for.clone(),
                state.clone(),
                secure,
                slot.clone(),
                downstream,
            );
            let alt_svc = ADVERTISE.then(|| alt_svc.as_ref().unwrap().clone());
            async move {
                let mut response = response.await?;
                if ADVERTISE {
                    response
                        .headers_mut()
                        .insert(hyper::header::ALT_SVC, alt_svc.unwrap());
                }
                Ok::<_, Infallible>(response)
            }
        });
        let (socket, mut headers) = transport::PrefaceTimeout::new(
            diagnostics::Io::new(socket, "downstream", downstream),
            Duration::from_millis(l.client_headers_ms),
        );
        let (executor, _stream_guard) =
            transport::StreamExecutor::new(Duration::from_millis(l.request_ms));
        let mut builder =
            auto::Builder::new(executor.with_active_counter(metrics.http2_tasks_active.clone()));
        builder
            .http1()
            .timer(hyper_util::rt::TokioTimer::new())
            .header_read_timeout(Duration::from_millis(l.client_headers_ms))
            .max_headers(l.max_headers)
            .max_buf_size(l.max_header_bytes)
            .half_close(false);
        builder
            .http2()
            .timer(hyper_util::rt::TokioTimer::new())
            .max_concurrent_streams(h2.max_concurrent_streams)
            .initial_stream_window_size(h2.initial_stream_window_bytes)
            .initial_connection_window_size(h2.initial_connection_window_bytes)
            .max_send_buf_size(h2.max_send_buffer_bytes)
            .max_header_list_size(l.max_header_bytes as u32)
            .max_pending_accept_reset_streams(h2.max_pending_accept_reset_streams)
            .max_local_error_reset_streams(h2.max_local_error_reset_streams)
            .adaptive_window(false);
        if secure {
            builder = if negotiated_h2 {
                builder.http2_only()
            } else {
                builder.http1_only()
            };
        } else if !h2.enabled {
            builder = builder.http1_only();
        }
        let connection = builder.serve_connection_with_upgrades(TokioIo::new(socket), service);
        tokio::pin!(connection);
        let (reason, error) = tokio::select! {
            result = &mut connection => ("connection_end", result.err()),
            _ = headers.expired() => ("header_deadline", None),
            _ = shutdown.changed() => {
                connection.as_mut().graceful_shutdown();
                tokio::select! {
                    result = tokio::time::timeout(Duration::from_millis(l.shutdown_ms), &mut connection) => {
                        match result {
                            Ok(result) => ("shutdown", result.err()),
                            Err(_) => ("shutdown_deadline", None),
                        }
                    },
                    _ = headers.expired() => ("header_deadline", None),
                }
            }
        };
        if let Some(task) = &mut lifecycle {
            task.finish(reason, error.as_deref().map(|error| error as _));
        }
        if headers.http2_expired() {
            metrics
                .http2_header_timeouts
                .fetch_add(1, Ordering::Relaxed);
        }
    }
}
