//! Binary entry point: wires the real process environment into [`gough_cli::run`].
#![forbid(unsafe_code)]

use std::io::IsTerminal;

fn main() -> std::process::ExitCode {
    let args: Vec<std::ffi::OsString> = std::env::args_os().collect();
    let code = gough_cli::run(
        &args,
        &gough_core::StdEnv,
        &mut std::io::stdout().lock(),
        &mut std::io::stderr().lock(),
        std::io::stderr().is_terminal(),
    );
    std::process::ExitCode::from(code)
}
