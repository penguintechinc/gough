//! Injectable environment lookup so precedence rules are testable without touching the process.

use std::collections::BTreeMap;

/// Read-only view of environment variables.
pub trait Env {
    /// Returns the variable's value; unset, empty and non-UTF-8 values all read as `None`.
    fn var(&self, key: &str) -> Option<String>;
}

/// The real process environment.
#[derive(Debug, Default, Clone, Copy)]
pub struct StdEnv;

impl Env for StdEnv {
    fn var(&self, key: &str) -> Option<String> {
        std::env::var(key).ok().filter(|v| !v.is_empty())
    }
}

/// In-memory environment for tests and embedding.
#[derive(Debug, Default, Clone)]
pub struct MapEnv(BTreeMap<String, String>);

impl MapEnv {
    /// Builds an environment from key/value pairs.
    pub fn new<K: Into<String>, V: Into<String>>(pairs: impl IntoIterator<Item = (K, V)>) -> Self {
        Self(
            pairs
                .into_iter()
                .map(|(k, v)| (k.into(), v.into()))
                .collect(),
        )
    }
}

impl Env for MapEnv {
    fn var(&self, key: &str) -> Option<String> {
        self.0.get(key).filter(|v| !v.is_empty()).cloned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn map_env_hides_empty_values() {
        let env = MapEnv::new([("A", "1"), ("B", "")]);
        assert_eq!(env.var("A").as_deref(), Some("1"));
        assert_eq!(env.var("B"), None);
        assert_eq!(env.var("C"), None);
    }

    #[test]
    fn std_env_reads_process_environment() {
        assert!(StdEnv.var("PATH").is_some());
        assert_eq!(StdEnv.var("GOUGH_SURELY_UNSET_VARIABLE_XYZ"), None);
    }
}
