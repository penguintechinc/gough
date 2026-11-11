"""Regression tests: no default admin credentials; forced first-login change.

Security fix (HIGH): api-manager used to seed ``admin@gough.local`` with the
hardcoded password ``admin`` (``create_all_tables``) and carried a second,
dead seeding path falling back to ``changeme123``. These tests pin the
replacement contract: one seeding path, weak operator-supplied passwords
rejected loudly at startup, a random one-time password (written once to a
0600 file, never logged) when none is supplied, and a must-change-password
gate that 403s every authenticated route except change-password and logout.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import secrets
import sqlite3
import stat
from collections.abc import AsyncIterator
from typing import Any

import bcrypt
import httpx
import pytest
from quart import Quart

KNOWN_DEFAULTS = ("admin", "changeme123", "changeme", "password", "gough")
ADMIN_ENV_VARS = (
    "ADMIN_EMAIL",
    "ADMIN_PASSWORD",
    "ADMIN_PASSWORD_FILE",
    "ADMIN_BOOTSTRAP_CREDENTIAL_PATH",
)


@pytest.fixture()
def sqlite_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, pathlib.Path]:
    """Point Config at a fresh sqlite file and isolate every ADMIN_* variable.

    ``init_db`` builds its URI from the base ``Config`` class, so the class
    attributes are patched (restored by monkeypatch). The one-time credential
    file goes under ``tmp_path`` so no test ever writes outside it.
    """
    from app.config import Config

    for var in ADMIN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Config, "DB_TYPE", "sqlite")
    monkeypatch.setattr(Config, "DB_NAME", str(tmp_path / "gough"))
    cred_path = tmp_path / "bootstrap" / "admin-initial-password"
    monkeypatch.setenv("ADMIN_BOOTSTRAP_CREDENTIAL_PATH", str(cred_path))
    return {"db": tmp_path / "gough.db", "cred": cred_path, "tmp": tmp_path}


def _users(db_file: pathlib.Path) -> list[dict[str, Any]]:
    """Return every auth_user row as a dict (raw sqlite, independent of the app)."""
    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM auth_user")]
    finally:
        con.close()


def _run_init_db() -> None:
    """Run the real startup DB path (schema + seeding) once, then release it."""
    from app.models import init_db

    db = init_db(Quart("bootstrap-test"))
    if db is not None:
        db.close()


def _read_generated_password(cred_path: pathlib.Path) -> str:
    """Parse the one-time password out of the bootstrap credential file."""
    for line in cred_path.read_text().splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise AssertionError("credential file has no password= line")


def _strong_password() -> str:
    """A unique random password that satisfies the bootstrap policy."""
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# No known/default password is ever seeded
# ---------------------------------------------------------------------------


def test_startup_never_seeds_a_known_default_password(
    sqlite_env: dict[str, pathlib.Path],
) -> None:
    """With no admin env configured, no user may verify against a known default."""
    _run_init_db()

    users = _users(sqlite_env["db"])
    assert len(users) == 1, f"expected exactly one bootstrap admin, got {len(users)}"
    checked = 0
    for user in users:
        for default in KNOWN_DEFAULTS:
            checked += 1
            assert not bcrypt.checkpw(default.encode(), user["password"].encode()), (
                f"user {user['id']} verifies against a known default password"
            )
    assert checked == len(KNOWN_DEFAULTS)


def test_source_has_no_hardcoded_admin_password() -> None:
    """Static guard: the removed literals must not come back anywhere in app/."""
    app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
    files = list(app_root.rglob("*.py"))
    assert files, "scanned zero files -- wrong root"
    default_fallback = re.compile(
        r"""(getenv|environ\.get)\(\s*["']ADMIN_PASSWORD["']\s*,"""
    )
    offenders = [
        f"{p.relative_to(app_root)}"
        for p in files
        if 'hashpw(b"admin"' in p.read_text() or default_fallback.search(p.read_text())
    ]
    assert not offenders, f"hardcoded admin password literal in: {offenders}"


# ---------------------------------------------------------------------------
# Operator-supplied password policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (None, "admin"),
        (None, "changeme"),
        (None, "changeme123"),
        (None, "password"),
        (None, "gough"),
        (None, "ChangeMe123"),
        (None, "alpha-changeme123"),
        (None, "short-pw-11"),
        (None, ""),
        ("sysadministrator@example.test", "sysadministrator"),
    ],
    ids=[
        "admin",
        "changeme",
        "changeme123",
        "password",
        "gough",
        "mixed-case",
        "changeme-substring",
        "too-short",
        "empty",
        "email-local-part",
    ],
)
def test_weak_admin_password_rejected_at_startup(
    sqlite_env: dict[str, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    email: str | None,
    password: str,
) -> None:
    """A weak ADMIN_PASSWORD aborts startup, names the variable, never echoes the value."""
    from app.security.admin_bootstrap import AdminBootstrapError

    if email:
        monkeypatch.setenv("ADMIN_EMAIL", email)
    monkeypatch.setenv("ADMIN_PASSWORD", password)

    with pytest.raises(AdminBootstrapError) as excinfo:
        _run_init_db()

    message = str(excinfo.value)
    assert "ADMIN_PASSWORD" in message
    # Short dictionary words ("admin") legitimately occur in the fixed message
    # text; the value itself must never be quoted/echoed, and distinctive
    # values must not appear at all.
    assert repr(password) not in message
    assert f'"{password}"' not in message
    if len(password) >= 10:
        assert password not in message
    assert _users(sqlite_env["db"]) == []
    assert not sqlite_env["cred"].exists()


def test_weak_admin_password_file_rejected(
    sqlite_env: dict[str, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weak value in the mounted secret file is rejected and names ADMIN_PASSWORD_FILE."""
    from app.security.admin_bootstrap import AdminBootstrapError

    secret = sqlite_env["tmp"] / "admin-password"
    secret.write_text("changeme123\n")
    monkeypatch.setenv("ADMIN_PASSWORD_FILE", str(secret))

    with pytest.raises(AdminBootstrapError, match="ADMIN_PASSWORD_FILE") as excinfo:
        _run_init_db()
    assert "changeme123" not in str(excinfo.value)
    assert _users(sqlite_env["db"]) == []


def test_admin_password_file_honored(
    sqlite_env: dict[str, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADMIN_PASSWORD_FILE seeds the admin with that secret; nothing is written to disk."""
    password = _strong_password()
    secret = sqlite_env["tmp"] / "admin-password"
    secret.write_text(password + "\n")  # mounted secrets commonly end in a newline
    monkeypatch.setenv("ADMIN_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")

    _run_init_db()

    users = _users(sqlite_env["db"])
    assert len(users) == 1
    assert users[0]["email"] == "ops@example.test"
    assert bcrypt.checkpw(password.encode(), users[0]["password"].encode())
    assert not users[0]["must_change_password"]
    assert not sqlite_env["cred"].exists()


def test_both_password_sources_is_an_error(
    sqlite_env: dict[str, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting ADMIN_PASSWORD and ADMIN_PASSWORD_FILE together is ambiguous -> fail."""
    from app.security.admin_bootstrap import AdminBootstrapError

    secret = sqlite_env["tmp"] / "admin-password"
    secret.write_text(_strong_password())
    monkeypatch.setenv("ADMIN_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("ADMIN_PASSWORD", _strong_password())

    with pytest.raises(AdminBootstrapError, match="ADMIN_PASSWORD_FILE"):
        _run_init_db()
    assert _users(sqlite_env["db"]) == []


def test_missing_password_file_is_an_error(
    sqlite_env: dict[str, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable ADMIN_PASSWORD_FILE fails loudly instead of falling back."""
    from app.security.admin_bootstrap import AdminBootstrapError

    monkeypatch.setenv("ADMIN_PASSWORD_FILE", str(sqlite_env["tmp"] / "nope"))
    with pytest.raises(AdminBootstrapError, match="ADMIN_PASSWORD_FILE"):
        _run_init_db()
    assert _users(sqlite_env["db"]) == []


# ---------------------------------------------------------------------------
# Generated one-time password
# ---------------------------------------------------------------------------


def test_generated_one_time_password_written_once_and_flagged(
    sqlite_env: dict[str, pathlib.Path],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No env -> random password, 0600 file, must-change flag, path logged, value never logged."""
    caplog.set_level(logging.DEBUG)

    _run_init_db()

    cred = sqlite_env["cred"]
    assert cred.exists(), "one-time credential file was not written"
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    password = _read_generated_password(cred)
    assert len(password) >= 24

    users = _users(sqlite_env["db"])
    assert len(users) == 1
    assert users[0]["must_change_password"]
    assert bcrypt.checkpw(password.encode(), users[0]["password"].encode())

    captured = capsys.readouterr()
    assert str(cred) in caplog.text, "log must say WHERE the password was written"
    assert password not in caplog.text
    assert password not in captured.out
    assert password not in captured.err

    # Second boot: users exist -> no rewrite, no second admin.
    before = cred.read_bytes()
    _run_init_db()
    assert cred.read_bytes() == before
    assert len(_users(sqlite_env["db"])) == 1


def test_existing_credential_file_is_never_overwritten(
    sqlite_env: dict[str, pathlib.Path],
) -> None:
    """A stale credential file blocks bootstrap loudly rather than being clobbered."""
    from app.security.admin_bootstrap import AdminBootstrapError

    cred = sqlite_env["cred"]
    cred.parent.mkdir(parents=True)
    cred.write_text("stale\n")

    with pytest.raises(AdminBootstrapError, match="ADMIN_BOOTSTRAP_CREDENTIAL_PATH"):
        _run_init_db()
    assert cred.read_text() == "stale\n"
    assert _users(sqlite_env["db"]) == []


# ---------------------------------------------------------------------------
# Real app: startup failure + forced first-login change through app.asgi_app
# ---------------------------------------------------------------------------


def _flow_config() -> type:
    """Test config mirroring the contract harness: TESTING keystore, no gRPC/limiter."""
    from app.config import Config

    class FlowConfig(Config):
        """Hermetic boot config for the forced-change flow."""

        TESTING = True
        GRPC_ENABLED = False
        RATE_LIMIT_ENABLED = False
        AUDIT_RECORDING_ENABLED = False

    return FlowConfig


@pytest.mark.asyncio
async def test_weak_password_aborts_real_app_startup(
    sqlite_env: dict[str, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bootstrap error is not swallowed by init_db's degraded-DB handling."""
    from quart.testing.app import LifespanError

    from app import create_app
    from app.security.admin_bootstrap import AdminBootstrapError

    monkeypatch.setenv("ADMIN_PASSWORD", "changeme123")
    app = await create_app(_flow_config())
    # Quart's test lifespan re-raises startup failures as LifespanError
    # carrying the original message; either way startup must not complete.
    with pytest.raises((AdminBootstrapError, LifespanError)) as excinfo:
        async with app.test_app():
            pass
    assert "ADMIN_PASSWORD" in str(excinfo.value)
    assert "changeme123" not in str(excinfo.value)
    assert _users(sqlite_env["db"]) == []


@pytest.fixture()
async def booted_app(sqlite_env: dict[str, pathlib.Path]) -> AsyncIterator[Any]:
    """Boot the real app (lifespan via test_app) on the sqlite fixture DB."""
    from app import create_app

    app = await create_app(_flow_config())
    async with app.test_app():
        yield app


@pytest.mark.asyncio
async def test_must_change_password_gate(
    booted_app: Any,
    sqlite_env: dict[str, pathlib.Path],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flagged admin: 403 on normal routes, 200 on change-password; then normal routes work.

    Driven through ``app.asgi_app`` (OIDC + cookie shim + audit layers), not
    ``app.test_client()``, so the real token validation runs.
    """
    caplog.set_level(logging.DEBUG)
    one_time = _read_generated_password(sqlite_env["cred"])
    new_password = _strong_password()
    normal_routes = ("/api/v1/auth/me", "/api/v1/audit/events")

    transport = httpx.ASGITransport(app=booted_app.asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        r = await c.post(
            "/api/v1/auth/login",
            json={"email": "admin@gough.local", "password": one_time},
        )
        assert r.status_code == 200, r.text
        assert r.json()["user"]["must_change_password"] is True
        token = r.json()["access_token"]
        c.cookies.clear()  # bearer only: keeps the CSRF cookie bridge out of play
        auth = {"Authorization": f"Bearer {token}"}

        for path in normal_routes:
            r = await c.get(path, headers=auth)
            assert r.status_code == 403, f"{path}: {r.status_code} {r.text}"
            assert r.json()["code"] == "password_change_required"

        r = await c.post(
            "/api/v1/auth/change-password",
            headers=auth,
            json={"current_password": one_time, "new_password": one_time},
        )
        assert r.status_code == 400, "re-using the one-time password must be refused"

        r = await c.post(
            "/api/v1/auth/change-password",
            headers=auth,
            json={"current_password": one_time, "new_password": new_password},
        )
        assert r.status_code == 200, r.text
        c.cookies.clear()

        for path in normal_routes:
            r = await c.get(path, headers=auth)
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"

        r = await c.post("/api/v1/auth/logout", headers=auth, json={})
        assert r.status_code == 200, r.text

    users = _users(sqlite_env["db"])
    assert not users[0]["must_change_password"]
    assert bcrypt.checkpw(new_password.encode(), users[0]["password"].encode())

    captured = capsys.readouterr()
    for secret in (one_time, new_password):
        assert secret not in caplog.text
        assert secret not in captured.out
        assert secret not in captured.err


@pytest.mark.asyncio
async def test_flagged_user_can_log_out(
    booted_app: Any, sqlite_env: dict[str, pathlib.Path]
) -> None:
    """Logout stays reachable while the must-change flag is set."""
    one_time = _read_generated_password(sqlite_env["cred"])
    transport = httpx.ASGITransport(app=booted_app.asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        r = await c.post(
            "/api/v1/auth/login",
            json={"email": "admin@gough.local", "password": one_time},
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        c.cookies.clear()
        r = await c.post(
            "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}, json={}
        )
        assert r.status_code == 200, r.text
    assert os.path.exists(sqlite_env["cred"])
