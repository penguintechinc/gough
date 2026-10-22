"""Feature-flag evaluation and node-count license entitlement.

Both concerns are served by ``license.penguintech.io``: a self-hosted PostHog
backs general-enablement feature flags, and the ``/api/v2`` entitlement API
meters how many nodes a deployment may activate. Every lookup is cached and
fails soft -- a licensing outage must never take an operator's control plane
down with it, so a failed refresh falls back to the last known-good value and
only a never-seen lookup falls back to the conservative default.

Node accounting deliberately follows "inventory is free, activation is
metered": a bare-metal node discovered over PXE and a pre-existing cloud VM
synced in from an operator's AWS account both cost nothing. Only nodes gough
has actually deployed to, and cloud machines gough itself provisioned, count
against the allowance. See :func:`count_active_nodes`.

Environment:
    ``POSTHOG_KEY``          PostHog project API key. Unset => all flags OFF.
    ``POSTHOG_HOST``         Flag endpoint. Default ``https://license.penguintech.io``.
    ``LICENSE_KEY``          ``PENG-XXXX-...``. Unset => free tier.
    ``LICENSE_SERVER_URL``   Entitlement API. Default ``https://license.penguintech.io``.
    ``PRODUCT_NAME``         Product identifier sent on validate. Default ``gough``.
    ``GOUGH_CLUSTER_ID``     Flag distinct_id. Default ``gough``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# --- Tier policy ----------------------------------------------------------

#: Nodes an unlicensed deployment may activate, any mix of physical/virtual/cloud.
FREE_TIER_NODE_LIMIT = 3

#: PenguinTech-controlled domains where license enforcement is bypassed
#: entirely. Domain-based bypass is the ONLY sanctioned mechanism -- never an
#: env var, CLI flag, or config toggle (critical-rules.md).
BYPASS_DOMAINS: tuple[str, ...] = (
    ".penguincloud.io",
    ".penguintech.cloud",
    ".localhost.local",
)

#: Flag governing the multi-cloud provider surface (``/api/v1/clouds/*``).
FLAG_MULTI_CLOUD = "gough.multi-cloud"

#: Tag gough stamps on every machine it provisions, so that a later inventory
#: sync can tell gough-created machines apart from an operator's pre-existing
#: fleet. Carried provider-side (AWS/GCP/Azure/Vultr all support tags/labels),
#: so it survives ``_sync_machines_to_db`` overwriting the local row.
MANAGED_TAG_KEY = "gough-managed"
MANAGED_TAG_VALUE = "true"

#: ``nodes.state`` values that represent an activated node. Everything else is
#: inventory (``new``/``probed``/``planned``) or gone (``decommissioned``/
#: ``rejected``) and is free.
ACTIVE_NODE_STATES = frozenset(
    {"deploying", "configuring", "ready", "upgrading", "quarantined", "draining"}
)

#: ``cloud_machines.status`` values that do NOT occupy an allowance slot. A
#: stopped VM still exists and still bills, so only fully-gone machines are
#: exempt.
INACTIVE_MACHINE_STATUSES = frozenset({"terminated", "failed", "broken"})

_CACHE_TTL_SECONDS = 300.0
_ENTITLEMENT_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _CacheEntry:
    """A cached lookup plus the monotonic timestamp it was fetched at."""

    value: Any
    fetched_at: float


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()

_posthog_client: Any = None
_posthog_lock = threading.Lock()
_posthog_warned = False


def _env(name: str, default: str = "") -> str:
    """Read a config value from the environment, trimmed."""
    return (os.getenv(name) or default).strip()


def _cache_get(key: str, *, allow_stale: bool = False) -> Any:
    """Return a cached value, or ``None`` if absent (or expired and not stale-ok)."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        if allow_stale or (time.monotonic() - entry.fetched_at) < _CACHE_TTL_SECONDS:
            return entry.value
        return None


def _cache_put(key: str, value: Any) -> None:
    """Store a value in the TTL cache."""
    with _cache_lock:
        _cache[key] = _CacheEntry(value=value, fetched_at=time.monotonic())


def reset_cache() -> None:
    """Drop all cached flag/entitlement state. Test seam."""
    global _posthog_client, _posthog_warned
    with _cache_lock:
        _cache.clear()
    with _posthog_lock:
        _posthog_client = None
        _posthog_warned = False


# --- Domain bypass --------------------------------------------------------


