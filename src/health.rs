//! Opt-in bounded HTTP/1 readiness probes. No pool reuse, redirects or response bodies.
use crate::{
    config::HealthCheck,
    pool::{Endpoint, Generation},
    tls,
};
use hyper_util::rt::TokioIo;
use std::{
    sync::{atomic::Ordering, Arc},
    time::Duration,
};
use tokio::{
    net::{TcpStream, UnixStream},
    sync::Semaphore,
    task::{JoinHandle, JoinSet},
};

trait Io: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send {}
impl<T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send> Io for T {}

#[derive(Default)]
struct Streak {
    successes: u32,
    failures: u32,
}
impl Streak {
    fn observe(&mut self, ok: bool, h: &HealthCheck, endpoint: &Endpoint) {
        if ok {
            self.failures = 0;
            self.successes = self.successes.saturating_add(1);
            if self.successes >= h.rise {
                endpoint.active_healthy.store(true, Ordering::Release);
            }
        } else {
            self.successes = 0;
            self.failures = self.failures.saturating_add(1);
            if self.failures >= h.fall {
                endpoint.active_healthy.store(false, Ordering::Release);
            }
        }
    }
}

pub fn start(generation: &Arc<Generation>) -> JoinHandle<()> {
    let endpoints: Vec<_> = generation
        .endpoints
        .values()
        .filter(|e| e.config.health_check.is_some())
        .cloned()
        .collect();
    tokio::spawn(async move {
        let gate = Arc::new(Semaphore::new(4));
        let mut tasks = JoinSet::new();
        for endpoint in endpoints {
            let gate = gate.clone();
            tasks.spawn(async move {
                let h = endpoint.config.health_check.as_ref().unwrap();
                let mut streak = Streak::default();
                loop {
                    // Queueing does not count as failure; at most four sockets exist.
                    let permit = gate.acquire().await.unwrap();
                    let ok = tokio::time::timeout(
                        Duration::from_millis(h.timeout_ms),
                        probe(&endpoint, h),
                    )
                    .await
                    .is_ok_and(|r| r.is_ok_and(|ok| ok));
                    drop(permit);
                    streak.observe(ok, h, &endpoint);
                    tokio::time::sleep(Duration::from_millis(h.interval_ms)).await;
                }
            });
        }
        // Dropping this JoinSet on reload/shutdown cancels all probes and sockets.
        while tasks.join_next().await.is_some() {}
    })
}

