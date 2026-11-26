//! Static files opened beneath a configured directory descriptor.
use crate::{
    body::{self, BoxError, ProxyBody},
    compression::{self, Coding},
    config::{PathMatch, Route},
};
use anyhow::{ensure, Context, Result};
use bytes::Bytes;
use futures_util::stream;
use http_body_util::{BodyExt, StreamBody};
use hyper::{body::Frame, header::*, Method, Request, Response, StatusCode};
use serde::{Deserialize, Serialize};
use std::{
    collections::VecDeque,
    ffi::{CStr, CString},
    fmt::Write,
    fs::{File, Metadata},
    io,
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{
            ffi::OsStrExt,
            fs::{FileExt, MetadataExt},
        },
    },
    path::{Component, Path},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

const CHUNK: usize = 64 * 1024;
const MAX_RANGES: usize = 8;
const MAX_LIST_ENTRIES: usize = 1024;
const MAX_LIST_BYTES: usize = 256 * 1024;

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Spec {
    root: Option<String>,
    alias: Option<String>,
    index: Vec<String>,
    listing: bool,
    hidden: bool,
    deny_suffixes: Vec<String>,
    etag: bool,
    precompressed: bool,
    cache_control: Option<String>,
    expires_seconds: Option<u32>,
    max_open: usize,
    try_files: Vec<String>,
}
impl Default for Spec {
    fn default() -> Self {
        Self {
            root: None,
            alias: None,
            index: vec!["index.html".into()],
            listing: false,
            hidden: false,
            deny_suffixes: [".pem", ".key", ".env", ".bak", ".sql", "~"]
                .map(str::to_owned)
                .into(),
            etag: true,
            precompressed: false,
            cache_control: None,
            expires_seconds: None,
            max_open: 32,
            try_files: vec![],
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(try_from = "Spec", into = "Spec")]
pub(crate) struct Policy {
    spec: Arc<Spec>,
    root: Arc<File>,
    gate: Arc<Semaphore>,
    cache_control: Option<HeaderValue>,
}
impl PartialEq for Policy {
    fn eq(&self, other: &Self) -> bool {
        self.spec == other.spec
    }
}
impl From<Policy> for Spec {
    fn from(policy: Policy) -> Self {
        Arc::unwrap_or_clone(policy.spec)
    }
}
impl TryFrom<Spec> for Policy {
    type Error = anyhow::Error;
    fn try_from(spec: Spec) -> Result<Self> {
        let path = match (spec.root.as_deref(), spec.alias.as_deref()) {
            (Some(root), None) | (None, Some(root)) => Path::new(root),
            _ => anyhow::bail!("static content requires exactly one root or alias"),
        };
        ensure!(
            path.is_absolute() && path != Path::new("/"),
            "static root must be an absolute document directory"
        );
        ensure!(
            (1..=256).contains(&spec.max_open)
                && spec.index.len() <= 16
                && spec.index.iter().all(|name| safe_part(name.as_bytes()))
                && spec.deny_suffixes.len() <= 32
                && spec
                    .deny_suffixes
                    .iter()
                    .all(|suffix| !suffix.is_empty() && suffix.len() <= 32 && suffix.is_ascii())
                && spec.try_files.len() <= 8,
            "invalid static bounds or filenames"
        );
        ensure!(spec.try_files.iter().all(|item| matches!(item.as_str(), "$uri" | "$uri/") || safe_part(item.as_bytes())),
            "static try_files supports $uri, $uri/ and safe relative filenames");
        let cache_control = spec
            .cache_control
            .as_deref()
            .map(HeaderValue::from_str)
            .transpose()?;
        let mut root = File::open("/")?;
        for component in path.components() {
            match component {
                Component::RootDir => (),
                Component::Normal(name) => {
                    root = open_child(&root, name.as_bytes(), true)
                        .context("open static document root")?
                }
                _ => anyhow::bail!("invalid static document root"),
            }
        }
        let gate = Arc::new(Semaphore::new(spec.max_open));
        Ok(Self {
            spec: Arc::new(spec),
            root: Arc::new(root),
            gate,
            cache_control,
        })
    }
}
impl Policy {
    pub fn validate(&self, route: &Route) -> Result<()> {
        ensure!(
            self.spec.alias.is_none()
                || matches!(
                    route.path_match,
                    PathMatch::Prefix | PathMatch::PrefixNoRegex | PathMatch::Exact
                )
                || (route.path_match == PathMatch::LiteralPrefix
                    && route.path_prefix.ends_with('/')),
            "alias requires a segment-aware literal location"
        );
        Ok(())
    }
    pub async fn response<B>(
        &self,
        request: &Request<B>,
        route: &Route,
        compression: Option<(&compression::Policy, compression::Choice)>,
    ) -> Response<ProxyBody> {
        if !matches!(*request.method(), Method::GET | Method::HEAD) {
            return Response::builder()
                .status(405)
                .header(ALLOW, "GET, HEAD")
                .body(body::full(""))
                .unwrap();
        }
        let head = request.method() == Method::HEAD;
        let path = if self.spec.alias.is_some() {
            request
                .uri()
                .path()
                .strip_prefix(&route.path_prefix)
                .unwrap_or("/")
        } else {
            request.uri().path()
        };
        let Some(parts) =
            decode_path(path).filter(|parts| parts.iter().all(|name| self.permitted(name)))
        else {
            return empty(StatusCode::NOT_FOUND);
        };
        // The blocking operation owns the permit even if the awaiting request
        // is cancelled. A streaming body retains it until the file is closed.
        let Ok(permit) = self.gate.clone().try_acquire_owned() else {
            return empty(StatusCode::SERVICE_UNAVAILABLE);
        };
        let trailing_slash = request.uri().path().ends_with('/');
        let encoding = compression
            .and_then(|(_, choice)| choice.coding)
            .filter(|_| {
                self.spec.precompressed
                    && self.cache_control.as_ref().is_none_or(|value| {
                        value.to_str().is_ok_and(|value| {
                            !value.split(',').any(|field| {
                                let name = field.split('=').next().unwrap_or_default().trim();
                                ["no-transform", "private", "no-store"]
                                    .iter()
                                    .any(|token| name.eq_ignore_ascii_case(token))
                            })
                        })
                    })
            });
        let policy = self.clone();
        let found = tokio::task::spawn_blocking(move || {
            policy
                .lookup(parts, trailing_slash, encoding)
                .map(|file| (file, permit))
        })
        .await;
        let (found, permit) = match found {
            Ok(Ok(found)) => found,
            Ok(Err(error))
                if matches!(
                    error.raw_os_error(),
                    Some(libc::ENOSPC | libc::EMFILE | libc::ENFILE)
                ) =>
            {
                return empty(StatusCode::SERVICE_UNAVAILABLE)
            }
            Ok(Err(_)) => return empty(StatusCode::NOT_FOUND),
            Err(_) => return empty(StatusCode::INTERNAL_SERVER_ERROR),
        };
        match found {
            Found::Directory => {
                let uri = request.uri();
                let mut location = format!("{}/", uri.path());
                if let Some(query) = uri.query() {
                    let _ = write!(location, "?{query}");
                }
                Response::builder()
                    .status(301)
                    .header(LOCATION, location)
                    .body(body::full(""))
                    .unwrap()
            }
            Found::Listing(data) => Response::builder()
                .header(CONTENT_TYPE, "text/html; charset=utf-8")
                .header("x-content-type-options", "nosniff")
                .header(CONTENT_LENGTH, data.len())
                .body(body::full(if head { String::new() } else { data }))
                .unwrap(),
            Found::File {
                file,
                name,
                metadata,
                variant,
            } => {
                let mut selected = (file, metadata, None);
                if let (Some((compressed, coding)), Some((policy, choice))) = (variant, compression)
                {
                    if policy.precompressed(choice, selected.1.len(), mime(&name)) == Some(coding) {
                        if let Ok(stat) = compressed.metadata() {
                            selected = (compressed, stat, Some(coding));
                        }
                    }
                }
                let mut response =
                    self.file_response(request, selected.0, &name, selected.1, permit);
                if compression.is_some() {
                    compression::vary(response.headers_mut());
                }
                if let Some(coding) = selected.2 {
                    response
                        .headers_mut()
                        .insert(CONTENT_ENCODING, HeaderValue::from_static(coding.name()));
                    response.headers_mut().remove(ACCEPT_RANGES);
                }
                response
            }
        }
    }
    fn permitted(&self, name: &[u8]) -> bool {
        safe_part(name)
            && (self.spec.hidden || name.first() != Some(&b'.'))
            && !self.spec.deny_suffixes.iter().any(|suffix| {
                name.get(name.len().saturating_sub(suffix.len())..)
                    .is_some_and(|end| end.eq_ignore_ascii_case(suffix.as_bytes()))
            })
    }
    fn lookup(
        &self,
        parts: Vec<Vec<u8>>,
        slash: bool,
        encoding: Option<Coding>,
    ) -> io::Result<Found> {
        let original = || open_parts(&self.root, &parts);
        let file = if self.spec.try_files.is_empty() {
            original()?
        } else {
            self.spec
                .try_files
                .iter()
                .find_map(|candidate| {
                    let item = match candidate.as_str() {
                        "$uri" => original()
                            .ok()
                            .filter(|(file, _)| file.metadata().is_ok_and(|m| m.is_file())),
                        "$uri/" => original()
                            .ok()
                            .filter(|(file, _)| file.metadata().is_ok_and(|m| m.is_dir())),
                        name if self.permitted(name.as_bytes()) => {
                            open_child(&self.root, name.as_bytes(), false)
                                .ok()
                                .filter(|file| file.metadata().is_ok_and(|m| m.is_file()))
                                .map(|file| (file, name.as_bytes().to_vec()))
                        }
                        _ => None,
                    };
                    item
                })
                .ok_or_else(not_found)?
        };
        let metadata = file.0.metadata()?;
        if metadata.is_file() && (!slash || !self.spec.try_files.is_empty()) {
            let variant = encoding
                .filter(|_| self.spec.try_files.is_empty())
                .and_then(|coding| {
                    let mut original = parts;
                    original.pop();
                    original.push(variant_name(&file.1, coding));
                    let candidate = open_parts(&self.root, &original).ok()?.0;
                    valid_variant(candidate, &metadata, coding)
                });
            return Ok(Found::File {
                file: file.0,
                name: file.1,
                metadata,
                variant,
            });
        }
        if !metadata.is_dir() {
            return Err(not_found());
        }
        if !slash {
            return Ok(Found::Directory);
        }
        for index in self
            .spec
            .index
            .iter()
            .filter(|index| self.permitted(index.as_bytes()))
        {
            if let Ok(child) = open_child(&file.0, index.as_bytes(), false) {
                let metadata = child.metadata()?;
                if metadata.is_file() {
                    let variant = encoding.and_then(|coding| {
                        let candidate =
                            open_child(&file.0, &variant_name(index.as_bytes(), coding), false)
                                .ok()?;
                        valid_variant(candidate, &metadata, coding)
                    });
                    return Ok(Found::File {
                        file: child,
                        name: index.as_bytes().to_vec(),
                        metadata,
                        variant,
                    });
                }
            }
        }
        if self.spec.listing {
            return Ok(Found::Listing(self.list(&file.0)?));
        }
        Err(not_found())
    }
    fn list(&self, file: &File) -> io::Result<String> {
        let dir = Directory::open(file)?;
        let mut entries = Vec::new();
        for i in 0..=4096 {
            let Some(name) = dir.next() else {
                break;
            };
            if i == 4096 {
                return Err(not_found());
            }
            if !self.permitted(&name) {
                continue;
            }
            // Open with the same portable policy before linking. Symlinks,
            // special files and entries removed by a concurrent writer are omitted.
            if let Ok(item) = open_child(file, &name, false) {
                let m = item.metadata()?;
                if !(m.is_file() || m.is_dir()) {
                    continue;
                }
                if entries.len() == MAX_LIST_ENTRIES {
                    return Err(not_found());
                }
                entries.push((name, m.is_dir()));
            }
        }
        entries.sort_unstable();
        let mut html = String::from("<!doctype html><meta charset=utf-8><title>Index</title><ul>");
        for (name, directory) in entries {
            html.push_str("<li><a href=\"");
            for &byte in &name {
                if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) {
                    html.push(char::from(byte));
                } else {
                    let _ = write!(html, "%{byte:02X}");
                }
            }
            if directory {
                html.push('/');
            }
            html.push_str("\">");
            for ch in String::from_utf8_lossy(&name).chars() {
                match ch {
                    '&' => html.push_str("&amp;"),
                    '<' => html.push_str("&lt;"),
                    '>' => html.push_str("&gt;"),
                    _ => html.push(ch),
                }
            }
            if directory {
                html.push('/');
            }
            html.push_str("</a></li>");
            if html.len() > MAX_LIST_BYTES {
                return Err(not_found());
            }
        }
        html.push_str("</ul>");
        Ok(html)
    }
    fn file_response<B>(
        &self,
        request: &Request<B>,
        file: File,
        name: &[u8],
        m: Metadata,
        permit: OwnedSemaphorePermit,
    ) -> Response<ProxyBody> {
        let size = m.len();
        let modified = m
            .modified()
            .ok()
            .filter(|time| *time >= UNIX_EPOCH && *time <= SystemTime::now());
        // Metadata validators are weak. If-Range therefore accepts only a date.
        let etag = self.spec.etag.then(|| {
            format!(
                "W/\"{:x}-{:x}-{:x}-{:x}-{:x}\"",
                size,
                m.mtime(),
                m.mtime_nsec(),
                m.ctime(),
                m.ctime_nsec()
            )
        });
        let mut response = Response::builder()
            .header(ACCEPT_RANGES, "bytes")
            .header("x-content-type-options", "nosniff");
        if let Some(etag) = &etag {
            response = response.header(ETAG, etag);
        }
        if let Some(modified) = modified {
            response = response.header(LAST_MODIFIED, httpdate::fmt_http_date(modified));
        }
        if let Some(cache) = &self.cache_control {
            response = response.header(CACHE_CONTROL, cache);
        }
        if let Some(seconds) = self.spec.expires_seconds {
            response = response.header(
                EXPIRES,
                httpdate::fmt_http_date(SystemTime::now() + Duration::from_secs(seconds.into())),
            );
        }
        let headers = request.headers();
        if let Some(status) = precondition(headers, etag.as_deref(), modified) {
            return response.status(status).body(body::full("")).unwrap();
        }
        let mime = mime(name);
        if request.method() == Method::HEAD {
            return response
                .header(CONTENT_TYPE, mime)
                .header(CONTENT_LENGTH, size)
                .body(body::full(""))
                .unwrap();
        }
        let selected = if headers.get(IF_RANGE).is_none_or(|value| {
            value
                .to_str()
                .ok()
                .and_then(|s| httpdate::parse_http_date(s).ok())
                .zip(modified)
                .is_some_and(|(date, m)| second(m) == second(date))
        }) {
            headers
                .get(RANGE)
                .and_then(|value| value.to_str().ok())
                .map(|value| ranges(value, size))
                .unwrap_or(Ranges::Ignore)
        } else {
            Ranges::Ignore
        };
        let (segments, length) = match selected {
            Ranges::Unsatisfiable => {
                return response
                    .status(416)
                    .header(CONTENT_RANGE, format!("bytes */{size}"))
                    .body(body::full(""))
                    .unwrap()
            }
            Ranges::Ignore => {
                response = response.header(CONTENT_TYPE, mime);
                (VecDeque::from([Segment::File(0, size)]), size)
            }
            Ranges::Parts(parts) if parts.len() == 1 => {
                let (start, end) = parts[0];
                response = response
                    .status(206)
                    .header(CONTENT_TYPE, mime)
                    .header(CONTENT_RANGE, format!("bytes {start}-{end}/{size}"));
                (
                    VecDeque::from([Segment::File(start, end - start + 1)]),
                    end - start + 1,
                )
            }
            Ranges::Parts(parts) => {
                let boundary = uuid::Uuid::new_v4().simple().to_string();
                response = response.status(206).header(
                    CONTENT_TYPE,
                    format!("multipart/byteranges; boundary={boundary}"),
                );
                let mut segments = VecDeque::new();
                for (i, (start, end)) in parts.into_iter().enumerate() {
                    let line = if i == 0 { "" } else { "\r\n" };
                    segments.push_back(Segment::Literal(Bytes::from(format!("{line}--{boundary}\r\nContent-Type: {mime}\r\nContent-Range: bytes {start}-{end}/{size}\r\n\r\n"))));
                    segments.push_back(Segment::File(start, end - start + 1));
                }
                segments.push_back(Segment::Literal(Bytes::from(format!(
                    "\r\n--{boundary}--\r\n"
                ))));
                let Some(length) = segments
                    .iter()
                    .try_fold(0u64, |n, segment| n.checked_add(segment.len()))
                else {
                    return empty(StatusCode::INTERNAL_SERVER_ERROR);
                };
                (segments, length)
            }
        };
        let stream = if length == 0 {
            body::full("")
        } else {
            file_body(file, segments, permit)
        };
        response
            .header(CONTENT_LENGTH, length)
            .body(stream)
            .unwrap()
    }
}

enum Found {
    File {
        file: File,
        name: Vec<u8>,
        metadata: Metadata,
        variant: Option<(File, Coding)>,
    },
    Directory,
    Listing(String),
}
fn variant_name(name: &[u8], coding: Coding) -> Vec<u8> {
    [name, coding.suffix().as_bytes()].concat()
}
fn valid_variant(file: File, original: &Metadata, coding: Coding) -> Option<(File, Coding)> {
    let metadata = file.metadata().ok()?;
    (metadata.is_file()
        && metadata.len() > 0
        && metadata.modified().ok()? >= original.modified().ok()?)
    .then_some((file, coding))
}
fn not_found() -> io::Error {
    io::Error::from(io::ErrorKind::NotFound)
}
fn empty(status: StatusCode) -> Response<ProxyBody> {
    Response::builder()
        .status(status)
        .body(body::full(""))
        .unwrap()
}
fn safe_part(name: &[u8]) -> bool {
    !name.is_empty()
        && name != b"."
        && name != b".."
        && name.len() <= 255
        && name
            .iter()
            .all(|byte| !matches!(*byte, 0..=31 | b'/' | b'\\' | 127))
}
fn decode_path(path: &str) -> Option<Vec<Vec<u8>>> {
    let mut parts = Vec::new();
    for component in path.split('/').filter(|s| !s.is_empty()) {
        if parts.len() == 32 {
            return None;
        }
        let mut bytes = Vec::with_capacity(component.len());
        let mut input = component.as_bytes().iter().copied();
        while let Some(byte) = input.next() {
            if byte == b'%' {
                let high = char::from(input.next()?).to_digit(16)?;
                let low = char::from(input.next()?).to_digit(16)?;
                bytes.push((high * 16 + low) as u8);
            } else {
                bytes.push(byte);
            }
        }
        if !safe_part(&bytes) {
            return None;
        }
        parts.push(bytes);
    }
    Some(parts)
}
fn open_child(parent: &File, name: &[u8], directory: bool) -> io::Result<File> {
    let name = CString::new(name).map_err(|_| not_found())?;
    let flags = libc::O_RDONLY
        | libc::O_CLOEXEC
        | libc::O_NOFOLLOW
        | libc::O_NONBLOCK
        | if directory { libc::O_DIRECTORY } else { 0 };
    // The C string lives until openat returns. Only the returned descriptor is owned.
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
fn open_parts(root: &File, parts: &[Vec<u8>]) -> io::Result<(File, Vec<u8>)> {
    #[cfg(target_os = "linux")]
    if !parts.is_empty() {
        let path = CString::new(parts.join(&b'/')).map_err(|_| not_found())?;
        // struct open_how contains three u64 words. Resolve flags keep the
        // lookup beneath root and reject symlinks, including magic links.
        let how = [
            u64::from((libc::O_CLOEXEC | libc::O_NONBLOCK) as u32),
            0,
            0x08 | 0x04 | 0x02,
        ];
        let fd = unsafe {
            libc::syscall(
                libc::SYS_openat2,
                root.as_raw_fd(),
                path.as_ptr(),
                how.as_ptr(),
                std::mem::size_of_val(&how),
            )
        };
        if fd >= 0 {
            return Ok((
                unsafe { File::from_raw_fd(fd as i32) },
                parts.last().cloned().unwrap_or_default(),
            ));
        }
        let error = io::Error::last_os_error();
        if !matches!(
            error.raw_os_error(),
            Some(libc::ENOSYS | libc::EINVAL | libc::EPERM)
        ) {
            return Err(error);
        }
    }
    let mut dir = root.try_clone()?;
    for (index, name) in parts.iter().enumerate() {
        dir = open_child(&dir, name, index + 1 < parts.len())?;
    }
    Ok((dir, parts.last().cloned().unwrap_or_default()))
}
struct Directory(*mut libc::DIR);
impl Directory {
    fn open(dir: &File) -> io::Result<Self> {
        // Open a new description so directory offsets are not shared by listings.
        let listing = open_child(dir, b".", true)?;
        let handle = unsafe { libc::fdopendir(listing.as_raw_fd()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        std::mem::forget(listing); // POSIX fdopendir takes ownership on success.
        Ok(Self(handle))
    }
    fn next(&self) -> Option<Vec<u8>> {
        let entry = unsafe { libc::readdir(self.0) };
        if entry.is_null() {
            return None;
        }
        // Copy from the DIR buffer before issuing the next readdir call.
        Some(
            unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }
                .to_bytes()
                .to_vec(),
        )
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        unsafe {
            libc::closedir(self.0);
        }
    }
}

fn second(time: SystemTime) -> Option<Duration> {
    time.duration_since(UNIX_EPOCH)
        .ok()
        .map(|d| Duration::from_secs(d.as_secs()))
}
fn tag_matches(headers: &HeaderMap, field: HeaderName, etag: Option<&str>, weak: bool) -> bool {
    headers
        .get_all(field)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .any(|raw| {
            let candidate = raw.trim();
            candidate == "*"
                || etag.is_some_and(|current| {
                    if weak {
                        candidate.trim_start_matches("W/") == current.trim_start_matches("W/")
                    } else {
                        !current.starts_with("W/") && candidate == current
                    }
                })
        })
}
pub(crate) fn precondition(
    headers: &HeaderMap,
    etag: Option<&str>,
    modified: Option<SystemTime>,
) -> Option<StatusCode> {
    let compare = |field| {
        headers
            .get(field)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| httpdate::parse_http_date(value).ok())
            .and_then(second)
            .zip(modified.and_then(second))
    };
    if headers.contains_key(IF_MATCH) {
        if !tag_matches(headers, IF_MATCH, etag, false) {
            return Some(StatusCode::PRECONDITION_FAILED);
        }
    } else if compare(IF_UNMODIFIED_SINCE).is_some_and(|(condition, actual)| actual > condition) {
        return Some(StatusCode::PRECONDITION_FAILED);
    }
    if headers.contains_key(IF_NONE_MATCH) {
        if tag_matches(headers, IF_NONE_MATCH, etag, true) {
            return Some(StatusCode::NOT_MODIFIED);
        }
    } else if compare(IF_MODIFIED_SINCE).is_some_and(|(condition, actual)| actual <= condition) {
        return Some(StatusCode::NOT_MODIFIED);
    }
    None
}

enum Ranges {
    Ignore,
    Unsatisfiable,
    Parts(Vec<(u64, u64)>),
}
fn ranges(value: &str, length: u64) -> Ranges {
    let Some(value) = value.strip_prefix("bytes=") else {
        return Ranges::Ignore;
    };
    let mut ranges = Vec::new();
    let parse = |s: &str| {
        (!s.is_empty() && s.bytes().all(|byte| byte.is_ascii_digit()))
            .then(|| s.parse::<u64>().unwrap_or(u64::MAX))
    };
    for (index, item) in value.split(',').enumerate() {
        if index == MAX_RANGES {
            return Ranges::Ignore;
        }
        let Some((low, high)) = item.trim().split_once('-') else {
            return Ranges::Ignore;
        };
        let pair = if low.is_empty() {
            let Some(suffix) = parse(high) else {
                return Ranges::Ignore;
            };
            if length == 0 || suffix == 0 {
                continue;
            }
            (length.saturating_sub(suffix), length - 1)
        } else {
            let Some(start) = parse(low) else {
                return Ranges::Ignore;
            };
            let end = if high.is_empty() {
                u64::MAX
            } else if let Some(end) = parse(high) {
                end
            } else {
                return Ranges::Ignore;
            };
            if end < start {
                return Ranges::Ignore;
            }
            if start >= length {
                continue;
            }
            (start, end.min(length - 1))
        };
        // Overlapping ranges can amplify work. Treat them as an ordinary GET.
        if ranges
            .iter()
            .any(|&(first, last)| pair.0 <= last && first <= pair.1)
        {
            return Ranges::Ignore;
        }
        ranges.push(pair);
    }
    if ranges.is_empty() {
        Ranges::Unsatisfiable
    } else {
        Ranges::Parts(ranges)
    }
}
enum Segment {
    Literal(Bytes),
    File(u64, u64),
}
impl Segment {
    fn len(&self) -> u64 {
        match self {
            Self::Literal(bytes) => bytes.len() as u64,
            Self::File(_, len) => *len,
        }
    }
}
fn file_body(file: File, segments: VecDeque<Segment>, permit: OwnedSemaphorePermit) -> ProxyBody {
    let reader = (file, permit);
    StreamBody::new(stream::try_unfold(
        (reader, segments),
        |(reader, mut segments)| async move {
            let Some(segment) = segments.pop_front() else {
                return Ok::<_, BoxError>(None);
            };
            let (bytes, reader) = match segment {
                Segment::Literal(bytes) => (bytes, reader),
                Segment::File(offset, left) => {
                    let amount = left.min(CHUNK as u64) as usize;
                    let (chunk, reader) = tokio::task::spawn_blocking(move || {
                        let mut chunk = vec![0; amount];
                        reader.0.read_exact_at(&mut chunk, offset)?;
                        Ok::<_, io::Error>((chunk, reader))
                    })
                    .await??;
                    if left > amount as u64 {
                        segments.push_front(Segment::File(
                            offset + amount as u64,
                            left - amount as u64,
                        ));
                    }
                    (Bytes::from(chunk), reader)
                }
            };
            Ok(Some((Frame::data(bytes), (reader, segments))))
        },
    ))
    .boxed_unsync()
}

fn mime(name: &[u8]) -> &'static str {
    let Some((_, ext)) = name
        .iter()
        .rposition(|&b| b == b'.')
        .map(|index| name.split_at(index + 1))
    else {
        return "application/octet-stream";
    };
    const TYPES: &[(&[u8], &str)] = &[
        (b"html", "text/html"),
        (b"htm", "text/html"),
        (b"css", "text/css"),
        (b"js", "text/javascript"),
        (b"mjs", "text/javascript"),
        (b"txt", "text/plain"),
        (b"json", "application/json"),
        (b"xml", "application/xml"),
        (b"svg", "image/svg+xml"),
        (b"png", "image/png"),
        (b"jpeg", "image/jpeg"),
        (b"jpg", "image/jpeg"),
        (b"gif", "image/gif"),
        (b"webp", "image/webp"),
        (b"ico", "image/x-icon"),
        (b"wasm", "application/wasm"),
        (b"pdf", "application/pdf"),
        (b"woff", "font/woff"),
        (b"woff2", "font/woff2"),
        (b"mp4", "video/mp4"),
        (b"zip", "application/zip"),
    ];
    TYPES
        .iter()
        .find(|(key, _)| ext.eq_ignore_ascii_case(key))
        .map_or("application/octet-stream", |(_, value)| value)
}
