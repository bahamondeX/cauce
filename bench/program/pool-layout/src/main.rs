//! Isolate the idle-store synchronization from HTTP, sockets and Tokio scheduling.
mod quota;

use quota::Quota;
use std::{
    env,
    sync::{Barrier, Mutex},
    thread,
    time::Instant,
};

type Bucket = Mutex<Vec<u64>>;

struct Endpoints {
    total: Quota,
    buckets: Vec<Bucket>,
}

struct HybridEndpoint {
    quota: Quota,
    local: Vec<Bucket>,
    shared: Bucket,
}

struct Hybrid {
    total: Quota,
    buckets: Vec<HybridEndpoint>,
}

enum Store {
    Global(Mutex<Vec<Vec<u64>>>),
    Sharded(Endpoints),
    Hybrid(Hybrid),
}

struct Stats {
    reused: usize,
    opened: usize,
    discarded: usize,
    cross: usize,
    denied: usize,
    wait_ns: Vec<u64>,
    hold_ns: Vec<u64>,
}

impl Stats {
    fn bucket<T, R>(
        &mut self,
        lock: &Mutex<T>,
        sample: bool,
        operation: impl FnOnce(&mut T) -> R,
    ) -> R {
        let queued = sample.then(Instant::now);
        let mut data = lock.lock().unwrap();
        let acquired = sample.then(Instant::now);
        let result = operation(&mut data);
        let finished = sample.then(Instant::now);
        drop(data);
        if let (Some(queued), Some(acquired), Some(finished)) = (queued, acquired, finished) {
            self.wait_ns.push((acquired - queued).as_nanos() as u64);
            self.hold_ns.push((finished - acquired).as_nanos() as u64);
        }
        result
    }
}

impl Store {
    fn new(
        kind: &str,
        workers: usize,
        endpoints: usize,
        limit: usize,
        endpoint_limit: usize,
    ) -> Self {
        match kind {
            "A" => Self::Global(Mutex::new((0..endpoints).map(|_| Vec::new()).collect())),
            "B" => Self::Sharded(Endpoints {
                total: Quota::new(limit),
                buckets: (0..endpoints).map(|_| Mutex::default()).collect(),
            }),
            "C" => Self::Hybrid(Hybrid {
                total: Quota::new(limit),
                buckets: (0..endpoints)
                    .map(|_| HybridEndpoint {
                        quota: Quota::new(endpoint_limit),
                        local: (0..workers).map(|_| Mutex::default()).collect(),
                        shared: Mutex::default(),
                    })
                    .collect(),
            }),
            _ => panic!("use pool variant A, B or C"),
        }
    }

    fn take(
        &self,
        endpoint: usize,
        worker: usize,
        peer: usize,
        stats: &mut Stats,
        sample: bool,
    ) -> Option<u64> {
        match self {
            Self::Global(lock) => stats.bucket(lock, sample, |lists| lists[endpoint].pop()),
            Self::Sharded(pool) => stats.bucket(&pool.buckets[endpoint], sample, |list| {
                let item = list.pop()?;
                pool.total.release();
                Some(item)
            }),
            Self::Hybrid(pool) => {
                let bucket = &pool.buckets[endpoint];
                let mut item = stats
                    .bucket(&bucket.local[worker], sample, Vec::pop)
                    .or_else(|| stats.bucket(&bucket.shared, sample, Vec::pop));
                if item.is_none() && worker != peer {
                    item = stats.bucket(&bucket.local[peer], sample, Vec::pop);
                    stats.cross += usize::from(item.is_some());
                }
                if item.is_some() {
                    bucket.quota.release();
                    pool.total.release();
                }
                item
            }
        }
    }

    fn put(
        &self,
        endpoint: usize,
        worker: usize,
        item: u64,
        limits: (usize, usize),
        stats: &mut Stats,
        sample: bool,
    ) -> bool {
        let (limit, endpoint_limit) = limits;
        match self {
            Self::Global(lock) => stats.bucket(lock, sample, |lists| {
                let count: usize = lists.iter().map(Vec::len).sum();
                if count == limit || lists[endpoint].len() == endpoint_limit {
                    return false;
                }
                lists[endpoint].push(item);
                true
            }),
            Self::Sharded(pool) => stats.bucket(&pool.buckets[endpoint], sample, |list| {
                if list.len() == endpoint_limit || !pool.total.try_take() {
                    return false;
                }
                list.push(item);
                true
            }),
            Self::Hybrid(pool) => {
                let bucket = &pool.buckets[endpoint];
                if !bucket.quota.try_take() {
                    return false;
                }
                if !pool.total.try_take() {
                    bucket.quota.release();
                    return false;
                }
                if !stats.bucket(&bucket.local[worker], sample, |list| {
                    if !list.is_empty() {
                        return false;
                    }
                    list.push(item);
                    true
                }) {
                    stats.bucket(&bucket.shared, sample, |list| list.push(item));
                }
                true
            }
        }
    }

