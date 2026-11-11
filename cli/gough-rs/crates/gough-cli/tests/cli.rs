#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! End-to-end behaviour of the CLI library: tree lock, help snapshots, fail-loud commands,
//! config commands and output/exit-code contracts, all with injected env and streams.

use std::collections::BTreeSet;
use std::ffi::OsString;
use std::path::Path;

use clap::Command;
use gough_cli::{command, run};
use gough_core::MapEnv;

struct Outcome {
    code: u8,
    out: String,
    err: String,
}

fn exec(args: &[&str], env: &MapEnv) -> Outcome {
    let argv: Vec<OsString> = std::iter::once("gough")
        .chain(args.iter().copied())
        .map(OsString::from)
        .collect();
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let code = run(&argv, env, &mut out, &mut err, false);
    Outcome {
        code,
        out: String::from_utf8(out).unwrap(),
        err: String::from_utf8(err).unwrap(),
    }
}

fn env_in(dir: &Path) -> MapEnv {
    MapEnv::new([("XDG_CONFIG_HOME", dir.display().to_string())])
}

/// All command paths below the root (skipping clap's auto-generated `help`).
fn walk(
    cmd: &Command,
    prefix: &mut Vec<String>,
    groups: &mut Vec<Vec<String>>,
    leaves: &mut Vec<Vec<String>>,
) {
    for sub in cmd.get_subcommands().filter(|s| s.get_name() != "help") {
        prefix.push(sub.get_name().to_owned());
        if sub.get_subcommands().any(|s| s.get_name() != "help") {
            groups.push(prefix.clone());
            walk(sub, prefix, groups, leaves);
        } else {
            leaves.push(prefix.clone());
        }
        prefix.pop();
    }
}

fn tree() -> (Vec<Vec<String>>, Vec<Vec<String>>) {
    let (mut groups, mut leaves) = (Vec::new(), Vec::new());
    walk(&command(), &mut Vec::new(), &mut groups, &mut leaves);
    (groups, leaves)
}

const IMPLEMENTED: [&str; 13] = [
    "version",
    "config get",
    "config set",
    "config list",
    "config get-contexts",
    "config current-context",
    "config use-context",
    "config set-context",
    "config delete-context",
    "completion bash",
    "completion zsh",
    "completion fish",
    "completion powershell",
];

#[test]
fn tree_is_locked_at_91_leaves() {
    let (groups, leaves) = tree();
    assert_eq!(leaves.len(), 91, "leaf commands");
    assert_eq!(groups.len(), 24, "command groups (nested groups included)");
    let top: BTreeSet<_> = command()
        .get_subcommands()
        .map(|s| s.get_name().to_owned())
        .collect();
    assert_eq!(top.len(), 25, "root commands: {top:?}");
}

#[test]
fn help_snapshot_for_every_command() {
    let (groups, leaves) = tree();
    let mut paths: Vec<Vec<String>> = vec![vec![]];
    paths.extend(groups);
    paths.extend(leaves);
    assert_eq!(paths.len(), 1 + 24 + 91);
    for path in paths {
        let mut args: Vec<&str> = path.iter().map(String::as_str).collect();
        args.push("--help");
        let o = exec(&args, &MapEnv::default());
        assert_eq!((o.code, o.err.as_str()), (0, ""), "{path:?}");
        let name = if path.is_empty() {
            "root".to_owned()
        } else {
            path.join("_")
        };
        insta::assert_snapshot!(format!("help_{name}"), o.out);
    }
}

/// Builds a minimal valid argv for a leaf by satisfying every required argument.
fn minimal_args(path: &[String]) -> Vec<String> {
    let mut cmd = command();
    cmd.build();
    let mut cur = &cmd;
    for name in path {
        cur = cur.find_subcommand(name).unwrap();
    }
    let mut positional = Vec::new();
    let mut flags = Vec::new();
    for arg in cur
        .get_arguments()
        .filter(|a| a.is_required_set() && !a.is_global_set())
    {
        let value = arg
            .get_possible_values()
            .first()
            .map(|p| p.get_name().to_owned())
            .unwrap_or_else(|| "1".to_owned());
        if arg.is_positional() {
            positional.push(value);
        } else {
            flags.push(format!("--{}", arg.get_long().unwrap()));
            flags.push(value);
        }
    }
    let mut args: Vec<String> = path.to_vec();
    if path == ["biome", "deploy"] {
        args.extend(["--frontend-mode".into(), "none".into()]);
    }
    args.extend(flags);
    args.extend(positional);
    args
}

