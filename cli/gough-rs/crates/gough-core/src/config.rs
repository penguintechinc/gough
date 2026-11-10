//! Config file, named contexts and precedence resolution (flags > env > file).
//!
//! The file is YAML at `$XDG_CONFIG_HOME/gough/config.yaml` (default `~/.config/gough`,
//! `%APPDATA%\gough` on Windows), written atomically with mode 0600 in a 0700 directory.
//! It holds only non-secret data: cluster URLs and tenant hints, never tokens.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::{CoreError, Env};

/// Env var overriding the cluster URL.
pub const ENV_CLUSTER_URL: &str = "GOUGH_CLUSTER_URL";
/// Env var holding the tenant hint (display only; the server takes the tenant from the JWT).
pub const ENV_TENANT_ID: &str = "GOUGH_TENANT_ID";
/// Env var selecting the active context.
pub const ENV_CURRENT_CONTEXT: &str = "GOUGH_CURRENT_CONTEXT";

/// Top-level `cluster:` section.
#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClusterSection {
    /// Default cluster API URL.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
}

/// One named context: a cluster URL plus an optional tenant hint.
#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContextEntry {
    /// Cluster API URL for this context.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cluster_url: Option<String>,
    /// Tenant hint for this context (never sent as a header).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tenant_id: Option<String>,
}

/// The on-disk config document.
#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfigFile {
    /// Default cluster section.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cluster: Option<ClusterSection>,
    /// Default tenant hint.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tenant_id: Option<String>,
    /// Name of the active context.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub current_context: Option<String>,
    /// Named contexts.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub contexts: BTreeMap<String, ContextEntry>,
}

/// Where an effective value came from, highest precedence first.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// A command-line flag.
    Flag,
    /// An environment variable.
    Env,
    /// The selected context in the config file.
    Context,
    /// A top-level config file key.
    File,
}

impl Source {
    /// Lowercase label used in `config list`.
    pub fn as_str(self) -> &'static str {
        match self {
            Source::Flag => "flag",
            Source::Env => "env",
            Source::Context => "context",
            Source::File => "file",
        }
    }
}

/// A resolved value tagged with its origin.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sourced {
    /// The value.
    pub value: String,
    /// Where it came from.
    pub source: Source,
}

/// Flag-level overrides fed into [`ConfigFile::resolve`].
#[derive(Debug, Default, Clone, Copy)]
pub struct Overrides<'a> {
    /// `--cluster`.
    pub cluster: Option<&'a str>,
    /// `--context`.
    pub context: Option<&'a str>,
}

/// Effective settings after applying flags > env > file.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Resolved {
    /// Effective cluster URL.
    pub cluster_url: Option<Sourced>,
    /// Effective tenant hint (display only).
    pub tenant_id: Option<Sourced>,
    /// Effective active context name.
    pub current_context: Option<Sourced>,
}

/// Resolves the config file location from the environment.
pub fn config_path(env: &dyn Env) -> Result<PathBuf, CoreError> {
    let base = if let Some(xdg) = env
        .var("XDG_CONFIG_HOME")
        .filter(|p| Path::new(p).is_absolute())
    {
        PathBuf::from(xdg)
    } else if let Some(appdata) = env.var("APPDATA").filter(|_| cfg!(windows)) {
        PathBuf::from(appdata)
    } else if let Some(home) = env.var("HOME").or_else(|| env.var("USERPROFILE")) {
        PathBuf::from(home).join(".config")
    } else {
        return Err(CoreError::Config(
            "cannot locate the config directory: set XDG_CONFIG_HOME or HOME".into(),
        ));
    };
    Ok(base.join("gough").join("config.yaml"))
}

/// Validates a cluster URL with the same rules as the transport (https, or http on loopback).
pub fn validate_cluster_url(url: &str) -> Result<(), CoreError> {
    gough_api::ClientConfig::new(url)
        .map(|_| ())
        .map_err(|e| CoreError::Config(e.to_string()))
}