    fn counts(&self, idx: usize) -> usize {
        match self {
            Self::Global(lock) => lock.lock().unwrap()[idx].len(),
            Self::Sharded(pool) => pool.buckets[idx].lock().unwrap().len(),
            Self::Hybrid(pool) => {
                let endpoint = &pool.buckets[idx];
                endpoint.shared.lock().unwrap().len()
                    + endpoint
                        .local
                        .iter()
                        .map(|l| l.lock().unwrap().len())
                        .sum::<usize>()
            }
        }
    }
}

fn percentile(samples: &mut [u64], p: f64) -> u64 {
    samples.sort_unstable();
    samples[(samples.len() as f64 * p).ceil() as usize - 1]
}

fn main() {
    let args = env::args().collect::<Vec<_>>();
    let [_, kind, workers, endpoints, ops, mode] = args.as_slice() else {
        panic!(
            "usage: cauce-pool-layout A|B|C WORKERS ENDPOINTS OPERATIONS_PER_WORKER stable|churn"
        );
    };
    let (workers, endpoints, ops): (usize, usize, usize) = (
        workers.parse().unwrap(),
        endpoints.parse().unwrap(),
        ops.parse().unwrap(),
    );
    assert!(workers > 0 && endpoints > 0 && ops >= 128);
    assert!(matches!(mode.as_str(), "stable" | "churn"));
    let churn = mode == "churn";
    let limits = (32 * endpoints, 32);
    let store = Store::new(kind, workers, endpoints, limits.0, limits.1);
    let start = Barrier::new(workers + 1);
    let (elapsed, all) = thread::scope(|scope| {
        let handles = (0..workers)
            .map(|worker| {
                let (store, start) = (&store, &start);
                scope.spawn(move || {
                    let mut stats = Stats {
                        reused: 0,
                        opened: 0,
                        discarded: 0,
                        cross: 0,
                        denied: 0,
                        wait_ns: Vec::with_capacity(3 * ops / 128 + 3),
                        hold_ns: Vec::with_capacity(3 * ops / 128 + 3),
                    };
                    start.wait();
                    for i in 0..ops {
                        let sample = i % 128 == 0;
                        let endpoint = (i + worker) % endpoints;
                        let peer = (worker + 1 + i % workers) % workers;
                        let item = store.take(endpoint, worker, peer, &mut stats, sample);
                        stats.reused += usize::from(item.is_some());
                        if churn && i % 97 == 0 && item.is_some() {
                            stats.discarded += 1;
                            continue;
                        }
                        let item = item.unwrap_or_else(|| {
                            stats.opened += 1;
                            (worker * ops + i) as u64
                        });
                        if !store.put(endpoint, worker, item, limits, &mut stats, sample) {
                            stats.denied += 1;
                        }
                    }
                    stats
                })
            })
            .collect::<Vec<_>>();
        let began = Instant::now();
        start.wait();
        let stats = handles
            .into_iter()
            .map(|h| h.join().unwrap())
            .collect::<Vec<_>>();
        (began.elapsed(), stats)
    });
    let idle = (0..endpoints)
        .map(|endpoint| {
            let count = store.counts(endpoint);
            assert!(count <= limits.1);
            count
        })
        .sum::<usize>();
    assert!(idle <= limits.0);
    let opened: usize = all.iter().map(|s| s.opened).sum();
    let discarded: usize = all.iter().map(|s| s.discarded).sum();
    let denied: usize = all.iter().map(|s| s.denied).sum();
    let reused: usize = all.iter().map(|s| s.reused).sum();
    assert_eq!(opened - discarded - denied, idle);
    assert_eq!(opened + reused, ops * workers);
    let cross: usize = all.iter().map(|s| s.cross).sum();
    let fairness_min = all.iter().map(|s| s.reused).min().unwrap();
    let fairness_max = all.iter().map(|s| s.reused).max().unwrap();
    let mut wait = all
        .iter()
        .flat_map(|s| s.wait_ns.iter().copied())
        .collect::<Vec<_>>();
    let mut hold = all
        .iter()
        .flat_map(|s| s.hold_ns.iter().copied())
        .collect::<Vec<_>>();
    println!(concat!("{{
        \"variant\":\"{}\", \"workers\":{}, \"endpoints\":{}, \"workload\":\"{}\",
        \"ops\":{}, \"elapsed_ns\":{}, \"reused\":{}, \"opened_tokens\":{}, \"idle\":{},
        \"dropped\":{}, \"denied\":{}, \"peer_reuse\":{}, \"worker_reuse_min\":{},
        \"worker_reuse_max\":{}, \"lock_samples\":{}, \"lock_wait_p99_ns\":{}, \"lock_hold_p99_ns\":{}
    }}"), kind, workers, endpoints, mode, ops * workers, elapsed.as_nanos(), reused, opened, idle,
        discarded, denied, cross, fairness_min, fairness_max, wait.len(),
        percentile(&mut wait, 0.99), percentile(&mut hold, 0.99));
}
