"""Unit tests for the LXD cluster join token endpoint, node-join authorization, and bootstrap seeding.

LXD is mocked at the HTTP layer with respx. The database is replaced by an in-memory
FakeStore that patches the module-level ``_db_*`` helpers, so no database is required.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx
import jwt
import pytest
import respx
from quart import Quart

from app.api import lxd_cluster
from app.api.lxd_cluster import (
    PURPOSE_BOOTSTRAP,
    PURPOSE_NODE_JOIN,
    AgentRecord,
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
MEMBER_URL = "/1.0/cluster/members/node-a"
TOKEN_META = {"serverName": "node-a", "fingerprint": "ab12", "addresses": ["10.0.0.1:8443"],
              "secret": "s3cr3t-value", "expiresAt": "2030-01-01T00:00:00Z"}
EXPECTED_TOKEN = base64.b64encode(
    json.dumps(TOKEN_META, separators=(",", ":"), sort_keys=True).encode()
).decode()
BOOTSTRAP = "B" * 40


@dataclass(slots=True)
class FakeStore:
    """In-memory stand-in for the access_agents, enrollment_keys, issuance and tombstone tables."""

    agents: dict[str, AgentRecord] = field(default_factory=dict)
    key_purpose: dict[str, Optional[str]] = field(default_factory=dict)
    issuances: list[dict] = field(default_factory=list)
    seeds: set[str] = field(default_factory=set)
    seeded_key_inserts: int = 0

    def add_agent(self, agent_id: str, hostname: str, purpose: Optional[str],
                  status: str = "active") -> None:
        """Register an agent whose enrollment key has the given purpose (None = not join-eligible)."""
        key_hash = hashlib.sha256(f"key-{agent_id}".encode()).hexdigest()
        self.agents[agent_id] = AgentRecord(agent_id, status, hostname, key_hash)
        self.key_purpose[key_hash] = purpose


def _load_agent(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every DB helper the handlers use to read or write this store."""
    monkeypatch.setattr(lxd_cluster, "_db_load_agent", lambda a: store.agents.get(a))
    monkeypatch.setattr(lxd_cluster, "_db_key_purpose", lambda h: store.key_purpose.get(h))

    def live(agent_id: str, member: str, now: datetime) -> bool:
        return any(
            i["status"] == 0 and i["expires_at"] > now
            and (i["member_name"] == member or i["agent_id"] == agent_id)
            for i in store.issuances
        )

    def record(agent_id: str, member: str, now: datetime) -> int:
        store.issuances.append({"id": len(store.issuances) + 1, "agent_id": agent_id,
                                "member_name": member, "status": 0,
                                "expires_at": now + lxd_cluster._ISSUANCE_TTL})
        return len(store.issuances)

    def mark_failed(issuance_id: int, now: datetime) -> None:
        store.issuances[issuance_id - 1]["status"] = 1
        store.issuances[issuance_id - 1]["expires_at"] = now

    def seeded(token_hash: str) -> bool:
        return token_hash in store.seeds

    def seed(token_hash: str, key_hash: str, owner: int, expires: datetime) -> None:
        store.key_purpose[key_hash] = PURPOSE_BOOTSTRAP
        store.seeds.add(token_hash)
        store.seeded_key_inserts += 1

    monkeypatch.setattr(lxd_cluster, "_db_has_live_issuance", live)
    monkeypatch.setattr(lxd_cluster, "_db_record_issuance", record)
    monkeypatch.setattr(lxd_cluster, "_db_mark_issuance_failed", mark_failed)
    monkeypatch.setattr(lxd_cluster, "_db_bootstrap_seeded", seeded)
    monkeypatch.setattr(lxd_cluster, "_db_first_active_user_id", lambda: 1)
    monkeypatch.setattr(lxd_cluster, "_db_seed_bootstrap", seed)


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
            "metadata": {"id": OP_UUID, "status_code": 200,
                         "metadata": TOKEN_META if metadata is None else metadata}}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """Fresh in-memory store with one active node-join agent named node-a."""
    fake = FakeStore()
    fake.add_agent("agent-1", "node-a", PURPOSE_NODE_JOIN)
    _load_agent(fake, monkeypatch)
    return fake


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> Quart:
    """Minimal Quart app with only the LXD blueprint and the test JWT secret."""
    monkeypatch.setenv("LXD_API_URL", LXD_URL)
    monkeypatch.setenv("LXD_VERIFY_SSL", "false")
    monkeypatch.delenv("LXD_CLIENT_CERT", raising=False)
    monkeypatch.delenv("LXD_CLIENT_KEY", raising=False)
    monkeypatch.delenv("LXD_CA_FILE", raising=False)
    application = Quart(__name__)
    application.config["JWT_SECRET_KEY"] = SECRET
    application.register_blueprint(lxd_bp)
    return application


