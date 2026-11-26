//! Immutable group members and independent selection cursors.
use crate::{
    config::{Balance, Route},
    pool::Endpoint,
};
use std::{
    collections::HashMap,
    net::IpAddr,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
};

pub(crate) struct Group {
    members: Vec<Arc<Endpoint>>,
    weights: Vec<u64>,
    ticket_order: Vec<usize>,
    seeds: Vec<u64>,
    cursor: AtomicUsize,
}

pub(crate) fn mix(mut value: u64) -> u64 {
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d049bb133111eb);
    value ^ (value >> 31)
}

pub(crate) fn hash(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0xcbf29ce484222325, |state, byte| {
        (state ^ u64::from(*byte)).wrapping_mul(0x100000001b3)
    })
}

pub(crate) fn key(ip: IpAddr) -> u64 {
    match ip.to_canonical() {
        IpAddr::V4(ip) => hash(&ip.octets()),
        IpAddr::V6(ip) => hash(&ip.octets()),
    }
}

pub(crate) fn tickets(weights: &[u64]) -> Vec<usize> {
    let sum = weights.iter().sum::<u64>() as i64;
    let mut debts = vec![0i64; weights.len()];
    (0..sum)
        .map(|_| {
            let mut best = 0;
            for i in 0..weights.len() {
                debts[i] += weights[i] as i64;
                if debts[i] > debts[best] {
                    best = i;
                }
            }
            debts[best] -= sum;
            best
        })
        .collect()
}

impl Group {
    pub fn compile(route: &Route, endpoints: &HashMap<String, Arc<Endpoint>>) -> Self {
        let members: Vec<_> = route
            .upstreams
            .iter()
            .map(|name| endpoints[name].clone())
            .collect();
        let weights: Vec<_> = members
            .iter()
            .map(|endpoint| u64::from(endpoint.config.weight))
            .collect();
        let ticket_order = if matches!(route.balance, Balance::WeightedRoundRobin | Balance::IpHash)
        {
            tickets(&weights)
        } else {
            Vec::new()
        };
        let seeds = if route.balance == Balance::ConsistentHash {
            members
                .iter()
                .map(|endpoint| hash(endpoint.config.id.as_bytes()))
                .collect()
        } else {
            Vec::new()
        };
        Self {
            members,
            weights,
            ticket_order,
            seeds,
            cursor: AtomicUsize::new(0),
        }
    }

    fn load(&self, index: usize, weighted: bool) -> (u128, u128) {
        (
            self.members[index].inflight.load(Ordering::Relaxed) as u128,
            if weighted {
                u128::from(self.weights[index])
            } else {
                1
            },
        )
    }

    fn less(&self, a: usize, b: usize, weighted: bool) -> std::cmp::Ordering {
        let (an, ad) = self.load(a, weighted);
        let (bn, bd) = self.load(b, weighted);
        (an * bd).cmp(&(bn * ad))
    }

    pub fn select(
        &self,
        kind: Balance,
        client: IpAddr,
        exclude: Option<&Endpoint>,
    ) -> Option<Arc<Endpoint>> {
        let count = self.members.len();
        let eligible = |index: usize| {
            let member = self.members[index].as_ref();
            exclude.is_none_or(|excluded| !std::ptr::eq(member, excluded)) && member.eligible()
        };
        let result = match kind {
            Balance::ConsistentHash => {
                let key = key(client);
                let score = |i| {
                    // Draw from (0, 1] with 53 bits before the weighted rendezvous transform.
                    let uniform =
                        ((mix(key ^ self.seeds[i]) >> 11) + 1) as f64 / (1u64 << 53) as f64;
                    -uniform.ln() / self.weights[i] as f64
                };
                (0..count)
                    .filter(|&i| eligible(i))
                    .map(|i| (i, score(i)))
                    .min_by(|(a, left), (b, right)| {
                        left.total_cmp(right).then_with(|| {
                            self.members[*a].config.id.cmp(&self.members[*b].config.id)
                        })
                    })
                    .map(|(i, _)| i)
            }
            Balance::IpHash => {
                let total = self.ticket_order.len();
                let start = (mix(key(client)) % total as u64) as usize;
                (0..total)
                    .map(|offset| self.ticket_order[(start + offset) % total])
                    .find(|&i| eligible(i))
            }
            Balance::WeightedRoundRobin => (0..self.ticket_order.len())
                .map(|_| {
                    self.ticket_order
                        [self.cursor.fetch_add(1, Ordering::Relaxed) % self.ticket_order.len()]
                })
                .find(|&i| eligible(i))
                .or_else(|| (0..count).find(|&i| eligible(i))),
            other => {
                let start = self.cursor.fetch_add(1, Ordering::Relaxed) % count;
                let mut peers = (0..count)
                    .map(|offset| (start + offset) % count)
                    .filter(|&i| eligible(i));
                match other {
                    Balance::RoundRobin => peers.next(),
                    Balance::LeastInflight
                    | Balance::LeastConnections
                    | Balance::WeightedLeastConnections => peers.min_by(|&a, &b| {
                        self.less(a, b, other == Balance::WeightedLeastConnections)
                    }),
                    Balance::PowerOfTwoChoices => {
                        let a = start;
                        let b = (a
                            + 1
                            + mix(self.cursor.load(Ordering::Relaxed) as u64) as usize
                                % (count - 1))
                            % count;
                        match (eligible(a), eligible(b)) {
                            (true, true) => Some(if self.less(a, b, true).is_le() { a } else { b }),
                            (true, false) => Some(a),
                            (false, true) => Some(b),
                            (false, false) => peers.next(),
                        }
                    }
                    _ => unreachable!(),
                }
            }
        };
        result.map(|i| self.members[i].clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn smooth_order_and_weights() {
        assert_eq!(tickets(&[3, 1]), [0, 0, 1, 0]);
        let order = tickets(&[3, 7, 2]);
        assert_eq!(order.len(), 12);
        for (i, expected) in [3, 7, 2].into_iter().enumerate() {
            assert_eq!(order.iter().filter(|&&peer| peer == i).count(), expected);
        }
    }
}
