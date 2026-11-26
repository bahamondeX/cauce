//! A bounded durable second tier for the shared HTTP cache policy.
use super::{Entry, Family, Key, Meta};
use anyhow::{ensure, Context, Result};
use bytes::Bytes;
use hyper::{
    header::{HeaderMap, HeaderName, HeaderValue},
    StatusCode,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    fs::{self, File, OpenOptions},
    io::{ErrorKind, Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Semaphore;

const META_MAX: usize = 128 * 1024;
const SCAN_MAX: usize = 65_536;
const SUFFIX: &str = ".cauce-object";

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub(crate) directory: PathBuf,
    #[serde(deserialize_with = "crate::config::bytes")]
    pub(crate) max_size: u64,
}

impl Settings {
    pub(crate) fn validate(&self, max_entry: u64) -> Result<()> {
        ensure!(
            self.directory.is_absolute()
                && self.directory.components().all(|part| matches!(
                    part,
                    std::path::Component::RootDir | std::path::Component::Normal(_)
                ))
                && (max_entry..=64 * (1 << 30)).contains(&self.max_size),
            "invalid disk cache directory or quota"
        );
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
struct StoredMeta {
    key: Key,
    status: u16,
    headers: Vec<(String, Vec<u8>)>,
    stored_at: u64,
    age_ms: u64,
    fresh_ms: u64,
    swr_ms: u64,
    sie_ms: u64,
}

struct Record {
    name: String,
    bytes: u64,
    sequence: u64,
}

#[derive(Default)]
struct Index {
    bytes: u64,
    objects: HashMap<Arc<Key>, Record>,
    order: VecDeque<Arc<Key>>,
    closed: bool,
    unsafe_shutdown: bool,
}

pub(super) struct Disk {
    directory: PathBuf,
    fingerprint: String,
    quota: u64,
    max_object: usize,
    max_entries: usize,
    index: Mutex<Index>,
    io: Arc<Semaphore>,
    epoch: AtomicU64,
    pending_invalidations: AtomicUsize,
    sequence: AtomicU64,
    _locked: File,
}

#[derive(Clone, Copy)]
pub(super) struct Fence {
    epoch: u64,
    sequence: u64,
}

fn wall_ms() -> Option<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()?
        .as_millis()
        .try_into()
        .ok()
}

pub(super) fn fingerprint(config: &[u8]) -> String {
    format!("{:x}", Sha256::digest(config))
}

fn entry_name(key: &Key) -> String {
    format!(
        "{}{SUFFIX}",
        fingerprint(&serde_json::to_vec(key).expect("cache key JSON"))
    )
}

fn owned(name: &str, suffix: &str) -> bool {
    name.strip_suffix(suffix)
        .is_some_and(|hash| hash.len() == 64 && hash.bytes().all(|c| c.is_ascii_hexdigit()))
}

fn private_read(path: &Path, max: u64) -> Result<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let meta = file.metadata()?;
    ensure!(
        meta.is_file()
            && meta.len() <= max
            && meta.nlink() == 1
            && meta.uid() == unsafe { libc::geteuid() }
            && meta.mode() & 0o077 == 0,
        "invalid disk cache file"
    );
    let mut bytes = Vec::new();
    file.take(max + 1).read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64 <= max, "disk cache file exceeded size");
    Ok(bytes)
}

fn decode(data: &[u8], max_body: usize) -> Option<(StoredMeta, Arc<Entry>)> {
    let (verified, expected) = data.split_at_checked(data.len().checked_sub(32)?)?;
    if Sha256::digest(verified).as_slice() != expected {
        return None;
    }
    let (size, rest) = verified.split_at_checked(4)?;
    let size = u32::from_be_bytes(size.try_into().ok()?) as usize;
    if size > META_MAX {
        return None;
    }
    let (metadata, body) = rest.split_at_checked(size)?;
    if body.len() > max_body {
        return None;
    }
    let stored: StoredMeta = serde_json::from_slice(metadata).ok()?;
    if stored.key.bytes() > 4 * super::MAX_KEY_BYTES || stored.headers.len() > 256 {
        return None;
    }
    let extra_age = wall_ms()?.checked_sub(stored.stored_at)?;
    let age = stored.age_ms.checked_add(extra_age)?;
    if age
        >= stored
            .fresh_ms
            .saturating_add(stored.swr_ms.max(stored.sie_ms))
    {
        return None;
    }
    let status = StatusCode::from_u16(stored.status).ok()?;
    let mut headers = HeaderMap::with_capacity(stored.headers.len());
    for (name, value) in &stored.headers {
        headers.append(
            HeaderName::from_bytes(name.as_bytes()).ok()?,
            HeaderValue::from_bytes(value).ok()?,
        );
    }
    let entry = Arc::new(Entry {
        background_after: Mutex::new(None),
        meta: Meta {
            status,
            headers,
            fresh: Duration::from_millis(stored.fresh_ms),
            age: Duration::from_millis(age),
            received: Instant::now(),
            swr: Duration::from_millis(stored.swr_ms),
            sie: Duration::from_millis(stored.sie_ms),
        },
        body: Bytes::copy_from_slice(body),
        capacity: body.len(),
    });
    Some((stored, entry))
}

