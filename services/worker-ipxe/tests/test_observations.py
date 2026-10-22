"""ObservedClients (ProxyDHCP MAC->IP hints) and the ProxyDHCP recording hook."""

import asyncio

import pytest

from test_dhcp_packets import ADDR, PXE_DISCOVER, FakeTransport, _server
from worker import observations as obs
from worker.observations import ObservedClients

MAC = "aabbccddeeff"


def test_record_and_lookup() -> None:
    """A recorded IP is returned for its MAC; unknown MACs return None."""
    seen = ObservedClients()
    seen.record(MAC, "10.0.0.5")

    assert seen.lookup(MAC) == "10.0.0.5"
    assert seen.lookup("112233445566") is None


@pytest.mark.parametrize("ip", ["", "0.0.0.0", "::"])
def test_unspecified_addresses_are_ignored(ip: str) -> None:
    """Broadcast-style source addresses carry no information and are not stored."""
    seen = ObservedClients()
    seen.record(MAC, ip)

    assert seen.lookup(MAC) is None


def test_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    """An observation older than the TTL is dropped on lookup."""
    seen = ObservedClients()
    seen.record(MAC, "10.0.0.5")
    now = obs.time.monotonic()
    monkeypatch.setattr(obs.time, "monotonic", lambda: now + obs.OBSERVATION_TTL_SECONDS + 1)

    assert seen.lookup(MAC) is None


def test_bounded_size_evicts_expired_then_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """At capacity a new MAC evicts expired entries first, otherwise the oldest."""
    monkeypatch.setattr(obs, "MAX_OBSERVATIONS", 3)
    clock = [1000.0]
    monkeypatch.setattr(obs.time, "monotonic", lambda: clock[0])
    seen = ObservedClients()
    for i, mac in enumerate(["a", "b", "c"]):
        clock[0] += 1
        seen.record(mac, f"10.0.0.{i}")
    clock[0] += 1
    seen.record("d", "10.0.0.9")  # full, none expired: oldest ("a") goes

    assert seen.lookup("a") is None and seen.lookup("d") == "10.0.0.9"

    clock[0] += obs.OBSERVATION_TTL_SECONDS + 1
    seen.record("e", "10.0.0.10")  # everything older is expired and swept

    assert seen.lookup("b") is None and seen.lookup("e") == "10.0.0.10"


def test_proxydhcp_records_pxe_client_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PXE DISCOVER records the MAC (separators stripped) against the datagram's source IP."""
    seen = ObservedClients()
    server = _server(monkeypatch)
    server.observed = seen
    server.transport = FakeTransport()  # type: ignore[assignment]

    asyncio.run(server._handle_packet(PXE_DISCOVER, ADDR))

    assert seen.lookup("005056c00008") == ADDR[0]
