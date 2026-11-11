#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! Smoke tests against the real `gough` binary: exit codes and streams as users see them.

use assert_cmd::Command;
use predicates::prelude::*;

fn gough() -> Command {
    let mut cmd = Command::cargo_bin("gough").unwrap();
    cmd.env_remove("GOUGH_CLUSTER_URL")
        .env_remove("GOUGH_TENANT_ID")
        .env_remove("GOUGH_CURRENT_CONTEXT")
        .env_remove("GOUGH_TOKEN");
    cmd
}

#[test]
fn help_exits_zero_and_documents_exit_codes() {
    gough()
        .arg("--help")
        .assert()
        .success()
        .stdout(predicate::str::contains("Exit codes:"))
        .stdout(predicate::str::contains("69  command not implemented yet"));
}

#[test]
fn unimplemented_command_exits_69_with_message() {
    gough()
        .args(["node", "list"])
        .assert()
        .code(69)
        .stdout("")
        .stderr("gough node list: not implemented yet (server contract pending)\n");
}

#[test]
fn version_and_usage_error() {
    gough()
        .arg("--version")
        .assert()
        .success()
        .stdout(predicate::str::starts_with("gough "));
    gough().arg("bogus").assert().code(64).stdout("");
}

#[test]
fn config_round_trip_through_the_binary() {
    let dir = tempfile::tempdir().unwrap();
    let run = |args: &[&str]| {
        let mut cmd = gough();
        cmd.env("XDG_CONFIG_HOME", dir.path()).args(args);
        cmd
    };
    run(&["config", "set", "tenant_id", "t-9"])
        .assert()
        .success();
    run(&["config", "get", "tenant_id"])
        .assert()
        .success()
        .stdout("t-9\n");
    run(&["-v", "config", "list", "-o", "json"])
        .assert()
        .success()
        .stdout(predicate::str::contains("t-9"));
}
