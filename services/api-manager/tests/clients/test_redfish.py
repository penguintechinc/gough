"""Tests for app.clients.redfish.

Covers cert-pinning TOFU + mismatch, default-credential refusal + NATS event,
session caching + 401 retry, all power/boot/sensor/SEL/firmware/BIOS/capability
operations, audit-chain emission, error mapping, and helpers.

The cert-pinning adapter performs a raw TLS handshake to capture the BMC's
fingerprint. We monkeypatch ``socket.create_connection`` and
``ssl.SSLContext.wrap_socket`` to deliver a deterministic DER blob without any
real network IO.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
import requests
import requests_mock as rm_module

from app.clients import redfish as redfish_mod
from app.clients.redfish import (
    BMCAuthFailure,
    BMCCertPinMismatch,
    BMCDefaultCredentialsDetected,
    BMCFirmwareUpdateFailed,
    BMCOperationFailed,
    BMCUnreachable,
    DEFAULT_CREDENTIAL_PAIRS,
    FirmwareComponent,
    RedfishClient,
    SelEntry,
    Sensor,
    _detect_vendor_string,
    _fingerprint_from_der,
    _get_in,
    _normalize_fingerprint,
    _safe_iter,
    _to_float,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNodeBmc:
    """Minimal stand-in for the ``NodeBmc`` ORM row."""

    def __init__(
        self,
        node_id: int = 42,
        cert_fingerprint: Optional[str] = None,
        session_ttl_sec: int = 1800,
    ) -> None:
        self.node_id = node_id
        self.endpoint = "https://bmc.test"
        self.username_ref = "secret/gough/c1/nodes/42/bmc#username"
        self.password_ref = "secret/gough/c1/nodes/42/bmc#password"
        self.cert_fingerprint = cert_fingerprint
        self.session_ttl_sec = session_ttl_sec
        self.capabilities: Any = None
        self.firmware_summary: Any = None
        self.factory_creds_detected = False


class _FakeVault:
    def __init__(self, mapping: dict[str, dict[str, str]]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def kv_read(self, path: str) -> Any:
        self.calls.append(path)
        return SimpleNamespace(data=self._mapping.get(path, {}))


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.deletes: list[str] = []

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def set(self, key: str, value: Any, ex: Optional[int] = None, nx: bool = False) -> Any:
        self.store[key] = value
        return True

    def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
            self.deletes.append(k)
        return n


class _FakeNats:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.events.append((subject, payload))


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, **kwargs: Any) -> Any:
        self.events.append(kwargs)
        return SimpleNamespace(id="audit-1")


# ---------------------------------------------------------------------------
# TLS-handshake patching
# ---------------------------------------------------------------------------


_DEFAULT_DER = b"-fake-der-cert-bytes-AAAA-"
_DEFAULT_FP = _fingerprint_from_der(_DEFAULT_DER)
_OTHER_DER = b"-fake-der-cert-bytes-OTHER-"
_OTHER_FP = _fingerprint_from_der(_OTHER_DER)


@pytest.fixture
def patch_tls_handshake(monkeypatch: pytest.MonkeyPatch):
    """Replace socket+ssl handshake with a deterministic DER provider."""

    state: dict[str, bytes] = {"der": _DEFAULT_DER, "fail": b""}

    class _FakeTLS:
        def __init__(self, der: bytes) -> None:
            self._der = der

        def __enter__(self) -> "_FakeTLS":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def getpeercert(self, binary_form: bool = False) -> bytes:
            return self._der if binary_form else b""

    class _FakeSocket:
        def __enter__(self) -> "_FakeSocket":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    def fake_create_connection(addr: Any, timeout: Any = None) -> _FakeSocket:
        if state["fail"]:
            raise OSError(state["fail"].decode("utf-8"))
        return _FakeSocket()

    def fake_wrap_socket(self: Any, sock: Any, server_hostname: str = "") -> _FakeTLS:
        return _FakeTLS(state["der"])

    monkeypatch.setattr(redfish_mod.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(redfish_mod.ssl.SSLContext, "wrap_socket", fake_wrap_socket)
    return state


@pytest.fixture
def vault() -> _FakeVault:
    return _FakeVault(
        {
            "secret/gough/c1/nodes/42/bmc": {
                "username": "gough_admin",
                "password": "operator-pw-1",
            }
        }
    )


@pytest.fixture
def node_bmc() -> _FakeNodeBmc:
    return _FakeNodeBmc()


@pytest.fixture
def audit() -> _FakeAudit:
    return _FakeAudit()


@pytest.fixture
def fake_nats() -> _FakeNats:
    return _FakeNats()


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


# ---------------------------------------------------------------------------
# Helper-function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_fingerprint_format(self) -> None:
        fp = _fingerprint_from_der(b"abc")
        assert len(fp) == 95
        assert fp == fp.upper()
        assert fp.count(":") == 31

    def test_normalize_fingerprint_round_trip(self) -> None:
        raw = hashlib.sha256(b"x").hexdigest()
        normalized = _normalize_fingerprint(raw)
        assert _normalize_fingerprint(normalized) == normalized

    def test_normalize_fingerprint_rejects_short(self) -> None:
        with pytest.raises(ValueError):
            _normalize_fingerprint("AA:BB")

    def test_safe_iter_skips_non_dicts(self) -> None:
        assert list(_safe_iter([{"a": 1}, "skip", None, {"b": 2}])) == [{"a": 1}, {"b": 2}]
        assert list(_safe_iter(None)) == []

    def test_to_float(self) -> None:
        assert _to_float(None) is None
        assert _to_float("3.5") == 3.5
        assert _to_float("nope") is None

    def test_get_in(self) -> None:
        assert _get_in({"a": {"b": {"c": 1}}}, ("a", "b", "c")) == 1
        assert _get_in({"a": 1}, ("a", "b")) is None
        assert _get_in(None, ("a",)) is None

    def test_detect_vendor_string(self) -> None:
        assert _detect_vendor_string({"Name": "iDRAC9"}) == "Dell"
        assert _detect_vendor_string({"Name": "iLO 6"}) == "HPE"
        assert _detect_vendor_string({"Name": "Supermicro X12"}) == "Supermicro"
        assert _detect_vendor_string({"Name": "OpenBMC"}) == "OpenBMC"
        assert _detect_vendor_string({"Name": "Lenovo XClarity"}) == "Lenovo"
        assert _detect_vendor_string({"Name": "WhiteboxAMI"}) == "WhiteboxAMI"
        assert _detect_vendor_string({}) is None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_empty_endpoint(self, vault, node_bmc) -> None:
        with pytest.raises(ValueError, match="endpoint is required"):
            RedfishClient("", vault, node_bmc)

    def test_rejects_non_https(self, vault, node_bmc) -> None:
        with pytest.raises(ValueError, match="must use https"):
            RedfishClient("http://bmc.test", vault, node_bmc)

    def test_session_cache_key(self, vault, node_bmc) -> None:
        client = RedfishClient("https://bmc.test", vault, node_bmc)
        assert client.session_cache_key == "bmc:42:session"


# ---------------------------------------------------------------------------
# Cert pinning
# ---------------------------------------------------------------------------


class TestCertPinning:
    def test_first_connect_captures_pin(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_nats, monkeypatch
    ) -> None:
        # TOFU capture with no pre-existing pin requires explicit operator
        # opt-in (see TestTOFUEnvGating); this test exercises that fresh
        # first-connect TOFU path.
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")
        with rm_module.Mocker() as m:
            # All default-cred probes fail (401)
            m.post("https://bmc.test/redfish/v1/SessionService/Sessions", status_code=401)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={"RedfishVersion": "1.13.0"})
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc,
                audit_writer=audit, nats_publisher=fake_nats,
            )
            fp = client.first_connect()
            assert fp == _DEFAULT_FP
            assert node_bmc.cert_fingerprint == _DEFAULT_FP
            assert any(e["action"] == "bmc.cert_pinned" for e in audit.events)

    def test_pin_mismatch_raises(self, patch_tls_handshake, vault, node_bmc) -> None:
        node_bmc.cert_fingerprint = _OTHER_FP  # pre-pinned to a different cert
        with rm_module.Mocker():
            client = RedfishClient("https://bmc.test", vault, node_bmc)
            with pytest.raises(BMCCertPinMismatch):
                client.first_connect()

    def test_pin_persists_across_calls(
        self, patch_tls_handshake, vault, node_bmc, audit, monkeypatch
    ) -> None:
        # Fresh TOFU capture on first_connect() requires the explicit opt-in.
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")
        with rm_module.Mocker() as m:
            m.post("https://bmc.test/redfish/v1/SessionService/Sessions", status_code=401)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc, audit_writer=audit
            )
            client.first_connect()
            # Mutate the live cert; subsequent connect should fail.
            patch_tls_handshake["der"] = _OTHER_DER
            with pytest.raises(BMCCertPinMismatch):
                client._request("GET", "/redfish/v1/")

    def test_unreachable_when_handshake_fails(
        self, patch_tls_handshake, vault, node_bmc
    ) -> None:
        patch_tls_handshake["fail"] = b"no route to host"
        with rm_module.Mocker():
            client = RedfishClient("https://bmc.test", vault, node_bmc)
            with pytest.raises(BMCUnreachable):
                client.first_connect()

    def test_rotate_cert_fingerprint(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        client = RedfishClient(
            "https://bmc.test", vault, node_bmc,
            audit_writer=audit, redis_client=fake_redis,
        )
        fake_redis.store["bmc:42:session"] = "x|y|99999999999"
        new_fp = _OTHER_FP
        client.rotate_cert_fingerprint(new_fp)
        assert node_bmc.cert_fingerprint == new_fp
        assert "bmc:42:session" not in fake_redis.store
        assert any(e["action"] == "bmc.cert_pin_rotated" for e in audit.events)


# ---------------------------------------------------------------------------
# Default-credential refusal
# ---------------------------------------------------------------------------


class TestDefaultCredentialRefusal:
    def test_refuses_root_calvin(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_nats, monkeypatch
    ) -> None:
        # Default-credential probing happens inside first_connect(); with no
        # pre-existing pin, TOFU capture requires the explicit opt-in.
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")
        with rm_module.Mocker() as m:
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})

            def post_callback(request, context):
                body = request.json()
                if body == {"UserName": "root", "Password": "calvin"}:
                    context.status_code = 200
                    context.headers["X-Auth-Token"] = "factory-token"
                    context.headers["Location"] = "/redfish/v1/SessionService/Sessions/1"
                    return {}
                context.status_code = 401
                return {}

            m.post("https://bmc.test/redfish/v1/SessionService/Sessions", json=post_callback)
            m.delete(rm_module.ANY, status_code=204)
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc,
                audit_writer=audit, nats_publisher=fake_nats, cluster_id="c1",
            )
            with pytest.raises(BMCDefaultCredentialsDetected) as exc:
                client.first_connect()
            assert exc.value.username == "root"
            assert node_bmc.factory_creds_detected is True
            assert any(
                ev[0] == "gough.bmc.default_credentials" for ev in fake_nats.events
            )
            assert any(
                e["action"] == "bmc.default_credentials_detected" for e in audit.events
            )

    def test_default_credential_pairs_cover_known_vendors(self) -> None:
        users = {pair[0] for pair in DEFAULT_CREDENTIAL_PAIRS}
        assert {"root", "admin", "Administrator"}.issubset(users)


# ---------------------------------------------------------------------------
# Login + session cache
# ---------------------------------------------------------------------------


class TestSessionCacheAndLogin:
    def _setup_login(self, m: Any, status_code: int = 201) -> None:
        m.post(
            "https://bmc.test/redfish/v1/SessionService/Sessions",
            status_code=status_code,
            headers={
                "X-Auth-Token": "tok-1",
                "Location": "/redfish/v1/SessionService/Sessions/9",
            },
            json={},
        )

    def test_login_caches_in_redis(
        self, patch_tls_handshake, vault, node_bmc, fake_redis
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP  # skip TOFU
        with rm_module.Mocker() as m:
            self._setup_login(m)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc, redis_client=fake_redis
            )
            client._request("GET", "/redfish/v1/")
            assert "bmc:42:session" in fake_redis.store
            assert fake_redis.store["bmc:42:session"].startswith("tok-1|")

    def test_login_uses_cached_session(
        self, patch_tls_handshake, vault, node_bmc, fake_redis
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        # Pre-populate a valid cached session.
        fake_redis.store["bmc:42:session"] = (
            "cached-tok|https://bmc.test/redfish/v1/SessionService/Sessions/3|99999999999"
        )
        with rm_module.Mocker() as m:
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={"x": 1})
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc, redis_client=fake_redis
            )
            result = client._request("GET", "/redfish/v1/")
            assert result.body == {"x": 1}
            # Vault was never consulted because session was cached.
            assert vault.calls == []

    def test_expired_cache_is_invalidated(
        self, patch_tls_handshake, vault, node_bmc, fake_redis
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        fake_redis.store["bmc:42:session"] = "x|y|0.0"  # expired
        with rm_module.Mocker() as m:
            self._setup_login(m)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc, redis_client=fake_redis
            )
            client._request("GET", "/redfish/v1/")
            # Login was performed → vault consulted twice (user + pass)
            assert len(vault.calls) == 2

    def test_401_invalidates_cache_and_retries_once(
        self, patch_tls_handshake, vault, node_bmc, fake_redis
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        fake_redis.store["bmc:42:session"] = (
            "stale|https://bmc.test/redfish/v1/SessionService/Sessions/3|99999999999"
        )
        call_count = {"n": 0}

        def get_handler(request, context):
            call_count["n"] += 1
            if call_count["n"] == 1:
                context.status_code = 401
                return {}
            context.status_code = 200
            return {"ok": True}

        with rm_module.Mocker() as m:
            self._setup_login(m)
            m.get("https://bmc.test/redfish/v1/Systems/1", json=get_handler)
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc, redis_client=fake_redis
            )
            result = client._request("GET", "/redfish/v1/Systems/1")
            assert result.body == {"ok": True}
            assert call_count["n"] == 2

    def test_login_rejected_credentials_raises_auth_failure(
        self, patch_tls_handshake, vault, node_bmc
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        with rm_module.Mocker() as m:
            m.post(
                "https://bmc.test/redfish/v1/SessionService/Sessions",
                status_code=401,
            )
            client = RedfishClient("https://bmc.test", vault, node_bmc)
            with pytest.raises(BMCAuthFailure):
                client._login()

    def test_login_missing_token_header_raises(
        self, patch_tls_handshake, vault, node_bmc
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        with rm_module.Mocker() as m:
            m.post(
                "https://bmc.test/redfish/v1/SessionService/Sessions",
                status_code=201,
                json={},
            )
            client = RedfishClient("https://bmc.test", vault, node_bmc)
            with pytest.raises(BMCOperationFailed):
                client._login()

    def test_request_network_error_raises_unreachable(
        self, patch_tls_handshake, vault, node_bmc
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        with rm_module.Mocker() as m:
            self._setup_login(m)
            m.get(
                "https://bmc.test/redfish/v1/Systems/1",
                exc=requests.ConnectionError("nope"),
            )
            client = RedfishClient("https://bmc.test", vault, node_bmc)
            with pytest.raises(BMCUnreachable):
                client._request("GET", "/redfish/v1/Systems/1")


# ---------------------------------------------------------------------------
# Vault credential resolution
# ---------------------------------------------------------------------------


class TestVaultLookup:
    def test_invalid_ref_raises(self, patch_tls_handshake, vault, node_bmc) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        node_bmc.username_ref = "bad-ref-no-hash"
        client = RedfishClient("https://bmc.test", vault, node_bmc)
        with pytest.raises(ValueError, match="must be 'path#field'"):
            client._read_credentials()

    def test_missing_field_raises_auth_failure(
        self, patch_tls_handshake, node_bmc
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        empty_vault = _FakeVault({"secret/gough/c1/nodes/42/bmc": {"password": "p"}})
        client = RedfishClient("https://bmc.test", empty_vault, node_bmc)
        with pytest.raises(BMCAuthFailure, match="missing field"):
            client._read_credentials()

    def test_empty_value_raises_auth_failure(
        self, patch_tls_handshake, node_bmc
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        empty_vault = _FakeVault(
            {"secret/gough/c1/nodes/42/bmc": {"username": "", "password": "p"}}
        )
        client = RedfishClient("https://bmc.test", empty_vault, node_bmc)
        with pytest.raises(BMCAuthFailure, match="empty"):
            client._read_credentials()

    def test_dict_response_supported(
        self, patch_tls_handshake, node_bmc
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP

        class DictVault:
            def kv_read(self, path: str) -> dict[str, Any]:
                return {"data": {"username": "u", "password": "p"}}

        client = RedfishClient("https://bmc.test", DictVault(), node_bmc)
        u, p = client._read_credentials()
        assert (u, p) == ("u", "p")


# ---------------------------------------------------------------------------
# Full operation surface
# ---------------------------------------------------------------------------


def _client_pinned(
    patch_tls_handshake, vault, node_bmc, audit, fake_redis, mocker
) -> RedfishClient:
    """Pre-pin the cert and stage a successful login on ``mocker``."""
    node_bmc.cert_fingerprint = _DEFAULT_FP
    mocker.post(
        "https://bmc.test/redfish/v1/SessionService/Sessions",
        status_code=201,
        headers={
            "X-Auth-Token": "tok-1",
            "Location": "/redfish/v1/SessionService/Sessions/9",
        },
        json={},
    )
    return RedfishClient(
        "https://bmc.test",
        vault,
        node_bmc,
        audit_writer=audit,
        redis_client=fake_redis,
    )


class TestPowerControl:
    @pytest.mark.parametrize(
        "method,reset_type",
        [
            ("power_on", "On"),
            ("power_off", "ForceOff"),
            ("power_cycle", "PowerCycle"),
            ("force_restart", "ForceRestart"),
        ],
    )
    def test_power_actions_post_correct_reset_type(
        self,
        patch_tls_handshake,
        vault,
        node_bmc,
        audit,
        fake_redis,
        method: str,
        reset_type: str,
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            captured: dict[str, Any] = {}

            def cb(request, context):
                captured["body"] = request.json()
                context.status_code = 204
                return {}

            m.post(
                "https://bmc.test/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                json=cb,
            )
            getattr(client, method)()
            assert captured["body"] == {"ResetType": reset_type}
            assert any(e["action"] == f"bmc.power_{reset_type.lower()}" for e in audit.events)


class TestOneTimeBoot:
    def test_set_pxe_one_time(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            captured: dict[str, Any] = {}

            def cb(request, context):
                captured["body"] = request.json()
                context.status_code = 204
                return {}

            m.patch("https://bmc.test/redfish/v1/Systems/1", json=cb)
            client.set_one_time_boot_target("Pxe")
            assert captured["body"] == {
                "Boot": {"BootSourceOverrideTarget": "Pxe", "BootSourceOverrideEnabled": "Once"}
            }
            assert any(
                e["action"] == "bmc.set_one_time_boot" and e["after"]["target"] == "Pxe"
                for e in audit.events
            )


class TestSensors:
    def test_get_sensors_aggregates_thermal_and_power(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            m.get(
                "https://bmc.test/redfish/v1/Chassis/1/Thermal",
                json={
                    "Temperatures": [
                        {"Name": "CPU1", "ReadingCelsius": 65.0,
                         "Status": {"Health": "OK"}}
                    ],
                    "Fans": [{"Name": "Fan1", "Reading": 4500, "ReadingUnits": "RPM"}],
                },
            )
            m.get(
                "https://bmc.test/redfish/v1/Chassis/1/Power",
                json={
                    "PowerSupplies": [
                        {"Name": "PSU1", "PowerOutputWatts": 250.0,
                         "Status": {"Health": "OK"}}
                    ],
                    "Voltages": [{"Name": "V1", "ReadingVolts": 12.0}],
                },
            )
            sensors = client.get_sensors()
            kinds = {s.kind for s in sensors}
            assert kinds == {"temperature", "fan", "power", "voltage"}
            assert all(isinstance(s, Sensor) for s in sensors)
            assert any(e["action"] == "bmc.get_sensors" for e in audit.events)


class TestSelLog:
    def test_get_sel_log(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            m.get(
                "https://bmc.test/redfish/v1/Systems/1/LogServices/Sel/Entries",
                json={
                    "Members": [
                        {
                            "Id": "1",
                            "Created": "2026-01-01T00:00:00Z",
                            "Severity": "OK",
                            "Message": "boot",
                            "SensorType": "System",
                            "EntryCode": "Asserted",
                        }
                    ]
                },
            )
            entries = client.get_sel_log()
            assert len(entries) == 1
            assert isinstance(entries[0], SelEntry)
            assert entries[0].id == "1"


class TestFirmwareInventory:
    def test_get_firmware_inventory_walks_members(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            m.get(
                "https://bmc.test/redfish/v1/UpdateService/FirmwareInventory",
                json={
                    "Members": [
                        {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
                        {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMC"},
                    ]
                },
            )
            m.get(
                "https://bmc.test/redfish/v1/UpdateService/FirmwareInventory/BIOS",
                json={"Id": "BIOS", "Name": "System BIOS", "Version": "2.10",
                      "Updateable": True, "Manufacturer": "Dell"},
            )
            m.get(
                "https://bmc.test/redfish/v1/UpdateService/FirmwareInventory/BMC",
                json={"Id": "BMC", "Name": "iDRAC", "Version": "5.10", "Updateable": True},
            )
            components = client.get_firmware_inventory()
            assert [c.component_id for c in components] == ["BIOS", "BMC"]
            assert all(isinstance(c, FirmwareComponent) for c in components)


class TestUpdateFirmware:
    def test_update_firmware_returns_task_id(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            m.post(
                "https://bmc.test/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
                status_code=202,
                headers={"Location": "/redfish/v1/TaskService/Tasks/abc-123"},
                json={},
            )
            task_id = client.update_firmware("BIOS", "https://lvfs/example.bin")
            assert task_id == "abc-123"
            assert any(
                e["action"] == "bmc.firmware_update_submitted" for e in audit.events
            )

    def test_update_firmware_missing_task_id_raises(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            m.post(
                "https://bmc.test/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
                status_code=202,
                json={},
            )
            with pytest.raises(BMCFirmwareUpdateFailed):
                client.update_firmware("BIOS", "https://lvfs/example.bin")


class TestBiosSettings:
    def test_get_bios_settings(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            m.get(
                "https://bmc.test/redfish/v1/Systems/1/Bios",
                json={"Attributes": {"BootMode": "Uefi", "SecureBoot": "Enabled"}},
            )
            attrs = client.get_bios_settings()
            assert attrs == {"BootMode": "Uefi", "SecureBoot": "Enabled"}

    def test_set_bios_setting(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            captured: dict[str, Any] = {}

            def cb(request, context):
                captured["body"] = request.json()
                context.status_code = 204
                return {}

            m.patch("https://bmc.test/redfish/v1/Systems/1/Bios/Settings", json=cb)
            client.set_bios_setting("SecureBoot", "Disabled")
            assert captured["body"] == {"Attributes": {"SecureBoot": "Disabled"}}
            assert any(e["action"] == "bmc.set_bios_setting" for e in audit.events)


class TestCapabilities:
    def test_get_capabilities_caches_and_audits(
        self, patch_tls_handshake, vault, node_bmc, audit, fake_redis
    ) -> None:
        with rm_module.Mocker() as m:
            client = _client_pinned(patch_tls_handshake, vault, node_bmc, audit, fake_redis, m)
            m.get(
                "https://bmc.test/redfish/v1/",
                json={
                    "RedfishVersion": "1.13.0",
                    "Product": "iDRAC9",
                    "Name": "iDRAC9 Service Root",
                    "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
                    "Oem": {"Dell": {"Type": "iDRACEnterprise"}},
                },
            )
            m.get(
                "https://bmc.test/redfish/v1/UpdateService",
                json={
                    "Actions": {
                        "#UpdateService.SimpleUpdate": {
                            "target": "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate"
                        }
                    },
                    "FirmwareInventory": {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"},
                },
            )
            caps = client.get_capabilities()
            assert caps["vendor"] == "Dell"
            assert caps["supports_simple_update"] is True
            assert caps["redfish_version"] == "1.13.0"
            assert node_bmc.capabilities == caps
            assert any(e["action"] == "bmc.get_capabilities" for e in audit.events)


class TestCloseAndCtxManager:
    def test_context_manager_closes(
        self, patch_tls_handshake, vault, node_bmc
    ) -> None:
        node_bmc.cert_fingerprint = _DEFAULT_FP
        with rm_module.Mocker():
            with RedfishClient("https://bmc.test", vault, node_bmc) as client:
                assert client.endpoint == "https://bmc.test"
        client.close()  # idempotent


class TestAuditFailureIsolation:
    def test_audit_writer_exception_does_not_break_op(
        self, patch_tls_handshake, vault, node_bmc, fake_redis
    ) -> None:
        class BadAudit:
            def append(self, **kwargs: Any) -> None:
                raise RuntimeError("boom")

        node_bmc.cert_fingerprint = _DEFAULT_FP
        with rm_module.Mocker() as m:
            m.post(
                "https://bmc.test/redfish/v1/SessionService/Sessions",
                status_code=201,
                headers={
                    "X-Auth-Token": "t",
                    "Location": "/redfish/v1/SessionService/Sessions/1",
                },
                json={},
            )
            m.post(
                "https://bmc.test/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                status_code=204,
            )
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc,
                audit_writer=BadAudit(), redis_client=fake_redis,
            )
            client.power_on()  # must not raise


class TestPinCallback:
    def test_on_fingerprint_pinned_invoked(
        self, patch_tls_handshake, vault, node_bmc, audit, monkeypatch
    ) -> None:
        # on_fingerprint_pinned only fires on a fresh TOFU capture, which
        # requires the explicit opt-in when no pin is pre-configured.
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")
        captured = MagicMock()
        with rm_module.Mocker() as m:
            m.post("https://bmc.test/redfish/v1/SessionService/Sessions", status_code=401)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})
            client = RedfishClient(
                "https://bmc.test", vault, node_bmc,
                audit_writer=audit, on_fingerprint_pinned=captured,
            )
            client.first_connect()
            captured.assert_called_once_with(_DEFAULT_FP)


class TestTOFUEnvGating:
    """Tests for BMC_ALLOW_INSECURE_TLS env flag gating TOFU first-connect."""

    def test_tofu_without_env_flag_raises(
        self, patch_tls_handshake, vault, node_bmc, monkeypatch
    ) -> None:
        """TOFU (no pre-existing pin) should raise if env flag not set."""
        node_bmc.cert_fingerprint = None  # TOFU scenario
        monkeypatch.delenv("BMC_ALLOW_INSECURE_TLS", raising=False)

        with rm_module.Mocker():
            client = RedfishClient("https://bmc.test", vault, node_bmc)
            with pytest.raises(BMCUnreachable) as exc_info:
                client.first_connect()
            assert "BMC_ALLOW_INSECURE_TLS" in str(exc_info.value)

    def test_tofu_with_env_flag_true_succeeds(
        self, patch_tls_handshake, vault, node_bmc, audit, monkeypatch
    ) -> None:
        """TOFU should succeed and warn when BMC_ALLOW_INSECURE_TLS=true."""
        node_bmc.cert_fingerprint = None  # TOFU scenario
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")

        with rm_module.Mocker() as m:
            m.post("https://bmc.test/redfish/v1/SessionService/Sessions", status_code=401)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})
            client = RedfishClient("https://bmc.test", vault, node_bmc, audit_writer=audit)
            fp = client.first_connect()
            assert fp == _DEFAULT_FP
            assert node_bmc.cert_fingerprint == _DEFAULT_FP

    def test_tofu_with_env_flag_1_succeeds(
        self, patch_tls_handshake, vault, node_bmc, audit, monkeypatch
    ) -> None:
        """TOFU should succeed when BMC_ALLOW_INSECURE_TLS=1."""
        node_bmc.cert_fingerprint = None
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "1")

        with rm_module.Mocker() as m:
            m.post("https://bmc.test/redfish/v1/SessionService/Sessions", status_code=401)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})
            client = RedfishClient("https://bmc.test", vault, node_bmc, audit_writer=audit)
            fp = client.first_connect()
            assert fp == _DEFAULT_FP

    def test_existing_pin_bypasses_env_flag(
        self, patch_tls_handshake, vault, node_bmc, audit, monkeypatch
    ) -> None:
        """When cert_fingerprint already exists, env flag not needed."""
        node_bmc.cert_fingerprint = _DEFAULT_FP
        monkeypatch.delenv("BMC_ALLOW_INSECURE_TLS", raising=False)

        with rm_module.Mocker() as m:
            m.post("https://bmc.test/redfish/v1/SessionService/Sessions", status_code=401)
            m.get("https://bmc.test/redfish/v1/", status_code=200, json={})
            client = RedfishClient("https://bmc.test", vault, node_bmc, audit_writer=audit)
            fp = client.first_connect()
            assert fp == _DEFAULT_FP  # Succeeds without env flag

    def test_pin_mismatch_rejected_regardless_of_env(
        self, patch_tls_handshake, vault, node_bmc, monkeypatch
    ) -> None:
        """Pinned cert mismatch always rejected, env flag irrelevant."""
        node_bmc.cert_fingerprint = _OTHER_FP  # Different pin
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")

        with rm_module.Mocker():
            client = RedfishClient("https://bmc.test", vault, node_bmc)
            with pytest.raises(BMCCertPinMismatch) as exc_info:
                client.first_connect()
            assert "mismatch" in str(exc_info.value).lower()