impl Disk {
    pub(super) fn open(
        settings: &Settings,
        fingerprint: String,
        max_object: usize,
        max_entries: usize,
    ) -> Result<Arc<Self>> {
        match fs::DirBuilder::new()
            .mode(0o700)
            .create(&settings.directory)
        {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
        let meta = fs::symlink_metadata(&settings.directory)?;
        ensure!(
            meta.is_dir() && meta.uid() == unsafe { libc::geteuid() } && meta.mode() & 0o077 == 0,
            "disk cache requires an owned private directory"
        );
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(settings.directory.join("lock"))?;
        let lock_meta = lock.metadata()?;
        ensure!(
            lock_meta.is_file() && lock_meta.nlink() == 1,
            "invalid disk cache lock"
        );
        fs2::FileExt::try_lock_exclusive(&lock).context("disk cache directory already in use")?;
        let clean = settings.directory.join("clean");
        let recover = private_read(&clean, 64).is_ok_and(|bytes| bytes == fingerprint.as_bytes());
        match fs::remove_file(&clean) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        File::open(&settings.directory)?.sync_all()?;
        let disk = Arc::new(Self {
            directory: settings.directory.clone(),
            fingerprint,
            quota: settings.max_size,
            max_object,
            max_entries,
            index: Mutex::new(Index::default()),
            io: Arc::new(Semaphore::new(8)),
            epoch: AtomicU64::new(0),
            pending_invalidations: AtomicUsize::new(0),
            sequence: AtomicU64::new(1),
            _locked: lock,
        });
        disk.rebuild(recover)?;
        Ok(disk)
    }

    fn max_file(&self) -> u64 {
        (self.max_object + META_MAX + 36) as u64
    }

    fn rebuild(&self, recover: bool) -> Result<()> {
        let mut index = self.index.lock().unwrap();
        for (ordinal, item) in fs::read_dir(&self.directory)?.enumerate() {
            ensure!(
                ordinal < SCAN_MAX,
                "disk cache directory exceeds scan budget"
            );
            let item = item?;
            let name = item.file_name().to_string_lossy().into_owned();
            if !owned(&name, SUFFIX) && !owned(&name, ".cauce-part") {
                continue;
            }
            if recover && owned(&name, SUFFIX) {
                if let Ok(bytes) = private_read(&item.path(), self.max_file()) {
                    if let Some((entry, _)) = decode(&bytes, self.max_object) {
                        if name == entry_name(&entry.key)
                            && index.bytes + bytes.len() as u64 <= self.quota
                            && index.objects.len() < self.max_entries
                        {
                            let key = Arc::new(entry.key);
                            index.bytes += bytes.len() as u64;
                            index.order.push_back(key.clone());
                            index.objects.insert(
                                key,
                                Record {
                                    name,
                                    bytes: bytes.len() as u64,
                                    sequence: 0,
                                },
                            );
                            continue;
                        }
                    }
                }
            }
            fs::remove_file(item.path())?;
        }
        Ok(())
    }

    pub(super) fn fence(&self) -> Fence {
        Fence {
            epoch: self.epoch.load(Ordering::Acquire),
            sequence: self.sequence.fetch_add(1, Ordering::Relaxed),
        }
    }

    pub(super) async fn load(self: &Arc<Self>, key: Arc<Key>, epoch: Fence) -> Option<Arc<Entry>> {
        let permit = self.io.clone().try_acquire_owned().ok()?;
        let disk = self.clone();
        tokio::task::spawn_blocking(move || {
            let _permit = permit;
            let mut index = disk.index.lock().unwrap();
            if index.closed
                || disk.pending_invalidations.load(Ordering::Acquire) != 0
                || disk.epoch.load(Ordering::Acquire) != epoch.epoch
            {
                return None;
            }
            let record = index.objects.get(&key)?;
            let body = private_read(&disk.directory.join(&record.name), disk.max_file()).ok();
            if let Some((stored, entry)) = body
                .as_ref()
                .and_then(|bytes| decode(bytes, disk.max_object))
            {
                if stored.key == *key {
                    return (disk.epoch.load(Ordering::Acquire) == epoch.epoch).then_some(entry);
                }
            }
            let _ = disk.remove(&mut index, &key);
            None
        })
        .await
        .ok()
        .flatten()
    }

    pub(super) fn enqueue(self: &Arc<Self>, key: Arc<Key>, entry: Arc<Entry>, fence: Fence) {
        let Ok(permit) = self.io.clone().try_acquire_owned() else {
            return;
        };
        let disk = self.clone();
        tokio::task::spawn_blocking(move || {
            let _permit = permit;
            if disk.save(key, &entry, fence).is_err() {
                eprintln!("{{\"event\":\"disk_cache_write_failed\"}}");
            }
        });
    }

    fn save(&self, key: Arc<Key>, entry: &Entry, fence: Fence) -> Result<()> {
        let stored = StoredMeta {
            key: (*key).clone(),
            status: entry.meta.status.as_u16(),
            stored_at: wall_ms().context("disk cache clock")?,
            age_ms: entry.age(Instant::now()).as_millis().min(u64::MAX as u128) as u64,
            fresh_ms: entry.meta.fresh.as_millis() as u64,
            swr_ms: entry.meta.swr.as_millis() as u64,
            sie_ms: entry.meta.sie.as_millis() as u64,
            headers: entry
                .meta
                .headers
                .iter()
                .map(|(name, value)| (name.as_str().to_owned(), value.as_bytes().to_vec()))
                .collect(),
        };
        let metadata = serde_json::to_vec(&stored)?;
        let length = 36 + metadata.len() + entry.body.len();
        if metadata.len() > META_MAX || length as u64 > self.quota {
            return Ok(());
        }
        let mut index = self.index.lock().unwrap();
        if index.closed
            || self.pending_invalidations.load(Ordering::Acquire) != 0
            || self.epoch.load(Ordering::Acquire) != fence.epoch
            || index
                .objects
                .get(&key)
                .is_some_and(|record| record.sequence >= fence.sequence)
        {
            return Ok(());
        }
        // Count both the old object and the pending write against the quota.
        while index.bytes + length as u64 > self.quota
            || (index.objects.len() >= self.max_entries && !index.objects.contains_key(&key))
        {
            let eldest = index
                .order
                .front()
                .cloned()
                .context("disk cache eviction candidate")?;
            self.remove(&mut index, &eldest)?;
        }
        let name = entry_name(&key);
        let part = self.directory.join(name.replace(SUFFIX, ".cauce-part"));
        let destination = self.directory.join(&name);
        let result = (|| -> Result<()> {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&part)?;
            let length = (metadata.len() as u32).to_be_bytes();
            let mut digest = Sha256::new();
            for bytes in [&length[..], &metadata, &entry.body] {
                file.write_all(bytes)?;
                digest.update(bytes);
            }
            file.write_all(&digest.finalize())?;
            file.sync_all()?;
            fs::rename(&part, destination)?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(part);
        }
        result?;
        if let Some(old) = index.objects.remove(&key) {
            index.bytes -= old.bytes;
            index.order.retain(|candidate| candidate != &key);
        }
        index.bytes += length as u64;
        index.order.push_back(key.clone());
        index.objects.insert(
            key,
            Record {
                name,
                bytes: length as u64,
                sequence: fence.sequence,
            },
        );
        Ok(())
    }

    fn remove(&self, index: &mut Index, key: &Key) -> Result<()> {
        let Some(record) = index.objects.get(key) else {
            return Ok(());
        };
        match fs::remove_file(self.directory.join(&record.name)) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => {
                index.closed = true;
                index.unsafe_shutdown = true;
                return Err(error.into());
            }
        }
        index.bytes -= record.bytes;
        index.objects.remove(key);
        index.order.retain(|candidate| candidate.as_ref() != key);
        Ok(())
    }

