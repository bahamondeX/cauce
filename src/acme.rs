//! ACME orders and certificate storage live outside ordinary request processing.
use crate::acme_config::Settings;
use anyhow::{ensure, Context, Result};
use instant_acme::{
    Account, AccountCredentials, AuthorizationStatus, ChallengeType, Identifier, NewAccount,
    NewOrder, OrderStatus, RetryPolicy,
};
use rustls::{
    pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer},
    server::{ClientHello, ResolvesServerCertUsingSni},
    sign::CertifiedKey,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::{ErrorKind, Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{Arc, RwLock},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const FILE_MAX: u64 = 1_048_576;
const CHALLENGE_PREFIX: &str = "/.well-known/acme-challenge/";

struct PrivateState {
    directory: PathBuf,
    _lock: File,
}

impl PrivateState {
    fn open(directory: &Path) -> Result<Self> {
        match fs::DirBuilder::new().mode(0o700).create(directory) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
        let directory_meta = fs::symlink_metadata(directory)?;
        ensure!(
            directory_meta.is_dir()
                && directory_meta.mode() & 0o077 == 0
                && directory_meta.uid() == unsafe { libc::geteuid() },
            "ACME state requires an owned private directory"
        );
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(directory.join("lock"))?;
        Self::validate_file(&lock)?;
        fs2::FileExt::try_lock_exclusive(&lock).context("ACME state is already in use")?;
        for entry in fs::read_dir(directory)?.take(1024) {
            let entry = entry?;
            if entry
                .file_name()
                .to_str()
                .and_then(|name| name.strip_prefix("pending-"))
                .is_some_and(|name| uuid::Uuid::parse_str(name).is_ok())
                && entry.file_type()?.is_file()
            {
                fs::remove_file(entry.path())?;
            }
        }
        Ok(Self {
            directory: directory.to_owned(),
            _lock: lock,
        })
    }

    fn validate_file(file: &File) -> Result<()> {
        let meta = file.metadata()?;
        ensure!(
            meta.is_file()
                && meta.uid() == unsafe { libc::geteuid() }
                && meta.mode() & 0o077 == 0
                && meta.nlink() == 1
                && meta.len() <= FILE_MAX,
            "invalid ACME state file permissions or size"
        );
        Ok(())
    }

    fn read<T: serde::de::DeserializeOwned>(&self, name: &str) -> Result<Option<T>> {
        let file = match OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(self.directory.join(name))
        {
            Ok(file) => file,
            Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        Self::validate_file(&file)?;
        let mut data = Vec::new();
        file.take(FILE_MAX + 1).read_to_end(&mut data)?;
        ensure!(data.len() as u64 <= FILE_MAX, "ACME state grew during read");
        serde_json::from_slice(&data)
            .map(Some)
            .map_err(|_| anyhow::anyhow!("invalid ACME state"))
    }

    fn write<T: Serialize>(&self, name: &str, data: &T) -> Result<()> {
        let data = serde_json::to_vec(data)?;
        ensure!(data.len() as u64 <= FILE_MAX, "ACME state exceeds quota");
        let pending = self
            .directory
            .join(format!("pending-{}", uuid::Uuid::new_v4()));
        let result = (|| {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&pending)?;
            file.write_all(&data)?;
            file.sync_all()?;
            fs::rename(&pending, self.directory.join(name))?;
            File::open(&self.directory)?.sync_all()
        })();
        if result.is_err() {
            let _ = fs::remove_file(pending);
        }
        result?;
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
struct StoredAccount {
    directory: String,
    credentials: AccountCredentials,
}

#[derive(Serialize, Deserialize)]
struct StoredCertificate {
    names: Vec<String>,
    chain: String,
    private_key: String,
}

struct Certificate {
    key: Arc<CertifiedKey>,
    not_after: u64,
    renew_at: u64,
}

fn now_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

impl Certificate {
    fn load(stored: &StoredCertificate, names: &[String], renew_before: u64) -> Result<Self> {
        ensure!(stored.names == names, "ACME certificate names changed");
        let certificates = CertificateDer::pem_slice_iter(stored.chain.as_bytes())
            .collect::<Result<Vec<_>, _>>()?;
        let leaf = certificates
            .first()
            .context("empty ACME certificate chain")?;
        let (_, parsed) = x509_parser::parse_x509_certificate(leaf.as_ref())
            .map_err(|_| anyhow::anyhow!("invalid ACME leaf certificate"))?;
        let valid_from: u64 = parsed.validity().not_before.timestamp().try_into()?;
        let not_after: u64 = parsed.validity().not_after.timestamp().try_into()?;
        let now = now_seconds();
        ensure!(
            valid_from <= now && now < not_after,
            "ACME certificate outside its validity period"
        );
        let renew_at = not_after - renew_before.min((not_after - valid_from) / 3);
        let private_key = PrivateKeyDer::from_pem_slice(stored.private_key.as_bytes())
            .context("missing ACME certificate key")?;
        let key = CertifiedKey::from_der(
            certificates,
            private_key,
            &rustls::crypto::ring::default_provider(),
        )?;
        let mut sanity = ResolvesServerCertUsingSni::new();
        for name in names {
            sanity.add(name, key.clone()).map_err(|_| {
                anyhow::anyhow!("ACME certificate does not cover its configured names")
            })?;
        }
        Ok(Self {
            key: Arc::new(key),
            not_after,
            renew_at,
        })
    }
}

pub(crate) struct Runtime {
    settings: Settings,
    state: Arc<PrivateState>,
    certificates: RwLock<Vec<Option<Certificate>>>,
    challenges: RwLock<HashMap<(String, String), String>>,
}

impl std::fmt::Debug for Runtime {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str("ACME certificate manager")
    }
}

impl crate::tls::ManagedResolver for Runtime {
    fn resolve_managed(&self, hello: &ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        let name = hello.server_name()?;
        let now = now_seconds();
        let current = self.certificates.read().unwrap();
        self.settings
            .certificates
            .iter()
            .zip(current.iter())
            .filter(|(names, _)| names.iter().any(|candidate| candidate == name))
            .filter_map(|(_, candidate)| candidate.as_ref())
            .find(|candidate| {
                candidate.not_after > now
                    && candidate
                        .key
                        .key
                        .choose_scheme(hello.signature_schemes())
                        .is_some()
            })
            .map(|candidate| candidate.key.clone())
    }
}

struct OrderChallenges<'a>(&'a Runtime);

impl Drop for OrderChallenges<'_> {
    fn drop(&mut self) {
        self.0.challenges.write().unwrap().clear();
    }
}

