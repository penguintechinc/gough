use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use reqwest::header::RETRY_AFTER;
use reqwest::{Certificate, Method, redirect};
use secrecy::ExposeSecret;
use serde::de::DeserializeOwned;
use url::Url;

use crate::error::Envelope;
use crate::retry::{is_idempotent, parse_retry_after};
use crate::{ApiError, RetryPolicy, TokenProvider, mask_token};

/// Transport settings for one cluster: URL, timeouts, retry policy and optional extra CA.
#[derive(Debug, Clone)]
pub struct ClientConfig {
    base_url: Url,
    /// Total time allowed for one request attempt.
    pub timeout: Duration,
    /// Time allowed to establish the connection.
    pub connect_timeout: Duration,
    /// Retry/backoff behaviour.
    pub retry: RetryPolicy,
    /// Extra PEM CA bundle trusted in addition to the system roots (`--ca-file`).
    pub ca_file: Option<PathBuf>,
    /// `User-Agent` header value.
    pub user_agent: String,
}

impl ClientConfig {
    /// Validates the cluster URL and returns defaults (30s timeout, 4 attempts).
    /// TLS is mandatory: plain `http` is accepted only for loopback hosts.
    pub fn new(base_url: &str) -> Result<Self, ApiError> {
        let url =
            Url::parse(base_url).map_err(|e| ApiError::InvalidUrl(format!("{base_url}: {e}")))?;
        let host = url
            .host_str()
            .ok_or_else(|| ApiError::InvalidUrl(format!("{base_url}: missing host")))?;
        match url.scheme() {
            "https" => {}
            "http" if is_loopback(host) => {}
            "http" => {
                return Err(ApiError::InvalidUrl(format!(
                    "{base_url}: plain http is only allowed for loopback hosts; use https"
                )));
            }
            other => {
                return Err(ApiError::InvalidUrl(format!(
                    "{base_url}: unsupported scheme {other}"
                )));
            }
        }
        if url.query().is_some() || url.fragment().is_some() {
            return Err(ApiError::InvalidUrl(format!(
                "{base_url}: must not contain query or fragment"
            )));
        }
        Ok(Self {
            base_url: url,
            timeout: Duration::from_secs(30),
            connect_timeout: Duration::from_secs(10),
            retry: RetryPolicy::default(),
            ca_file: None,
            user_agent: format!("gough-cli/{}", env!("CARGO_PKG_VERSION")),
        })
    }

    /// The validated base URL.
    pub fn base_url(&self) -> &Url {
        &self.base_url
    }
}

fn is_loopback(host: &str) -> bool {
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    bare.eq_ignore_ascii_case("localhost")
        || bare
            .parse::<std::net::IpAddr>()
            .is_ok_and(|ip| ip.is_loopback())
}

/// One API call: method, path under the base URL, query pairs and optional JSON body.
#[derive(Debug, Clone)]
pub struct Request {
    method: Method,
    path: String,
    query: Vec<(String, String)>,
    body: Option<serde_json::Value>,
}

impl Request {
    /// Starts a request; `path` must begin with `/`.
    pub fn new(method: Method, path: impl Into<String>) -> Self {
        Self {
            method,
            path: path.into(),
            query: Vec::new(),
            body: None,
        }
    }

    /// Appends a query parameter.
    pub fn query(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.query.push((key.into(), value.into()));
        self
    }

    /// Sets the JSON request body.
    pub fn json(mut self, body: serde_json::Value) -> Self {
        self.body = Some(body);
        self
    }
}

/// A successful (2xx) response with the raw body.
#[derive(Debug, Clone)]
pub struct ApiResponse {
    /// HTTP status code (200, 202, ...).
    pub status: u16,
    /// Raw response body.
    pub body: Vec<u8>,
}

impl ApiResponse {
    /// Decodes the body as JSON into `T`.
    pub fn json<T: DeserializeOwned>(&self) -> Result<T, ApiError> {
        serde_json::from_slice(&self.body).map_err(|e| ApiError::Decode(e.to_string()))
    }
}

/// The transport client; cheap to clone-share behind an `Arc` and safe across tasks.
pub struct Client {
    http: reqwest::Client,
    base: String,
    retry: RetryPolicy,
    tokens: Arc<dyn TokenProvider>,
}

impl std::fmt::Debug for Client {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Client")
            .field("base", &self.base)
            .finish_non_exhaustive()
    }
}

impl Client {
    /// Builds the rustls-backed HTTP client; an unreadable or invalid `ca_file` is a hard error.
    pub fn new(config: ClientConfig, tokens: Arc<dyn TokenProvider>) -> Result<Self, ApiError> {
        let mut builder = reqwest::Client::builder()
            .timeout(config.timeout)
            .connect_timeout(config.connect_timeout)
            .user_agent(config.user_agent.clone())
            .redirect(redirect::Policy::none());
        if let Some(path) = &config.ca_file {
            for cert in load_ca_bundle(path)? {
                builder = builder.add_root_certificate(cert);
            }
        }
        let http = builder
            .build()
            .map_err(|e| ApiError::ClientBuild(e.to_string()))?;
        Ok(Self {
            http,
            base: config.base_url.as_str().trim_end_matches('/').to_owned(),
            retry: config.retry,
            tokens,
        })
    }