    pub(super) fn invalidate(
        self: &Arc<Self>,
        family: Family,
        fence: Option<Fence>,
    ) -> Option<tokio::task::JoinHandle<()>> {
        let disk = self.clone();
        // Stop reads immediately. Enqueued older fills cannot become visible
        // while deletion is waiting for a blocking worker.
        disk.pending_invalidations.fetch_add(1, Ordering::AcqRel);
        match fence {
            Some(fence) => {
                if disk
                    .epoch
                    .compare_exchange(
                        fence.epoch,
                        fence.epoch.wrapping_add(1),
                        Ordering::AcqRel,
                        Ordering::Acquire,
                    )
                    .is_err()
                {
                    disk.pending_invalidations.fetch_sub(1, Ordering::AcqRel);
                    return None;
                }
            }
            None => {
                disk.epoch.fetch_add(1, Ordering::AcqRel);
            }
        }
        Some(tokio::task::spawn_blocking(move || {
            let mut index = disk.index.lock().unwrap();
            let victims: Vec<_> = index
                .objects
                .keys()
                .filter(|key| key.family == family)
                .cloned()
                .collect();
            for key in victims {
                if disk.remove(&mut index, &key).is_err() {
                    break;
                }
            }
            disk.pending_invalidations.fetch_sub(1, Ordering::AcqRel);
        }))
    }

    pub(super) async fn close(self: Arc<Self>) {
        let _ = tokio::task::spawn_blocking(move || {
            let mut index = self.index.lock().unwrap();
            index.closed = true;
            if index.unsafe_shutdown || self.pending_invalidations.load(Ordering::Acquire) != 0 {
                return;
            }
            let path = self.directory.join("clean");
            let result = (|| -> Result<()> {
                let mut marker = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(&path)?;
                marker.write_all(self.fingerprint.as_bytes())?;
                marker.sync_all()?;
                File::open(&self.directory)?.sync_all()?;
                Ok(())
            })();
            if result.is_err() {
                let _ = fs::remove_file(path);
            }
        })
        .await;
    }
}
