//! Immutable host and path indexes compiled once per validated configuration.
use crate::config::Route;
use std::collections::{btree_map::Entry, BTreeMap, HashMap};

pub struct Routes {
    single: bool,
    hosts: HashMap<String, Node>,
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
        let mut hosts = HashMap::<String, Node>::new();
        if routes.len() > 1 {
            for (id, route) in routes.iter().enumerate() {
                hosts
                    .entry(route.host.clone())
                    .or_default()
                    .insert(route.path_prefix.as_bytes(), id);
            }
        }
        Self {
            single: routes.len() == 1,
            hosts,
        }
    }

    pub fn find<'a>(&self, routes: &'a [Route], host: &str, path: &str) -> Option<&'a Route> {
        if self.single {
            let route = &routes[0];
            let prefix = route.path_prefix.as_bytes();
            return (route.host == host
                && path
                    .as_bytes()
                    .strip_prefix(prefix)
                    .is_some_and(|rest| path_boundary(prefix, rest)))
            .then_some(route);
        }

        let mut node = self.hosts.get(host)?;
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