def requires_license(request_host: str) -> bool:
    """Return True if this request's host must pass license enforcement.

    PenguinTech-controlled domains are exempt. Host may carry a port
    (``gough.penguintech.cloud:8080``); it is stripped before matching.
    """
    host = (request_host or "").split(":", 1)[0].strip().lower().rstrip(".")
    if not host:
        # No Host header to reason about -- enforce rather than hand out a
        # free bypass to whoever omits it.
        return True
    for domain in BYPASS_DOMAINS:
        if host == domain.lstrip(".") or host.endswith(domain):
            return False
    return True


# --- Feature flags --------------------------------------------------------


def _get_posthog() -> Any:
    """Lazily build the PostHog client, or return None when unconfigured."""
    global _posthog_client, _posthog_warned

    with _posthog_lock:
        if _posthog_client is not None:
            return _posthog_client

        api_key = _env("POSTHOG_KEY")
        if not api_key:
            if not _posthog_warned:
                log.warning(
                    "POSTHOG_KEY unset -- feature flags evaluate to OFF. Flag-gated "
                    "surfaces (e.g. %s) will be unavailable until it is configured.",
                    FLAG_MULTI_CLOUD,
                )
                _posthog_warned = True
            return None

        try:
            from posthog import Posthog

            _posthog_client = Posthog(
                project_api_key=api_key,
                host=_env("POSTHOG_HOST", "https://license.penguintech.io"),
            )
        except Exception as exc:  # noqa: BLE001 -- never let flag setup break a request
            log.warning("PostHog client init failed (flags default OFF): %s", exc)
            return None

        return _posthog_client


def _feature_enabled_sync(flag_key: str, distinct_id: str) -> bool:
    """Blocking flag evaluation. Call via :func:`feature_enabled`."""
    client = _get_posthog()
    if client is None:
        return False

    cache_key = f"flag:{flag_key}:{distinct_id}"
    try:
        # PostHog returns Optional[bool]: None means "no such flag", which is
        # a never-seen flag and must read as OFF, not as truthy-unknown.
        result = bool(client.feature_enabled(flag_key, distinct_id))
    except Exception as exc:  # noqa: BLE001
        stale = _cache_get(cache_key, allow_stale=True)
        if stale is not None:
            log.warning(
                "Flag %s lookup failed (%s); using last-known value %s",
                flag_key,
                exc,
                stale,
            )
            return bool(stale)
        log.warning("Flag %s lookup failed (%s); defaulting OFF", flag_key, exc)
        return False

    _cache_put(cache_key, result)
    return result


async def feature_enabled(flag_key: str, distinct_id: str = "") -> bool:
    """Evaluate a PostHog feature flag without blocking the event loop.

    Defaults OFF on every failure path: unconfigured client, unknown flag, or
    an unreachable server with nothing cached.
    """
    ident = distinct_id or _env("GOUGH_CLUSTER_ID", "gough")
    cached = _cache_get(f"flag:{flag_key}:{ident}")
    if cached is not None:
        return bool(cached)
    return await asyncio.to_thread(_feature_enabled_sync, flag_key, ident)


# --- Node entitlement -----------------------------------------------------


