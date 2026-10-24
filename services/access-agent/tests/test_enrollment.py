"""Behaviour tests for AgentEnrollment against a mocked management server.

httpx.MockTransport stands in for the management server; no sockets are opened.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent import enrollment as enrollment_mod
from agent.config import AgentConfig
from agent.enrollment import AgentEnrollment, EnrollmentError

Handler = Callable[[httpx.Request], httpx.Response]


def make_enrollment(tmp_path: Path, handler: Handler, **overrides: Any) -> AgentEnrollment:
    """Build an enrollment handler whose HTTP client routes through ``handler``.

    Args:
        tmp_path: Directory used for the token and CA files.
        handler: Request handler standing in for the management server.
        **overrides: Extra AgentConfig fields.

    Returns:
        Enrollment handler with an injected MockTransport-backed client.
    """
    values: dict[str, Any] = {
        "management_server_url": "https://mgmt.test",
        "enrollment_key": "ek-123",
        "hostname": "host-a",
        "token_file": str(tmp_path / "state" / "tokens.json"),
        "ca_public_key_file": str(tmp_path / "state" / "ca.pub"),
    }
    values.update(overrides)
    enr = AgentEnrollment(AgentConfig(**values))
    enr._http_client = httpx.Client(
        base_url="https://mgmt.test", transport=httpx.MockTransport(handler)
    )
    return enr


def enroll_ok_body(**extra: Any) -> dict[str, Any]:
    """Return a 201 enrollment response body with optional extra keys."""
    body: dict[str, Any] = {
        "agent_id": "agent-1",
        "access_token": "acc-1",
        "refresh_token": "ref-1",
        "ca_public_key": "ssh-ed25519 AAAAC3 gough-ca",
    }
    body.update(extra)
    return body


def test_http_client_property_is_built_from_config(tmp_path: Path) -> None:
    """The lazily-built client targets the configured base URL and verify setting."""
    enr = AgentEnrollment(AgentConfig(management_server_url="https://x.test", verify_ssl=False))
    client = enr.http_client
    assert str(client.base_url) == "https://x.test"
    assert enr.http_client is client
    enr.close()
    assert enr._http_client is None
    enr.close()  # closing twice is a no-op


def test_enroll_without_key_raises_before_any_request(tmp_path: Path) -> None:
    """Enrollment refuses to run when no enrollment key is configured."""
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(500)

    enr = make_enrollment(tmp_path, handler, enrollment_key=None)
    with pytest.raises(EnrollmentError, match="No enrollment key"):
        enr.enroll()
    assert calls == []


def test_enroll_success_sends_key_and_persists_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 201 stores identity on the config, writes tokens and the CA key file."""
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["key"] = req.headers["X-Enrollment-Key"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json=enroll_ok_body(config={"heartbeat_interval": 45}))

    monkeypatch.setattr(AgentEnrollment, "_get_primary_ip", lambda self: "10.1.2.3")
    enr = make_enrollment(tmp_path, handler, capabilities=["ssh"])
    agent_id, access, refresh = enr.enroll()

    assert (agent_id, access, refresh) == ("agent-1", "acc-1", "ref-1")
    assert seen["path"] == "/api/v1/agents/enroll"
    assert seen["key"] == "ek-123"
    assert seen["body"] == {
        "hostname": "host-a",
        "ip_address": "10.1.2.3",
        "agent_version": enrollment_mod.__version__,
        "capabilities": ["ssh"],
    }
    assert enr.config.heartbeat_interval == 45
    assert (tmp_path / "state" / "ca.pub").read_text() == "ssh-ed25519 AAAAC3 gough-ca"
    saved = json.loads((tmp_path / "state" / "tokens.json").read_text())
    assert saved["agent_id"] == "agent-1" and saved["refresh_token"] == "ref-1"
    assert "saved_at" in saved


