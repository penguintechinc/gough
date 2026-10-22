"""Behaviour tests for worker.services.power_manager: IPMI, Redfish, WOL, boot device and BMC pinning.

External tools and network are faked: ipmitool/wakeonlan via a fake subprocess, Redfish via respx,
TLS/socket fingerprinting via fake socket and SSL objects.
"""

import asyncio
import hashlib
import socket
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.certs import make_cert as _make_cert
from worker.services import power_manager as pm
from worker.services.power_manager import BMCCredentials, PowerManager

HOST = "bmc.lab.test"
PIN = "AB:" * 31 + "AB"  # well-formed 32-byte fingerprint (uppercase, colon separated)


def _creds(power_type: str = "ipmi", address: str = HOST, **kw: object) -> BMCCredentials:
    """Build BMC credentials for the given power type."""
    return BMCCredentials(
        address=address,
        username="admin",
        password="s3cret-pass",
        power_type=power_type,
        cert_fingerprint=kw.get("cert_fingerprint"),  # type: ignore[arg-type]
    )


class FakeProc:
    """Subprocess stand-in exposing communicate() and returncode."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"",
                 communicate_error: Exception | None = None) -> None:
        """Script the exit code, output and optional communicate() failure."""
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._error = communicate_error

    async def communicate(self) -> tuple[bytes, bytes]:
        """Return scripted output or raise the scripted failure."""
        if self._error is not None:
            raise self._error
        return self._stdout, self._stderr


def _spawn(monkeypatch: pytest.MonkeyPatch, proc: FakeProc | None, calls: list[dict[str, Any]],
           error: Exception | None = None) -> None:
    """Replace create_subprocess_exec: record argv/env, then return proc or raise error."""

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProc:
        """Record the invocation and return the scripted process."""
        calls.append({"argv": list(args), **kwargs})
        if error is not None:
            raise error
        assert proc is not None
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def _no_cert_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Satisfy the mandatory-TLS gate with a CA bundle (HTTP itself is mocked by respx)."""
    ca = Path(tempfile.mkdtemp()) / "ca.pem"
    ca.write_bytes(_make_cert("ca.test")[0])
    monkeypatch.setenv("REDFISH_CA_FILE", str(ca))


# --- fingerprint helpers ---------------------------------------------------------------

def test_normalize_fingerprint_accepts_plain_lower_and_spaced_forms() -> None:
    """Plain hex, lowercase and space-separated inputs all normalise to colon-separated uppercase."""
    plain = "ab" * 32
    assert pm._normalize_fingerprint(plain) == PIN
    assert pm._normalize_fingerprint(PIN.lower()) == PIN
    assert pm._normalize_fingerprint(" ".join(["ab"] * 32)) == PIN


@pytest.mark.parametrize("bad", ["", "abcd", "zz" * 32 + "00"])
def test_normalize_fingerprint_rejects_wrong_length(bad: str) -> None:
    """Anything that is not 64 hex characters after cleanup is refused with the length in the message."""
    with pytest.raises(ValueError, match="Invalid SHA-256 fingerprint length"):
        pm._normalize_fingerprint(bad)


# --- dispatch ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsupported_power_type_is_rejected() -> None:
    """Unknown power types return a failure tuple instead of raising."""
    ok, msg = await PowerManager().power_control(_creds(power_type="ilo"), "on")

    assert ok is False
    assert msg == "Unsupported power type: ilo"


# --- IPMI ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ipmi_power_on_success_passes_password_via_env_not_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPMI power on runs ipmitool with -E and the password only in IPMI_PASSWORD, and returns stdout."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(returncode=0, stdout=b"Chassis Power Control: Up/On\n"), calls)

    ok, out = await PowerManager().power_control(_creds(), "on")

    assert ok is True
    assert out == "Chassis Power Control: Up/On"
    argv = calls[0]["argv"]
    assert argv == ["ipmitool", "-I", "lanplus", "-H", HOST, "-U", "admin", "-E", "power", "on"]
    assert "s3cret-pass" not in " ".join(argv)
    assert calls[0]["env"]["IPMI_PASSWORD"] == "s3cret-pass"


