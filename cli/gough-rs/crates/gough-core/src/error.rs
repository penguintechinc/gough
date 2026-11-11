//! The CLI error type and the documented process exit codes.

use gough_api::ApiError;

/// Process exit codes; stable and documented in `gough --help`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum ExitCode {
    /// Success.
    Ok = 0,
    /// Any error not covered by a more specific code.
    Generic = 1,
    /// The cluster Vault is sealed.
    VaultSealed = 2,
    /// Not authenticated or insufficient scope (HTTP 401/403).
    AuthDenied = 3,
    /// Token tenant does not match the requested tenant.
    TenantMismatch = 4,
    /// Rate limited (HTTP 429) after all retries.
    RateLimited = 5,
    /// Server-side validation failed (HTTP 422).
    ValidationFailed = 6,
    /// Deployment plan compilation failed.
    PlanCompilationFailed = 7,
    /// Safety envelope rejected the operation.
    SafetyEnvelopeRejected = 8,
    /// Cluster unhealthy or unavailable (HTTP 503).
    ClusterUnhealthy = 9,
    /// Disaster-recovery drill failed.
    DrDrillFailed = 10,
    /// Invalid command line (unknown flag, missing argument, bad value).
    Usage = 64,
    /// The command exists in the tree but its server contract is not implemented yet.
    NotImplemented = 69,
    /// Config file unreadable, unparsable or holding invalid values.
    Config = 78,
}

impl ExitCode {
    /// Numeric process exit status.
    pub fn code(self) -> u8 {
        self as u8
    }

