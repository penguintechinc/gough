"""Tests for app.clients.ipmi.

Covers cipher-suite enforcement (17 required, 0 forbidden), default-credential
refusal + NATS event, all power/boot/SEL operations, audit-chain emission,
Vault credential resolution, error mapping, and pyghmi wrapping.

Tests use a mock command factory to avoid pyghmi's real network IO.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.clients import ipmi as ipmi_mod
from app.clients.ipmi import (
    BMCAuthFailure,
    BMCDefaultCredentialsDetected,
    BMCError,
    BMCOperationFailed,
    BMCUnreachable,
    DEFAULT_TIMEOUT_SEC,
    REQUIRED_CIPHER_SUITE,
    IpmiClient,
    IpmiInsecureCipherRefused,
    SelEntry,
    _coerce_iterable,
    _negotiated_cipher,
    _parse_endpoint,
    _stringify,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNodeBmc:
    """Minimal stand-in for the ``NodeBmc`` ORM row."""

    def __init__(self, node_id: int = 42) -> None:
        self.node_id = node_id
        self.endpoint = "ipmi://bmc.test"
        self.username_ref = "secret/gough/c1/nodes/42/bmc#username"
        self.password_ref = "secret/gough/c1/nodes/42/bmc#password"
        self.factory_creds_detected = False


class _FakeVault:
    def __init__(self, mapping: dict[str, dict[str, str]]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def kv_read(self, path: str) -> Any:
        self.calls.append(path)
        return SimpleNamespace(data=self._mapping.get(path, {}))


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


class _FakePyghmiCommand:
    """Mock pyghmi command object."""

    def __init__(
        self,
        cipher_suite: int = REQUIRED_CIPHER_SUITE,
        fail_reason: Optional[str] = None,
    ) -> None:
        self.cipher_suite = cipher_suite
        self.fail_reason = fail_reason
        self.ipmi_session = SimpleNamespace(cipher_suite=cipher_suite)
        self.logouts: int = 0

    def logout(self) -> None:
        self.logouts += 1

    def set_power(self, state: str, wait: bool = False) -> str:
        if self.fail_reason:
            raise ipmi_mod._PyghmiIpmiException(self.fail_reason)
        return f"power state: {state}"

    def set_bootdev(self, dev: str, persist: bool = False, uefiboot: bool = False) -> str:
        if self.fail_reason:
            raise ipmi_mod._PyghmiIpmiException(self.fail_reason)
        return f"boot device set to {dev}"

    def get_event_log(self) -> list[dict[str, Any]]:
        if self.fail_reason:
            raise ipmi_mod._PyghmiIpmiException(self.fail_reason)
        return [
            {
                "record_id": 1,
                "timestamp": "2026-04-28T12:00:00Z",
                "severity": "Info",
                "message": "System event",
                "sensor_type": "Thermal",
            }
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault() -> _FakeVault:
    return _FakeVault(
        {
            "secret/gough/c1/nodes/42/bmc": {
                "username": "ipmi_admin",
                "password": "ipmi-pw-1",
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
def fake_command() -> _FakePyghmiCommand:
    return _FakePyghmiCommand()


# ---------------------------------------------------------------------------
# Constructor / initialization tests
# ---------------------------------------------------------------------------


def test_ipmi_client_init(vault: _FakeVault, node_bmc: _FakeNodeBmc) -> None:
    """Constructor accepts valid endpoint and stores params."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test:623",
        vault_client=vault,
        node_bmc_record=node_bmc,
    )
    assert client.endpoint == "ipmi://bmc.test:623"
    assert client._host == "bmc.test"
    assert client._port == 623


def test_ipmi_client_endpoint_forms(vault: _FakeVault, node_bmc: _FakeNodeBmc) -> None:
    """Constructor parses multiple endpoint forms: bare host, host:port, ipmi://...."""
    for endpoint, expected_host, expected_port in [
        ("bmc.test", "bmc.test", 623),
        ("bmc.test:624", "bmc.test", 624),
        ("ipmi://bmc.test", "bmc.test", 623),
        ("ipmi://bmc.test:624", "bmc.test", 624),
        ("rmcp://bmc.test:624", "bmc.test", 624),
        ("rmcpp://bmc.test:624", "bmc.test", 624),
    ]:
        client = IpmiClient(
            endpoint=endpoint,
            vault_client=vault,
            node_bmc_record=node_bmc,
        )
        assert client._host == expected_host
        assert client._port == expected_port


def test_ipmi_client_init_cipher_suite_enforced(
    vault: _FakeVault, node_bmc: _FakeNodeBmc
) -> None:
    """Constructor refuses cipher suite 0 unconditionally."""
    with pytest.raises(IpmiInsecureCipherRefused) as exc_info:
        IpmiClient(
            endpoint="ipmi://bmc.test",
            vault_client=vault,
            node_bmc_record=node_bmc,
            cipher_suite=0,
        )
    assert exc_info.value.cipher_suite == 0


