"""Process-local record of which IP recently spoke ProxyDHCP for which MAC.

Used only as a soft signal when minting boot tokens: ProxyDHCP often does not see the
client's final address, so a mismatch is logged and counted, never a hard failure.
"""

import time

OBSERVATION_TTL_SECONDS = 600
MAX_OBSERVATIONS = 4096


class ObservedClients:
    """Bounded MAC -> (IP, timestamp) map with expiry."""

    def __init__(self) -> None:
        """Start with no observations."""
        self._seen: dict[str, tuple[str, float]] = {}

    def record(self, mac: str, ip: str) -> None:
        """Remember `ip` for `mac` (normalised 12-hex MAC); unspecified addresses are ignored."""
        if not ip or ip in ("0.0.0.0", "::"):
            return
        if len(self._seen) >= MAX_OBSERVATIONS and mac not in self._seen:
            self._evict()
        self._seen[mac] = (ip, time.monotonic())

    def lookup(self, mac: str) -> str | None:
        """Return the IP seen for `mac` within the TTL, else None."""
        entry = self._seen.get(mac)
        if entry is None:
            return None
        if time.monotonic() - entry[1] > OBSERVATION_TTL_SECONDS:
            del self._seen[mac]
            return None
        return entry[0]

    def _evict(self) -> None:
        """Drop expired entries, then the oldest one if still full."""
        now = time.monotonic()
        for key in [k for k, (_, ts) in self._seen.items() if now - ts > OBSERVATION_TTL_SECONDS]:
            del self._seen[key]
        if len(self._seen) >= MAX_OBSERVATIONS:
            del self._seen[min(self._seen, key=lambda k: self._seen[k][1])]
