"""SMART Sweeper Worker.

Periodically polls every ``ready`` node for SMART data via the gRPC control
tunnel, parses ``smartctl --json`` output, persists raw + parsed attributes on
the disk row, and emits ``gough.smart.warning`` events on transition into a
warning state.

Per spec ``SMART & Hardware Inventory``:

- Helper image bundles ``smartmontools`` (``smartctl -H -A -x``).
- Passive only — no short/long self-tests during sweeps.
- Warning conditions:
    * SMART overall-health != PASSED
    * Reallocated_Sector_Ct > 0
    * Temperature_Celsius > 50
    * Pending_Sector_Ct > 0
    * Power_On_Hours > 45000
    * NVMe critical_warning != 0

The sweeper is a *leader-only* worker — only one replica runs at a time, gated
by the ``smart_sweeper`` row in ``leader_leases``. Non-leader replicas idle.

Scheduled by ``supercronic`` at the cluster's configured interval (default 15
minutes); ``run_forever`` is also available for environments running the
sweeper as a long-lived sidecar.

**gRPC tunnel wiring is flag-gated.** The default ``smartctl_runner``
(:func:`_default_smartctl_runner`) dispatches to the real
``Discovery.ExecCommand`` tunnel (:func:`_grpc_smartctl_runner`) only when
the ``gough.smart-sweeper-tunnel`` PostHog flag
(:data:`FLAG_SMART_SWEEPER_TUNNEL`) evaluates ON for this cluster. OFF --
the default for an unconfigured or never-seen flag -- preserves the
pre-tunnel behaviour of raising ``NotImplementedError`` rather than silently
running a half-validated transport against production nodes; this is a
loud, intentional failure, not a stub.

**No Quart app context required.** Regression: gh-22 (DB pool
consolidation). ``run_once()``/``run_forever()`` call
``app.db.database.get_db()`` -- the RLS-wired, app-context-free accessor
(see that module's docstring) -- rather than the app-context-bound
``app.models.get_db()`` this worker used to depend on. This worker runs
OUTSIDE Quart's request pipeline entirely (leader-only,
supercronic-scheduled or long-lived-sidecar, never an HTTP handler), so
there is no request/app context to push in the first place; the previous
``_require_app_context()`` guard existed only because the old accessor
needed one, and has been removed along with the dependency that required it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from prometheus_client import Counter, Gauge

from ..db.database import get_db
from ..db.rls import CROSS_TENANT_SENTINEL, get_current_tenant, set_current_tenant
from . import leader_lease as _leader_lease

# pydal builds SQL from `field == value`; named constants keep that operator form (E712-clean).
_DB_TRUE = True

logger = logging.getLogger(__name__)


@contextmanager
def _cross_tenant_scope() -> Iterator[None]:
    """Push the RLS cross-tenant sentinel for one sweep's DB work.

    ``nodes``/``disks`` are RLS-protected (baseline migration ``rls_tables``
    -- see ``alembic/versions/20260805_1000_baseline_full_schema.py``), and
    the sweeper is a leader-only background worker that never runs inside
    Quart's HTTP request pipeline -- ``app.db.rls``'s tenant ContextVar is
    never set by anything upstream. The sweep is intentionally cluster-wide
    (every ``ready`` node across every tenant, per the module docstring), so
    this pushes the sentinel for the duration of ``_sweep()`` -- mirrors
    ``app.workers.audit_chain_writer._cross_tenant_scope()`` /
    ``app.grpc_server._cross_tenant_scope()`` (duplicated locally rather than
    imported, matching this codebase's existing per-module convention).
    Omitting this would silently fail-closed to zero visible nodes/disks --
    ``get_db()`` here resolves to ``app.db.database``'s RLS-wired,
    app-context-free engine (wired via ``install_rls_events`` inside that
    module's own ``init_db()`` -- see gh-22) -- not raise, just sweep
    nothing, ever.
    """
    previous = get_current_tenant()
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        yield
    finally:
        set_current_tenant(previous)


# =============================================================================
# Prometheus metrics
# =============================================================================


STORAGE_CAPACITY_USED = Gauge(
    "gough_storage_capacity_used_bytes",
    "Bytes of disk capacity claimed by storage backends or planned partitions.",
    labelnames=("tenant_id", "storage_backend", "tier"),
)

SMART_WARNINGS_EMITTED = Counter(
    "gough_smart_warnings_emitted_total",
    "Number of gough.smart.warning events emitted by the sweeper.",
    labelnames=("reason",),
)

DISKS_PROBED = Counter(
    "gough_smart_disks_probed_total",
    "Number of disks probed by the SMART sweeper.",
    labelnames=("status",),
)


# =============================================================================
# Warning detection
# =============================================================================


_HOURS_THRESHOLD = 45000
_TEMP_THRESHOLD = 50


def _attr_int(attrs: list[dict[str, Any]], name: str) -> Optional[int]:
    for a in attrs or []:
        if (a.get("name") or "").lower() == name.lower():
            raw = a.get("raw") or {}
            v = raw.get("value")
            if isinstance(v, (int, float)):
                return int(v)
    return None


def detect_warnings(smart: dict[str, Any]) -> list[str]:
    """Return the list of warning reasons present in a parsed SMART blob.

    The ``smart`` dict mirrors ``smartctl --json``'s schema:

    .. code-block:: json

        {
          "smart_status": {"passed": true},
          "ata_smart_attributes": {"table": [{"name": "Reallocated_Sector_Ct", "raw": {"value": 0}}, ...]},
          "nvme_smart_health_information_log": {"critical_warning": 0, "temperature": 30, "power_on_hours": 1000}
        }
    """
    reasons: list[str] = []

    overall = (smart.get("smart_status") or {}).get("passed")
    if overall is False:
        reasons.append("smart_overall_health_failed")

    attrs = (smart.get("ata_smart_attributes") or {}).get("table") or []
    realloc = _attr_int(attrs, "Reallocated_Sector_Ct")
    pending = _attr_int(attrs, "Current_Pending_Sector") or _attr_int(
        attrs, "Pending_Sector_Ct"
    )
    temp = _attr_int(attrs, "Temperature_Celsius")
    poh = _attr_int(attrs, "Power_On_Hours")

    nvme_log = smart.get("nvme_smart_health_information_log") or {}
    nvme_crit = nvme_log.get("critical_warning")
    nvme_temp = nvme_log.get("temperature")
    nvme_poh = nvme_log.get("power_on_hours")

    if realloc is not None and realloc > 0:
        reasons.append("reallocated_sector_ct_gt_0")
    if pending is not None and pending > 0:
        reasons.append("pending_sector_ct_gt_0")

    effective_temp = nvme_temp if nvme_temp is not None else temp
    if effective_temp is not None and effective_temp > _TEMP_THRESHOLD:
        reasons.append("temperature_celsius_over_50")

    effective_poh = nvme_poh if nvme_poh is not None else poh
    if effective_poh is not None and effective_poh > _HOURS_THRESHOLD:
        reasons.append("power_on_hours_over_45000")

    if isinstance(nvme_crit, int) and nvme_crit != 0:
        reasons.append("nvme_critical_warning_nonzero")

    return reasons


def derive_status(reasons: list[str], smart: dict[str, Any]) -> str:
    """Map a warning-reasons list + raw blob to the disks.smart_status enum."""
    overall = (smart.get("smart_status") or {}).get("passed")
    if overall is False or "smart_overall_health_failed" in reasons:
        return "failed"
    if reasons:
        return "warning"
    if overall is True:
        return "passed"
    return "unknown"


# =============================================================================
# Feature flag
# =============================================================================


#: PostHog flag gating the gRPC-tunnel-backed default smartctl runner.
#: Defined here rather than alongside ``FLAG_MULTI_CLOUD`` in
#: ``app.licensing`` -- that flag has an external consumer
#: (``app.api.clouds``) licensing.py itself enforces, whereas this flag has
#: exactly one consumer: this module's own :func:`_default_smartctl_runner`.
#: OFF (PostHog's default for an unconfigured or never-seen flag) preserves
#: the pre-tunnel behaviour -- see the module docstring.
FLAG_SMART_SWEEPER_TUNNEL = "gough.smart-sweeper-tunnel"


def _tunnel_enabled() -> bool:
    """Evaluate :data:`FLAG_SMART_SWEEPER_TUNNEL` for this cluster.

    Deferred import of ``app.licensing`` mirrors this codebase's existing
    convention for heavy/app-layer imports in worker modules (see
    ``app.api.primary``'s deferred ``discovery_pb2``/``grpc`` imports) --
    constructing a :class:`SMARTSweeper` with an explicit ``smartctl_runner``
    (every existing test does) never needs ``app.licensing`` at all. Calls
    the private sync evaluator directly rather than the async
    ``feature_enabled()`` wrapper: this worker runs fully synchronously,
    outside any event loop, by design (see "No Quart app context required"
    in the module docstring) -- the same reasoning that already applies to
    this module's own app-context-free ``get_db()`` resolution.
    """
    from .. import licensing

    distinct_id = os.getenv("GOUGH_CLUSTER_ID", "gough").strip() or "gough"
    return bool(licensing._feature_enabled_sync(FLAG_SMART_SWEEPER_TUNNEL, distinct_id))


# =============================================================================
# Default smartctl runner — overridable for tests / different transports
# =============================================================================


#: Discovery gRPC service port. Matches every other ``Discovery.ExecCommand``
#: caller in this codebase (see ``app.api.primary``).
_DISCOVERY_GRPC_PORT = 50051

#: Per-``ExecCommand`` RPC timeout, matching every other Discovery gRPC
#: caller in this codebase (``app.api.primary._update_endpoint_on_nodes`` /
#: ``_emit_gracious_arp``).
_SMARTCTL_RPC_TIMEOUT_SECONDS = 30.0

#: smartctl's exit code is a bitmask of health conditions, not a plain
#: success/failure flag (see ``man smartctl`` EXIT STATUS). Only bit 0
#: (command-line syntax error) means "no usable JSON was produced" -- every
#: other bit (device open failed, SMART-failure predicted, error log
#: present, ...) still ships a usable JSON body alongside it, which is
#: exactly the pre-fail/failed signal this sweeper exists to catch.
_SMARTCTL_EXIT_PARSE_ERROR_BIT = 0x01


def _resolve_node_address(node: Any) -> str | None:
    """Resolve the IP address to dial for ``node``'s Discovery gRPC tunnel.

    Mirrors ``app.api.primary._update_endpoint_on_nodes``'s address
    resolution exactly (``ipv4``/``ipv6`` honouring
    ``preferred_addr_family``) -- duplicated locally rather than imported,
    matching this codebase's existing per-module convention for this kind of
    small, call-site-specific logic (see ``_cross_tenant_scope``'s
    docstring above for the same convention applied elsewhere in this file).
    """
    ipv4 = getattr(node, "ipv4", None)
    ipv6 = getattr(node, "ipv6", None)
    if (getattr(node, "preferred_addr_family", None) or "").lower() == "ipv6":
        return ipv6 or ipv4
    return ipv4 or ipv6


def _grpc_smartctl_runner(node: Any) -> dict[str, dict[str, Any]]:
    """Probe every known disk on ``node`` for SMART data over the real
    Discovery gRPC ``ExecCommand`` tunnel.

    Per spec ``SMART & Hardware Inventory``: the node-side helper image
    bundles ``smartmontools``, invoked passively (``smartctl -H -A -x``,
    never ``-t short``/``-t long`` -- no self-tests during a sweep). Reuses
    the exact ``Discovery.ExecCommand`` RPC/channel-building pattern every
    other node operation in this codebase uses
    (``app.api.primary._update_endpoint_on_nodes`` /
    ``_emit_gracious_arp``) rather than inventing a new transport or RPC --
    ``gough.v1.Discovery`` (``proto/v1/gough/discovery.proto``) only defines
    ``Probe``/``EventStream``/``ExecCommand``/``ExtractSecret``; there is no
    dedicated ``SmartProbe`` RPC.

    Returns ``{device_path: parsed_smartctl_json}`` for every device that
    produced usable output; a device whose RPC or JSON parse failed is
    simply omitted (logged + counted as a probe error) rather than failing
    the whole node -- one bad disk shouldn't blind the sweep to its siblings.
    """
    # The buf/protoc-generated discovery_pb2_grpc.py uses a bare
    # ``from gough import discovery_pb2 ...`` (buf's python plugin output
    # dir is app/grpc, proto package path is gough/discovery.proto, so the
    # generated cross-import assumes app/grpc itself -- not app/grpc/gough,
    # not app.grpc.gough -- is on sys.path). app.grpc_runner/app.grpc_server
    # already perform this exact insertion as a side effect of Quart app
    # startup, but this worker must not depend on that having already
    # happened: it runs OUTSIDE the Quart process entirely when
    # supercronic-scheduled (see module docstring), so the insertion is
    # repeated here, guarded against duplicate inserts exactly like those
    # two call sites.
    _grpc_pkg_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "grpc")
    if _grpc_pkg_dir not in sys.path:
        sys.path.insert(0, _grpc_pkg_dir)

    import grpc
    from gough import discovery_pb2, discovery_pb2_grpc

    node_id = getattr(node, "id", "?")
    node_ip = _resolve_node_address(node)
    if not node_ip:
        raise RuntimeError(f"node {node_id} has no ipv4 or ipv6 address")

    db = get_db()
    device_paths = [
        row.device_path
        for row in db(db.disks.node_id == node.id).select(db.disks.device_path)
    ]
    if not device_paths:
        return {}

    # Build gRPC channel to discovery-agent on node (SPIRE mTLS in
    # production) -- same port, same credentials placeholder as every other
    # Discovery.ExecCommand caller in this codebase.
    channel = grpc.secure_channel(
        f"{node_ip}:{_DISCOVERY_GRPC_PORT}",
        grpc.ssl_channel_credentials(),  # SPIRE credentials in real impl
    )
    stub = discovery_pb2_grpc.DiscoveryStub(channel)

    results: dict[str, dict[str, Any]] = {}
    try:
        for device_path in device_paths:
            req = discovery_pb2.ExecCommandRequest(
                command=f"smartctl --json -H -A -x {device_path}",
                target="",  # local execution on the node, matching arping's usage
            )
            try:
                resp = stub.ExecCommand(req, timeout=_SMARTCTL_RPC_TIMEOUT_SECONDS)
            except grpc.RpcError as exc:
                logger.warning(
                    "smart_sweeper: ExecCommand RPC failed for node=%s device=%s: %s",
                    node_id,
                    device_path,
                    exc,
                )
                DISKS_PROBED.labels(status="error").inc()
                continue

            if resp.exit_code & _SMARTCTL_EXIT_PARSE_ERROR_BIT:
                logger.warning(
                    "smart_sweeper: smartctl command-line error for node=%s "
                    "device=%s (exit_code=%s): %s",
                    node_id,
                    device_path,
                    resp.exit_code,
                    resp.output,
                )
                DISKS_PROBED.labels(status="error").inc()
                continue

            try:
                results[device_path] = json.loads(resp.output) if resp.output else {}
            except json.JSONDecodeError as exc:
                logger.warning(
                    "smart_sweeper: smartctl output for node=%s device=%s was not "
                    "valid JSON (exit_code=%s): %s",
                    node_id,
                    device_path,
                    resp.exit_code,
                    exc,
                )
                DISKS_PROBED.labels(status="error").inc()
    finally:
        channel.close()

    return results


def _default_smartctl_runner(node: Any) -> dict[str, Any]:
    """Default smartctl runner: the real gRPC-tunnel probe, flag-gated.

    Dispatches to :func:`_grpc_smartctl_runner` when
    :data:`FLAG_SMART_SWEEPER_TUNNEL` is ON; otherwise raises
    ``NotImplementedError`` (see the module docstring's "gRPC tunnel wiring
    is flag-gated" section for why this is a loud failure, not a silent
    fallback). Tests that need deterministic probe output inject their own
    ``smartctl_runner`` into :class:`SMARTSweeper` and never reach this
    function at all.
    """
    if not _tunnel_enabled():
        raise NotImplementedError(
            "default smartctl runner is not wired to the gRPC control tunnel "
            f"-- flag {FLAG_SMART_SWEEPER_TUNNEL!r} is OFF for this cluster"
        )
    return _grpc_smartctl_runner(node)


# =============================================================================
# Sweeper
# =============================================================================


@dataclass(slots=True)
class SweepResult:
    """Outcome of a single ``run_once`` invocation."""

    nodes_visited: int = 0
    disks_probed: int = 0
    warnings_emitted: int = 0
    transitions: list[tuple[int, str, str]] = field(default_factory=list)
    leader: bool = False


class SMARTSweeper:
    """SMART sweeper — leader-only background worker.

    Parameters
    ----------
    smartctl_runner:
        Callable ``(node_row) -> {device_path: smart_json}`` used to invoke
        ``smartctl`` on the node side. Tests override this.
    publisher:
        Callable ``(subject, payload_dict) -> None`` used to fan out
        ``gough.smart.warning`` events. Defaults to ``app.nats_client`` from a
        Quart context, falling back to a logger.info stub when absent.
    leader_lease_module:
        Indirection seam for tests; defaults to :mod:`.leader_lease`.
    holder_id:
        Optional explicit lease holder id (defaults to a per-process unique id).
    """

    LEASE_NAME = "smart_sweeper"

    def __init__(
        self,
        smartctl_runner: Optional[Callable[[Any], dict[str, dict[str, Any]]]] = None,
        publisher: Optional[Callable[[str, dict[str, Any]], None]] = None,
        leader_lease_module: Any = None,
        holder_id: Optional[str] = None,
    ) -> None:
        self._runner = smartctl_runner or _default_smartctl_runner
        self._publisher = publisher
        self._lease_mod = leader_lease_module or _leader_lease
        self._holder_id = holder_id

    # ---- public entrypoints ------------------------------------------------

    def run_once(self, ttl: int = 900) -> SweepResult:
        """Acquire the lease (best-effort), sweep all ``ready`` nodes, return
        a :class:`SweepResult`.

        If the lease is held by another replica, returns a result with
        ``leader=False`` and zero counters — caller is expected to back off and
        retry on the next supercron tick.
        """
        result = SweepResult()
        db = get_db()

        # leader_lease.acquire()/release() take a penguin-dal DB directly
        # (each of its statements is its own autocommitted db.executesql()
        # call -- see that module's docstring) -- no raw engine.connect()
        # or manual commit() needed.
        handle = self._lease_mod.acquire(
            db, self.LEASE_NAME, ttl_seconds=ttl, holder_id=self._holder_id
        )
        if handle is None:
            logger.debug("smart_sweeper: lease not held — skipping sweep")
            return result

        result.leader = True
        try:
            with _cross_tenant_scope():
                self._sweep(db, result)
        finally:
            try:
                self._lease_mod.release(db, handle)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("smart_sweeper: failed to release lease: %s", exc)
        return result

    def run_forever(self, interval_seconds: int = 900) -> None:
        """Long-lived sweep loop. Sleeps ``interval_seconds`` between ticks
        regardless of leader status — non-leaders idle-poll cheaply.
        """
        logger.info(
            "smart_sweeper: starting run_forever interval=%ss", interval_seconds
        )
        while True:
            try:
                self.run_once(ttl=interval_seconds)
            except Exception as exc:  # pragma: no cover - log-and-continue
                logger.exception("smart_sweeper: sweep tick failed: %s", exc)
            time.sleep(interval_seconds)

    # ---- internal helpers --------------------------------------------------

    def _sweep(self, db: Any, result: SweepResult) -> None:
        nodes = db(db.nodes.state == "ready").select()
        for node in nodes:
            result.nodes_visited += 1
            try:
                probe = self._runner(node) or {}
            except Exception as exc:
                logger.warning(
                    "smart_sweeper: smartctl probe failed for node=%s: %s",
                    getattr(node, "id", "?"),
                    exc,
                )
                DISKS_PROBED.labels(status="error").inc()
                continue

            for device_path, smart_blob in probe.items():
                row = (
                    db(
                        (db.disks.node_id == node.id)
                        & (db.disks.device_path == device_path)
                    )
                    .select()
                    .first()
                )
                if row is None:
                    logger.debug(
                        "smart_sweeper: unknown disk %s on node %s",
                        device_path,
                        node.id,
                    )
                    continue
                self._update_disk(db, row, smart_blob, result)
                result.disks_probed += 1
                DISKS_PROBED.labels(status="ok").inc()

        self._refresh_capacity_metrics(db)

    def _update_disk(
        self, db: Any, row: Any, smart: dict[str, Any], result: SweepResult
    ) -> None:
        prior = row.smart_status or "unknown"
        reasons = detect_warnings(smart)
        new_status = derive_status(reasons, smart)

        db(db.disks.id == row.id).update(
            smart_status=new_status,
            smart_attributes_json=smart,
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        if new_status != prior:
            result.transitions.append((int(row.id), prior, new_status))

        # Emit a warning event on transition into a non-passed state.
        if new_status in {"warning", "failed"} and prior not in {"warning", "failed"}:
            payload = {
                "node_id": int(row.node_id),
                "disk_id": int(row.id),
                "device_path": row.device_path,
                "smart_status": new_status,
                "reasons": reasons,
                "emitted_at": datetime.now(timezone.utc).isoformat(),
            }
            # WARN log alongside the metric/publish below -- the pre-fail/
            # failed transition itself (not just the publish outcome) must
            # be visible in logs without needing a NATS subscriber attached.
            logger.warning(
                "smart_sweeper: disk %s on node %s transitioned %s -> %s (%s)",
                row.device_path,
                int(row.node_id),
                prior,
                new_status,
                ", ".join(reasons) or "no specific reason",
            )
            self._emit_warning(payload)
            result.warnings_emitted += 1
            for reason in reasons or ["unspecified"]:
                SMART_WARNINGS_EMITTED.labels(reason=reason).inc()

    def _emit_warning(self, payload: dict[str, Any]) -> None:
        publisher = self._publisher or _resolve_publisher()
        try:
            publisher("gough.smart.warning", payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("smart_sweeper: warning publish failed: %s", exc)

    def _refresh_capacity_metrics(self, db: Any) -> None:
        """Recompute ``gough_storage_capacity_used_bytes`` from disk_plans."""
        STORAGE_CAPACITY_USED._metrics.clear()  # reset stale label series
        rows = db(db.disks.reserved_for_storage == _DB_TRUE).select(
            db.disks.tenant_id,
            db.disks.storage_backend,
            db.disks.tier,
            db.disks.capacity_bytes,
            db.disks.reserved_for_storage,
        )
        sums: dict[tuple[str, str, str], int] = {}
        for r in rows:
            if not r.reserved_for_storage:
                continue
            key = (
                r.tenant_id or "__default__",
                r.storage_backend or "unassigned",
                r.tier or "bulk",
            )
            sums[key] = sums.get(key, 0) + int(r.capacity_bytes or 0)
        for (tenant_id, backend, tier), used in sums.items():
            STORAGE_CAPACITY_USED.labels(
                tenant_id=tenant_id, storage_backend=backend, tier=tier
            ).set(used)


def _resolve_publisher() -> Callable[[str, dict[str, Any]], None]:
    """Default publisher: NATS if a client is attached to the Quart app,
    otherwise a structured ``logger.info`` so the event is at least visible.
    """
    try:
        from unittest.mock import MagicMock

        from quart import current_app

        nats = getattr(current_app, "nats_client", None)
        if (
            nats is not None
            and not isinstance(nats, MagicMock)
            and hasattr(nats, "publish")
            and callable(nats.publish)
        ):

            def _publish(subject: str, payload: dict[str, Any]) -> None:
                nats.publish(subject, json.dumps(payload).encode("utf-8"))

            return _publish
    except (RuntimeError, ModuleNotFoundError):
        # No app context or quart not available — fall through to logger.
        pass

    def _log_publish(subject: str, payload: dict[str, Any]) -> None:
        logger.info("smart_sweeper: %s %s", subject, json.dumps(payload))

    return _log_publish


__all__ = [
    "DISKS_PROBED",
    "FLAG_SMART_SWEEPER_TUNNEL",
    "SMART_WARNINGS_EMITTED",
    "STORAGE_CAPACITY_USED",
    "SMARTSweeper",
    "SweepResult",
    "derive_status",
    "detect_warnings",
]
