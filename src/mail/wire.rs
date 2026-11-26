use anyhow::{ensure, Context, Result};
use std::str;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

pub(super) type Wire = BufReader<Box<dyn crate::ClientIo>>;

const MAX_LINE: usize = 2048;
const MAX_CREDENTIAL: usize = 512;

pub(super) async fn line(wire: &mut Wire) -> Result<String> {
    let mut line = Vec::new();
    loop {
        let buffer = wire.fill_buf().await?;
        ensure!(!buffer.is_empty(), "mail peer closed");
        let newline = buffer.iter().position(|byte| *byte == b'\n');
        let count = newline.map_or(buffer.len(), |index| index + 1);
        ensure!(line.len() + count <= MAX_LINE, "mail line limit");
        line.extend_from_slice(&buffer[..count]);
        wire.consume(count);
        if newline.is_some() {
            let text = line.strip_suffix(b"\r\n").context("mail CRLF required")?;
            ensure!(
                text.iter().all(|byte| (0x20..=0x7e).contains(byte)),
                "invalid mail line"
            );
            line.truncate(line.len() - 2);
            return String::from_utf8(line).context("mail line encoding");
        }
    }
}

pub(super) async fn send(wire: &mut Wire, text: &str) -> Result<()> {
    wire.write_all(text.as_bytes()).await?;
    wire.flush().await?;
    Ok(())
}

pub(super) fn split(text: &str) -> (&str, &str) {
    text.split_once(' ').unwrap_or((text, ""))
}

pub(super) struct Credentials {
    pub user: String,
    pub password: String,
}

pub(super) fn valid_credential(value: &str) -> bool {
    (1..=MAX_CREDENTIAL).contains(&value.len()) && value.bytes().all(|b| (0x20..=0x7e).contains(&b))
}

impl Credentials {
    pub(super) fn new(user: String, password: String) -> Result<Self> {
        ensure!(
            valid_credential(&user) && valid_credential(&password),
            "invalid mail credentials"
        );
        Ok(Self { user, password })
    }

    pub(super) fn decode_plain(value: &str) -> Result<Self> {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let decoded = STANDARD
            .decode(value)
            .map_err(|_| anyhow::anyhow!("invalid SASL encoding"))?;
        let mut values = str::from_utf8(&decoded)?.split('\0');
        let authorization = values.next().unwrap_or_default();
        let user = values.next().context("missing SASL user")?;
        let password = values.next().context("missing SASL password")?;
        ensure!(
            values.next().is_none() && (authorization.is_empty() || authorization == user),
            "invalid SASL authorization"
        );
        Self::new(user.to_owned(), password.to_owned())
    }

    pub(super) fn encode_plain(&self) -> String {
        use base64::{engine::general_purpose::STANDARD, Engine};
        STANDARD.encode(format!("\0{}\0{}", self.user, self.password))
    }
}

pub(super) fn decode_login(value: &str) -> Result<String> {
    use base64::{engine::general_purpose::STANDARD, Engine};
    String::from_utf8(
        STANDARD
            .decode(value)
            .map_err(|_| anyhow::anyhow!("invalid SASL encoding"))?,
    )
    .map_err(|_| anyhow::anyhow!("invalid SASL text"))
}

pub(super) fn imap_login(mut args: &str) -> Result<Credentials> {
    fn argument(text: &str) -> Result<(String, &str)> {
        let text = text.trim_start_matches(' ');
        if let Some(quoted) = text.strip_prefix('"') {
            let mut value = String::new();
            let mut escaped = false;
            for (index, ch) in quoted.char_indices() {
                if escaped {
                    ensure!(matches!(ch, '"' | '\\'), "invalid IMAP quoted escape");
                    escaped = false;
                } else if ch == '\\' {
                    escaped = true;
                    continue;
                } else if ch == '"' {
                    return Ok((value, &quoted[index + 1..]));
                }
                value.push(ch);
            }
            anyhow::bail!("unfinished IMAP quoted string");
        }
        let (atom, _) = split(text);
        ensure!(
            !atom.bytes().any(|b| b"(){%*\"]\\".contains(&b)),
            "unsupported IMAP login argument"
        );
        Ok((atom.to_owned(), &text[atom.len()..]))
    }

    let (user, rest) = argument(args)?;
    args = rest
        .strip_prefix(' ')
        .context("missing IMAP argument separator")?;
    let (password, rest) = argument(args)?;
    ensure!(rest.is_empty(), "unexpected IMAP login arguments");
    Credentials::new(user, password)
}

pub(super) fn quote_imap(value: &str) -> String {
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push('"');
    for ch in value.chars() {
        if matches!(ch, '"' | '\\') {
            quoted.push('\\');
        }
        quoted.push(ch);
    }
    quoted.push('"');
    quoted
}

pub(super) fn imap_tag(tag: &str) -> bool {
    (1..=32).contains(&tag.len())
        && tag
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_credentials_and_imap_quoting() {
        let creds = imap_login(r#"fixture "a \" b \\ c""#).unwrap();
        assert_eq!(creds.password, "a \" b \\ c");
        assert_eq!(quote_imap(&creds.password), r#""a \" b \\ c""#);
        assert!(imap_login(r#"fixture {15}"#).is_err());
        assert!(imap_login(r#""fixture"pass"#).is_err());
        assert!(Credentials::decode_plain("dQBwAHcA").is_err());
        assert!(Credentials::new("fixture".into(), "xx\r\nQUIT".into()).is_err());
    }
}
