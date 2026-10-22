# penguin-python-dev memory

- [gough api-manager auth](gough-api-manager-auth.md) — penguin-aaa ES256 self-issuer; StaticKeyVerifier local; public_paths empty-set gotcha; no penguin-dal `.ALL`
- [gough commit/lint quirks](gough-commit-lint-quirks.md) — `PRE_COMMIT_ALLOW_NO_CONFIG=1`; non-blocking lint; penguin-dal `== False` DSL; `--no-upgrade` req compile
- [gough auth test fixture](gough-auth-test-fixture.md) — REAL ES256 `real_auth_env` (full create_app boot); asyncio not-auto; per-test DB via Config monkeypatch; real status codes (no-tenant→401); pre-existing bootstrap-crypto fails
