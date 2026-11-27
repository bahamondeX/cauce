//! Bounded request expressions compiled before a configuration is published.
use hyper::{header::HOST, Request};
use std::{fmt::Write, net::IpAddr, ops::Range};

const MAX_OPERATIONS: usize = 64;
pub(crate) const MAX_OUTPUT: usize = 16_384;

#[derive(Clone, Copy, Debug, PartialEq)]
enum Variable {
    Host,
    Uri,
    Method,
    Args,
    Scheme,
    RemoteAddr,
    RequestId,
}

#[derive(Clone, Debug, PartialEq)]
enum Op {
    Literal(Range<usize>),
    Variable(Variable),
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Expression(Box<[Op]>);

pub(crate) struct RequestValues<'a, B> {
    pub request: &'a Request<B>,
    pub secure: bool,
    pub client: IpAddr,
    pub request_id: &'a str,
}

impl Expression {
    pub fn compile(source: &str) -> Result<Self, &'static str> {
        if source.len() > 4096 {
            return Err("body expression exceeds 4096 bytes");
        }
        let mut operations = Vec::new();
        let mut position = 0;
        while position < source.len() {
            let remainder = &source[position..];
            if let Some(tail) = remainder.strip_prefix('$') {
                position += 1;
                if tail.starts_with('$') {
                    operations.push(Op::Literal(position..position + 1));
                    position += 1;
                } else {
                    let length = tail
                        .bytes()
                        .take_while(|c| c.is_ascii_alphanumeric() || *c == b'_')
                        .count();
                    let variable = match &tail[..length] {
                        "host" => Variable::Host,
                        "uri" => Variable::Uri,
                        "request_method" => Variable::Method,
                        "args" => Variable::Args,
                        "scheme" => Variable::Scheme,
                        "remote_addr" => Variable::RemoteAddr,
                        "request_id" => Variable::RequestId,
                        "upstream_addr" | "status" => {
                            return Err(
                                "body expression cannot read upstream or response variables",
                            )
                        }
                        _ => return Err("body expression contains an undefined request variable"),
                    };
                    operations.push(Op::Variable(variable));
                    position += length;
                }
            } else {
                let end = position + remainder.find('$').unwrap_or(remainder.len());
                operations.push(Op::Literal(position..end));
                position = end;
            }
            if operations.len() > MAX_OPERATIONS {
                return Err("body expression exceeds 64 operations");
            }
        }
        Ok(Self(operations.into_boxed_slice()))
    }

    pub fn evaluate<B>(&self, source: &str, values: RequestValues<'_, B>) -> Result<String, ()> {
        let request = values.request;
        let mut output = String::new();
        let mut client = None;
        let mut host_lower = None;
        for operation in &self.0 {
            let fragment: &str = match operation {
                Op::Literal(span) => &source[span.clone()],
                Op::Variable(Variable::Host) => {
                    let host = request
                        .uri()
                        .host()
                        .or_else(|| {
                            let value = request.headers().get(HOST)?.to_str().ok()?;
                            let end = match value.strip_prefix('[') {
                                Some(_) => value.find(']').map_or(value.len(), |index| index + 1),
                                None => value.find(':').unwrap_or(value.len()),
                            };
                            Some(&value[..end])
                        })
                        .unwrap_or_default();
                    if host.bytes().any(|byte| byte.is_ascii_uppercase()) {
                        host_lower.get_or_insert_with(|| host.to_ascii_lowercase())
                    } else {
                        host
                    }
                }
                Op::Variable(Variable::Uri) => request.uri().path(),
                Op::Variable(Variable::Method) => request.method().as_str(),
                Op::Variable(Variable::Args) => request.uri().query().unwrap_or_default(),
                Op::Variable(Variable::Scheme) => {
                    if values.secure {
                        "https"
                    } else {
                        "http"
                    }
                }
                Op::Variable(Variable::RequestId) => values.request_id,
                Op::Variable(Variable::RemoteAddr) => client.get_or_insert_with(|| {
                    let mut address = String::with_capacity(39);
                    write!(address, "{}", values.client).expect("writing a string");
                    address
                }),
            };
            if output.len() + fragment.len() > MAX_OUTPUT {
                return Err(());
            }
            output.push_str(fragment);
        }
        Ok(output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compiles_variables_and_uses_current_uri() {
        let source = "$scheme://$host$uri?$args $$";
        let expression = Expression::compile(source).unwrap();
        let mut request = Request::builder()
            .uri("/before")
            .header(HOST, "EXAMPLE.TEST:443")
            .body(())
            .unwrap();
        *request.uri_mut() = "/after?keep=1".parse().unwrap();
        let values = RequestValues {
            request: &request,
            secure: true,
            client: "127.0.0.1".parse().unwrap(),
            request_id: "",
        };
        assert_eq!(
            expression.evaluate(source, values).unwrap(),
            "https://example.test/after?keep=1 $"
        );
    }

    #[test]
    fn rejects_undefined_wrong_phase_and_oversized_programs() {
        assert!(Expression::compile("$missing").is_err());
        assert!(Expression::compile("$upstream_addr").is_err());
        assert!(Expression::compile(&"$uri".repeat(65)).is_err());
        let source = "$args".repeat(32);
        let request = Request::builder()
            .uri(format!("/?{}", "x".repeat(1024)))
            .body(())
            .unwrap();
        let values = RequestValues {
            request: &request,
            secure: false,
            client: "127.0.0.1".parse().unwrap(),
            request_id: "",
        };
        assert_eq!(
            Expression::compile(&source)
                .unwrap()
                .evaluate(&source, values),
            Err(())
        );
    }
}
