//! Optional access-log collector. proposal-http is an opt-in proposal-1 client.

use anyhow::{bail, ensure, Context, Result};
use fs2::FileExt;
use reqwest::{redirect::Policy, Client};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs::{self, File, Metadata, OpenOptions},
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    time::Duration,
};
use time::format_description::well_known::Rfc3339;
use uuid::Uuid;

const PROPOSAL: &str = "proposal-1";
// Atomic log rotation briefly removes a generation name while replacing it.
// Requiring a persisted quiet window makes two very fast directory snapshots
// insufficient evidence that a file was really evicted.
const MISSING_CONFIRMATION_MS: i64 = 50;
const DEFAULT_LINE_MAX: u64 = 262_144;
const DEFAULT_BATCH_MAX: u64 = 524_288;
const DEFAULT_SPOOL_MAX: u64 = 8_388_608;
const DEFAULT_DB_MAX: u64 = 12_582_912;
const HARD_FILES_MAX: u32 = 1024;
const ACK_MAX: usize = 4096;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub collector: CollectorConfig,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CollectorConfig {
    pub access_log: PathBuf,
    pub state_path: PathBuf,
    #[serde(default = "default_spool")]
    pub spool_max_bytes: u64,
    #[serde(default = "default_db")]
    pub db_max_bytes: u64,
    #[serde(default = "default_batch_events")]
    pub batch_max_events: usize,
    #[serde(default = "default_batch")]
    pub batch_max_bytes: u64,
    #[serde(default = "default_line")]
    pub max_line_bytes: u64,
    #[serde(default = "default_files")]
    pub max_files: u32,
    #[serde(default = "default_poll")]
    pub poll_ms: u64,
    #[serde(default = "default_backoff_min")]
    pub backoff_min_ms: u64,
    #[serde(default = "default_backoff_max")]
    pub backoff_max_ms: u64,
    #[serde(default = "default_timeout")]
    pub request_timeout_ms: u64,
    #[serde(default)]
    pub delivery: Delivery,
    pub endpoint: Option<String>,
    pub token_file: Option<PathBuf>,
    pub ca_file: Option<PathBuf>,
}