def test_ipmi_client_init_cipher_suite_only_17_allowed(
    vault: _FakeVault, node_bmc: _FakeNodeBmc
) -> None:
    """Constructor refuses any cipher suite other than 17."""
    with pytest.raises(ValueError, match="not permitted"):
        IpmiClient(
            endpoint="ipmi://bmc.test",
            vault_client=vault,
            node_bmc_record=node_bmc,
            cipher_suite=3,
        )


def test_ipmi_client_init_missing_endpoint(vault: _FakeVault, node_bmc: _FakeNodeBmc) -> None:
    """Constructor rejects empty endpoint."""
    with pytest.raises(ValueError, match="endpoint is required"):
        IpmiClient(
            endpoint="",
            vault_client=vault,
            node_bmc_record=node_bmc,
        )


# ---------------------------------------------------------------------------
# Credential resolution tests
# ---------------------------------------------------------------------------


def test_ipmi_kv_lookup_success(vault: _FakeVault, node_bmc: _FakeNodeBmc) -> None:
    """_kv_lookup resolves path#field from Vault KV v2."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
    )
    cred = client._kv_lookup("secret/gough/c1/nodes/42/bmc#username")
    assert cred == "ipmi_admin"


def test_ipmi_kv_lookup_missing_field(vault: _FakeVault, node_bmc: _FakeNodeBmc) -> None:
    """_kv_lookup raises BMCAuthFailure if field not in Vault response."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
    )
    with pytest.raises(BMCAuthFailure, match="missing field"):
        client._kv_lookup("secret/gough/c1/nodes/42/bmc#missing")


def test_ipmi_kv_lookup_empty_credential(vault: _FakeVault, node_bmc: _FakeNodeBmc) -> None:
    """_kv_lookup raises BMCAuthFailure if credential is empty."""
    vault._mapping["secret/gough/c1/nodes/42/bmc"]["empty"] = ""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
    )
    with pytest.raises(BMCAuthFailure, match="is empty"):
        client._kv_lookup("secret/gough/c1/nodes/42/bmc#empty")


def test_ipmi_read_credentials(vault: _FakeVault, node_bmc: _FakeNodeBmc) -> None:
    """_read_credentials resolves both username and password refs."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
    )
    user, pw = client._read_credentials()
    assert user == "ipmi_admin"
    assert pw == "ipmi-pw-1"


# ---------------------------------------------------------------------------
# First-connect: default-credential detection
# ---------------------------------------------------------------------------


def test_ipmi_first_connect_default_creds_detected(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_nats: _FakeNats,
    audit: _FakeAudit,
) -> None:
    """first_connect() raises BMCDefaultCredentialsDetected if any pair succeeds."""

    def factory(bmc: str, userid: str, password: str, **kwargs: Any) -> _FakePyghmiCommand:
        # First pair (root/calvin) succeeds; audit + NATS event fired
        if userid == "root" and password == "calvin":
            cmd = _FakePyghmiCommand()
            cmd.logouts = 0
            return cmd
        # Other pairs fail normally
        raise ipmi_mod._PyghmiIpmiException("Auth failed")

    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        audit_writer=audit,
        nats_publisher=fake_nats,
        command_factory=factory,
    )

    with pytest.raises(BMCDefaultCredentialsDetected) as exc_info:
        client.first_connect()

    assert exc_info.value.username == "root"
    assert node_bmc.factory_creds_detected is True

    # Audit event emitted
    assert len(audit.events) == 1
    assert audit.events[0]["action"] == "bmc.default_credentials_detected"
    assert audit.events[0]["after"]["username"] == "root"

    # NATS event emitted
    assert len(fake_nats.events) == 1
    subject, payload = fake_nats.events[0]
    assert subject == "gough.bmc.default_credentials"
    assert payload["username"] == "root"
    assert payload["node_id"] == 42


def test_ipmi_first_connect_no_default_creds(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
) -> None:
    """first_connect() completes silently if no default-credential pair succeeds."""

    def factory(**kwargs: Any) -> _FakePyghmiCommand:
        raise ipmi_mod._PyghmiIpmiException("Auth failed")

    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=factory,
    )
    # Should not raise; just returns silently
    client.first_connect()
    assert node_bmc.factory_creds_detected is False


def test_ipmi_first_connect_unreachable_defers(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
) -> None:
    """first_connect() defers if BMC is unreachable (transient network error)."""

    def factory(**kwargs: Any) -> _FakePyghmiCommand:
        raise OSError("Network unreachable")

    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=factory,
    )
    # Should not raise; just returns
    client.first_connect()


# ---------------------------------------------------------------------------
# Power control tests
# ---------------------------------------------------------------------------


def test_ipmi_power_on(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
    audit: _FakeAudit,
) -> None:
    """power_on() sets power state and audits the action."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        audit_writer=audit,
        command_factory=lambda **kw: fake_command,
    )
    client.power_on()
    assert len(audit.events) == 1
    assert audit.events[0]["action"] == "bmc.power_on"
    assert audit.events[0]["after"]["state"] == "on"


