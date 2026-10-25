"""Regression tests for the route audit of the routes first enabled by 385dd0f.

Each test pins one defect found by the audit: cross-tenant reads/writes, SSRF through
request-supplied URLs, stub success responses and gate-parity drift.
"""

from __future__ import annotations

import importlib
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart, g


def _passthrough(*dargs, **dkwargs):
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


def _tenant_app(tenant: str, *, cross_tenant: bool = False) -> Quart:
    """A bare Quart app whose requests carry the given validated tenant context."""
    app = Quart(__name__)

    @app.before_request
    async def _inject() -> None:
        g.current_user = {"id": 1, "email": "a@b.c", "_jwt_payload": {"tenant": tenant}}
        g.tenant_context = SimpleNamespace(tenant_id=tenant, cross_tenant=cross_tenant)

    return app


class TestTenantHelpers:
    """tenant_owns / resolve_node_id_by_name: the shared isolation primitives."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("row_tenant", "cross", "expected"),
        [
            ("acme", False, True),
            ("globex", False, False),
            (None, False, False),
            ("", False, False),
            (None, True, True),
            ("globex", True, True),
        ],
    )
    async def test_tenant_owns(self, row_tenant, cross, expected) -> None:
        from app.api._helpers import tenant_owns

        app = _tenant_app("acme", cross_tenant=cross)
        async with app.test_request_context("/"):
            await app.preprocess_request()
            assert tenant_owns(row_tenant) is expected

    def test_resolve_node_id_by_name_is_tenant_scoped_and_fails_closed(
        self, tmp_path
    ) -> None:
        from app.api._helpers import resolve_node_id_by_name
        from penguin_dal import DB, Field

        db = DB(
            f"sqlite:///{tmp_path}/n-{threading.get_ident()}.db",
            pool_size=1,
            reflect=False,
            migrate=True,
        )
        db.define_table(
            "nodes", Field("name", "string"), Field("tenant_id", "string"), migrate=True
        )
        a = db.nodes.insert(name="host-a", tenant_id="acme")
        db.nodes.insert(name="dup", tenant_id="acme")
        db.nodes.insert(name="dup", tenant_id="globex")
        db.commit()
        assert resolve_node_id_by_name(db, "host-a", "acme") == a
        assert resolve_node_id_by_name(db, "host-a", "globex") is None  # foreign tenant
        assert (
            resolve_node_id_by_name(db, "dup", None) is None
        )  # ambiguous across tenants
        assert resolve_node_id_by_name(db, "dup", "acme") is not None
        assert resolve_node_id_by_name(db, "ghost", "acme") is None


class TestSecretsTenantNamespace:
    """regression: /api/v1/secrets was one platform-wide keyspace reachable by any tenant admin."""

    @pytest.fixture()
    def secrets_client(self, monkeypatch):
        import app.middleware as mw_mod

        monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
        monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
        sys.modules.pop("app.api.secrets", None)
        mod = importlib.import_module("app.api.secrets")
        app = _tenant_app("acme")
        app.url_map.strict_slashes = False
        app.register_blueprint(mod.secrets_bp)
        return app, mod

    @pytest.mark.asyncio
    async def test_tenant_admin_is_confined_to_its_namespace(
        self, secrets_client
    ) -> None:
        app, mod = secrets_client
        manager = AsyncMock()
        manager.get_secret = AsyncMock(return_value={"k": "v"})
        with patch.object(mod, "get_secrets_manager", return_value=manager):
            resp = await app.test_client().get("/cloud/aws/credentials")
        assert resp.status_code == 200
        manager.get_secret.assert_awaited_once_with(
            "tenants/acme/cloud/aws/credentials"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["../globex/x", "a/../../b", "a//b", "a/./b"])
    async def test_traversal_rejected_before_the_backend(
        self, secrets_client, path
    ) -> None:
        app, mod = secrets_client
        manager = AsyncMock()
        with patch.object(mod, "get_secrets_manager", return_value=manager):
            for method in ("get", "put", "delete"):
                resp = await getattr(app.test_client(), method)(
                    f"/{path}",
                    **({"json": {"data": {"a": 1}}} if method == "put" else {}),
                )
                assert resp.status_code in (400, 404), (method, path, resp.status_code)
        manager.get_secret.assert_not_called()
        manager.set_secret.assert_not_called()
        manager.delete_secret.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_tenant_super_admin_addresses_raw_paths(
        self, monkeypatch
    ) -> None:
        import app.middleware as mw_mod

        monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
        monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
        sys.modules.pop("app.api.secrets", None)
        mod = importlib.import_module("app.api.secrets")
        app = _tenant_app("system", cross_tenant=True)
        app.register_blueprint(mod.secrets_bp)
        manager = AsyncMock()
        manager.get_secret = AsyncMock(return_value={})
        with patch.object(mod, "get_secrets_manager", return_value=manager):
            await app.test_client().get("/platform/db")
        manager.get_secret.assert_awaited_once_with("platform/db")


class TestSsrfGuards:
    """regression: request/admin-supplied URLs reached outbound fetches unvetted."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url", ["https://127.0.0.1:8443", "https://169.254.169.254", "ftp://x"]
    )
    async def test_cloud_provider_endpoint_refused(self, url) -> None:
        from app.api.clouds import _refuse_unsafe_endpoint

        assert await _refuse_unsafe_endpoint({"MAAS_API_URL": url}) is not None
        assert await _refuse_unsafe_endpoint({"LXD_API_URL": url}) is not None

    @pytest.mark.asyncio
    async def test_cloud_provider_without_url_keys_passes(self) -> None:
        from app.api.clouds import _refuse_unsafe_endpoint

        assert await _refuse_unsafe_endpoint({"aws_region": "us-east-1"}) is None

    @pytest.mark.asyncio
    async def test_storage_endpoint_refused_before_client_build(self) -> None:
        from app.services import storage as svc

        row = SimpleNamespace(
            id=1,
            name="s",
            provider_type="minio",
            endpoint_url="http://169.254.169.254",
            region=None,
            bucket_name="b",
            credentials_path="p",
            is_default=True,
            is_active=True,
            use_ssl=False,
            config_data=None,
            created_by=1,
            created_at=None,
            updated_at=None,
        )
        db = MagicMock()
        db.return_value.select.return_value.first.return_value = row
        with patch.object(svc, "get_db", return_value=db), pytest.raises(
            svc.StorageValidationError
        ):
            await svc.get_storage_service(config_id=1)

    @pytest.mark.asyncio
    async def test_elder_url_refused_on_write(self, monkeypatch) -> None:
        import app.api.ipxe as ipxe_mod

        app = _tenant_app("acme")
        monkeypatch.setattr(ipxe_mod, "admin_required", _passthrough, raising=False)
        # Call the underlying coroutine through a request context, bypassing decorators.
        fn = ipxe_mod.update_elder_config
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        async with app.test_request_context(
            "/", method="PUT", json={"elder_url": "http://127.0.0.1:9", "api_key": "k"}
        ):
            body, status = await fn()
        assert status == 400


