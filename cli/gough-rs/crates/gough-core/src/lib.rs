//! Shared core of the gough CLI: config and contexts, output writer, error type and the
//! documented exit-code mapping. No HTTP and no command wiring lives here.
#![forbid(unsafe_code)]

pub mod config;
pub mod env;
pub mod error;
pub mod output;

pub use config::{ConfigFile, ContextEntry, Overrides, Resolved, Source, Sourced};
pub use env::{Env, MapEnv, StdEnv};
pub use error::{CoreError, ExitCode};
pub use output::{OutputFormat, Writer, color_enabled};
