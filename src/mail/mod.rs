//! TLS-gated SMTP, IMAP and POP3 negotiation and authenticated relaying.
mod auth;
pub(crate) mod config;
mod protocol;
mod wire;

use crate::{stream, tls, ClientIo};
use anyhow::{Context, Result};
use auth::{Decision, Service};
use config::{Settings, TlsMode};
use protocol::{authentication_reply, Attempt, Event, Negotiation};
use rustls::pki_types::ServerName;
use std::{
    collections::HashMap,
    net::SocketAddr,
    sync::{
        atomic::{AtomicU64, Ordering::Relaxed},
        Arc,
    },
    time::Duration,
};
use tokio::{
    io::BufReader,
    net::{TcpListener, TcpStream},
    sync::watch,
    task::JoinSet,
    time::{timeout_at, Instant},
};
use tokio_rustls::{TlsAcceptor, TlsConnector};
use wire::Wire;

#[derive(Default)]
pub(crate) struct Counters {
    pub(crate) accepted: AtomicU64,
    pub(crate) active: AtomicU64,
    pub(crate) rejected: AtomicU64,
    pub(crate) authenticated: AtomicU64,
    pub(crate) denied: AtomicU64,
    pub(crate) failures: AtomicU64,
}

struct Active(Arc<Counters>);

impl Drop for Active {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Relaxed);
    }
}

pub(crate) struct Listener {
    config: Settings,
    tls: TlsAcceptor,
    auth: Service,
    backends: HashMap<SocketAddr, Option<(TlsConnector, ServerName<'static>)>>,
}

pub(crate) async fn prepare(
    specs: &[Settings],
    resolver: Option<Arc<crate::dns::Resolver>>,
) -> Result<Vec<Listener>> {
    if specs.is_empty() {
        return Ok(Vec::new());
    }
    let specs = specs.to_owned();
    tokio::task::spawn_blocking(move || -> Result<_> {
        specs
            .into_iter()
            .map(|spec| Listener::prepare(spec, resolver.clone()))
            .collect()
    })
    .await
    .context("mail preparation task")?
}

impl Listener {
    fn prepare(config: Settings, resolver: Option<Arc<crate::dns::Resolver>>) -> Result<Self> {
        let tls = TlsAcceptor::from(tls::load_raw_server(&config.tls)?);
        let auth = Service::new(
            config.auth_url()?,
            Duration::from_millis(config.auth_timeout_ms),
            resolver,
        )?;
        let mut backends = HashMap::with_capacity(config.backends.len());
        for backend in &config.backends {
            let tls = backend
                .tls
                .as_ref()
                .map(|client| -> Result<_> {
                    let name = tls::server_name(client)?;
                    let connector = TlsConnector::from(tls::load_raw_client(client)?);
                    Ok((connector, name))
                })
                .transpose()?;
            backends.insert(backend.address, tls);
        }
        Ok(Self {
            config,
            tls,
            auth,
            backends,
        })
    }

    pub(crate) fn bind(&self) -> Result<TcpListener> {
        let socket = stream::socket(self.config.listen, socket2::Type::STREAM, false)
            .context("mail TCP bind")?;
        socket.listen(1024)?;
        Ok(TcpListener::from_std(socket.into())?)
    }

    pub(crate) async fn serve(
        self,
        listener: TcpListener,
        mut shutdown: watch::Receiver<bool>,
        counters: Arc<Counters>,
    ) -> Result<()> {
        let this = Arc::new(self);
        let mut tasks = JoinSet::new();
        loop {
            tokio::select! {
                biased;
                _ = shutdown.changed() => break,
                _ = tasks.join_next(), if !tasks.is_empty() => {},
                accepted = listener.accept() => {
                    let (client, peer) = accepted.context("mail accept")?;
                    if tasks.len() >= this.config.max_connections {
                        counters.rejected.fetch_add(1, Relaxed);
                        continue;
                    }
                    let service = this.clone();
                    let cancel = shutdown.clone();
                    counters.accepted.fetch_add(1, Relaxed);
                    counters.active.fetch_add(1, Relaxed);
                    let active = Active(counters.clone());
                    tasks.spawn(async move {
                        service.session(client, peer, cancel, active).await;
                    });
                }
            }
        }
        drop(listener);
        while tasks.join_next().await.is_some() {}
        Ok(())
    }

