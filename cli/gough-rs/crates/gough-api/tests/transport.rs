#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! Transport behaviour against a local mock server: retries, backoff, auth, envelopes, CA.

use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use gough_api::{
    ApiError, Client, ClientConfig, Method, NoToken, Request, RetryPolicy, StaticToken,
    TokenProvider,
};
use secrecy::SecretString;
use serde_json::{Value, json};
use wiremock::matchers::{body_json, header, header_exists, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn fast_retry() -> RetryPolicy {
    RetryPolicy {
        max_attempts: 3,
        base_delay: Duration::from_millis(2),
        max_delay: Duration::from_millis(10),
        max_retry_after: Duration::from_secs(5),
    }
}

fn client(server: &MockServer, tokens: Arc<dyn TokenProvider>) -> Client {
    let mut cfg = ClientConfig::new(&server.uri()).unwrap();
    cfg.retry = fast_retry();
    Client::new(cfg, tokens).unwrap()
}

async fn requests(server: &MockServer) -> usize {
    server.received_requests().await.unwrap().len()
}

#[tokio::test]
async fn injects_bearer_query_and_json_body() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/v1/x"))
        .and(query_param("a", "b"))
        .and(header("authorization", "Bearer s3cret"))
        .and(body_json(json!({"k": 1})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
        .expect(1)
        .mount(&server)
        .await;
    let c = client(
        &server,
        Arc::new(StaticToken::new(SecretString::from("s3cret"))),
    );
    let req = Request::new(Method::POST, "/api/v1/x")
        .query("a", "b")
        .json(json!({"k": 1}));
    let resp = c.send(req).await.unwrap();
    assert_eq!(resp.status, 200);
    assert_eq!(resp.json::<Value>().unwrap(), json!({"ok": true}));
    assert!(resp.json::<Vec<u8>>().is_err());
}

#[tokio::test]
async fn anonymous_sends_no_authorization_header() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(202))
        .mount(&server)
        .await;
    let c = client(&server, Arc::new(NoToken));
    let resp = c.send(Request::new(Method::GET, "/v")).await.unwrap();
    assert_eq!(resp.status, 202);
    let got = server.received_requests().await.unwrap();
    assert!(!got[0].headers.contains_key("authorization"));
}

#[tokio::test]
async fn retries_503_then_succeeds() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(503))
        .up_to_n_times(2)
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;
    let c = client(&server, Arc::new(NoToken));
    assert_eq!(
        c.send(Request::new(Method::GET, "/r"))
            .await
            .unwrap()
            .status,
        200
    );
    assert_eq!(requests(&server).await, 3);
}

#[tokio::test]
async fn gives_up_after_max_attempts_on_429() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(
            ResponseTemplate::new(429)
                .set_body_json(json!({"error": {"code": "slow", "message": "later"}})),
        )
        .mount(&server)
        .await;
    let c = client(&server, Arc::new(NoToken));
    let err = c.send(Request::new(Method::GET, "/r")).await.unwrap_err();
    match err {
        ApiError::Status {
            status,
            message,
            code,
        } => {
            assert_eq!(
                (status, message.as_str(), code.as_deref()),
                (429, "later", Some("slow"))
            );
        }
        other => panic!("unexpected {other:?}"),
    }
    assert_eq!(requests(&server).await, 3);
}

#[tokio::test]
async fn honours_retry_after_seconds() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(429).insert_header("Retry-After", "1"))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;
    let c = client(&server, Arc::new(NoToken));
    let started = Instant::now();
    c.send(Request::new(Method::GET, "/r")).await.unwrap();
    assert!(
        started.elapsed() >= Duration::from_millis(950),
        "{:?}",
        started.elapsed()
    );
}

#[tokio::test]
async fn never_retries_401_or_403() {
    for status in [401u16, 403] {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(status).set_body_json(json!({"error": "denied"})))
            .mount(&server)
            .await;
        let c = client(&server, Arc::new(NoToken));
        let err = c.send(Request::new(Method::GET, "/r")).await.unwrap_err();
        assert!(matches!(err, ApiError::Status { status: s, .. } if s == status));
        assert_eq!(
            requests(&server).await,
            1,
            "status {status} must not be retried"
        );
    }
}

#[tokio::test]
async fn post_500_is_not_retried_but_get_500_is() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&server)
        .await;
    let c = client(&server, Arc::new(NoToken));
    assert!(c.send(Request::new(Method::POST, "/p")).await.is_err());
    assert_eq!(requests(&server).await, 1);
    assert!(c.send(Request::new(Method::GET, "/g")).await.is_err());
    assert_eq!(requests(&server).await, 4);
}

#[tokio::test]
async fn string_object_and_plain_error_bodies_are_parsed() {
    let server = MockServer::start().await;
    Mock::given(path("/s"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({"error": "missing"})))
        .mount(&server)
        .await;
    Mock::given(path("/t"))
        .respond_with(ResponseTemplate::new(422).set_body_string("not json"))
        .mount(&server)
        .await;
    Mock::given(path("/u"))
        .respond_with(ResponseTemplate::new(302))
        .mount(&server)
        .await;
    let c = client(&server, Arc::new(NoToken));
    let e = c.send(Request::new(Method::GET, "/s")).await.unwrap_err();
    assert_eq!(e.to_string(), "HTTP 404: missing");
    let e = c.send(Request::new(Method::GET, "/t")).await.unwrap_err();
    assert_eq!(e.to_string(), "HTTP 422: not json");
    let e = c.send(Request::new(Method::GET, "/u")).await.unwrap_err();
    assert!(e.to_string().contains("unexpected redirect"), "{e}");
}

