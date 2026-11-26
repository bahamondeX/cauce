//! Application-server transport. A request takes an exclusive socket from the
//! shared pool and yields it only after the protocol's terminal record.
mod cgi;
pub(crate) mod config;
mod envelope;
mod fastcgi;

use config::Protocol;

use crate::{body::ProxyBody, config::Limits, ClientIo};
use anyhow::{bail, ensure, Context, Result};
use bytes::{Bytes, BytesMut};
use futures_util::{stream, Stream};
use http_body_util::BodyExt;
use hyper::{
    body::{Body, Frame},
    header::CONTENT_LENGTH,
    http::uri::Authority,
    Method, Request, Response, StatusCode,
};
use std::{
    io,
    net::IpAddr,
    pin::Pin,
    sync::{
        atomic::{AtomicU8, Ordering},
        Arc, Mutex,
    },
    task::{Context as TaskContext, Poll},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    sync::Semaphore,
};

pub(crate) type Socket = Box<dyn ClientIo>;
// The body owns transport I/O. The lease owns the pool permits and takes
// an intact socket back only after observing successful protocol completion.
pub(crate) type Returned = Arc<Mutex<Option<Socket>>>;

pub(crate) const TOTAL_BUFFER_BYTES: usize = 128 * 1024 * 1024;

pub(crate) fn length<B: Body>(request: &Request<B>) -> Result<Option<u64>> {
    let declared = cgi::content_length(request.headers())?;
    Ok(declared
        .or_else(|| request.body().size_hint().exact())
        .or_else(|| request.body().is_end_stream().then_some(0)))
}

pub(crate) struct Contextual<'a> {
    pub route: &'a config::Route,
    pub authority: &'a Authority,
    pub client: IpAddr,
    pub secure: bool,
    pub length: Option<u64>,
    pub buffers: &'a Arc<Semaphore>,
    pub limits: &'a Limits,
    pub upload_error: &'a AtomicU8,
}

fn reported(error: &AtomicU8, code: u8, message: &'static str) -> anyhow::Error {
    error.store(code, Ordering::Release);
    anyhow::anyhow!(message)
}

