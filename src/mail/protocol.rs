use super::{
    config::{Mechanism, Protocol},
    wire::{self, Credentials, Wire},
};
use anyhow::{ensure, Result};

pub(super) enum Event {
    Upgrade(String),
    Authenticate(Attempt),
    Quit,
}

pub(super) struct Attempt {
    pub credentials: Credentials,
    tag: String,
}

pub(super) struct Negotiation {
    protocol: Protocol,
    mechanisms: Vec<Mechanism>,
    smtp_ehlo: bool,
    pop_user: Option<String>,
    client_helo: Option<String>,
    commands_left: u8,
}

impl Negotiation {
    pub(super) fn new(protocol: Protocol, mechanisms: &[Mechanism]) -> Self {
        Self {
            protocol,
            mechanisms: mechanisms.to_vec(),
            smtp_ehlo: false,
            pop_user: None,
            client_helo: None,
            commands_left: 32,
        }
    }

    pub(super) fn helo(&self) -> Option<&str> {
        self.client_helo.as_deref()
    }

    pub(super) async fn greeting(&self, wire: &mut Wire) -> Result<()> {
        wire::send(
            wire,
            match self.protocol {
                Protocol::Smtp => "220 cauce ESMTP ready\r\n",
                Protocol::Imap => "* OK Cauce ready\r\n",
                Protocol::Pop3 => "+OK Cauce ready\r\n",
            },
        )
        .await
    }

    pub(super) fn upgraded(&mut self) {
        self.smtp_ehlo = false;
        self.pop_user = None;
        self.client_helo = None;
    }

    pub(super) async fn event(&mut self, stream: &mut Wire, secure: bool) -> Result<Event> {
        while self.commands_left > 0 {
            self.commands_left -= 1;
            let line = wire::line(stream).await?;
            let next = match self.protocol {
                Protocol::Smtp => self.smtp(stream, &line, secure).await?,
                Protocol::Imap => self.imap(stream, &line, secure).await?,
                Protocol::Pop3 => self.pop3(stream, &line, secure).await?,
            };
            if let Some(next) = next {
                return Ok(next);
            }
        }
        anyhow::bail!("mail command limit")
    }

    async fn smtp(&mut self, stream: &mut Wire, line: &str, secure: bool) -> Result<Option<Event>> {
        let (command, args) = wire::split(line);
        match command.to_ascii_uppercase().as_str() {
            "EHLO" | "HELO" if valid_helo(args) => {
                self.client_helo = Some(args.to_owned());
                self.smtp_ehlo = command.eq_ignore_ascii_case("EHLO");
                if !self.smtp_ehlo {
                    wire::send(stream, "250 cauce\r\n").await?;
                } else if secure {
                    let methods = self
                        .mechanisms
                        .iter()
                        .map(|method| match method {
                            Mechanism::Plain => "PLAIN",
                            Mechanism::Login => "LOGIN",
                            Mechanism::UserPass => unreachable!("validated SMTP mechanism"),
                        })
                        .collect::<Vec<_>>()
                        .join(" ");
                    wire::send(stream, &format!("250-cauce\r\n250 AUTH {methods}\r\n")).await?;
                } else {
                    wire::send(stream, "250-cauce\r\n250 STARTTLS\r\n").await?;
                }
            }
            "STARTTLS" if !secure && args.is_empty() => {
                return Ok(Some(Event::Upgrade("220 Ready to start TLS\r\n".into())));
            }
            "QUIT" if args.is_empty() => {
                wire::send(stream, "221 Closing connection\r\n").await?;
                return Ok(Some(Event::Quit));
            }
            "NOOP" | "RSET" => wire::send(stream, "250 OK\r\n").await?,
            "AUTH" if secure && self.smtp_ehlo => {
                let (mechanism, data) = wire::split(args);
                let result = if mechanism.eq_ignore_ascii_case("PLAIN")
                    && self.mechanisms.contains(&Mechanism::Plain)
                {
                    let response = if data.is_empty() {
                        wire::send(stream, "334 \r\n").await?;
                        wire::line(stream).await?
                    } else {
                        data.to_owned()
                    };
                    Credentials::decode_plain(&response)
                } else if mechanism.eq_ignore_ascii_case("LOGIN")
                    && self.mechanisms.contains(&Mechanism::Login)
                {
                    let user = if data.is_empty() {
                        wire::send(stream, "334 VXNlcm5hbWU6\r\n").await?;
                        wire::line(stream).await?
                    } else {
                        data.to_owned()
                    };
                    if user == "*" {
                        wire::send(stream, "501 Authentication cancelled\r\n").await?;
                        return Ok(None);
                    }
                    wire::send(stream, "334 UGFzc3dvcmQ6\r\n").await?;
                    let password = wire::line(stream).await?;
                    wire::decode_login(&user)
                        .and_then(|user| Credentials::new(user, wire::decode_login(&password)?))
                } else {
                    wire::send(stream, "504 Unsupported authentication mechanism\r\n").await?;
                    return Ok(None);
                };
                if let Ok(credentials) = result {
                    return Ok(Some(Event::Authenticate(Attempt {
                        credentials,
                        tag: String::new(),
                    })));
                }
                wire::send(stream, "501 Invalid authentication exchange\r\n").await?;
            }
            "AUTH" | "MAIL" | "RCPT" | "DATA" | "BDAT" if !secure => {
                wire::send(stream, "530 Must issue STARTTLS first\r\n").await?;
            }
            "MAIL" | "RCPT" | "DATA" | "BDAT" => {
                wire::send(stream, "530 Authentication required\r\n").await?;
            }
            "AUTH" => wire::send(stream, "503 EHLO required\r\n").await?,
            _ => wire::send(stream, "500 Command unavailable\r\n").await?,
        }
        Ok(None)
    }

