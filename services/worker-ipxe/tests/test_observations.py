"""ObservedClients (ProxyDHCP MAC/IP hints): signal only, one record per pair, never overwritten."""

import asyncio

import pytest

from test_dhcp_packets import ADDR, PXE_DISCOVER, FakeTransport, _server
from worker import observations as obs
from worker.observations import ObservedClients

MAC = "aabbccddeeff"


def test_record_and_lookup() -> None:
    """Recorded IPs are returned per MAC (several allowed); unknown MACs return an empty set."""
    seen = ObservedClients()
    seen.record(MAC, "10.0.0.5")
    seen.record(MAC, "10.0.0.6")

    assert seen.lookup(MAC) == {"10.0.0.5", "10.0.0.6"}
    assert seen.lookup("112233445566") == set()


@pytest.mark.parametrize("ip", ["", "0.0.0.0", "::"])
def test_unspecified_addresses_are_ignored(ip: str) -> None:
    """Broadcast-style source addresses carry no information and are not stored."""
    seen = ObservedClients()
    seen.record(MAC, ip)

    assert seen.lookup(MAC) == set()


def test_live_record_is_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-observing a live (MAC, IP) keeps the first timestamp, so it still expires on schedule."""
    clock = [1000.0]
    monkeypatch.setattr(obs.time, "monotonic", lambda: clock[0])
    seen = ObservedClients()
    seen.record(MAC, "10.0.0.5")
    clock[0] += 500
    seen.record(MAC, "10.0.0.5")
    clock[0] += 101  # 601s after the FIRST sighting

    assert seen.lookup(MAC) == set()
    seen.record(MAC, "10.0.0.5")  # expired record may be re-recorded
    assert seen.lookup(MAC) == {"10.0.0.5"}


def test_bounded_size_evicts_expired_then_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """At capacity a new pair evicts expired entries first, otherwise the oldest (memory bound only)."""
    monkeypatch.setattr(obs, "MAX_OBSERVATIONS", 3)
    clock = [1000.0]
    monkeypatch.setattr(obs.time, "monotonic", lambda: clock[0])
    seen = ObservedClients()
    for mac in ["a", "b", "c"]:
        clock[0] += 1
        seen.record(mac, "10.0.0.1")
    clock[0] += 1
    seen.record("d", "10.0.0.9")

    assert seen.lookup("a") == set() and seen.lookup("d") == {"10.0.0.9"}

    clock[0] += obs.OBSERVATION_TTL_SECONDS + 1
    seen.record("e", "10.0.0.10")

    assert seen.lookup("b") == set() and seen.lookup("e") == {"10.0.0.10"}


def test_proxydhcp_records_pxe_client_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PXE DISCOVER records the MAC (separators stripped) against the datagram's source IP."""
    seen = ObservedClients()
    server = _server(monkeypatch)
    server.observed = seen
    server.transport = FakeTransport()  # type: ignore[assignment]

    asyncio.run(server._handle_packet(PXE_DISCOVER, ADDR))

    assert seen.lookup("005056c00008") == {ADDR[0]}
