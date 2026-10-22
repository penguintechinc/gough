"""Extended LXD client operations for cluster bootstrap and lifecycle.

Extends ``app.clouds.lxd.LXDCloud`` with cluster-administration primitives that
are not part of the unified ``BaseCloud`` interface:

- Mint cluster join tokens (Phase 0 → bootstrap)
- Join a node to an existing cluster (Phase 1 → 2 handoff)
- Live-migrate instances between cluster members
- Set / rotate the LXD ``core.trust_password`` (stored in Vault)
- Inspect cluster quorum status

All functions are synchronous (penguin-dal mandate per backend-database.md).
Network calls go through the LXD HTTPS API using ``requests`` with strict
TLS cert pinning against the cluster's trust CA bundle stored in Vault at
``secret/gough/<cluster-id>/lxd/server-cert``.

Errors:
    LXDError: base for all errors raised by this module.
    LXDConfigError: invalid configuration / Vault material missing.
    LXDClusterError: cluster join / token mint failure.
    LXDMigrationError: live-migration failure.
    LXDOperationTimeout: LXD async operation did not complete in time.

No plaintext passwords or trust certs are ever logged. Trust passwords are
masked as ``<first4>...<last4>`` (only when length permits); shorter values
collapse to ``****``.
"""

from __future__ import annotations

import logging
import os
import secrets
import string
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from pydantic import BaseModel, Field

from app.clients.vault import VaultClient, VaultError

logger = logging.getLogger(__name__)


# -- Errors -------------------------------------------------------------------


class LXDError(Exception):
    """Base exception for LXD extra client operations."""


class LXDConfigError(LXDError):
    """Configuration or required Vault material is missing/invalid."""


class LXDClusterError(LXDError):
    """Cluster join, token mint, or status retrieval failed."""


class LXDMigrationError(LXDError):
    """Live migration failed."""


class LXDOperationTimeout(LXDError):
    """LXD async operation did not reach a terminal state in time."""


# -- Models -------------------------------------------------------------------


class JoinToken(BaseModel):
    """LXD cluster join token."""

    token: str = Field(..., description="Base64-encoded join token blob")
    fingerprint: str = Field(..., description="Cluster certificate fingerprint")
    expires_at: datetime = Field(
        ..., description="UTC timestamp at which the token expires"
    )


class ClusterMember(BaseModel):
    """LXD cluster member after join."""

    name: str
    address: str
    role: str = Field(..., description="e.g. 'database', 'database-leader', ''")
    status: str = Field(..., description="e.g. 'Online', 'Offline', 'Evacuated'")


class MigrationResult(BaseModel):
    """Result of a live migration operation."""

    operation_id: str
    status: str = Field(..., description="Final LXD operation status")
    duration_seconds: float


class ClusterStatus(BaseModel):
    """Aggregate cluster status for a cluster."""

    quorum_status: str = Field(
        ..., description="'healthy' if all DB members online, else 'degraded'"
    )
    members: list[ClusterMember]


# -- Constants ----------------------------------------------------------------

_VAULT_TRUST_PASSWORD_PATH = "gough/{cluster_id}/lxd/cluster-trust-password"
_VAULT_SERVER_CERT_PATH = "gough/{cluster_id}/lxd/server-cert"

_DEFAULT_OPERATION_POLL_INTERVAL = 1.0
_DEFAULT_OPERATION_TIMEOUT = 600.0  # 10 minutes for live migrations
_TRUST_PASSWORD_LENGTH = 128


# -- Helpers ------------------------------------------------------------------


def _mask_password(password: str) -> str:
    """Return masked representation of a password suitable for logs."""
    if not password or len(password) < 12:
        return "****"
    return f"{password[:4]}...{password[-4:]}"


def _vault_kv(vault: VaultClient | None) -> VaultClient:
    if vault is not None:
        return vault
    try:
        return VaultClient()
    except ValueError as exc:
        raise LXDConfigError(f"Vault client not configured: {exc}") from exc


