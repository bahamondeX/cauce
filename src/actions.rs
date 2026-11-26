//! Compile route actions once and bound their per-request output.
use hyper::{header::HeaderValue, http::uri::PathAndQuery, StatusCode};
use regex::bytes::{Captures, Regex, RegexBuilder};
use serde::{Deserialize, Serialize};

const MAX_DESTINATION: usize = 8192;

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Query {
    #[default]
    Preserve,
    Replace,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Spec {
    Rewrite {
        target: String,
        pattern: Option<String>,
        #[serde(default)]
        query: Query,
    },
    Redirect {
        status: u16,
        target: String,
        pattern: Option<String>,
        #[serde(default)]
        query: Query,
    },
    Named {
        location: String,
    },
}

#[derive(Clone, Debug)]
enum Part {
    Text(String),
    Capture(usize),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(try_from = "Spec", into = "Spec")]
pub(crate) struct Action {
    spec: Spec,
    pattern: Option<Regex>,
    parts: Vec<Part>,
}

impl PartialEq for Action {
    fn eq(&self, other: &Self) -> bool {
        self.spec == other.spec
    }
}

impl From<Action> for Spec {
    fn from(action: Action) -> Self {
        action.spec
    }
}

fn parts(text: &str, captures: usize) -> Result<Vec<Part>, &'static str> {
    let mut parts = Vec::new();
    let mut rest = text;
    while let Some((initial, next)) = rest.split_once('$') {
        if !initial.is_empty() {
            parts.push(Part::Text(initial.into()));
        }
        match next.as_bytes().first() {
            Some(b'$') => parts.push(Part::Text("$".into())),
            Some(digit @ b'1'..=b'9') if usize::from(*digit - b'0') < captures => {
                if next.as_bytes().get(1).is_some_and(u8::is_ascii_digit) {
                    return Err("route action captures require one digit");
                }
                parts.push(Part::Capture(usize::from(*digit - b'0')));
            }
            _ => return Err("route action requires a known capture or $$"),
        }
        rest = &next[1..];
    }
    if !rest.is_empty() {
        parts.push(Part::Text(rest.into()));
    }
    Ok(parts)
}

impl TryFrom<Spec> for Action {
    type Error = &'static str;

    fn try_from(spec: Spec) -> Result<Self, Self::Error> {
        let (target, pattern) = match &spec {
            Spec::Named { location } => {
                if !crate::config::identifier(location) {
                    return Err("invalid named location");
                }
                return Ok(Self {
                    spec,
                    pattern: None,
                    parts: Vec::new(),
                });
            }
            Spec::Rewrite {
                target, pattern, ..
            } => (target, pattern),
            Spec::Redirect {
                target,
                pattern,
                status,
                ..
            } => {
                if !matches!(status, 301 | 302 | 303 | 307 | 308) {
                    return Err("redirect status must be 301, 302, 303, 307 or 308");
                }
                (target, pattern)
            }
        };
        if !target.starts_with('/') || target.starts_with("//") || target.len() > 512 {
            return Err("action destination must be an origin-relative path of at most 512 bytes");
        }
        let pattern = pattern
            .as_deref()
            .map(|text| {
                if text.len() > 256 || text.is_empty() {
                    return Err("action pattern must contain 1 to 256 bytes");
                }
                RegexBuilder::new(text)
                    .unicode(false)
                    .nest_limit(32)
                    .size_limit(128 * 1024)
                    .dfa_size_limit(64 * 1024)
                    .build()
                    .map_err(|_| "invalid route action pattern")
            })
            .transpose()?;
        let parts = parts(target, pattern.as_ref().map_or(0, Regex::captures_len))?;
        // Check literal components with a harmless representative capture.
        render(&parts, None, None, Query::Replace, true)
            .map_err(|_| "invalid route action destination")?;
        Ok(Self {
            spec,
            pattern,
            parts,
        })
    }
}

fn render(
    parts: &[Part],
    captures: Option<&Captures<'_>>,
    query: Option<&str>,
    mode: Query,
    validating: bool,
) -> Result<PathAndQuery, ()> {
    let mut target = String::new();
    for part in parts {
        let value = match part {
            Part::Text(text) => text,
            Part::Capture(_) if validating => "x",
            Part::Capture(index) => std::str::from_utf8(
                captures
                    .and_then(|c| c.get(*index))
                    .map_or(b"", |c| c.as_bytes()),
            )
            .map_err(|_| ())?,
        };
        if target.len() + value.len() > MAX_DESTINATION {
            return Err(());
        }
        target.push_str(value);
    }
    if let Some(query) = query.filter(|query| mode == Query::Preserve && !query.is_empty()) {
        if target.len() + query.len() + 1 > MAX_DESTINATION {
            return Err(());
        }
        target.push(if target.contains('?') { '&' } else { '?' });
        target.push_str(query);
    }
    let parsed: PathAndQuery = target.parse().map_err(|_| ())?;
    if !parsed.path().starts_with('/') || !crate::proxy::normal_path(parsed.path()) {
        return Err(());
    }
    // Do not allow parser normalization to hide a fragment or other discarded text.
    if parsed.as_str() != target {
        return Err(());
    }
    Ok(parsed)
}

pub(crate) enum Outcome<'a> {
    Rewrite(PathAndQuery),
    Redirect(StatusCode, HeaderValue),
    Named(&'a str),
}

impl Action {
    pub fn unconditional(&self) -> bool {
        self.pattern.is_none()
    }

    pub fn named(&self) -> Option<&str> {
        match &self.spec {
            Spec::Named { location } => Some(location),
            _ => None,
        }
    }

    pub fn apply(&self, path: &str, args: Option<&str>) -> Result<Option<Outcome<'_>>, ()> {
        let mode = match &self.spec {
            Spec::Named { location } => return Ok(Some(Outcome::Named(location))),
            Spec::Rewrite { query, .. } | Spec::Redirect { query, .. } => *query,
        };
        let captures = match &self.pattern {
            Some(pattern) => match pattern.captures(path.as_bytes()) {
                Some(captures) => Some(captures),
                None => return Ok(None),
            },
            None => None,
        };
        let target = render(&self.parts, captures.as_ref(), args, mode, false)?;
        let action = match &self.spec {
            Spec::Redirect { status, .. } => Outcome::Redirect(
                StatusCode::from_u16(*status).expect("validated redirect code"),
                HeaderValue::from_str(target.as_str()).map_err(|_| ())?,
            ),
            _ => Outcome::Rewrite(target),
        };
        Ok(Some(action))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_modes_and_capture_safety() {
        let mut spec = Spec::Rewrite {
            target: "/v1/$1?new=1".into(),
            pattern: Some("^/old/(.*)$".into()),
            query: Query::Preserve,
        };
        let action = Action::try_from(spec.clone()).unwrap();
        let Some(Outcome::Rewrite(uri)) = action.apply("/old/test", Some("a=2")).unwrap() else {
            panic!("no rewrite")
        };
        assert_eq!(uri.as_str(), "/v1/test?new=1&a=2");
        assert!(action.apply("/old/%2F", None).is_err());
        assert!(action.apply("/old/..", None).is_err());
        let Spec::Rewrite { query, .. } = &mut spec else {
            unreachable!()
        };
        *query = Query::Replace;
        let action = Action::try_from(spec).unwrap();
        let Some(Outcome::Rewrite(uri)) = action.apply("/old/test", Some("a=2")).unwrap() else {
            panic!("no rewrite")
        };
        assert_eq!(uri.as_str(), "/v1/test?new=1");
    }
}
