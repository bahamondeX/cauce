//! The publication fence lives under the same lock as the cache index.
#[derive(Clone, Copy, Default)]
pub(crate) struct Epoch {
    version: u64,
    retired: bool,
}
impl Epoch {
    pub fn ticket(self) -> u64 {
        self.version
    }

    pub fn current(self, ticket: u64) -> bool {
        !self.retired && self.version == ticket
    }

    pub fn invalidate(&mut self) {
        self.version = self.version.wrapping_add(1);
    }

    pub fn retire(&mut self) {
        self.retired = true;
    }

    pub fn retired(self) -> bool {
        self.retired
    }
}
