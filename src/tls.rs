use crate::config::{InboundTls, UpstreamProtocol, UpstreamTls};
use anyhow::{anyhow, bail, Context, Result};
use rustls::{pki_types::CertificateDer, ClientConfig, RootCertStore, ServerConfig};
use std::{
    fs::File,
    io::{BufReader, Read},
    sync::Arc,
};

fn pem(path: &str) -> Result<Vec<u8>> {
    if !std::fs::metadata(path).context("stat TLS PEM")?.is_file() {
        bail!("TLS PEM path is not a regular file")
    }
    let file = File::open(path).context("open TLS PEM")?;
    if !file.metadata()?.is_file() {
        bail!("TLS PEM path is not a regular file")
    }
    let mut data = Vec::new();
    BufReader::new(file)
        .take(1_048_577)
        .read_to_end(&mut data)?;
    if data.len() > 1_048_576 {
        bail!("TLS PEM exceeds 1 MiB")
    }
    Ok(data)
}

pub fn load_server(config: Option<&InboundTls>, http2: bool) -> Result<Option<Arc<ServerConfig>>> {
    let Some(config) = config else {
        return Ok(None);
    };
    let cert_bytes = pem(&config.cert_file)?;
    let key_bytes = pem(&config.key_file)?;
    let certs = rustls_pemfile::certs(&mut BufReader::new(cert_bytes.as_slice()))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if certs.is_empty() {
        bail!("TLS certificate chain is empty")
    }
    let key = rustls_pemfile::private_key(&mut BufReader::new(key_bytes.as_slice()))?
        .ok_or_else(|| anyhow!("TLS private key is missing or unsupported"))?;
    let mut server = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .context("TLS certificate and private key do not match or are invalid")?;
    server.alpn_protocols = if http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    Ok(Some(Arc::new(server)))
}

pub fn load_client(config: &UpstreamTls, protocol: UpstreamProtocol) -> Result<Arc<ClientConfig>> {
    let ca_bytes = pem(&config.ca_file)?;
    let certs: Vec<CertificateDer<'static>> =
        rustls_pemfile::certs(&mut BufReader::new(ca_bytes.as_slice()))
            .collect::<std::result::Result<Vec<_>, _>>()?;
    if certs.is_empty() {
        bail!("upstream CA bundle is empty")
    }
    let mut roots = RootCertStore::empty();
    for cert in certs {
        roots.add(cert).context("invalid upstream CA certificate")?;
    }
    let mut client = ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    client.alpn_protocols = vec![match protocol {
        UpstreamProtocol::Http1 => b"http/1.1".to_vec(),
        UpstreamProtocol::Http2 => b"h2".to_vec(),
    }];
    Ok(Arc::new(client))
}

pub fn server_name(config: &UpstreamTls) -> Result<rustls::pki_types::ServerName<'static>> {
    config
        .server_name
        .clone()
        .try_into()
        .context("invalid TLS server_name")
}
