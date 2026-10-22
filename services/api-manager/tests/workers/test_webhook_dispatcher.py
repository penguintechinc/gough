"""Tests for app/workers/webhook_dispatcher.py.

Verifies signing across all three modes (with end-to-end public-key
verification using the ``cryptography`` library), retry policy, dead-letter
behavior, header construction, and event filtering.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as _hmac
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from app.workers.webhook_dispatcher import (
    MAX_ATTEMPTS,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    WebhookDispatcher,
    WebhookEndpoint,
    _RETRY_DELAYS_SECONDS,
    build_signing_string,
)
from app.security.webhook_keys import (
    ECDSA_P256_SHA256,
    ED25519,
    HMAC_SHA256,
    WebhookKeyManager,
)
from tests._webhook_fakes import FakeVaultClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _RecordingTransport(httpx.AsyncBaseTransport):
    """httpx mock transport that returns scripted responses and records hits."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(500)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _make_client(transport: _RecordingTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, timeout=REQUEST_TIMEOUT_SECONDS)


def _make_dispatcher(
    vault: FakeVaultClient,
    transport: _RecordingTransport,
    *,
    sleep: Any = None,
    nats: Any = None,
) -> WebhookDispatcher:
    if sleep is None:
        async def _no_sleep(_d: float) -> None:
            return None
        sleep = _no_sleep
    return WebhookDispatcher(
        db_session=None,
        vault_client=vault,
        cluster_id="cluster-test",
        http_client=_make_client(transport),
        sleep=sleep,
        nats_publisher=nats,
        endpoint_loader=lambda _t: [],
    )


def _verify_ed25519(public_pem: str, sig_hdr: str, signing_input: bytes) -> None:
    assert sig_hdr.startswith("v1.ed25519=")
    sig = base64.b64decode(sig_hdr.split("=", 1)[1])
    assert len(sig) == 64
    pub = serialization.load_pem_public_key(public_pem.encode("ascii"))
    assert isinstance(pub, ed25519.Ed25519PublicKey)
    pub.verify(sig, signing_input)


def _verify_ecdsa(public_pem: str, sig_hdr: str, signing_input: bytes) -> None:
    assert sig_hdr.startswith("v1.ecdsa=")
    sig = base64.b64decode(sig_hdr.split("=", 1)[1])
    pub = serialization.load_pem_public_key(public_pem.encode("ascii"))
    assert isinstance(pub, ec.EllipticCurvePublicKey)
    pub.verify(sig, signing_input, ec.ECDSA(hashes.SHA256()))


def _verify_hmac(secret: bytes, sig_hdr: str, signing_input: bytes) -> None:
    assert sig_hdr.startswith("v1.hmac=")
    expected = _hmac.new(secret, signing_input, hashlib.sha256).hexdigest()
    assert _hmac.compare_digest(sig_hdr.split("=", 1)[1], expected)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestBuildSigningString:
    def test_format_matches_spec(self) -> None:
        body = b'{"a":1}'
        s = build_signing_string("2026-01-02T03:04:05.000000Z", "EVT123", body)
        text = s.decode()
        # Spec: ts || "." || event_id || "." || sha256_hex
        # Timestamp has a dot in it (microseconds), so split carefully:
        # "2026-01-02T03:04:05.000000Z.EVT123.<hex>"
        parts = text.split(".")
        assert len(parts) == 4  # timestamp parts + event_id + digest
        ts_iso, microseconds_z, evt, digest = parts
        assert ts_iso == "2026-01-02T03:04:05"
        assert microseconds_z == "000000Z"
        assert evt == "EVT123"
        assert digest == hashlib.sha256(body).hexdigest()
        assert text.startswith("2026-01-02T03:04:05.000000Z.EVT123.")


class TestRetryPolicyConstants:
    def test_eight_attempts_total(self) -> None:
        assert MAX_ATTEMPTS == 8
        assert _RETRY_DELAYS_SECONDS == (
            1.0, 5.0, 30.0, 300.0, 1800.0, 7200.0, 43200.0,
        )


