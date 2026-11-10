//! `gough completion <shell>`: static completion scripts via clap_complete.

use clap_complete::Shell;
use gough_core::{CoreError, Writer};

use crate::cli::CompletionCmd;

/// Writes the script to stdout through the writer's stream; never suppressed by `--quiet`
/// because the script is the command's whole purpose.
pub(crate) fn run(cmd: &CompletionCmd, w: &mut Writer<'_>) -> Result<(), CoreError> {
    let shell = match cmd {
        CompletionCmd::Bash => Shell::Bash,
        CompletionCmd::Zsh => Shell::Zsh,
        CompletionCmd::Fish => Shell::Fish,
        CompletionCmd::Powershell => Shell::PowerShell,
    };
    let mut buf = Vec::new();
    clap_complete::generate(shell, &mut crate::command(), "gough", &mut buf);
    w.raw(&buf)
}