impl Runtime {
    pub(crate) async fn prepare(settings: Settings) -> Result<Arc<Self>> {
        tokio::task::spawn_blocking(move || {
            let state = Arc::new(PrivateState::open(&settings.state_dir).context("prepare ACME state")?);
            let certificates = settings.certificates.iter().enumerate().map(|(index, names)| {
                let loaded = state.read::<StoredCertificate>(&format!("certificate-{index}.json"))
                    .and_then(|stored| stored.map(|stored|
                        Certificate::load(&stored, names, settings.renew_before_seconds)).transpose());
                match loaded {
                    Ok(certificate) => certificate,
                    Err(_) => {
                        eprintln!("{{\"event\":\"acme_stored_certificate_unavailable\",\"group\":{index}}}");
                        None
                    }
                }
            }).collect();
            Ok(Arc::new(Self {
                settings,
                state,
                certificates: RwLock::new(certificates),
                challenges: RwLock::new(HashMap::new()),
            }))
        }).await?
    }

    pub(crate) fn challenge(&self, authority: &str, path: &str) -> Option<String> {
        let token = path.strip_prefix(CHALLENGE_PREFIX)?;
        let host = authority.parse::<hyper::http::uri::Authority>().ok()?;
        if !valid_token(token) {
            return None;
        }
        self.challenges
            .read()
            .unwrap()
            .get(&(host.host().to_ascii_lowercase(), token.to_owned()))
            .cloned()
    }