pub(crate) async fn exchange(
    mut socket: Socket,
    returned: Returned,
    request: Request<ProxyBody>,
    context: Contextual<'_>,
) -> Result<Response<ResponseBody>> {
    let Contextual {
        route,
        authority,
        client,
        secure,
        length,
        buffers,
        limits,
        upload_error,
    } = context;
    let (parts, mut upload) = request.into_parts();
    let public = Request::from_parts(parts, ());
    let head = public.method() == Method::HEAD;
    let mut buffered = None;
    let mut retained = None;
    let length = if let Some(buffer) = route
        .settings
        .request_buffer_bytes
        .filter(|_| length != Some(0))
    {
        if length.is_some_and(|n| n > u64::from(buffer)) {
            return Err(reported(
                upload_error,
                3,
                "application request buffer limit",
            ));
        }
        retained = Some(
            buffers
                .clone()
                .try_acquire_many_owned(buffer)
                .map_err(|_| reported(upload_error, 5, "application request buffer capacity"))?,
        );
        let mut bytes = BytesMut::new();
        while let Some(data) = next_upload(&mut upload, upload_error).await? {
            if data.len() > buffer as usize - bytes.len() {
                return Err(reported(
                    upload_error,
                    3,
                    "application request buffer limit",
                ));
            }
            bytes.extend_from_slice(&data);
        }
        let actual = bytes.len() as u64;
        if length.is_some_and(|n| n != actual) {
            return Err(reported(
                upload_error,
                4,
                "application request body length mismatch",
            ));
        }
        buffered = Some(bytes.freeze());
        actual
    } else {
        length.context("missing application request length")?
    };
    let params =
        cgi::params(route, &public, client, secure, authority, length).inspect_err(|_| {
            upload_error.store(6, Ordering::Release);
        })?;
    match route.protocol {
        Protocol::Fastcgi => {
            let mut begin = [0; 8];
            begin[1] = 1; // FCGI_RESPONDER
            begin[2] = u8::from(route.settings.keep_connection);
            write_record(&mut socket, fastcgi::BEGIN, &begin).await?;
            write_record(&mut socket, fastcgi::PARAMS, &params).await?;
            write_record(&mut socket, fastcgi::PARAMS, b"").await?;
        }
        Protocol::Uwsgi => {
            let prefix = envelope::uwsgi_prefix(
                params.len(),
                route.settings.modifier1.unwrap_or(0),
                route.settings.modifier2.unwrap_or(0),
            )
            .expect("application parameter limit");
            socket.write_all(&prefix).await?;
            socket.write_all(&params).await?;
        }
        Protocol::Scgi => {
            let prefix = envelope::scgi_prefix(params.len()).expect("application parameter limit");
            socket.write_all(prefix.as_bytes()).await?;
            socket.write_all(&params).await?;
            socket.write_all(b",").await?;
        }
    }
    let (read, mut write) = tokio::io::split(socket);
    let mut reader = Reader {
        io: read,
        protocol: route.protocol,
        pending: Bytes::new(),
        stdout_closed: false,
        stderr_closed: false,
        stderr_bytes: 0,
    };
    let writing = async {
        let mut sent = 0;
        if let Some(data) = buffered {
            for chunk in data.chunks(16 * 1024) {
                write_upload(&mut write, route.protocol, chunk).await?;
            }
            sent = data.len() as u64;
        } else {
            while let Some(data) = next_upload(&mut upload, upload_error).await? {
                if data.len() as u64 > length - sent {
                    return Err(reported(
                        upload_error,
                        4,
                        "application request body exceeds content length",
                    ));
                }
                for chunk in data.chunks(16 * 1024) {
                    write_upload(&mut write, route.protocol, chunk).await?;
                }
                sent += data.len() as u64;
            }
        }
        if sent != length {
            return Err(reported(
                upload_error,
                4,
                "application request body is truncated",
            ));
        }
        if route.protocol == Protocol::Fastcgi {
            write_record(&mut write, fastcgi::STDIN, b"").await?;
        }
        Ok::<_, anyhow::Error>(())
    };
    let ((), mut response) = tokio::try_join!(writing, reader.head(limits))?;
    drop(retained);
    let mut reader = reader.unsplit(write);
    let remaining = cgi::content_length(response.headers())?;
    let no_body = head
        || matches!(
            response.status(),
            StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED | StatusCode::RESET_CONTENT
        );
    let returned = route.settings.keep_connection.then_some(returned);
    if no_body {
        let mut count = 0u64;
        while let Some(data) = reader.data().await? {
            count = count.saturating_add(data.len() as u64);
            ensure!(
                count <= limits.max_body_bytes,
                "discarded CGI body exceeds limit"
            );
        }
        save_socket(returned, reader);
        if matches!(
            response.status(),
            StatusCode::NO_CONTENT | StatusCode::RESET_CONTENT
        ) {
            response.headers_mut().remove(CONTENT_LENGTH);
        }
        return Ok(response.map(|_| ResponseBody { stream: None }));
    }
    if let Some(length) = remaining {
        ensure!(
            length <= limits.max_body_bytes,
            "CGI response length exceeds limit"
        );
        if length == 0 {
            ensure!(
                reader.data().await?.is_none(),
                "CGI body exceeds content length"
            );
            save_socket(returned, reader);
            return Ok(response.map(|_| ResponseBody { stream: None }));
        }
    }
    let frames = stream::try_unfold(Some((reader, remaining, returned)), |state| async {
        let Some((mut reader, remaining, returned)) = state else {
            return Ok(None);
        };
        let next = reader.data().await.map_err(as_io)?;
        match (next, remaining) {
            (None, Some(n)) if n != 0 => Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "CGI body is truncated",
            )),
            (None, _) => {
                save_socket(returned, reader);
                Ok(None)
            }
            (Some(data), Some(n)) if data.len() as u64 > n => {
                Err(io::Error::other("CGI body exceeds content length"))
            }
            (Some(data), Some(n)) if data.len() as u64 == n => {
                if reader.data().await.map_err(as_io)?.is_some() {
                    return Err(io::Error::other("CGI body exceeds content length"));
                }
                save_socket(returned, reader);
                Ok(Some(((Frame::data(data), true), None)))
            }
            (Some(data), n) => {
                let remaining = n.map(|remaining| remaining - data.len() as u64);
                Ok(Some((
                    (Frame::data(data), false),
                    Some((reader, remaining, returned)),
                )))
            }
        }
    });
    Ok(response.map(|_| ResponseBody {
        stream: Some(Box::pin(frames)),
    }))
}

fn as_io(error: anyhow::Error) -> io::Error {
    io::Error::other(error)
}

fn save_socket(returned: Option<Returned>, reader: Reader<Socket>) {
    if let Some(returned) = returned {
        *returned.lock().unwrap() = Some(reader.io);
    }
}

async fn next_upload(body: &mut ProxyBody, error: &AtomicU8) -> Result<Option<Bytes>> {
    loop {
        let Some(frame) = body.frame().await else {
            return Ok(None);
        };
        let frame = frame.map_err(io::Error::other)?;
        let data = frame
            .into_data()
            .map_err(|_| reported(error, 4, "CGI request trailers are unsupported"))?;
        if !data.is_empty() {
            return Ok(Some(data));
        }
    }
}

async fn write_upload(
    io: &mut (impl AsyncWrite + Unpin),
    protocol: Protocol,
    data: &[u8],
) -> io::Result<()> {
    if protocol == Protocol::Fastcgi {
        write_record(io, fastcgi::STDIN, data).await
    } else {
        io.write_all(data).await
    }
}