    async fn imap(&self, stream: &mut Wire, line: &str, secure: bool) -> Result<Option<Event>> {
        let (tag, command) = wire::split(line);
        if !wire::imap_tag(tag) {
            wire::send(stream, "* BAD Invalid tag\r\n").await?;
            return Ok(None);
        }
        let (command, args) = wire::split(command);
        match command.to_ascii_uppercase().as_str() {
            "CAPABILITY" if args.is_empty() => {
                let mut response = "* CAPABILITY IMAP4rev1".to_owned();
                if secure && self.mechanisms.contains(&Mechanism::Plain) {
                    response.push_str(" AUTH=PLAIN SASL-IR");
                }
                if !secure {
                    response.push_str(" STARTTLS");
                }
                if !secure || !self.mechanisms.contains(&Mechanism::Login) {
                    response.push_str(" LOGINDISABLED");
                }
                wire::send(
                    stream,
                    &format!("{response}\r\n{tag} OK CAPABILITY completed\r\n"),
                )
                .await?;
            }
            "STARTTLS" if !secure && args.is_empty() => {
                return Ok(Some(Event::Upgrade(format!(
                    "{tag} OK Begin TLS negotiation\r\n"
                ))));
            }
            "LOGOUT" if args.is_empty() => {
                wire::send(
                    stream,
                    &format!("* BYE Closing connection\r\n{tag} OK LOGOUT completed\r\n"),
                )
                .await?;
                return Ok(Some(Event::Quit));
            }
            "NOOP" if args.is_empty() => {
                wire::send(stream, &format!("{tag} OK NOOP completed\r\n")).await?
            }
            "LOGIN" | "AUTHENTICATE" if !secure => {
                wire::send(
                    stream,
                    &format!("{tag} NO [PRIVACYREQUIRED] STARTTLS required\r\n"),
                )
                .await?;
            }
            "LOGIN" if self.mechanisms.contains(&Mechanism::Login) => {
                if let Ok(credentials) = wire::imap_login(args) {
                    return Ok(Some(Event::Authenticate(Attempt {
                        credentials,
                        tag: tag.to_owned(),
                    })));
                }
                wire::send(stream, &format!("{tag} BAD Invalid LOGIN arguments\r\n")).await?;
            }
            "AUTHENTICATE" if self.mechanisms.contains(&Mechanism::Plain) => {
                let (mechanism, initial) = wire::split(args);
                if !mechanism.eq_ignore_ascii_case("PLAIN") {
                    wire::send(
                        stream,
                        &format!("{tag} NO Unsupported authentication mechanism\r\n"),
                    )
                    .await?;
                    return Ok(None);
                }
                let response = if initial.is_empty() {
                    wire::send(stream, "+ \r\n").await?;
                    wire::line(stream).await?
                } else {
                    initial.to_owned()
                };
                if let Ok(credentials) = Credentials::decode_plain(&response) {
                    return Ok(Some(Event::Authenticate(Attempt {
                        credentials,
                        tag: tag.to_owned(),
                    })));
                }
                wire::send(
                    stream,
                    &format!("{tag} BAD Invalid authentication exchange\r\n"),
                )
                .await?;
            }
            _ => wire::send(stream, &format!("{tag} BAD Command unavailable\r\n")).await?,
        }
        Ok(None)
    }

    async fn pop3(&mut self, stream: &mut Wire, line: &str, secure: bool) -> Result<Option<Event>> {
        let (command, args) = wire::split(line);
        match command.to_ascii_uppercase().as_str() {
            "CAPA" if args.is_empty() => {
                wire::send(
                    stream,
                    if secure {
                        "+OK Capability list follows\r\nUSER\r\n.\r\n"
                    } else {
                        "+OK Capability list follows\r\nSTLS\r\n.\r\n"
                    },
                )
                .await?
            }
            "STLS" if !secure && args.is_empty() => {
                return Ok(Some(Event::Upgrade("+OK Begin TLS negotiation\r\n".into())));
            }
            "QUIT" if args.is_empty() => {
                wire::send(stream, "+OK Closing connection\r\n").await?;
                return Ok(Some(Event::Quit));
            }
            "USER" | "PASS" if !secure => {
                wire::send(stream, "-ERR [AUTH] STLS required\r\n").await?
            }
            "USER" => {
                self.pop_user = None;
                if wire::valid_credential(args) && !args.contains(' ') {
                    self.pop_user = Some(args.to_owned());
                    wire::send(stream, "+OK User accepted\r\n").await?;
                } else {
                    wire::send(stream, "-ERR Invalid user\r\n").await?;
                }
            }
            "PASS" => {
                if let Some(user) = self.pop_user.take() {
                    if let Ok(credentials) = Credentials::new(user, args.to_owned()) {
                        return Ok(Some(Event::Authenticate(Attempt {
                            credentials,
                            tag: String::new(),
                        })));
                    }
                }
                wire::send(stream, "-ERR Invalid authentication exchange\r\n").await?;
            }
            _ => wire::send(stream, "-ERR Command unavailable\r\n").await?,
        }
        Ok(None)
    }
}