# ---------------------------------------------------------------------------
# Signing round-trips
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSigningRoundTrip:
    async def test_ed25519_signature_verifies(self) -> None:
        vault = FakeVaultClient()
        endpoint = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://example.test/hook",
            signing_mode=ED25519,
        )
        transport = _RecordingTransport([httpx.Response(200)])
        dispatcher = _make_dispatcher(vault, transport)
        try:
            results = await dispatcher.dispatch(
                {"type": "x.event", "tenant_id": "acme",
                 "data": {"foo": "bar"}}
            ) if False else None
            # We bypass the loader by passing the endpoint via the dispatcher's
            # endpoint_loader override:
            dispatcher._endpoint_loader = lambda _t: [endpoint]
            results = await dispatcher.dispatch(
                {"type": "x.event", "tenant_id": "acme",
                 "data": {"foo": "bar"}}
            )
        finally:
            await dispatcher.aclose()

        assert results[0].delivered is True
        req = transport.requests[0]
        sig = req.headers["X-Gough-Signature"]
        ts = req.headers["X-Gough-Timestamp"]
        evt = req.headers["X-Gough-Event-Id"]
        signing_input = build_signing_string(ts, evt, req.content)

        public_pem = vault.secrets.transit.keys["acme/webhook-ed25519"].versions[1].public_pem
        _verify_ed25519(public_pem, sig, signing_input)

    async def test_ecdsa_signature_verifies(self) -> None:
        vault = FakeVaultClient()
        endpoint = WebhookEndpoint(
            id="w2", tenant_id="acme", url="https://example.test/hook",
            signing_mode=ECDSA_P256_SHA256,
        )
        transport = _RecordingTransport([httpx.Response(204)])
        dispatcher = _make_dispatcher(vault, transport)
        dispatcher._endpoint_loader = lambda _t: [endpoint]
        try:
            results = await dispatcher.dispatch(
                {"type": "node.added", "tenant_id": "acme"}
            )
        finally:
            await dispatcher.aclose()

        assert results[0].delivered is True
        req = transport.requests[0]
        signing_input = build_signing_string(
            req.headers["X-Gough-Timestamp"],
            req.headers["X-Gough-Event-Id"],
            req.content,
        )
        public_pem = vault.secrets.transit.keys[
            "acme/webhook-ecdsa_p256_sha256"
        ].versions[1].public_pem
        _verify_ecdsa(public_pem, req.headers["X-Gough-Signature"], signing_input)

    async def test_hmac_signature_verifies(self) -> None:
        vault = FakeVaultClient()
        mgr = WebhookKeyManager(vault)
        kid = mgr.ensure_keys_for_tenant("acme", HMAC_SHA256)
        secret = mgr.get_hmac_secret("acme", kid.kid)

        endpoint = WebhookEndpoint(
            id="w3", tenant_id="acme", url="https://example.test/hook",
            signing_mode=HMAC_SHA256,
        )
        transport = _RecordingTransport([httpx.Response(200)])
        dispatcher = _make_dispatcher(vault, transport)
        dispatcher._endpoint_loader = lambda _t: [endpoint]
        try:
            results = await dispatcher.dispatch(
                {"type": "node.added", "tenant_id": "acme"}
            )
        finally:
            await dispatcher.aclose()

        assert results[0].delivered is True
        req = transport.requests[0]
        signing_input = build_signing_string(
            req.headers["X-Gough-Timestamp"],
            req.headers["X-Gough-Event-Id"],
            req.content,
        )
        _verify_hmac(secret, req.headers["X-Gough-Signature"], signing_input)


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestHeaders:
    async def test_standard_headers_present(self) -> None:
        vault = FakeVaultClient()
        endpoint = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://example.test/hook",
            signing_mode=ED25519,
        )
        transport = _RecordingTransport([httpx.Response(200)])
        dispatcher = _make_dispatcher(vault, transport)
        dispatcher._endpoint_loader = lambda _t: [endpoint]
        try:
            await dispatcher.dispatch({"type": "x", "tenant_id": "acme"})
        finally:
            await dispatcher.aclose()

        req = transport.requests[0]
        assert req.headers["User-Agent"] == USER_AGENT
        assert req.headers["X-Gough-Cluster-Id"] == "cluster-test"
        assert req.headers["X-Gough-Delivery-Attempt"] == "1"
        assert req.headers["X-Gough-Key-Id"].startswith("ed25519-v")
        assert req.headers["X-Gough-Event-Id"]
        assert "T" in req.headers["X-Gough-Timestamp"]