def test_ipmi_power_off(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
    audit: _FakeAudit,
) -> None:
    """power_off() sets power state and audits the action."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        audit_writer=audit,
        command_factory=lambda **kw: fake_command,
    )
    client.power_off()
    assert len(audit.events) == 1
    assert audit.events[0]["action"] == "bmc.power_off"
    assert audit.events[0]["after"]["state"] == "off"


def test_ipmi_power_cycle(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
    audit: _FakeAudit,
) -> None:
    """power_cycle() sets power state and audits the action."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        audit_writer=audit,
        command_factory=lambda **kw: fake_command,
    )
    client.power_cycle()
    assert len(audit.events) == 1
    assert audit.events[0]["action"] == "bmc.power_cycle"
    assert audit.events[0]["after"]["state"] == "cycle"


def test_ipmi_power_operation_failed(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
) -> None:
    """power operations raise BMCOperationFailed on pyghmi errors."""
    cmd = _FakePyghmiCommand(fail_reason="Firmware error")
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=lambda **kw: cmd,
    )
    with pytest.raises(BMCOperationFailed, match="Firmware error"):
        client.power_on()


# ---------------------------------------------------------------------------
# Boot device tests
# ---------------------------------------------------------------------------


def test_ipmi_set_chassis_bootdev(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
    audit: _FakeAudit,
) -> None:
    """set_chassis_bootdev() sets boot device and audits the action."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        audit_writer=audit,
        command_factory=lambda **kw: fake_command,
    )
    client.set_chassis_bootdev(target="pxe", persistent=False)
    assert len(audit.events) == 1
    assert audit.events[0]["action"] == "bmc.set_chassis_bootdev"
    assert audit.events[0]["after"]["target"] == "pxe"
    assert audit.events[0]["after"]["persistent"] is False


def test_ipmi_set_chassis_bootdev_persistent(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
    audit: _FakeAudit,
) -> None:
    """set_chassis_bootdev() respects persistent flag."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        audit_writer=audit,
        command_factory=lambda **kw: fake_command,
    )
    client.set_chassis_bootdev(target="disk", persistent=True)
    assert audit.events[0]["after"]["target"] == "disk"
    assert audit.events[0]["after"]["persistent"] is True


# ---------------------------------------------------------------------------
# SEL log tests
# ---------------------------------------------------------------------------


def test_ipmi_get_sel_log(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
    audit: _FakeAudit,
) -> None:
    """get_sel_log() retrieves and parses event log entries."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        audit_writer=audit,
        command_factory=lambda **kw: fake_command,
    )
    entries = client.get_sel_log()
    assert len(entries) == 1
    assert isinstance(entries[0], SelEntry)
    assert entries[0].record_id == "1"
    assert entries[0].severity == "Info"
    assert entries[0].message == "System event"

    # Audit event emitted
    assert len(audit.events) == 1
    assert audit.events[0]["action"] == "bmc.get_sel_log"
    assert audit.events[0]["after"]["count"] == 1


def test_ipmi_get_sel_log_operation_failed(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
) -> None:
    """get_sel_log() raises BMCOperationFailed on pyghmi errors."""
    cmd = _FakePyghmiCommand(fail_reason="SEL empty")
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=lambda **kw: cmd,
    )
    with pytest.raises(BMCOperationFailed, match="SEL empty"):
        client.get_sel_log()


# ---------------------------------------------------------------------------
# Cipher suite enforcement tests
# ---------------------------------------------------------------------------


def test_ipmi_cipher_suite_17_required_on_session(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
) -> None:
    """_open() enforces cipher suite 17; refuses 0."""
    cmd = _FakePyghmiCommand(cipher_suite=0)
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=lambda **kw: cmd,
    )
    with pytest.raises(IpmiInsecureCipherRefused) as exc_info:
        client._ensure_open()
    assert exc_info.value.cipher_suite == 0


def test_ipmi_cipher_suite_0_at_negotiation_raises(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
) -> None:
    """pyghmi exception mentioning cipher 0 is mapped to IpmiInsecureCipherRefused."""

    def factory(**kwargs: Any) -> _FakePyghmiCommand:
        raise ipmi_mod._PyghmiIpmiException(
            "No compatible cipher suites (session expects 0)"
        )

    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=factory,
    )
    with pytest.raises(IpmiInsecureCipherRefused):
        client._ensure_open()


# ---------------------------------------------------------------------------
# Error mapping tests
# ---------------------------------------------------------------------------


def test_ipmi_auth_failure_mapped(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
) -> None:
    """pyghmi auth exceptions mapped to BMCAuthFailure."""

    def factory(**kwargs: Any) -> _FakePyghmiCommand:
        raise ipmi_mod._PyghmiIpmiException("Authentication failed (password incorrect)")

    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=factory,
    )
    with pytest.raises(BMCAuthFailure):
        client._ensure_open()


def test_ipmi_unreachable_mapped(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
) -> None:
    """OSError mapped to BMCUnreachable."""

    def factory(**kwargs: Any) -> _FakePyghmiCommand:
        raise OSError("Connection refused")

    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=factory,
    )
    with pytest.raises(BMCUnreachable, match="Connection refused"):
        client._ensure_open()


# ---------------------------------------------------------------------------
# Session lifetime tests
# ---------------------------------------------------------------------------


def test_ipmi_session_reuse(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
) -> None:
    """Multiple operations reuse the same session."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=lambda **kw: fake_command,
    )
    client.power_on()
    client.power_off()
    # Only one command object created; same instance used both times
    assert fake_command.logouts == 0


