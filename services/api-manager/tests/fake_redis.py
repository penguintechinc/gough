"""Minimal in-test Redis double (set NX/EX, getdel, delete) for ticket-store tests."""

from __future__ import annotations

import time


class FakeRedis:
    """Dict-backed stand-in implementing only the commands the store uses."""

    def __init__(self) -> None:
        """Start empty."""
        self.data: dict[str, tuple[float | None, str]] = {}

    def _live(self, key: str) -> str | None:
        item = self.data.get(key)
        if item is None:
            return None
        if item[0] is not None and item[0] <= time.monotonic():
            del self.data[key]
            return None
        return item[1]

    def set(self, key, value, nx=False, ex=None):
        """SET with NX/EX semantics."""
        if nx and self._live(key) is not None:
            return None
        self.data[key] = (time.monotonic() + ex if ex else None, value)
        return True

    def getdel(self, key):
        """Atomic get-and-delete."""
        val = self._live(key)
        self.data.pop(key, None)
        return val

    def delete(self, key):
        """Delete a key."""
        self.data.pop(key, None)


def make_store():
    """Return a ShellTicketStore wired to a FakeRedis (no network)."""
    from app.shell_tickets import ShellTicketStore

    store = ShellTicketStore(None)
    store._redis = FakeRedis()
    return store