fn default_spool() -> u64 {
    DEFAULT_SPOOL_MAX
}
fn default_db() -> u64 {
    DEFAULT_DB_MAX
}
fn default_batch_events() -> usize {
    128
}
fn default_batch() -> u64 {
    DEFAULT_BATCH_MAX
}
fn default_line() -> u64 {
    DEFAULT_LINE_MAX
}
fn default_files() -> u32 {
    32
}
fn default_poll() -> u64 {
    500
}
fn default_backoff_min() -> u64 {
    500
}
fn default_backoff_max() -> u64 {
    30_000
}
fn default_timeout() -> u64 {
    10_000
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Delivery {
    #[default]
    Spool,
    ProposalHttp,
}

impl Config {
    pub fn load(path: &Path) -> Result<CollectorConfig> {
        let metadata = fs::symlink_metadata(path)
            .with_context(|| format!("cannot read collector configuration {}", path.display()))?;
        ensure!(
            metadata.is_file() && !metadata.file_type().is_symlink() && metadata.len() <= 65_536,
            "collector configuration must be a regular file no larger than 64 KiB"
        );
        let text = String::from_utf8(read_capped_regular(path, 65_536)?)
            .context("collector configuration must be UTF-8")?;
        let config: Config = toml::from_str(&text).context("invalid collector TOML")?;
        config.collector.validate()?;
        Ok(config.collector)
    }
}

impl CollectorConfig {
    fn validate(&self) -> Result<()> {
        ensure!(
            self.access_log != self.state_path,
            "access_log and state_path must differ"
        );
        ensure!(
            self.spool_max_bytes >= 1024
                && self.db_max_bytes >= self.spool_max_bytes.saturating_add(65_536),
            "db_max_bytes must be at least spool_max_bytes + 65536"
        );
        ensure!(
            self.batch_max_events > 0 && self.batch_max_events <= 4096,
            "batch_max_events must be in 1..=4096"
        );
        ensure!(
            self.batch_max_bytes >= 256 && self.batch_max_bytes <= self.spool_max_bytes,
            "batch_max_bytes must be between 256 and spool_max_bytes"
        );
        ensure!(
            self.max_line_bytes >= 256 && self.max_line_bytes <= self.batch_max_bytes,
            "max_line_bytes must be between 256 and batch_max_bytes"
        );
        ensure!(
            self.max_files > 0 && self.max_files <= HARD_FILES_MAX,
            "max_files must be in 1..=1024"
        );
        ensure!(
            self.poll_ms > 0
                && self.poll_ms <= 60_000
                && self.backoff_min_ms > 0
                && self.backoff_max_ms >= self.backoff_min_ms
                && self.backoff_max_ms <= 3_600_000,
            "poll/backoff durations are invalid"
        );
        ensure!(
            self.request_timeout_ms > 0 && self.request_timeout_ms <= 300_000,
            "request_timeout_ms must be in 1..=300000"
        );
        match self.delivery {
            Delivery::Spool => {
                ensure!(
                    self.endpoint.is_none() && self.token_file.is_none() && self.ca_file.is_none(),
                    "spool mode cannot specify remote transport settings"
                );
            }
            Delivery::ProposalHttp => {
                let endpoint = self
                    .endpoint
                    .as_deref()
                    .context("proposal-http requires endpoint")?;
                let parsed =
                    reqwest::Url::parse(endpoint).context("proposal-http endpoint is invalid")?;
                ensure!(
                    parsed.scheme() == "https"
                        && parsed.host_str().is_some()
                        && parsed.username().is_empty()
                    && parsed.password().is_none()
                    && parsed.query().is_none()
                    && parsed.fragment().is_none(),
                    "proposal-http endpoint must be HTTPS, with no user information, query or fragment"
                );
                ensure!(
                    self.token_file.is_some(),
                    "proposal-http requires token_file"
                );
            }
        }
        Ok(())
    }
}

#[derive(Serialize)]
struct Envelope<'a> {
    contract_version: &'static str,
    batch_id: String,
    events: &'a [Value],
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Ack {
    batch_id: String,
    accepted_count: usize,
}

fn parse_ack(body: &[u8]) -> Result<Ack> {
    // Do not attach serde's error: it can include untrusted field names from
    // the response body, which could contain a reflected bearer credential.
    serde_json::from_slice(body)
        .map_err(|_| anyhow::anyhow!("proposal HTTP returned invalid acknowledgement JSON"))
}

#[derive(Serialize)]
pub struct Status {
    delivery: Delivery,
    pending_batches: u64,
    pending_events: u64,
    pending_bytes: u64,
    oldest_batch_id: Option<String>,
    checkpoints: u64,
    db_bytes: u64,
    db_max_bytes: u64,
    spool_max_bytes: u64,
}

pub struct State {
    db: Connection,
    _lock: File,
    db_path: PathBuf,
    source_path: PathBuf,
}

struct CheckpointFrontier {
    offset: u64,
    size: u64,
    anchor: Vec<u8>,
    modified_ns: i64,
    changed_ns: i64,
    partial_at_eof: bool,
}

impl CheckpointFrontier {
    fn only_partial_tail_removed(&self, size: u64) -> bool {
        self.partial_at_eof
            && size == self.offset
            && size < self.size
            && (self.offset == 0 || !self.anchor.is_empty())
    }
}

impl State {
    pub fn open(cfg: &CollectorConfig) -> Result<Self> {
        let parent = cfg
            .state_path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        fs::create_dir_all(parent)
            .with_context(|| format!("cannot create state directory {}", parent.display()))?;
        let source_path = canonical_target(&cfg.access_log)?;
        let state_path = canonical_target(&cfg.state_path)?;
        ensure!(
            source_path != state_path,
            "collector state and source log resolve to the same path"
        );
        if cfg.access_log.exists() && cfg.state_path.exists() {
            let source_meta = fs::metadata(&cfg.access_log)?;
            let state_meta = fs::metadata(&cfg.state_path)?;
            ensure!(
                (source_meta.dev(), source_meta.ino()) != (state_meta.dev(), state_meta.ino()),
                "collector state and source log are hard links to the same file"
            );
        }
        let lock_path = cfg.state_path.with_extension("lock");
        let mut lock_options = OpenOptions::new();
        lock_options.create(true).read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            lock_options
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .mode(0o600);
        }
        let lock = lock_options
            .open(&lock_path)
            .with_context(|| format!("cannot open collector lock {}", lock_path.display()))?;
        lock.try_lock_exclusive()
            .context("another collector process owns this state directory")?;
        if let Ok(metadata) = fs::metadata(&cfg.state_path) {
            let symlink = fs::symlink_metadata(&cfg.state_path)?;
            ensure!(
                metadata.is_file()
                    && !symlink.file_type().is_symlink()
                    && metadata.len() <= cfg.db_max_bytes,
                "collector database is not a regular file or exceeds db_max_bytes"
            );
        }
        let db = Connection::open(&cfg.state_path).context("cannot open collector database")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&cfg.state_path, fs::Permissions::from_mode(0o600))?;
        }
        db.busy_timeout(Duration::from_secs(2))?;
        db.pragma_update(None, "journal_mode", "DELETE")?;
        let page_size: u64 = db.pragma_query_value(None, "page_size", |r| r.get(0))?;
        ensure!(
            page_size == 4096,
            "collector database page size must be exactly 4096 bytes"
        );
        db.pragma_update(None, "synchronous", "EXTRA")?;
        #[cfg(target_os = "macos")]
        db.pragma_update(None, "fullfsync", "ON")?;
        db.pragma_update(None, "foreign_keys", "ON")?;
        let page_limit = cfg.db_max_bytes / 4096;
        db.pragma_update(None, "max_page_count", page_limit)?;
        let actual_limit: u64 = db.pragma_query_value(None, "max_page_count", |row| row.get(0))?;
        ensure!(
            actual_limit.saturating_mul(4096) <= cfg.db_max_bytes,
            "SQLite page size prevents enforcing db_max_bytes"
        );
        db.execute_batch(
            "CREATE TABLE IF NOT EXISTS checkpoints(
                dev INTEGER NOT NULL, ino INTEGER NOT NULL, path TEXT NOT NULL,
                offset INTEGER NOT NULL CHECK(offset >= 0), observed_size INTEGER NOT NULL CHECK(observed_size >= 0),
                anchor BLOB NOT NULL DEFAULT X'', missing_scans INTEGER NOT NULL DEFAULT 0 CHECK(missing_scans >= 0),
                missing_since_ms INTEGER NOT NULL DEFAULT 0,
                mtime_ns INTEGER NOT NULL DEFAULT 0, ctime_ns INTEGER NOT NULL DEFAULT 0,
                partial_at_eof INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(dev,ino));
             CREATE TABLE IF NOT EXISTS spool(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE,
                body BLOB NOT NULL, event_count INTEGER NOT NULL CHECK(event_count > 0));
             CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);",
        ).context("cannot initialize collector state")?;
        let has_anchor: bool = db
            .prepare("PRAGMA table_info(checkpoints)")?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()?
            .iter()
            .any(|name| name == "anchor");
        if !has_anchor {
            db.execute(
                "ALTER TABLE checkpoints ADD COLUMN anchor BLOB NOT NULL DEFAULT X''",
                [],
            )?;
        }
        let has_missing_scans: bool = db
            .prepare("PRAGMA table_info(checkpoints)")?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()?
            .iter()
            .any(|name| name == "missing_scans");
        if !has_missing_scans {
            db.execute(
                "ALTER TABLE checkpoints ADD COLUMN missing_scans INTEGER NOT NULL DEFAULT 0",
                [],
            )?;
        }
        let columns: Vec<String> = db
            .prepare("PRAGMA table_info(checkpoints)")?
            .query_map([], |row| row.get(1))?
            .collect::<std::result::Result<_, _>>()?;
        if !columns.iter().any(|column| column == "missing_since_ms") {
            db.execute(
                "ALTER TABLE checkpoints ADD COLUMN missing_since_ms INTEGER NOT NULL DEFAULT 0",
                [],
            )?;
        }
        for (name, definition) in [
            ("mtime_ns", "INTEGER NOT NULL DEFAULT 0"),
            ("ctime_ns", "INTEGER NOT NULL DEFAULT 0"),
            ("partial_at_eof", "INTEGER NOT NULL DEFAULT 0"),
        ] {
            let columns: Vec<String> = db
                .prepare("PRAGMA table_info(checkpoints)")?
                .query_map([], |row| row.get(1))?
                .collect::<std::result::Result<_, _>>()?;
            if !columns.iter().any(|column| column == name) {
                db.execute(
                    &format!("ALTER TABLE checkpoints ADD COLUMN {name} {definition}"),
                    [],
                )?;
            }
        }
        let pinned_source: Option<String> = db
            .query_row(
                "SELECT value FROM settings WHERE key='source_path'",
                [],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(pinned) = pinned_source {
            ensure!(
                pinned == source_path.to_string_lossy(),
                "collector source log path changed for this state database"
            );
        } else {
            db.execute(
                "INSERT INTO settings(key,value) VALUES('source_path',?1)",
                params![source_path.to_string_lossy()],
            )?;
        }
        if let Some(endpoint) = cfg.endpoint.as_deref() {
            let pinned_endpoint: Option<String> = db
                .query_row("SELECT value FROM settings WHERE key='endpoint'", [], |r| {
                    r.get(0)
                })
                .optional()?;
            if let Some(pinned) = pinned_endpoint {
                ensure!(
                    pinned == endpoint,
                    "proposal endpoint changed for this state database"
                );
            } else {
                db.execute(
                    "INSERT INTO settings(key,value) VALUES('endpoint',?1)",
                    params![endpoint],
                )?;
            }
        }
        ensure!(
            db.query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0))? == "delete",
            "SQLite journal mode must be DELETE for bounded disk use"
        );
        Ok(Self {
            db,
            _lock: lock,
            db_path: state_path,
            source_path,
        })
    }

    pub fn status(&self, cfg: &CollectorConfig) -> Result<Status> {
        let pending_batches: u64 = self
            .db
            .query_row("SELECT count(*) FROM spool", [], |r| r.get(0))?;
        let pending_events: u64 =
            self.db
                .query_row("SELECT coalesce(sum(event_count),0) FROM spool", [], |r| {
                    r.get(0)
                })?;
        let pending_bytes: u64 =
            self.db
                .query_row("SELECT coalesce(sum(length(body)),0) FROM spool", [], |r| {
                    r.get(0)
                })?;
        let oldest_batch_id = self
            .db
            .query_row("SELECT batch_id FROM spool ORDER BY seq LIMIT 1", [], |r| {
                r.get(0)
            })
            .optional()?;
        let checkpoints = self
            .db
            .query_row("SELECT count(*) FROM checkpoints", [], |r| r.get(0))?;
        let db_bytes = fs::metadata(&self.db_path).map(|m| m.len()).unwrap_or(0);
        Ok(Status {
            delivery: cfg.delivery,
            pending_batches,
            pending_events,
            pending_bytes,
            oldest_batch_id,
            checkpoints,
            db_bytes,
            db_max_bytes: cfg.db_max_bytes,
            spool_max_bytes: cfg.spool_max_bytes,
        })
    }

    pub fn export(&self, destination: &str, cfg: &CollectorConfig) -> Result<()> {
        let mut stmt = self.db.prepare("SELECT body FROM spool ORDER BY seq")?;
        let mut rows = stmt.query([])?;
        let stdout = destination == "stdout";
        if !stdout {
            let target = canonical_target(Path::new(destination))?;
            ensure!(
                target != self.db_path,
                "export path must not overwrite collector state"
            );
            ensure!(
                target != self.source_path,
                "export path must not overwrite access log"
            );
            if let Some(token) = &cfg.token_file {
                ensure!(
                    target != canonical_target(token)?,
                    "export path must not overwrite token file"
                );
            }
            if let Some(ca) = &cfg.ca_file {
                ensure!(
                    target != canonical_target(ca)?,
                    "export path must not overwrite CA file"
                );
            }
        }
        let mut output: Box<dyn Write> = if stdout {
            Box::new(std::io::stdout())
        } else {
            Box::new(OpenOptions::new().create_new(true).write(true).open(destination).with_context(|| format!("cannot create export file {destination}; existing files are never overwritten"))?)
        };
        while let Some(row) = rows.next()? {
            let body: Vec<u8> = row.get(0)?;
            output.write_all(&body)?;
            output.write_all(b"\n")?;
        }
        output.flush()?;
        Ok(())
    }

    fn checkpoint_frontier(&self, candidate: &Candidate) -> Result<Option<CheckpointFrontier>> {
        Ok(self.db
            .query_row(
                "SELECT offset,observed_size,anchor,mtime_ns,ctime_ns,partial_at_eof FROM checkpoints WHERE dev=?1 AND ino=?2",
                params![candidate.dev, candidate.ino],
                |row| Ok(CheckpointFrontier {
                    offset: row.get(0)?,
                    size: row.get(1)?,
                    anchor: row.get(2)?,
                    modified_ns: row.get(3)?,
                    changed_ns: row.get(4)?,
                    partial_at_eof: row.get(5)?,
                }),
            )
            .optional()?)
    }

    pub fn scan_one(&mut self, cfg: &CollectorConfig) -> Result<bool> {
        ensure!(
            self.db_path != cfg.access_log,
            "state database cannot replace access log"
        );
        let candidates = find_logs(&cfg.access_log, cfg.max_files)?;
        let present: std::collections::HashSet<(i64, i64)> =
            candidates.iter().map(|c| (c.dev, c.ino)).collect();
        let now_ms = system_time_ms();
        {
            let mut stmt = self.db.prepare(
                "SELECT dev,ino,offset,observed_size,path,missing_scans,missing_since_ms FROM checkpoints",
            )?;
            let rows = stmt.query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, u64>(2)?,
                    r.get::<_, u64>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, u32>(5)?,
                    r.get::<_, i64>(6)?,
                ))
            })?;
            let mut missing = Vec::new();
            let mut pending_missing = Vec::new();
            let mut recovered = Vec::new();
            for row in rows {
                let (dev, ino, offset, observed_size, path, missing_scans, missing_since_ms) = row?;
                if present.contains(&(dev, ino)) {
                    if missing_scans > 0 {
                        recovered.push((dev, ino));
                    }
                } else {
                    let was_active_path = canonical_target(Path::new(&path))? == self.source_path;
                    if offset < observed_size || was_active_path {
                        if missing_scans > 0
                            && now_ms.saturating_sub(missing_since_ms) >= MISSING_CONFIRMATION_MS
                        {
                            bail!(
                                "rotated log disappeared before it was fully checkpointed: {path}"
                            );
                        }
                        pending_missing.push((dev, ino));
                        continue;
                    }
                    missing.push((dev, ino));
                }
            }
            drop(stmt);
            if !missing.is_empty() || !pending_missing.is_empty() || !recovered.is_empty() {
                let tx = self
                    .db
                    .transaction_with_behavior(TransactionBehavior::Immediate)?;
                for (dev, ino) in missing {
                    tx.execute(
                        "DELETE FROM checkpoints WHERE dev=?1 AND ino=?2",
                        params![dev, ino],
                    )?;
                }
                for (dev, ino) in &pending_missing {
                    tx.execute(
                        "UPDATE checkpoints SET missing_since_ms=CASE WHEN missing_scans=0 THEN ?3 ELSE missing_since_ms END,missing_scans=missing_scans+1 WHERE dev=?1 AND ino=?2",
                        params![dev, ino, now_ms],
                    )?;
                }
                for (dev, ino) in recovered {
                    tx.execute(
                        "UPDATE checkpoints SET missing_scans=0,missing_since_ms=0 WHERE dev=?1 AND ino=?2",
                        params![dev, ino],
                    )?;
                }
                tx.commit()?;
            }
            if !pending_missing.is_empty() {
                return Ok(false);
            }
        }

        // A full spool pauses source progress. It never acknowledges or skips a line.
        if spool_bytes(&self.db)? >= cfg.spool_max_bytes {
            self.track_source_frontier(&candidates, cfg)?;
            return Ok(false);
        }
        for candidate in &candidates {
            let mut file = match open_candidate(&candidate.path) {
                Ok(file) => file,
                Err(error) if is_not_found(&error) => return Ok(false),
                Err(error) => return Err(error),
            };
            let metadata = file.metadata()?;
            if (metadata.dev() as i64, metadata.ino() as i64) != (candidate.dev, candidate.ino) {
                return Ok(false);
            }
            let current_size = metadata.len();
            let (mtime_ns, ctime_ns) = metadata_stamps(&metadata);
            let old = self.checkpoint_frontier(candidate)?;
            let offset = old.as_ref().map_or(0, |frontier| frontier.offset);
            if let Some(frontier) = &old {
                let repaired = frontier.only_partial_tail_removed(current_size);
                if repaired && !candidate.rotated && recent_tail_repair(mtime_ns) {
                    return Ok(false);
                }
                ensure!(current_size >= frontier.size || (candidate.rotated && repaired),
                    "observed unread log bytes disappeared (truncation/inode reuse); stopping to avoid silent loss");
                if current_size == frontier.size
                    && frontier.modified_ns != 0
                    && frontier.changed_ns != 0
                {
                    ensure!(mtime_ns == frontier.modified_ns, "log metadata changed without an append (copytruncate/rewrite detected); stopping to avoid silent loss");
                }
                let anchor = &frontier.anchor;
                if !anchor.is_empty() {
                    let actual = read_anchor(&file, offset, candidate.dev, candidate.ino)?;
                    let matches = if anchor.len() <= 64 {
                        actual.ends_with(anchor)
                    } else {
                        actual == *anchor
                    };
                    ensure!(matches, "log content changed before its checkpoint (copytruncate/rewrite detected); stopping to avoid silent loss");
                }
            }
            ensure!(current_size >= offset, "copytruncate/truncation detected for {} (offset {offset}, size {current_size}); stopping to avoid silent loss", candidate.path.display());
            file.seek(SeekFrom::Start(offset))?;
            let anchor_file = file.try_clone()?;
            let limited = file.take(current_size.saturating_sub(offset));
            let mut reader = BufReader::with_capacity(8192, limited);
            let mut events: Vec<(Value, u64, u64)> = Vec::new();
            let mut total_event_bytes = 0u64;
            let mut cursor_offset = offset;
            let mut partial_at_eof = false;
            while events.len() < cfg.batch_max_events {
                let Some((line, next_offset, complete)) =
                    read_bounded_line(&mut reader, cfg.max_line_bytes)?
                else {
                    break;
                };
                if !complete {
                    ensure!(
                        !candidate.rotated,
                        "rotated log ends with an incomplete JSONL line: {}",
                        candidate.path.display()
                    );
                    // Only mark bytes scanned through the recorded frontier.
                    partial_at_eof = cursor_offset.saturating_add(next_offset) == current_size;
                    break;
                }
                let event = validate_event(&line).with_context(|| {
                    format!(
                        "invalid access-log event at {} byte {}",
                        candidate.path.display(),
                        cursor_offset
                    )
                })?;
                let bytes = line.len() as u64 + 1;
                if !events.is_empty()
                    && total_event_bytes.saturating_add(bytes) > cfg.batch_max_bytes
                {
                    break;
                }
                ensure!(
                    bytes <= cfg.batch_max_bytes,
                    "one event exceeds batch_max_bytes"
                );
                cursor_offset = cursor_offset.saturating_add(next_offset);
                events.push((event, cursor_offset, bytes));
                total_event_bytes = total_event_bytes.saturating_add(bytes);
                if total_event_bytes >= cfg.batch_max_bytes {
                    break;
                }
            }
            if events.is_empty() {
                self.db.execute(
                    "INSERT INTO checkpoints(dev,ino,path,offset,observed_size,anchor,missing_scans,mtime_ns,ctime_ns,partial_at_eof) VALUES(?1,?2,?3,?4,?5,X'',0,?6,?7,?8) ON CONFLICT(dev,ino) DO UPDATE SET path=excluded.path, observed_size=excluded.observed_size, missing_scans=0,missing_since_ms=0,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,partial_at_eof=excluded.partial_at_eof",
                    params![candidate.dev,candidate.ino,candidate.path.to_string_lossy(),offset,current_size,mtime_ns,ctime_ns,partial_at_eof],
                )?;
                continue;
            }
            let batch_id = Uuid::new_v4().to_string();
            let (body, new_offset, event_count) = loop {
                let values: Vec<Value> = events.iter().map(|row| row.0.clone()).collect();
                let body = serde_json::to_vec(&Envelope {
                    contract_version: PROPOSAL,
                    batch_id: batch_id.clone(),
                    events: &values,
                })?;
                let end = events.last().map(|e| e.1).unwrap_or(offset);
                if body.len() as u64 <= cfg.batch_max_bytes {
                    break (body, end, values.len());
                }
                ensure!(
                    events.len() > 1,
                    "one event plus the proposal envelope exceeds batch_max_bytes"
                );
                events.pop();
            };
            let partial_at_eof = !candidate.rotated
                && ((partial_at_eof && new_offset == cursor_offset)
                    || only_incomplete_tail(
                        &anchor_file,
                        new_offset,
                        current_size,
                        cfg.max_line_bytes,
                    )?);
            let anchor = read_anchor(&anchor_file, new_offset, candidate.dev, candidate.ino)?;
            let current_spool = spool_bytes(&self.db)?;
            if current_spool.saturating_add(body.len() as u64) > cfg.spool_max_bytes {
                self.track_source_frontier(&candidates, cfg)?;
                return Ok(false);
            }
            ensure!(
                fs::metadata(&self.db_path)?.len() <= cfg.db_max_bytes,
                "collector database reached db_max_bytes; source checkpoint was not advanced"
            );
            let tx = self
                .db
                .transaction_with_behavior(TransactionBehavior::Immediate)?;
            let inside_spool: u64 =
                tx.query_row("SELECT coalesce(sum(length(body)),0) FROM spool", [], |r| {
                    r.get(0)
                })?;
            if inside_spool.saturating_add(body.len() as u64) > cfg.spool_max_bytes {
                return Ok(false);
            }
            tx.execute(
                "INSERT INTO spool(batch_id,body,event_count) VALUES(?1,?2,?3)",
                params![batch_id, body, event_count],
            )?;
            tx.execute(
                "INSERT INTO checkpoints(dev,ino,path,offset,observed_size,anchor,missing_scans,mtime_ns,ctime_ns,partial_at_eof) VALUES(?1,?2,?3,?4,?5,?6,0,?7,?8,?9) ON CONFLICT(dev,ino) DO UPDATE SET path=excluded.path,offset=excluded.offset,observed_size=excluded.observed_size,anchor=excluded.anchor,missing_scans=0,missing_since_ms=0,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,partial_at_eof=excluded.partial_at_eof",
                params![candidate.dev,candidate.ino,candidate.path.to_string_lossy(),new_offset,current_size,anchor,mtime_ns,ctime_ns,partial_at_eof],
            )?;
            tx.commit()
                .context("could not durably commit spool batch and source checkpoint")?;
            ensure!(
                fs::metadata(&self.db_path)?.len() <= cfg.db_max_bytes,
                "collector database exceeded configured hard limit"
            );
            return Ok(true);
        }
        Ok(false)
    }

    fn track_source_frontier(&self, candidates: &[Candidate], cfg: &CollectorConfig) -> Result<()> {
        let mut observations = Vec::new();
        for candidate in candidates {
            let file = match open_candidate(&candidate.path) {
                Ok(file) => file,
                Err(error) if is_not_found(&error) => return Ok(()),
                Err(error) => return Err(error),
            };
            let metadata = file.metadata()?;
            if (metadata.dev() as i64, metadata.ino() as i64) != (candidate.dev, candidate.ino) {
                return Ok(());
            }
            let (mtime_ns, ctime_ns) = metadata_stamps(&metadata);
            let old = self.checkpoint_frontier(candidate)?;
            let offset = old.as_ref().map_or(0, |frontier| frontier.offset);
            let size = metadata.len();
            ensure!(
                size >= offset,
                "copytruncate/truncation detected while spool is full"
            );
            let mut same_partial = false;
            if let Some(frontier) = &old {
                let repaired = frontier.only_partial_tail_removed(size);
                if repaired && !candidate.rotated && recent_tail_repair(mtime_ns) {
                    return Ok(());
                }
                ensure!(size >= frontier.size || (candidate.rotated && repaired),
                    "observed unread log bytes disappeared while spool is full (truncation/inode reuse)");
                same_partial =
                    !candidate.rotated && frontier.partial_at_eof && size == frontier.size;
                if size == frontier.size && frontier.modified_ns != 0 && frontier.changed_ns != 0 {
                    ensure!(
                        mtime_ns == frontier.modified_ns,
                        "log modification time changed without an append while spool is full"
                    );
                }
                let anchor = &frontier.anchor;
                if !anchor.is_empty() {
                    let actual = read_anchor(&file, offset, candidate.dev, candidate.ino)?;
                    ensure!(
                        actual.ends_with(anchor) || actual == *anchor,
                        "log content changed before its checkpoint while spool is full"
                    );
                }
            }
            observations.push((
                candidate.dev,
                candidate.ino,
                candidate.path.to_string_lossy().into_owned(),
                offset,
                metadata.len(),
                mtime_ns,
                ctime_ns,
                same_partial,
            ));
        }
        let tx = self.db.unchecked_transaction()?;
        for (dev, ino, path, offset, size, mtime_ns, ctime_ns, partial_at_eof) in observations {
            tx.execute(
                "INSERT INTO checkpoints(dev,ino,path,offset,observed_size,anchor,missing_scans,mtime_ns,ctime_ns,partial_at_eof) VALUES(?1,?2,?3,?4,?5,X'',0,?6,?7,?8) ON CONFLICT(dev,ino) DO UPDATE SET path=excluded.path,observed_size=excluded.observed_size,missing_scans=0,missing_since_ms=0,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,partial_at_eof=excluded.partial_at_eof",
                params![dev,ino,path,offset,size,mtime_ns,ctime_ns,partial_at_eof],
            )?;
        }
        tx.commit()?;
        ensure!(
            fs::metadata(&self.db_path)?.len() <= cfg.db_max_bytes,
            "collector database exceeded configured hard limit"
        );
        Ok(())
    }

    pub async fn deliver_one(&mut self, cfg: &CollectorConfig) -> Result<bool> {
        if cfg.delivery == Delivery::Spool {
            return Ok(false);
        }
        let row: Option<(i64, String, Vec<u8>, usize)> = self
            .db
            .query_row(
                "SELECT seq,batch_id,body,event_count FROM spool ORDER BY seq LIMIT 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?;
        let Some((seq, batch_id, body, event_count)) = row else {
            return Ok(false);
        };
        let token_path = cfg.token_file.as_ref().context("missing token_file")?;
        let token_metadata = fs::symlink_metadata(token_path)
            .with_context(|| format!("cannot read token file {}", token_path.display()))?;
        ensure!(
            token_metadata.is_file() && token_metadata.len() <= 4096,
            "token_file must be a regular file no larger than 4096 bytes"
        );
        ensure!(
            !token_metadata.file_type().is_symlink(),
            "token_file must not be a symlink"
        );
        let token = String::from_utf8(
            read_capped_regular(token_path, 4096).context("cannot read token file")?,
        )
        .context("token_file must contain UTF-8")?;
        let token = token.trim();
        ensure!(
            !token.is_empty() && token.len() <= 4096 && !token.contains(['\r', '\n']),
            "token_file must contain a single non-empty token line"
        );
        let mut builder = Client::builder()
            .redirect(Policy::none())
            .no_proxy()
            .timeout(Duration::from_millis(cfg.request_timeout_ms));
        if let Some(ca_file) = &cfg.ca_file {
            let ca_meta = fs::symlink_metadata(ca_file)
                .with_context(|| format!("cannot read CA file {}", ca_file.display()))?;
            ensure!(
                ca_meta.is_file() && ca_meta.len() <= 1_048_576,
                "ca_file must be a regular PEM file no larger than 1 MiB"
            );
            ensure!(
                !ca_meta.file_type().is_symlink(),
                "ca_file must not be a symlink"
            );
            let pem = read_capped_regular(ca_file, 1_048_576).context("cannot read CA file")?;
            let mut certificates = reqwest::Certificate::from_pem_bundle(&pem)
                .context("ca_file contains invalid PEM certificates")?;
            ensure!(
                !certificates.is_empty(),
                "ca_file must contain at least one certificate"
            );
            for certificate in certificates.drain(..) {
                builder = builder.add_root_certificate(certificate);
            }
        }
        let client = builder
            .build()
            .context("cannot initialize HTTPS transport")?;
        let endpoint = cfg.endpoint.as_deref().context("missing endpoint")?;
        let response = client
            .post(endpoint)
            .bearer_auth(token)
            .header("content-type", "application/json")
            .body(body.clone())
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("proposal HTTP request failed"))?;
        ensure!(
            response.status() == reqwest::StatusCode::OK,
            "proposal HTTP returned status {}",
            response.status().as_u16()
        );
        if let Some(length) = response.content_length() {
            ensure!(
                length <= ACK_MAX as u64,
                "proposal HTTP acknowledgement exceeds 4096 bytes"
            );
        }
        let mut response = response;
        let mut ack_body = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| anyhow::anyhow!("cannot read proposal HTTP acknowledgement"))?
        {
            ensure!(
                ack_body.len().saturating_add(chunk.len()) <= ACK_MAX,
                "proposal HTTP acknowledgement exceeds 4096 bytes"
            );
            ack_body.extend_from_slice(&chunk);
        }
        let ack = parse_ack(&ack_body)?;
        ensure!(
            ack.batch_id == batch_id && ack.accepted_count == event_count,
            "proposal HTTP acknowledgement does not match batch id and accepted count"
        );
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute(
            "DELETE FROM spool WHERE seq=?1 AND batch_id=?2",
            params![seq, batch_id],
        )?;
        ensure!(
            tx.changes() == 1,
            "spool acknowledgement row changed concurrently"
        );
        tx.commit()
            .context("could not durably record batch acknowledgement")?;
        Ok(true)
    }
}