# ---------------------------------------------------------------------------
# Retry / dead-letter
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRetryAndDeadLetter:
    async def test_retries_until_dead_letter(self) -> None:
        vault = FakeVaultClient()
        endpoint = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://example.test/hook",
            signing_mode=ED25519,
        )
        # 8 attempts of 5xx → dead-letter.
        transport = _RecordingTransport(
            [httpx.Response(503) for _ in range(MAX_ATTEMPTS)]
        )
        nats = AsyncMock()
        sleeps: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleeps.append(d)

        dispatcher = _make_dispatcher(vault, transport, sleep=fake_sleep, nats=nats)
        dispatcher._endpoint_loader = lambda _t: [endpoint]
        try:
            results = await dispatcher.dispatch(
                {"type": "node.added", "tenant_id": "acme"}
            )
        finally:
            await dispatcher.aclose()

        assert len(transport.requests) == MAX_ATTEMPTS
        assert results[0].delivered is False
        assert results[0].attempts == MAX_ATTEMPTS
        assert results[0].final_status == 503

        # Retry delays should match spec: between each attempt we slept once.
        assert sleeps == list(_RETRY_DELAYS_SECONDS)

        # Dead-letter published exactly once on the right subject.
        nats.assert_awaited_once()
        subject, payload = nats.await_args.args
        assert subject == "gough.webhook.acme.dead_letter"
        body = json.loads(payload.decode("utf-8"))
        assert body["last_status"] == 503
        assert body["attempts"] == MAX_ATTEMPTS
        assert body["endpoint_id"] == "w1"
        assert body["event"]["type"] == "node.added"

    async def test_succeeds_on_second_attempt(self) -> None:
        vault = FakeVaultClient()
        endpoint = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://example.test/hook",
            signing_mode=ED25519,
        )
        transport = _RecordingTransport(
            [httpx.Response(500), httpx.Response(200)]
        )
        nats = AsyncMock()
        dispatcher = _make_dispatcher(vault, transport, nats=nats)
        dispatcher._endpoint_loader = lambda _t: [endpoint]
        try:
            results = await dispatcher.dispatch(
                {"type": "ev", "tenant_id": "acme"}
            )
        finally:
            await dispatcher.aclose()

        assert results[0].delivered is True
        assert results[0].attempts == 2
        nats.assert_not_awaited()
        # Second attempt's header value should reflect attempt 2.
        assert transport.requests[1].headers["X-Gough-Delivery-Attempt"] == "2"

    async def test_timeout_then_success(self) -> None:
        vault = FakeVaultClient()
        endpoint = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://example.test/hook",
            signing_mode=ED25519,
        )
        transport = _RecordingTransport(
            [httpx.ReadTimeout("slow"), httpx.Response(200)]
        )
        dispatcher = _make_dispatcher(vault, transport)
        dispatcher._endpoint_loader = lambda _t: [endpoint]
        try:
            results = await dispatcher.dispatch(
                {"type": "ev", "tenant_id": "acme"}
            )
        finally:
            await dispatcher.aclose()
        assert results[0].delivered is True
        assert results[0].attempts == 2


# ---------------------------------------------------------------------------
# Event filtering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestEventFilter:
    async def test_filter_match_exact(self) -> None:
        ep = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://x", signing_mode=ED25519,
            event_filter=("node.added",),
        )
        assert ep.matches("node.added")
        assert not ep.matches("node.removed")

    async def test_filter_wildcard(self) -> None:
        ep = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://x", signing_mode=ED25519,
            event_filter=("node.*",),
        )
        assert ep.matches("node.added")
        assert not ep.matches("disk.added")

    async def test_filter_inactive_blocks(self) -> None:
        ep = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://x", signing_mode=ED25519,
            active=False,
        )
        assert not ep.matches("node.added")

    async def test_no_filter_matches_all(self) -> None:
        ep = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://x", signing_mode=ED25519,
        )
        assert ep.matches("anything.at.all")

    async def test_dispatch_skips_non_matching(self) -> None:
        vault = FakeVaultClient()
        ep = WebhookEndpoint(
            id="w1", tenant_id="acme", url="https://example.test/hook",
            signing_mode=ED25519, event_filter=("node.*",),
        )
        transport = _RecordingTransport([])
        dispatcher = _make_dispatcher(vault, transport)
        dispatcher._endpoint_loader = lambda _t: [ep]
        try:
            results = await dispatcher.dispatch(
                {"type": "disk.added", "tenant_id": "acme"}
            )
        finally:
            await dispatcher.aclose()
        assert results == []
        assert transport.requests == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestEventValidation:
    async def test_missing_type_raises(self) -> None:
        vault = FakeVaultClient()
        transport = _RecordingTransport([])
        dispatcher = _make_dispatcher(vault, transport)
        try:
            with pytest.raises(ValueError):
                await dispatcher.dispatch({"tenant_id": "acme"})
        finally:
            await dispatcher.aclose()

    async def test_missing_tenant_raises(self) -> None:
        vault = FakeVaultClient()
        transport = _RecordingTransport([])
        dispatcher = _make_dispatcher(vault, transport)
        try:
            with pytest.raises(ValueError):
                await dispatcher.dispatch({"type": "x"})
        finally:
            await dispatcher.aclose()
