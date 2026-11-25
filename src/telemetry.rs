use crate::config::Logs;
use serde::Serialize;
use std::{
    fs::{self, File, OpenOptions},
    io::{BufWriter, Read, Seek, SeekFrom, Write},
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc, Arc,
    },
    time::Duration,
};

#[derive(Default)]
pub struct Metrics {
    pub requests: AtomicU64,
    pub rejected: AtomicU64,
    pub new_connections: AtomicU64,
    pub reused: AtomicU64,
    pub failures: AtomicU64,
    pub cancellations: AtomicU64,
    pub log_dropped: AtomicU64,
    pub log_errors: AtomicU64,
    pub pool_wait_us: AtomicU64,
    pub responses: AtomicU64,
    pub response_bytes: AtomicU64,
    pub body_errors: AtomicU64,
    pub http2_header_timeouts: AtomicU64,
    pub upstream_streams_active: AtomicU64,
    pub http2_tasks_active: Arc<AtomicU64>,
}
impl Metrics {
    pub fn prometheus(&self, name: &str) -> String {
        let mut s = String::new();
        for (label, value) in [
            ("requests", &self.requests),
            ("rejected", &self.rejected),
            ("upstream_connections_created", &self.new_connections),
            ("upstream_reused", &self.reused),
            ("upstream_failures", &self.failures),
            ("cancellations", &self.cancellations),
            ("log_dropped", &self.log_dropped),
            ("log_io_errors", &self.log_errors),
            ("pool_wait_microseconds", &self.pool_wait_us),
            ("responses", &self.responses),
            ("response_bytes", &self.response_bytes),
            ("body_errors", &self.body_errors),
            ("http2_header_timeouts", &self.http2_header_timeouts),
        ] {
            s.push_str(&format!(
                "# TYPE {name}_{label}_total counter\n{name}_{label}_total {}\n",
                value.load(Ordering::Relaxed)
            ));
        }
        for (label, value) in [
            ("upstream_streams_active", &self.upstream_streams_active),
            ("http2_tasks_active", self.http2_tasks_active.as_ref()),
        ] {
            s.push_str(&format!(
                "# TYPE {name}_{label} gauge\n{name}_{label} {}\n",
                value.load(Ordering::Relaxed)
            ));
        }
        s
    }
}
#[derive(Clone)]
pub struct Logger {
    tx: Option<mpsc::SyncSender<Event>>,
    pub metrics: Arc<Metrics>,
}
#[derive(Serialize, Clone)]
pub struct Attempt {
    pub number: u32,
    pub outcome: String,
    pub connection_id: Option<String>,
    pub reused: Option<bool>,
    pub pool_wait_ms: Option<f64>,
    pub connect_gate_ms: Option<f64>,
    pub dns_ms: Option<f64>,
    pub tcp_ms: Option<f64>,
    pub tls_ms: Option<f64>,
    pub headers_ms: Option<f64>,
}
#[derive(Serialize, Clone)]
pub struct Event {
    pub schema_version: &'static str,
    pub event_id: String,
    pub timestamp: String,
    pub instance_id: String,
    pub config_version: u64,
    pub request_id: String,
    pub traceparent: Option<String>,
    pub route_id: Option<String>,
    pub upstream_id: Option<String>,
    pub method: String,
    pub path: Option<String>,
    pub protocol: &'static str,
    pub status: u16,
    pub duration_ms: f64,
    pub transfer_ms: Option<f64>,
    pub request_bytes: u64,
    pub response_bytes: u64,
    pub outcome: String,
    pub error_direction: Option<String>,
    pub error_category: Option<String>,
    pub error_phase: Option<String>,
    pub reason: Option<String>,
    pub backpressure_observations: Option<u64>,
    pub attempts: Vec<Attempt>,
}
// The access log has one exclusive writer. Preserve every newline-terminated
// record, but discard an interrupted final record before appending or rotating.
fn open(config: &Logs, rotation_pending: &mut bool) -> std::io::Result<BufWriter<File>> {
    let path = &config.path;
    let existed = std::path::Path::new(path).exists();
    if let Some(parent) = std::path::Path::new(path).parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .open(path)?;
    let original = file.metadata()?.len();
    let mut end = original;
    let mut block = [0u8; 8192];
    while end > 0 {
        let start = end.saturating_sub(block.len() as u64);
        let len = (end - start) as usize;
        file.seek(SeekFrom::Start(start))?;
        file.read_exact(&mut block[..len])?;
        if let Some(last) = block[..len].iter().rposition(|byte| *byte == b'\n') {
            end = start + last as u64 + 1;
            break;
        }
        end = start;
    }
    if end != original {
        *rotation_pending = true;
        file.set_len(end)?;
    }
    if existed && *rotation_pending {
        // Hand off a repaired inode before appending again. A tailing collector
        // may have observed the old partial length; reusing that same inode at
        // the same length would look like a forbidden in-place rewrite.
        // Keep the handoff pending across failed truncation/rename/open calls.
        drop(file);
        rotate(config)?;
        file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .append(true)
            .open(path)?;
    }
    *rotation_pending = false;
    Ok(BufWriter::new(file))
}

