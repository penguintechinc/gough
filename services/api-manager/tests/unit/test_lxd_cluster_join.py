"""Unit tests for LXD join authorization, single-issuance ledger, and bootstrap seeding.

LXD is mocked at the HTTP layer with respx. The database is an in-memory FakeStore that
patches the module-level ``_db_*`` helpers. The fake enforces the same unique "live"
constraints as the real table, so concurrency tests exercise the DB-level guarantee.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx
import jwt
import pytest
import respx
from app.api import lxd_cluster
from app.api.lxd_cluster import (
    PURPOSE_BOOTSTRAP,
    PURPOSE_NODE_JOIN,
    AgentRecord,
    IssuanceConflictError,
    KeyJoinInfo,
    bootstrap_token_hash,
    is_valid_hostname,
    lxd_bp,
    mask_token,
    parse_join_token_request,
    parse_metadata,
)
from quart import Quart

SECRET = "test-jwt-secret"
LXD_URL = "https://lxd.test:8443"
OP_UUID = "11111111-2222-3333-4444-555555555555"
OP_PATH = f"/1.0/operations/{OP_UUID}"
ENDPOINT = "/api/v1/lxd/cluster-join-tokens"
MEMBER_URL = "/1.0/cluster/members/node-a"
TOKEN_META = {
    "serverName": "node-a",
    "fingerprint": "ab12",
    "addresses": ["10.0.0.1:8443"],
    "secret": "s3cr3t-value",
    "expiresAt": "2030-01-01T00:00:00Z",
}
EXPECTED_TOKEN = base64.b64encode(
    json.dumps(TOKEN_META, separators=(",", ":"), sort_keys=True).encode()
).decode()
BOOTSTRAP = "B" * 40


@dataclass(slots=True)
class FakeStore:
    """In-memory agents, enrollment keys, ledger, and tombstones with unique live columns."""

    agents: dict[str, AgentRecord] = field(default_factory=dict)
    keys: dict[str, dict] = field(
        default_factory=dict
    )  # key_hash -> {purpose, intended, revoked}
    issuances: list[dict] = field(default_factory=list)
    seeds: set[str] = field(default_factory=set)
    seeded_key_inserts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_agent(
        self,
        agent_id: str,
        hostname: str,
        purpose: Optional[str],
        *,
        intended: Optional[str] = None,
        status: str = "active",
    ) -> None:
        """Register an agent and the enrollment key it used."""
        key_hash = hashlib.sha256(f"key-{agent_id}".encode()).hexdigest()
        self.agents[agent_id] = AgentRecord(agent_id, status, hostname, key_hash)
        self.keys[key_hash] = {
            "purpose": purpose,
            "intended": intended,
            "revoked": False,
        }


def _live_conflict(
    issuances: list[dict], agent_id: str, member: str, key_hash: str
) -> bool:
    """True when a live (status ISSUED) row already holds the agent, member, or key."""
    return any(
        i["status"] == 0
        and (
            i["agent_id"] == agent_id
            or i["member_name"] == member
            or i["key_hash"] == key_hash
        )
        for i in issuances
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """Fresh store with one active node-join agent named node-a bound to intended hostname node-a."""
    fake = FakeStore()
    fake.add_agent("agent-1", "node-a", PURPOSE_NODE_JOIN, intended="node-a")
    monkeypatch.setattr(lxd_cluster, "_db_load_agent", lambda a: fake.agents.get(a))

    def key_info(key_hash: str) -> Optional[KeyJoinInfo]:
        raw = fake.keys.get(key_hash)
        if raw is None:
            return None
        return KeyJoinInfo(key_hash, raw["purpose"], raw["intended"], raw["revoked"])

    def record(agent_id: str, member: str, key_hash: str, now: datetime) -> int:
        with fake.lock:
            if _live_conflict(fake.issuances, agent_id, member, key_hash):
                raise IssuanceConflictError()
            fake.issuances.append(
                {
                    "agent_id": agent_id,
                    "member_name": member,
                    "key_hash": key_hash,
                    "status": 0,
                    "expires_at": now + timedelta(minutes=15),
                }
            )
            return len(fake.issuances)

    def mark_failed(issuance_id: int) -> None:
        with fake.lock:
            fake.issuances[issuance_id - 1]["status"] = 1

    def bootstrap_state(token_hash: str) -> tuple[bool, bool]:
        return token_hash in fake.seeds, token_hash in fake.keys

    def write_tombstone(token_hash: str) -> None:
        fake.seeds.add(token_hash)

    def seed(token_hash: str, owner: int, expires: datetime) -> None:
        fake.keys[token_hash] = {
            "purpose": PURPOSE_BOOTSTRAP,
            "intended": None,
            "revoked": False,
        }
        fake.seeds.add(token_hash)
        fake.seeded_key_inserts += 1

    monkeypatch.setattr(lxd_cluster, "_db_key_join_info", key_info)
    monkeypatch.setattr(lxd_cluster, "_db_record_issuance", record)
    monkeypatch.setattr(lxd_cluster, "_db_mark_issuance_failed", mark_failed)
    monkeypatch.setattr(lxd_cluster, "_db_bootstrap_state", bootstrap_state)
    monkeypatch.setattr(lxd_cluster, "_db_write_tombstone", write_tombstone)
    monkeypatch.setattr(lxd_cluster, "_db_first_active_user_id", lambda: 1)
    monkeypatch.setattr(lxd_cluster, "_db_seed_bootstrap", seed)
    return fake


def _agent_jwt(
    agent_id: str = "agent-1",
    token_type: str = "agent_access",
    expires: timedelta = timedelta(hours=1),
) -> str:
    """Build an agent access token signed with the test secret."""
    payload = {
        "sub": f"agent:{agent_id}",
        "type": token_type,
        "exp": datetime.utcnow() + expires,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _auth(agent_id: str = "agent-1") -> dict[str, str]:
    """Return an Authorization header for an agent token."""
    return {"Authorization": f"Bearer {_agent_jwt(agent_id)}"}


def _operation_ok(metadata: object = None) -> dict:
    """Build a successful LXD operation wait response."""
    return {
        "type": "sync",
        "status_code": 200,
        "metadata": {
            "id": OP_UUID,
            "status_code": 200,
            "metadata": TOKEN_META if metadata is None else metadata,
        },
    }


def _lxd_success_routes(mock: respx.MockRouter):
    """Register the LXD mock: member absent (404), POST accepted, operation completes."""
    mock.get(MEMBER_URL).respond(404, json={"error": "not found"})
    post = mock.post("/1.0/cluster/members").respond(
        202, json={"type": "async", "status_code": 100, "operation": OP_PATH}
    )
    mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok())
    return post


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


class TestSuccess:
    """Happy-path behaviour of the join token endpoint."""

    @pytest.mark.asyncio
    async def test_issues_token_and_records_live_grant(
        self, client, store: FakeStore
    ) -> None:
        """A completed operation returns the base64 token and records one live ISSUED row."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 200
        assert await resp.get_json() == {"token": EXPECTED_TOKEN}
        assert len(store.issuances) == 1
        assert store.issuances[0]["status"] == 0

    @pytest.mark.asyncio
    async def test_sends_server_name_to_lxd(self, client, store: FakeStore) -> None:
        """The member name is forwarded as LXD server_name."""
        store.agents["agent-1"] = AgentRecord(
            "agent-1",
            "active",
            "node-a.lab",
            store.agents["agent-1"].enrollment_key_hash,
        )
        store.keys[store.agents["agent-1"].enrollment_key_hash][
            "intended"
        ] = "node-a.lab"
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get("/1.0/cluster/members/node-a.lab").respond(404)
            route = mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH}
            )
            mock.get(f"{OP_PATH}/wait").respond(200, json=_operation_ok())
            await client.post(
                ENDPOINT, json={"member_name": "node-a.lab"}, headers=_auth()
            )
        assert json.loads(route.calls.last.request.content) == {
            "server_name": "node-a.lab"
        }

    @pytest.mark.asyncio
    async def test_bootstrap_purpose_agent_allowed(
        self, client, store: FakeStore
    ) -> None:
        """An agent enrolled with a bootstrap key is join-eligible (no intended hostname needed)."""
        store.add_agent("agent-boot", "node-a", PURPOSE_BOOTSTRAP)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth("agent-boot")
            )
        assert resp.status_code == 200


