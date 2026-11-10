//! Command handlers. Only `version`, `config` and `completion` are implemented; every other
//! leaf returns [`CoreError::NotImplemented`] so it can never pretend to succeed.

pub(crate) mod completion;
pub(crate) mod config;
pub(crate) mod version;

use clap::ArgMatches;
use gough_core::{CoreError, Env, Writer};

use crate::cli::{BiomeCmd, Cli, Commands};
use crate::command_path;

/// Routes a parsed invocation to its handler or fails loudly.
pub(crate) fn dispatch(
    cli: &Cli,
    matches: &ArgMatches,
    env: &dyn Env,
    w: &mut Writer<'_>,
) -> Result<(), CoreError> {
    let Some(command) = &cli.command else {
        return Err(CoreError::Usage(
            "a command is required; see `gough --help`".into(),
        ));
    };
    match command {
        Commands::Version => version::run(w),
        Commands::Config(cmd) => config::run(cmd, &cli.global, env, w),
        Commands::Completion(shell) => completion::run(shell, w),
        other => {
            if let Commands::Biome(BiomeCmd::Deploy {
                frontend_mode,
                frontend_endpoint,
                ..
            }) = other
            {
                frontend_mode.validate_endpoint(frontend_endpoint.as_deref())?;
            }
            Err(CoreError::NotImplemented {
                command: command_path(matches),
            })
        }
    }
}