class TestPrimaryFrontendSwitchSsrf:
    """regression: frontend-switch external mode TCP-connected to any host:port from input."""

    @pytest.mark.asyncio
    async def test_loopback_endpoint_rejected_without_connecting(
        self, monkeypatch
    ) -> None:
        import app.api.primary as primary_mod

        app = _tenant_app("acme")
        monkeypatch.setattr(primary_mod, "get_db", lambda: MagicMock())
        fn = primary_mod.switch_frontend
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        with patch("socket.create_connection") as conn:
            async with app.test_request_context(
                "/",
                method="POST",
                json={"target_mode": "external", "new_endpoint": "127.0.0.1:22"},
            ):
                body, status = await fn()
        assert status == 422
        conn.assert_not_called()


class TestIpxePowerNotFaked:
    """regression: power endpoint returned 'initiated' without any power driver behind it."""

    @pytest.mark.asyncio
    async def test_power_returns_501_and_logs_nothing(self) -> None:
        import app.api.ipxe as ipxe_mod

        app = _tenant_app("acme")
        machine = {
            "id": 1,
            "system_id": "m1",
            "mac_address": "aa:bb:cc:dd:ee:ff",
            "power_type": "ipmi",
            "status": "ready",
        }
        fn = ipxe_mod.power_control
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        with patch.object(
            ipxe_mod, "_get_machine_by_id", return_value=machine
        ), patch.object(ipxe_mod, "_log_boot_event") as logged:
            async with app.test_request_context("/", method="POST"):
                resp, status = await fn("1", "on")
        assert status == 501
        logged.assert_not_called()


def _secrets_app_for(tenant: str, monkeypatch):
    import app.middleware as mw_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
    sys.modules.pop("app.api.secrets", None)
    mod = importlib.import_module("app.api.secrets")
    app = _tenant_app(tenant)
    app.url_map.strict_slashes = False
    app.register_blueprint(mod.secrets_bp)
    return app, mod


