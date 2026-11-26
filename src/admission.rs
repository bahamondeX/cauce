//! Bounded, reload-stable HTTP admission and socket-peer concurrency.
use bytes::Bytes;
use hyper::StatusCode;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    net::IpAddr,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::OwnedSemaphorePermit;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum KeyMode {
    ClientIp,
    Route,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Spec {
    pub key: KeyMode,
    pub max_keys: usize,
    pub rate_per_second: Option<u32>,
    #[serde(default)]
    pub burst: u32,
    pub max_delay_ms: Option<u64>,
    pub max_concurrent: Option<usize>,
    #[serde(default = "default_status")]
    pub reject_status: u16,
    pub error_body: Option<String>,
}

fn default_status() -> u16 {
    429
}

impl Spec {
    pub fn valid(&self) -> bool {
        (1..=65536).contains(&self.max_keys)
            && self
                .rate_per_second
                .is_none_or(|n| (1..=1_000_000).contains(&n))
            && self.max_concurrent.is_none_or(|n| (1..=65536).contains(&n))
            && (self.rate_per_second.is_some() || self.max_concurrent.is_some())
            && (self.rate_per_second.is_some() || (self.burst == 0 && self.max_delay_ms.is_none()))
            && self.burst <= 65536
            && self.max_delay_ms.is_none_or(|n| (1..=30_000).contains(&n))
            && (400..=599).contains(&self.reject_status)
            && self
                .error_body
                .as_ref()
                .is_none_or(|text| text.len() <= 4096)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Key {
    Client(IpAddr),
    Route([u8; 64]),
}

#[derive(Default)]
struct Entry {
    next: u64,
    active: usize,
}

#[derive(Default)]
struct State {
    entries: HashMap<Key, Entry>,
    sweep_after: u64,
}

pub(crate) struct Zone {
    spec: Spec,
    interval: u64,
    started: Instant,
    state: Mutex<State>,
    body: Option<Bytes>,
}

pub(crate) struct Admission {
    pub delay: Duration,
    pub guard: Option<Guard>,
}

pub(crate) struct Guard {
    zone: Arc<Zone>,
    key: Key,
}

impl Drop for Guard {
    fn drop(&mut self) {
        let mut state = self.zone.state.lock().unwrap();
        let entry = state
            .entries
            .get_mut(&self.key)
            .expect("active admission cannot be evicted");
        entry.active -= 1;
        if entry.active == 0 && self.zone.interval == 0 {
            state.entries.remove(&self.key);
        }
    }
}

impl Zone {
    pub fn new(spec: Spec) -> Self {
        let interval = spec
            .rate_per_second
            .map_or(0, |rate| 1_000_000_000u64.div_ceil(u64::from(rate)));
        let body = spec
            .error_body
            .as_ref()
            .map(|text| Bytes::copy_from_slice(text.as_bytes()));
        Self {
            spec,
            interval,
            started: Instant::now(),
            state: Mutex::new(State::default()),
            body,
        }
    }

    pub fn rejection(&self) -> (StatusCode, Option<Bytes>) {
        (
            StatusCode::from_u16(self.spec.reject_status).expect("validated rejection status"),
            self.body.clone(),
        )
    }

    pub fn admit(self: &Arc<Self>, ip: IpAddr, route: &str) -> Option<Admission> {
        let key = match self.spec.key {
            KeyMode::ClientIp => Key::Client(ip.to_canonical()),
            KeyMode::Route => {
                let mut key = [0; 64];
                key[..route.len()].copy_from_slice(route.as_bytes());
                Key::Route(key)
            }
        };
        let now = self.started.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64;
        self.admit_at(key, now)
    }

    fn admit_at(self: &Arc<Self>, key: Key, now: u64) -> Option<Admission> {
        let mut state = self.state.lock().unwrap();
        if state.entries.len() == self.spec.max_keys && !state.entries.contains_key(&key) {
            if now >= state.sweep_after {
                state
                    .entries
                    .retain(|_, entry| entry.active > 0 || entry.next > now);
                state.sweep_after = now.saturating_add(1_000_000_000);
            }
            if state.entries.len() == self.spec.max_keys {
                return None;
            }
        }
        let entry = state.entries.entry(key).or_default();
        if self
            .spec
            .max_concurrent
            .is_some_and(|max| entry.active >= max)
        {
            return None;
        }
        let debt = entry.next.saturating_sub(now);
        if debt > self.interval * u64::from(self.spec.burst) {
            return None;
        }
        let delay = match self.spec.max_delay_ms {
            Some(max_delay) if debt > max_delay * 1_000_000 => return None,
            Some(_) => Duration::from_nanos(debt),
            None => Duration::ZERO,
        };
        entry.next = entry.next.max(now).saturating_add(self.interval);
        let guard = self.spec.max_concurrent.map(|_| {
            entry.active += 1;
            Guard {
                zone: self.clone(),
                key,
            }
        });
        Some(Admission { delay, guard })
    }
}

#[derive(Clone)]
pub(crate) enum RequestSlot {
    Global { _slot: Arc<OwnedSemaphorePermit> },
    Scoped { _slot: Arc<ScopedSlot> },
}

pub(crate) struct ScopedSlot {
    _global: OwnedSemaphorePermit,
    _guard: Guard,
}

impl RequestSlot {
    pub fn new(global: OwnedSemaphorePermit, guard: Option<Guard>) -> Self {
        match guard {
            Some(guard) => Self::Scoped {
                _slot: Arc::new(ScopedSlot {
                    _global: global,
                    _guard: guard,
                }),
            },
            None => Self::Global {
                _slot: Arc::new(global),
            },
        }
    }
}

pub(crate) struct PeerGate {
    limit: usize,
    active: Mutex<HashMap<IpAddr, usize>>,
}

pub(crate) struct PeerSlot {
    gate: Arc<PeerGate>,
    ip: IpAddr,
}

impl PeerGate {
    pub fn new(limit: usize) -> Self {
        Self {
            limit,
            active: Mutex::new(HashMap::new()),
        }
    }

    pub fn enter(self: &Arc<Self>, ip: IpAddr) -> Option<PeerSlot> {
        let ip = ip.to_canonical();
        let mut active = self.active.lock().unwrap();
        let count = active.entry(ip).or_default();
        if *count == self.limit {
            return None;
        }
        *count += 1;
        Some(PeerSlot {
            gate: self.clone(),
            ip,
        })
    }
}

impl Drop for PeerSlot {
    fn drop(&mut self) {
        let mut active = self.gate.active.lock().unwrap();
        let count = active.get_mut(&self.ip).expect("live peer admission");
        *count -= 1;
        if *count == 0 {
            active.remove(&self.ip);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        alloc::{GlobalAlloc, Layout, System},
        cell::Cell,
    };

    thread_local! {
        static ALLOCATIONS: Cell<Option<(usize, usize)>> = const { Cell::new(None) };
    }

    struct Counter;

    #[global_allocator]
    static ALLOCATOR: Counter = Counter;

    unsafe impl GlobalAlloc for Counter {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let ptr = unsafe { System.alloc(layout) };
            if !ptr.is_null() {
                let _ = ALLOCATIONS.try_with(|count| {
                    if let Some((calls, bytes)) = count.get() {
                        count.set(Some((calls + 1, bytes + layout.size())));
                    }
                });
            }
            ptr
        }

        unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
            unsafe { System.dealloc(ptr, layout) }
        }
    }

    #[test]
    fn disabled_admission_reuses_the_baseline_permit_allocation() {
        let slots = Arc::new(tokio::sync::Semaphore::new(2));
        let prior = slots.clone().try_acquire_owned().unwrap();
        let current = slots.clone().try_acquire_owned().unwrap();
        ALLOCATIONS.with(|count| count.set(Some((0, 0))));
        let prior = std::hint::black_box(Arc::new(prior));
        let baseline = ALLOCATIONS.with(|count| count.replace(Some((0, 0))).unwrap());
        let current = std::hint::black_box(RequestSlot::new(current, None));
        let candidate = ALLOCATIONS.with(|count| count.replace(None).unwrap());
        drop((prior, current));
        assert_eq!(baseline.0, 1);
        assert_eq!(candidate, baseline);
        println!("request_permit baseline_allocations={} baseline_bytes={} disabled_allocations={} disabled_bytes={}",
                 baseline.0, baseline.1, candidate.0, candidate.1);
    }

    #[test]
    fn capture_rewrite_allocation_sample() {
        let action: crate::actions::Action = serde_json::from_str(
            r#"{"kind":"rewrite","pattern":"^/echo/(.*)$","target":"/internal/$1"}"#,
        )
        .unwrap();
        let path = "/echo/capture-that-needs-more-than-one-string-allocation";
        let _ = action.apply(path, Some("q=value")).unwrap();
        ALLOCATIONS.with(|count| count.set(Some((0, 0))));
        for _ in 0..100 {
            let output = std::hint::black_box(action.apply(path, Some("q=value")).unwrap());
            let Some(crate::actions::Outcome::Rewrite(uri)) = output else {
                panic!("expected rewrite");
            };
            assert_eq!(
                uri.as_str(),
                "/internal/capture-that-needs-more-than-one-string-allocation?q=value"
            );
        }
        let (calls, bytes) = ALLOCATIONS.with(|count| count.replace(None).unwrap());
        println!("capture_rewrite iterations=100 allocations={calls} allocated_bytes={bytes}");
    }

    #[test]
    fn burst_delay_and_live_entry_budget_fail_closed() {
        let zone = Arc::new(Zone::new(Spec {
            key: KeyMode::ClientIp,
            max_keys: 1,
            rate_per_second: Some(10),
            burst: 1,
            max_delay_ms: Some(100),
            max_concurrent: Some(2),
            reject_status: 429,
            error_body: None,
        }));
        let key = Key::Client("192.0.2.1".parse().unwrap());
        let other = Key::Client("192.0.2.2".parse().unwrap());
        let first = zone.admit_at(key, 0).unwrap();
        let second = zone.admit_at(key, 0).unwrap();
        assert_eq!(first.delay, Duration::ZERO);
        assert_eq!(second.delay, Duration::from_millis(100));
        assert!(zone.admit_at(key, 0).is_none());
        assert!(zone.admit_at(other, 0).is_none());
        assert!(zone.admit_at(other, 1_000_000_000).is_none());
        drop((first, second));
        assert!(zone.admit_at(other, 2_000_000_000).is_some());
    }

    #[test]
    fn peer_cap_uses_canonical_address_and_recovers_on_drop() {
        let gate = Arc::new(PeerGate::new(1));
        let ipv4 = "192.0.2.1".parse().unwrap();
        let slot = gate.enter(ipv4).unwrap();
        assert!(gate.enter("::ffff:192.0.2.1".parse().unwrap()).is_none());
        drop(slot);
        assert!(gate.enter(ipv4).is_some());
    }
}
