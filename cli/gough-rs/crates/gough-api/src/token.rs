use async_trait::async_trait;
use secrecy::{ExposeSecret, SecretString};

use crate::ApiError;

/// Supplies the bearer token for each request attempt; implemented by `gough-auth`.
/// It is consulted per attempt so a refreshed token is picked up without rebuilding the client.
#[async_trait]
pub trait TokenProvider: Send + Sync {
    /// Returns the current access token, or `None` for anonymous calls.
    async fn bearer_token(&self) -> Result<Option<SecretString>, ApiError>;
}

/// Provider for anonymous endpoints; never attaches an `Authorization` header.
#[derive(Debug, Default, Clone, Copy)]
pub struct NoToken;

#[async_trait]
impl TokenProvider for NoToken {
    async fn bearer_token(&self) -> Result<Option<SecretString>, ApiError> {
        Ok(None)
    }
}

/// Provider holding one fixed token, e.g. from `GOUGH_TOKEN` or stdin in CI.
#[derive(Debug, Clone)]
pub struct StaticToken(SecretString);

impl StaticToken {
    /// Wraps an already-obtained token; the value is zeroized on drop and never printed.
    pub fn new(token: SecretString) -> Self {
        Self(token)
    }
}

#[async_trait]
impl TokenProvider for StaticToken {
    async fn bearer_token(&self) -> Result<Option<SecretString>, ApiError> {
        Ok(Some(SecretString::from(self.0.expose_secret().to_owned())))
    }
}

/// Masks a token for logs as `tok_****1234`; short values are fully masked.
pub fn mask_token(token: &str) -> String {
    let chars: Vec<char> = token.chars().collect();
    if chars.len() <= 8 {
        return "tok_****".to_owned();
    }
    let tail: String = chars[chars.len() - 4..].iter().collect();
    format!("tok_****{tail}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks_long_and_short_tokens() {
        assert_eq!(mask_token("abcdefghijkl1234"), "tok_****1234");
        assert_eq!(mask_token("short"), "tok_****");
        assert_eq!(mask_token(""), "tok_****");
    }

    #[tokio::test]
    async fn providers_yield_expected_tokens() {
        assert!(NoToken.bearer_token().await.unwrap().is_none());
        let p = StaticToken::new(SecretString::from("t0k"));
        let got = p.bearer_token().await.unwrap().unwrap();
        assert_eq!(got.expose_secret(), "t0k");
        assert!(!format!("{p:?}").contains("t0k"));
    }
}