    async fn account(&self) -> Result<Account> {
        let state = self.state.clone();
        let root = self.settings.directory_ca_file.clone();
        let (builder, stored) = tokio::task::spawn_blocking(move || -> Result<_> {
            let builder = match root {
                Some(root) => Account::builder_with_root(root)?,
                None => Account::builder()?,
            };
            Ok((builder, state.read::<StoredAccount>("account.json")?))
        })
        .await??;
        if let Some(stored) = stored {
            ensure!(
                stored.directory == self.settings.directory,
                "ACME state belongs to a different directory"
            );
            return Ok(builder.from_credentials(stored.credentials).await?);
        }
        let contacts: Vec<_> = self.settings.contact.iter().map(String::as_str).collect();
        let (account, credentials) = builder
            .create(
                &NewAccount {
                    contact: &contacts,
                    terms_of_service_agreed: true,
                    only_return_existing: false,
                },
                self.settings.directory.clone(),
                None,
            )
            .await?;
        let record = StoredAccount {
            directory: self.settings.directory.clone(),
            credentials,
        };
        let state = self.state.clone();
        tokio::task::spawn_blocking(move || state.write("account.json", &record)).await??;
        Ok(account)
    }

    async fn order(&self, account: &Account, index: usize) -> Result<()> {
        let names = &self.settings.certificates[index];
        let ids: Vec<_> = names.iter().cloned().map(Identifier::Dns).collect();
        let _clear = OrderChallenges(self);
        let mut order = account.new_order(&NewOrder::new(&ids)).await?;
        let mut authorizations = order.authorizations();
        while let Some(authorization) = authorizations.next().await {
            let mut auth = authorization?;
            match auth.status {
                AuthorizationStatus::Valid => continue,
                AuthorizationStatus::Pending => {}
                _ => anyhow::bail!("ACME authorization rejected"),
            }
            let mut challenge = auth
                .challenge(ChallengeType::Http01)
                .context("ACME HTTP-01 unavailable")?;
            let name = challenge.identifier().to_string();
            ensure!(
                names.contains(&name) && valid_token(&challenge.token),
                "unexpected ACME authorization"
            );
            let key = challenge.key_authorization().as_str().to_owned();
            self.challenges
                .write()
                .unwrap()
                .insert((name, challenge.token.clone()), key);
            challenge.set_ready().await?;
        }
        let retry = RetryPolicy::default();
        ensure!(
            order.poll_ready(&retry).await? == OrderStatus::Ready,
            "ACME order was rejected"
        );
        let private_key = order.finalize().await?;
        let chain = order.poll_certificate(&retry).await?;
        let stored = StoredCertificate {
            names: names.clone(),
            chain,
            private_key,
        };
        let state = self.state.clone();
        let renew = self.settings.renew_before_seconds;
        let certificate = tokio::task::spawn_blocking(move || -> Result<_> {
            let certificate = Certificate::load(&stored, &stored.names, renew)?;
            state.write(&format!("certificate-{index}.json"), &stored)?;
            Ok(certificate)
        })
        .await??;
        let expires = certificate.not_after;
        self.certificates.write().unwrap()[index] = Some(certificate);
        eprintln!("{{\"event\":\"acme_certificate_published\",\"group\":{index},\"expires_at_unix\":{expires}}}");
        Ok(())
    }

    pub(crate) fn next_expiration(&self) -> u64 {
        self.certificates
            .read()
            .unwrap()
            .iter()
            .map(|cert| cert.as_ref().map_or(0, |cert| cert.not_after))
            .min()
            .unwrap_or(0)
    }

    pub(crate) async fn serve(self: Arc<Self>) {
        let mut account = None;
        let mut failure_backoff = 5;
        loop {
            let now = now_seconds();
            let due: Vec<_> = self
                .certificates
                .read()
                .unwrap()
                .iter()
                .enumerate()
                .filter(|(_, cert)| cert.as_ref().is_none_or(|cert| cert.renew_at <= now))
                .map(|(index, _)| index)
                .collect();
            let mut failed = false;
            for index in due {
                let operation = async {
                    if account.is_none() {
                        account = Some(self.account().await?);
                    }
                    self.order(account.as_ref().unwrap(), index).await
                };
                if !tokio::time::timeout(Duration::from_secs(180), operation)
                    .await
                    .is_ok_and(|result| result.is_ok())
                {
                    failed = true;
                    eprintln!("{{\"event\":\"acme_renewal_failed\",\"group\":{index}}}");
                }
            }
            let seconds = if failed {
                let delay = failure_backoff;
                failure_backoff = (failure_backoff * 2).min(300);
                delay
            } else {
                failure_backoff = 5;
                self.settings.check_interval_seconds
            };
            tokio::time::sleep(Duration::from_secs(seconds)).await;
        }
    }
}

fn valid_token(token: &str) -> bool {
    (16..=128).contains(&token.len())
        && token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}