def _entitlement_sync(license_key: str) -> int | float:
    """Blocking entitlement lookup. Call via :func:`node_allowance`."""
    cache_key = f"entitlement:{license_key[-6:]}"
    server = _env("LICENSE_SERVER_URL", "https://license.penguintech.io")

    try:
        import requests

        # Contract per docs/licensing/license-server-integration.md: the key
        # travels in the Authorization header, never in the body or a query
        # string, so it stays out of access logs and proxy traces.
        response = requests.post(
            f"{server}/api/v2/validate",
            headers={"Authorization": f"Bearer {license_key}"},
            json={"product": _env("PRODUCT_NAME", "gough")},
            timeout=_ENTITLEMENT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        stale = _cache_get(cache_key, allow_stale=True)
        if stale is not None:
            log.warning(
                "License entitlement refresh failed (%s); using last-known "
                "allowance %s",
                exc,
                stale,
            )
            return stale  # type: ignore[return-value]
        log.warning(
            "License entitlement lookup failed (%s) and nothing cached; "
            "falling back to free-tier limit of %d nodes",
            exc,
            FREE_TIER_NODE_LIMIT,
        )
        return FREE_TIER_NODE_LIMIT

    allowance = _parse_allowance(payload)
    _cache_put(cache_key, allowance)
    return allowance


def _parse_allowance(payload: dict[str, Any]) -> int | float:
    """Derive the node allowance from a license-server validate response.

    Shape per ``docs/licensing/license-server-integration.md``::

        {
          "valid": true,
          "tier": "professional",          # community | professional | enterprise
          "limits": {"max_servers": 25}    # -1 = unlimited
        }

    Both paid tiers are metered per node, so ``limits.max_servers`` is
    authoritative. ``community`` is the license server's name for the free
    tier. An invalid licence, an unrecognised tier, or a missing/unparseable
    ``max_servers`` on a paid tier all fall back to the free-tier limit rather
    than handing out an unbounded allowance.
    """
    if not isinstance(payload, dict) or not payload.get("valid", True):
        return FREE_TIER_NODE_LIMIT

    tier = str(payload.get("tier", "community")).strip().lower()
    if tier not in ("professional", "enterprise"):
        return FREE_TIER_NODE_LIMIT

    limits = payload.get("limits")
    raw = limits.get("max_servers") if isinstance(limits, dict) else None
    if raw is None:
        log.warning(
            "License tier %s carries no limits.max_servers; falling back to "
            "free-tier limit of %d nodes",
            tier,
            FREE_TIER_NODE_LIMIT,
        )
        return FREE_TIER_NODE_LIMIT

    if isinstance(raw, str) and raw.strip().lower() in ("unlimited", "inf", "-1"):
        return math.inf
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        log.warning(
            "License limits.max_servers %r unparseable; using free-tier limit", raw
        )
        return FREE_TIER_NODE_LIMIT
    if parsed < 0:  # -1 = unlimited per the license schema
        return math.inf
    return parsed


async def node_allowance(request_host: str = "") -> int | float:
    """Return how many nodes this deployment may have active.

    ``math.inf`` for bypass domains and unlimited licences;
    :data:`FREE_TIER_NODE_LIMIT` when unlicensed or when entitlement cannot be
    resolved and nothing is cached.
    """
    if not requires_license(request_host):
        return math.inf

    license_key = _env("LICENSE_KEY")
    if not license_key:
        return FREE_TIER_NODE_LIMIT

    cache_key = f"entitlement:{license_key[-6:]}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    return await asyncio.to_thread(_entitlement_sync, license_key)


# --- Node accounting ------------------------------------------------------


def _machine_is_gough_managed(row: Any) -> bool:
    """Return True if this ``cloud_machines`` row was provisioned by gough.

    Identified by the :data:`MANAGED_TAG_KEY` tag gough stamps at create time.
    Tags round-trip through the provider, so this survives an inventory sync
    overwriting the local row. Machines merely discovered in an operator's
    cloud account carry no such tag and are therefore free inventory.
    """
    raw = getattr(row, "tags", None)
    if raw is None and isinstance(row, dict):
        raw = row.get("tags")
    if not raw:
        return False

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            # Not JSON -- fall back to a substring probe so a plain
            # comma-joined tag string still matches.
            return MANAGED_TAG_KEY in raw

    if isinstance(raw, dict):
        return str(raw.get(MANAGED_TAG_KEY, "")).lower() == MANAGED_TAG_VALUE
    if isinstance(raw, (list, tuple, set)):
        return any(MANAGED_TAG_KEY in str(item) for item in raw)
    return False


def count_active_nodes(db: Any) -> int:
    """Count nodes occupying an allowance slot, across both registries.

    Blocking (penguin-dal); callers must wrap this in ``run_db()``.

    Counts ``nodes`` rows in an :data:`ACTIVE_NODE_STATES` state, plus
    ``cloud_machines`` rows that gough provisioned and that are not
    :data:`INACTIVE_MACHINE_STATUSES`. The two registries do not mirror each
    other, so neither alone is a complete picture and there is nothing to
    de-duplicate between them.

    Returns 0 rather than raising if a table is absent -- a counting failure
    must not wedge deployment.
    """
    total = 0

    try:
        if hasattr(db, "nodes"):
            total += db(db.nodes.state.belongs(tuple(ACTIVE_NODE_STATES))).count()
    except Exception as exc:  # noqa: BLE001
        log.warning("Active bare-metal node count failed (counted as 0): %s", exc)

    try:
        if hasattr(db, "cloud_machines"):
            rows = db(
                ~db.cloud_machines.status.belongs(tuple(INACTIVE_MACHINE_STATUSES))
            ).select()
            total += sum(1 for row in rows if _machine_is_gough_managed(row))
    except Exception as exc:  # noqa: BLE001
        log.warning("Active cloud machine count failed (counted as 0): %s", exc)

    return total


def stamp_managed_tag(tags: dict[str, str] | None) -> dict[str, str]:
    """Return ``tags`` with gough's managed marker added.

    Applied to every :class:`~app.clouds.base.MachineSpec` gough provisions so
    the machine stays attributable to gough across inventory syncs.
    """
    stamped = dict(tags or {})
    stamped[MANAGED_TAG_KEY] = MANAGED_TAG_VALUE
    return stamped
