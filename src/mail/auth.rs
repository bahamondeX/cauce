use super::{config::Protocol, wire::Credentials};
use anyhow::{ensure, Context, Result};
use hyper::header::{HeaderMap, HeaderValue};
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};

pub(super) enum Decision {
    Reject,
    Connect(SocketAddr),
}

pub(super) struct Service {
    client: reqwest::Client,
    url: reqwest::Url,
}

impl Service {
    pub(super) fn new(url: reqwest::Url, timeout: Duration) -> Result<Self> {
        let client = reqwest::Client::builder()
            .no_proxy()
            .http1_only()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .timeout(timeout)
            .pool_max_idle_per_host(0)
            .build()?;
        Ok(Self { client, url })
    }

    pub(super) async fn check(
        &self,
        protocol: Protocol,
        peer: IpAddr,
        helo: Option<&str>,
        number: u8,
        credentials: &Credentials,
    ) -> Result<Decision> {
        let mut request = self
            .client
            .get(self.url.clone())
            .version(hyper::Version::HTTP_10)
            .header("Auth-Method", "plain")
            .header("Auth-User", sensitive(&credentials.user)?)
            .header("Auth-Pass", sensitive(&credentials.password)?)
            .header("Auth-Protocol", protocol.name())
            .header("Auth-Login-Attempt", number.to_string())
            .header("Client-IP", peer.to_string())
            .header("Auth-SSL", "on");
        if let Some(helo) = helo {
            request = request.header("Auth-SMTP-Helo", helo);
        }
        let response = request
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("mail auth service request failed"))?;
        ensure!(
            response.status() == hyper::StatusCode::OK,
            "mail auth service HTTP status"
        );
        let headers = response.headers();
        ensure!(
            headers.len() <= 64
                && headers
                    .iter()
                    .map(|(name, value)| name.as_str().len() + value.len())
                    .sum::<usize>()
                    <= 8192,
            "mail auth service header limit"
        );
        if field(headers, "auth-status")? != "OK" {
            return Ok(Decision::Reject);
        }
        let ip: IpAddr = field(headers, "auth-server")?.parse()?;
        let port: u16 = field(headers, "auth-port")?.parse()?;
        ensure!(port != 0, "invalid mail backend port");
        Ok(Decision::Connect(SocketAddr::new(ip, port)))
    }
}

fn field<'a>(headers: &'a HeaderMap, name: &'static str) -> Result<&'a str> {
    let mut values = headers.get_all(name).iter();
    let value = values
        .next()
        .context("mail auth service field missing")?
        .to_str()?;
    ensure!(values.next().is_none(), "duplicate mail auth service field");
    Ok(value)
}

fn sensitive(value: &str) -> Result<HeaderValue> {
    // NGINX's mail auth escaping encodes space, percent and ASCII controls.
    // Credentials are already restricted to printable ASCII by the mail parser.
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            ' ' => escaped.push_str("%20"),
            '%' => escaped.push_str("%25"),
            _ => escaped.push(ch),
        }
    }
    let mut header = HeaderValue::from_str(&escaped)?;
    header.set_sensitive(true);
    Ok(header)
}