fn valid_helo(host: &str) -> bool {
    (1..=255).contains(&host.len())
        && host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"[]:.%-_".contains(&b))
}

pub(super) async fn authentication_reply(
    protocol: Protocol,
    stream: &mut Wire,
    attempt: &Attempt,
    result: Option<bool>,
) -> Result<()> {
    let line = match (protocol, result) {
        (Protocol::Smtp, Some(true)) => "235 2.7.0 Authentication successful\r\n".into(),
        (Protocol::Smtp, Some(false)) => "535 5.7.8 Authentication rejected\r\n".into(),
        (Protocol::Smtp, None) => "454 4.7.0 Temporary authentication failure\r\n".into(),
        (Protocol::Imap, Some(true)) => format!("{} OK Authentication successful\r\n", attempt.tag),
        (Protocol::Imap, Some(false)) => format!(
            "{} NO [AUTHENTICATIONFAILED] Authentication rejected\r\n",
            attempt.tag
        ),
        (Protocol::Imap, None) => format!(
            "{} NO [UNAVAILABLE] Temporary authentication failure\r\n",
            attempt.tag
        ),
        (Protocol::Pop3, Some(true)) => "+OK Authenticated\r\n".into(),
        (Protocol::Pop3, Some(false)) => "-ERR [AUTH] Authentication rejected\r\n".into(),
        (Protocol::Pop3, None) => "-ERR [SYS/TEMP] Temporary authentication failure\r\n".into(),
    };
    wire::send(stream, &line).await
}

pub(super) async fn backend_login(
    protocol: Protocol,
    origin: &mut Wire,
    credentials: &Credentials,
) -> Result<()> {
    match protocol {
        Protocol::Smtp => {
            smtp_reply(origin, "220").await?;
            wire::send(origin, "EHLO cauce.invalid\r\n").await?;
            smtp_reply(origin, "250").await?;
            wire::send(
                origin,
                &format!("AUTH PLAIN {}\r\n", credentials.encode_plain()),
            )
            .await?;
            smtp_reply(origin, "235").await
        }
        Protocol::Imap => {
            ensure!(
                prefixed(&wire::line(origin).await?, "* OK"),
                "unexpected IMAP greeting"
            );
            let command = format!(
                "cauce LOGIN {} {}\r\n",
                wire::quote_imap(&credentials.user),
                wire::quote_imap(&credentials.password)
            );
            wire::send(origin, &command).await?;
            for _ in 0..32 {
                let reply = wire::line(origin).await?;
                if !reply.starts_with("* ") {
                    ensure!(prefixed(&reply, "cauce OK"), "IMAP authentication failed");
                    return Ok(());
                }
                ensure!(!prefixed(&reply, "* BYE"), "IMAP backend closed");
            }
            anyhow::bail!("IMAP backend response limit")
        }
        Protocol::Pop3 => {
            ensure!(
                !credentials.user.contains(' '),
                "invalid POP3 backend username"
            );
            pop_ok(origin).await?;
            wire::send(origin, &format!("USER {}\r\n", credentials.user)).await?;
            pop_ok(origin).await?;
            wire::send(origin, &format!("PASS {}\r\n", credentials.password)).await?;
            pop_ok(origin).await
        }
    }
}

async fn smtp_reply(origin: &mut Wire, code: &str) -> Result<()> {
    for _ in 0..32 {
        let reply = wire::line(origin).await?;
        let suffix = reply
            .strip_prefix(code)
            .ok_or_else(|| anyhow::anyhow!("unexpected SMTP backend status"))?;
        if suffix.is_empty() || suffix.starts_with(' ') {
            return Ok(());
        }
        ensure!(suffix.starts_with('-'), "invalid SMTP backend response");
    }
    anyhow::bail!("SMTP backend response limit")
}

fn prefixed(line: &str, expected: &str) -> bool {
    line.get(..expected.len())
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case(expected))
        && line
            .as_bytes()
            .get(expected.len())
            .is_none_or(|b| *b == b' ')
}

async fn pop_ok(origin: &mut Wire) -> Result<()> {
    ensure!(
        prefixed(&wire::line(origin).await?, "+OK"),
        "POP3 backend rejected command"
    );
    Ok(())
}