    /// Sends the request with bearer injection and retries; non-2xx becomes [`ApiError::Status`].
    pub async fn send(&self, request: Request) -> Result<ApiResponse, ApiError> {
        if !request.path.starts_with('/') {
            return Err(ApiError::InvalidUrl(format!(
                "path must start with '/': {}",
                request.path
            )));
        }
        let url = format!("{}{}", self.base, request.path);
        let attempts = self.retry.max_attempts.max(1);
        let mut attempt = 1;
        loop {
            let token = self.tokens.bearer_token().await?;
            let mut builder = self.http.request(request.method.clone(), &url);
            if !request.query.is_empty() {
                builder = builder.query(&request.query);
            }
            if let Some(body) = &request.body {
                builder = builder.json(body);
            }
            let auth = match &token {
                Some(secret) => {
                    builder = builder.bearer_auth(secret.expose_secret());
                    mask_token(secret.expose_secret())
                }
                None => "none".to_owned(),
            };
            tracing::debug!(method = %request.method, %url, %auth, attempt, "request");
            let outcome = match builder.send().await {
                Ok(resp) => self.finish(resp, &request.method).await,
                Err(err) => Err(Attempt::Transport(err)),
            };
            let (error, retry_after) = match outcome {
                Ok(done) => return Ok(done),
                Err(failure) => failure.into_error(&request.method),
            };
            let Retryable::Yes = error.retryable else {
                return Err(error.error);
            };
            if attempt >= attempts {
                return Err(error.error);
            }
            let wait = self.retry.delay(attempt, retry_after);
            tracing::debug!(attempt, wait_ms = wait.as_millis() as u64, "retrying");
            tokio::time::sleep(wait).await;
            attempt += 1;
        }
    }

    async fn finish(
        &self,
        resp: reqwest::Response,
        method: &Method,
    ) -> Result<ApiResponse, Attempt> {
        let status = resp.status();
        let retry_after = resp
            .headers()
            .get(RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| parse_retry_after(v, SystemTime::now()));
        let body = resp.bytes().await.map_err(Attempt::Transport)?.to_vec();
        tracing::debug!(status = status.as_u16(), "response");
        if status.is_success() {
            return Ok(ApiResponse {
                status: status.as_u16(),
                body,
            });
        }
        let envelope = Envelope::parse(status.as_u16(), &body);
        Err(Attempt::Status {
            status: status.as_u16(),
            envelope,
            retry_after,
            retryable: RetryPolicy::status_retryable(status.as_u16(), method),
        })
    }
}

enum Retryable {
    Yes,
    No,
}

struct Classified {
    error: ApiError,
    retryable: Retryable,
}

enum Attempt {
    Transport(reqwest::Error),
    Status {
        status: u16,
        envelope: Envelope,
        retry_after: Option<Duration>,
        retryable: bool,
    },
}

impl Attempt {
    fn into_error(self, method: &Method) -> (Classified, Option<Duration>) {
        match self {
            Attempt::Status {
                status,
                envelope,
                retry_after,
                retryable,
            } => {
                let message = if (300..400).contains(&status) {
                    format!(
                        "unexpected redirect; check the cluster URL ({})",
                        envelope.message
                    )
                } else {
                    envelope.message
                };
                let retryable = if retryable {
                    Retryable::Yes
                } else {
                    Retryable::No
                };
                (
                    Classified {
                        error: ApiError::Status {
                            status,
                            message,
                            code: envelope.code,
                        },
                        retryable,
                    },
                    retry_after,
                )
            }
            Attempt::Transport(err) => {
                let safe = err.is_connect() || is_idempotent(method);
                let error = if err.is_timeout() {
                    ApiError::Timeout
                } else {
                    ApiError::Network(describe(err))
                };
                let retryable = if safe { Retryable::Yes } else { Retryable::No };
                (Classified { error, retryable }, None)
            }
        }
    }
}

fn describe(err: reqwest::Error) -> String {
    let err = err.without_url();
    let mut text = err.to_string();
    let mut source = std::error::Error::source(&err);
    while let Some(inner) = source {
        text.push_str(": ");
        text.push_str(&inner.to_string());
        source = inner.source();
    }
    text
}

fn load_ca_bundle(path: &PathBuf) -> Result<Vec<Certificate>, ApiError> {
    let shown = path.display().to_string();
    let pem = std::fs::read(path).map_err(|e| ApiError::CaFileRead {
        path: shown.clone(),
        reason: e.to_string(),
    })?;
    let certs = Certificate::from_pem_bundle(&pem).map_err(|e| ApiError::CaFileInvalid {
        path: shown.clone(),
        reason: e.to_string(),
    })?;
    if certs.is_empty() {
        return Err(ApiError::CaFileInvalid {
            path: shown,
            reason: "no certificates found".into(),
        });
    }
    Ok(certs)
}
