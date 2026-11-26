//! Bounded, compiled edits for proxy-managed request and response header maps.
use hyper::header::{HeaderMap, HeaderName, HeaderValue};
use hyper::Response;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Policy {
    pub request: Edits,
    pub response: Edits,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_add: Option<Additions>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(try_from = "Vec<RawAddition>", into = "Vec<RawAddition>")]
pub(crate) struct Additions(Box<[Addition]>);

#[derive(Clone, Debug, PartialEq)]
struct Addition {
    name: HeaderName,
    value: HeaderValue,
    always: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RawAddition {
    name: String,
    value: String,
    #[serde(default)]
    always: bool,
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

fn editable_value(value: &str) -> Result<HeaderValue, &'static str> {
    if !value.bytes().all(|byte| (0x20..=0x7e).contains(&byte)) {
        return Err("header policy values must use printable ASCII");
    }
    let mut value = HeaderValue::from_str(value).map_err(|_| "invalid header policy value")?;
    value.set_sensitive(true);
    Ok(value)
}

impl TryFrom<Vec<RawAddition>> for Additions {
    type Error = &'static str;

    fn try_from(entries: Vec<RawAddition>) -> Result<Self, Self::Error> {
        if entries.len() > 16 {
            return Err("response header policy permits at most 16 additions");
        }
        let mut additions = Vec::with_capacity(entries.len());
        for raw in entries {
            additions.push(Addition {
                name: editable_name(&raw.name)?,
                value: editable_value(&raw.value)?,
                always: raw.always,
            });
        }
        Ok(Self(additions.into_boxed_slice()))
    }
}

impl From<Additions> for Vec<RawAddition> {
    fn from(additions: Additions) -> Self {
        additions
            .0
            .into_vec()
            .into_iter()
            .map(|entry| RawAddition {
                name: entry.name.to_string(),
                value: entry.value.to_str().expect("validated ASCII").to_owned(),
                always: entry.always,
            })
            .collect()
    }
}

impl Policy {
    pub fn validate(&self) -> anyhow::Result<()> {
        if let Some(additions) = &self.response_add {
            let count = self.response.set.len() + self.response.remove.len() + additions.0.len();
            let bytes = self
                .response
                .set
                .iter()
                .map(|(name, value)| name.as_str().len() + value.len() + 4)
                .chain(
                    additions
                        .0
                        .iter()
                        .map(|entry| entry.name.as_str().len() + entry.value.len() + 4),
                )
                .sum::<usize>();
            anyhow::ensure!(
                count <= 16 && bytes <= 4096,
                "response header policy exceeds its edit or byte budget"
            );
        }
        Ok(())
    }

    pub fn response_touches(&self, names: &[&str]) -> bool {
        self.response.touches(names)
            || self.response_add.as_ref().is_some_and(|additions| {
                additions
                    .0
                    .iter()
                    .any(|entry| names.contains(&entry.name.as_str()))
            })
    }

    pub fn apply_response<B>(&self, response: &mut Response<B>) -> bool {
        let mut added = self.response.apply(response.headers_mut());
        if let Some(additions) = &self.response_add {
            let selected = matches!(
                response.status().as_u16(),
                200 | 201 | 204 | 206 | 301 | 302 | 303 | 304 | 307 | 308
            );
            for entry in &additions.0 {
                if (selected || entry.always) && !entry.value.is_empty() {
                    response
                        .headers_mut()
                        .append(entry.name.clone(), entry.value.clone());
                    added = true;
                }
            }
        }
        added
    }
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
            bytes += name.as_str().len() + value.len() + 4;
            if bytes > 4096 {
                return Err("header policy values exceed 4 KiB per direction");
            }
            let value = editable_value(&value)?;
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
    pub fn touches(&self, names: &[&str]) -> bool {
        self.set
            .iter()
            .map(|(name, _)| name)
            .chain(&self.remove)
            .any(|name| names.contains(&name.as_str()))
    }

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
    fn additions_use_the_same_protocol_ownership_and_value_guards() {
        for (name, value) in [
            ("Content-Length", "1"),
            ("Grpc-Status", "0"),
            ("X-Test", "bad\r\nline"),
        ] {
            assert!(Additions::try_from(vec![RawAddition {
                name: name.into(),
                value: value.into(),
                always: true
            }])
            .is_err());
        }
    }

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