def test_ipmi_close(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
) -> None:
    """close() logs out the pyghmi session."""
    client = IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=lambda **kw: fake_command,
    )
    client.power_on()
    client.close()
    assert fake_command.logouts == 1


def test_ipmi_context_manager(
    vault: _FakeVault,
    node_bmc: _FakeNodeBmc,
    fake_command: _FakePyghmiCommand,
) -> None:
    """IpmiClient works as a context manager."""
    with IpmiClient(
        endpoint="ipmi://bmc.test",
        vault_client=vault,
        node_bmc_record=node_bmc,
        command_factory=lambda **kw: fake_command,
    ) as client:
        client.power_on()
    assert fake_command.logouts == 1


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_parse_endpoint_host_only() -> None:
    host, port = _parse_endpoint("bmc.example.com")
    assert host == "bmc.example.com"
    assert port == 623


def test_parse_endpoint_host_port() -> None:
    host, port = _parse_endpoint("bmc.example.com:624")
    assert host == "bmc.example.com"
    assert port == 624


def test_parse_endpoint_ipmi_uri() -> None:
    host, port = _parse_endpoint("ipmi://bmc.example.com:625")
    assert host == "bmc.example.com"
    assert port == 625


def test_parse_endpoint_rmcp_uri() -> None:
    host, port = _parse_endpoint("rmcp://bmc.example.com")
    assert host == "bmc.example.com"
    assert port == 623


def test_parse_endpoint_rmcpp_uri() -> None:
    host, port = _parse_endpoint("rmcpp://bmc.example.com:626")
    assert host == "bmc.example.com"
    assert port == 626


def test_parse_endpoint_invalid_port() -> None:
    with pytest.raises(ValueError, match="invalid IPMI port"):
        _parse_endpoint("bmc.example.com:bad")


def test_parse_endpoint_no_host() -> None:
    with pytest.raises(ValueError, match="could not parse IPMI host"):
        _parse_endpoint("")


def test_negotiated_cipher_none_session() -> None:
    obj = SimpleNamespace(ipmi_session=None)
    assert _negotiated_cipher(obj) is None


def test_negotiated_cipher_extractable() -> None:
    obj = SimpleNamespace(ipmi_session=SimpleNamespace(cipher_suite=17))
    assert _negotiated_cipher(obj) == 17


def test_negotiated_cipher_non_int() -> None:
    obj = SimpleNamespace(ipmi_session=SimpleNamespace(cipher_suite="17"))
    assert _negotiated_cipher(obj) == 17


def test_coerce_iterable_dict() -> None:
    result = _coerce_iterable({"key": "value"})
    assert list(result) == [{"key": "value"}]


def test_coerce_iterable_list() -> None:
    result = _coerce_iterable([{"a": 1}, {"b": 2}])
    assert list(result) == [{"a": 1}, {"b": 2}]


def test_coerce_iterable_non_dict_filtered() -> None:
    result = list(_coerce_iterable([{"a": 1}, "string", 42]))
    assert result == [{"a": 1}]


def test_coerce_iterable_none() -> None:
    result = _coerce_iterable(None)
    assert list(result) == []


def test_stringify_none() -> None:
    assert _stringify(None) is None


def test_stringify_string() -> None:
    assert _stringify("hello") == "hello"


def test_stringify_other() -> None:
    assert _stringify(42) == "42"
    assert _stringify([1, 2, 3]) == "[1, 2, 3]"