@pytest.fixture
def client(app: Quart):
    """Quart test client."""
    return app.test_client()


def _lxd_success_routes(mock: respx.MockRouter, member_absent: bool = True):
    """Register the LXD mock: member absent (404), POST accepted, operation completes."""
    if member_absent:
        mock.get(MEMBER_URL).respond(404, json={"error": "not found"})
    post = mock.post("/1.0/cluster/members").respond(
        202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
    mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok())
    return post


class TestSuccess:
    """Happy-path behaviour of the join token endpoint."""

    @pytest.mark.asyncio
    async def test_issues_token_from_completed_operation(self, client, store: FakeStore) -> None:
        """A completed operation returns the base64 join token and records one issuance."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 200
        assert await resp.get_json() == {"token": EXPECTED_TOKEN}
        assert len(store.issuances) == 1
        assert store.issuances[0]["status"] == 0

    @pytest.mark.asyncio
    async def test_sends_server_name_to_lxd(self, client, store: FakeStore) -> None:
        """The member name is forwarded as LXD server_name."""
        store.agents["agent-1"] = AgentRecord("agent-1", "active", "node-a.lab",
                                              store.agents["agent-1"].enrollment_key_hash)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get("/1.0/cluster/members/node-a.lab").respond(404)
            route = mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
            mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok())
            await client.post(ENDPOINT, json={"member_name": "node-a.lab"}, headers=_auth())
        assert json.loads(route.calls.last.request.content) == {"server_name": "node-a.lab"}

    @pytest.mark.asyncio
    async def test_bootstrap_purpose_agent_allowed(self, client, store: FakeStore) -> None:
        """An agent enrolled with a bootstrap key is join-eligible."""
        store.add_agent("agent-boot", "node-a", PURPOSE_BOOTSTRAP)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"},
                                     headers=_auth("agent-boot"))
        assert resp.status_code == 200


class TestUpstreamErrors:
    """LXD failure modes map to client-facing status codes."""

    @pytest.mark.asyncio
    async def test_member_exists_precheck_409(self, client, store: FakeStore) -> None:
        """Member already in the cluster (GET 200) refuses the request before minting."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(200, json={"name": "node-a"})
            post = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 409
        assert post.call_count == 0

    @pytest.mark.asyncio
    async def test_lxd_post_409_member_exists(self, client) -> None:
        """LXD 409 on the POST itself surfaces as 409."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").respond(409, json={"error": "exists"})
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_lxd_timeout_returns_503(self, client) -> None:
        """An LXD read timeout surfaces as 503."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").mock(side_effect=httpx.ReadTimeout("slow"))
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_lxd_connect_error_returns_503(self, client) -> None:
        """An unreachable LXD surfaces as 503."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).mock(side_effect=httpx.ConnectError("refused"))
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_lxd_operation_failed_returns_502(self, client) -> None:
        """A failed LXD operation (non-200 status_code) surfaces as 502."""
        failed = {"type": "sync", "status_code": 200,
                  "metadata": {"id": OP_UUID, "status_code": 400, "metadata": None}}
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH})
            mock.get(f"{OP_PATH}/wait").respond(200, json=failed)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_operation_path_outside_lxd_namespace_refused(self, client) -> None:
        """An operation reference outside /1.0/operations/ is refused (502), not followed."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
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

    @pytest.mark.asyncio
    async def test_failed_grant_is_expired_so_retry_allowed(self, client, store: FakeStore) -> None:
        """A failed LXD call marks the issuance failed; a retry is not blocked by it."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").respond(500)
            first = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert first.status_code == 502
        assert store.issuances[0]["status"] == 1
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            second = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert second.status_code == 200


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
    async def test_unknown_agent_401(self, client) -> None:
        """A valid token for an agent that no longer exists yields 401."""
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth("ghost"))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_suspended_agent_403(self, client, store: FakeStore) -> None:
        """A suspended agent with a valid token is refused with 403 and never reaches LXD."""
        store.agents["agent-1"] = AgentRecord("agent-1", "suspended", "node-a",
                                              store.agents["agent-1"].enrollment_key_hash)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 403
        assert route.call_count == 0


class TestAuthorization:
    """Server-side join authorization: hostname binding, key purpose, issuance ledger."""

    @pytest.mark.asyncio
    async def test_wrong_hostname_403(self, client, store: FakeStore) -> None:
        """member_name differing from the agent's enrolled hostname is refused; LXD untouched."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(ENDPOINT, json={"member_name": "node-b"}, headers=_auth())
        assert resp.status_code == 403
        assert route.call_count == 0
        assert store.issuances == []

    @pytest.mark.asyncio
    async def test_hostname_comparison_is_exact(self, client, store: FakeStore) -> None:
        """A case-changed hostname does not match the recorded one."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False):
            resp = await client.post(ENDPOINT, json={"member_name": "NODE-A"}, headers=_auth())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("purpose", [None, "admin", "viewer"])
    async def test_non_join_agent_403(self, client, store: FakeStore, purpose) -> None:
        """An agent whose enrollment key is not node-join or bootstrap is refused; LXD untouched."""
        store.key_purpose[store.agents["agent-1"].enrollment_key_hash] = purpose
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 403
        assert route.call_count == 0

    @pytest.mark.asyncio
    async def test_agent_without_enrollment_key_403(self, client, store: FakeStore) -> None:
        """An agent row with no enrollment key hash is not join-eligible."""
        store.agents["agent-1"] = AgentRecord("agent-1", "active", "node-a", None)
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_repeat_while_unexpired_409(self, client, store: FakeStore) -> None:
        """A second request for the same member while the first grant is live is refused with 409."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            first = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert first.status_code == 200
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            second = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert second.status_code == 409
        assert route.call_count == 0
        assert len(store.issuances) == 1

    @pytest.mark.asyncio
    async def test_live_issuance_for_member_blocks_other_agent(self, client, store: FakeStore) -> None:
        """A live grant for the member blocks a different agent that claims the same hostname."""
        store.add_agent("agent-2", "node-a", PURPOSE_NODE_JOIN)
        store.issuances.append({"id": 1, "agent_id": "agent-1", "member_name": "node-a",
                                "status": 0, "expires_at": datetime.utcnow() + timedelta(minutes=5)})
        resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth("agent-2"))
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_expired_issuance_allows_new_grant(self, client, store: FakeStore) -> None:
        """An issuance past its expiry no longer blocks a new grant."""
        store.issuances.append({"id": 1, "agent_id": "agent-1", "member_name": "node-a",
                                "status": 0, "expires_at": datetime.utcnow() - timedelta(seconds=1)})
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            resp = await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_grant_and_refusal_are_audited(self, client, store: FakeStore, monkeypatch) -> None:
        """Both the grant and each refusal reach the audit hook with the right action."""
        events: list[str] = []
        monkeypatch.setattr(lxd_cluster, "_audit",
                            lambda action, message, severity, details: events.append(action))
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        await client.post(ENDPOINT, json={"member_name": "node-b"}, headers=_auth())
        assert events == ["lxd.cluster_join_token.issued", "lxd.cluster_join_token.refused"]


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
            _lxd_success_routes(mock)
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


