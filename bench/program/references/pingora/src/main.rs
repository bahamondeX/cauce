use async_trait::async_trait;
use pingora::{
    http::RequestHeader,
    proxy::{http_proxy_service, ProxyHttp, Session},
    server::{configuration::ServerConf, Server},
    upstreams::peer::HttpPeer,
    Result,
};
use std::{net::SocketAddr, sync::Arc, time::Duration};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

struct Reference {
    upstream: SocketAddr,
    requests: Arc<Semaphore>,
}

#[async_trait]
impl ProxyHttp for Reference {
    type CTX = Option<OwnedSemaphorePermit>;

    fn new_ctx(&self) -> Self::CTX {
        None
    }

    async fn request_filter(&self, session: &mut Session, slot: &mut Self::CTX) -> Result<bool> {
        let request = session.req_header();
        if request.method != "GET"
            || request.headers.contains_key("transfer-encoding")
            || request.headers.contains_key("content-length")
        {
            session.respond_error(400).await?;
            return Ok(true);
        }
        match self.requests.clone().try_acquire_owned() {
            Ok(permit) => {
                *slot = Some(permit);
                Ok(false)
            }
            Err(_) => {
                session.respond_error(503).await?;
                Ok(true)
            }
        }
    }

    async fn upstream_peer(
        &self,
        _session: &mut Session,
        _slot: &mut Self::CTX,
    ) -> Result<Box<HttpPeer>> {
        let mut peer = HttpPeer::new(self.upstream, false, String::new());
        peer.options.set_http_version(1, 1);
        peer.options.connection_timeout = Some(Duration::from_secs(1));
        peer.options.read_timeout = Some(Duration::from_secs(15));
        peer.options.write_timeout = Some(Duration::from_secs(15));
        peer.options.idle_timeout = Some(Duration::from_secs(15));
        Ok(Box::new(peer))
    }

    async fn upstream_request_filter(
        &self,
        _session: &mut Session,
        request: &mut RequestHeader,
        _slot: &mut Self::CTX,
    ) -> Result<()> {
        request.insert_header("Host", "backend.test:8000")
    }
}

fn main() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let [listen, upstream] = args.as_slice() else {
        return Err("usage: cauce-pingora-reference LISTEN_SOCKET UPSTREAM_SOCKET".into());
    };
    let listen = listen.parse::<SocketAddr>()?;
    let reference = Reference {
        upstream: upstream.parse()?,
        requests: Arc::new(Semaphore::new(32)),
    };
    let configuration = ServerConf {
        threads: 1,
        upstream_keepalive_pool_size: 32,
        max_retries: 1,
        ..ServerConf::default()
    };
    let mut server = Server::new_with_opt_and_conf(None, configuration);
    server.bootstrap();
    let mut proxy = http_proxy_service(&server.configuration, reference);
    proxy.add_tcp(&listen.to_string());
    server.add_service(proxy);
    server.run_forever();
}