#[tokio::test]
async fn connect_failure_is_network_error_after_retries() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    let mut cfg = ClientConfig::new(&format!("http://{addr}")).unwrap();
    cfg.retry = fast_retry();
    let c = Client::new(cfg, Arc::new(NoToken)).unwrap();
    let err = c.send(Request::new(Method::POST, "/x")).await.unwrap_err();
    assert!(matches!(err, ApiError::Network(_)), "{err:?}");
}

#[tokio::test]
async fn timeout_is_reported_and_retried_only_when_idempotent() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200).set_delay(Duration::from_millis(400)))
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_delay(Duration::from_millis(400)))
        .mount(&server)
        .await;
    let mut cfg = ClientConfig::new(&server.uri()).unwrap();
    cfg.retry = fast_retry();
    cfg.timeout = Duration::from_millis(100);
    let c = Client::new(cfg, Arc::new(NoToken)).unwrap();
    assert!(matches!(
        c.send(Request::new(Method::POST, "/p")).await,
        Err(ApiError::Timeout)
    ));
    assert_eq!(requests(&server).await, 1);
    assert!(matches!(
        c.send(Request::new(Method::GET, "/g")).await,
        Err(ApiError::Timeout)
    ));
    assert_eq!(requests(&server).await, 4);
}

struct FailingTokens;

#[async_trait]
impl TokenProvider for FailingTokens {
    async fn bearer_token(&self) -> Result<Option<SecretString>, ApiError> {
        Err(ApiError::Token("keyring locked".into()))
    }
}

#[tokio::test]
async fn token_provider_failure_aborts_before_any_request() {
    let server = MockServer::start().await;
    let c = client(&server, Arc::new(FailingTokens));
    let err = c.send(Request::new(Method::GET, "/r")).await.unwrap_err();
    assert_eq!(err.to_string(), "cannot obtain credentials: keyring locked");
    assert_eq!(requests(&server).await, 0);
}

#[tokio::test]
async fn path_must_start_with_slash() {
    let server = MockServer::start().await;
    let c = client(&server, Arc::new(NoToken));
    let err = c.send(Request::new(Method::GET, "nope")).await.unwrap_err();
    assert!(matches!(err, ApiError::InvalidUrl(_)));
}

#[tokio::test]
async fn base_url_with_prefix_is_preserved() {
    let server = MockServer::start().await;
    Mock::given(path("/prefix/api/v1/ping"))
        .and(header_exists("user-agent"))
        .respond_with(ResponseTemplate::new(200))
        .expect(1)
        .mount(&server)
        .await;
    let mut cfg = ClientConfig::new(&format!("{}/prefix/", server.uri())).unwrap();
    cfg.retry = fast_retry();
    let c = Client::new(cfg, Arc::new(NoToken)).unwrap();
    c.send(Request::new(Method::GET, "/api/v1/ping"))
        .await
        .unwrap();
    assert!(format!("{c:?}").contains("prefix"));
}

#[test]
fn url_validation() {
    assert!(ClientConfig::new("https://gough.example.com").is_ok());
    assert!(ClientConfig::new("http://127.0.0.1:8080").is_ok());
    assert!(ClientConfig::new("http://localhost").is_ok());
    assert!(ClientConfig::new("http://[::1]:8080").is_ok());
    for bad in [
        "http://gough.example.com",
        "ftp://x",
        "not a url",
        "https://x/?q=1",
        "https://x/#f",
        "unix:/tmp/sock",
    ] {
        assert!(
            matches!(ClientConfig::new(bad), Err(ApiError::InvalidUrl(_))),
            "{bad}"
        );
    }
    assert_eq!(
        ClientConfig::new("https://a/b")
            .unwrap()
            .base_url()
            .as_str(),
        "https://a/b"
    );
}

#[test]
fn ca_file_failures_are_loud() {
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = ClientConfig::new("https://gough.example.com").unwrap();

    cfg.ca_file = Some(dir.path().join("missing.pem"));
    let err = Client::new(cfg.clone(), Arc::new(NoToken)).unwrap_err();
    assert!(matches!(err, ApiError::CaFileRead { .. }), "{err:?}");

    let junk = dir.path().join("junk.pem");
    std::fs::write(&junk, "hello").unwrap();
    cfg.ca_file = Some(junk);
    let err = Client::new(cfg.clone(), Arc::new(NoToken)).unwrap_err();
    assert!(matches!(err, ApiError::CaFileInvalid { .. }), "{err:?}");

    let good = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/test-ca.pem");
    cfg.ca_file = Some(good.into());
    assert!(Client::new(cfg, Arc::new(NoToken)).is_ok());
}