def test_enroll_without_optional_fields_skips_ca_and_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing CA key and server config leave the CA file unwritten and interval unchanged."""
    monkeypatch.setattr(AgentEnrollment, "_get_primary_ip", lambda self: None)
    body = {"agent_id": "a", "access_token": "t", "refresh_token": "r"}
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(201, json=body), heartbeat_interval=30)
    enr.enroll()
    assert not (tmp_path / "state" / "ca.pub").exists()
    assert enr.config.heartbeat_interval == 30
    assert enr.config.ca_public_key is None


def test_token_file_is_mode_0600_from_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The token file must never be world-readable, not even while it is being written.

    The mode is sampled from inside json.dump, i.e. after the file exists but before
    the write completes, so a create-then-chmod implementation fails here.
    """
    observed: list[int] = []
    real_dump = json.dump

    def recording_dump(obj: Any, fp: Any, **kwargs: Any) -> None:
        observed.append(stat.S_IMODE(os.fstat(fp.fileno()).st_mode))
        real_dump(obj, fp, **kwargs)

    monkeypatch.setattr(enrollment_mod.json, "dump", recording_dump)
    monkeypatch.setattr(os, "umask", lambda _m: 0o022)  # ensure a permissive umask in play
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(201, json=enroll_ok_body()))
    monkeypatch.setattr(AgentEnrollment, "_get_primary_ip", lambda self: None)
    enr.enroll()

    token_path = tmp_path / "state" / "tokens.json"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert observed == [0o600]


def test_save_tokens_tightens_existing_world_readable_file(tmp_path: Path) -> None:
    """A pre-existing 0644 token file is reset to 0600 on the next save."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    enr.config.agent_id = "a"
    enr.config.access_token = "t"
    enr.config.refresh_token = "r"
    token_path = tmp_path / "state" / "tokens.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}")
    token_path.chmod(0o644)
    enr.save_tokens()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "Invalid or expired enrollment key"),
        (409, "Enrollment key already used"),
    ],
)
def test_enroll_maps_auth_and_conflict_statuses(
    tmp_path: Path, status: int, fragment: str
) -> None:
    """401 and 409 map to specific, actionable enrollment errors."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(EnrollmentError, match=fragment):
        enr.enroll()
    assert not (tmp_path / "state" / "tokens.json").exists()


def test_enroll_server_error_with_json_body_is_reported(tmp_path: Path) -> None:
    """A 5xx with a JSON error message surfaces that message (after retries are spent)."""
    enr = make_enrollment(
        tmp_path,
        lambda req: httpx.Response(500, json={"error": "db down"}),
        enroll_max_attempts=1,
    )
    with pytest.raises(EnrollmentError, match="Enrollment failed: db down"):
        enr.enroll()


def test_enroll_non_json_error_body_still_raises_enrollment_error(tmp_path: Path) -> None:
    """A proxy HTML error page must become EnrollmentError, not a raw JSON decode failure."""
    enr = make_enrollment(
        tmp_path,
        lambda req: httpx.Response(502, text="<html>Bad Gateway</html>"),
        enroll_max_attempts=1,
    )
    with pytest.raises(EnrollmentError, match="Enrollment failed"):
        enr.enroll()


def test_enroll_network_error_is_wrapped(tmp_path: Path) -> None:
    """A transport failure during enrollment is wrapped in EnrollmentError."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    enr = make_enrollment(tmp_path, handler, enroll_max_attempts=1)
    with pytest.raises(EnrollmentError, match="Network error during enrollment"):
        enr.enroll()


def test_refresh_without_refresh_token_raises(tmp_path: Path) -> None:
    """Refresh needs a refresh token; without one it fails before any request."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    with pytest.raises(EnrollmentError, match="No refresh token"):
        enr.refresh_tokens()


