"""iPXE / Elder write-path hardening (security review of 385dd0f).

* PUT /api/v1/ipxe/elder/config: https-only, SSRF-vetted ``elder_url``
  (loopback / link-local / metadata / unspecified, incl. a hostname that
  resolves to one), type-checked ``timeout`` / ``max_retries``.
* iPXE config / boot-config / image writes: input type checks, allow-listed
  response DTOs, and an audit record per change that names fields, never values.
"""

from __future__ import annotations

import logging
import re
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from quart import Quart, g

import app.api.ipxe as ipxe_mod
from app.audit import AuditEventType
from app.integrations import elder_egress

ELDER_REFUSED = "elder_url is not an allowed destination"


def _resolve_to(monkeypatch: pytest.MonkeyPatch, addresses: list[str]) -> None:
    """Make the Elder egress guard resolve every hostname to ``addresses``."""

    async def _fake(host: str, port: int) -> list[str]:
        return list(addresses)

    monkeypatch.setattr(elder_egress, "_resolve_host", _fake)


@pytest.fixture(autouse=True)
def _no_elder_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with the default (empty) Elder allowlist."""
    monkeypatch.delenv(elder_egress.ELDER_ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.delenv(elder_egress.ELDER_ALLOWED_CIDRS_ENV, raising=False)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Capture audit-trail writes made by the iPXE module."""
    logger = MagicMock()
    monkeypatch.setattr(ipxe_mod, "get_audit_logger", lambda: logger)
    return logger


@pytest.fixture
def client() -> Any:
    """iPXE blueprint with an admin principal injected."""
    app = Quart(__name__)
    app.register_blueprint(ipxe_mod.ipxe_bp, url_prefix="/api/v1/ipxe")

    @app.before_request
    async def _admin() -> None:
        g.current_user = {
            "id": 42,
            "role": "admin",
            "_jwt_payload": {"scope": "gough.cluster.superadmin"},
        }

    return app.test_client()


def _elder_db(existing: Any, refetched: Any) -> MagicMock:
    """``db`` mock for the Elder handler: existence check, then post-write refetch."""
    db = MagicMock()
    db.tables = ["elder_config"]
    db.elder_config.insert.return_value = 1
    db.return_value.select.return_value.first.side_effect = [existing, refetched]
    return db