async fn write_record(io: &mut (impl AsyncWrite + Unpin), kind: u8, data: &[u8]) -> io::Result<()> {
    io.write_all(&fastcgi::header(
        kind,
        u16::try_from(data.len()).expect("bounded FastCGI record"),
    ))
    .await?;
    io.write_all(data).await
}

struct Reader<T> {
    io: T,
    protocol: Protocol,
    pending: Bytes,
    stdout_closed: bool,
    stderr_closed: bool,
    stderr_bytes: usize,
}

impl Reader<tokio::io::ReadHalf<Socket>> {
    fn unsplit(self, write: tokio::io::WriteHalf<Socket>) -> Reader<Socket> {
        Reader {
            io: self.io.unsplit(write),
            protocol: self.protocol,
            pending: self.pending,
            stdout_closed: self.stdout_closed,
            stderr_closed: self.stderr_closed,
            stderr_bytes: self.stderr_bytes,
        }
    }
}

impl<T: AsyncRead + Unpin> Reader<T> {
    async fn head(&mut self, limits: &Limits) -> Result<Response<()>> {
        let mut headers = BytesMut::new();
        while let Some(data) = self.data().await? {
            let prev = headers.len();
            headers.extend_from_slice(&data);
            if let Some(end) = cgi::header_end(&headers, prev) {
                ensure!(
                    end <= limits.max_header_bytes,
                    "CGI response headers exceed limit"
                );
                self.pending = headers.split_off(end).freeze();
                return cgi::response(&headers, limits);
            }
            ensure!(
                headers.len() < limits.max_header_bytes,
                "CGI response headers exceed limit"
            );
        }
        bail!("CGI response ended before headers")
    }

    async fn data(&mut self) -> Result<Option<Bytes>> {
        if !self.pending.is_empty() {
            return Ok(Some(std::mem::take(&mut self.pending)));
        }
        if self.protocol != Protocol::Fastcgi {
            let mut buffer = BytesMut::with_capacity(16 * 1024);
            return Ok((self.io.read_buf(&mut buffer).await? != 0).then(|| buffer.freeze()));
        }
        loop {
            let mut header = [0; 8];
            self.io.read_exact(&mut header).await?;
            let record = fastcgi::parse_header(header).context("invalid FastCGI record")?;
            match record.kind {
                fastcgi::STDOUT if record.length != 0 => {
                    ensure!(!self.stdout_closed, "FastCGI stdout reopened");
                    let mut payload = vec![0; record.length];
                    self.io.read_exact(&mut payload).await?;
                    discard(&mut self.io, record.padding).await?;
                    return Ok(Some(Bytes::from(payload)));
                }
                fastcgi::STDOUT => {
                    ensure!(!self.stdout_closed, "duplicate FastCGI stdout terminator");
                    self.stdout_closed = true;
                    discard(&mut self.io, record.padding).await?;
                }
                fastcgi::STDERR => {
                    ensure!(!self.stderr_closed, "FastCGI stderr reopened");
                    self.stderr_closed = record.length == 0;
                    self.stderr_bytes += record.length;
                    ensure!(
                        self.stderr_bytes <= 64 * 1024,
                        "FastCGI stderr exceeds limit"
                    );
                    discard(&mut self.io, record.length + record.padding).await?;
                }
                fastcgi::END => {
                    let mut end = [0; 8];
                    self.io.read_exact(&mut end).await?;
                    ensure!(fastcgi::ended(end), "FastCGI responder rejected request");
                    discard(&mut self.io, record.padding).await?;
                    return Ok(None);
                }
                _ => unreachable!(),
            }
        }
    }
}

async fn discard(reader: &mut (impl AsyncRead + Unpin), mut size: usize) -> io::Result<()> {
    let mut buffer = [0; 512];
    while size != 0 {
        let chunk = size.min(buffer.len());
        reader.read_exact(&mut buffer[..chunk]).await?;
        size -= chunk;
    }
    Ok(())
}

type Frames = Pin<Box<dyn Stream<Item = io::Result<(Frame<Bytes>, bool)>> + Send>>;

pub(crate) struct ResponseBody {
    stream: Option<Frames>,
}

impl Body for ResponseBody {
    type Data = Bytes;
    type Error = io::Error;

    fn is_end_stream(&self) -> bool {
        self.stream.is_none()
    }

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut TaskContext<'_>,
    ) -> Poll<Option<io::Result<Frame<Bytes>>>> {
        let Some(stream) = &mut self.stream else {
            return Poll::Ready(None);
        };
        match stream.as_mut().poll_next(cx) {
            Poll::Ready(Some(Ok((frame, done)))) => {
                if done {
                    self.stream = None;
                }
                Poll::Ready(Some(Ok(frame)))
            }
            Poll::Ready(Some(Err(error))) => {
                self.stream = None;
                Poll::Ready(Some(Err(error)))
            }
            Poll::Ready(None) => {
                self.stream = None;
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}
