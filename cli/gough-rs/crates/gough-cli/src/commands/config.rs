//! `gough config ...`: local config file and context management (no network).

use gough_core::config::config_path;
use gough_core::{ConfigFile, CoreError, Env, Overrides, Resolved, Source, Writer};

use crate::cli::{ConfigCmd, Global};

fn load(env: &dyn Env) -> Result<(std::path::PathBuf, ConfigFile), CoreError> {
    let path = config_path(env)?;
    let file = ConfigFile::load(&path)?;
    Ok((path, file))
}

fn overrides(global: &Global) -> Overrides<'_> {
    Overrides {
        cluster: global.cluster.as_deref(),
        context: global.context.as_deref(),
    }
}

fn effective(file: &ConfigFile, env: &dyn Env, global: &Global) -> Result<Resolved, CoreError> {
    file.resolve(env, overrides(global))
}

/// Runs one config subcommand; mutating commands save atomically with mode 0600.
pub(crate) fn run(
    cmd: &ConfigCmd,
    global: &Global,
    env: &dyn Env,
    w: &mut Writer<'_>,
) -> Result<(), CoreError> {
    let (path, mut file) = load(env)?;
    match cmd {
        ConfigCmd::Get { key } => get(&file, key, global, env, w),
        ConfigCmd::List => list(&file, global, env, w),
        ConfigCmd::GetContexts => get_contexts(&file, global, env, w),
        ConfigCmd::CurrentContext => {
            let current = effective(&file, env, global)?.current_context;
            match current {
                Some(c) => w.value(&c.value),
                None => Err(CoreError::Other("no current context is set".into())),
            }
        }
        ConfigCmd::Set { key, value } => {
            file.set(key, value)?;
            file.save(&path)?;
            tracing::debug!(key = %key, "config key updated");
            w.success(&format!("{key} set to {value}"))
        }
        ConfigCmd::UseContext { name } => {
            file.use_context(name)?;
            file.save(&path)?;
            w.success(&format!("Switched to context \"{name}\""))
        }
        ConfigCmd::SetContext {
            name,
            cluster_url,
            tenant,
        } => {
            file.set_context(name, cluster_url, tenant.as_deref())?;
            file.save(&path)?;
            w.success(&format!("Context \"{name}\" set"))
        }
        ConfigCmd::DeleteContext { name } => {
            file.delete_context(name)?;
            file.save(&path)?;
            w.success(&format!("Deleted context \"{name}\""))
        }
    }
}

/// Top-level keys reflect flags > env > file; `contexts.*` keys read the file.
fn get(
    file: &ConfigFile,
    key: &str,
    global: &Global,
    env: &dyn Env,
    w: &mut Writer<'_>,
) -> Result<(), CoreError> {
    let stored = file.get(key)?;
    let resolved = effective(file, env, global)?;
    let value = match key {
        "cluster.url" => resolved.cluster_url.map(|s| s.value),
        "tenant_id" => resolved.tenant_id.map(|s| s.value),
        "current_context" => resolved.current_context.map(|s| s.value),
        _ => stored,
    };
    match value {
        Some(v) => w.value(&v),
        None => Err(CoreError::Other(format!("config key \"{key}\" is not set"))),
    }
}

fn list(
    file: &ConfigFile,
    global: &Global,
    env: &dyn Env,
    w: &mut Writer<'_>,
) -> Result<(), CoreError> {
    let resolved = effective(file, env, global)?;
    let mut rows: Vec<Vec<String>> = Vec::new();
    for (key, sourced) in [
        ("cluster.url", resolved.cluster_url),
        ("tenant_id", resolved.tenant_id),
        ("current_context", resolved.current_context),
    ] {
        if let Some(s) = sourced {
            rows.push(vec![key.into(), s.value, s.source.as_str().into()]);
        }
    }
    for (key, value) in file
        .flatten()
        .into_iter()
        .filter(|(k, _)| k.starts_with("contexts."))
    {
        rows.push(vec![key, value, Source::File.as_str().into()]);
    }
    if rows.is_empty() {
        w.info("No configuration set")?;
    }
    if !rows.is_empty() || *w.format() != gough_core::OutputFormat::Table {
        w.table(&["KEY", "VALUE", "SOURCE"], &rows)?;
    }
    Ok(())
}

fn get_contexts(
    file: &ConfigFile,
    global: &Global,
    env: &dyn Env,
    w: &mut Writer<'_>,
) -> Result<(), CoreError> {
    let current = effective(file, env, global)?
        .current_context
        .map(|c| c.value);
    let rows: Vec<Vec<String>> = file
        .contexts
        .iter()
        .map(|(name, ctx)| {
            vec![
                name.clone(),
                ctx.cluster_url.clone().unwrap_or_default(),
                ctx.tenant_id.clone().unwrap_or_default(),
                if current.as_deref() == Some(name.as_str()) {
                    "*".into()
                } else {
                    String::new()
                },
            ]
        })
        .collect();
    if rows.is_empty() && *w.format() == gough_core::OutputFormat::Table {
        return w.info("No contexts configured");
    }
    w.table(&["NAME", "CLUSTER_URL", "TENANT_ID", "ACTIVE"], &rows)
}
