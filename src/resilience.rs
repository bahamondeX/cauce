//! Passive endpoint ejection with a single cancellation-safe recovery probe.
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Settings {
    pub failures: u16,
    #[serde(deserialize_with = "crate::config::milliseconds")]
    pub max_backoff_ms: u64,
    pub eject_on_gateway_status: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            failures: 2,
            max_backoff_ms: 30_000,
            eject_on_gateway_status: false,
        }
    }
}

impl Settings {
    pub(crate) fn validate(&self, base_ms: u64) -> Result<()> {
        ensure!(
            (1..=100).contains(&self.failures)
                && (base_ms..=300_000).contains(&self.max_backoff_ms),
            "invalid upstream resilience threshold or backoff"
        );
        Ok(())
    }
}

#[derive(Default)]
struct State {
    epoch: u64,
    failures: u16,
    ejections: u32,
    next_attempt: Option<Instant>,
    recovery_inflight: bool,
}

pub(crate) struct Circuit {
    settings: Settings,
    base_ms: u64,
    seed: u64,
    state: Mutex<State>,
}

pub(crate) struct Attempt {
    circuit: Arc<Circuit>,
    epoch: u64,
    recovery: bool,
    finished: bool,
}

impl Circuit {
    pub(crate) fn new(settings: Settings, base_ms: u64, seed: u64) -> Arc<Self> {
        Arc::new(Self {
            settings,
            base_ms,
            seed,
            state: Mutex::new(State::default()),
        })
    }

    pub(crate) fn eligible(&self, now: Instant) -> bool {
        let state = self.state.lock().unwrap();
        !state.recovery_inflight && state.next_attempt.is_none_or(|time| now >= time)
    }

    pub(crate) fn begin(self: &Arc<Self>, now: Instant) -> Option<Attempt> {
        let mut state = self.state.lock().unwrap();
        if state.recovery_inflight || state.next_attempt.is_some_and(|time| now < time) {
            return None;
        }
        let recovery = state.next_attempt.is_some();
        state.recovery_inflight = recovery;
        Some(Attempt {
            circuit: self.clone(),
            epoch: state.epoch,
            recovery,
            finished: false,
        })
    }
}

impl Attempt {
    pub(crate) fn finish(mut self, success: bool, now: Instant) {
        self.finished = true;
        let mut state = self.circuit.state.lock().unwrap();
        if state.epoch != self.epoch {
            return;
        }
        state.recovery_inflight = false;
        if success {
            state.failures = 0;
            if self.recovery {
                *state = State {
                    epoch: state.epoch.wrapping_add(1),
                    ..State::default()
                };
            }
            return;
        }
        state.failures += 1;
        if self.recovery || state.failures >= self.circuit.settings.failures {
            state.failures = 0;
            state.epoch = state.epoch.wrapping_add(1);
            let cap = self
                .circuit
                .base_ms
                .saturating_mul(1 << state.ejections.min(20))
                .min(self.circuit.settings.max_backoff_ms);
            let floor = cap.div_ceil(2);
            let jitter = crate::balance::mix(self.circuit.seed ^ state.epoch) % (cap - floor + 1);
            state.next_attempt = Some(now + Duration::from_millis(floor + jitter));
            state.ejections = state.ejections.saturating_add(1);
        }
    }
}

impl Drop for Attempt {
    fn drop(&mut self) {
        if self.recovery && !self.finished {
            let mut state = self.circuit.state.lock().unwrap();
            if state.epoch == self.epoch {
                state.recovery_inflight = false;
                // Cancellation says nothing about backend health. Delay another
                // probe so client churn cannot bypass the reconnection gate.
                state.next_attempt =
                    Some(Instant::now() + Duration::from_millis(self.circuit.base_ms));
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RetrySettings {
    pub rate: u32,
    pub concurrent: usize,
}

impl RetrySettings {
    pub(crate) fn validate(&self) -> Result<()> {
        ensure!(
            (1..=1000).contains(&self.rate) && (1..=128).contains(&self.concurrent),
            "invalid upstream retry budget"
        );
        Ok(())
    }
}

pub(crate) struct RetryBudget {
    slots: Arc<tokio::sync::Semaphore>,
    balance: Mutex<(Instant, f64)>,
    rate: f64,
    burst: f64,
}

impl RetryBudget {
    pub(crate) fn new(settings: &RetrySettings) -> Self {
        let burst = f64::from(settings.rate.min(settings.concurrent as u32));
        Self {
            slots: Arc::new(tokio::sync::Semaphore::new(settings.concurrent)),
            balance: Mutex::new((Instant::now(), burst)),
            rate: f64::from(settings.rate),
            burst,
        }
    }

    pub(crate) fn take(&self) -> Option<tokio::sync::OwnedSemaphorePermit> {
        let permit = self.slots.clone().try_acquire_owned().ok()?;
        let mut balance = self.balance.lock().unwrap();
        let remaining = (balance.1 + balance.0.elapsed().as_secs_f64() * self.rate).min(self.burst);
        *balance = (Instant::now(), remaining);
        if remaining < 1.0 {
            return None;
        }
        balance.1 -= 1.0;
        Some(permit)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_completions_cannot_close_breaker_and_only_one_probe_enters() {
        let now = Instant::now();
        let circuit = Circuit::new(Settings::default(), 100, 8);
        let stale = circuit.begin(now).unwrap();
        circuit.begin(now).unwrap().finish(false, now);
        assert!(circuit.eligible(now));
        circuit.begin(now).unwrap().finish(false, now);
        stale.finish(true, now);
        assert!(!circuit.eligible(now));
        let next = now + Duration::from_millis(100);
        let recovery = circuit.begin(next).unwrap();
        assert!(circuit.begin(next).is_none());
        recovery.finish(false, next);
        assert!(!circuit.eligible(next + Duration::from_millis(99)));
        let next = next + Duration::from_millis(200);
        circuit.begin(next).unwrap().finish(true, next);
        assert!(circuit.eligible(next));
        assert_eq!(circuit.state.lock().unwrap().ejections, 0);
    }

    #[test]
    fn cancelled_recovery_delays_and_releases_probe() {
        let circuit = Circuit::new(
            Settings {
                failures: 1,
                ..Settings::default()
            },
            100,
            5,
        );
        let now = Instant::now();
        circuit.begin(now).unwrap().finish(false, now);
        drop(circuit.begin(now + Duration::from_millis(100)).unwrap());
        let after = circuit.state.lock().unwrap().next_attempt.unwrap();
        assert!(!circuit.eligible(after - Duration::from_millis(1)));
        assert!(circuit.begin(after).is_some());
    }
}
