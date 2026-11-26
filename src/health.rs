//! Opt-in bounded upstream readiness probes. No pool reuse or redirects.
use crate::{
    config::{HealthCheck, HealthMode, UpstreamProtocol},
    pool::{Endpoint, Generation},
    tls,
};
use bytes::{BufMut, Bytes, BytesMut};
use http_body_util::BodyExt;
use hyper::{body::Incoming, Response, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo};
use prost::Message;
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

pub fn start(generation: &Arc<Generation>) -> Option<JoinHandle<()>> {
    let endpoints: Vec<_> = generation
        .endpoints
        .values()
        .filter(|e| e.config.health_check.is_some())
        .cloned()
        .collect();
    if endpoints.is_empty() {
        return None;
    }
    Some(tokio::spawn(async move {
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
    }))
}

const GRPC_HEALTH_PATH: &str = "/grpc.health.v1.Health/Check";
const GRPC_MAX_MESSAGE: usize = 1024;
const GRPC_FRAME_PREFIX: usize = 5;

#[derive(Message)]
struct HealthCheckRequest {
    #[prost(string, tag = "1")]
    service: String,
}

#[derive(Message)]
struct HealthCheckResponse {
    #[prost(int32, tag = "1")]
    status: i32,
}

async fn probe(endpoint: &Endpoint, health: &HealthCheck) -> anyhow::Result<bool> {
    let io: Box<dyn Io> = if let Some(path) = endpoint.config.address.strip_prefix("unix:") {
        Box::new(UnixStream::connect(path).await?)
    } else {
        let tcp = match endpoint.resolved_address().await? {
            Some((address, _)) => TcpStream::connect(address).await?,
            None => TcpStream::connect(&endpoint.config.address).await?,
        };
        if let Some(config) = &endpoint.config.tls {
            let client = endpoint
                .tls_client
                .clone()
                .ok_or_else(|| anyhow::anyhow!("missing TLS client"))?;
            let stream = tokio_rustls::TlsConnector::from(client)
                .connect(tls::server_name(config)?, tcp)
                .await?;
            if endpoint.config.protocol == UpstreamProtocol::Http2 {
                anyhow::ensure!(
                    stream.get_ref().1.alpn_protocol() == Some(b"h2"),
                    "HTTP/2 health probe requires h2 ALPN"
                );
            }
            Box::new(stream)
        } else {
            Box::new(tcp)
        }
    };
    if endpoint.config.protocol == UpstreamProtocol::Http2 {
        return probe_h2(io, endpoint, health).await;
    }
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

async fn probe_h2(
    io: Box<dyn Io>,
    endpoint: &Endpoint,
    health: &HealthCheck,
) -> anyhow::Result<bool> {
    let scheme = if endpoint.config.tls.is_some() {
        "https"
    } else {
        "http"
    };
    let is_grpc = health.mode == HealthMode::Grpc;
    let mut request = hyper::Request::builder();
    let (path, body) = if is_grpc {
        let message = HealthCheckRequest {
            service: health.service.clone().unwrap_or_default(),
        };
        let length = message.encoded_len();
        let mut body = BytesMut::with_capacity(GRPC_FRAME_PREFIX + length);
        body.put_u8(0); // Uncompressed message.
        body.put_u32(length as u32);
        message.encode(&mut body)?;
        request = request
            .method("POST")
            .header("content-type", "application/grpc")
            .header("te", "trailers")
            .header("grpc-timeout", format!("{}m", health.timeout_ms));
        (GRPC_HEALTH_PATH, body.freeze())
    } else {
        (health.path.as_str(), Bytes::new())
    };
    let uri = hyper::Uri::builder()
        .scheme(scheme)
        .authority(endpoint.config.host.as_str())
        .path_and_query(path)
        .build()?;
    let request = request.uri(uri).body(crate::body::full(body))?;
    let (mut sender, connection) = hyper::client::conn::http2::Builder::new(TokioExecutor::new())
        .initial_max_send_streams(Some(0))
        .max_header_list_size(8192)
        .initial_stream_window_size(4096)
        .handshake(TokioIo::new(io))
        .await?;
    let exchange = async {
        let response = sender.send_request(request).await?;
        if is_grpc {
            grpc_serving(response).await
        } else {
            Ok(response.status().is_success())
        }
    };
    tokio::pin!(connection, exchange);
    tokio::select! {
        biased;
        done = &mut exchange => done,
        result = &mut connection => {
            result?;
            exchange.await
        }
    }
}

async fn grpc_serving(response: Response<Incoming>) -> anyhow::Result<bool> {
    let grpc_type = response
        .headers()
        .get(hyper::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|media_type| {
            matches!(
                media_type.trim(),
                "application/grpc" | "application/grpc+proto"
            )
        });
    if response.status() != StatusCode::OK
        || !grpc_type
        || response.headers().contains_key("grpc-status")
    {
        return Ok(false);
    }

    let mut body = response.into_body();
    let mut message = [0; GRPC_FRAME_PREFIX + GRPC_MAX_MESSAGE];
    let mut used = 0;
    let mut ok_status = false;
    while let Some(frame) = body.frame().await.transpose()? {
        if let Some(data) = frame.data_ref() {
            if data.len() > message.len() - used {
                return Ok(false);
            }
            message[used..used + data.len()].copy_from_slice(data);
            used += data.len();
        }
        if let Some(trailers) = frame.trailers_ref() {
            let mut statuses = trailers.get_all("grpc-status").iter();
            ok_status =
                statuses.next().is_some_and(|value| value == "0") && statuses.next().is_none();
        }
    }
    if !ok_status || used < GRPC_FRAME_PREFIX || message[0] != 0 {
        return Ok(false);
    }
    let declared = u32::from_be_bytes(message[1..GRPC_FRAME_PREFIX].try_into().unwrap());
    if declared as usize != used - GRPC_FRAME_PREFIX {
        return Ok(false);
    }
    let response = HealthCheckResponse::decode(&message[GRPC_FRAME_PREFIX..used]);
    // ServingStatus.SERVING = 1 in grpc/health/v1/health.proto.
    Ok(response.is_ok_and(|message| message.status == 1))
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
            mode: HealthMode::Http,
            service: None,
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
        assert!(start(&default).is_none());
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

    struct TestFrames(
        std::collections::VecDeque<Result<hyper::body::Frame<Bytes>, std::convert::Infallible>>,
    );

    impl hyper::body::Body for TestFrames {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            mut self: std::pin::Pin<&mut Self>,
            _: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Result<hyper::body::Frame<Bytes>, Self::Error>>> {
            std::task::Poll::Ready(self.0.pop_front())
        }
    }

    #[tokio::test]
    async fn http2_and_grpc_probes_validate_responses_over_real_sockets() {
        use http_body_util::{combinators::UnsyncBoxBody, Full};
        use hyper::{body::Frame, service::service_fn};
        use std::convert::Infallible;

        enum Reply {
            Http(u16),
            Grpc(Bytes, Option<&'static str>),
            Stalled,
        }
        for (reply, expected) in [
            (Reply::Http(204), Some(true)),
            (Reply::Http(503), Some(false)),
            (
                Reply::Grpc(
                    Bytes::from_static(b"\x00\x00\x00\x00\x02\x08\x01"),
                    Some("0"),
                ),
                Some(true),
            ),
            (
                Reply::Grpc(
                    Bytes::from_static(b"\x00\x00\x00\x00\x02\x08\x02"),
                    Some("0"),
                ),
                Some(false),
            ),
            (
                Reply::Grpc(
                    Bytes::from_static(b"\x00\x00\x00\x00\x02\x08\x01"),
                    Some("14"),
                ),
                Some(false),
            ),
            (
                Reply::Grpc(Bytes::from_static(b"\x00\x00\x00\x00\x02\x08\x01"), None),
                Some(false),
            ),
            (
                Reply::Grpc(
                    Bytes::from_static(b"\x01\x00\x00\x00\x02\x08\x01"),
                    Some("0"),
                ),
                Some(false),
            ),
            (
                Reply::Grpc(
                    Bytes::from_static(b"\x00\x00\x00\x00\x03\x08\x01"),
                    Some("0"),
                ),
                Some(false),
            ),
            (
                Reply::Grpc(
                    Bytes::from(vec![0; GRPC_MAX_MESSAGE + GRPC_FRAME_PREFIX + 1]),
                    Some("0"),
                ),
                Some(false),
            ),
            (Reply::Stalled, None),
        ] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let mut config = generation(listener.local_addr().unwrap().to_string())
                .config
                .clone();
            let grpc = !matches!(reply, Reply::Http(_));
            config.upstreams[0].protocol = UpstreamProtocol::Http2;
            let health = config.upstreams[0].health_check.as_mut().unwrap();
            if grpc {
                health.mode = HealthMode::Grpc;
                health.path.clear();
                health.service = Some("ready.service".into());
            }
            config.validate().unwrap();
            let generation = Generation::new(config, 1, &HashMap::new());
            let e = &generation.endpoints[&generation.config.upstreams[0].id];
            let authority = e.config.host.clone();
            let reply = Arc::new(reply);
            let server = tokio::spawn(async move {
                let (socket, _) = listener.accept().await.unwrap();
                let service = service_fn(move |request: hyper::Request<Incoming>| {
                    let reply = reply.clone();
                    let authority = authority.clone();
                    async move {
                        assert_eq!(request.uri().authority().unwrap().as_str(), authority);
                        assert_eq!(request.uri().scheme_str(), Some("http"));
                        let (parts, body) = request.into_parts();
                        let response = match &*reply {
                            Reply::Http(status) => {
                                assert_eq!(parts.method, hyper::Method::GET);
                                assert_eq!(parts.uri.path(), "/ready");
                                Response::builder()
                                    .status(*status)
                                    .body(Full::new(Bytes::new()).boxed_unsync())
                                    .unwrap()
                            }
                            Reply::Grpc(message, status) => {
                                assert_eq!(parts.method, hyper::Method::POST);
                                assert_eq!(parts.uri.path(), GRPC_HEALTH_PATH);
                                assert_eq!(parts.headers["content-type"], "application/grpc");
                                assert_eq!(parts.headers["grpc-timeout"], "50m");
                                let body = body.collect().await.unwrap().to_bytes();
                                assert_eq!(&body[..5], b"\x00\x00\x00\x00\x0f");
                                assert_eq!(&body[5..], b"\x0a\x0dready.service");
                                let mut frames = std::collections::VecDeque::from([
                                    Ok(Frame::data(message.slice(..3))),
                                    Ok(Frame::data(message.slice(3..))),
                                ]);
                                if let Some(status) = status {
                                    let mut trailers = hyper::HeaderMap::new();
                                    trailers.insert(
                                        "grpc-status",
                                        hyper::header::HeaderValue::from_static(status),
                                    );
                                    frames.push_back(Ok(Frame::trailers(trailers)));
                                }
                                Response::builder()
                                    .header("content-type", "application/grpc+proto")
                                    .body(TestFrames(frames).boxed_unsync())
                                    .unwrap()
                            }
                            Reply::Stalled => {
                                std::future::pending::<Response<UnsyncBoxBody<Bytes, Infallible>>>()
                                    .await
                            }
                        };
                        Ok::<_, Infallible>(response)
                    }
                });
                let server = hyper::server::conn::http2::Builder::new(TokioExecutor::new());
                server.serve_connection(TokioIo::new(socket), service).await
            });
            let actual = tokio::time::timeout(
                Duration::from_millis(if expected.is_some() { 1000 } else { 75 }),
                probe(e, e.config.health_check.as_ref().unwrap()),
            )
            .await;
            match expected {
                Some(expected) => assert_eq!(actual.unwrap().unwrap(), expected),
                None => assert!(actual.is_err()),
            }
            // A cancelled probe must close its socket as well as stop polling.
            assert!(tokio::time::timeout(Duration::from_secs(1), server)
                .await
                .unwrap()
                .is_ok());
        }
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
        assert!(g.select(&route, "127.0.0.1".parse().unwrap()).is_none());
        let runner = start(&g).unwrap();
        for expected in [true, false, true] {
            ready.store(expected, Ordering::Release);
            tokio::time::timeout(Duration::from_secs(3), async {
                while e.active_healthy.load(Ordering::Acquire) != expected {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            assert_eq!(
                g.select(&route, "127.0.0.1".parse().unwrap()).is_some(),
                expected
            );
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