fn spool_bytes(db: &Connection) -> Result<u64> {
    Ok(
        db.query_row("SELECT coalesce(sum(length(body)),0) FROM spool", [], |r| {
            r.get(0)
        })?,
    )
}

fn canonical_target(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    if absolute.exists() {
        return fs::canonicalize(&absolute)
            .with_context(|| format!("cannot canonicalize path {}", absolute.display()));
    }
    let mut ancestor = absolute.as_path();
    let mut suffix = Vec::new();
    while !ancestor.exists() {
        suffix.push(
            ancestor
                .file_name()
                .context("path has no file name")?
                .to_os_string(),
        );
        ancestor = ancestor.parent().context("path has no existing ancestor")?;
    }
    let mut canonical = fs::canonicalize(ancestor)?;
    for part in suffix.iter().rev() {
        canonical.push(part);
    }
    Ok(canonical)
}

fn read_capped_regular(path: &Path, maximum: u64) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "path must be a regular non-symlink file"
    );
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let file = options.open(path)?;
    let opened = file.metadata()?;
    ensure!(
        (opened.dev(), opened.ino()) == (metadata.dev(), metadata.ino()),
        "file identity changed while opening bounded input"
    );
    let mut bytes = Vec::new();
    file.take(maximum.saturating_add(1))
        .read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 <= maximum,
        "file grew past its configured size limit"
    );
    Ok(bytes)
}