def _rebinding_resolver(monkeypatch, answers: list[str]) -> list[str]:
    """Make the event loop's resolver return ``answers`` in order (one per lookup), like a rebinding DNS."""
    import asyncio

    seen: list[str] = []
    queue = list(answers)

    async def fake_getaddrinfo(self, host, port, *a, **kw):
        seen.append(host)
        ip = queue.pop(0) if len(queue) > 1 else queue[0]
        return [(2, 1, 6, "", (ip, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
    return seen


class TestDnsRebinding:
    """regression: a hostname that resolves publicly at check time and to loopback at use time."""

    @pytest.mark.asyncio
    async def test_elder_connects_to_the_vetted_ip_never_re_resolving(
        self, monkeypatch
    ) -> None:
        import httpx
        from app.integrations.elder import get_elder_client

        _rebinding_resolver(
            monkeypatch, ["93.184.216.34", "127.0.0.1"]
        )  # 2nd answer must never be used
        db = MagicMock()
        db.tables = ["elder_config"]
        cfg = MagicMock()
        cfg.elder_url = "https://elder.example.test:8443"
        cfg.api_key = "k"
        cfg.timeout = "5"
        cfg.max_retries = "1"
        db.return_value.select.return_value.first.return_value = cfg
        client = await get_elder_client(db)
        assert client.pinned_ip == "93.184.216.34"

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["host"] = request.url.host
            captured["host_header"] = request.headers["host"]
            captured["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, json={"ok": True})

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await client._request("GET", "/api/v1/health")
        assert captured == {
            "host": "93.184.216.34",
            "host_header": "elder.example.test:8443",
            "sni": "elder.example.test",
        }

    @pytest.mark.asyncio
    async def test_elder_use_time_vet_refuses_when_dns_now_points_at_loopback(
        self, monkeypatch
    ) -> None:
        from app.integrations.elder import ElderError, get_elder_client

        _rebinding_resolver(monkeypatch, ["127.0.0.1"])
        db = MagicMock()
        db.tables = ["elder_config"]
        cfg = MagicMock()
        cfg.elder_url = "https://elder.example.test"
        cfg.api_key = "k"
        db.return_value.select.return_value.first.return_value = cfg
        with pytest.raises(ElderError):
            await get_elder_client(db)

    @pytest.mark.asyncio
    async def test_stored_cloud_config_is_revetted_on_every_build(
        self, monkeypatch
    ) -> None:
        """Stored config_data passed write-time vetting once; a later rebind to loopback is caught."""
        from app.api import clouds as clouds_mod
        from app.clouds import CloudError

        built: list = []
        monkeypatch.setattr(
            clouds_mod, "get_cloud_provider", lambda t, c: built.append(c) or object()
        )
        cfg = {"MAAS_API_URL": "https://maas.example.test:5240/MAAS"}

        _rebinding_resolver(monkeypatch, ["93.184.216.34"])
        await clouds_mod._build_provider("maas", cfg)  # first lookup: public -> allowed
        assert built == [cfg]

        _rebinding_resolver(
            monkeypatch, ["127.0.0.1"]
        )  # DNS flipped after the first check
        with pytest.raises(CloudError):
            await clouds_mod._build_provider("maas", cfg)
        assert built == [cfg]  # the SDK was NOT constructed for the rebound lookup

    @pytest.mark.asyncio
    async def test_storage_use_time_vet_refuses_rebound_endpoint(
        self, monkeypatch
    ) -> None:
        from app.services import storage as svc

        _rebinding_resolver(monkeypatch, ["169.254.169.254"])
        row = SimpleNamespace(
            id=1,
            name="s",
            provider_type="minio",
            endpoint_url="http://minio.example.test:9000",
            region=None,
            bucket_name="b",
            credentials_path="p",
            is_default=True,
            is_active=True,
            use_ssl=False,
            config_data=None,
            created_by=1,
            created_at=None,
            updated_at=None,
        )
        db = MagicMock()
        db.return_value.select.return_value.first.return_value = row
        with patch.object(svc, "get_db", return_value=db), pytest.raises(
            svc.StorageValidationError
        ):
            await svc.get_storage_service(config_id=1)


class TestSecretsListingPrefix:
    """regression: listing must not leak a sibling tenant whose id shares a prefix."""

    @pytest.mark.asyncio
    async def test_tenant_a_does_not_see_tenant_ab(self, monkeypatch) -> None:
        app, mod = _secrets_app_for("a", monkeypatch)
        manager = AsyncMock()
        # A sloppy backend returns everything under a string prefix, plus an unrelated key.
        manager.list_secrets = AsyncMock(
            return_value=[
                "tenants/a/x",
                "tenants/ab/secret",
                "platform/db",
                "tenants/a/y/z",
            ]
        )
        with patch.object(mod, "get_secrets_manager", return_value=manager):
            resp = await app.test_client().get("/?path=")
        manager.list_secrets.assert_awaited_once_with("tenants/a/")
        body = await resp.get_json()
        assert body["secrets"] == ["x", "y/z"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tenant", ["a/b", "..", "a/../b", "", "x y", "a\\b"])
    async def test_malformed_tenant_id_never_reaches_backend(
        self, monkeypatch, tenant
    ) -> None:
        app, mod = _secrets_app_for(tenant, monkeypatch)
        manager = AsyncMock()
        with patch.object(mod, "get_secrets_manager", return_value=manager):
            resp = await app.test_client().get("/some/path")
            listing = await app.test_client().get("/?path=")
        assert resp.status_code == 400
        assert listing.status_code == 400
        manager.get_secret.assert_not_called()
        manager.list_secrets.assert_not_called()
