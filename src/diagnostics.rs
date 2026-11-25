//! Opt-in, bounded transport event capture for diagnostic runs.
use serde_json::{json, Value};
use std::{
    error::Error,
    sync::{
        atomic::{AtomicU64, Ordering},
        OnceLock,
    },
    time::Instant,
};

static ORIGIN: OnceLock<Option<Instant>> = OnceLock::new();
static SEQUENCE: AtomicU64 = AtomicU64::new(0);
static NEXT_DOWNSTREAM: AtomicU64 = AtomicU64::new(1);
const EVENT_LIMIT: u64 = 250_000;

pub fn init() {
    let enabled = std::env::var("CAUCE_TRANSPORT_DIAGNOSTICS").as_deref() == Ok("1");
    let _ = ORIGIN.set(enabled.then(Instant::now));
}

pub fn enabled() -> bool {
    ORIGIN.get().is_some_and(Option::is_some)
}

pub fn downstream_id() -> Option<u64> {
    enabled().then(|| NEXT_DOWNSTREAM.fetch_add(1, Ordering::Relaxed))
}

pub fn emit(event: &'static str, fields: impl FnOnce() -> Value) {
    let Some(Some(origin)) = ORIGIN.get() else {
        return;
    };
    let seq = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    if seq == EVENT_LIMIT {
        eprintln!(r#"{{"event":"transport_diagnostics_truncated","limit":{EVENT_LIMIT}}}"#);
    }
    if seq >= EVENT_LIMIT {
        return;
    }
    eprintln!(
        "{}",
        json!({
            "event": event, "seq": seq, "elapsed_us": origin.elapsed().as_micros() as u64,
            "worker": format!("{:?}", std::thread::current().id()), "fields": fields()
        })
    );
}

pub fn chain(error: &(dyn Error + 'static)) -> Vec<String> {
    let mut chain = vec![error.to_string()];
    let mut next = error.source();
    while let Some(cause) = next {
        chain.push(cause.to_string());
        next = cause.source();
    }
    chain
}

pub struct Task {
    kind: &'static str,
    id: u64,
    terminal: bool,
}

impl Task {
    pub fn new(kind: &'static str, id: Option<u64>) -> Option<Self> {
        id.map(|id| Self {
            kind,
            id,
            terminal: false,
        })
    }

    pub fn finish(&mut self, reason: &'static str, error: Option<&(dyn Error + 'static)>) {
        self.terminal = true;
        emit("transport_task_end", || {
            json!({
                "kind": self.kind, "connection": self.id,
                "reason": reason, "error": error.map(chain),
            })
        });
    }
}

impl Drop for Task {
    fn drop(&mut self) {
        if !self.terminal {
            self.finish("future_dropped", None);
        }
    }
}