def _load_pinned_ca_bundle(vault: VaultClient, cluster_id: str) -> str:
    """Fetch the cluster server cert from Vault and write to a temp file.

    Returns the path to the temp file. Caller is responsible for cleanup
    via ``os.unlink`` once the request completes.

    Raises:
        LXDConfigError: if the cert is missing or unreadable.
    """
    try:
        resp = vault.kv_read(_VAULT_SERVER_CERT_PATH.format(cluster_id=cluster_id))
    except VaultError as exc:
        raise LXDConfigError(
            f"Failed to load LXD server cert from Vault: {exc}"
        ) from exc
    cert_pem = resp.data.get("certificate") if resp.data else None
    if not cert_pem:
        raise LXDConfigError(
            f"LXD server cert missing at vault path "
            f"{_VAULT_SERVER_CERT_PATH.format(cluster_id=cluster_id)}"
        )
    fd, path = tempfile.mkstemp(prefix="lxd-ca-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(cert_pem)
    except Exception:
        os.unlink(path)
        raise
    return path


def _client_cert_tuple() -> tuple[str, str] | None:
    cert = os.environ.get("LXD_CLIENT_CERT", "")
    key = os.environ.get("LXD_CLIENT_KEY", "")
    if cert and key:
        if not os.path.isfile(cert) or not os.path.isfile(key):
            raise LXDConfigError(
                "LXD_CLIENT_CERT/LXD_CLIENT_KEY paths do not exist on disk"
            )
        return (cert, key)
    return None


def _api_url() -> str:
    return os.environ.get("LXD_API_URL", "https://localhost:8443").rstrip("/")


def _request(
    method: str,
    path: str,
    *,
    cluster_id: str,
    json_body: dict[str, Any] | None = None,
    vault: VaultClient | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Perform a TLS-pinned HTTPS request against the LXD API.

    Returns the parsed JSON body. Raises ``LXDClusterError`` on transport
    failure or non-2xx response.
    """
    vault_client = _vault_kv(vault)
    ca_bundle = _load_pinned_ca_bundle(vault_client, cluster_id)
    cert = _client_cert_tuple()
    url = f"{_api_url()}{path}"
    try:
        resp = requests.request(
            method,
            url,
            json=json_body,
            cert=cert,
            verify=ca_bundle,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise LXDClusterError(f"LXD request {method} {path} failed: {exc}") from exc
    finally:
        try:
            os.unlink(ca_bundle)
        except OSError:
            pass

    try:
        body: dict[str, Any] = resp.json()
    except ValueError as exc:
        raise LXDClusterError(
            f"LXD {method} {path} returned non-JSON body (status={resp.status_code})"
        ) from exc

    if resp.status_code >= 400:
        err = body.get("error") if isinstance(body, dict) else str(body)
        raise LXDClusterError(
            f"LXD {method} {path} failed: HTTP {resp.status_code}: {err}"
        )
    return body


def _wait_for_operation(
    operation_url: str,
    *,
    cluster_id: str,
    vault: VaultClient | None = None,
    timeout: float = _DEFAULT_OPERATION_TIMEOUT,
    poll_interval: float = _DEFAULT_OPERATION_POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll an LXD async operation until terminal state or timeout.

    ``operation_url`` is typically of the form ``/1.0/operations/<uuid>``.
    """
    deadline = time.monotonic() + timeout
    last_status = "Unknown"
    while time.monotonic() < deadline:
        body = _request(
            "GET",
            f"{operation_url}/wait",
            cluster_id=cluster_id,
            vault=vault,
            timeout=min(poll_interval + 5.0, timeout),
        )
        meta = body.get("metadata", {}) if isinstance(body, dict) else {}
        status = meta.get("status", "Unknown")
        last_status = status
        # 200 = Pending, 101 = Running, 200/Success, 400/Failure, 401/Cancelled
        status_code = meta.get("status_code", 0)
        if status_code >= 200 and status not in ("Pending", "Running"):
            return meta
        time.sleep(poll_interval)
    raise LXDOperationTimeout(
        f"LXD operation {operation_url} did not finish within {timeout}s "
        f"(last status: {last_status})"
    )


# -- Public API ---------------------------------------------------------------


def mint_join_token(
    cluster_id: str,
    ttl_seconds: int = 3600,
    *,
    vault: VaultClient | None = None,
) -> JoinToken:
    """Mint a new cluster join token via ``POST /1.0/cluster/members``.

    Args:
        cluster_id: Logical Gough cluster identifier (used for Vault paths).
        ttl_seconds: Token validity in seconds. Must be positive.
        vault: Optional pre-built ``VaultClient`` (mainly for testing).

    Returns:
        JoinToken with token blob, fingerprint, and expiry.

    Raises:
        LXDConfigError: invalid arguments or missing Vault material.
        LXDClusterError: LXD API call failed.
    """
    if not cluster_id:
        raise LXDConfigError("cluster_id is required")
    if ttl_seconds <= 0:
        raise LXDConfigError("ttl_seconds must be positive")

    body = _request(
        "POST",
        "/1.0/cluster/members",
        cluster_id=cluster_id,
        json_body={"server_name": f"join-{secrets.token_hex(4)}"},
        vault=vault,
    )
    meta = body.get("metadata") if isinstance(body, dict) else None
    if not meta or "addresses" not in meta or "fingerprint" not in meta:
        raise LXDClusterError(f"LXD returned malformed join-token response: {body}")

    secret = meta.get("secret")
    server_name = meta.get("server_name", "")
    fingerprint = meta["fingerprint"]
    addresses = meta["addresses"]
    if not secret:
        raise LXDClusterError("LXD join token response missing 'secret'")

    # LXD's CLI builds the user-facing token by base64-encoding a JSON blob.
    # We mirror that structure so consumers can paste the token directly.
    import base64
    import json

    blob = {
        "server_name": server_name,
        "fingerprint": fingerprint,
        "addresses": addresses,
        "secret": secret,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat(),
    }
    token = base64.b64encode(json.dumps(blob).encode()).decode()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    logger.info(
        "Minted LXD cluster join token cluster_id=%s server_name=%s ttl=%ds",
        cluster_id,
        server_name,
        ttl_seconds,
    )
    return JoinToken(token=token, fingerprint=fingerprint, expires_at=expires_at)


def cluster_join(
    node_address: str,
    join_token: str,
    server_address: str,
    *,
    cluster_id: str | None = None,
    vault: VaultClient | None = None,
    timeout: float = _DEFAULT_OPERATION_TIMEOUT,
) -> ClusterMember:
    """Instruct the local LXD daemon to join an existing cluster.

    Args:
        node_address: Address (``host:port``) the joining node listens on.
        join_token: Base64-encoded join token from ``mint_join_token``.
        server_address: Cluster leader address (``host:port``).
        cluster_id: Logical cluster id (defaults to LXD_CLUSTER_ID env var).
        vault: Optional pre-built ``VaultClient``.
        timeout: Maximum seconds to wait for join operation.

    Returns:
        ClusterMember describing the joined node.

    Raises:
        LXDConfigError: missing required arguments.
        LXDClusterError: join failed at the LXD API.
        LXDOperationTimeout: join did not complete in time.
    """
    if not node_address or not join_token or not server_address:
        raise LXDConfigError(
            "node_address, join_token, and server_address are all required"
        )
    cluster_id = cluster_id or os.environ.get("LXD_CLUSTER_ID", "")
    if not cluster_id:
        raise LXDConfigError(
            "cluster_id is required (pass explicitly or set LXD_CLUSTER_ID)"
        )

    payload = {
        "server_name": node_address.split(":")[0],
        "enabled": True,
        "member_config": [],
        "cluster_address": server_address,
        "cluster_certificate": "",
        "cluster_token": join_token,
        "server_address": node_address,
    }

    body = _request(
        "PUT",
        "/1.0/cluster",
        cluster_id=cluster_id,
        json_body=payload,
        vault=vault,
    )

    operation_url = body.get("operation") if isinstance(body, dict) else None
    if operation_url:
        _wait_for_operation(
            operation_url, cluster_id=cluster_id, vault=vault, timeout=timeout
        )

    # Read back the member record
    member_body = _request(
        "GET",
        f"/1.0/cluster/members/{payload['server_name']}",
        cluster_id=cluster_id,
        vault=vault,
    )
    meta = member_body.get("metadata", {}) if isinstance(member_body, dict) else {}
    member = ClusterMember(
        name=meta.get("server_name", payload["server_name"]),
        address=meta.get("url", node_address),
        role=meta.get("roles", [""])[0] if meta.get("roles") else "",
        status=meta.get("status", "Unknown"),
    )
    logger.info(
        "Joined LXD cluster cluster_id=%s node=%s server=%s status=%s",
        cluster_id,
        member.name,
        server_address,
        member.status,
    )
    return member


def live_migrate(
    instance_name: str,
    target_member: str,
    *,
    allow_inconsistent: bool = False,
    cluster_id: str | None = None,
    vault: VaultClient | None = None,
    timeout: float = _DEFAULT_OPERATION_TIMEOUT,
) -> MigrationResult:
    """Live-migrate an instance to a different cluster member.

    Mirrors ``lxc move --live <instance> --target <member>``: issues a POST
    to ``/1.0/instances/<name>`` with ``migration: true``, ``live: true`` and
    polls the resulting operation.

    Raises:
        LXDConfigError: invalid arguments.
        LXDMigrationError: migration failed.
        LXDOperationTimeout: operation did not complete in time.
    """
    if not instance_name or not target_member:
        raise LXDConfigError("instance_name and target_member are required")
    cluster_id = cluster_id or os.environ.get("LXD_CLUSTER_ID", "")
    if not cluster_id:
        raise LXDConfigError(
            "cluster_id is required (pass explicitly or set LXD_CLUSTER_ID)"
        )

    payload: dict[str, Any] = {
        "name": instance_name,
        "migration": True,
        "live": True,
        "target": target_member,
        "allow_inconsistent": allow_inconsistent,
    }

    started = time.monotonic()
    try:
        body = _request(
            "POST",
            f"/1.0/instances/{instance_name}",
            cluster_id=cluster_id,
            json_body=payload,
            vault=vault,
        )
    except LXDClusterError as exc:
        raise LXDMigrationError(str(exc)) from exc

    operation_url = body.get("operation") if isinstance(body, dict) else None
    if not operation_url:
        raise LXDMigrationError(
            f"LXD migration response missing operation URL: {body}"
        )
    operation_id = operation_url.rsplit("/", 1)[-1]

    try:
        meta = _wait_for_operation(
            operation_url, cluster_id=cluster_id, vault=vault, timeout=timeout
        )
    except LXDOperationTimeout:
        raise
    except LXDClusterError as exc:
        raise LXDMigrationError(str(exc)) from exc

    duration = time.monotonic() - started
    status = meta.get("status", "Unknown")
    if status != "Success":
        raise LXDMigrationError(
            f"Live migration of {instance_name} to {target_member} ended with "
            f"status={status} (operation {operation_id})"
        )

    logger.info(
        "Live-migrated instance=%s target=%s op=%s duration=%.2fs",
        instance_name,
        target_member,
        operation_id,
        duration,
    )
    return MigrationResult(
        operation_id=operation_id,
        status=status,
        duration_seconds=duration,
    )


def set_trust_password(
    password: str,
    *,
    cluster_id: str | None = None,
    vault: VaultClient | None = None,
) -> None:
    """Set ``core.trust_password`` on the LXD daemon.

    Equivalent to ``lxc config set core.trust_password <password>``. Logs only
    a masked form of the password.

    Raises:
        LXDConfigError: invalid password or missing config.
    """
    if not password or len(password) < 16:
        raise LXDConfigError("trust password must be at least 16 characters")
    cluster_id = cluster_id or os.environ.get("LXD_CLUSTER_ID", "")
    if not cluster_id:
        raise LXDConfigError(
            "cluster_id is required (pass explicitly or set LXD_CLUSTER_ID)"
        )

    try:
        _request(
            "PATCH",
            "/1.0",
            cluster_id=cluster_id,
            json_body={"config": {"core.trust_password": password}},
            vault=vault,
        )
    except LXDClusterError as exc:
        raise LXDConfigError(
            f"Failed to set core.trust_password: {exc}"
        ) from exc

    logger.info(
        "Set LXD core.trust_password cluster_id=%s password=%s",
        cluster_id,
        _mask_password(password),
    )


def rotate_trust_password(
    *,
    cluster_id: str | None = None,
    vault: VaultClient | None = None,
) -> str:
    """Generate a fresh trust password, store it in Vault, and apply it.

    Returns the new plaintext password. Callers MUST NOT log this value.

    Raises:
        LXDConfigError: configuration / Vault failure.
    """
    cluster_id = cluster_id or os.environ.get("LXD_CLUSTER_ID", "")
    if not cluster_id:
        raise LXDConfigError(
            "cluster_id is required (pass explicitly or set LXD_CLUSTER_ID)"
        )

    alphabet = string.ascii_letters + string.digits
    new_password = "".join(
        secrets.choice(alphabet) for _ in range(_TRUST_PASSWORD_LENGTH)
    )

    vault_client = _vault_kv(vault)
    try:
        vault_client.kv_write(
            _VAULT_TRUST_PASSWORD_PATH.format(cluster_id=cluster_id),
            {"password": new_password},
        )
    except VaultError as exc:
        raise LXDConfigError(
            f"Failed to store rotated trust password in Vault: {exc}"
        ) from exc

    set_trust_password(new_password, cluster_id=cluster_id, vault=vault_client)
    logger.info(
        "Rotated LXD trust password cluster_id=%s password=%s",
        cluster_id,
        _mask_password(new_password),
    )
    return new_password


def get_cluster_status(
    *,
    cluster_id: str | None = None,
    vault: VaultClient | None = None,
) -> ClusterStatus:
    """Fetch cluster member roster and quorum status.

    Quorum is considered ``healthy`` iff every member with a database role
    reports status ``Online``; otherwise ``degraded``.
    """
    cluster_id = cluster_id or os.environ.get("LXD_CLUSTER_ID", "")
    if not cluster_id:
        raise LXDConfigError(
            "cluster_id is required (pass explicitly or set LXD_CLUSTER_ID)"
        )

    body = _request(
        "GET",
        "/1.0/cluster/members?recursion=2",
        cluster_id=cluster_id,
        vault=vault,
    )
    raw_members = body.get("metadata", []) if isinstance(body, dict) else []
    members: list[ClusterMember] = []
    db_offline = False
    db_present = False
    for entry in raw_members:
        roles = entry.get("roles") or []
        status = entry.get("status", "Unknown")
        is_db = any("database" in r for r in roles)
        if is_db:
            db_present = True
            if status != "Online":
                db_offline = True
        members.append(
            ClusterMember(
                name=entry.get("server_name", ""),
                address=entry.get("url", ""),
                role=roles[0] if roles else "",
                status=status,
            )
        )

    quorum = "healthy" if db_present and not db_offline else "degraded"
    logger.info(
        "Cluster status cluster_id=%s members=%d quorum=%s",
        cluster_id,
        len(members),
        quorum,
    )
    return ClusterStatus(quorum_status=quorum, members=members)


__all__ = [
    "ClusterMember",
    "ClusterStatus",
    "JoinToken",
    "LXDClusterError",
    "LXDConfigError",
    "LXDError",
    "LXDMigrationError",
    "LXDOperationTimeout",
    "MigrationResult",
    "cluster_join",
    "get_cluster_status",
    "live_migrate",
    "mint_join_token",
    "rotate_trust_password",
    "set_trust_password",
]