#[cfg(unix)]
fn metadata_stamps(metadata: &Metadata) -> (i64, i64) {
    let mtime = (metadata.mtime() as i128)
        .saturating_mul(1_000_000_000)
        .saturating_add(metadata.mtime_nsec() as i128);
    let ctime = (metadata.ctime() as i128)
        .saturating_mul(1_000_000_000)
        .saturating_add(metadata.ctime_nsec() as i128);
    (
        mtime.clamp(i64::MIN as i128, i64::MAX as i128) as i64,
        ctime.clamp(i64::MIN as i128, i64::MAX as i128) as i64,
    )
}

// The writer truncates a known partial tail before renaming its inode. Give
// that narrow transition the same confirmation window as a disappearing name.
fn recent_tail_repair(modified_ns: i64) -> bool {
    let age = system_time_ms().saturating_sub(modified_ns / 1_000_000);
    (0..MISSING_CONFIRMATION_MS).contains(&age)
}

fn system_time_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}

fn read_anchor(file: &File, end: u64, dev: i64, ino: i64) -> Result<Vec<u8>> {
    let length = end.min(256) as usize;
    if length == 0 {
        return Ok(Vec::new());
    }
    let metadata = file.metadata()?;
    ensure!(
        (metadata.dev() as i64, metadata.ino() as i64) == (dev, ino),
        "log file identity changed while checkpointing"
    );
    ensure!(metadata.len() >= end, "log changed while checkpointing");
    let mut file = file.try_clone()?;
    file.seek(SeekFrom::Start(0))?;
    let mut bytes = vec![0; length];
    file.read_exact(&mut bytes)?;
    file.seek(SeekFrom::Start(end - length as u64))?;
    let mut tail = vec![0; length];
    file.read_exact(&mut tail)?;
    bytes.extend_from_slice(&tail);
    Ok(bytes)
}

