//! HTTP transport for the gough CLI: TLS via rustls only, bounded timeouts, retry with
//! exponential backoff and jitter, bearer injection and tolerant error-envelope parsing.
//! Endpoint functions are intentionally absent; they will come from a generated client.
#![forbid(unsafe_code)]

mod client;
mod error;
mod retry;
mod token;

pub use client::{ApiResponse, Client, ClientConfig, Request};
pub use error::ApiError;
pub use reqwest::Method;
pub use retry::RetryPolicy;
pub use token::{NoToken, StaticToken, TokenProvider, mask_token};
