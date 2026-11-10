use serde_json::Value;

const MAX_MESSAGE_CHARS: usize = 512;

/// Every failure the transport can report; callers map it to an exit code and a message.
#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    /// The server answered with a non-2xx status.
    #[error("HTTP {status}: {message}")]
    Status {
        /// HTTP status code.
        status: u16,
        /// Human-readable message extracted from the error envelope.
        message: String,
        /// Machine-readable error code from the envelope, when present.
        code: Option<String>,
    },
    /// Connection, DNS or TLS failure before a response arrived.
    #[error("network error: {0}")]
    Network(String),
    /// The request exceeded its timeout.
    #[error("request timed out")]
    Timeout,
    /// The `--ca-file` could not be read.
    #[error("cannot read CA file {path}: {reason}")]
    CaFileRead {
        /// Path given by the user.
        path: String,
        /// Underlying I/O failure.
        reason: String,
    },
    /// The `--ca-file` held no usable PEM certificate.
    #[error("invalid CA file {path}: {reason}")]
    CaFileInvalid {
        /// Path given by the user.
        path: String,
        /// Why parsing failed.
        reason: String,
    },
    /// The cluster URL is malformed or not allowed.
    #[error("invalid cluster URL: {0}")]
    InvalidUrl(String),
    /// The response body was not the expected shape.
    #[error("cannot decode response: {0}")]
    Decode(String),
    /// The token provider could not supply a credential.
    #[error("cannot obtain credentials: {0}")]
    Token(String),
    /// The HTTP client could not be constructed.
    #[error("cannot build HTTP client: {0}")]
    ClientBuild(String),
}

/// Parsed form of a server error body.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Envelope {
    pub message: String,
    pub code: Option<String>,
}

impl Envelope {
    /// Parses an error body tolerating `{"error":"text"}`, `{"error":{"code","message"}}`,
    /// top-level `message`/`detail`, and non-JSON bodies.
    pub(crate) fn parse(status: u16, body: &[u8]) -> Self {
        if let Ok(Value::Object(map)) = serde_json::from_slice::<Value>(body) {
            let mut code = map.get("code").and_then(as_text);
            let mut message = None;
            match map.get("error") {
                Some(Value::Object(inner)) => {
                    message = inner
                        .get("message")
                        .or_else(|| inner.get("detail"))
                        .and_then(as_text);
                    code = inner.get("code").and_then(as_text).or(code);
                }
                Some(other) => message = as_text(other),
                None => {}
            }
            let message = message
                .or_else(|| map.get("message").and_then(as_text))
                .or_else(|| map.get("detail").and_then(as_text));
            if let Some(message) = message {
                return Self {
                    message: truncate(&message),
                    code,
                };
            }
        }
        let text = String::from_utf8_lossy(body);
        let text = text.trim();
        let message = if text.is_empty() {
            format!("empty response body (status {status})")
        } else {
            truncate(text)
        };
        Self {
            message,
            code: None,
        }
    }
}

fn as_text(value: &Value) -> Option<String> {
    match value {
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn truncate(text: &str) -> String {
    match text.char_indices().nth(MAX_MESSAGE_CHARS) {
        Some((idx, _)) => format!("{}...", &text[..idx]),
        None => text.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(body: &str) -> Envelope {
        Envelope::parse(500, body.as_bytes())
    }

    #[test]
    fn string_error() {
        let e = parse(r#"{"error":"boom"}"#);
        assert_eq!(
            e,
            Envelope {
                message: "boom".into(),
                code: None
            }
        );
    }

    #[test]
    fn object_error_with_code() {
        let e = parse(r#"{"error":{"code":"vault_sealed","message":"sealed"}}"#);
        assert_eq!(
            e,
            Envelope {
                message: "sealed".into(),
                code: Some("vault_sealed".into())
            }
        );
    }

    #[test]
    fn object_error_detail_and_numeric_code() {
        let e = parse(r#"{"error":{"code":42,"detail":"bad"}}"#);
        assert_eq!(
            e,
            Envelope {
                message: "bad".into(),
                code: Some("42".into())
            }
        );
    }

    #[test]
    fn top_level_message_and_code() {
        let e = parse(r#"{"message":"nope","code":"x"}"#);
        assert_eq!(
            e,
            Envelope {
                message: "nope".into(),
                code: Some("x".into())
            }
        );
        let e = parse(r#"{"detail":"d"}"#);
        assert_eq!(e.message, "d");
    }

    #[test]
    fn non_json_and_empty_bodies() {
        assert_eq!(parse("  plain text \n").message, "plain text");
        assert_eq!(parse("").message, "empty response body (status 500)");
        assert_eq!(parse(r#"{"unrelated":1}"#).message, r#"{"unrelated":1}"#);
        assert_eq!(parse("[1]").message, "[1]");
    }

    #[test]
    fn long_bodies_are_truncated_on_char_boundary() {
        let long = "é".repeat(2000);
        let e = parse(&long);
        assert!(e.message.ends_with("..."));
        assert_eq!(e.message.chars().count(), MAX_MESSAGE_CHARS + 3);
    }
}
