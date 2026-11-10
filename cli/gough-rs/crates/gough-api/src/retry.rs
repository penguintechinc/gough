use std::time::{Duration, SystemTime};

use reqwest::Method;

/// Retry behaviour: exponential backoff with equal jitter, bounded attempts, `Retry-After`
/// honoured (capped). 401 and 403 are never retried; non-idempotent methods retry only when
/// the server provably did not process the request (429, 503, connect failure).
#[derive(Debug, Clone)]
pub struct RetryPolicy {
    /// Total attempts including the first (1 disables retries).
    pub max_attempts: u32,
    /// Backoff base; attempt `n` waits about `base * 2^(n-1)`.
    pub base_delay: Duration,
    /// Upper bound for a computed backoff.
    pub max_delay: Duration,
    /// Upper bound honoured for a server `Retry-After`.
    pub max_retry_after: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 4,
            base_delay: Duration::from_millis(250),
            max_delay: Duration::from_secs(8),
            max_retry_after: Duration::from_secs(30),
        }
    }
}

impl RetryPolicy {
    /// Delay before retry number `retry` (1-based), preferring the server's `Retry-After`.
    pub fn delay(&self, retry: u32, retry_after: Option<Duration>) -> Duration {
        if let Some(wait) = retry_after {
            return wait.min(self.max_retry_after);
        }
        let shift = retry.saturating_sub(1).min(20);
        let exp = self
            .base_delay
            .saturating_mul(1u32 << shift)
            .min(self.max_delay);
        let half = exp / 2;
        let jitter_ms = u64::try_from(half.as_millis()).unwrap_or(u64::MAX);
        half + Duration::from_millis(fastrand::u64(0..=jitter_ms))
    }

    /// Whether a response status may be retried for this method.
    pub fn status_retryable(status: u16, method: &Method) -> bool {
        match status {
            401 | 403 => false,
            429 | 503 => true,
            500 | 502 | 504 => is_idempotent(method),
            _ => false,
        }
    }
}

/// RFC 9110 idempotent methods.
pub(crate) fn is_idempotent(method: &Method) -> bool {
    matches!(
        *method,
        Method::GET | Method::HEAD | Method::PUT | Method::DELETE | Method::OPTIONS | Method::TRACE
    )
}

/// Parses `Retry-After` as delta-seconds or an HTTP date (relative to `now`).
pub(crate) fn parse_retry_after(value: &str, now: SystemTime) -> Option<Duration> {
    let value = value.trim();
    if let Ok(secs) = value.parse::<u64>() {
        return Some(Duration::from_secs(secs));
    }
    let at = httpdate::parse_http_date(value).ok()?;
    Some(at.duration_since(now).unwrap_or(Duration::ZERO))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_and_stays_in_jitter_band() {
        let p = RetryPolicy::default();
        for retry in 1..=5u32 {
            let exp = p
                .base_delay
                .saturating_mul(1 << (retry - 1))
                .min(p.max_delay);
            for _ in 0..50 {
                let d = p.delay(retry, None);
                assert!(d >= exp / 2 && d <= exp, "retry {retry}: {d:?} vs {exp:?}");
            }
        }
        assert!(p.delay(40, None) <= p.max_delay);
    }

    #[test]
    fn retry_after_is_honoured_and_capped() {
        let p = RetryPolicy::default();
        assert_eq!(
            p.delay(1, Some(Duration::from_secs(3))),
            Duration::from_secs(3)
        );
        assert_eq!(
            p.delay(1, Some(Duration::from_secs(900))),
            p.max_retry_after
        );
    }

    #[test]
    fn status_matrix() {
        for m in [Method::GET, Method::POST] {
            assert!(!RetryPolicy::status_retryable(401, &m));
            assert!(!RetryPolicy::status_retryable(403, &m));
            assert!(!RetryPolicy::status_retryable(404, &m));
            assert!(!RetryPolicy::status_retryable(422, &m));
            assert!(RetryPolicy::status_retryable(429, &m));
            assert!(RetryPolicy::status_retryable(503, &m));
        }
        assert!(RetryPolicy::status_retryable(500, &Method::GET));
        assert!(RetryPolicy::status_retryable(502, &Method::DELETE));
        assert!(!RetryPolicy::status_retryable(500, &Method::POST));
        assert!(!RetryPolicy::status_retryable(504, &Method::PATCH));
    }

    #[test]
    fn parses_retry_after_forms() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000_000);
        assert_eq!(parse_retry_after(" 7 ", now), Some(Duration::from_secs(7)));
        let future = httpdate::fmt_http_date(now + Duration::from_secs(90));
        assert_eq!(
            parse_retry_after(&future, now),
            Some(Duration::from_secs(90))
        );
        let past = httpdate::fmt_http_date(now - Duration::from_secs(90));
        assert_eq!(parse_retry_after(&past, now), Some(Duration::ZERO));
        assert_eq!(parse_retry_after("soon", now), None);
    }
}
