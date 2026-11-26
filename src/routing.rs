//! Immutable host and path indexes compiled once per validated configuration.
use crate::config::{PathMatch, Route};
use std::collections::{btree_map::Entry, BTreeMap, HashMap};

pub struct Routes {
    single: bool,
    hosts: HashMap<String, Host>,
    #[expect(
        clippy::box_collection,
        reason = "keep the optional index one pointer wide"
    )]
    wildcards: Option<Box<HashMap<String, WildcardHosts>>>,
}

#[derive(Default)]
struct WildcardHosts {
    suffixes: HashMap<String, Host>,
    prefixes: HashMap<String, Host>,
}

#[derive(Default)]
struct Host {
    prefixes: Node,
    #[expect(
        clippy::box_collection,
        reason = "avoid an inline map in every prefix-only host"
    )]
    exact: Option<Box<HashMap<String, usize>>>,
}

pub(crate) struct Wildcard<'a> {
    domain: &'a str,
    port: &'a str,
    suffix: bool,
}

fn authority_parts(authority: &str) -> (&str, &str) {
    authority
        .rfind(':')
        .map_or((authority, ""), |index| authority.split_at(index))
}

fn dns_name(name: &str) -> bool {
    name.len() <= 253
        && name.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
}

pub(crate) fn wildcard(authority: &str) -> Option<Wildcard<'_>> {
    let (name, port) = authority_parts(authority);
    let (domain, suffix) = if let Some(suffix) = name.strip_prefix("*.") {
        (suffix, true)
    } else {
        (name.strip_suffix(".*")?, false)
    };
    if !dns_name(domain)
        || (!port.is_empty()
            && (port.len() > 6
                || !port[1..].bytes().all(|b| b.is_ascii_digit())
                || port[1..].parse::<u16>().ok().filter(|&p| p != 0).is_none()))
    {
        return None;
    }
    Some(Wildcard {
        domain,
        port,
        suffix,
    })
}

#[derive(Default)]
struct Node {
    route: Option<usize>,
    children: BTreeMap<u8, Edge>,
}

struct Edge {
    prefix: Box<[u8]>,
    next: Node,
}

fn path_boundary(prefix: &[u8], suffix: &[u8]) -> bool {
    prefix.ends_with(b"/") || suffix.is_empty() || suffix.starts_with(b"/")
}

impl Routes {
    pub fn compile(routes: &[Route]) -> Self {
        let mut hosts = HashMap::<String, Host>::new();
        let mut wildcards = None;
        let single = matches!(routes, [route] if !route.host.contains('*'));
        if !single {
            for (id, route) in routes.iter().enumerate() {
                let index = if let Some(pattern) = wildcard(&route.host) {
                    let by_port = wildcards.get_or_insert_with(|| Box::new(HashMap::new()));
                    let group: &mut WildcardHosts =
                        by_port.entry(pattern.port.to_owned()).or_default();
                    let names = if pattern.suffix {
                        &mut group.suffixes
                    } else {
                        &mut group.prefixes
                    };
                    names.entry(pattern.domain.to_owned()).or_default()
                } else {
                    hosts.entry(route.host.clone()).or_default()
                };
                match route.path_match {
                    PathMatch::Exact => {
                        index
                            .exact
                            .get_or_insert_with(|| Box::new(HashMap::new()))
                            .insert(route.path_prefix.clone(), id);
                    }
                    PathMatch::Prefix => index.prefixes.insert(route.path_prefix.as_bytes(), id),
                }
            }
        }
        Self {
            single,
            hosts,
            wildcards,
        }
    }

    pub fn find<'a>(&self, routes: &'a [Route], host: &str, path: &str) -> Option<&'a Route> {
        if self.single {
            let route = &routes[0];
            let prefix = route.path_prefix.as_bytes();
            return (route.host == host
                && path.as_bytes().strip_prefix(prefix).is_some_and(|rest| {
                    match route.path_match {
                        PathMatch::Prefix => path_boundary(prefix, rest),
                        PathMatch::Exact => rest.is_empty(),
                    }
                }))
            .then_some(route);
        }

        let host = self.hosts.get(host).or_else(|| {
            let wildcards = self.wildcards.as_ref()?;
            let (name, port) = authority_parts(host);
            let group = wildcards.get(port)?;
            if !dns_name(name) {
                return None;
            }
            name.match_indices('.')
                .find_map(|(index, _)| group.suffixes.get(&name[index + 1..]))
                .or_else(|| {
                    name.rmatch_indices('.')
                        .find_map(|(index, _)| group.prefixes.get(&name[..index]))
                })
        })?;
        if let Some(&id) = host.exact.as_ref().and_then(|index| index.get(path)) {
            return Some(&routes[id]);
        }
        let mut node = &host.prefixes;
        let mut remaining = path.as_bytes();
        let mut matched = None;
        while let Some(first) = remaining.first() {
            let Some(edge) = node.children.get(first) else {
                break;
            };
            let Some(suffix) = remaining.strip_prefix(edge.prefix.as_ref()) else {
                break;
            };
            node = &edge.next;
            if node.route.is_some() && path_boundary(&edge.prefix, suffix) {
                matched = node.route;
            }
            remaining = suffix;
        }
        matched.map(|id| &routes[id])
    }
}