fn rotate(config: &Logs) -> std::io::Result<()> {
    if config.retained_files == 0 {
        fs::remove_file(&config.path)?;
    } else {
        for i in (1..config.retained_files).rev() {
            let from = format!("{}.{}", config.path, i);
            if std::path::Path::new(&from).exists() {
                fs::rename(from, format!("{}.{}", config.path, i + 1))?;
            }
        }
        fs::rename(&config.path, format!("{}.1", config.path))?;
    }
    Ok(())
}

fn discard_buffer<W: Write>(writer: &mut Option<BufWriter<W>>) {
    if let Some(writer) = writer.take() {
        // Drop otherwise retries flush and may append the discarded fragment.
        let _ = writer.into_parts();
    }
}

fn flush(writer: &mut Option<BufWriter<File>>) -> std::io::Result<()> {
    if let Some(w) = writer.as_mut() {
        if let Err(error) = w.flush() {
            discard_buffer(writer);
            return Err(error);
        }
    }
    Ok(())
}
impl Logger {
    pub fn enabled(&self) -> bool {
        self.tx.is_some()
    }

    pub fn start(
        config: Logs,
        metrics: Arc<Metrics>,
    ) -> (Self, Option<std::thread::JoinHandle<()>>) {
        if !config.enabled {
            return (Self { tx: None, metrics }, None);
        }
        let (tx, rx) = mpsc::sync_channel::<Event>(config.queue);
        let m = metrics.clone();
        let worker = std::thread::spawn(move || {
            // A restart also hands off the prior inode. This covers interruption
            // between tail truncation and its rename in the previous process.
            let mut rotation_pending = true;
            let mut writer = open(&config, &mut rotation_pending).ok();
            let mut size = fs::metadata(&config.path).map(|x| x.len()).unwrap_or(0);
            let mut batch = Vec::with_capacity(128);
            let mut data = Vec::with_capacity(1024);
            loop {
                let first = match rx.recv_timeout(Duration::from_millis(100)) {
                    Ok(e) => e,
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        if flush(&mut writer).is_err() {
                            m.log_errors.fetch_add(1, Ordering::Relaxed);
                        }
                        continue;
                    }
                    Err(_) => break,
                };
                batch.push(first);
                batch.extend(rx.try_iter().take(127));
                for event in batch.drain(..) {
                    data.clear();
                    serde_json::to_writer(&mut data, &event).expect("serializable event");
                    data.push(b'\n');
                    if writer.is_none() {
                        writer = open(&config, &mut rotation_pending).ok();
                        size = fs::metadata(&config.path).map(|x| x.len()).unwrap_or(0);
                        if writer.is_none() {
                            m.log_errors.fetch_add(1, Ordering::Relaxed);
                            m.log_dropped.fetch_add(1, Ordering::Relaxed);
                            continue;
                        }
                    }
                    if size + data.len() as u64 > config.rotate_bytes {
                        if flush(&mut writer).is_err() {
                            m.log_errors.fetch_add(1, Ordering::Relaxed);
                            m.log_dropped.fetch_add(1, Ordering::Relaxed);
                            continue;
                        }
                        discard_buffer(&mut writer);
                        let rotation = rotate(&config);
                        if rotation.is_err() {
                            m.log_errors.fetch_add(1, Ordering::Relaxed);
                            m.log_dropped.fetch_add(1, Ordering::Relaxed);
                            continue;
                        }
                        size = fs::metadata(&config.path).map(|x| x.len()).unwrap_or(0);
                    }
                    if writer.is_none() {
                        writer = open(&config, &mut rotation_pending).ok();
                    }
                    let result = writer.as_mut().map(|w| w.write_all(&data));
                    if matches!(result, Some(Ok(()))) {
                        size += data.len() as u64;
                    } else {
                        m.log_errors.fetch_add(1, Ordering::Relaxed);
                        m.log_dropped.fetch_add(1, Ordering::Relaxed);
                        discard_buffer(&mut writer);
                    }
                }
                if flush(&mut writer).is_err() {
                    m.log_errors.fetch_add(1, Ordering::Relaxed);
                }
            }
            let _ = flush(&mut writer);
        });
        (
            Self {
                tx: Some(tx),
                metrics,
            },
            Some(worker),
        )
    }
    pub fn emit(&self, event: Event) {
        if let Some(tx) = &self.tx {
            if tx.try_send(event).is_err() {
                self.metrics.log_dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
}
pub fn traceparent(value: &str) -> bool {
    value.is_ascii()
        && value.len() == 55
        && &value[0..2] == "00"
        && value.as_bytes()[2] == b'-'
        && value.as_bytes()[35] == b'-'
        && value.as_bytes()[52] == b'-'
        && value.bytes().enumerate().all(|(i, b)| {
            [2, 35, 52].contains(&i) || b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
        })
        && value[3..35].bytes().any(|b| b != b'0')
        && value[36..52].bytes().any(|b| b != b'0')
}
#[cfg(test)]
mod tests {
    use std::io::{BufWriter, Write};

    #[test]
    fn reopen_preserves_complete_records_and_removes_only_unterminated_tail() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("access.jsonl");
        let config = crate::config::Logs {
            enabled: true,
            path: path.to_str().unwrap().to_owned(),
            queue: 16,
            rotate_bytes: 4096,
            retained_files: 1,
        };
        for tail_size in [0, 1, 8191, 8192, 8193, 20000] {
            let mut data = b"{\"old\":1}\n{\"old\":2}\n".to_vec();
            data.extend(vec![b'x'; tail_size]);
            std::fs::write(&path, data).unwrap();
            let mut writer = super::open(&config, &mut true).unwrap();
            writer.write_all(b"{\"new\":3}\n").unwrap();
            writer.flush().unwrap();
            assert_eq!(std::fs::read(&path).unwrap(), b"{\"new\":3}\n");
            assert_eq!(
                std::fs::read(dir.path().join("access.jsonl.1")).unwrap(),
                b"{\"old\":1}\n{\"old\":2}\n"
            );
        }
        std::fs::write(&path, b"{\"partial\":").unwrap();
        drop(super::open(&config, &mut true).unwrap());
        assert_eq!(std::fs::metadata(path).unwrap().len(), 0);
    }

    #[test]
    fn failed_buffer_is_discarded_without_a_drop_flush_retry() {
        struct PartialWriter(std::rc::Rc<std::cell::Cell<usize>>);
        impl Write for PartialWriter {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                let call = self.0.get();
                self.0.set(call + 1);
                if call == 0 {
                    Ok(bytes.len().min(3))
                } else {
                    Err(std::io::Error::from_raw_os_error(28))
                }
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let calls = std::rc::Rc::new(std::cell::Cell::new(0));
        let mut writer = Some(BufWriter::new(PartialWriter(calls.clone())));
        writer
            .as_mut()
            .unwrap()
            .write_all(b"partial record\n")
            .unwrap();
        assert!(writer.as_mut().unwrap().flush().is_err());
        let before = calls.get();
        super::discard_buffer(&mut writer);
        assert!(writer.is_none());
        assert_eq!(calls.get(), before);
    }

    #[test]
    fn failed_rotation_after_tail_repair_still_hands_off_inode_on_retry() {
        use std::os::unix::fs::MetadataExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("access.jsonl");
        let rotated = dir.path().join("access.jsonl.1");
        let config = crate::config::Logs {
            enabled: true,
            path: path.to_str().unwrap().to_owned(),
            queue: 16,
            rotate_bytes: 4096,
            retained_files: 1,
        };
        std::fs::write(&path, b"{\"old\":1}\npartial").unwrap();
        let old_inode = std::fs::metadata(&path).unwrap().ino();
        std::fs::create_dir(&rotated).unwrap();
        let mut pending = false;
        assert!(super::open(&config, &mut pending).is_err());
        assert!(pending);
        assert_eq!(std::fs::read(&path).unwrap(), b"{\"old\":1}\n");
        std::fs::remove_dir(&rotated).unwrap();
        let mut writer = super::open(&config, &mut pending).unwrap();
        assert!(!pending);
        writer.write_all(b"{\"new\":2}\n").unwrap();
        writer.flush().unwrap();
        assert_eq!(std::fs::metadata(&rotated).unwrap().ino(), old_inode);
        assert_ne!(std::fs::metadata(&path).unwrap().ino(), old_inode);
        assert_eq!(std::fs::read(&rotated).unwrap(), b"{\"old\":1}\n");
        assert_eq!(std::fs::read(&path).unwrap(), b"{\"new\":2}\n");
    }

    #[test]
    fn trace_validation() {
        assert!(super::traceparent(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ));
        assert!(!super::traceparent(
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01"
        ));
        assert!(!super::traceparent(
            "éé-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ));
    }
}