#[test]
fn every_unimplemented_leaf_fails_loudly_with_exit_69() {
    let (_, leaves) = tree();
    let implemented: BTreeSet<&str> = IMPLEMENTED.into_iter().collect();
    let dir = tempfile::tempdir().unwrap();
    let env = env_in(dir.path());
    let mut checked = 0;
    for path in &leaves {
        let joined = path.join(" ");
        if implemented.contains(joined.as_str()) {
            continue;
        }
        let args = minimal_args(path);
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        for extra in [&[][..], &["-o", "json"][..], &["-q"][..]] {
            let argv: Vec<&str> = refs.iter().chain(extra).copied().collect();
            let o = exec(&argv, &env);
            assert_eq!(o.code, 69, "{joined} {extra:?}: {}", o.err);
            assert_eq!(
                o.err,
                format!("gough {joined}: not implemented yet (server contract pending)\n"),
                "{joined}"
            );
            assert_eq!(o.out, "", "{joined} must not write stdout");
        }
        checked += 1;
    }
    assert_eq!(checked, 78, "78 fail-loud leaves + 13 implemented = 91");
    assert_eq!(IMPLEMENTED.len() + checked, 91);
    for imp in IMPLEMENTED {
        assert!(
            leaves.iter().any(|l| l.join(" ") == imp),
            "{imp} must be a real leaf"
        );
    }
}

#[test]
fn biome_deploy_frontend_rules() {
    let env = MapEnv::default();
    let base = ["biome", "deploy", "b", "--node", "n"];
    let run_with = |extra: &[&str]| {
        let args: Vec<&str> = base.iter().chain(extra).copied().collect();
        exec(&args, &env).code
    };
    assert_eq!(run_with(&[]), 64, "default kube-vip needs an endpoint");
    assert_eq!(run_with(&["--frontend-mode", "external"]), 64);
    assert_eq!(
        run_with(&["--frontend-mode", "none", "--frontend-endpoint", "x"]),
        64
    );
    assert_eq!(run_with(&["--frontend-endpoint", "10.0.0.1"]), 69);
    assert_eq!(run_with(&["--frontend-mode", "none"]), 69);
}

#[test]
fn removed_flags_are_rejected() {
    let env = MapEnv::default();
    for flag in [
        &["--token", "x"][..],
        &["--insecure"][..],
        &["--tenant", "t"][..],
    ] {
        let mut args = vec!["version"];
        args.extend(flag);
        let o = exec(&args, &env);
        assert_eq!(o.code, 64, "{flag:?}");
        assert!(o.err.contains("unexpected argument"), "{}", o.err);
    }
}

#[test]
fn usage_errors_exit_64() {
    let env = MapEnv::default();
    for args in [
        &["node", "reject"][..],
        &["node", "reject", "n1"][..],
        &["bogus"][..],
        &["-o", "xml", "version"][..],
        &["-o", "jsonpath=$[", "version"][..],
        &["primary", "frontend-switch", "--mode", "teleport"][..],
        &["capacity", "forecast", "--horizon-days", "0"][..],
        &["config", "set-context", "x"][..],
        &["-q"][..],
    ] {
        let o = exec(args, &env);
        assert_eq!(o.code, 64, "{args:?}: {}", o.err);
        assert_eq!(o.out, "");
        assert!(!o.err.is_empty());
    }
}

#[test]
fn no_arguments_shows_help_on_stderr_with_usage_code() {
    let o = exec(&[], &MapEnv::default());
    assert_eq!(o.code, 64);
    assert!(o.err.contains("Usage: gough"));
}

