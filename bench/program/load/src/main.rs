use anyhow::{anyhow, ensure, Context, Result};
use bytes::Bytes;
use hdrhistogram::Histogram;
use http_body_util::{BodyExt, Full};
use hyper::{header, Method, Request, Uri, Version};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::{
    client::legacy::{
        connect::{capture_connection, HttpConnector, HttpInfo},
        Client,
    },
    rt::{TokioExecutor, TokioTimer},
};
use rustls::{ClientConfig, RootCertStore};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    hint,
    io::{BufReader, BufWriter, Write},
    path::{Path, PathBuf},
    sync::Arc,
    thread,
    time::{Duration, Instant},
};
use tokio::{sync::mpsc, task::JoinSet};
use url::Url;

fn default_timeout_ms() -> u64 {
    5_000
}

fn default_lag_ms() -> u64 {
    250
}

fn default_spin_us() -> u64 {
    40
}

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
enum Protocol {
    Http1,
    Http2,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Config {
    url: String,
    protocol: Protocol,
    host: Option<String>,
    ca_file: Option<PathBuf>,
    rate: u64,
    seconds: u64,
    warmup_seconds: u64,
    concurrency: usize,
    queue: usize,
    request_bytes: usize,
    response_bytes: usize,
    response_byte: u8,
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
    #[serde(default = "default_lag_ms")]
    drop_lag_ms: u64,
    #[serde(default = "default_spin_us")]
    spin_us: u64,
    #[serde(default)]
    trace_offers: bool,
    warmup_done_file: Option<PathBuf>,
    resume_file: Option<PathBuf>,
    measurement_done_file: Option<PathBuf>,
    measurement_release_file: Option<PathBuf>,
}

impl Config {
    fn validate(&self) -> Result<Url> {
        let url = Url::parse(&self.url)?;
        ensure!(
            matches!(url.scheme(), "http" | "https")
                && url.host().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.fragment().is_none(),
            "expected HTTP(S) URL without credentials or fragment"
        );
        ensure!(
            (url.scheme() == "https") == self.ca_file.is_some(),
            "HTTPS requires the fixture CA, HTTP does not use one"
        );
        ensure!(
            self.rate > 0
                && self.seconds > 0
                && self
                    .rate
                    .checked_mul(self.seconds.max(self.warmup_seconds))
                    .is_some_and(|offers| offers <= 2_000_000),
            "each phase needs at most two million offers"
        );
        ensure!(
            (1..=4096).contains(&self.concurrency) && (1..=65_536).contains(&self.queue),
            "invalid concurrency or queue capacity"
        );
        ensure!(
            self.request_bytes <= 16 * 1024 * 1024 && self.response_bytes <= 64 * 1024 * 1024,
            "payload exceeds fixture limits"
        );
        ensure!(
            (1..=60_000).contains(&self.timeout_ms)
                && (1..=self.timeout_ms).contains(&self.drop_lag_ms)
                && self.spin_us <= 500,
            "invalid scheduler or timeout settings"
        );
        Ok(url)
    }
}

#[derive(Clone, Copy)]
struct Offer {
    n: u64,
    scheduled_ns: u64,
    arrival_ns: u64,
}

#[derive(Serialize)]
struct Row {
    n: u64,
    scheduled_ns: u64,
    arrival_ns: u64,
    start_ns: Option<u64>,
    end_ns: u64,
    status: Option<u16>,
    bytes: usize,
    outcome: &'static str,
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    request_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    local_port: Option<u16>,
}

impl Row {
    fn new(offer: Offer, end_ns: u64, outcome: &'static str) -> Self {
        Self {
            n: offer.n,
            scheduled_ns: offer.scheduled_ns,
            arrival_ns: offer.arrival_ns,
            start_ns: None,
            end_ns,
            status: None,
            bytes: 0,
            outcome,
            error: None,
            request_id: None,
            local_port: None,
        }
    }
}

fn ns(duration: Duration) -> u64 {
    duration
        .as_nanos()
        .try_into()
        .expect("bounded measurement duration")
}

fn scheduled_ns(n: u64, rate: u64) -> u64 {
    n * 1_000_000_000 / rate
}

fn schedule(
    tx: mpsc::Sender<Offer>,
    origin: Instant,
    rate: u64,
    seconds: u64,
    spin: Duration,
    drop_lag_ns: u64,
) -> Vec<Row> {
    let mut rejected = Vec::new();
    for n in 0..rate * seconds {
        let scheduled_ns = scheduled_ns(n, rate);
        let planned = origin + Duration::from_nanos(scheduled_ns);
        while let Some(remaining) = planned.checked_duration_since(Instant::now()) {
            if remaining > spin {
                thread::sleep(remaining - spin);
            } else {
                hint::spin_loop();
            }
        }
        let arrival_ns = ns(origin.elapsed());
        let offer = Offer {
            n,
            scheduled_ns,
            arrival_ns,
        };
        let reason = if arrival_ns - scheduled_ns > drop_lag_ns {
            "scheduler_drop"
        } else {
            match tx.try_send(offer) {
                Ok(()) => continue,
                Err(mpsc::error::TrySendError::Full(_)) => "queue_rejection",
                Err(mpsc::error::TrySendError::Closed(_)) => "dispatcher_closed",
            }
        };
        rejected.push(Row::new(offer, arrival_ns, reason));
    }
    rejected
}

type LoadClient = Client<HttpsConnector<HttpConnector>, Full<Bytes>>;

struct RequestTemplate {
    uri: Uri,
    method: Method,
    version: Version,
    host: Option<header::HeaderValue>,
    payload: Bytes,
}

impl RequestTemplate {
    fn request(&self) -> Request<Full<Bytes>> {
        let mut request = Request::new(Full::new(self.payload.clone()));
        *request.uri_mut() = self.uri.clone();
        *request.method_mut() = self.method.clone();
        *request.version_mut() = self.version;
        request
            .headers_mut()
            .insert(header::ACCEPT, header::HeaderValue::from_static("*/*"));
        if let Some(host) = &self.host {
            request.headers_mut().insert(header::HOST, host.clone());
        }
        request
    }
}

fn create_client(config: &Config) -> Result<LoadClient> {
    let mut roots = RootCertStore::empty();
    if let Some(path) = &config.ca_file {
        let mut reader = BufReader::new(File::open(path)?);
        for certificate in rustls_pemfile::certs(&mut reader) {
            roots.add(certificate?)?;
        }
        ensure!(
            !roots.is_empty(),
            "fixture CA file contains no certificates"
        );
    }
    let tls =
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()?
            .with_root_certificates(roots)
            .with_no_client_auth();
    let mut transport = HttpConnector::new();
    transport.enforce_http(false);
    transport.set_nodelay(true);
    transport.set_keepalive(Some(Duration::from_secs(15)));
    let tls = HttpsConnectorBuilder::new()
        .with_tls_config(tls)
        .https_or_http();
    let connector = match config.protocol {
        Protocol::Http1 => tls.enable_http1().wrap_connector(transport),
        Protocol::Http2 => tls.enable_http2().wrap_connector(transport),
    };
    // Reqwest's retry-never policy does not disable the Hyper client's replay
    // of a request canceled on a reused connection before it was written.
    Ok(Client::builder(TokioExecutor::new())
        .pool_timer(TokioTimer::new())
        .pool_max_idle_per_host(config.concurrency)
        .http2_only(matches!(config.protocol, Protocol::Http2))
        .retry_canceled_requests(false)
        .build(connector))
}

async fn transfer(
    row: &mut Row,
    request: Request<Full<Bytes>>,
    client: &LoadClient,
    version: Version,
    expected_len: usize,
    expected_byte: u8,
    trace: bool,
) -> Result<()> {
    let response = client.request(request).await?;
    row.status = Some(response.status().as_u16());
    if trace {
        row.request_id = response
            .headers()
            .get("x-request-id")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
    }
    if !response.status().is_success() {
        row.outcome = "http_error";
        return Ok(());
    }
    if response.version() != version {
        row.outcome = "protocol_error";
        return Ok(());
    }
    let mut body = response.into_body();
    while let Some(frame) = body.frame().await {
        let Ok(chunk) = frame?.into_data() else {
            continue;
        };
        row.bytes += chunk.len();
        if row.bytes > expected_len || chunk.iter().any(|byte| *byte != expected_byte) {
            row.outcome = "body_error";
            return Ok(());
        }
    }
    row.outcome = if row.bytes == expected_len {
        "success"
    } else {
        "body_error"
    };
    Ok(())
}

async fn phase(
    config: &Config,
    client: &LoadClient,
    request: &RequestTemplate,
    seconds: u64,
    warmup: bool,
) -> Result<Vec<Row>> {
    let (tx, mut rx) = mpsc::channel(config.queue);
    let origin = Instant::now() + Duration::from_millis(50);
    let rate = config.rate;
    let spin = Duration::from_micros(config.spin_us);
    let lag = config.drop_lag_ms * 1_000_000;
    let scheduler = thread::Builder::new()
        .name("offer-clock".into())
        .spawn(move || schedule(tx, origin, rate, seconds, spin, lag))?;
    let mut tasks = JoinSet::new();
    let mut rows = Vec::with_capacity((rate * seconds) as usize);
    let mut receiving = true;
    while receiving || !tasks.is_empty() {
        tokio::select! {
            finished = tasks.join_next(), if !tasks.is_empty() => {
                rows.push(finished.expect("task set is nonempty")?);
            }
            incoming = rx.recv(), if receiving && tasks.len() < config.concurrency => {
                let Some(offer) = incoming else {
                    receiving = false;
                    continue;
                };
                let client = client.clone();
                let mut req = request.request();
                let trace = config.trace_offers;
                if trace {
                    let offer_id = if warmup { 1_000_000_000 + offer.n } else { offer.n };
                    req.headers_mut().insert("x-cauce-bench-offer", offer_id.into());
                }
                let connection = trace.then(|| capture_connection(&mut req));
                let timeout = Duration::from_millis(config.timeout_ms);
                let expected_len = config.response_bytes;
                let expected_byte = config.response_byte;
                tasks.spawn(async move {
                    let start_ns = ns(origin.elapsed());
                    let mut row = Row::new(offer, start_ns, "transport_error");
                    row.start_ns = Some(start_ns);
                    let deadline = origin + Duration::from_nanos(offer.scheduled_ns) + timeout;
                    if Instant::now() >= deadline {
                        row.outcome = "queue_timeout";
                    } else {
                        let version = req.version();
                        match tokio::time::timeout_at(
                            deadline.into(), transfer(&mut row, req, &client, version, expected_len, expected_byte, trace)
                        ).await {
                            Ok(Ok(())) => {}
                            Ok(Err(error)) => row.error = Some(format!("{error:#}")),
                            Err(_) => row.outcome = "request_timeout",
                        }
                    }
                    row.end_ns = ns(origin.elapsed());
                    if let Some(connection) = connection {
                        if let Some(metadata) = connection.connection_metadata().as_ref() {
                            let mut extras = hyper::http::Extensions::new();
                            metadata.get_extras(&mut extras);
                            row.local_port = extras.get::<HttpInfo>().map(|info| info.local_addr().port());
                        }
                    }
                    row
                });
            }
        }
    }
    rows.extend(
        scheduler
            .join()
            .map_err(|_| anyhow!("offer clock panicked"))?,
    );
    rows.sort_unstable_by_key(|row| row.n);
    ensure!(
        rows.len() == (rate * seconds) as usize
            && rows.iter().enumerate().all(|(n, row)| row.n == n as u64),
        "arrival accounting failed"
    );
    Ok(rows)
}

fn histogram(values: impl Iterator<Item = u64>) -> Result<serde_json::Value> {
    let mut hist = Histogram::<u64>::new(3)?;
    for value in values {
        hist.record(value)?;
    }
    let quantile = |q| (!hist.is_empty()).then(|| hist.value_at_quantile(q));
    Ok(serde_json::json!({
        "count": hist.len(),
        "p50_ns": quantile(0.5),
        "p95_ns": quantile(0.95),
        "p99_ns": quantile(0.99),
        "p99_9_ns": quantile(0.999),
        "max_ns": (!hist.is_empty()).then(|| hist.max()),
        "buckets": hist.iter_recorded().map(|bucket| [
            bucket.value_iterated_to(), bucket.count_at_value()
        ]).collect::<Vec<_>>(),
    }))
}

fn report(config: &Config, rows: &[Row], warmup: &[Row], raw: &Path, summary: &Path) -> Result<()> {
    if !warmup.is_empty() {
        let mut warm = BufWriter::new(File::create(raw.with_extension("warmup.jsonl"))?);
        for row in warmup {
            serde_json::to_writer(&mut warm, row)?;
            warm.write_all(b"\n")?;
        }
        warm.flush()?;
    }
    let mut out = BufWriter::new(File::create(raw)?);
    let mut outcomes = BTreeMap::<_, usize>::new();
    for row in rows {
        *outcomes.entry(row.outcome).or_default() += 1;
        serde_json::to_writer(&mut out, row)?;
        out.write_all(b"\n")?;
    }
    out.flush()?;
    let success = || rows.iter().filter(|row| row.outcome == "success");
    let within_window = success()
        .filter(|row| row.end_ns <= config.seconds * 1_000_000_000)
        .count();
    let result = serde_json::json!({
        "schema": "cauce.open-loop.v1",
        "config": config,
        "offered": rows.len(),
        "outcomes": outcomes,
        "warmup_offers": warmup.len(),
        "warmup_successes": warmup.iter().filter(|row| row.outcome == "success").count(),
        "offer_window_successes": within_window,
        "offer_window_goodput_per_second": within_window as f64 / config.seconds as f64,
        "scheduled_latency": histogram(success().map(|row| row.end_ns - row.scheduled_ns))?,
        "scheduler_latency": histogram(rows.iter().map(|row| row.arrival_ns - row.scheduled_ns))?,
        "queue_latency": histogram(rows.iter().filter_map(|row| row.start_ns.map(|start| start - row.arrival_ns)))?,
        "service_latency": histogram(success().map(|row| row.end_ns - row.start_ns.expect("successful request was dispatched")))?,
    });
    fs::write(summary, serde_json::to_vec_pretty(&result)?)?;
    Ok(())
}

async fn barrier(marker: &Option<PathBuf>, release: &Option<PathBuf>) -> Result<()> {
    if let Some(marker) = marker {
        fs::write(marker, "")?;
    }
    if let Some(release) = release {
        tokio::time::timeout(Duration::from_secs(45), async {
            while !release.is_file() {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .with_context(|| format!("wait for {}", release.display()))?;
    }
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let paths = std::env::args_os()
        .skip(1)
        .map(PathBuf::from)
        .collect::<Vec<_>>();
    let [config_path, raw_path, summary_path] = paths.as_slice() else {
        anyhow::bail!("usage: cauce-load SPEC.json OFFERS.jsonl SUMMARY.json");
    };
    let config: Config = serde_json::from_reader(File::open(config_path)?)?;
    let url = config.validate()?;
    let client = create_client(&config)?;
    let request = RequestTemplate {
        uri: url.as_str().parse()?,
        version: match config.protocol {
            Protocol::Http1 => Version::HTTP_11,
            Protocol::Http2 => Version::HTTP_2,
        },
        method: if config.request_bytes == 0 {
            Method::GET
        } else {
            Method::POST
        },
        host: config.host.as_deref().map(str::parse).transpose()?,
        payload: Bytes::from(vec![b'x'; config.request_bytes]),
    };
    let warmup = if config.warmup_seconds > 0 {
        phase(&config, &client, &request, config.warmup_seconds, true).await?
    } else {
        Vec::new()
    };
    barrier(&config.warmup_done_file, &config.resume_file).await?;
    let rows = phase(&config, &client, &request, config.seconds, false).await?;
    barrier(
        &config.measurement_done_file,
        &config.measurement_release_file,
    )
    .await?;
    report(&config, &rows, &warmup, raw_path, summary_path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integer_schedule_has_no_interval_rounding_drift() {
        assert_eq!(scheduled_ns(0, 3), 0);
        assert_eq!(scheduled_ns(1, 3), 333_333_333);
        assert_eq!(scheduled_ns(2, 3), 666_666_666);
        assert_eq!(scheduled_ns(3, 3), 1_000_000_000);
    }

    #[test]
    fn saturated_queue_accounts_for_every_offer() {
        let (tx, mut rx) = mpsc::channel(1);
        let rows = schedule(tx, Instant::now(), 3, 1, Duration::ZERO, 250_000_000);
        assert!(rx.try_recv().is_ok());
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].n, 1);
        assert!(rows.iter().all(|row| row.outcome == "queue_rejection"));
    }
}