class TestBootstrapSeeding:
    """Bootstrap enrollment key seeding and tombstone behaviour."""

    def test_hash_matches_enroll_scheme(self) -> None:
        """Hash is unsalted SHA-256 hex, identical to /agents/enroll lookups."""
        assert bootstrap_token_hash("x" * 40) == hashlib.sha256(("x" * 40).encode()).hexdigest()

    def test_first_seed_inserts_key_and_tombstone(self, store: FakeStore) -> None:
        """First seed inserts one bootstrap-purpose key and records the tombstone."""
        assert lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP) is True
        assert store.seeded_key_inserts == 1
        assert bootstrap_token_hash(BOOTSTRAP) in store.seeds

    def test_deleted_key_is_not_resurrected(self, store: FakeStore) -> None:
        """After the key row is deleted or revoked, reseeding the same token is refused."""
        lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP)
        store.key_purpose.clear()  # simulate hard delete of the enrollment key row
        assert lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP) is False
        assert store.seeded_key_inserts == 1
        assert store.key_purpose == {}

    @pytest.mark.parametrize("token", ["short", "has space " * 4, "bad/char" * 6])
    def test_seed_rejects_malformed_token(self, store: FakeStore, token: str) -> None:
        """Malformed bootstrap tokens are refused before any write."""
        with pytest.raises(ValueError):
            lxd_cluster.seed_bootstrap_enrollment_key(token)
        assert store.seeded_key_inserts == 0

    def test_seed_rejects_bad_ttl(self, store: FakeStore) -> None:
        """TTL outside 1-24 hours is refused before any write."""
        with pytest.raises(ValueError):
            lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP, ttl_hours=48)
        assert store.seeded_key_inserts == 0
