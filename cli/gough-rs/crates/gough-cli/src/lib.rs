//! gough command line interface: clap tree, dispatch and the testable [`run`] entry point.
#![forbid(unsafe_code)]

mod cli;
mod commands;

use std::ffi::OsString;
use std::io::Write;

use clap::{ArgMatches, FromArgMatches, error::ErrorKind};
use gough_core::{CoreError, Env, ExitCode, Writer, color_enabled};

pub use cli::{Cli, Commands, Global, command};

/// Version line printed by `gough --version`.
pub fn version_line() -> String {
    format!(
        "gough {} (commit {}, built {})",
        commands::version::VERSION,
        commands::version::COMMIT,
        commands::version::BUILD_DATE
    )
}

/// Runs the CLI against injected args, environment and streams; returns the process exit code.
/// Keeping all I/O injectable is what makes every path, including exit codes, testable.
pub fn run(
    args: &[OsString],
    env: &dyn Env,
    out: &mut dyn Write,
    err: &mut dyn Write,
    stderr_is_terminal: bool,
) -> u8 {
    match execute(args, env, out, err, stderr_is_terminal) {
        Ok(code) => code.code(),
        Err(e) => {
            let _ = writeln!(err, "{}", e.render());
            e.exit_code().code()
        }
    }
}

fn execute(
    args: &[OsString],
    env: &dyn Env,
    out: &mut dyn Write,
    err: &mut dyn Write,
    stderr_is_terminal: bool,
) -> Result<ExitCode, CoreError> {
    let matches = match command().try_get_matches_from(args.iter().cloned()) {
        Ok(m) => m,
        Err(e) => return clap_exit(&e, out, err),
    };
    let cli = match Cli::from_arg_matches(&matches) {
        Ok(cli) => cli,
        Err(e) => return clap_exit(&e, out, err),
    };
    init_tracing(cli.global.verbose);
    if cli.version {
        writeln!(out, "{}", version_line())?;
        return Ok(ExitCode::Ok);
    }
    let color = color_enabled(cli.global.no_color, env, stderr_is_terminal);
    let mut writer = Writer::new(cli.global.output.clone(), cli.global.quiet, color, out, err);
    match commands::dispatch(&cli, &matches, env, &mut writer) {
        Ok(()) => Ok(ExitCode::Ok),
        Err(e) => {
            writer.error(&e.render())?;
            Ok(e.exit_code())
        }
    }
}

fn clap_exit(
    e: &clap::Error,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> Result<ExitCode, CoreError> {
    let text = e.render().to_string();
    match e.kind() {
        ErrorKind::DisplayHelp | ErrorKind::DisplayVersion => {
            write!(out, "{text}")?;
            Ok(ExitCode::Ok)
        }
        _ => {
            write!(err, "{text}")?;
            Ok(ExitCode::Usage)
        }
    }
}

fn init_tracing(verbose: bool) {
    if verbose {
        let _ = tracing_subscriber::fmt()
            .with_max_level(tracing::Level::DEBUG)
            .with_writer(std::io::stderr)
            .with_ansi(false)
            .try_init();
    }
}

/// Space-joined subcommand path of the parsed invocation, e.g. `node tag add`.
pub(crate) fn command_path(matches: &ArgMatches) -> String {
    let mut parts = Vec::new();
    let mut current = matches;
    while let Some((name, sub)) = current.subcommand() {
        parts.push(name);
        current = sub;
    }
    parts.join(" ")
}
