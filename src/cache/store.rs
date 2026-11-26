//! Route-local hard-bounded storage and cancellation-safe fill coordination.
use super::{Entry, Family, Key};
use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, Mutex},
};
use tokio::sync::watch;

pub(super) struct Store {
    inner: Mutex<Inner>,
    bytes: usize,
    entries: usize,
    fills: usize,
    waiters: usize,
}
#[derive(Default)]
struct Inner {
    entries: HashMap<Arc<Key>, (Arc<Entry>, usize)>,
    order: VecDeque<Arc<Key>>,
    flights: HashMap<Arc<Key>, watch::Sender<()>>,
    bytes: usize,
    epoch: u64,
    retired: bool,
}
pub(super) enum Selection {
    Hit(Arc<Entry>),
    Fill(Ticket, Option<Arc<Entry>>),
    Wait(watch::Receiver<()>),
    Unavailable,
}
impl Store {
    pub fn new(bytes: usize, entries: usize, fills: usize, waiters: usize) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::default(),
            bytes,
            entries,
            fills,
            waiters,
        })
    }
    pub fn select(
        self: &Arc<Self>,
        key: &Arc<Key>,
        decide: impl FnOnce(Option<&Entry>, bool) -> Read,
    ) -> Selection {
        let mut state = self.inner.lock().unwrap();
        if state.retired {
            return Selection::Unavailable;
        }
        let entry = state.entries.get(key).map(|(entry, _)| entry);
        let flight = state.flights.get(key);
        match decide(entry.map(AsRef::as_ref), flight.is_some()) {
            Read::Hit => return Selection::Hit(entry.expect("existing entry").clone()),
            Read::Bypass => return Selection::Unavailable,
            Read::Fill => {}
        }
        if let Some(flight) = flight {
            if flight.receiver_count() < self.waiters {
                return Selection::Wait(flight.subscribe());
            }
            return Selection::Unavailable;
        }
        if state.flights.len() == self.fills {
            return Selection::Unavailable;
        }
        let stale = entry.cloned();
        let (notify, _) = watch::channel(());
        state.flights.insert(key.clone(), notify);
        let ticket = Ticket {
            store: self.clone(),
            key: key.clone(),
            epoch: state.epoch,
        };
        Selection::Fill(ticket, stale)
    }
    pub fn drain(&self) {
        let retired = {
            let mut state = self.inner.lock().unwrap();
            std::mem::replace(
                &mut *state,
                Inner {
                    retired: true,
                    ..Inner::default()
                },
            )
        };
        drop(retired);
    }
    pub fn invalidate(&self, family: &Family) {
        let mut state = self.inner.lock().unwrap();
        state.epoch = state.epoch.wrapping_add(1);
        let mut removed = 0;
        state.entries.retain(|key, (_, weight)| {
            let evict = &key.family == family;
            if evict {
                removed += *weight;
            }
            !evict
        });
        state.bytes -= removed;
        state.order.retain(|key| &key.family != family);
    }
}
pub(super) enum Read {
    Hit,
    Bypass,
    Fill,
}

pub(super) struct Ticket {
    store: Arc<Store>,
    key: Arc<Key>,
    epoch: u64,
}
impl Ticket {
    pub fn publish(self, entry: Arc<Entry>) -> bool {
        let mut state = self.store.inner.lock().unwrap();
        let weight = self.key.bytes() + entry.bytes();
        if state.retired || state.epoch != self.epoch || weight > self.store.bytes {
            return false;
        }
        if let Some((_, old_weight)) = state.entries.remove(&self.key) {
            state.bytes -= old_weight;
            state.order.retain(|key| key != &self.key);
        }
        while state.entries.len() >= self.store.entries || state.bytes > self.store.bytes - weight {
            let key = state.order.pop_front().expect("one eviction candidate");
            state.bytes -= state
                .entries
                .remove(&key)
                .expect("consistent storage order")
                .1;
        }
        state.bytes += weight;
        state.entries.insert(self.key.clone(), (entry, weight));
        state.order.push_back(self.key.clone());
        true
    }
    pub fn current(&self) -> bool {
        let state = self.store.inner.lock().unwrap();
        !state.retired && state.epoch == self.epoch
    }
    pub fn invalidate(&self) {
        self.store.invalidate(&self.key.family);
    }
}
impl Drop for Ticket {
    fn drop(&mut self) {
        // Removing the sender wakes all ready waiters, even if the fill was
        // cancelled before producing a response or the response was oversized.
        self.store.inner.lock().unwrap().flights.remove(&self.key);
    }
}
