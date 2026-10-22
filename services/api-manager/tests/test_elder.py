"""Tests for app/integrations/elder.py — ElderClient async HTTP client."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.elder import (
    AppEndpoint,
    ElderAuthError,
    ElderClient,
    ElderConflictError,
    ElderConnectionError,
    ElderError,
    ElderNotFoundError,
    HostRegistration,
    get_elder_client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_data=None, raise_json=False):
    """Build a mock httpx Response."""
    import json as _json

    resp = MagicMock()
    resp.status_code = status_code
    if raise_json:
        resp.json.side_effect = _json.JSONDecodeError("bad json", "", 0)
    else:
        resp.json.return_value = json_data if json_data is not None else {}
    return resp


def _make_client(url="http://elder.example.com", api_key="test-key", **kwargs):
    return ElderClient(elder_url=url, api_key=api_key, **kwargs)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_host_registration_defaults(self):
        hr = HostRegistration(hostname="h1", ip="10.0.0.1", fqdn="h1.local")
        assert hr.apps == []
        assert hr.zone == "default"
        assert hr.pool == "default"
        assert hr.tags == {}
        assert hr.metadata == {}

    def test_app_endpoint_defaults(self):
        ae = AppEndpoint(app_name="myapp", hosts=["h1"], port=8080)
        assert ae.protocol == "http"
        assert ae.path == "/"
        assert ae.health_check_url is None
        assert ae.priority == 100


# ---------------------------------------------------------------------------
# ElderClient init and context manager
# ---------------------------------------------------------------------------


class TestElderClientInit:
    def test_url_trailing_slash_stripped(self):
        c = _make_client(url="http://elder.example.com/")
        assert c.elder_url == "http://elder.example.com"

    def test_defaults(self):
        c = _make_client()
        assert c.timeout == ElderClient.REQUEST_TIMEOUT_SECONDS
        assert c.max_retries == ElderClient.MAX_RETRIES
        assert c._client is None

    @pytest.mark.asyncio
    async def test_context_manager_initialises_and_closes_client(self):
        c = _make_client()
        async with c as entered:
            assert entered is c
            assert c._client is not None
        assert c._client is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        c = _make_client()
        await c.close()  # client never opened — should not raise
        await c._ensure_client()
        await c.close()
        assert c._client is None

    @pytest.mark.asyncio
    async def test_ensure_client_sets_auth_headers(self):
        c = _make_client(api_key="secret")
        await c._ensure_client()
        headers = c._client.headers
        assert "Bearer secret" in headers.get("authorization", headers.get("Authorization", ""))
        await c.close()


# ---------------------------------------------------------------------------
# _request: authentication and HTTP error codes
# ---------------------------------------------------------------------------


class TestRequestErrors:
    @pytest.mark.asyncio
    async def test_401_raises_elder_auth_error(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(401))
        with pytest.raises(ElderAuthError, match="Authentication failed"):
            await c._request("GET", "/api/v1/health")
        await c.close()

    @pytest.mark.asyncio
    async def test_404_raises_elder_not_found_error(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(404))
        with pytest.raises(ElderNotFoundError, match="Resource not found"):
            await c._request("GET", "/api/v1/hosts/missing")
        await c.close()

    @pytest.mark.asyncio
    async def test_409_raises_elder_conflict_error(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(409))
        with pytest.raises(ElderConflictError, match="Resource conflict"):
            await c._request("POST", "/api/v1/hosts")
        await c.close()

    @pytest.mark.asyncio
    async def test_422_raises_elder_error(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(422, {"error": "bad field"}))
        with pytest.raises(ElderError, match="bad field"):
            await c._request("POST", "/api/v1/hosts")
        await c.close()

    @pytest.mark.asyncio
    async def test_400_with_bad_json_raises_elder_error(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(400, raise_json=True))
        with pytest.raises(ElderError, match="Client error 400"):
            await c._request("POST", "/api/v1/hosts")
        await c.close()

    @pytest.mark.asyncio
    async def test_unexpected_status_raises_elder_error(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(302))
        with pytest.raises(ElderError, match="Unexpected response code"):
            await c._request("GET", "/api/v1/hosts")
        await c.close()


# ---------------------------------------------------------------------------
# _request: success responses
# ---------------------------------------------------------------------------


class TestRequestSuccess:
    @pytest.mark.asyncio
    async def test_200_returns_json(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {"id": 1}))
        result = await c._request("GET", "/api/v1/hosts/1")
        assert result == {"id": 1}
        await c.close()

    @pytest.mark.asyncio
    async def test_201_returns_json(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(201, {"id": 2}))
        result = await c._request("POST", "/api/v1/hosts")
        assert result == {"id": 2}
        await c.close()

    @pytest.mark.asyncio
    async def test_200_with_bad_json_returns_empty_dict(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, raise_json=True))
        result = await c._request("DELETE", "/api/v1/hosts/x")
        assert result == {}
        await c.close()


# ---------------------------------------------------------------------------
# _request: retry logic
# ---------------------------------------------------------------------------


class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_500_retries_and_raises_on_exhaustion(self):
        c = _make_client(max_retries=2)
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(500))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ElderConnectionError, match="Server error 500"):
                await c._request("GET", "/api/v1/hosts")
        assert c._client.request.call_count == 2
        await c.close()

    @pytest.mark.asyncio
    async def test_500_retries_then_succeeds(self):
        c = _make_client(max_retries=3)
        await c._ensure_client()
        c._client.request = AsyncMock(
            side_effect=[
                _make_response(500),
                _make_response(200, {"ok": True}),
            ]
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await c._request("GET", "/api/v1/hosts")
        assert result == {"ok": True}
        await c.close()

    @pytest.mark.asyncio
    async def test_connection_error_retries(self):
        import httpx

        c = _make_client(max_retries=2)
        await c._ensure_client()
        c._client.request = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ElderConnectionError, match="Connection failed"):
                await c._request("GET", "/api/v1/health")
        assert c._client.request.call_count == 2
        await c.close()

    @pytest.mark.asyncio
    async def test_timeout_error_retries(self):
        import httpx

        c = _make_client(max_retries=2)
        await c._ensure_client()
        c._client.request = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ElderConnectionError, match="Connection failed"):
                await c._request("GET", "/api/v1/health")
        await c.close()


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {"status": "healthy"}))
        assert await c.health_check() is True
        await c.close()

    @pytest.mark.asyncio
    async def test_health_check_not_healthy(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {"status": "degraded"}))
        assert await c.health_check() is False
        await c.close()

    @pytest.mark.asyncio
    async def test_health_check_connection_error_wraps(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(401))
        with pytest.raises(ElderConnectionError, match="Health check failed"):
            await c.health_check()
        await c.close()


# ---------------------------------------------------------------------------
# register_host
# ---------------------------------------------------------------------------


class TestRegisterHost:
    @pytest.mark.asyncio
    async def test_register_host_success(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(201, {"id": "h1"}))
        result = await c.register_host(hostname="h1", ip="10.0.0.1", fqdn="h1.local")
        assert result == {"id": "h1"}
        await c.close()

    @pytest.mark.asyncio
    async def test_register_host_with_optional_fields(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(201, {"id": "h2"}))
        result = await c.register_host(
            hostname="h2",
            ip="10.0.0.2",
            fqdn="h2.local",
            apps=["myapp"],
            zone="us-east",
            pool="compute",
            tags={"env": "prod"},
            metadata={"rack": "R1"},
        )
        assert result == {"id": "h2"}
        # Verify POST body includes all fields
        call_kwargs = c._client.request.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["hostname"] == "h2"
        assert body["apps"] == ["myapp"]
        assert body["zone"] == "us-east"
        await c.close()

    @pytest.mark.asyncio
    async def test_register_host_conflict_triggers_update(self):
        """409 on register → should fall back to update_host (PUT)."""
        c = _make_client()
        # Patch update_host directly to avoid cascading _request mocks
        with patch.object(c, "update_host", new_callable=AsyncMock) as mock_update:
            mock_update.return_value = {"updated": True}
            c._ensure_client = AsyncMock()
            c._request = AsyncMock(side_effect=ElderConflictError("conflict"))
            result = await c.register_host(hostname="h1", ip="10.0.0.1", fqdn="h1.local")
        assert result == {"updated": True}
        mock_update.assert_called_once()


# ---------------------------------------------------------------------------
# update_host
# ---------------------------------------------------------------------------


class TestUpdateHost:
    @pytest.mark.asyncio
    async def test_update_host_success(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {"updated": True}))
        result = await c.update_host("h1", ip="10.0.0.99")
        assert result == {"updated": True}
        await c.close()

    @pytest.mark.asyncio
    async def test_update_host_not_found_triggers_register(self):
        """404 on update with ip+fqdn available → fall back to register_host."""
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(
            side_effect=[
                _make_response(404),
                _make_response(201, {"registered": True}),
            ]
        )
        result = await c.update_host("h1", ip="10.0.0.1", fqdn="h1.local")
        assert result == {"registered": True}
        await c.close()

    @pytest.mark.asyncio
    async def test_update_host_not_found_without_ip_reraises(self):
        """404 on update without ip to register → reraise NotFoundError."""
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(404))
        with pytest.raises(ElderNotFoundError):
            await c.update_host("h1")
        await c.close()


# ---------------------------------------------------------------------------
# deregister_host
# ---------------------------------------------------------------------------


class TestDeregisterHost:
    @pytest.mark.asyncio
    async def test_deregister_success(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {"deleted": True}))
        result = await c.deregister_host("h1")
        assert result == {"deleted": True}
        await c.close()

    @pytest.mark.asyncio
    async def test_deregister_not_found_returns_message(self):
        """404 on deregister → return already-removed message (idempotent)."""
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(404))
        result = await c.deregister_host("h-gone")
        assert "already removed" in result["message"].lower()
        await c.close()


# ---------------------------------------------------------------------------
# register_app
# ---------------------------------------------------------------------------


class TestRegisterApp:
    @pytest.mark.asyncio
    async def test_register_app_success(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(201, {"app": "myapp"}))
        result = await c.register_app("myapp", ["h1"], 8080)
        assert result == {"app": "myapp"}
        await c.close()

    @pytest.mark.asyncio
    async def test_register_app_conflict_triggers_update(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(
            side_effect=[
                _make_response(409),
                _make_response(200, {"updated": True}),
            ]
        )
        result = await c.register_app("myapp", ["h1"], 8080)
        assert result == {"updated": True}
        await c.close()

    @pytest.mark.asyncio
    async def test_register_app_with_all_fields(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(201, {}))
        await c.register_app(
            "myapp", ["h1", "h2"], 9090,
            protocol="grpc",
            path="/grpc",
            health_check_url="http://h1:9090/health",
            priority=50,
        )
        call_kwargs = c._client.request.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["protocol"] == "grpc"
        assert body["priority"] == 50
        await c.close()


# ---------------------------------------------------------------------------
# sync_machine
# ---------------------------------------------------------------------------


class TestSyncMachine:
    @pytest.mark.asyncio
    async def test_sync_machine_success(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {"synced": True}))
        machine = {
            "system_id": "abc-123",
            "hostname": "node1",
            "ip_address": "10.0.0.1",
            "status": "deployed",
        }
        result = await c.sync_machine(machine)
        assert result == {"synced": True}
        await c.close()

    @pytest.mark.asyncio
    async def test_sync_machine_uses_external_id_fallback(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {"synced": True}))
        machine = {"external_id": "ext-456", "ip_address": "10.0.0.2"}
        result = await c.sync_machine(machine)
        assert result == {"synced": True}
        call_kwargs = c._client.request.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["machine_id"] == "ext-456"
        assert body["hostname"].startswith("machine-")
        await c.close()

    @pytest.mark.asyncio
    async def test_sync_machine_error_reraises(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(401))
        with pytest.raises(ElderError):
            await c.sync_machine({"system_id": "x"})
        await c.close()

    @pytest.mark.asyncio
    async def test_sync_machine_includes_all_metadata(self):
        c = _make_client()
        await c._ensure_client()
        c._client.request = AsyncMock(return_value=_make_response(200, {}))
        machine = {
            "system_id": "m1",
            "hostname": "server1",
            "ip_address": "10.1.0.1",
            "status": "ready",
            "machine_type": "baremetal",
            "architecture": "arm64",
            "cpu_count": 16,
            "memory_mb": 65536,
            "storage_gb": 1000,
            "zone": "zone-a",
            "tags": {"rack": "A1"},
            "metadata": {"bmc": "10.0.0.100"},
        }
        await c.sync_machine(machine)
        call_kwargs = c._client.request.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["machine_type"] == "baremetal"
        assert body["architecture"] == "arm64"
        assert body["cpu_count"] == 16
        assert body["tags"] == {"rack": "A1"}
        await c.close()


# ---------------------------------------------------------------------------
# get_elder_client
# ---------------------------------------------------------------------------


class TestGetElderClient:
    @pytest.mark.asyncio
    async def test_returns_none_when_table_missing(self):
        db = MagicMock()
        db.tables = []  # no elder_config table
        result = await get_elder_client(db)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_config(self):
        db = MagicMock()
        db.tables = ["elder_config"]
        db.elder_config = MagicMock()
        db.return_value.select.return_value.first.return_value = None
        result = await get_elder_client(db)
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_when_config_incomplete(self):
        db = MagicMock()
        db.tables = ["elder_config"]
        config = MagicMock()
        config.elder_url = ""  # missing URL
        config.api_key = "key"
        db.return_value.select.return_value.first.return_value = config
        with pytest.raises(ElderError, match="incomplete"):
            await get_elder_client(db)

    @pytest.mark.asyncio
    async def test_returns_client_when_config_valid(self):
        db = MagicMock()
        db.tables = ["elder_config"]
        config = MagicMock()
        config.elder_url = "http://elder.local"
        config.api_key = "my-api-key"
        config.timeout = "15.0"
        config.max_retries = "5"
        db.return_value.select.return_value.first.return_value = config
        client = await get_elder_client(db)
        assert client is not None
        assert isinstance(client, ElderClient)
        assert client.elder_url == "http://elder.local"
        assert client.api_key == "my-api-key"
        assert client.timeout == 15.0
        assert client.max_retries == 5
