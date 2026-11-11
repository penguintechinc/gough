"""Seed the running api-test-server and mint test JWTs via the app's own login.

The app issues ES256 tokens from an in-memory keystore generated at boot, so
the only genuine way to mint a valid JWT is its /auth/login path. This logs in
as the app's self-seeded default admin, creates a viewer and a maintainer user
directly in the sqlite DB (POST /users is not in SCOPE_POLICY, so the
real API answers 403 even for admin), then logs each in. Tokens are written (mode 0600)
to <state>/tokens.json -- never printed. The tenant is hard-coded
"__default__" in app/auth/__init__.py; there is no tenant-creation path.
"""

import json
import os
import secrets
import sqlite3
import sys
import urllib.request
import uuid
from pathlib import Path

import bcrypt

STATE = Path(
    os.environ.get(
        "API_TEST_STATE_DIR",
        Path(os.environ.get("TMPDIR", "/tmp")) / f"gough-api-test-server-{os.getuid()}",
    )
)
BASE = f"http://127.0.0.1:{(STATE / 'port').read_text().strip()}"
ADMIN = ("admin@gough.local", "admin")  # app's own ephemeral default seed


def call(method, path, body=None, token=None):
    """Perform a JSON HTTP call; return (status, parsed-json-or-None)."""
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def login(email, password):
    """Log in and return the access token, failing loudly otherwise."""
    status, body = call("POST", "/api/v1/auth/login", {"email": email, "password": password})
    if status != 200:
        sys.exit(f"seed: login {email} -> {status} {body}")
    return body["access_token"]


def create_user(email, password, role):
    """Insert an active user with one role row (hash format matches login)."""
    pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    con = sqlite3.connect(STATE / "gough.db")
    try:
        role_id = con.execute("SELECT id FROM auth_role WHERE name=?", (role,)).fetchone()[0]
        cur = con.execute(
            "INSERT INTO auth_user (email,password,active,fs_uniquifier,full_name,login_count)"
            " VALUES (?,?,1,?,?,0)",
            (email, pw, str(uuid.uuid4()), f"Contract {role}"),
        )
        con.execute(
            "INSERT INTO auth_user_roles (user_id,role_id) VALUES (?,?)", (cur.lastrowid, role_id)
        )
        con.commit()
    finally:
        con.close()


def main():
    """Create users, mint tokens, write tokens.json."""
    tokens = {"admin": login(*ADMIN)}
    for role in ("maintainer", "viewer"):
        email = f"contract-{role}@gough.test"
        password = secrets.token_urlsafe(24)
        create_user(email, password, role)
        tokens[role] = login(email, password)
    out = STATE / "tokens.json"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f)
    print(f"seed: wrote {len(tokens)} tokens for roles {sorted(tokens)} -> {out}")


if __name__ == "__main__":
    main()