def test_refresh_success_rotates_and_persists_tokens(tmp_path: Path) -> None:
    """A 200 replaces both tokens on the config and saves them to disk."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["Authorization"]
        return httpx.Response(200, json={"access_token": "acc-2", "refresh_token": "ref-2"})

    enr = make_enrollment(tmp_path, handler, refresh_token="ref-1", agent_id="agent-1")
    access, refresh = enr.refresh_tokens()
    assert (access, refresh) == ("acc-2", "ref-2")
    assert seen["auth"] == "Bearer ref-1"
    saved = json.loads((tmp_path / "state" / "tokens.json").read_text())
    assert saved["access_token"] == "acc-2" and saved["refresh_token"] == "ref-2"


def test_refresh_401_means_revoked(tmp_path: Path) -> None:
    """A 401 on refresh reports the refresh token as expired or revoked."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(401), refresh_token="ref-1")
    with pytest.raises(EnrollmentError, match="expired or revoked"):
        enr.refresh_tokens()


def test_refresh_server_error_is_reported(tmp_path: Path) -> None:
    """Non-200, non-401 refresh responses surface the server's error message."""
    enr = make_enrollment(
        tmp_path, lambda req: httpx.Response(500, json={"error": "kaboom"}), refresh_token="ref-1"
    )
    with pytest.raises(EnrollmentError, match="Token refresh failed: kaboom"):
        enr.refresh_tokens()


def test_refresh_network_error_is_wrapped(tmp_path: Path) -> None:
    """Transport failure during refresh is wrapped in EnrollmentError."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    enr = make_enrollment(tmp_path, handler, refresh_token="ref-1")
    with pytest.raises(EnrollmentError, match="Network error during token refresh"):
        enr.refresh_tokens()


def test_is_enrolled_requires_all_three_fields(tmp_path: Path) -> None:
    """is_enrolled is true only when access, refresh and agent id are all present."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    path = Path(enr.config.token_file)
    assert enr.is_enrolled() is False
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"access_token": "a", "refresh_token": "r"}))
    assert enr.is_enrolled() is False
    path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "agent_id": "x"}))
    assert enr.is_enrolled() is True


def test_is_enrolled_false_for_corrupt_file(tmp_path: Path) -> None:
    """A corrupt token file means not enrolled, not a crash."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    path = Path(enr.config.token_file)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert enr.is_enrolled() is False


def test_load_tokens_populates_config(tmp_path: Path) -> None:
    """load_tokens copies every persisted field into the config."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    path = Path(enr.config.token_file)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"agent_id": "a1", "access_token": "t1", "refresh_token": "r1", "ca_public_key": "k"}
        )
    )
    assert enr.load_tokens() is True
    assert (enr.config.agent_id, enr.config.access_token, enr.config.refresh_token) == ("a1", "t1", "r1")
    assert enr.config.ca_public_key == "k"


def test_load_tokens_missing_or_corrupt_returns_false(tmp_path: Path) -> None:
    """Missing and unparseable token files both return False without raising."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    assert enr.load_tokens() is False
    path = Path(enr.config.token_file)
    path.parent.mkdir(parents=True)
    path.write_text("][")
    assert enr.load_tokens() is False


def test_save_ca_key_is_noop_without_key(tmp_path: Path) -> None:
    """_save_ca_public_key writes nothing when no CA key is held."""
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500), ca_public_key=None)
    enr._save_ca_public_key()
    assert not (tmp_path / "state" / "ca.pub").exists()


def test_primary_ip_returns_socket_address(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_get_primary_ip reports the local address the kernel picks for the route."""

    class FakeUDP:
        def __init__(self, *_a: object) -> None:
            self.closed = False

        def connect(self, _addr: tuple[str, int]) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("192.168.7.9", 0)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(socket, "socket", FakeUDP)
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    assert enr._get_primary_ip() == "192.168.7.9"


def test_primary_ip_none_when_no_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A host without a default route yields None instead of raising."""

    class NoRoute:
        def __init__(self, *_a: object) -> None:
            raise OSError("network unreachable")

    monkeypatch.setattr(socket, "socket", NoRoute)
    enr = make_enrollment(tmp_path, lambda req: httpx.Response(500))
    assert enr._get_primary_ip() is None
