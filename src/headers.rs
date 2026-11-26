//! Bounded, compiled edits for proxy-managed request and response header maps.
use hyper::header::{HeaderMap, HeaderName, HeaderValue};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Policy {
    pub request: Edits,
    pub response: Edits,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(try_from = "RawEdits", into = "RawEdits")]
pub(crate) struct Edits {
    set: Vec<(HeaderName, HeaderValue)>,
    remove: Vec<HeaderName>,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct RawEdits {
    set: BTreeMap<String, String>,
    remove: Vec<String>,
}

fn editable_name(value: &str) -> Result<HeaderName, &'static str> {
    if value.len() > 64 {
        return Err("header policy name must contain at most 64 bytes");
    }
    let name =
        HeaderName::from_bytes(value.as_bytes()).map_err(|_| "invalid header policy name")?;
    let key = name.as_str();
    if matches!(
        key,
        "host"
            | "connection"
            | "keep-alive"
            | "proxy-connection"
            | "transfer-encoding"
            | "content-length"
            | "content-type"
            | "content-encoding"
            | "trailer"
            | "te"
            | "upgrade"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "forwarded"
            | "x-real-ip"
            | "x-request-id"
            | "traceparent"
            | "tracestate"
            | "baggage"
    ) || ["x-forwarded-", "grpc-", "sec-websocket-"]
        .iter()
        .any(|prefix| key.starts_with(prefix))
    {
        return Err("header policy cannot edit a proxy-owned or protocol header");
    }
    Ok(name)
}

impl TryFrom<RawEdits> for Edits {
    type Error = &'static str;

    fn try_from(raw: RawEdits) -> Result<Self, Self::Error> {
        if raw.set.len() + raw.remove.len() > 16 {
            return Err("header policy permits at most 16 edits per direction");
        }
        let mut edits = Self::default();
        let mut seen = HashSet::new();
        let mut bytes = 0;
        for (key, value) in raw.set {
            let name = editable_name(&key)?;
            if !seen.insert(name.clone()) {
                return Err("duplicate header policy name");
            }
            if !value.bytes().all(|byte| (0x20..=0x7e).contains(&byte)) {
                return Err("header policy values must use printable ASCII");
            }
            bytes += name.as_str().len() + value.len() + 4;
            if bytes > 4096 {
                return Err("header policy values exceed 4 KiB per direction");
            }
            let mut value =
                HeaderValue::from_str(&value).map_err(|_| "invalid header policy value")?;
            value.set_sensitive(true);
            edits.set.push((name, value));
        }
        for key in raw.remove {
            let name = editable_name(&key)?;
            if !seen.insert(name.clone()) {
                return Err("duplicate header policy name");
            }
            edits.remove.push(name);
        }
        Ok(edits)
    }
}

impl From<Edits> for RawEdits {
    fn from(value: Edits) -> Self {
        Self {
            set: value
                .set
                .into_iter()
                .map(|(name, value)| {
                    (
                        name.to_string(),
                        value.to_str().expect("validated ASCII").to_owned(),
                    )
                })
                .collect(),
            remove: value
                .remove
                .into_iter()
                .map(|name| name.to_string())
                .collect(),
        }
    }
}

impl Edits {
    pub fn apply(&self, headers: &mut HeaderMap) -> bool {
        for name in &self.remove {
            headers.remove(name);
        }
        for (name, value) in &self.set {
            headers.insert(name.clone(), value.clone());
        }
        !self.set.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn policies_protect_protocol_identity_and_budgets() {
        for key in [
            "Content-Length",
            "X-Forwarded-For",
            "TrAcEpArEnT",
            "Sec-WebSocket-Accept",
            "grpc-status",
            ":authority",
        ] {
            assert!(editable_name(key).is_err(), "{key}");
        }
        for set in [
            BTreeMap::from([("x-valid".into(), "invalid\r\nheader".into())]),
            BTreeMap::from([("x-valid".into(), "a".repeat(4097))]),
            (0..17).map(|n| (format!("x-{n}"), "1".into())).collect(),
        ] {
            assert!(Edits::try_from(RawEdits {
                set,
                ..RawEdits::default()
            })
            .is_err());
        }
        assert!(Edits::try_from(RawEdits {
            set: BTreeMap::from([("X-Tenant".into(), "a".into())]),
            remove: vec!["x-tenant".into()],
        })
        .is_err());
    }
}
