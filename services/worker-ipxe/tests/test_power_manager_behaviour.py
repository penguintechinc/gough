"""Behaviour tests for worker.services.power_manager: IPMI, Redfish, WOL, boot device and BMC pinning.

External tools and network are faked: ipmitool/wakeonlan via a fake subprocess, Redfish via respx,
TLS/socket fingerprinting via fake socket and SSL objects.
"""

import asyncio
import hashlib
import socket
import ssl
from typing import Any

import httpx
import pytest
import respx

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
    """Bypass BMC certificate verification for control-path tests."""
    monkeypatch.setattr(pm, "_verify_bmc_cert", lambda *a, **k: True)


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


class _FakeTLS:
    """TLS socket stand-in returning a fixed DER certificate."""

    def __init__(self, der: bytes | None) -> None:
        """Remember the DER bytes to return from getpeercert."""
        self._der = der

    def __enter__(self) -> "_FakeTLS":
        """Enter the context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Leave the context manager without suppressing errors."""

    def getpeercert(self, binary_form: bool = False) -> bytes | None:
        """Return the DER certificate when binary form is requested."""
        assert binary_form is True
        return self._der


class _FakeRaw:
    """Plain socket stand-in usable as a context manager."""

    def __enter__(self) -> "_FakeRaw":
        """Enter the context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Leave the context manager without suppressing errors."""


class _FakeCtx:
    """SSL context stand-in recording the verification settings used."""

    def __init__(self, der: bytes | None, settings: dict[str, Any]) -> None:
        """Hold the certificate to present and a dict to record settings into."""
        self._der = der
        self._settings = settings
        self.check_hostname = True
        self.verify_mode = None

    def wrap_socket(self, raw: object, server_hostname: str) -> _FakeTLS:
        """Record the SNI host and present the fake certificate."""
        self._settings["sni"] = server_hostname
        self._settings["check_hostname"] = self.check_hostname
        self._settings["verify_mode"] = self.verify_mode
        return _FakeTLS(self._der)


def _fake_tls(monkeypatch: pytest.MonkeyPatch, der: bytes | None, settings: dict[str, Any],
              connect_error: Exception | None = None) -> None:
    """Patch socket.create_connection and ssl context creation for fingerprint fetches."""

    def fake_connect(address: tuple[str, int], timeout: float) -> _FakeRaw:
        """Simulate TCP connect to the BMC, optionally failing."""
        settings["address"] = address
        if connect_error is not None:
            raise connect_error
        return _FakeRaw()

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    monkeypatch.setattr(ssl, "create_default_context", lambda: _FakeCtx(der, settings))


def test_get_fingerprint_is_sha256_of_peer_der(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live fingerprint is the uppercase SHA-256 of the presented DER certificate."""
    der = b"\x30\x82fake-der-bytes"
    settings: dict[str, Any] = {}
    _fake_tls(monkeypatch, der, settings)

    fp = pm._get_bmc_cert_fingerprint(HOST, 8443, timeout=3.0)

    assert fp == hashlib.sha256(der).hexdigest().upper()
    assert settings["address"] == (HOST, 8443)
    assert settings["sni"] == HOST
    assert settings["check_hostname"] is False
    assert settings["verify_mode"] == ssl.CERT_NONE


def test_get_fingerprint_socket_error_is_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TCP/TLS failure is re-raised as RuntimeError naming host and port."""
    _fake_tls(monkeypatch, b"x", {}, connect_error=ConnectionRefusedError("refused"))

    with pytest.raises(RuntimeError, match=rf"TLS handshake failed for {HOST}:443"):
        pm._get_bmc_cert_fingerprint(HOST)


def test_get_fingerprint_without_peer_certificate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TLS session that yields no peer certificate is an error, never an empty fingerprint."""
    _fake_tls(monkeypatch, None, {})

    with pytest.raises(RuntimeError, match="No peer certificate"):
        pm._get_bmc_cert_fingerprint(HOST)


def test_verify_without_pin_fails_closed_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no pin and insecure mode off, verification refuses before opening any connection."""
    monkeypatch.delenv("BMC_ALLOW_INSECURE_TLS", raising=False)
    touched: list[bool] = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: touched.append(True))

    with pytest.raises(RuntimeError, match="no certificate pin configured"):
        pm._verify_bmc_cert(HOST, None)

    assert touched == []


@pytest.mark.parametrize("flag", ["true", "1", "YES"])
def test_verify_without_pin_allowed_when_insecure_flag_set(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """BMC_ALLOW_INSECURE_TLS (true/1/yes, any case) explicitly permits unpinned connections."""
    monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", flag)

    assert pm._verify_bmc_cert(HOST, None) is True


def test_verify_with_matching_pin_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live certificate whose fingerprint equals the pin (any spelling) verifies."""
    der = b"matching-cert"
    _fake_tls(monkeypatch, der, {})
    pin = hashlib.sha256(der).hexdigest()

    assert pm._verify_bmc_cert(HOST, pin.lower()) is True


def test_verify_with_mismatched_pin_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A different live certificate is rejected with both fingerprints in the message."""
    _fake_tls(monkeypatch, b"attacker-cert", {})

    with pytest.raises(RuntimeError, match="cert fingerprint mismatch"):
        pm._verify_bmc_cert(HOST, PIN)


def test_verify_with_malformed_stored_pin_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt stored pin is reported as a stored-format error, before any network I/O."""
    touched: list[bool] = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: touched.append(True))

    with pytest.raises(RuntimeError, match="Invalid stored fingerprint format"):
        pm._verify_bmc_cert(HOST, "not-a-fingerprint")

    assert touched == []


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
async def test_redfish_certificate_failure_blocks_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """If certificate verification fails, no reset is sent and the reason is returned."""

    def bad_cert(*a: object, **k: object) -> bool:
        """Simulate a pin mismatch."""
        raise RuntimeError("cert fingerprint mismatch")

    monkeypatch.setattr(pm, "_verify_bmc_cert", bad_cert)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(REDFISH_RESET).mock(return_value=httpx.Response(204))
        ok, msg = await PowerManager().power_control(_creds("redfish", cert_fingerprint=PIN), "on")

    assert route.call_count == 0
    assert ok is False
    assert msg == "Certificate verification failed: cert fingerprint mismatch"


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
async def test_redfish_status_certificate_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status reads are also gated by certificate verification."""

    def bad_cert(*a: object, **k: object) -> bool:
        """Simulate an unpinned BMC."""
        raise RuntimeError("no pin")

    monkeypatch.setattr(pm, "_verify_bmc_cert", bad_cert)

    ok, msg = await PowerManager().power_control(_creds("redfish"), "status")

    assert ok is False
    assert msg == "Certificate verification failed: no pin"


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