fn validate_context_name(name: &str) -> Result<(), CoreError> {
    let ok = !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if ok {
        Ok(())
    } else {
        Err(CoreError::Config(format!(
            "invalid context name {name:?}: use letters, digits, '-' and '_'"
        )))
    }
}

fn validate_tenant(tenant: &str) -> Result<(), CoreError> {
    if tenant.is_empty() || tenant.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(CoreError::Config(format!("invalid tenant id {tenant:?}")));
    }
    Ok(())
}

impl ConfigFile {
    /// Loads the file; a missing file is an empty config, a malformed one is a loud error.
    pub fn load(path: &Path) -> Result<Self, CoreError> {
        let text = match std::fs::read_to_string(path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Self::default()),
            Err(e) => {
                return Err(CoreError::Config(format!(
                    "cannot read {}: {e}",
                    path.display()
                )));
            }
        };
        if text.trim().is_empty() {
            return Ok(Self::default());
        }
        serde_norway::from_str(&text)
            .map_err(|e| CoreError::Config(format!("invalid config {}: {e}", path.display())))
    }

    /// Writes the file atomically (temp file in the same directory, fsync, rename) with mode 0600.
    pub fn save(&self, path: &Path) -> Result<(), CoreError> {
        let fail = |what: &str, e: &dyn std::fmt::Display| {
            CoreError::Config(format!("{what} {}: {e}", path.display()))
        };
        let dir = path
            .parent()
            .ok_or_else(|| fail("no parent directory for", &"-"))?;
        create_private_dir(dir).map_err(|e| fail("cannot create directory for", &e))?;
        let text = serde_norway::to_string(self).map_err(|e| fail("cannot serialize", &e))?;
        let mut tmp = tempfile::Builder::new()
            .prefix(".config-")
            .suffix(".tmp")
            .tempfile_in(dir)
            .map_err(|e| fail("cannot create temp file for", &e))?;
        tmp.write_all(text.as_bytes())
            .map_err(|e| fail("cannot write", &e))?;
        tmp.as_file()
            .sync_all()
            .map_err(|e| fail("cannot sync", &e))?;
        set_private_mode(tmp.path()).map_err(|e| fail("cannot set mode on", &e))?;
        tmp.persist(path)
            .map_err(|e| fail("cannot replace", &e.error))?;
        Ok(())
    }

    /// Value of a dotted key as stored in the file (no env/flag layering).
    pub fn get(&self, key: &str) -> Result<Option<String>, CoreError> {
        Ok(match parse_key(key)? {
            Key::ClusterUrl => self.cluster.as_ref().and_then(|c| c.url.clone()),
            Key::TenantId => self.tenant_id.clone(),
            Key::CurrentContext => self.current_context.clone(),
            Key::ContextClusterUrl(n) => self.contexts.get(&n).and_then(|c| c.cluster_url.clone()),
            Key::ContextTenantId(n) => self.contexts.get(&n).and_then(|c| c.tenant_id.clone()),
        })
    }

    /// Sets a dotted key after validating key and value.
    pub fn set(&mut self, key: &str, value: &str) -> Result<(), CoreError> {
        match parse_key(key)? {
            Key::ClusterUrl => {
                validate_cluster_url(value)?;
                self.cluster.get_or_insert_with(Default::default).url = Some(value.into());
            }
            Key::TenantId => {
                validate_tenant(value)?;
                self.tenant_id = Some(value.into());
            }
            Key::CurrentContext => self.use_context(value)?,
            Key::ContextClusterUrl(name) => {
                validate_cluster_url(value)?;
                self.contexts.entry(name).or_default().cluster_url = Some(value.into());
            }
            Key::ContextTenantId(name) => {
                validate_tenant(value)?;
                self.contexts.entry(name).or_default().tenant_id = Some(value.into());
            }
        }
        Ok(())
    }

    /// Creates or updates a context; an omitted tenant keeps the existing one.
    pub fn set_context(
        &mut self,
        name: &str,
        cluster_url: &str,
        tenant: Option<&str>,
    ) -> Result<(), CoreError> {
        validate_context_name(name)?;
        validate_cluster_url(cluster_url)?;
        if let Some(t) = tenant {
            validate_tenant(t)?;
        }
        let entry = self.contexts.entry(name.into()).or_default();
        entry.cluster_url = Some(cluster_url.into());
        if let Some(t) = tenant {
            entry.tenant_id = Some(t.into());
        }
        Ok(())
    }

    /// Makes an existing context the active one.
    pub fn use_context(&mut self, name: &str) -> Result<(), CoreError> {
        if !self.contexts.contains_key(name) {
            return Err(unknown_context(name));
        }
        self.current_context = Some(name.into());
        Ok(())
    }

    /// Removes a context, clearing `current_context` when it pointed at it.
    pub fn delete_context(&mut self, name: &str) -> Result<(), CoreError> {
        if self.contexts.remove(name).is_none() {
            return Err(unknown_context(name));
        }
        if self.current_context.as_deref() == Some(name) {
            self.current_context = None;
        }
        Ok(())
    }

    /// Applies flags > env > (selected context > top-level file) for cluster URL, tenant hint
    /// and active context. A selected context that does not exist is an error, never ignored.
    pub fn resolve(&self, env: &dyn Env, flags: Overrides<'_>) -> Result<Resolved, CoreError> {
        let current_context = pick([
            (flags.context.map(str::to_owned), Source::Flag),
            (env.var(ENV_CURRENT_CONTEXT), Source::Env),
            (self.current_context.clone(), Source::File),
        ]);
        let context = match &current_context {
            Some(sel) => Some(
                self.contexts
                    .get(&sel.value)
                    .ok_or_else(|| unknown_context(&sel.value))?,
            ),
            None => None,
        };
        let cluster_url = pick([
            (flags.cluster.map(str::to_owned), Source::Flag),
            (env.var(ENV_CLUSTER_URL), Source::Env),
            (context.and_then(|c| c.cluster_url.clone()), Source::Context),
            (
                self.cluster.as_ref().and_then(|c| c.url.clone()),
                Source::File,
            ),
        ]);
        if let Some(url) = &cluster_url {
            validate_cluster_url(&url.value)
                .map_err(|e| CoreError::Config(format!("{e} (from {})", url.source.as_str())))?;
        }
        let tenant_id = pick([
            (env.var(ENV_TENANT_ID), Source::Env),
            (context.and_then(|c| c.tenant_id.clone()), Source::Context),
            (self.tenant_id.clone(), Source::File),
        ]);
        Ok(Resolved {
            cluster_url,
            tenant_id,
            current_context,
        })
    }

    /// Every file-stored key with its value, sorted by key.
    pub fn flatten(&self) -> Vec<(String, String)> {
        let mut out = Vec::new();
        if let Some(url) = self.cluster.as_ref().and_then(|c| c.url.clone()) {
            out.push(("cluster.url".to_owned(), url));
        }
        if let Some(t) = &self.tenant_id {
            out.push(("tenant_id".to_owned(), t.clone()));
        }
        if let Some(c) = &self.current_context {
            out.push(("current_context".to_owned(), c.clone()));
        }
        for (name, ctx) in &self.contexts {
            if let Some(u) = &ctx.cluster_url {
                out.push((format!("contexts.{name}.cluster_url"), u.clone()));
            }
            if let Some(t) = &ctx.tenant_id {
                out.push((format!("contexts.{name}.tenant_id"), t.clone()));
            }
        }
        out.sort();
        out
    }
}