async fn probe(endpoint: &Endpoint, health: &HealthCheck) -> anyhow::Result<bool> {
    let io: Box<dyn Io> = if let Some(path) = endpoint.config.address.strip_prefix("unix:") {
        Box::new(UnixStream::connect(path).await?)
    } else {
        let tcp = TcpStream::connect(&endpoint.config.address).await?;
        if let Some(config) = &endpoint.config.tls {
            let client = endpoint
                .tls_client
                .clone()
                .ok_or_else(|| anyhow::anyhow!("missing TLS client"))?;
            Box::new(
                tokio_rustls::TlsConnector::from(client)
                    .connect(tls::server_name(config)?, tcp)
                    .await?,
            )
        } else {
            Box::new(tcp)
        }
    };
    let (mut sender, connection) = hyper::client::conn::http1::Builder::new()
        .max_buf_size(8192)
        .max_headers(32)
        .handshake(TokioIo::new(io))
        .await?;
    let request = hyper::Request::builder()
        .method("GET")
        .uri(&health.path)
        .header("host", &endpoint.config.host)
        .header("connection", "close")
        .body(crate::body::full(bytes::Bytes::new()))?;
    let response = sender.send_request(request);
    tokio::pin!(connection, response);
    tokio::select! {
        biased;
        response = &mut response => Ok(response?.status().is_success()),
        result = &mut connection => {
            result?;
            Ok(response.await?.status().is_success())
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use std::collections::HashMap;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    fn generation(address: String) -> Arc<Generation> {
        let mut config: Config = toml::from_str(include_str!("../examples/cauce.toml")).unwrap();
        config.upstreams[0].address = address;
        config.upstreams[0].health_check = Some(HealthCheck {
            path: "/ready".into(),
            interval_ms: 100,
            timeout_ms: 50,
            rise: 2,
            fall: 2,
        });
        Generation::new(config, 1, &HashMap::new())
    }

    #[test]
    fn thresholds_and_disabled_default() {
        let g = generation("127.0.0.1:1".into());
        let e = &g.endpoints[&g.config.upstreams[0].id];
        let h = e.config.health_check.as_ref().unwrap();
        let mut streak = Streak::default();
        assert!(!e.active_healthy.load(Ordering::Acquire));
        streak.observe(true, h, e);
        assert!(!e.active_healthy.load(Ordering::Acquire));
        streak.observe(true, h, e);
        assert!(e.active_healthy.load(Ordering::Acquire));
        streak.observe(false, h, e);
        assert!(e.active_healthy.load(Ordering::Acquire));
        streak.observe(false, h, e);
        assert!(!e.active_healthy.load(Ordering::Acquire));
        streak.observe(true, h, e);
        streak.observe(false, h, e);
        streak.observe(true, h, e);
        assert!(!e.active_healthy.load(Ordering::Acquire));
        let config: Config = toml::from_str(include_str!("../examples/cauce.toml")).unwrap();
        let default = Generation::new(config, 2, &HashMap::new());
        assert!(default
            .endpoints
            .values()
            .all(|e| e.active_healthy.load(Ordering::Acquire)));
    }

    #[tokio::test]
    async fn actual_http_status_and_timeout() {
        for (status, expected) in [(200, true), (204, true), (302, false), (503, false)] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let g = generation(listener.local_addr().unwrap().to_string());
            let server = tokio::spawn(async move {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut buffer = [0; 4096];
                let n = socket.read(&mut buffer).await.unwrap();
                assert!(std::str::from_utf8(&buffer[..n])
                    .unwrap()
                    .starts_with("GET /ready HTTP/1.1"));
                socket
                    .write_all(
                        format!("HTTP/1.1 {status} Test\r\nContent-Length: 1000000000\r\n\r\n")
                            .as_bytes(),
                    )
                    .await
                    .unwrap();
                // Probe completes at headers; it does not buffer or await the body.
                tokio::time::sleep(Duration::from_millis(150)).await;
            });
            let e = &g.endpoints[&g.config.upstreams[0].id];
            let result = tokio::time::timeout(
                Duration::from_millis(100),
                probe(e, e.config.health_check.as_ref().unwrap()),
            )
            .await
            .unwrap()
            .unwrap();
            assert_eq!(result, expected);
            server.abort();
        }
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let g = generation(listener.local_addr().unwrap().to_string());
        let e = &g.endpoints[&g.config.upstreams[0].id];
        assert!(tokio::time::timeout(
            Duration::from_millis(30),
            probe(e, e.config.health_check.as_ref().unwrap())
        )
        .await
        .is_err());
    }

    #[tokio::test]
    async fn scheduler_ejects_recovers_and_stops() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let g = generation(listener.local_addr().unwrap().to_string());
        let e = g.endpoints[&g.config.upstreams[0].id].clone();
        let ready = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let server_ready = ready.clone();
        let count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let server_count = count.clone();
        let server = tokio::spawn(async move {
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                server_count.fetch_add(1, Ordering::SeqCst);
                let mut buf = [0; 4096];
                let n = socket.read(&mut buf).await.unwrap();
                assert!(n > 0);
                let status = if server_ready.load(Ordering::Acquire) {
                    200
                } else {
                    503
                };
                let _ = socket
                    .write_all(
                        format!("HTTP/1.1 {status} Test\r\nContent-Length: 0\r\n\r\n").as_bytes(),
                    )
                    .await;
            }
        });
        let mut route = g.config.routes[0].clone();
        route.upstreams = vec![e.config.id.clone()];
        assert!(g.select(&route).is_none());
        let runner = start(&g);
        for expected in [true, false, true] {
            ready.store(expected, Ordering::Release);
            tokio::time::timeout(Duration::from_secs(3), async {
                while e.active_healthy.load(Ordering::Acquire) != expected {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            assert_eq!(g.select(&route).is_some(), expected);
        }
        runner.abort();
        let _ = runner.await;
        tokio::time::sleep(Duration::from_millis(30)).await;
        let before = count.load(Ordering::SeqCst);
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert_eq!(count.load(Ordering::SeqCst), before);
        server.abort();
    }

    #[test]
    fn invalid_config_rejected() {
        let g = generation("127.0.0.1:1".into());
        let mut config = g.config.clone();
        config.validate().unwrap();
        config.upstreams[0].health_check.as_mut().unwrap().path = "https://other.test/".into();
        assert!(config.validate().is_err());
        config.upstreams[0].health_check.as_mut().unwrap().path = "/".into();
        config.upstreams[0]
            .health_check
            .as_mut()
            .unwrap()
            .timeout_ms = 0;
        assert!(config.validate().is_err());
    }
}