    /// Every code with its meaning, in numeric order; rendered into `--help`.
    pub const TABLE: [(ExitCode, &'static str); 14] = [
        (ExitCode::Ok, "success"),
        (ExitCode::Generic, "generic error"),
        (ExitCode::VaultSealed, "vault sealed"),
        (
            ExitCode::AuthDenied,
            "not authenticated / insufficient scope (401, 403)",
        ),
        (ExitCode::TenantMismatch, "tenant mismatch"),
        (ExitCode::RateLimited, "rate limited (429)"),
        (ExitCode::ValidationFailed, "validation failed (422)"),
        (ExitCode::PlanCompilationFailed, "plan compilation failed"),
        (ExitCode::SafetyEnvelopeRejected, "safety envelope rejected"),
        (ExitCode::ClusterUnhealthy, "cluster unhealthy (503)"),
        (ExitCode::DrDrillFailed, "DR drill failed"),
        (ExitCode::Usage, "usage error (bad flags or arguments)"),
        (
            ExitCode::NotImplemented,
            "command not implemented yet (server contract pending)",
        ),
        (ExitCode::Config, "configuration error"),
    ];

    /// Multi-line exit-code reference for help text.
    pub fn help_table() -> String {
        let mut text = String::from("Exit codes:\n");
        for (code, meaning) in Self::TABLE {
            text.push_str(&format!("  {:>2}  {meaning}\n", code.code()));
        }
        text.trim_end().to_owned()
    }
}

/// Every failure a command can end with.
#[derive(Debug, thiserror::Error)]
pub enum CoreError {
    /// The command line was invalid.
    #[error("{0}")]
    Usage(String),
    /// The config file or a config value is invalid.
    #[error("{0}")]
    Config(String),
    /// The command is declared but not implemented yet; fails loudly by design.
    #[error("gough {command}: not implemented yet (server contract pending)")]
    NotImplemented {
        /// Full command path, e.g. `node tag add`.
        command: String,
    },
    /// The API transport reported a failure.
    #[error("{0}")]
    Api(#[from] ApiError),
    /// Writing output failed.
    #[error("cannot write output: {0}")]
    Io(#[from] std::io::Error),
    /// Anything else, with context.
    #[error("{0}")]
    Other(String),
}

impl CoreError {
    /// Maps the error to its documented exit code.
    pub fn exit_code(&self) -> ExitCode {
        match self {
            CoreError::Usage(_) => ExitCode::Usage,
            CoreError::Config(_) => ExitCode::Config,
            CoreError::NotImplemented { .. } => ExitCode::NotImplemented,
            CoreError::Api(err) => api_exit_code(err),
            CoreError::Io(_) | CoreError::Other(_) => ExitCode::Generic,
        }
    }

    /// The stderr line for this error: `error: <msg>`, except not-implemented which is bare.
    pub fn render(&self) -> String {
        match self {
            CoreError::NotImplemented { .. } => self.to_string(),
            _ => format!("error: {self}"),
        }
    }
}

fn api_exit_code(err: &ApiError) -> ExitCode {
    let ApiError::Status {
        status,
        message,
        code,
    } = err
    else {
        return ExitCode::Generic;
    };
    match code.as_deref() {
        Some("vault_sealed") => return ExitCode::VaultSealed,
        Some("plan_compilation_failed") => return ExitCode::PlanCompilationFailed,
        Some("safety_envelope_rejected") => return ExitCode::SafetyEnvelopeRejected,
        Some("dr_drill_failed") => return ExitCode::DrDrillFailed,
        _ => {}
    }
    match status {
        401 => ExitCode::AuthDenied,
        403 if code.as_deref() == Some("forbidden_tenant")
            || message.to_ascii_lowercase().contains("tenant") =>
        {
            ExitCode::TenantMismatch
        }
        403 => ExitCode::AuthDenied,
        429 => ExitCode::RateLimited,
        422 => ExitCode::ValidationFailed,
        503 => ExitCode::ClusterUnhealthy,
        _ => ExitCode::Generic,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rstest::rstest;

    fn status(status: u16, message: &str, code: Option<&str>) -> CoreError {
        CoreError::Api(ApiError::Status {
            status,
            message: message.into(),
            code: code.map(Into::into),
        })
    }

    #[rstest]
    #[case(status(401, "x", None), 3)]
    #[case(status(403, "no scope", None), 3)]
    #[case(status(403, "Tenant mismatch", None), 4)]
    #[case(status(403, "x", Some("forbidden_tenant")), 4)]
    #[case(status(429, "x", None), 5)]
    #[case(status(422, "x", None), 6)]
    #[case(status(503, "x", None), 9)]
    #[case(status(503, "sealed", Some("vault_sealed")), 2)]
    #[case(status(400, "x", Some("plan_compilation_failed")), 7)]
    #[case(status(409, "x", Some("safety_envelope_rejected")), 8)]
    #[case(status(500, "x", Some("dr_drill_failed")), 10)]
    #[case(status(404, "x", None), 1)]
    #[case(CoreError::Api(ApiError::Timeout), 1)]
    #[case(CoreError::Usage("u".into()), 64)]
    #[case(CoreError::Config("c".into()), 78)]
    #[case(CoreError::NotImplemented { command: "a b".into() }, 69)]
    #[case(CoreError::Other("o".into()), 1)]
    #[case(CoreError::Io(std::io::Error::other("broken pipe")), 1)]
    fn maps_to_documented_code(#[case] err: CoreError, #[case] want: u8) {
        assert_eq!(err.exit_code().code(), want, "{err}");
    }

    #[test]
    fn table_is_sorted_unique_and_complete() {
        let codes: Vec<u8> = ExitCode::TABLE.iter().map(|(c, _)| c.code()).collect();
        let mut sorted = codes.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(codes, sorted);
        assert_eq!(codes.len(), 14);
        assert!(ExitCode::help_table().contains("69  command not implemented yet"));
    }

    #[test]
    fn render_prefixes_everything_but_not_implemented() {
        assert_eq!(CoreError::Usage("bad".into()).render(), "error: bad");
        let ni = CoreError::NotImplemented {
            command: "node list".into(),
        };
        assert_eq!(
            ni.render(),
            "gough node list: not implemented yet (server contract pending)"
        );
    }
}