#[test]
fn version_outputs() {
    let env = MapEnv::default();
    let o = exec(&["version"], &env);
    assert_eq!(o.code, 0);
    assert!(o.out.starts_with("version     0."), "{}", o.out);
    let o = exec(&["version", "-o", "json"], &env);
    let v: serde_json::Value = serde_json::from_str(&o.out).unwrap();
    assert_eq!(v["version"], env!("CARGO_PKG_VERSION"));
    for key in ["commit", "build_date", "os", "arch"] {
        assert!(v[key].is_string(), "{key}");
    }
    let o = exec(&["--version"], &env);
    assert_eq!(o.out, format!("{}\n", gough_cli::version_line()));
    assert!(o.out.starts_with("gough "));
    assert_eq!(exec(&["version", "-q"], &env).out, "");
    let o = exec(&["version", "-o", "jsonpath=$.version"], &env);
    assert_eq!(o.out, format!("{}\n", env!("CARGO_PKG_VERSION")));
}

#[test]
fn completion_scripts_are_generated_even_when_quiet() {
    for (shell, marker) in [
        ("bash", "complete -F"),
        ("zsh", "#compdef gough"),
        ("fish", "complete -c gough"),
        ("powershell", "Register-ArgumentCompleter"),
    ] {
        let o = exec(&["completion", shell, "-q"], &MapEnv::default());
        assert_eq!(o.code, 0, "{shell}");
        assert!(
            o.out.contains(marker),
            "{shell}: {}",
            &o.out[..o.out.len().min(80)]
        );
    }
}

#[test]
fn config_commands_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    let env = env_in(dir.path());
    let ok = |args: &[&str]| {
        let o = exec(args, &env);
        assert_eq!(o.code, 0, "{args:?}: {}", o.err);
        o.out
    };

    assert_eq!(ok(&["config", "list"]), "No configuration set\n");
    assert_eq!(ok(&["config", "get-contexts"]), "No contexts configured\n");
    assert_eq!(exec(&["config", "current-context"], &env).code, 1);
    let o = exec(&["config", "get", "cluster.url"], &env);
    assert_eq!(
        (o.code, o.err.as_str()),
        (1, "error: config key \"cluster.url\" is not set\n")
    );

    assert_eq!(
        ok(&["config", "set", "cluster.url", "https://base.example.com"]),
        "✓ cluster.url set to https://base.example.com\n"
    );
    ok(&[
        "config",
        "set-context",
        "prod",
        "-c",
        "https://prod.example.com",
        "-t",
        "t-prod",
    ]);
    ok(&[
        "config",
        "set-context",
        "dev",
        "--cluster-url",
        "http://127.0.0.1:9",
    ]);
    assert_eq!(
        ok(&["config", "use-context", "prod"]),
        "✓ Switched to context \"prod\"\n"
    );
    assert_eq!(ok(&["config", "current-context"]), "prod\n");
    assert_eq!(
        ok(&["config", "get", "cluster.url"]),
        "https://prod.example.com\n"
    );
    assert_eq!(
        ok(&["config", "get", "contexts.dev.cluster_url"]),
        "http://127.0.0.1:9\n"
    );
    insta::assert_snapshot!("config_list_table", ok(&["config", "list"]));
    insta::assert_snapshot!("config_get_contexts_table", ok(&["config", "get-contexts"]));
    insta::assert_snapshot!(
        "config_get_contexts_yaml",
        ok(&["config", "get-contexts", "-o", "yaml"])
    );
    insta::assert_snapshot!("config_list_json", ok(&["config", "list", "-o", "json"]));
    assert_eq!(
        ok(&["config", "get-contexts", "-o", "jsonpath=$[*].name"]),
        "dev prod\n"
    );

    // flags > env > file
    assert_eq!(
        ok(&["config", "get", "cluster.url", "--context", "dev"]),
        "http://127.0.0.1:9\n"
    );
    let o = exec(
        &[
            "config",
            "get",
            "cluster.url",
            "--cluster",
            "https://flag.example.com",
        ],
        &env,
    );
    assert_eq!(o.out, "https://flag.example.com\n");
    let with_env = MapEnv::new([
        ("XDG_CONFIG_HOME", dir.path().display().to_string()),
        ("GOUGH_CLUSTER_URL", "https://env.example.com".to_owned()),
        ("GOUGH_CURRENT_CONTEXT", "dev".to_owned()),
    ]);
    assert_eq!(
        exec(&["config", "get", "cluster.url"], &with_env).out,
        "https://env.example.com\n"
    );
    assert_eq!(exec(&["config", "current-context"], &with_env).out, "dev\n");
    let o = exec(
        &[
            "config",
            "get",
            "cluster.url",
            "--cluster",
            "https://flag.example.com",
        ],
        &with_env,
    );
    assert_eq!(o.out, "https://flag.example.com\n");
    let o = exec(&["config", "list"], &with_env);
    assert!(o.out.contains("env"), "{}", o.out);

    // unknown context / key / bad values fail loudly
    for args in [
        &["config", "use-context", "nope"][..],
        &["config", "delete-context", "nope"][..],
        &["config", "set", "bogus", "1"][..],
        &["config", "get", "bogus"][..],
        &[
            "config",
            "set",
            "cluster.url",
            "http://insecure.example.com",
        ][..],
        &["config", "list", "--context", "nope"][..],
    ] {
        let o = exec(args, &env);
        assert_ne!(o.code, 0, "{args:?}");
        assert!(o.err.starts_with("error: "), "{args:?}: {}", o.err);
    }
    assert_eq!(exec(&["config", "set", "bogus", "1"], &env).code, 78);

    assert_eq!(
        ok(&["config", "delete-context", "prod"]),
        "✓ Deleted context \"prod\"\n"
    );
    assert_eq!(exec(&["config", "current-context"], &env).code, 1);
    assert_eq!(
        ok(&["config", "set", "current_context", "dev"]),
        "✓ current_context set to dev\n"
    );
    assert_eq!(ok(&["config", "set", "tenant_id", "t-1", "-q"]), "");
    assert_eq!(
        ok(&["config", "get", "tenant_id", "-o", "json"]),
        "\"t-1\"\n"
    );

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let file = dir.path().join("gough/config.yaml");
        assert_eq!(
            std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            std::fs::metadata(file.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
    }
}