fn is_not_found(error: &anyhow::Error) -> bool {
    error
        .root_cause()
        .downcast_ref::<std::io::Error>()
        .is_some_and(|io| io.kind() == std::io::ErrorKind::NotFound)
}

#[derive(Debug)]
struct Candidate {
    path: PathBuf,
    dev: i64,
    ino: i64,
    rotated: bool,
}

fn find_logs(base: &Path, max_files: u32) -> Result<Vec<Candidate>> {
    let mut paths = Vec::new();
    let parent = base
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let base_name = base
        .file_name()
        .and_then(|name| name.to_str())
        .context("access_log basename must be valid UTF-8")?;
    let rotation_prefix = format!("{base_name}.");
    let entries = match fs::read_dir(parent) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("cannot scan log directory {}", parent.display()))
        }
    };
    for entry in entries {
        let entry = entry?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        let (generation, rotated) = if name == base_name {
            (0, false)
        } else if let Some(suffix) = name.strip_prefix(&rotation_prefix) {
            if !suffix.bytes().all(|byte| byte.is_ascii_digit()) {
                continue;
            }
            let generation: u32 = suffix
                .parse()
                .context("rotated log suffix is out of range")?;
            // Only the canonical names emitted by the proxy are considered.
            // This also prevents arbitrarily many aliases (01, 001, ...) from
            // bypassing the configured candidate bound.
            if generation == 0 || suffix != generation.to_string() {
                continue;
            }
            ensure!(
                generation <= max_files,
                "more rotated log generations exist than max_files permits"
            );
            (generation, true)
        } else {
            continue;
        };
        let path = entry.path();
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("cannot inspect log candidate {}", path.display()))
            }
        };
        if metadata.file_type().is_symlink() {
            bail!("log candidate is a symlink: {}", path.display());
        }
        if metadata.is_file() {
            paths.push((generation, path, rotated));
            ensure!(
                paths.len() <= max_files as usize + 1,
                "more log candidates exist than max_files permits"
            );
        }
    }
    // Oldest immutable generation first; the current access log follows it.
    paths.sort_by(|a, b| b.0.cmp(&a.0));
    let mut out = Vec::new();
    for (_, path, rotated) in paths {
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("cannot inspect log candidate {}", path.display()))
            }
        };
        if metadata.file_type().is_symlink() {
            bail!("log candidate is a symlink: {}", path.display());
        }
        if !metadata.is_file() {
            continue;
        }
        out.push(Candidate {
            path,
            dev: metadata.dev() as i64,
            ino: metadata.ino() as i64,
            rotated,
        });
    }
    Ok(out)
}

