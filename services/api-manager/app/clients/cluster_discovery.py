"""Brownfield cluster discovery boundary for adoption (M2).

``app.api.clusters.adopt_cluster``'s M2 execution ingests an existing LXD or
Kubernetes cluster's node/member inventory into gough's model. gough has no
existing client for an arbitrary *external* (not-yet-managed) LXD or K8s
endpoint -- ``app/clients/lxd_extra.py`` talks to clusters gough already
manages via Vault-pinned credentials, which do not exist yet for a cluster
that has not been adopted. This module is the real boundary that gap needs:
a genuine HTTPS call against the target cluster's own REST API, parsing its
real response shape (LXD's ``/1.0/cluster/members`` or Kubernetes'
``/api/v1/nodes``) -- never a fabricated member list. An unreachable or
non-2xx endpoint raises :class:`ClusterDiscoveryUnavailable`; the caller
treats that as a hard failure and rolls back, it never reports members that
were never actually observed.

``credentials_ref`` is a Vault path per ``security.md`` (secrets are never
inline) -- resolving it to a bearer token requires ``VaultClient``, which
needs ``VAULT_TOKEN`` configured. Best-effort: when Vault is not configured
in this environment, discovery proceeds unauthenticated and lets the target
API's own ``401``/``403`` surface as the failure, rather than silently
skipping discovery.

Tests patch :func:`discover_inventory` wholesale to exercise both the
success and rollback/failure paths without a live cluster.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

#: Discovery call timeout. Short because this gates a user-facing request.
DISCOVERY_TIMEOUT_SECONDS = float(os.environ.get("CLUSTER_DISCOVERY_TIMEOUT_SECONDS", "10"))

_LXD_MEMBERS_PATH = "/1.0/cluster/members"
_K8S_NODES_PATH = "/api/v1/nodes"

_VALID_KINDS = frozenset({"lxd", "k8s"})


class ClusterDiscoveryError(Exception):
    """Base class for brownfield cluster discovery failures."""


class ClusterDiscoveryUnavailable(ClusterDiscoveryError):
    """The target cluster could not be reached/parsed to discover inventory."""


@dataclass(slots=True)
class DiscoveredMember:
    """One node/member observed on the adopted cluster."""

    name: str
    address: str | None = None
    role: str | None = None
    status: str | None = None


def _resolve_bearer_token(credentials_ref: str) -> str | None:
    """Best-effort Vault lookup for ``credentials_ref``.

    Returns ``None`` (proceed unauthenticated) rather than raising when
    Vault is not configured -- an unconfigured Vault is an environment gap,
    not a reason to fabricate discovery results, so the HTTP call below
    still happens and surfaces the target API's own auth failure if a token
    was actually required.
    """
    if not credentials_ref:
        return None
    try:
        from app.clients.vault import VaultClient

        client = VaultClient()
        secret = client.kv_read(credentials_ref)
        token = secret.data.get("token") if hasattr(secret, "data") else None
        return str(token) if token else None
    except Exception as exc:  # noqa: BLE001 - best-effort only, see docstring
        log.warning(
            "credentials_ref=%s could not be resolved via Vault (%s); "
            "proceeding with discovery unauthenticated",
            credentials_ref,
            exc,
        )
        return None


def _parse_lxd_members(payload: dict) -> list[DiscoveredMember]:
    raw = payload.get("metadata") or []
    members: list[DiscoveredMember] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        roles = item.get("roles") or []
        members.append(
            DiscoveredMember(
                name=str(item.get("server_name", "")),
                address=item.get("url"),
                role=",".join(roles) if roles else None,
                status=item.get("status"),
            )
        )
    return members


def _parse_k8s_nodes(payload: dict) -> list[DiscoveredMember]:
    raw = payload.get("items") or []
    members: list[DiscoveredMember] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        addresses = status.get("addresses") or []
        address = next(
            (a.get("address") for a in addresses if isinstance(a, dict) and a.get("address")),
            None,
        )
        conditions = status.get("conditions") or []
        ready = any(
            isinstance(c, dict) and c.get("type") == "Ready" and c.get("status") == "True"
            for c in conditions
        )
        members.append(
            DiscoveredMember(
                name=str(meta.get("name", "")),
                address=address,
                role="worker",
                status="ready" if ready else "not_ready",
            )
        )
    return members


async def discover_inventory(
    *, kind: str, endpoint: str, credentials_ref: str = ""
) -> list[DiscoveredMember]:
    """Discover a brownfield cluster's member inventory for adoption.

    ``kind`` must be ``"lxd"`` or ``"k8s"``. Raises
    :class:`ClusterDiscoveryUnavailable` if ``endpoint`` is empty, the
    target is unreachable, or the response cannot be parsed.
    """
    if kind not in _VALID_KINDS:
        raise ClusterDiscoveryUnavailable(f"unsupported adoption kind={kind!r}")
    if not endpoint:
        raise ClusterDiscoveryUnavailable(
            "endpoint is required to discover an adopted cluster's inventory"
        )

    path = _LXD_MEMBERS_PATH if kind == "lxd" else _K8S_NODES_PATH
    url = endpoint.rstrip("/") + path

    headers: dict[str, str] = {}
    token = _resolve_bearer_token(credentials_ref)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_SECONDS) as http:
            response = await http.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise ClusterDiscoveryUnavailable(
            f"brownfield cluster unreachable at {url!r}: {exc}"
        ) from exc
    except ValueError as exc:  # JSON decode failure
        raise ClusterDiscoveryUnavailable(
            f"brownfield cluster at {url!r} returned a non-JSON response: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ClusterDiscoveryUnavailable(
            f"brownfield cluster at {url!r} returned an unexpected response shape"
        )

    return _parse_lxd_members(payload) if kind == "lxd" else _parse_k8s_nodes(payload)


__all__ = [
    "DISCOVERY_TIMEOUT_SECONDS",
    "ClusterDiscoveryError",
    "ClusterDiscoveryUnavailable",
    "DiscoveredMember",
    "discover_inventory",
]