@pytest.mark.asyncio
async def test_ipmi_child_env_excludes_worker_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """ipmitool gets a minimal environment: the BMC password, never the worker's own secrets."""
    monkeypatch.setenv("WORKER_API_KEY", "worker-key-should-not-leak")
    monkeypatch.setenv("STORAGE_SECRET_KEY", "storage-should-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(returncode=0, stdout=b"ok"), calls)

    await PowerManager().power_control(_creds(), "on")

    assert set(calls[0]["env"]) <= {"PATH", "LANG", "LC_ALL", "IPMI_PASSWORD"}
    assert "IPMI_PASSWORD" in calls[0]["env"]


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["-oProxyCommand=evil", "-h"])
async def test_ipmi_address_that_looks_like_an_option_is_refused(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """A BMC address starting with '-' would be parsed as an ipmitool option; it never spawns."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(), calls)

    ok, msg = await PowerManager().power_control(_creds(address=address), "on")
    ok_boot, _ = await PowerManager().set_boot_device(_creds(address=address), "pxe")

    assert (ok, msg, ok_boot, calls) == (False, "Invalid BMC address", False, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mac", ["-v", "not-a-mac", "00:11:22:33:44", "00:11:22:33:44:55:66"])
async def test_wol_rejects_non_mac_address_before_spawning(monkeypatch: pytest.MonkeyPatch, mac: str) -> None:
    """Only a 6-octet MAC reaches wakeonlan; anything else (incl. option-like input) is refused."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(), calls)

    ok, msg = await PowerManager().power_control(_creds("wol", address=mac), "on")

    assert (ok, msg, calls) == (False, "Invalid MAC address", [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action, expected_tail",
    [("off", ["power", "off"]), ("cycle", ["power", "cycle"]),
     ("reset", ["power", "reset"]), ("status", ["power", "status"])],
)
async def test_ipmi_action_mapping(monkeypatch: pytest.MonkeyPatch, action: str, expected_tail: list[str]) -> None:
    """Each power action maps to its ipmitool subcommand."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(returncode=0, stdout=b"ok"), calls)

    await PowerManager().power_control(_creds(), action)  # type: ignore[arg-type]

    assert calls[0]["argv"][-2:] == expected_tail


@pytest.mark.asyncio
async def test_ipmi_invalid_action_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown action is refused before ipmitool is launched."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(), calls)

    ok, msg = await PowerManager().power_control(_creds(), "explode")  # type: ignore[arg-type]

    assert ok is False
    assert msg == "Invalid IPMI action: explode"
    assert calls == []


@pytest.mark.asyncio
async def test_ipmi_nonzero_exit_returns_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing ipmitool returns its stderr text as the failure message."""
    _spawn(monkeypatch, FakeProc(returncode=1, stderr=b"Unable to establish IPMI v2 / RMCP+ session\n"), [])

    ok, msg = await PowerManager().power_control(_creds(), "on")

    assert ok is False
    assert msg == "Unable to establish IPMI v2 / RMCP+ session"


@pytest.mark.asyncio
async def test_ipmi_timeout_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A communicate() that times out yields the documented timeout message."""
    _spawn(monkeypatch, FakeProc(communicate_error=asyncio.TimeoutError()), [])

    ok, msg = await PowerManager().power_control(_creds(), "on")

    assert ok is False
    assert msg == "IPMI command timeout"


@pytest.mark.asyncio
async def test_ipmi_missing_binary_reports_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ipmitool binary is reported as not installed, not as a crash."""
    _spawn(monkeypatch, None, [], error=FileNotFoundError("ipmitool"))

    ok, msg = await PowerManager().power_control(_creds(), "on")

    assert ok is False
    assert msg == "ipmitool not installed"


@pytest.mark.asyncio
async def test_ipmi_unexpected_exception_returns_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other exception is converted to a failure message."""
    _spawn(monkeypatch, None, [], error=PermissionError("no exec"))

    ok, msg = await PowerManager().power_control(_creds(), "on")

    assert ok is False
    assert msg == "no exec"


# --- Redfish -------------------------------------------------------------------------

REDFISH_RESET = f"https://{HOST}/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"
REDFISH_SYSTEM = f"https://{HOST}/redfish/v1/Systems/1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action, reset_type",
    [("on", "On"), ("off", "ForceOff"), ("cycle", "ForceRestart"), ("reset", "ForceRestart")],
)
async def test_redfish_reset_maps_action_and_sends_basic_auth(
    monkeypatch: pytest.MonkeyPatch, action: str, reset_type: str
) -> None:
    """Each action POSTs its Redfish ResetType with the BMC credentials; 204 counts as success."""
    _no_cert_check(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(REDFISH_RESET).mock(return_value=httpx.Response(204))
        ok, msg = await PowerManager().power_control(_creds("redfish"), action)  # type: ignore[arg-type]

    assert ok is True
    assert msg == f"Redfish {action} successful"
    req = route.calls.last.request
    assert httpx.Response(200, content=req.content).json() == {"ResetType": reset_type}
    assert req.headers["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_redfish_non_success_status_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 from the BMC is a failure carrying the status code."""
    _no_cert_check(monkeypatch)
    with respx.mock:
        respx.post(REDFISH_RESET).mock(return_value=httpx.Response(403, text="forbidden"))
        ok, msg = await PowerManager().power_control(_creds("redfish"), "on")

    assert ok is False
    assert msg == "Redfish error: 403"


@pytest.mark.asyncio
async def test_redfish_transport_error_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport error during reset is returned as the failure message."""
    _no_cert_check(monkeypatch)
    with respx.mock:
        respx.post(REDFISH_RESET).mock(side_effect=httpx.ConnectError("bmc unreachable"))
        ok, msg = await PowerManager().power_control(_creds("redfish"), "off")

    assert ok is False
    assert msg == "bmc unreachable"


@pytest.mark.asyncio
async def test_redfish_invalid_action_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An action outside the Redfish map (and not status) is refused without any request."""
    _no_cert_check(monkeypatch)
    ok, msg = await PowerManager().power_control(_creds("redfish"), "nuke")  # type: ignore[arg-type]

    assert ok is False
    assert msg == "Invalid Redfish action: nuke"


@pytest.mark.asyncio
async def test_redfish_without_ca_or_pin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No REDFISH_CA_FILE and no pin: no request is made and the error says why."""
    monkeypatch.delenv("REDFISH_CA_FILE", raising=False)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(REDFISH_RESET).mock(return_value=httpx.Response(204))
        ok, msg = await PowerManager().power_control(_creds("redfish"), "on")

    assert route.call_count == 0
    assert ok is False
    assert "refusing Redfish without TLS verification" in msg


@pytest.mark.asyncio
async def test_redfish_status_returns_power_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status action returns the PowerState field of the system resource."""
    _no_cert_check(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(REDFISH_SYSTEM).mock(return_value=httpx.Response(200, json={"PowerState": "On"}))
        ok, state = await PowerManager().power_control(_creds("redfish"), "status")

    assert ok is True
    assert state == "On"


@pytest.mark.asyncio
async def test_redfish_status_defaults_to_unknown_when_field_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A system resource without PowerState reports 'Unknown' instead of failing."""
    _no_cert_check(monkeypatch)
    with respx.mock:
        respx.get(REDFISH_SYSTEM).mock(return_value=httpx.Response(200, json={}))
        ok, state = await PowerManager().power_control(_creds("redfish"), "status")

    assert ok is True
    assert state == "Unknown"


@pytest.mark.asyncio
async def test_redfish_status_non_200_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-200 status read is reported with its code."""
    _no_cert_check(monkeypatch)
    with respx.mock:
        respx.get(REDFISH_SYSTEM).mock(return_value=httpx.Response(503))
        ok, msg = await PowerManager().power_control(_creds("redfish"), "status")

    assert ok is False
    assert msg == "Status check failed: 503"


@pytest.mark.asyncio
async def test_redfish_status_exception_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport error during a status read is returned as the message."""
    _no_cert_check(monkeypatch)
    with respx.mock:
        respx.get(REDFISH_SYSTEM).mock(side_effect=httpx.ReadTimeout("slow bmc"))
        ok, msg = await PowerManager().power_control(_creds("redfish"), "status")

    assert ok is False
    assert msg == "slow bmc"


@pytest.mark.asyncio
async def test_redfish_status_without_ca_or_pin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status reads are gated by the same mandatory TLS verification."""
    monkeypatch.delenv("REDFISH_CA_FILE", raising=False)

    ok, msg = await PowerManager().power_control(_creds("redfish"), "status")

    assert ok is False
    assert "refusing Redfish without TLS verification" in msg


# --- Wake-on-LAN ---------------------------------------------------------------------

WOL_MAC = "00:11:22:33:44:55"


@pytest.mark.asyncio
async def test_wol_only_supports_power_on() -> None:
    """Wake-on-LAN cannot power a machine off; the request is refused."""
    ok, msg = await PowerManager().power_control(_creds("wol", address=WOL_MAC), "off")

    assert ok is False
    assert msg == "Wake-on-LAN only supports power on"


@pytest.mark.asyncio
async def test_wol_uses_wakeonlan_utility_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the wakeonlan utility installed, it is invoked with the MAC and success is reported."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(returncode=0), calls)

    ok, msg = await PowerManager().power_control(_creds("wol", address=WOL_MAC), "on")

    assert ok is True
    assert msg == "Wake-on-LAN packet sent"
    assert calls[0]["argv"] == ["wakeonlan", WOL_MAC]


@pytest.mark.asyncio
async def test_wol_utility_failure_exit_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wakeonlan exit code of non-zero is a failure."""
    _spawn(monkeypatch, FakeProc(returncode=1), [])

    ok, msg = await PowerManager().power_control(_creds("wol", address=WOL_MAC), "on")

    assert ok is False
    assert msg == "Wake-on-LAN failed"


@pytest.mark.asyncio
async def test_wol_unexpected_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-missing-binary failure while sending is reported, not raised."""
    _spawn(monkeypatch, FakeProc(communicate_error=RuntimeError("pipe broke")), [])

    ok, msg = await PowerManager().power_control(_creds("wol", address=WOL_MAC), "on")

    assert ok is False
    assert msg == "pipe broke"


class _FakeUDP:
    """UDP socket stand-in recording broadcast options and datagrams."""

    sent: list[tuple[bytes, tuple[str, int]]] = []
    options: list[tuple[int, int, int]] = []

    def __init__(self, family: int, kind: int) -> None:
        """Record the socket family and type."""
        self.family = family
        self.kind = kind

    def setsockopt(self, level: int, opt: int, value: int) -> None:
        """Record the socket option set."""
        _FakeUDP.options.append((level, opt, value))

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        """Record the broadcast datagram."""
        _FakeUDP.sent.append((data, addr))

    def close(self) -> None:
        """Close the fake socket."""


@pytest.mark.asyncio
async def test_wol_manual_fallback_sends_magic_packet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the utility, a standard magic packet (6x FF + 16x MAC) is broadcast on UDP/9."""
    _FakeUDP.sent = []
    _FakeUDP.options = []
    monkeypatch.setattr(socket, "socket", _FakeUDP)
    _spawn(monkeypatch, None, [], error=FileNotFoundError("wakeonlan"))

    ok, msg = await PowerManager().power_control(_creds("wol", address=WOL_MAC), "on")

    assert ok is True
    assert msg == "Wake-on-LAN magic packet sent"
    assert len(_FakeUDP.sent) == 1
    data, addr = _FakeUDP.sent[0]
    assert data == b"\xff" * 6 + bytes.fromhex("001122334455") * 16
    assert addr == ("255.255.255.255", 9)
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in _FakeUDP.options


@pytest.mark.asyncio
async def test_wol_manual_rejects_wrong_length_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MAC that is not exactly 6 bytes never produces a packet."""
    _FakeUDP.sent = []
    monkeypatch.setattr(socket, "socket", _FakeUDP)

    ok, msg = await PowerManager()._send_wol_manual("00:11:22:33")

    assert ok is False
    assert msg == "Invalid MAC address length: 4 bytes"
    assert _FakeUDP.sent == []


@pytest.mark.asyncio
async def test_wol_manual_rejects_non_hex_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MAC with non-hex characters fails parsing and is reported."""
    _FakeUDP.sent = []
    monkeypatch.setattr(socket, "socket", _FakeUDP)

    ok, msg = await PowerManager()._send_wol_manual("zz:zz:zz:zz:zz:zz")

    assert ok is False
    assert msg


@pytest.mark.asyncio
async def test_wol_manual_socket_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broadcast socket failure is returned as a failure message."""

    class BrokenUDP(_FakeUDP):
        """UDP socket whose send fails."""

        def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
            """Simulate a network unreachable error."""
            raise OSError("network unreachable")

    monkeypatch.setattr(socket, "socket", BrokenUDP)

    ok, msg = await PowerManager()._send_wol_manual(WOL_MAC)

    assert ok is False
    assert msg == "network unreachable"


# --- boot device ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_boot_device_requires_ipmi() -> None:
    """Boot device selection is only implemented over IPMI."""
    ok, msg = await PowerManager().set_boot_device(_creds("redfish"), "pxe")

    assert ok is False
    assert msg == "Boot device setting only supported via IPMI"


@pytest.mark.asyncio
async def test_set_boot_device_rejects_unknown_device() -> None:
    """Only pxe, disk and bios are accepted boot devices."""
    ok, msg = await PowerManager().set_boot_device(_creds("ipmi"), "floppy")  # type: ignore[arg-type]

    assert ok is False
    assert msg == "Invalid boot device: floppy"


@pytest.mark.asyncio
async def test_set_boot_device_one_shot_uses_efiboot_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-persistent PXE selection uses options=efiboot and keeps the password out of argv."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(returncode=0), calls)

    ok, msg = await PowerManager().set_boot_device(_creds("ipmi"), "pxe")

    assert ok is True
    assert msg == "Boot device set to pxe"
    argv = calls[0]["argv"]
    assert argv[-4:] == ["chassis", "bootdev", "pxe", "options=efiboot"]
    assert "-E" in argv and "s3cret-pass" not in argv
    assert calls[0]["env"]["IPMI_PASSWORD"] == "s3cret-pass"


@pytest.mark.asyncio
async def test_set_boot_device_persistent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent selection passes the 'persistent' keyword instead."""
    calls: list[dict[str, Any]] = []
    _spawn(monkeypatch, FakeProc(returncode=0), calls)

    ok, _ = await PowerManager().set_boot_device(_creds("ipmi"), "disk", persistent=True)

    assert ok is True
    assert calls[0]["argv"][-1] == "persistent"


@pytest.mark.asyncio
async def test_set_boot_device_failure_returns_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing bootdev command returns its stderr text."""
    _spawn(monkeypatch, FakeProc(returncode=1, stderr=b"Set Boot Device failed\n"), [])

    ok, msg = await PowerManager().set_boot_device(_creds("ipmi"), "bios")

    assert ok is False
    assert msg == "Set Boot Device failed"


@pytest.mark.asyncio
async def test_set_boot_device_exception_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn errors during bootdev are returned as messages."""
    _spawn(monkeypatch, None, [], error=FileNotFoundError("ipmitool"))

    ok, msg = await PowerManager().set_boot_device(_creds("ipmi"), "pxe")

    assert ok is False
    assert "ipmitool" in msg