fn open_candidate(path: &Path) -> Result<File> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .with_context(|| format!("cannot open log candidate {}", path.display()))
    }
    #[cfg(not(unix))]
    {
        File::open(path).with_context(|| format!("cannot open log candidate {}", path.display()))
    }
}

/// Read one line while never allocating beyond max bytes plus a fixed 8 KiB buffer.
/// Returned offset includes the newline. EOF without newline is a pending line.
fn read_bounded_line<R: Read>(
    reader: &mut BufReader<R>,
    max: u64,
) -> Result<Option<(Vec<u8>, u64, bool)>> {
    let mut line = Vec::new();
    let mut consumed = 0u64;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if line.is_empty() {
                Ok(None)
            } else {
                Ok(Some((line, consumed, false)))
            };
        }
        let length = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(available.len(), |i| i + 1);
        ensure!(
            line.len().saturating_add(length) as u64 <= max + 1,
            "access-log JSONL line exceeds max_line_bytes"
        );
        let complete = available.get(length.saturating_sub(1)) == Some(&b'\n');
        let payload_len = length - usize::from(complete);
        line.extend_from_slice(&available[..payload_len]);
        reader.consume(length);
        consumed = consumed.saturating_add(length as u64);
        if complete {
            return Ok(Some((line, consumed, true)));
        }
    }
}

// A batch can reach its event or byte limit at the last complete line.
// Inspect a bounded remainder so one-shot scans can still prove a partial tail.
fn only_incomplete_tail(file: &File, offset: u64, size: u64, max_line: u64) -> Result<bool> {
    let tail = size.saturating_sub(offset);
    if tail == 0 || tail > max_line + 1 {
        return Ok(false);
    }
    let mut file = file.try_clone()?;
    file.seek(SeekFrom::Start(offset))?;
    let mut reader = BufReader::with_capacity(8192, file.take(tail));
    Ok(read_bounded_line(&mut reader, max_line)?
        .is_some_and(|(_, consumed, complete)| !complete && consumed == tail))
}

#[allow(dead_code)]
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Event {
    schema_version: String,
    event_id: String,
    timestamp: String,
    instance_id: String,
    config_version: u64,
    request_id: String,
    #[serde(deserialize_with = "required_nullable")]
    traceparent: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    route_id: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    upstream_id: Option<String>,
    method: String,
    #[serde(deserialize_with = "required_nullable")]
    path: Option<String>,
    protocol: String,
    status: u16,
    duration_ms: f64,
    #[serde(deserialize_with = "required_nullable")]
    transfer_ms: Option<f64>,
    request_bytes: u64,
    response_bytes: u64,
    outcome: String,
    #[serde(deserialize_with = "required_nullable")]
    error_direction: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    error_category: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    error_phase: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    reason: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    backpressure_observations: Option<u64>,
    attempts: Vec<Attempt>,
}
#[allow(dead_code)]
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Attempt {
    number: u32,
    outcome: String,
    #[serde(deserialize_with = "required_nullable")]
    connection_id: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    reused: Option<bool>,
    #[serde(deserialize_with = "required_nullable")]
    pool_wait_ms: Option<f64>,
    #[serde(deserialize_with = "required_nullable")]
    connect_gate_ms: Option<f64>,
    #[serde(deserialize_with = "required_nullable")]
    dns_ms: Option<f64>,
    #[serde(deserialize_with = "required_nullable")]
    tcp_ms: Option<f64>,
    #[serde(deserialize_with = "required_nullable")]
    tls_ms: Option<f64>,
    #[serde(deserialize_with = "required_nullable")]
    headers_ms: Option<f64>,
}
fn required_nullable<'de, D, T>(deserializer: D) -> std::result::Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

fn validate_event(line: &[u8]) -> Result<Value> {
    let event: Event =
        serde_json::from_slice(line).context("event is not valid access-log JSON")?;
    ensure!(
        (event.schema_version == "1.0" && event.protocol == "HTTP/1.1")
            || (event.schema_version == "1.1"
                && matches!(event.protocol.as_str(), "HTTP/1.1" | "HTTP/2")),
        "unsupported access-log schema_version or protocol"
    );
    for id in [&event.event_id, &event.instance_id, &event.request_id] {
        ensure!(
            id.len() == 36
                && [8, 13, 18, 23]
                    .into_iter()
                    .all(|i| id.as_bytes()[i] == b'-'),
            "access-log UUID field must use canonical hyphenated form"
        );
        let parsed = Uuid::parse_str(id).context("access-log UUID field is invalid")?;
        ensure!(
            parsed.hyphenated().to_string() == *id,
            "access-log UUID field must use lowercase canonical form"
        );
    }
    ensure!(
        time::OffsetDateTime::parse(&event.timestamp, &Rfc3339).is_ok(),
        "access-log timestamp must be RFC3339 date-time"
    );
    ensure!(
        event.config_version >= 1 && (100..=599).contains(&event.status),
        "access-log config_version or status is out of range"
    );
    ensure!(
        event.duration_ms.is_finite()
            && event.duration_ms >= 0.0
            && event.transfer_ms.is_none_or(|v| v.is_finite() && v >= 0.0),
        "access-log duration is invalid"
    );
    const METHODS: &[&str] = &[
        "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "OTHER",
    ];
    const OUTCOMES: &[&str] = &[
        "complete",
        "cancelled",
        "rejected",
        "stream_timeout",
        "body_error",
        "body_limit",
    ];
    ensure!(
        METHODS.contains(&event.method.as_str()) && OUTCOMES.contains(&event.outcome.as_str()),
        "access-log method or outcome is invalid"
    );
    ensure!(
        event.attempts.len() <= 1,
        "access-log may contain at most one attempt"
    );
    if let Some(trace) = &event.traceparent {
        ensure!(
            trace.len() == 55
                && trace.starts_with("00-")
                && trace.as_bytes()[35] == b'-'
                && trace.as_bytes()[52] == b'-'
                && trace.bytes().enumerate().all(|(i, c)| {
                    if [2, 35, 52].contains(&i) {
                        c == b'-'
                    } else {
                        c.is_ascii_hexdigit() && !c.is_ascii_uppercase()
                    }
                }),
            "access-log traceparent is invalid"
        );
    }
    for text in [
        event.route_id.as_ref(),
        event.upstream_id.as_ref(),
        event.path.as_ref(),
        event.error_direction.as_ref(),
        event.error_category.as_ref(),
        event.error_phase.as_ref(),
        event.reason.as_ref(),
    ]
    .into_iter()
    .flatten()
    {
        ensure!(
            text.chars().count() <= 256,
            "access-log string field is too long"
        );
    }
    if let Some(attempt) = event.attempts.first() {
        ensure!(
            attempt.number == 1 && attempt.outcome.chars().count() <= 128,
            "access-log attempt is invalid"
        );
        for duration in [
            attempt.pool_wait_ms,
            attempt.connect_gate_ms,
            attempt.dns_ms,
            attempt.tcp_ms,
            attempt.tls_ms,
            attempt.headers_ms,
        ]
        .into_iter()
        .flatten()
        {
            ensure!(
                duration.is_finite() && duration >= 0.0,
                "access-log attempt duration is invalid"
            );
        }
        if let Some(id) = &attempt.connection_id {
            ensure!(
                id.chars().count() <= 256,
                "access-log connection id is too long"
            );
        }
    }
    Ok(serde_json::from_slice(line)?)
}

