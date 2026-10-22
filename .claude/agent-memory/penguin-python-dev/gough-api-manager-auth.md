---
name: gough-api-manager-auth
description: gough services/api-manager auth is penguin-aaa ES256 (gough is its own OIDC issuer); key gotchas
metadata:
  type: project
---

`services/api-manager` (Quart, penguin-dal) authenticates via **penguin-aaa**: gough is its OWN first-party ES256 issuer.

**Why:** gh-31 — old local login minted HS256 tokens with no sub/tenant/scope that its own middleware rejected; migrated to penguin-aaa 0.2.1 so a login token works end-to-end.

**How to apply (load-bearing facts):**
- Mint tokens with `app.config["OIDC_PROVIDER"].issue_token_set(Claims(...))` (ES256). Validate locally with a `StaticKeyVerifier` built from the keystore's public PEM — NOT `OIDCRelyingParty` (no external JWKS). penguin-aaa **forbids HS256**; issuer must be an **https** URL even in dev.
- ASGI stack: `AuditMiddleware(OIDCAuthMiddleware(quart))`. `OIDCAuthMiddleware` sets `request.scope["state"]["claims"]` (a dict) on success, 401s before Quart. Handlers/before_request read claims from there.
- gough handles tenant + `g.current_user` shim in its own `before_request` (`app/middleware.py` install_security_middleware) — do NOT use penguin-aaa TenantMiddleware (no public_paths skip). Legacy handlers read `g.current_user["_jwt_payload"]` (scope/tenant/sub) — keep the shim shape.
- **Gotcha:** `OIDCAuthMiddleware` does `public_paths or set()`; an empty set is falsy and gets dropped. gough's `_AnonymousPathSet` (template-aware, in scope_policy.py) overrides `__bool__=True` for this reason. Anonymous paths live in `scope_policy.ANONYMOUS_PATHS` as (method, flask-template-path).
- `_get_user_roles` must NOT use `db.auth_role.ALL` — penguin-dal has no `.ALL`; use split queries (link rows, then role by id).
- Prod/multi-replica needs a SHARED ES256 key file mounted at `GOUGH_KEY_STORE_PATH` (FileKeyStore); dev/test use in-memory keystore per process.
- Blocking penguin-dal calls go through `run_db()` (app/db/run_db.py) off the event loop — this service is single-process/single-loop with gRPC sharing it.

See [[gough-commit-lint-quirks]] for the commit/lint workflow in this repo.