#[test]
fn corrupt_config_is_a_config_error() {
    let dir = tempfile::tempdir().unwrap();
    let cfg = dir.path().join("gough");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(cfg.join("config.yaml"), "cluster: [oops").unwrap();
    let o = exec(&["config", "list"], &env_in(dir.path()));
    assert_eq!(o.code, 78);
    assert!(o.err.contains("invalid config"), "{}", o.err);
    let o = exec(&["config", "list"], &MapEnv::default());
    assert_eq!(o.code, 78, "no HOME/XDG_CONFIG_HOME: {}", o.err);
}

#[test]
fn verbose_flag_is_accepted() {
    let o = exec(&["-v", "version", "-o", "json"], &MapEnv::default());
    assert_eq!(o.code, 0);
}

#[test]
fn error_color_follows_no_color_rules() {
    let argv: Vec<OsString> = ["gough", "config", "get", "tenant_id"]
        .map(OsString::from)
        .to_vec();
    let dir = tempfile::tempdir().unwrap();
    let env = env_in(dir.path());
    let (mut out, mut err) = (Vec::new(), Vec::new());
    assert_eq!(run(&argv, &env, &mut out, &mut err, true), 1);
    assert!(
        String::from_utf8(err)
            .unwrap()
            .starts_with("\x1b[31merror:")
    );
    let argv: Vec<OsString> = ["gough", "--no-color", "config", "get", "tenant_id"]
        .map(OsString::from)
        .to_vec();
    let (mut out, mut err) = (Vec::new(), Vec::new());
    assert_eq!(run(&argv, &env, &mut out, &mut err, true), 1);
    assert!(String::from_utf8(err).unwrap().starts_with("error:"));
}
