use crate::config::{InboundTls, TlsVersion, UpstreamProtocol, UpstreamTls};
use anyhow::{anyhow, bail, Context, Result};
use rustls::{
    client::Resumption,
    pki_types::{CertificateDer, PrivateKeyDer},
    server::{
        ClientHello, NoServerSessionStorage, ResolvesServerCert, ResolvesServerCertUsingSni,
        WebPkiClientVerifier,
    },
    sign::CertifiedKey,
    ClientConfig, RootCertStore, ServerConfig, SupportedProtocolVersion,
};
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

fn cert_chain(path: &str) -> Result<Vec<CertificateDer<'static>>> {
    let bytes = pem(path)?;
    let certificates =
        rustls_pemfile::certs(&mut bytes.as_slice()).collect::<std::io::Result<Vec<_>>>()?;
    if certificates.is_empty() {
        bail!("TLS certificate chain is empty")
    }
    Ok(certificates)
}

fn private_key(path: &str) -> Result<PrivateKeyDer<'static>> {
    let bytes = pem(path)?;
    rustls_pemfile::private_key(&mut bytes.as_slice())?
        .ok_or_else(|| anyhow!("TLS private key is missing or unsupported"))
}

fn trust_roots(path: &str) -> Result<RootCertStore> {
    let mut roots = RootCertStore::empty();
    for cert in cert_chain(path)? {
        roots.add(cert).context("invalid TLS CA certificate")?;
    }
    Ok(roots)
}

const TLS12_ONLY: &[&SupportedProtocolVersion] = &[&rustls::version::TLS12];
const TLS13_ONLY: &[&SupportedProtocolVersion] = &[&rustls::version::TLS13];

fn versions(config: Option<&[TlsVersion]>) -> &[&'static SupportedProtocolVersion] {
    match config {
        Some([TlsVersion::Tls12]) => TLS12_ONLY,
        Some([TlsVersion::Tls13]) => TLS13_ONLY,
        _ => rustls::DEFAULT_VERSIONS,
    }
}

#[derive(Debug)]
struct SniDefault {
    named: ResolvesServerCertUsingSni,
    default: Arc<CertifiedKey>,
}

impl ResolvesServerCert for SniDefault {
    fn resolve(&self, hello: ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        self.named
            .resolve(hello)
            .or_else(|| Some(self.default.clone()))
    }
}

pub fn load_server(config: Option<&InboundTls>, http2: bool) -> Result<Option<Arc<ServerConfig>>> {
    let Some(config) = config else {
        return Ok(None);
    };
    let protocols = if http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    build_server(config, versions(config.versions.as_deref()), protocols)
        .map(|server| Some(Arc::new(server)))
}

pub(crate) fn load_quic(config: &InboundTls) -> Result<ServerConfig> {
    let mut server = build_server(config, TLS13_ONLY, vec![b"h3".to_vec()])?;
    server.max_early_data_size = 0;
    Ok(server)
}

fn build_server(
    config: &InboundTls,
    versions: &[&'static SupportedProtocolVersion],
    alpn_protocols: Vec<Vec<u8>>,
) -> Result<ServerConfig> {
    let certs = cert_chain(&config.cert_file).context("default inbound TLS certificate")?;
    let key = private_key(&config.key_file).context("default inbound TLS private key")?;
    let builder = ServerConfig::builder_with_protocol_versions(versions);
    let builder = if let Some(ca) = &config.client_ca_file {
        let roots = trust_roots(ca).context("inbound client CA bundle")?;
        builder.with_client_cert_verifier(WebPkiClientVerifier::builder(Arc::new(roots)).build()?)
    } else {
        builder.with_no_client_auth()
    };
    let mut server = if config.sni.is_empty() {
        builder
            .with_single_cert(certs, key)
            .context("TLS certificate and private key do not match or are invalid")?
    } else {
        let provider = builder.crypto_provider();
        let default = Arc::new(
            CertifiedKey::from_der(certs, key, provider)
                .context("default TLS certificate and key are incompatible")?,
        );
        let mut named = ResolvesServerCertUsingSni::new();
        for (name, files) in &config.sni {
            let certs = cert_chain(&files.cert_file).context("SNI certificate")?;
            let key = private_key(&files.key_file).context("SNI private key")?;
            let certified = CertifiedKey::from_der(certs, key, provider)
                .context("SNI certificate and key are incompatible")?;
            named
                .add(name, certified)
                .context("SNI certificate does not cover its DNS name")?;
        }
        builder.with_cert_resolver(Arc::new(SniDefault { named, default }))
    };
    if config.session_resumption == Some(false) {
        server.session_storage = Arc::new(NoServerSessionStorage {});
        server.send_tls13_tickets = 0;
    }
    server.alpn_protocols = alpn_protocols;
    Ok(server)
}

pub fn load_client(config: &UpstreamTls, protocol: UpstreamProtocol) -> Result<Arc<ClientConfig>> {
    let roots = trust_roots(&config.ca_file).context("upstream CA bundle")?;
    let builder =
        ClientConfig::builder_with_protocol_versions(versions(config.versions.as_deref()))
            .with_root_certificates(roots);
    let mut client = if let Some(files) = &config.client_certificate {
        let certs = cert_chain(&files.cert_file).context("upstream client certificate")?;
        let key = private_key(&files.key_file).context("upstream client private key")?;
        builder
            .with_client_auth_cert(certs, key)
            .context("upstream client certificate and key are incompatible")?
    } else {
        builder.with_no_client_auth()
    };
    if config.session_resumption == Some(false) {
        client.resumption = Resumption::disabled();
    }
    client.alpn_protocols = vec![match protocol {
        UpstreamProtocol::Http1 => b"http/1.1".to_vec(),
        UpstreamProtocol::Http2 => b"h2".to_vec(),
        _ => anyhow::bail!("application TLS is unavailable"),
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