fn unknown_context(name: &str) -> CoreError {
    CoreError::Config(format!(
        "unknown context {name:?}; see `gough config get-contexts`"
    ))
}

fn pick<const N: usize>(candidates: [(Option<String>, Source); N]) -> Option<Sourced> {
    candidates.into_iter().find_map(|(v, source)| {
        v.filter(|v| !v.is_empty())
            .map(|value| Sourced { value, source })
    })
}

enum Key {
    ClusterUrl,
    TenantId,
    CurrentContext,
    ContextClusterUrl(String),
    ContextTenantId(String),
}

fn parse_key(key: &str) -> Result<Key, CoreError> {
    let bad = || {
        CoreError::Config(format!(
            "unknown config key {key:?}; valid keys: cluster.url, tenant_id, current_context, \
             contexts.<name>.cluster_url, contexts.<name>.tenant_id"
        ))
    };
    match key {
        "cluster.url" => Ok(Key::ClusterUrl),
        "tenant_id" => Ok(Key::TenantId),
        "current_context" => Ok(Key::CurrentContext),
        _ => {
            let rest = key.strip_prefix("contexts.").ok_or_else(bad)?;
            let (name, field) = rest.rsplit_once('.').ok_or_else(bad)?;
            validate_context_name(name)?;
            match field {
                "cluster_url" => Ok(Key::ContextClusterUrl(name.into())),
                "tenant_id" => Ok(Key::ContextTenantId(name.into())),
                _ => Err(bad()),
            }
        }
    }
}

