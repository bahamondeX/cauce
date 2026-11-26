//! Admission and drain ownership for independently bound Stream and mail sockets.
use crate::{config::Config, dns, mail, stream, telemetry::Metrics};
use anyhow::{ensure, Result};
use std::{
    collections::{HashMap, HashSet},
    future::Future,
    net::SocketAddr,
    sync::Arc,
};
use tokio::{
    net::TcpListener,
    sync::watch,
    task::{AbortHandle, Id, JoinSet},
};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum Key {
    Tcp(SocketAddr),
    Udp(SocketAddr),
}

impl Key {
    fn overlaps(self, other: Self) -> bool {
        match (self, other) {
            (Self::Tcp(candidate), Self::Tcp(active))
            | (Self::Udp(candidate), Self::Udp(active)) => {
                crate::stream::config::add_address(&mut vec![active], candidate).is_err()
            }
            _ => false,
        }
    }
}

#[derive(PartialEq)]
enum Identity<'a> {
    Tcp(&'a stream::config::Tcp),
    Passthrough(&'a stream::config::Passthrough),
    Udp(&'a stream::config::Udp),
    Mail(&'a mail::config::Settings),
}

fn identities(config: &Config) -> HashMap<Key, Identity<'_>> {
    let mut descriptors = HashMap::new();
    if let Some(stream) = &config.stream {
        descriptors.extend(
            stream
                .tcp
                .iter()
                .map(|spec| (Key::Tcp(spec.listen), Identity::Tcp(spec))),
        );
        descriptors.extend(
            stream
                .tls_passthrough
                .iter()
                .map(|spec| (Key::Tcp(spec.listen), Identity::Passthrough(spec))),
        );
        descriptors.extend(
            stream
                .udp
                .iter()
                .map(|spec| (Key::Udp(spec.listen), Identity::Udp(spec))),
        );
    }
    descriptors.extend(
        config
            .mail
            .iter()
            .map(|spec| (Key::Tcp(spec.listen), Identity::Mail(spec))),
    );
    descriptors
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Command {
    Active,
    Retire,
    Shutdown,
}

#[derive(Default)]
pub(crate) struct Supervisor {
    active: HashMap<Key, (watch::Sender<Command>, AbortHandle)>,
    draining: HashMap<Id, (Key, watch::Sender<Command>)>,
}

impl Supervisor {
    pub(crate) fn start<F, T>(&mut self, key: Key, tasks: &mut JoinSet<Result<()>>, serve: F)
    where
        F: FnOnce(watch::Receiver<Command>) -> T,
        T: Future<Output = Result<()>> + Send + 'static,
    {
        let (stop, signal) = watch::channel(Command::Active);
        let handler = tasks.spawn(serve(signal));
        let previous = self.active.insert(key, (stop, handler));
        debug_assert!(previous.is_none());
    }

    fn retire_removed(&mut self, desired: &HashSet<Key>) {
        self.active.retain(|key, (stop, task)| {
            if desired.contains(key) {
                return true;
            }
            stop.send_replace(Command::Retire);
            self.draining.insert(task.id(), (*key, stop.clone()));
            false
        });
    }

    pub(crate) fn draining_keys(&self) -> Vec<Key> {
        self.draining.values().map(|(key, _)| *key).collect()
    }

    pub(crate) fn drained(&mut self, id: Id) -> bool {
        self.draining.remove(&id).is_some()
    }

    pub(crate) fn shutdown(&mut self) {
        for (_, stop) in self.draining.values() {
            stop.send_replace(Command::Shutdown);
        }
        for (_, (stop, _)) in self.active.drain() {
            stop.send_replace(Command::Shutdown);
        }
    }
}

pub(crate) struct Prepared {
    desired: HashSet<Key>,
    stream: Option<stream::Listeners>,
    mail: Vec<(mail::Listener, TcpListener)>,
}

impl Prepared {
    pub(crate) async fn reload(
        current: &Config,
        next: &Config,
        draining: &[Key],
        resolver: Option<Arc<dns::Resolver>>,
    ) -> Result<Self> {
        let active = identities(current);
        let candidates = identities(next);
        if active.len() != candidates.len()
            || active.keys().any(|key| !candidates.contains_key(key))
        {
            ensure!(
                draining.is_empty(),
                "wait for the retired listener generation before another topology change"
            );
        }

        for (key, candidate) in &candidates {
            if let Some(existing) = active.get(key) {
                ensure!(
                    existing == candidate,
                    "changes on a retained Stream or mail binding require restart"
                );
            } else {
                ensure!(
                    active
                        .keys()
                        .chain(draining)
                        .all(|active| !key.overlaps(*active)),
                    "new Stream or mail binding overlaps an active or draining socket"
                );
            }
        }
        if let (Some(old), Some(new)) = (&current.stream, &next.stream) {
            for group in &old.upstreams {
                if let Some(candidate) = new
                    .upstreams
                    .iter()
                    .find(|candidate| candidate.id == group.id)
                {
                    ensure!(
                        candidate == group,
                        "changes to existing Stream upstream groups require restart"
                    );
                }
            }
        }
        let mut stream = cfg!(feature = "stream")
            .then_some(next.stream.as_deref())
            .flatten()
            .cloned();
        if let Some(stream) = &mut stream {
            stream
                .tcp
                .retain(|spec| !active.contains_key(&Key::Tcp(spec.listen)));
            stream
                .tls_passthrough
                .retain(|spec| !active.contains_key(&Key::Tcp(spec.listen)));
            stream
                .udp
                .retain(|spec| !active.contains_key(&Key::Udp(spec.listen)));
        }
        let stream = match stream.filter(stream::config::StreamConfig::has_listeners) {
            Some(spec) => {
                let tls = stream::prepare_tls(Some(&spec)).await?;
                Some(stream::Listeners::bind(spec, resolver.clone(), tls)?)
            }
            None => None,
        };
        let added_mail: Vec<_> = next
            .mail
            .iter()
            .filter(|spec| cfg!(feature = "mail") && !active.contains_key(&Key::Tcp(spec.listen)))
            .cloned()
            .collect();
        let mail = mail::prepare(&added_mail, resolver)
            .await?
            .into_iter()
            .map(|listener| listener.bind().map(|socket| (listener, socket)))
            .collect::<Result<Vec<_>>>()?;
        Ok(Self {
            desired: candidates.keys().copied().collect(),
            stream,
            mail,
        })
    }

    pub(crate) fn publish(
        self,
        supervisor: &mut Supervisor,
        tasks: &mut JoinSet<Result<()>>,
        metrics: &Metrics,
    ) {
        supervisor.retire_removed(&self.desired);
        start(self.stream, self.mail, supervisor, tasks, metrics);
    }
}

pub(crate) fn start(
    stream: Option<stream::Listeners>,
    mail: Vec<(mail::Listener, TcpListener)>,
    supervisor: &mut Supervisor,
    tasks: &mut JoinSet<Result<()>>,
    metrics: &Metrics,
) {
    if let Some(stream) = stream {
        stream.start(supervisor, tasks, metrics);
    }
    for (listener, socket) in mail {
        let key = Key::Tcp(socket.local_addr().expect("bound mail socket"));
        supervisor.start(key, tasks, |stop| {
            listener.serve(
                socket,
                stop,
                metrics.mail.as_ref().expect("mail counters").clone(),
            )
        });
    }
}
