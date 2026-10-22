---
name: gough-auth-test-fixture
description: gough api-manager REAL ES256 auth test fixture (real_auth_env) + gh-31 test facts
metadata:
  type: project
---

Testing gough `services/api-manager` auth after the penguin-aaa ES256 migration (gh-31).

**Why:** the old HS256 `decode_token`/`generate_jwt_token`/`_credential_validation`/`tenant_middleware` were deleted; test modules referencing them broke, and the old `real_auth_env` fixture minted HS256 through a partial app (no ASGI gate). Rebuilt as REAL ES256 through the full stack.

**How to apply (load-bearing facts):**
- `real_auth_env` (in `tests/api/conftest.py`) is an **async** fixture (`@pytest_asyncio.fixture`) that boots the WHOLE app via `create_app` + `app.test_app()`, seeds admin/viewer/deactivated users + a `__default__` node, and mints REAL ES256 tokens by signing with the app's own keystore key: `provider._keystore.get_signing_key()` → `jwt.encode(payload, key, algorithm="ES256", headers={"kid": kid})`. Never inject `g.current_user` / `request.scope[...]["claims"]`.
- **pytest asyncio is NOT auto**: the repo-root `pytest.ini` uses a `[tool:pytest]` header that plain pytest.ini ignores, so `asyncio_mode=auto` is inert. Async tests need `@pytest.mark.asyncio` (or `pytestmark`); async fixtures need `@pytest_asyncio.fixture`.
- **Per-test DB isolation**: `Config.DB_TYPE`/`DB_NAME` are class attrs frozen from env at import; `init_db()` reads the BASE `Config.get_db_uri()` (ignores the passed config class). So `monkeypatch.setattr(Config, "DB_TYPE", "sqlite")` + `DB_NAME`=unique-tmp-path per test. seed DB via `app.config["db"]` (get_db() needs a Quart context).
- **Real status codes proven** (see `test_auth_e2e_gh31.py`, `test_auth_regression_gh31.py`): `GET /api/v1/nodes` is a **308→200** (needs `follow_redirects=True`; scope-403 only reached after the redirect). No-token/garbage/expired/id-token(token_use)/deactivated-user → **401**. **Missing `tenant` claim → 401 at the verifier** (StaticKeyVerifier requires tenant via jwt.decode `require` + strict `Claims`), NOT the tenant-bridge 403 — that 403 branch is unreachable defense-in-depth. Storage `@require_auth` route → **403** (absent from SCOPE_POLICY, fail-closed), NOT 500 (`.ALL` fix proven by `/auth/me`=200). agents/enroll no key + nodes/discover bad bootstrap → 401 FROM HANDLER (distinct error body from edge "Missing or invalid Bearer token"/"Token verification failed").
- nodes list response envelope: `{"status":"success","data":{"nodes":[...],"total":n},"meta":{...}}`.
- **Pre-existing failures (NOT auth, don't fix under gh-31):** `validate_one_time_bootstrap_token` / `validate_service_svid` crypto in `app/security/credentials.py` — fails in `test_credentials.py`, `test_security_extended.py::TestValidateOneTimeBootstrapToken`, `test_ipxe.py` bootstrap tests. Plus 4 pre-existing collection errors (test_nats, test_grpc_server, test_openapi_export, workers pytest_plugins).
- Booting create_app per test ~3s; full app degrades gracefully (capacity predictor VAULT warning, RATE_LIMIT/AUDIT off via config attrs, GRPC_ENABLED=False).

Related: [[gough-api-manager-auth]], [[gough-commit-lint-quirks]].
