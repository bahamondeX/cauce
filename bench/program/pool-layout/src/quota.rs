#[cfg(test)]
use loom::sync::atomic::{AtomicUsize, Ordering};
#[cfg(not(test))]
use std::sync::atomic::{AtomicUsize, Ordering};

pub struct Quota {
    remaining: AtomicUsize,
}

impl Quota {
    pub fn new(capacity: usize) -> Self {
        Self {
            remaining: AtomicUsize::new(capacity),
        }
    }

    pub fn try_take(&self) -> bool {
        self.remaining
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |remaining| {
                remaining.checked_sub(1)
            })
            .is_ok()
    }

    pub fn release(&self) {
        self.remaining.fetch_add(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use loom::{sync::Arc, thread};

    #[test]
    fn cancelling_reservation_and_concurrent_returns_preserve_both_budgets() {
        loom::model(|| {
            let global = Arc::new(Quota::new(1));
            let endpoint = Arc::new(Quota::new(2));
            let proof = Arc::new(AtomicUsize::new(0));
            let spawn = || {
                let (global, endpoint, proof) = (global.clone(), endpoint.clone(), proof.clone());
                thread::spawn(move || {
                    assert!(endpoint.try_take());
                    if global.try_take() {
                        assert_eq!(proof.fetch_add(1, Ordering::SeqCst), 0);
                        thread::yield_now();
                        assert_eq!(proof.fetch_sub(1, Ordering::SeqCst), 1);
                        global.release();
                    }
                    endpoint.release();
                })
            };
            let a = spawn();
            let b = spawn();
            a.join().unwrap();
            b.join().unwrap();
            assert_eq!(global.remaining.load(Ordering::Relaxed), 1);
            assert_eq!(endpoint.remaining.load(Ordering::Relaxed), 2);
        });
    }
}