class TestUpstreamErrors:
    """LXD failure modes map to client-facing status codes."""

    @pytest.mark.asyncio
    async def test_member_exists_precheck_409(self, client, store: FakeStore) -> None:
        """Member already in the cluster (GET 200) refuses before minting; grant is released."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(200, json={"name": "node-a"})
            post = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 409
        assert post.call_count == 0
        assert store.issuances[0]["status"] == 1

    @pytest.mark.asyncio
    async def test_lxd_post_409_member_exists(self, client) -> None:
        """LXD 409 on the POST itself surfaces as 409."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").respond(409, json={"error": "exists"})
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_lxd_timeout_returns_503(self, client) -> None:
        """An LXD read timeout surfaces as 503."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").mock(
                side_effect=httpx.ReadTimeout("slow")
            )
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_lxd_connect_error_returns_503(self, client) -> None:
        """An unreachable LXD surfaces as 503."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).mock(side_effect=httpx.ConnectError("refused"))
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_lxd_operation_failed_returns_502(self, client) -> None:
        """A failed LXD operation (non-200 status_code) surfaces as 502."""
        failed = {
            "type": "sync",
            "status_code": 200,
            "metadata": {"id": OP_UUID, "status_code": 400, "metadata": None},
        }
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").respond(
                202, json={"type": "async", "status_code": 100, "operation": OP_PATH}
            )
            mock.get(f"{OP_PATH}/wait").respond(200, json=failed)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_operation_path_outside_lxd_namespace_refused(self, client) -> None:
        """An operation reference outside /1.0/operations/ is refused (502), not followed."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").respond(
                202,
                json={
                    "type": "async",
                    "status_code": 100,
                    "operation": "/1.0/instances",
                },
            )
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_lxd_not_configured_returns_503(self, client, monkeypatch) -> None:
        """A non-https LXD_API_URL fails loudly as 503 instead of calling out."""
        monkeypatch.setenv("LXD_API_URL", "http://lxd.test:8080")
        resp = await client.post(
            ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_failed_mint_allows_retry(self, client, store: FakeStore) -> None:
        """A failed mint marks the row FAILED and releases the live columns; a retry succeeds."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            mock.get(MEMBER_URL).respond(404)
            mock.post("/1.0/cluster/members").respond(500)
            first = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert first.status_code == 502
        assert store.issuances[0]["status"] == 1
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            second = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert second.status_code == 200
        assert len(store.issuances) == 2


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
        forged = jwt.encode(
            {
                "sub": "agent:agent-1",
                "type": "agent_access",
                "exp": datetime.utcnow() + timedelta(minutes=5),
            },
            "other-secret",
            algorithm="HS256",
        )
        resp = await client.post(
            ENDPOINT,
            json={"member_name": "node-a"},
            headers={"Authorization": f"Bearer {forged}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_401(self, client) -> None:
        """An expired agent token yields 401."""
        expired = _agent_jwt(expires=timedelta(seconds=-60))
        resp = await client.post(
            ENDPOINT,
            json={"member_name": "node-a"},
            headers={"Authorization": f"Bearer {expired}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_token_type_401(self, client) -> None:
        """A user access token (type access) is not accepted here."""
        user_token = _agent_jwt(token_type="access")
        resp = await client.post(
            ENDPOINT,
            json={"member_name": "node-a"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_agent_401(self, client) -> None:
        """A valid token for an agent that no longer exists yields 401."""
        resp = await client.post(
            ENDPOINT, json={"member_name": "node-a"}, headers=_auth("ghost")
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_suspended_agent_403(self, client, store: FakeStore) -> None:
        """A suspended agent with a valid token is refused with 403 and never reaches LXD."""
        store.agents["agent-1"] = AgentRecord(
            "agent-1",
            "suspended",
            "node-a",
            store.agents["agent-1"].enrollment_key_hash,
        )
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 403
        assert route.call_count == 0


class TestAuthorization:
    """Join authorization: hostname binding, key purpose, revocation, consumable join right."""

    @pytest.mark.asyncio
    async def test_wrong_hostname_403(self, client, store: FakeStore) -> None:
        """member_name differing from the agent's enrolled hostname is refused; LXD untouched."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-b"}, headers=_auth()
            )
        assert resp.status_code == 403
        assert route.call_count == 0
        assert store.issuances == []

    @pytest.mark.asyncio
    async def test_hostname_comparison_is_exact(self, client) -> None:
        """A case-changed hostname does not match the recorded one."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False):
            resp = await client.post(
                ENDPOINT, json={"member_name": "NODE-A"}, headers=_auth()
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("purpose", [None, "admin", "viewer"])
    async def test_non_join_agent_403(self, client, store: FakeStore, purpose) -> None:
        """An agent whose enrollment key is not node-join or bootstrap is refused; LXD untouched."""
        store.keys[store.agents["agent-1"].enrollment_key_hash]["purpose"] = purpose
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 403
        assert route.call_count == 0

    @pytest.mark.asyncio
    async def test_agent_without_enrollment_key_403(
        self, client, store: FakeStore
    ) -> None:
        """An agent row with no enrollment key hash is not join-eligible."""
        store.agents["agent-1"] = AgentRecord("agent-1", "active", "node-a", None)
        resp = await client.post(
            ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_revoked_key_403(self, client, store: FakeStore) -> None:
        """A revoked join key refuses joins even though the agent is active."""
        store.keys[store.agents["agent-1"].enrollment_key_hash]["revoked"] = True
        resp = await client.post(
            ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_intended_hostname_mismatch_403(
        self, client, store: FakeStore
    ) -> None:
        """A node-join key bound to another hostname refuses even when the agent name matches."""
        store.keys[store.agents["agent-1"].enrollment_key_hash]["intended"] = "node-z"
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert resp.status_code == 403
        assert route.call_count == 0

    @pytest.mark.asyncio
    async def test_success_consumes_join_right_even_after_expiry(
        self, client, store: FakeStore
    ) -> None:
        """After a successful grant, a second request is refused with 409 regardless of expiry."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            first = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert first.status_code == 200
        store.issuances[0]["expires_at"] = datetime.utcnow() - timedelta(days=30)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            second = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
        assert second.status_code == 409
        assert route.call_count == 0
        assert len(store.issuances) == 1

    @pytest.mark.asyncio
    async def test_live_issuance_for_member_blocks_other_agent(
        self, client, store: FakeStore
    ) -> None:
        """A live grant for the member blocks a different agent that claims the same hostname."""
        store.add_agent("agent-2", "node-a", PURPOSE_NODE_JOIN, intended="node-a")
        store.issuances.append(
            {
                "agent_id": "agent-1",
                "member_name": "node-a",
                "key_hash": store.agents["agent-1"].enrollment_key_hash,
                "status": 0,
                "expires_at": datetime.utcnow() + timedelta(minutes=5),
            }
        )
        resp = await client.post(
            ENDPOINT, json={"member_name": "node-a"}, headers=_auth("agent-2")
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_concurrent_requests_exactly_one_200(
        self, client, store: FakeStore
    ) -> None:
        """Two concurrent requests for the same join right: exactly one 200, one LXD POST."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            post = _lxd_success_routes(mock)
            r1, r2 = await asyncio.gather(
                client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth()),
                client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth()),
            )
        statuses = sorted([r1.status_code, r2.status_code])
        assert statuses == [200, 409]
        assert post.call_count == 1
        assert len([i for i in store.issuances if i["status"] == 0]) == 1

    @pytest.mark.asyncio
    async def test_grant_and_refusal_are_audited(
        self, client, store: FakeStore, monkeypatch
    ) -> None:
        """Both the grant and each refusal reach the audit hook with the right action."""
        events: list[str] = []
        monkeypatch.setattr(
            lxd_cluster,
            "_audit",
            lambda action, message, severity, details: events.append(action),
        )
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            await client.post(ENDPOINT, json={"member_name": "node-a"}, headers=_auth())
        await client.post(ENDPOINT, json={"member_name": "node-b"}, headers=_auth())
        assert events == [
            "lxd.cluster_join_token.issued",
            "lxd.cluster_join_token.refused",
        ]


class TestValidation:
    """Request body and hostname validation."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        [
            "a" * 64,
            "-leading",
            "trailing-",
            "under_score",
            "has space",
            "",
            "bad..dots",
        ],
    )
    async def test_bad_hostname_400(self, client, name: str) -> None:
        """Hostnames that are not RFC 1123 or exceed 63 characters are rejected with 400."""
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            route = mock.post("/1.0/cluster/members").respond(202)
            resp = await client.post(
                ENDPOINT, json={"member_name": name}, headers=_auth()
            )
        assert resp.status_code == 400
        assert route.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [None, [], {}, {"member_name": 42}, {"member_name": "node-a", "extra": True}],
    )
    async def test_bad_body_400(self, client, body) -> None:
        """Non-object, missing, wrong-typed, or unknown-field bodies are rejected with 400."""
        resp = await client.post(ENDPOINT, json=body, headers=_auth())
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "name, ok",
        [
            ("node-a", True),
            ("a", True),
            ("A1-b2", True),
            ("a" * 63, True),
            ("a" * 64, False),
            ("node_a", False),
            ("-node", False),
        ],
    )
    def test_is_valid_hostname(self, name: str, ok: bool) -> None:
        """Hostname helper enforces RFC 1123 labels and the 63-char cap."""
        assert is_valid_hostname(name) is ok

    def test_parse_request_returns_dto(self) -> None:
        """A valid body parses into the slotted request DTO."""
        req = parse_join_token_request({"member_name": "node-a"})
        assert req.member_name == "node-a"
        with pytest.raises(AttributeError):
            req.other = "x"  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "raw, purpose",
        [
            ('{"purpose": "node-join"}', "node-join"),
            ("{'purpose': 'bootstrap'}", "bootstrap"),
            ("not-a-dict", None),
            (None, None),
        ],
    )
    def test_parse_metadata_json_or_repr(self, raw, purpose) -> None:
        """Metadata written as JSON or as a Python dict repr (agents.py) both parse; garbage yields {}."""
        assert parse_metadata(raw).get("purpose") == purpose


class TestSecrecy:
    """The LXD join token and bootstrap token never appear in logs."""

    @pytest.mark.asyncio
    async def test_token_not_logged(
        self, client, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Neither the issued token nor its secret appears in any log record; the masked form does."""
        caplog.set_level(logging.DEBUG)
        with respx.mock(base_url=LXD_URL, assert_all_called=False) as mock:
            _lxd_success_routes(mock)
            resp = await client.post(
                ENDPOINT, json={"member_name": "node-a"}, headers=_auth()
            )
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
    """Bootstrap seeding: tombstone or key row blocks a re-seed."""

    def test_hash_matches_enroll_scheme(self) -> None:
        """Hash is unsalted SHA-256 hex, identical to /agents/enroll lookups."""
        assert (
            bootstrap_token_hash("x" * 40)
            == hashlib.sha256(("x" * 40).encode()).hexdigest()
        )

    def test_first_seed_inserts_key_and_tombstone(self, store: FakeStore) -> None:
        """First seed inserts one bootstrap-purpose key and records the tombstone."""
        assert lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP) is True
        assert store.seeded_key_inserts == 1
        assert bootstrap_token_hash(BOOTSTRAP) in store.seeds

    def test_deleted_key_is_not_resurrected(self, store: FakeStore) -> None:
        """After the key row is deleted or revoked, reseeding the same token is refused."""
        lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP)
        store.keys.clear()  # simulate hard delete of the enrollment key row
        assert lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP) is False
        assert store.seeded_key_inserts == 1
        assert store.keys == {}

    def test_key_without_tombstone_writes_tombstone_only(
        self, store: FakeStore
    ) -> None:
        """If a key row exists without a tombstone, seeding writes the tombstone and inserts nothing."""
        token_hash = bootstrap_token_hash(BOOTSTRAP)
        store.keys[token_hash] = {
            "purpose": PURPOSE_BOOTSTRAP,
            "intended": None,
            "revoked": False,
        }
        assert lxd_cluster.seed_bootstrap_enrollment_key(BOOTSTRAP) is False
        assert store.seeded_key_inserts == 0
        assert token_hash in store.seeds

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