def _elder_row(**over: Any) -> SimpleNamespace:
    """A stored elder_config row."""
    row = {
        "id": 1,
        "name": "default",
        "elder_url": "https://elder.corp.example",
        "api_key": "elder-secret",
        "timeout": 10,
        "max_retries": 3,
        "is_active": True,
        "last_sync_at": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    row.update(over)
    return SimpleNamespace(**row)


def _elder_body(**over: Any) -> dict[str, Any]:
    """A valid Elder config body."""
    body: dict[str, Any] = {
        "elder_url": "https://elder.corp.example",
        "api_key": "elder-secret",
    }
    body.update(over)
    return body


class TestElderUrlSsrf:
    """The Elder URL is fetched server-side, so it is https-only and SSRF-vetted."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://169.254.169.254/latest/meta-data",
            "https://0.0.0.0/",
            "https://[::ffff:127.0.0.1]/",
            "https://[fe80::1]/",
        ],
    )
    async def test_blocked_literal_addresses_rejected(
        self, client: Any, audit: MagicMock, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        """Loopback, link-local/metadata, unspecified and mapped-loopback literals are refused."""
        db = MagicMock()
        monkeypatch.setattr(ipxe_mod, "get_db", lambda: db)
        resp = await client.put(
            "/api/v1/ipxe/elder/config", json=_elder_body(elder_url=url)
        )
        assert resp.status_code == 400
        assert (await resp.get_json())["error"] == ELDER_REFUSED
        db.elder_config.insert.assert_not_called()
        audit.log.assert_not_called()

    async def test_rebinding_hostname_rejected(
        self, client: Any, audit: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hostname whose answer set includes a blocked address is refused."""
        _resolve_to(monkeypatch, ["203.0.113.10", "127.0.0.1"])
        monkeypatch.setattr(ipxe_mod, "get_db", MagicMock)
        resp = await client.put(
            "/api/v1/ipxe/elder/config",
            json=_elder_body(elder_url="https://rebind.attacker.example"),
        )
        assert resp.status_code == 400
        assert (await resp.get_json())["error"] == ELDER_REFUSED

    @pytest.mark.parametrize(
        "url",
        [
            "http://elder.corp.example",
            "ftp://elder.corp.example",
            "file:///etc/passwd",
            "elder.corp.example",
        ],
    )
    async def test_non_https_rejected(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        """Only https is accepted (checked before any DNS lookup)."""
        _resolve_to(monkeypatch, ["203.0.113.10"])
        resp = await client.put(
            "/api/v1/ipxe/elder/config", json=_elder_body(elder_url=url)
        )
        assert resp.status_code == 400
        assert "scheme" in (await resp.get_json())["error"]

    async def test_embedded_credentials_rejected(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user:pass@host URLs are refused so credentials are never stored in the URL."""
        _resolve_to(monkeypatch, ["203.0.113.10"])
        resp = await client.put(
            "/api/v1/ipxe/elder/config",
            json=_elder_body(elder_url="https://u:p@elder.corp.example"),
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "over",
        [
            {"timeout": "10"},
            {"timeout": True},
            {"timeout": 0},
            {"timeout": 121},
            {"max_retries": -1},
            {"max_retries": 11},
            {"max_retries": "3"},
            {"max_retries": 2.5},
            {"is_active": "yes"},
            {"api_key": 123},
        ],
    )
    async def test_type_checks(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, over: dict[str, Any]
    ) -> None:
        """timeout/max_retries must be bounded ints, is_active a bool, api_key a string."""
        _resolve_to(monkeypatch, ["203.0.113.10"])
        resp = await client.put("/api/v1/ipxe/elder/config", json=_elder_body(**over))
        assert resp.status_code == 400

    async def test_valid_config_is_stored_and_audited(
        self, client: Any, audit: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A public https URL is accepted, stored, and audited without the api_key value."""
        _resolve_to(monkeypatch, ["10.20.0.5"])
        # Private on-prem Elder: allowed only via the explicit operator allowlist.
        monkeypatch.setenv(elder_egress.ELDER_ALLOWED_CIDRS_ENV, "10.20.0.0/16")
        db = _elder_db(existing=None, refetched=_elder_row())
        monkeypatch.setattr(ipxe_mod, "get_db", lambda: db)
        resp = await client.put(
            "/api/v1/ipxe/elder/config", json=_elder_body(timeout=15, max_retries=2)
        )
        assert resp.status_code == 201
        stored = db.elder_config.insert.call_args.kwargs
        assert stored["timeout"] == 15 and stored["max_retries"] == 2
        audit.log.assert_called_once()
        kwargs = audit.log.call_args.kwargs
        assert kwargs["event_type"] is AuditEventType.SYSTEM_CONFIG_CHANGE
        assert kwargs["resource_type"] == "elder_config"
        assert kwargs["user_id"] == 42
        assert "api_key" in kwargs["details"]["fields"]
        assert "elder-secret" not in repr(kwargs)


class TestElderUrlErrorOracle:
    """A refused elder_url returns one fixed message; detail stays server-side.

    Regression: security review of PR #52 (error-echo oracle).
    """

    async def test_resolver_error_not_echoed(
        self,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Resolver failure text never reaches the response; it is logged at WARNING."""

        async def _boom(host: str, port: int) -> list[str]:
            raise socket.gaierror(-3, "Temporary failure in name resolution ZZQ")

        monkeypatch.setattr(elder_egress, "_resolve_host", _boom)
        monkeypatch.setattr(ipxe_mod, "get_db", MagicMock)
        with caplog.at_level(logging.WARNING):
            resp = await client.put(
                "/api/v1/ipxe/elder/config",
                json=_elder_body(elder_url="https://probe.internal.example"),
            )
        raw = await resp.get_data(as_text=True)
        assert resp.status_code == 400
        assert (await resp.get_json()) == {"error": ELDER_REFUSED}
        assert (
            "ZZQ" not in raw and "resolution" not in raw and "probe.internal" not in raw
        )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("ZZQ" in r.getMessage() for r in warnings)
        assert all("elder-secret" not in r.getMessage() for r in caplog.records)

    async def test_resolved_ip_not_echoed(
        self,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An internal address the name resolved to is never disclosed to the caller."""
        _resolve_to(monkeypatch, ["10.9.8.7"])
        monkeypatch.setattr(ipxe_mod, "get_db", MagicMock)
        with caplog.at_level(logging.WARNING):
            resp = await client.put(
                "/api/v1/ipxe/elder/config",
                json=_elder_body(elder_url="https://probe.internal.example"),
            )
        raw = await resp.get_data(as_text=True)
        assert resp.status_code == 400
        assert (await resp.get_json()) == {"error": ELDER_REFUSED}
        assert "10.9.8.7" not in raw
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", raw)
        assert any("10.9.8.7" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "url",
        ["https://elder.example.com:99999/", "https://elder.example.com:abc/"],
    )
    async def test_bad_port_returns_fixed_message(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        """A port that fails to parse (ValueError) gets the same fixed 400, not a 500."""
        _resolve_to(monkeypatch, ["93.184.216.34"])
        monkeypatch.setattr(ipxe_mod, "get_db", MagicMock)
        resp = await client.put(
            "/api/v1/ipxe/elder/config", json=_elder_body(elder_url=url)
        )
        assert resp.status_code == 400
        assert (await resp.get_json()) == {"error": ELDER_REFUSED}


class TestIpxeWriteAudit:
    """Every iPXE configuration change lands in the audit trail."""

    async def test_config_create_audited_with_dto(
        self, client: Any, audit: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PUT /config (create branch) audits and returns only allow-listed fields."""
        row = SimpleNamespace(
            id=5,
            name="lab",
            dhcp_mode="proxy",
            is_active=True,
            internal_secret="x",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=None,
        )
        db = MagicMock()
        db.ipxe_config.insert.return_value = 5
        db.return_value.select.return_value.first.side_effect = [None, row]
        monkeypatch.setattr(ipxe_mod, "get_db", lambda: db)
        resp = await client.put(
            "/api/v1/ipxe/config",
            json={"name": "lab", "chain_url": "http://boot.lab.example/chain"},
        )
        assert resp.status_code == 201
        body = await resp.get_json()
        assert "internal_secret" not in body
        assert set(body) <= set(ipxe_mod._IPXE_CONFIG_FIELDS)
        kwargs = audit.log.call_args.kwargs
        assert kwargs["resource_type"] == "ipxe_config"
        assert kwargs["details"]["action"] == "create"
        assert "chain_url" in kwargs["details"]["fields"]

    async def test_boot_config_delete_audited(
        self, client: Any, audit: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DELETE /boot-configs/<id> audits the deletion."""
        monkeypatch.setattr(
            ipxe_mod, "_get_boot_config_by_id", lambda cid: {"id": cid, "name": "c"}
        )
        db = MagicMock()
        db.return_value.count.return_value = 0
        monkeypatch.setattr(ipxe_mod, "get_db", lambda: db)
        resp = await client.delete("/api/v1/ipxe/boot-configs/3")
        assert resp.status_code == 200
        kwargs = audit.log.call_args.kwargs
        assert (
            kwargs["resource_type"],
            kwargs["resource_id"],
            kwargs["details"]["action"],
        ) == (
            "ipxe_boot_config",
            "3",
            "delete",
        )

    async def test_image_update_audited(
        self, client: Any, audit: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PUT /images/<id> audits field names and returns the image DTO."""
        monkeypatch.setattr(
            ipxe_mod, "_get_image_by_id", lambda iid: {"id": iid, "name": "jammy"}
        )
        db = MagicMock()
        db.return_value.select.return_value.first.return_value = SimpleNamespace(
            id=9,
            name="jammy",
            display_name="Jammy",
            is_active=False,
            created_at=None,
            updated_at=None,
        )
        monkeypatch.setattr(ipxe_mod, "get_db", lambda: db)
        resp = await client.put("/api/v1/ipxe/images/9", json={"is_active": False})
        assert resp.status_code == 200
        assert (await resp.get_json())["id"] == 9
        assert audit.log.call_args.kwargs["details"]["fields"] == ["is_active"]


class TestIpxeWriteValidation:
    """Malformed write bodies are rejected before any DB write."""

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "lab", "chain_url": "file:///etc/passwd"},
            {"name": "lab", "http_boot_url": "javascript:alert(1)"},
            {"name": "lab", "dhcp_mode": "rogue"},
            {"name": "lab", "dns_servers": ["not-an-ip"]},
            {"name": "lab", "dhcp_gateway": "999.1.1.1"},
            {"name": "lab", "tftp_enabled": "true"},
        ],
    )
    async def test_config_rejects_bad_input(
        self,
        client: Any,
        audit: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        body: dict[str, Any],
    ) -> None:
        """Bad URLs, enums, addresses and booleans are 400s with no write and no audit."""
        db = MagicMock()
        monkeypatch.setattr(ipxe_mod, "get_db", lambda: db)
        resp = await client.put("/api/v1/ipxe/config", json=body)
        assert resp.status_code == 400
        db.ipxe_config.insert.assert_not_called()
        audit.log.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "c", "timeout_seconds": "30"},
            {"name": "c", "timeout_seconds": 0},
            {"name": "c", "boot_order": "disk"},
            {"name": "c", "default_image_id": "1"},
            {"name": "c", "is_default": 1},
        ],
    )
    async def test_boot_config_rejects_bad_input(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
    ) -> None:
        """Boot-config numeric/list/bool fields are type-checked."""
        db = MagicMock()
        monkeypatch.setattr(ipxe_mod, "get_db", lambda: db)
        resp = await client.post("/api/v1/ipxe/boot-configs", json=body)
        assert resp.status_code == 400
        db.ipxe_boot_configs.insert.assert_not_called()

    async def test_image_rejects_negative_size(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """size_bytes must be a non-negative int."""
        monkeypatch.setattr(
            ipxe_mod, "_get_image_by_id", lambda iid: {"id": iid, "name": "jammy"}
        )
        monkeypatch.setattr(ipxe_mod, "get_db", MagicMock)
        resp = await client.put("/api/v1/ipxe/images/9", json={"size_bytes": -1})
        assert resp.status_code == 400