impl Node {
    fn insert(&mut self, path: &[u8], id: usize) {
        let Some(&first) = path.first() else {
            debug_assert!(self.route.is_none(), "validated route keys are unique");
            self.route = Some(id);
            return;
        };
        match self.children.entry(first) {
            Entry::Vacant(entry) => {
                entry.insert(Edge {
                    prefix: path.into(),
                    next: Node {
                        route: Some(id),
                        ..Node::default()
                    },
                });
            }
            Entry::Occupied(mut entry) => {
                let edge = entry.get_mut();
                let common = path
                    .iter()
                    .zip(edge.prefix.iter())
                    .take_while(|(left, right)| left == right)
                    .count();
                if common < edge.prefix.len() {
                    let previous = Edge {
                        prefix: edge.prefix[common..].into(),
                        next: std::mem::take(&mut edge.next),
                    };
                    edge.prefix = edge.prefix[..common].into();
                    edge.next.children.insert(previous.prefix[0], previous);
                }
                edge.next.insert(&path[common..], id);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Balance;

    fn route(host: &str, path: &str, id: usize) -> Route {
        Route {
            id: format!("r{id}"),
            host: host.into(),
            path_prefix: path.into(),
            path_match: PathMatch::Prefix,
            balance: Balance::RoundRobin,
            upstreams: vec!["origin".into()],
        }
    }

    fn linear<'a>(routes: &'a [Route], host: &str, path: &str) -> Option<&'a Route> {
        routes
            .iter()
            .filter(|r| {
                r.host == host
                    && path.starts_with(&r.path_prefix)
                    && (r.path_prefix.ends_with('/')
                        || path.len() == r.path_prefix.len()
                        || path.as_bytes().get(r.path_prefix.len()) == Some(&b'/'))
            })
            .max_by_key(|r| r.path_prefix.len())
    }

    #[test]
    fn preserves_linear_match_semantics() {
        let prefixes = [
            "/api/v1/", "/", "/a", "/api", "/api/", "/apple", "/api/v12", "/á", "/â",
        ];
        let paths = [
            "/",
            "/a",
            "/a/b",
            "/ab",
            "/api",
            "/api/",
            "/api/v1",
            "/api/v1/",
            "/api/v1/items",
            "/api/v12",
            "/api/v12x",
            "/apples",
            "/unknown",
            "/á",
            "/â/o",
            "/ã",
            "*",
        ];
        for shift in 0..prefixes.len() {
            let mut routes: Vec<_> = prefixes
                .iter()
                .cycle()
                .skip(shift)
                .take(prefixes.len())
                .enumerate()
                .map(|(id, path)| route("a.test", path, id))
                .collect();
            routes.push(route("a.test:8080", "/api/", routes.len()));
            for routes in [&routes[..], &routes[..1], &routes[..0]] {
                let index = Routes::compile(routes);
                for host in ["a.test", "a.test:8080", "other.test"] {
                    for path in paths {
                        assert_eq!(
                            index.find(routes, host, path).map(|r| r.id.as_str()),
                            linear(routes, host, path).map(|r| r.id.as_str()),
                            "{host} {path}",
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn wildcard_hosts_use_nginx_name_precedence_before_path_lookup() {
        let routes = [
            route("*.test", "/", 0),
            route("*.example.test", "/", 1),
            route("a.deep.*", "/", 2),
            route("a.*", "/", 3),
            route("a.example.test", "/exact", 4),
            route("*.example.test:8080", "/", 5),
            route("*.example.test", "/api", 6),
        ];
        let compiled = Routes::compile(&routes);
        let resolve = |host, path| compiled.find(&routes, host, path).map(|r| r.id.as_str());
        assert_eq!(resolve("a.example.test", "/exact"), Some("r4"));
        assert_eq!(resolve("a.example.test", "/api"), None);
        assert_eq!(resolve("a.deep.example.test", "/"), Some("r1"));
        assert_eq!(resolve("a.deep.example.test", "/api/item"), Some("r6"));
        assert_eq!(resolve("a.deep.example.test", "/apix"), Some("r1"));
        assert_eq!(resolve("a.deep.invalid", "/"), Some("r2"));
        assert_eq!(resolve("a.invalid", "/"), Some("r3"));
        assert_eq!(resolve("example.test", "/"), Some("r0"));
        assert_eq!(resolve("test", "/"), None);
        assert_eq!(resolve("a.example.test:8080", "/"), Some("r5"));
        for host in [
            "a.example.test:8081",
            "a.example.test:",
            ".example.test",
            "a..example.test",
        ] {
            assert_eq!(resolve(host, "/"), None, "{host}");
        }
        let single = &routes[1..2];
        assert!(Routes::compile(single)
            .find(single, "a.example.test", "/")
            .is_some());
        assert!(Routes::compile(single)
            .find(single, "example.test", "/")
            .is_none());
    }

    #[test]
    fn wildcard_config_accepts_a_complete_edge_label_only() {
        for valid in [
            "*.example.org",
            "mail.*",
            "*.example.org:443",
            "a.b.*:65535",
        ] {
            assert!(wildcard(valid).is_some(), "{valid}");
        }
        for invalid in [
            "*",
            "*org",
            "www.*.org",
            "*.org.*",
            "*.Example.org",
            "*..org",
            "*.-org",
            "*.org:",
            "*.org:0",
            "*.org:65536",
            "*.org:+2",
            "*.org@edge",
        ] {
            assert!(wildcard(invalid).is_none(), "{invalid}");
        }
    }

    #[test]
    fn exact_location_precedes_prefix_only_for_the_identical_path() {
        let mut routes = [
            route("*.example.test", "/api", 0),
            route("*.example.test", "/api", 1),
        ];
        routes[1].path_match = PathMatch::Exact;
        let resolve = |routes: &[Route], path| {
            Routes::compile(routes)
                .find(routes, "x.example.test", path)
                .map(|r| r.id.clone())
        };
        assert_eq!(resolve(&routes, "/api"), Some("r1".into()));
        assert_eq!(resolve(&routes, "/api/"), Some("r0".into()));
        assert_eq!(resolve(&routes, "/api/child"), Some("r0".into()));
        assert_eq!(resolve(&routes, "/apix"), None);
        assert_eq!(resolve(&routes[1..], "/api/child"), None);
        routes[1].host = "x.example.test".into();
        assert_eq!(resolve(&routes, "/api/child"), None);
        assert_eq!(resolve(&routes[1..], "/api"), Some("r1".into()));
    }

    #[test]
    #[ignore = "release-mode route lookup measurement"]
    fn lookup_benchmark() {
        use std::{hint::black_box, time::Instant};
        const LOOKUPS: usize = 262_144;
        println!("layout,routes,repeat,matcher,lookups,elapsed_ns");
        for layout in ["shared-host", "distinct-hosts"] {
            for size in [1, 16, 128, 1024] {
                let routes: Vec<_> = (0..size)
                    .map(|id| {
                        let host = if layout == "shared-host" {
                            "a.test".to_owned()
                        } else {
                            format!("a{id}.test")
                        };
                        route(&host, &format!("/api/{id:04}/"), id)
                    })
                    .collect();
                let index = Routes::compile(&routes);
                let queries: Vec<_> = routes
                    .iter()
                    .map(|r| (r.host.as_str(), format!("{}resource", r.path_prefix)))
                    .collect();
                let measure = |matcher: &str| {
                    let start = Instant::now();
                    for i in 0..LOOKUPS {
                        let (host, path) = &queries[i.wrapping_mul(239) % size];
                        let (host, path) = (black_box(*host), black_box(path.as_str()));
                        let found = if matcher == "radix" {
                            index.find(&routes, host, path)
                        } else {
                            linear(&routes, host, path)
                        };
                        black_box(found);
                    }
                    start.elapsed().as_nanos()
                };
                measure("linear");
                measure("radix");
                for repeat in 1..=7 {
                    let order = if repeat % 2 == 0 {
                        ["linear", "radix"]
                    } else {
                        ["radix", "linear"]
                    };
                    for matcher in order {
                        println!(
                            "{layout},{size},{repeat},{matcher},{LOOKUPS},{}",
                            measure(matcher)
                        );
                    }
                }
            }
        }
    }
}
