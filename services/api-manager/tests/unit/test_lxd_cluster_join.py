"""Unit tests for the LXD cluster join token endpoint and bootstrap key helpers.

LXD is mocked at the HTTP layer with respx; the agent-status DB lookup is
monkeypatched so no database is required.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta

import httpx
import jwt
import pytest
import respx
from quart import Quart

from app.api import lxd_cluster
from app.api.lxd_cluster import (
    bootstrap_token_hash,
    is_valid_hostname,
    lxd_bp,
    mask_token,
    parse_join_token_request,
)

SECRET = "test-jwt-secret"
LXD_URL = "https://lxd.test:8443"
OP_UUID = "11111111-2222-3333-4444-555555555555"
OP_PATH = f"/1.0/operations/{OP_UUID}"
ENDPOINT = "/api/v1/lxd/cluster-join-tokens"
TOKEN_META = {"serverName": "node-a", "fingerprint": "ab12", "addresses": ["10.0.0.1:8443"],
              "secret": "s3cr3t-value", "expiresAt": "2030-01-01T00:00:00Z"}
EXPECTED_TOKEN = base64.b64encode(
    json.dumps(TOKEN_META, separators=(",", ":"), sort_keys=True).encode()
).decode()


def _agent_jwt(agent_id: str = "agent-1", token_type: str = "agent_access",
               expires: timedelta = timedelta(hours=1)) -> str:
    """Build an agent access token signed with the test secret."""
    payload = {"sub": f"agent:{agent_id}", "type": token_type,
               "exp": datetime.utcnow() + expires, "iat": datetime.utcnow()}
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _auth(agent_id: str = "agent-1") -> dict[str, str]:
    """Return an Authorization header for an agent token."""
    return {"Authorization": f"Bearer {_agent_jwt(agent_id)}"}


def _operation_ok(metadata: object = None) -> dict:
    """Build a successful LXD operation wait response."""
    return {"type": "sync", "status_code": 200,
            "metadata": {"id": OP_UUID, "status_code": 200, "metadata": TOKEN_META if metadata is None else metadata}}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Quart:
    """Minimal Quart app with only the LXD blueprint and the test JWT secret."""
    monkeypatch.setenv("LXD_API_URL", LXD_URL)
    monkeypatch.setenv("LXD_VERIFY_SSL", "false")
    monkeypatch.delenv("LXD_CLIENT_CERT", raising=False)
    monkeypatch.delenv("LXD_CLIENT_KEY", raising=False)
    monkeypatch.delenv("LXD_CA_FILE", raising=False)
    monkeypatch.setattr(lxd_cluster, "_agent_status", _status_active)
    application = Quart(__name__)
    application.config["JWT_SECRET_KEY"] = SECRET
    application.register_blueprint(lxd_bp)
    return application


async def _status_active(agent_id: str) -> str:
    """Stub agent lookup returning an active agent."""
    return "active"


@pytest.fixture
def client(app: Quart):
    """Quart test client."""
    return app.test_client()


class TestSuccess:
    """Happy-path behaviour of the join token endpoint."""

    @pytest.mark.asyncio
    async def test_issues_token_from_completed_operation(self, client) -> None:
        """A 202 operation that completes returns the base64 join token."""
        with respx.mock(base_url=LXD_URL, assert_all_called=True) as mock:
            mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
            mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok())
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 200
        assert await resp.get_json() == {"token": EXPECTED_TOKEN}

    @pytest.mark.asyncio
    async def test_sends_server_name_to_lxd(self, client) -> None:
        """The member name is forwarded as LXD server_name."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
            mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok())
            await client.post(ENDPOINT, json={"member_name": "node-a.lab"}, headers=_auth())
        assert json.loads(route.calls.last.request.content) == {"server_name": "node-a.lab"}

    @pytest.mark.asyncio
    async def test_string_token_metadata_passed_through(self, client) -> None:
        """A pre-encoded string token from LXD is returned unchanged."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
            mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok("PRE-ENCODED"))
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert await resp.get_json() == {"token": "PRE-ENCODED"}


class TestUpstreamErrors:
    """LXD failure modes map to client-facing status codes."""

    @pytest.mark.asyncio
    async def test_lxd_409_member_exists(self, client) -> None:
        """LXD 409 surfaces as 409."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.post("/1.0/cluster/members").respond(409, json={"error": "exists"})
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_lxd_timeout_returns_503(self, client) -> None:
        """An LXD read timeout surfaces as 503."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.post("/1.0/cluster/members").mock(side_effect=httpx.ReadTimeout("slow"))
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_lxd_connect_error_returns_503(self, client) -> None:
        """An unreachable LXD surfaces as 503."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.post("/1.0/cluster/members").mock(side_effect=httpx.ConnectError("refused"))
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_lxd_operation_failed_returns_502(self, client) -> None:
        """A failed LXD operation (non-200 status_code) surfaces as 502."""
        failed = {"type": "sync", "status_code": 200,
                  "metadata": {"id": OP_UUID, "status_code": 400, "metadata": None}}
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
            mock.get(f"{OP_PATH}/wait").respond(200, json=failed)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_operation_path_outside_lxd_namespace_refused(self, client) -> None:
        """An operation reference outside /1.0/operations/ is refused (502), not followed."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": "/1.0/instances"})
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_lxd_not_configured_returns_503(self, client, monkeypatch) -> None:
        """A non-https LXD_API_URL fails loudly as 503 instead of calling out."""
        monkeypatch.setenv("LXD_API_URL", "http://lxd.test:8080")
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 503


class TestAuth:
    """Agent bearer token and agent-state checks."""

    @pytest.mark.asyncio
    async def test_missing_token_401(self, client) -> None:
        """No Authorization header yields 401."""
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_bad_signature_401(self, client) -> None:
        """A token signed with another key yields 401."""
        forged = jwt.encode({"sub": "agent:agent-1", "type": "agent_access",
                             "exp": datetime.utcnow() + timedelta(minutes=5)},
                            "other-secret", algorithm="HS256")
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"},
                                 headers={"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_401(self, client) -> None:
        """An expired agent token yields 401."""
        expired = _agent_jwt(expires=timedelta(seconds=-60))
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"},
                                 headers={"Authorization": f"Bearer {expired}"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_token_type_401(self, client) -> None:
        """A user access token (type access) is not accepted here."""
        user_token = _agent_jwt(token_type="access")
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"},
                                 headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_agent_401(self, client, monkeypatch) -> None:
        """A valid token for an agent that no longer exists yields 401."""

        async def _missing(agent_id: str) -> None:
            return None

        monkeypatch.setattr(lxd_cluster, "_agent_status", _missing)
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_suspended_agent_403(self, client, monkeypatch) -> None:
        """A suspended agent with a valid token is refused with 403 and never reaches LXD."""

        async def _suspended(agent_id: str) -> str:
            return "suspended"

        monkeypatch.setattr(lxd_cluster, "_agent_status", _suspended)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 403
        assert route.call_count == 0


class TestValidation:
    """Request body and hostname validation."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [
        "a" * 64,
        "-leading",
        "trailing-",
        "under_score",
        "has space",
        "",
        "bad..dots",
    ])
    async def test_bad_hostname_400(self, client, name: str) -> None:
        """Hostnames that are not RFC 1123 or exceed 63 characters are rejected with 400."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(ENDPOINT, json={"member_name": name}, headers=_auth())
        assert resp.status_code == 400
        assert route.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [
        None,
        [],
        {},
        {"member_name": 42},
        {"member_name": "node-a", "extra": True},
    ])
    async def test_bad_body_400(self, client, body) -> None:
        """Non-object, missing, wrong-typed, or unknown-field bodies are rejected with 400."""
        resp = await client.post(ENDPOINT, json=body, headers=_auth())
        assert resp.status_code == 400

    @pytest.mark.parametrize("name, ok", [
        ("node-a", True),
        ("a", True),
        ("A1-b2", True),
        ("a" * 63, True),
        ("a" * 64, False),
        ("node_a", False),
        ("-node", False),
    ])
    def test_is_valid_hostname(self, name: str, ok: bool) -> None:
        """Hostname helper enforces RFC 1123 labels and the 63-char cap."""
        assert is_valid_hostname(name) is ok

    def test_parse_request_returns_dto(self) -> None:
        """A valid body parses into the slotted request DTO."""
        req = parse_join_token_request({"member_name": "node-a"})
        assert req.member_name == "node-a"
        with pytest.raises(AttributeError):
            req.other = "x"  # type: ignore[attr-defined]


class TestSecrecy:
    """The LXD join token and bootstrap token never appear in logs."""

    @pytest.mark.asyncio
    async def test_token_not_logged(self, client, caplog: pytest.LogCaptureFixture) -> None:
        """Neither the issued token nor its secret appears in any log record; the masked form does."""
        caplog.set_level(logging.DEBUG)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
            mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok())
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 200
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert EXPECTED_TOKEN not in text
        assert TOKEN_META["secret"] not in text
        assert mask_token(EXPECTED_TOKEN) in text

    def test_mask_token_format(self) -> None:
        """Masking keeps only the last four characters."""
        assert mask_token("abcdefgh1234") == "tok_****1234"
        assert mask_token("ab") == "tok_****"


class TestBootstrapKey:
    """Bootstrap enrollment key helpers that do not touch the database."""

    def test_hash_matches_enroll_scheme(self) -> None:
        """Hash is unsalted SHA-256 hex, identical to /agents/enroll lookups."""
        import hashlib

        assert bootstrap_token_hash("x" * 40) == hashlib.sha256(("x" * 40).encode()).hexdigest()

    @pytest.mark.parametrize("token", ["short", "has space " * 4, "bad/char" * 6])
    def test_seed_rejects_malformed_token(self, token: str) -> None:
        """Malformed bootstrap tokens fail before any DB access."""
        with pytest.raises(ValueError):
            lxd_cluster.seed_bootstrap_enrollment_key(token)

    def test_seed_rejects_bad_ttl(self) -> None:
        """TTL outside 1-24 hours is refused before any DB access."""
        with pytest.raises(ValueError):
            lxd_cluster.seed_bootstrap_enrollment_key("A" * 40, ttl_hours=48)
