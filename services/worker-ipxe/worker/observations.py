"""Process-local record of which IPs recently spoke ProxyDHCP for which MAC.

A logged/metric signal only: ProxyDHCP often does not see the client's final address and
the packets are spoofable, so an observation never grants or denies anything.
"""

import time

OBSERVATION_TTL_SECONDS = 600
MAX_OBSERVATIONS = 4096


class ObservedClients:
    """Bounded (MAC, IP) -> first-seen timestamp map with expiry; live entries are never overwritten."""

    def __init__(self) -> None:
        """Start with no observations."""
        self._seen: dict[tuple[str, str], float] = {}

    def record(self, mac: str, ip: str) -> None:
        """Remember one record per (MAC, IP) (normalised 12-hex MAC); an existing live record is kept."""
        if not ip or ip in ("0.0.0.0", "::"):
            return
        now = time.monotonic()
        existing = self._seen.get((mac, ip))
        if existing is not None and now - existing <= OBSERVATION_TTL_SECONDS:
            return
        if len(self._seen) >= MAX_OBSERVATIONS and (mac, ip) not in self._seen:
            self._evict(now)
        self._seen[(mac, ip)] = now

    def lookup(self, mac: str) -> set[str]:
        """Return the live observed IPs for `mac` (empty if none)."""
        now = time.monotonic()
        return {ip for (m, ip), ts in self._seen.items() if m == mac and now - ts <= OBSERVATION_TTL_SECONDS}

    def _evict(self, now: float) -> None:
        """Drop expired entries, then the oldest one if still full (memory bound only)."""
        for key in [k for k, ts in self._seen.items() if now - ts > OBSERVATION_TTL_SECONDS]:
            del self._seen[key]
        if len(self._seen) >= MAX_OBSERVATIONS:
            del self._seen[min(self._seen, key=lambda k: self._seen[k])]
