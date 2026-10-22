"""Tests for the NATS JetStream client.

These tests do not require a running nats-server; the NATS client and the
JetStream context are mocked at the boundary.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.clients.nats import (
    AUDIT_STREAM,
    EVENTS_STREAM,
    EVENTS_SUBJECT_PREFIX,
    EventEnvelope,
    NatsClient,
    NatsConnectionError,
    NatsCredentialError,
    NatsPublishError,
    Subscription,
    _generate_ulid,
    _rfc3339_now,
)
from app.clients.vault import VaultError, VaultKvReadResponse


# ---------- helpers ---------------------------------------------------------


def _vault_with_creds(creds: str | None = "-----BEGIN NATS USER NKEY-----\nSEED\n-----END-----") -> MagicMock:
    vault = MagicMock()
    if creds is None:
        vault.kv_read.return_value = VaultKvReadResponse(data={}, metadata={})
    else:
        vault.kv_read.return_value = VaultKvReadResponse(
            data={"creds": creds}, metadata={"version": 1}
        )
    return vault


def _make_client(vault: MagicMock | None = None, **kwargs) -> NatsClient:
    return NatsClient(
        server_urls=["nats://nats-1:4222", "nats://nats-2:4222"],
        vault_client=vault or _vault_with_creds(),
        cluster_id="alpha",
        tenant_id_default="tenant-x",
        **kwargs,
    )


def _patch_connect(mock_js: AsyncMock | MagicMock):
    """Patch nats.connect to return a mock NatsAioClient with given JS."""
    nc = AsyncMock()
    nc.is_connected = True
    nc.jetstream = MagicMock(return_value=mock_js)
    nc.drain = AsyncMock()
    return patch("app.clients.nats.nats.connect", new=AsyncMock(return_value=nc)), nc


# ---------- ULID + envelope -------------------------------------------------


class TestUlid:
    def test_generates_26_chars(self) -> None:
        ulid = _generate_ulid()
        assert len(ulid) == 26
        assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in ulid)

    def test_uniqueness(self) -> None:
        ids = {_generate_ulid() for _ in range(200)}
        assert len(ids) == 200


class TestRfc3339:
    def test_format(self) -> None:
        ts = _rfc3339_now()
        assert ts.endswith("Z")
        assert "T" in ts


class TestEventEnvelope:
    def test_to_bytes_round_trip(self) -> None:
        env = EventEnvelope(
            version="1",
            event="nodes.created",
            id="01H1234",
            ts="2026-04-28T00:00:00Z",
            cluster_id="alpha",
            tenant_id="tenant-x",
            actor_sub="user:42",
            trace_id="abc",
            request_id="req-1",
            data={"foo": "bar"},
        )
        decoded = json.loads(env.to_bytes().decode("utf-8"))
        assert decoded["event"] == "nodes.created"
        assert decoded["data"] == {"foo": "bar"}
        assert decoded["cluster_id"] == "alpha"


# ---------- credential loading ----------------------------------------------


class TestCredentialLoading:
    def test_missing_creds_raises(self) -> None:
        client = _make_client(vault=_vault_with_creds(creds=None))
        with pytest.raises(NatsCredentialError):
            client._load_creds_from_vault()

    def test_vault_error_wrapped(self) -> None:
        vault = MagicMock()
        vault.kv_read.side_effect = VaultError("boom")
        client = _make_client(vault=vault)
        with pytest.raises(NatsCredentialError):
            client._load_creds_from_vault()

    def test_vault_path_uses_cluster_id(self) -> None:
        client = _make_client()
        assert client._vault_creds_path() == "gough/alpha/nats/credentials"


# ---------- connect ---------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_creates_streams(self) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock(side_effect=Exception("not found"))
        # Catch the exception flow - first try update, fall back to add.
        from nats.js.errors import NotFoundError as JsNotFoundError

        js.update_stream = AsyncMock(side_effect=JsNotFoundError("missing"))
        js.add_stream = AsyncMock()
        js.publish = AsyncMock()

        client = _make_client()
        ctx, _nc = _patch_connect(js)
        with ctx:
            await client.connect()
        assert js.add_stream.await_count == 2  # events + audit
        names_added = [
            call.kwargs["config"].name for call in js.add_stream.await_args_list
        ]
        assert EVENTS_STREAM in names_added
        assert AUDIT_STREAM in names_added

    @pytest.mark.asyncio
    async def test_connect_idempotent(self) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()

        client = _make_client()
        ctx, nc = _patch_connect(js)
        with ctx as connect_mock:
            await client.connect()
            await client.connect()
        # Only one underlying connect call.
        assert connect_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_connect_no_servers_raises(self) -> None:
        from nats.errors import NoServersError

        client = _make_client()
        with patch(
            "app.clients.nats.nats.connect",
            new=AsyncMock(side_effect=NoServersError()),
        ):
            with pytest.raises(NatsConnectionError):
                await client.connect()

    @pytest.mark.asyncio
    async def test_close_cleans_creds_file(self, tmp_path) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        client = _make_client()
        ctx, nc = _patch_connect(js)
        with ctx:
            await client.connect()
        creds_path = client._creds_path  # type: ignore[attr-defined]
        import os as _os

        assert _os.path.exists(creds_path)
        await client.close()
        assert not _os.path.exists(creds_path)


# ---------- publish ---------------------------------------------------------


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_requires_connect(self) -> None:
        client = _make_client()
        with pytest.raises(NatsConnectionError):
            await client.publish("nodes.created", {"x": 1})

    @pytest.mark.asyncio
    async def test_publish_wraps_envelope(self) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        ack = MagicMock(seq=42)
        js.publish = AsyncMock(return_value=ack)

        client = _make_client()
        ctx, _nc = _patch_connect(js)
        with ctx:
            await client.connect()
            seq = await client.publish(
                "nodes.created",
                {"node_id": "n-1"},
                actor_sub="user:1",
                trace_id="trace-1",
                request_id="req-1",
            )
        assert seq == "42"
        assert js.publish.await_count == 1
        call = js.publish.await_args
        assert call.kwargs["subject"] == f"{EVENTS_SUBJECT_PREFIX}.nodes.created"
        body = json.loads(call.kwargs["payload"].decode("utf-8"))
        assert body["event"] == "nodes.created"
        assert body["cluster_id"] == "alpha"
        assert body["tenant_id"] == "tenant-x"
        assert body["actor_sub"] == "user:1"
        assert body["data"] == {"node_id": "n-1"}
        # Headers include event id + version
        headers = call.kwargs["headers"]
        assert headers["gough-event-version"] == "1"
        assert headers["gough-cluster-id"] == "alpha"

    @pytest.mark.asyncio
    async def test_publish_audit_mirror(self) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        js.publish = AsyncMock(return_value=MagicMock(seq=7))

        client = _make_client()
        ctx, _nc = _patch_connect(js)
        with ctx:
            await client.connect()
            await client.publish("audit.login", {}, audit=True)
        # Two publishes - events and audit
        assert js.publish.await_count == 2
        subjects = [c.kwargs["subject"] for c in js.publish.await_args_list]
        assert f"{EVENTS_SUBJECT_PREFIX}.audit.login" in subjects
        assert any(s.startswith("gough.audit.") for s in subjects)

    @pytest.mark.asyncio
    async def test_publish_audit_failure_does_not_raise(self) -> None:
        from nats.errors import ConnectionClosedError

        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        # First publish ok, second (audit) fails
        js.publish = AsyncMock(
            side_effect=[MagicMock(seq=1), ConnectionClosedError()]
        )

        client = _make_client()
        ctx, _nc = _patch_connect(js)
        with ctx:
            await client.connect()
            seq = await client.publish("audit.event", {}, audit=True)
        # Primary publish succeeded; audit error swallowed
        assert seq == "1"

    @pytest.mark.asyncio
    async def test_publish_propagates_connection_error(self) -> None:
        from nats.errors import ConnectionClosedError

        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        js.publish = AsyncMock(side_effect=ConnectionClosedError())

        client = _make_client()
        ctx, _nc = _patch_connect(js)
        with ctx:
            await client.connect()
            with pytest.raises(NatsPublishError):
                await client.publish("nodes.created", {})


# ---------- subscribe -------------------------------------------------------


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_requires_connect(self) -> None:
        client = _make_client()
        with pytest.raises(NatsConnectionError):
            await client.subscribe("nodes.created", "durable", AsyncMock())

    @pytest.mark.asyncio
    async def test_subscribe_uses_durable_consumer(self) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        nats_sub = MagicMock()
        nats_sub.unsubscribe = AsyncMock()
        js.subscribe = AsyncMock(return_value=nats_sub)

        client = _make_client()
        ctx, _nc = _patch_connect(js)
        handler = AsyncMock()
        with ctx:
            await client.connect()
            sub = await client.subscribe("nodes.*", "worker-1", handler)
        assert isinstance(sub, Subscription)
        assert sub.durable_name == "worker-1"
        assert js.subscribe.await_count == 1
        kwargs = js.subscribe.await_args.kwargs
        assert kwargs["subject"] == f"{EVENTS_SUBJECT_PREFIX}.nodes.*"
        assert kwargs["durable"] == "worker-1"
        assert kwargs["manual_ack"] is True

    @pytest.mark.asyncio
    async def test_handler_failure_naks(self) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        nats_sub = MagicMock()
        nats_sub.unsubscribe = AsyncMock()

        captured_cb: list = []

        async def fake_subscribe(**kwargs):
            captured_cb.append(kwargs["cb"])
            return nats_sub

        js.subscribe = fake_subscribe

        client = _make_client()
        ctx, _nc = _patch_connect(js)

        async def bad_handler(_msg):
            raise RuntimeError("boom")

        with ctx:
            await client.connect()
            await client.subscribe("foo", "d1", bad_handler)
        cb = captured_cb[0]
        msg = MagicMock()
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        msg.subject = f"{EVENTS_SUBJECT_PREFIX}.foo"
        await cb(msg)
        msg.nak.assert_awaited_once()
        msg.ack.assert_not_awaited()


# ---------- introspection ---------------------------------------------------


class TestIntrospection:
    @pytest.mark.asyncio
    async def test_stream_info(self) -> None:
        js = MagicMock()
        js.update_stream = AsyncMock()
        js.add_stream = AsyncMock()
        info = MagicMock()
        info.config.name = EVENTS_STREAM
        info.config.subjects = ["gough.events.>"]
        info.config.retention = MagicMock(value="interest")
        info.config.max_age = 24 * 3600 * 1_000_000_000
        info.state.messages = 5
        info.state.bytes = 1234
        info.state.first_seq = 1
        info.state.last_seq = 5
        js.stream_info = AsyncMock(return_value=info)

        client = _make_client()
        ctx, _nc = _patch_connect(js)
        with ctx:
            await client.connect()
            info_dict = await client.stream_info(EVENTS_STREAM)
        assert info_dict["name"] == EVENTS_STREAM
        assert info_dict["messages"] == 5
        assert info_dict["last_seq"] == 5

    def test_is_connected_false_initially(self) -> None:
        client = _make_client()
        assert client.is_connected is False