    async fn session(
        &self,
        client: TcpStream,
        peer: SocketAddr,
        mut shutdown: watch::Receiver<bool>,
        active: Active,
    ) {
        let start = Instant::now();
        let auth_deadline = start + Duration::from_millis(self.config.auth_timeout_ms);
        let session_deadline = start + Duration::from_millis(self.config.session_timeout_ms);
        let result = tokio::select! {
            biased;
            _ = shutdown.changed() => return,
            result = timeout_at(auth_deadline, self.authenticate(client, peer, &active.0)) => result,
        };
        match result {
            Ok(Ok(Some((client, origin)))) => {
                let _ = stream::tcp::relay(
                    client,
                    origin,
                    8192,
                    Duration::from_millis(self.config.idle_timeout_ms),
                    session_deadline,
                )
                .await;
            }
            Ok(Ok(None)) => {}
            Ok(Err(_)) | Err(_) => {
                active.0.failures.fetch_add(1, Relaxed);
            }
        }
    }

    async fn authenticate(
        &self,
        client: TcpStream,
        peer: SocketAddr,
        counters: &Counters,
    ) -> Result<Option<(Wire, Wire)>> {
        client.set_nodelay(true)?;
        let mut secure = self.config.tls_mode == TlsMode::Implicit;
        let io: Box<dyn ClientIo> = if secure {
            Box::new(self.tls.accept(client).await?)
        } else {
            Box::new(client)
        };
        let mut wire = BufReader::with_capacity(2048, io);
        let mut negotiation = Negotiation::new(self.config.protocol, &self.config.auth_mechanisms);
        negotiation.greeting(&mut wire).await?;
        let mut number = 0;
        loop {
            match negotiation.event(&mut wire, secure).await? {
                Event::Quit => return Ok(None),
                Event::Upgrade(reply) => {
                    // Any bytes read past the upgrade command belong to the insecure transcript.
                    anyhow::ensure!(wire.buffer().is_empty(), "pipelined mail TLS upgrade");
                    wire::send(&mut wire, &reply).await?;
                    let socket = self.tls.accept(wire.into_inner()).await?;
                    wire = BufReader::with_capacity(2048, Box::new(socket) as Box<dyn ClientIo>);
                    secure = true;
                    negotiation.upgraded();
                }
                Event::Authenticate(attempt) => {
                    debug_assert!(secure);
                    number += 1;
                    match self.login(peer, negotiation.helo(), number, &attempt).await {
                        Ok(Some(origin)) => {
                            authentication_reply(
                                self.config.protocol,
                                &mut wire,
                                &attempt,
                                Some(true),
                            )
                            .await?;
                            counters.authenticated.fetch_add(1, Relaxed);
                            return Ok(Some((wire, origin)));
                        }
                        Ok(None) => {
                            counters.denied.fetch_add(1, Relaxed);
                            authentication_reply(
                                self.config.protocol,
                                &mut wire,
                                &attempt,
                                Some(false),
                            )
                            .await?;
                            if number >= 3 {
                                return Ok(None);
                            }
                        }
                        Err(_) => {
                            authentication_reply(self.config.protocol, &mut wire, &attempt, None)
                                .await?;
                            anyhow::bail!("mail authentication dependency failed");
                        }
                    }
                }
            }
        }
    }

    async fn login(
        &self,
        peer: SocketAddr,
        helo: Option<&str>,
        number: u8,
        attempt: &Attempt,
    ) -> Result<Option<Wire>> {
        let Decision::Connect(address) = self
            .auth
            .check(
                self.config.protocol,
                peer.ip(),
                helo,
                number,
                &attempt.credentials,
            )
            .await?
        else {
            return Ok(None);
        };
        let tls = self
            .backends
            .get(&address)
            .context("mail backend outside allowlist")?;
        let socket = TcpStream::connect(address).await?;
        socket.set_nodelay(true)?;
        let io: Box<dyn ClientIo> = if let Some((connector, name)) = tls {
            Box::new(connector.connect(name.clone(), socket).await?)
        } else {
            Box::new(socket)
        };
        let mut origin = BufReader::with_capacity(2048, io);
        protocol::backend_login(self.config.protocol, &mut origin, &attempt.credentials).await?;
        Ok(Some(origin))
    }
}