#[cfg(unix)]
use std::os::unix::fs::MetadataExt;

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn config(dir: &Path) -> CollectorConfig {
        CollectorConfig {
            access_log: dir.join("access.jsonl"),
            state_path: dir.join("state.db"),
            spool_max_bytes: 32_768,
            db_max_bytes: 262_144,
            batch_max_events: 2,
            batch_max_bytes: 16_384,
            max_line_bytes: 8192,
            max_files: 8,
            poll_ms: 10,
            backoff_min_ms: 5,
            backoff_max_ms: 100,
            request_timeout_ms: 100,
            delivery: Delivery::Spool,
            endpoint: None,
            token_file: None,
            ca_file: None,
        }
    }

    fn fixture(id: &str) -> String {
        serde_json::json!({
            "schema_version":"1.0","event_id":id,"timestamp":"2026-11-16T12:00:00Z",
            "instance_id":"00000000-0000-4000-8000-000000000001","config_version":1,
            "request_id":"00000000-0000-4000-8000-000000000002","traceparent":null,
            "route_id":"api","upstream_id":"python","method":"GET","path":"/health",
            "protocol":"HTTP/1.1","status":200,"duration_ms":1.25,"transfer_ms":null,
            "request_bytes":0,"response_bytes":5,"outcome":"complete","error_direction":null,
            "error_category":null,"error_phase":null,"reason":null,"backpressure_observations":0,
            "attempts":[{"number":1,"outcome":"complete","connection_id":null,"reused":false,
            "pool_wait_ms":0.1,"connect_gate_ms":0.1,"dns_ms":null,"tcp_ms":0.2,"tls_ms":null,"headers_ms":0.3}]
        }).to_string()
    }

    fn append(path: &Path, data: &str) {
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .unwrap();
        writeln!(f, "{data}").unwrap();
        f.sync_all().unwrap();
    }

    #[test]
    fn rotated_tail_repair_accepts_only_a_scanned_fragment() {
        for full in [false, true] {
            let dir = tempdir().unwrap();
            let mut cfg = config(dir.path());
            cfg.batch_max_events = 1;
            append(
                &cfg.access_log,
                &fixture("00000000-0000-4000-8000-000000000003"),
            );
            let committed = fs::metadata(&cfg.access_log).unwrap().len();
            let mut log = OpenOptions::new()
                .append(true)
                .open(&cfg.access_log)
                .unwrap();
            log.write_all(b"{\"event_id\":").unwrap();
            let observed = log.metadata().unwrap().len();
            let mut state = State::open(&cfg).unwrap();
            assert!(state.scan_one(&cfg).unwrap());
            if full {
                cfg.spool_max_bytes = state.status(&cfg).unwrap().pending_bytes;
            }
            drop(state);

            log.set_len(committed).unwrap();
            log.set_modified(std::time::UNIX_EPOCH + Duration::from_secs(1))
                .unwrap();
            drop(log);
            let mut state = State::open(&cfg).unwrap();
            assert!(state.scan_one(&cfg).is_err());
            let frontier = |state: &State| {
                state
                    .db
                    .query_row(
                        "SELECT offset,observed_size,partial_at_eof FROM checkpoints",
                        [],
                        |row| {
                            Ok((
                                row.get::<_, u64>(0)?,
                                row.get::<_, u64>(1)?,
                                row.get::<_, bool>(2)?,
                            ))
                        },
                    )
                    .unwrap()
            };
            assert_eq!(frontier(&state), (committed, observed, true));
            fs::rename(&cfg.access_log, dir.path().join("access.jsonl.1")).unwrap();
            assert!(!state.scan_one(&cfg).unwrap());
            assert_eq!(frontier(&state), (committed, committed, false));

            cfg.spool_max_bytes = 32768;
            append(
                &cfg.access_log,
                &fixture("00000000-0000-4000-8000-000000000004"),
            );
            assert!(state.scan_one(&cfg).unwrap());
            assert_eq!(state.status(&cfg).unwrap().pending_events, 2);
        }
    }

    #[test]
    fn schema_rejects_unknown_missing_and_bad_values() {
        let valid = fixture("00000000-0000-4000-8000-000000000003");
        assert!(validate_event(valid.as_bytes()).is_ok());
        let h2 = valid
            .replace("\"schema_version\":\"1.0\"", "\"schema_version\":\"1.1\"")
            .replace("HTTP/1.1", "HTTP/2");
        assert!(validate_event(h2.as_bytes()).is_ok());
        assert!(validate_event(valid.replace("HTTP/1.1", "HTTP/2").as_bytes()).is_err());
        assert!(validate_event(
            format!("{} {{\"extra\":1}}", &valid[..valid.len() - 1]).as_bytes()
        )
        .is_err());
        let missing = valid.replace("\"duration_ms\":1.25,", "");
        assert!(validate_event(missing.as_bytes()).is_err());
        let bad = valid.replace("\"status\":200", "\"status\":700");
        assert!(validate_event(bad.as_bytes()).is_err());
    }

    #[test]
    fn invalid_ack_error_does_not_echo_untrusted_field_names() {
        let reflected_secret = "Bearer-secret-that-must-not-enter-logs";
        let body = format!(r#"{{"{reflected_secret}":"{reflected_secret}"}}"#);
        let error = format!(
            "{:#}",
            parse_ack(body.as_bytes())
                .err()
                .expect("invalid ACK rejected")
        );
        assert!(error.contains("invalid acknowledgement JSON"));
        assert!(!error.contains(reflected_secret));
        assert!(!error.contains("syntheticsecret"));
        assert!(parse_ack(br#"{"batch_id":"id","accepted_count":1}"#).is_ok());
        assert!(parse_ack(br#"{"batch_id":"id","accepted_count":1,"extra":true}"#).is_err());
    }

    #[test]
    fn checkpoint_and_spool_commit_together_then_resume() {
        let dir = tempdir().unwrap();
        let cfg = config(dir.path());
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000003"),
        );
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000004"),
        );
        {
            let mut state = State::open(&cfg).unwrap();
            assert!(state.scan_one(&cfg).unwrap());
            let status = state.status(&cfg).unwrap();
            assert_eq!(status.pending_events, 2);
            assert_eq!(status.pending_batches, 1);
        }
        {
            let mut state = State::open(&cfg).unwrap();
            assert!(!state.scan_one(&cfg).unwrap());
            let status = state.status(&cfg).unwrap();
            assert_eq!(status.pending_events, 2);
        }
    }

    #[test]
    fn rename_rotation_follows_inode_and_truncation_stops() {
        let dir = tempdir().unwrap();
        let cfg = config(dir.path());
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000003"),
        );
        let mut state = State::open(&cfg).unwrap();
        assert!(state.scan_one(&cfg).unwrap());
        drop(state);
        fs::rename(&cfg.access_log, format!("{}.1", cfg.access_log.display())).unwrap();
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000004"),
        );
        state = State::open(&cfg).unwrap();
        assert!(state.scan_one(&cfg).unwrap());
        fs::write(&cfg.access_log, b"{}").unwrap();
        assert!(state.scan_one(&cfg).is_err());
    }

    #[test]
    fn same_inode_same_size_rewrite_is_rejected() {
        let dir = tempdir().unwrap();
        let cfg = config(dir.path());
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000003"),
        );
        let mut state = State::open(&cfg).unwrap();
        assert!(state.scan_one(&cfg).unwrap());
        let before = fs::metadata(&cfg.access_log).unwrap().len();
        let replacement = format!("{}\n", fixture("00000000-0000-4000-8000-000000000004"));
        assert_eq!(replacement.len() as u64, before);
        let old_metadata = fs::metadata(&cfg.access_log).unwrap();
        std::thread::sleep(Duration::from_millis(5));
        fs::write(&cfg.access_log, replacement).unwrap();
        let (offset, saved): (u64, Vec<u8>) = state
            .db
            .query_row("SELECT offset,anchor FROM checkpoints", [], |row| {
                Ok((row.get(0)?, row.get(1)?))
            })
            .unwrap();
        let file = open_candidate(&cfg.access_log).unwrap();
        assert_eq!(offset, before);
        let metadata = file.metadata().unwrap();
        let actual =
            read_anchor(&file, offset, metadata.dev() as i64, metadata.ino() as i64).unwrap();
        assert_eq!(actual, saved); // Stable JSON suffixes require the same-size mtime guard.
        assert_ne!(
            metadata_stamps(&metadata).0,
            metadata_stamps(&old_metadata).0
        );
        assert!(state.scan_one(&cfg).is_err());
    }

    fn observed_frontier_shrink(resume: bool) {
        let dir = tempdir().unwrap();
        let mut cfg = config(dir.path());
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000003"),
        );
        let mut state = State::open(&cfg).unwrap();
        assert!(state.scan_one(&cfg).unwrap());
        let committed = fs::metadata(&cfg.access_log).unwrap().len();
        cfg.spool_max_bytes = state.status(&cfg).unwrap().pending_bytes;
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000004"),
        );
        assert!(!state.scan_one(&cfg).unwrap());
        let observed = fs::metadata(&cfg.access_log).unwrap().len();
        let before = state.status(&cfg).unwrap().pending_bytes;
        // Deterministic same-inode shrink: the committed prefix is unchanged,
        // but an already observed unread event disappears.
        OpenOptions::new()
            .write(true)
            .open(&cfg.access_log)
            .unwrap()
            .set_len(committed)
            .unwrap();
        if resume {
            cfg.spool_max_bytes *= 4;
        }
        assert!(
            state.scan_one(&cfg).is_err(),
            "observed unread bytes must never disappear silently"
        );
        let saved: u64 = state
            .db
            .query_row("SELECT observed_size FROM checkpoints", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            saved, observed,
            "rejected scan must not lower the durable frontier"
        );
        assert_eq!(state.status(&cfg).unwrap().pending_bytes, before);
    }

    #[test]
    fn observed_frontier_shrink_while_full_is_rejected() {
        observed_frontier_shrink(false);
    }

    #[test]
    fn observed_frontier_shrink_after_resume_is_rejected() {
        observed_frontier_shrink(true);
    }

    #[test]
    fn lost_unread_log_fails_even_when_spool_is_full() {
        let dir = tempdir().unwrap();
        let mut cfg = config(dir.path());
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000003"),
        );
        let mut state = State::open(&cfg).unwrap();
        assert!(state.scan_one(&cfg).unwrap());
        cfg.spool_max_bytes = state.status(&cfg).unwrap().pending_bytes;
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000004"),
        );
        assert!(!state.scan_one(&cfg).unwrap()); // full spool records the source frontier
        let _old_inode = File::open(&cfg.access_log).unwrap();
        fs::remove_file(&cfg.access_log).unwrap();
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000005"),
        );
        assert!(!state.scan_one(&cfg).unwrap()); // first absent snapshot allows atomic rotation
        std::thread::sleep(Duration::from_millis(MISSING_CONFIRMATION_MS as u64 + 10));
        assert!(state.scan_one(&cfg).is_err()); // persistent second snapshot confirms loss
    }

    #[test]
    fn fast_same_inode_rename_gaps_do_not_confirm_missing_generation() {
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::Arc;

        let dir = tempdir().unwrap();
        let cfg = config(dir.path());
        File::create(&cfg.access_log).unwrap();
        let mut state = State::open(&cfg).unwrap();
        assert!(!state.scan_one(&cfg).unwrap());

        let running = Arc::new(AtomicBool::new(true));
        let worker_running = Arc::clone(&running);
        let base = cfg.access_log.clone();
        let rotated = PathBuf::from(format!("{}.1", cfg.access_log.display()));
        let worker = std::thread::spawn(move || {
            for _ in 0..180 {
                if fs::rename(&base, &rotated).is_err() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(2));
                if fs::rename(&rotated, &base).is_err() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            worker_running.store(false, Ordering::Release);
        });

        while running.load(Ordering::Acquire) {
            assert!(!state.scan_one(&cfg).unwrap());
            std::thread::yield_now();
        }
        worker.join().unwrap();
        assert!(!state.scan_one(&cfg).unwrap());
        assert!(cfg.access_log.exists());
    }

    #[test]
    fn a_present_inode_resets_missing_debounce_before_early_return() {
        let dir = tempdir().unwrap();
        let cfg = config(dir.path());
        File::create(&cfg.access_log).unwrap();
        let rotated = PathBuf::from(format!("{}.1", cfg.access_log.display()));
        File::create(&rotated).unwrap();
        let mut state = State::open(&cfg).unwrap();
        assert!(!state.scan_one(&cfg).unwrap());

        let metadata = fs::metadata(&rotated).unwrap();
        let identity = (metadata.dev() as i64, metadata.ino() as i64);
        state.db.execute(
            "UPDATE checkpoints SET missing_scans=1,missing_since_ms=?3 WHERE dev=?1 AND ino=?2",
            params![identity.0, identity.1, system_time_ms() - MISSING_CONFIRMATION_MS - 1],
        ).unwrap();

        // Keep the base inode missing (so scan_one returns early), while the
        // rotated inode is present in the snapshot and must clear its timer.
        let hidden_base = dir.path().join("access.hidden");
        fs::rename(&cfg.access_log, &hidden_base).unwrap();
        assert!(!state.scan_one(&cfg).unwrap());
        let debounce: (u32, i64) = state
            .db
            .query_row(
                "SELECT missing_scans,missing_since_ms FROM checkpoints WHERE dev=?1 AND ino=?2",
                params![identity.0, identity.1],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(debounce, (0, 0));
        fs::rename(hidden_base, &cfg.access_log).unwrap();
    }

    #[test]
    fn partial_line_is_not_checkpointed_until_complete() {
        let dir = tempdir().unwrap();
        let cfg = config(dir.path());
        let mut file = File::create(&cfg.access_log).unwrap();
        write!(file, "{}", fixture("00000000-0000-4000-8000-000000000003")).unwrap();
        file.sync_all().unwrap();
        let mut state = State::open(&cfg).unwrap();
        assert!(!state.scan_one(&cfg).unwrap());
        assert_eq!(state.status(&cfg).unwrap().pending_events, 0);
        drop(file);
        let mut file = OpenOptions::new()
            .append(true)
            .open(&cfg.access_log)
            .unwrap();
        file.write_all(b"\n").unwrap();
        file.sync_all().unwrap();
        assert!(state.scan_one(&cfg).unwrap());
        assert_eq!(state.status(&cfg).unwrap().pending_events, 1);
    }

    #[test]
    fn export_retains_queued_batches_and_lock_is_exclusive() {
        let dir = tempdir().unwrap();
        let cfg = config(dir.path());
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000003"),
        );
        let mut state = State::open(&cfg).unwrap();
        assert!(State::open(&cfg).is_err());
        assert!(state.scan_one(&cfg).unwrap());
        let out = dir.path().join("export.jsonl");
        state.export(out.to_str().unwrap(), &cfg).unwrap();
        assert_eq!(fs::read_to_string(out).unwrap().lines().count(), 1);
        assert_eq!(state.status(&cfg).unwrap().pending_batches, 1);
    }

    #[test]
    fn spool_quota_pauses_at_a_durable_checkpoint() {
        let dir = tempdir().unwrap();
        let mut cfg = config(dir.path());
        cfg.spool_max_bytes = 1024;
        cfg.db_max_bytes = 131_072;
        cfg.batch_max_bytes = 1024;
        cfg.max_line_bytes = 1024;
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000003"),
        );
        append(
            &cfg.access_log,
            &fixture("00000000-0000-4000-8000-000000000004"),
        );
        let mut state = State::open(&cfg).unwrap();
        assert!(state.scan_one(&cfg).unwrap());
        let status = state.status(&cfg).unwrap();
        assert!(status.pending_bytes <= cfg.spool_max_bytes);
        let offset: u64 = state
            .db
            .query_row("SELECT offset FROM checkpoints", [], |r| r.get(0))
            .unwrap();
        assert!(!state.scan_one(&cfg).unwrap());
        assert_eq!(
            offset,
            state
                .db
                .query_row("SELECT offset FROM checkpoints", [], |r| r.get::<_, u64>(0))
                .unwrap()
        );
    }
}