#[cfg(unix)]
fn create_private_dir(dir: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
}

#[cfg(not(unix))]
fn create_private_dir(dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)
}

#[cfg(unix)]
fn set_private_mode(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn set_private_mode(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::MapEnv;

    const URL: &str = "https://gough.example.com";

    fn file() -> ConfigFile {
        let mut f = ConfigFile::default();
        f.set("cluster.url", "https://file.example.com").unwrap();
        f.set("tenant_id", "t-file").unwrap();
        f.set_context("prod", "https://prod.example.com", Some("t-prod"))
            .unwrap();
        f.set_context("dev", "http://127.0.0.1:8080", None).unwrap();
        f
    }

    #[test]
    fn path_precedence() {
        let p = |pairs: &[(&str, &str)]| {
            config_path(&MapEnv::new(pairs.iter().copied())).map(|p| p.display().to_string())
        };
        assert_eq!(
            p(&[("XDG_CONFIG_HOME", "/x"), ("HOME", "/h")]).unwrap(),
            "/x/gough/config.yaml"
        );
        assert_eq!(
            p(&[("XDG_CONFIG_HOME", "rel"), ("HOME", "/h")]).unwrap(),
            "/h/.config/gough/config.yaml"
        );
        assert_eq!(
            p(&[("HOME", "/h")]).unwrap(),
            "/h/.config/gough/config.yaml"
        );
        assert!(matches!(p(&[]), Err(CoreError::Config(_))));
    }

    #[test]
    fn precedence_flag_env_context_file() {
        let f = file();
        let none = MapEnv::default();
        let r = f.resolve(&none, Overrides::default()).unwrap();
        assert_eq!(
            r.cluster_url.unwrap(),
            Sourced {
                value: "https://file.example.com".into(),
                source: Source::File
            }
        );
        assert_eq!(r.tenant_id.unwrap().source, Source::File);
        assert!(r.current_context.is_none());

        let r = f
            .resolve(
                &none,
                Overrides {
                    cluster: None,
                    context: Some("prod"),
                },
            )
            .unwrap();
        assert_eq!(
            r.cluster_url.unwrap(),
            Sourced {
                value: "https://prod.example.com".into(),
                source: Source::Context
            }
        );
        assert_eq!(
            r.tenant_id.unwrap(),
            Sourced {
                value: "t-prod".into(),
                source: Source::Context
            }
        );

        let env = MapEnv::new([
            (ENV_CLUSTER_URL, "https://env.example.com"),
            (ENV_TENANT_ID, "t-env"),
        ]);
        let r = f
            .resolve(
                &env,
                Overrides {
                    cluster: None,
                    context: Some("prod"),
                },
            )
            .unwrap();
        assert_eq!(r.cluster_url.unwrap().source, Source::Env);
        assert_eq!(r.tenant_id.unwrap().source, Source::Env);

        let r = f
            .resolve(
                &env,
                Overrides {
                    cluster: Some("https://flag.example.com"),
                    context: Some("prod"),
                },
            )
            .unwrap();
        assert_eq!(
            r.cluster_url.unwrap(),
            Sourced {
                value: "https://flag.example.com".into(),
                source: Source::Flag
            }
        );
    }

    #[test]
    fn context_selection_order_and_unknown_context() {
        let mut f = file();
        f.use_context("dev").unwrap();
        let env = MapEnv::new([(ENV_CURRENT_CONTEXT, "prod")]);
        let r = f.resolve(&env, Overrides::default()).unwrap();
        assert_eq!(
            r.current_context.unwrap(),
            Sourced {
                value: "prod".into(),
                source: Source::Env
            }
        );
        let r = f
            .resolve(
                &env,
                Overrides {
                    cluster: None,
                    context: Some("dev"),
                },
            )
            .unwrap();
        assert_eq!(r.current_context.unwrap().source, Source::Flag);
        let r = f.resolve(&MapEnv::default(), Overrides::default()).unwrap();
        assert_eq!(r.current_context.unwrap().source, Source::File);
        let err = f.resolve(
            &MapEnv::default(),
            Overrides {
                cluster: None,
                context: Some("nope"),
            },
        );
        assert!(matches!(err, Err(CoreError::Config(m)) if m.contains("unknown context")));
    }

    #[test]
    fn resolved_url_is_validated_with_origin() {
        let f = ConfigFile::default();
        let env = MapEnv::new([(ENV_CLUSTER_URL, "http://insecure.example.com")]);
        let err = f
            .resolve(&env, Overrides::default())
            .unwrap_err()
            .to_string();
        assert!(
            err.contains("plain http") && err.contains("from env"),
            "{err}"
        );
    }

    #[test]
    fn empty_config_resolves_to_nothing() {
        let r = ConfigFile::default()
            .resolve(&MapEnv::default(), Overrides::default())
            .unwrap();
        assert_eq!(r, Resolved::default());
    }

    #[test]
    fn get_set_round_trip_and_validation() {
        let mut f = ConfigFile::default();
        f.set("cluster.url", URL).unwrap();
        f.set("contexts.a-b_1.tenant_id", "t1").unwrap();
        f.set("contexts.a-b_1.cluster_url", URL).unwrap();
        f.set("current_context", "a-b_1").unwrap();
        assert_eq!(f.get("cluster.url").unwrap().as_deref(), Some(URL));
        assert_eq!(
            f.get("contexts.a-b_1.tenant_id").unwrap().as_deref(),
            Some("t1")
        );
        assert_eq!(
            f.get("contexts.a-b_1.cluster_url").unwrap().as_deref(),
            Some(URL)
        );
        assert_eq!(f.get("current_context").unwrap().as_deref(), Some("a-b_1"));
        assert_eq!(f.get("tenant_id").unwrap(), None);
        assert_eq!(f.get("contexts.zzz.tenant_id").unwrap(), None);
        for (k, v) in [
            ("nope", "x"),
            ("contexts.x", "v"),
            ("contexts.x.bogus", "v"),
            ("contexts.bad name.tenant_id", "v"),
            ("contexts..tenant_id", "v"),
            ("cluster.url", "http://example.com"),
            ("tenant_id", "has space"),
            ("tenant_id", ""),
            ("current_context", "missing"),
            ("contexts.x.cluster_url", "ftp://x"),
        ] {
            assert!(f.set(k, v).is_err(), "{k}={v}");
        }
        assert!(f.get("nope").is_err());
    }

    #[test]
    fn context_lifecycle() {
        let mut f = ConfigFile::default();
        assert!(f.set_context("bad.name", URL, None).is_err());
        assert!(f.set_context("ok", "http://x.example", None).is_err());
        assert!(f.set_context("ok", URL, Some("a b")).is_err());
        f.set_context("ok", URL, Some("t1")).unwrap();
        f.set_context("ok", "https://other.example.com", None)
            .unwrap();
        assert_eq!(f.contexts["ok"].tenant_id.as_deref(), Some("t1"));
        assert_eq!(
            f.contexts["ok"].cluster_url.as_deref(),
            Some("https://other.example.com")
        );
        assert!(f.use_context("missing").is_err());
        f.use_context("ok").unwrap();
        assert!(f.delete_context("missing").is_err());
        f.delete_context("ok").unwrap();
        assert!(f.current_context.is_none() && f.contexts.is_empty());
        f.set_context("a", URL, None).unwrap();
        f.set_context("b", URL, None).unwrap();
        f.use_context("a").unwrap();
        f.delete_context("b").unwrap();
        assert_eq!(f.current_context.as_deref(), Some("a"));
    }

    #[test]
    fn flatten_is_sorted_and_complete() {
        let keys: Vec<String> = file().flatten().into_iter().map(|(k, _)| k).collect();
        assert_eq!(
            keys,
            [
                "cluster.url",
                "contexts.dev.cluster_url",
                "contexts.prod.cluster_url",
                "contexts.prod.tenant_id",
                "tenant_id"
            ]
        );
        let mut f = file();
        f.use_context("dev").unwrap();
        assert!(f.flatten().iter().any(|(k, _)| k == "current_context"));
    }

    #[test]
    fn save_load_round_trip_with_private_modes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/gough/config.yaml");
        let f = file();
        f.save(&path).unwrap();
        assert_eq!(ConfigFile::load(&path).unwrap(), f);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode(&path), 0o600);
            assert_eq!(mode(path.parent().unwrap()), 0o700);
        }
        let mut g = f.clone();
        g.set("tenant_id", "t2").unwrap();
        g.save(&path).unwrap();
        assert_eq!(ConfigFile::load(&path).unwrap(), g);
        let leftovers: Vec<_> = std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(leftovers, ["config.yaml"], "temp file must not linger");
    }

    #[test]
    fn save_replaces_loose_permissions() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.yaml");
        std::fs::write(&path, "tenant_id: old\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        }
        file().save(&path).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn save_failures_are_loud() {
        let dir = tempfile::tempdir().unwrap();
        let blocker = dir.path().join("file");
        std::fs::write(&blocker, "x").unwrap();
        let err = file().save(&blocker.join("config.yaml")).unwrap_err();
        assert!(matches!(err, CoreError::Config(m) if m.contains("cannot create directory")));
        let err = file().save(Path::new("/")).unwrap_err();
        assert!(matches!(err, CoreError::Config(_)));
        // The target is a directory: rename must fail and the temp file must be cleaned up.
        let target = dir.path().join("as-dir");
        std::fs::create_dir(&target).unwrap();
        assert!(file().save(&target).is_err());
        let names: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert!(
            names.iter().all(|n| !n.to_string_lossy().ends_with(".tmp")),
            "{names:?}"
        );
    }

    #[test]
    fn load_edge_cases() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(
            ConfigFile::load(&dir.path().join("absent.yaml")).unwrap(),
            ConfigFile::default()
        );
        let blank = dir.path().join("blank.yaml");
        std::fs::write(&blank, "  \n").unwrap();
        assert_eq!(ConfigFile::load(&blank).unwrap(), ConfigFile::default());
        let bad = dir.path().join("bad.yaml");
        std::fs::write(&bad, "tenant_id: [unterminated").unwrap();
        assert!(
            matches!(ConfigFile::load(&bad), Err(CoreError::Config(m)) if m.contains("invalid config"))
        );
        std::fs::write(&bad, "typo_key: 1\n").unwrap();
        assert!(ConfigFile::load(&bad).is_err());
        assert!(
            matches!(ConfigFile::load(dir.path()), Err(CoreError::Config(m)) if m.contains("cannot read"))
        );
    }

    #[test]
    fn reads_legacy_go_config_layout() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("config.yaml");
        std::fs::write(
            &p,
            "cluster:\n  url: https://g.example.com\ntenant_id: t1\ncurrent_context: a\ncontexts:\n  a:\n    cluster_url: https://a.example.com\n    tenant_id: ta\n",
        )
        .unwrap();
        let f = ConfigFile::load(&p).unwrap();
        assert_eq!(
            f.get("contexts.a.tenant_id").unwrap().as_deref(),
            Some("ta")
        );
    }

    #[test]
    fn source_labels() {
        let labels: Vec<_> = [Source::Flag, Source::Env, Source::Context, Source::File]
            .iter()
            .map(|s| s.as_str())
            .collect();
        assert_eq!(labels, ["flag", "env", "context", "file"]);
    }
}
