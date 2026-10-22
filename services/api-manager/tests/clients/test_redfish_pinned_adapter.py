"""Direct tests of the BMC certificate-pinning transport adapter in app/clients/redfish.py.

``_PinnedAdapter.send`` is the per-request gate that compares the live BMC
certificate fingerprint against the pinned one before any request bytes leave.
These tests drive it with a stubbed TLS fingerprint probe and a stubbed parent
``HTTPAdapter.send`` so the assertions cover: first-connect capture, matching
pin, mismatch refusal (parent never called), timeout normalisation and default
port selection.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from requests.adapters import HTTPAdapter

from app.clients import redfish
from app.clients.redfish import BMCCertPinMismatch, _PinnedAdapter

PIN_A = "AA:" * 31 + "AA"
PIN_B = "BB:" * 31 + "BB"


@pytest.fixture()
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the out-of-band fingerprint probe and record its arguments."""
    rec: dict[str, Any] = {"calls": [], "fingerprint": PIN_A}

    def _fetch(host: str, port: int, timeout: float) -> str:
        rec["calls"].append((host, port, timeout))
        return rec["fingerprint"]

    monkeypatch.setattr(redfish, "_fetch_cert_fingerprint", _fetch)
    return rec


@pytest.fixture()
def parent_send(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace HTTPAdapter.send so reaching the parent is observable."""
    sent: list[Any] = []

    def _send(self: Any, request: Any, **kwargs: Any) -> str:
        sent.append((request, kwargs))
        return "RESPONSE"

    monkeypatch.setattr(HTTPAdapter, "send", _send)
    return sent


def _request(url: str) -> SimpleNamespace:
    """Minimal PreparedRequest stand-in carrying only ``url``."""
    return SimpleNamespace(url=url)


def test_first_connect_captures_fingerprint_and_forwards(probe: dict, parent_send: list) -> None:
    """With no pin configured the live fingerprint is captured and the request proceeds."""
    adapter = _PinnedAdapter(expected_fingerprint=None)

    result = adapter.send(_request("https://10.0.0.9/redfish/v1"), timeout=5)

    assert result == "RESPONSE"
    assert adapter.captured_fingerprint == PIN_A
    assert probe["calls"] == [("10.0.0.9", 443, 5.0)]
    assert len(parent_send) == 1


def test_matching_pin_forwards_request(probe: dict, parent_send: list) -> None:
    """A live fingerprint equal to the pin lets the request through."""
    adapter = _PinnedAdapter(expected_fingerprint=PIN_A)

    assert adapter.send(_request("https://bmc.lan:8443/redfish")) == "RESPONSE"
    assert adapter.captured_fingerprint == PIN_A
    assert probe["calls"] == [("bmc.lan", 8443, redfish.DEFAULT_TIMEOUT_SEC)]


def test_mismatched_pin_refuses_before_parent_send(probe: dict, parent_send: list) -> None:
    """A rotated/substituted certificate raises BMCCertPinMismatch and no bytes are sent."""
    probe["fingerprint"] = PIN_B
    adapter = _PinnedAdapter(expected_fingerprint=PIN_A)

    with pytest.raises(BMCCertPinMismatch, match="fingerprint mismatch for bmc.lan:443"):
        adapter.send(_request("https://bmc.lan/redfish"))

    assert parent_send == []
    assert adapter.captured_fingerprint == PIN_B


def test_pin_comparison_is_format_insensitive(probe: dict, parent_send: list) -> None:
    """A pin given lowercase without colons is normalised and still matches the live value."""
    probe["fingerprint"] = PIN_A
    adapter = _PinnedAdapter(expected_fingerprint=PIN_A.replace(":", "").lower())

    assert adapter.send(_request("https://bmc.lan/")) == "RESPONSE"


def test_tuple_timeout_uses_connect_component(probe: dict, parent_send: list) -> None:
    """A (connect, read) timeout tuple probes with the connect timeout only."""
    adapter = _PinnedAdapter(expected_fingerprint=None)

    adapter.send(_request("https://bmc.lan/"), timeout=(3.5, 60))

    assert probe["calls"] == [("bmc.lan", 443, 3.5)]
    assert parent_send[0][1] == {"timeout": (3.5, 60)}


def test_invalid_pin_rejected_at_construction() -> None:
    """A malformed expected fingerprint fails when the adapter is built, not on first request."""
    with pytest.raises(ValueError, match="invalid SHA-256 fingerprint length"):
        _PinnedAdapter(expected_fingerprint="ABCD")


def test_empty_pin_treated_as_unpinned() -> None:
    """An empty string pin means 'capture only' (no pin enforced)."""
    adapter = _PinnedAdapter(expected_fingerprint="")

    assert adapter.expected_fingerprint is None
