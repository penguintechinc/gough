//! `gough version`: build identity of this binary.

use gough_core::{CoreError, Writer};

/// Crate version.
pub(crate) const VERSION: &str = env!("CARGO_PKG_VERSION");
/// Git commit injected at build time via `GOUGH_COMMIT`.
pub(crate) const COMMIT: &str = match option_env!("GOUGH_COMMIT") {
    Some(c) => c,
    None => "unknown",
};
/// Build date injected at build time via `GOUGH_BUILD_DATE`.
pub(crate) const BUILD_DATE: &str = match option_env!("GOUGH_BUILD_DATE") {
    Some(d) => d,
    None => "unknown",
};

/// Prints version, commit, build date, OS and architecture.
pub(crate) fn run(w: &mut Writer<'_>) -> Result<(), CoreError> {
    w.kv(&[
        ("version", VERSION.to_owned()),
        ("commit", COMMIT.to_owned()),
        ("build_date", BUILD_DATE.to_owned()),
        ("os", std::env::consts::OS.to_owned()),
        ("arch", std::env::consts::ARCH.to_owned()),
    ])
}
