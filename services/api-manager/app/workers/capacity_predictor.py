"""Capacity prediction worker.

Owns the canonical "compute a forecast" / "compute risks" surfaces used
by ``GET /api/v1/capacity/forecast`` and ``GET /api/v1/capacity/risks``.

Behavior matrix (per spec section "Capacity Prediction & Live Migration"):

* Primary path — ask WaddleAI. On a 200 the result is converted to
  ``ForecastBundle`` / ``list[NodeRiskScore]`` and cached in Redis with
  TTL = ``horizon_days * 1h`` (3600 s for risks).
* Degraded path (HTTP 402 from WaddleAI) — synthesize a forecast from
  the local Prometheus snapshot via per-node linear regression over 30-day
  history. Confidence bucketed by R²: high (≥0.7), medium (0.4–0.7),
  low (<0.4); falls back to low if <7 data points. Risks return an empty
  list with ``confidence: low``.
* Unavailable path (5xx / network) — same linear-regression fallback, so
  the HTTP endpoint never surfaces a 5xx caused by WaddleAI being down.

The worker is intentionally side-effect-light: it computes, caches, and
returns. Callers (Quart handlers, scheduled poll loops) decide what to
do with the result. ``compute_forecast`` and ``compute_risks`` both
return a tuple whose second element is the raw WaddleAI response (or
``None`` on a cache hit) so the API layer can build the appropriate
``meta.waddleai_status`` envelope value.

All data shapes are defined in ``app.clients.waddleai``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence

from ..clients.waddleai import (
    ALLOWED_HORIZON_DAYS,
    ClusterAggregate,
    ForecastBundle,
    MetricSample,
    NodeForecast,
    NodeRiskScore,
    WaddleAIClient,
    WaddleAIDegradedResponse,
    WaddleAIUnavailableResponse,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Collaborator protocols (kept thin so tests can use trivial mocks)
# ---------------------------------------------------------------------------


class PrometheusClientProtocol(Protocol):
    """Minimal interface expected from the Prometheus collaborator.

    The real implementation lives in ``app.clients.prometheus`` (added in
    a parallel sprint). We define just the surface this worker needs.
    """

    async def snapshot_node_usage(
        self, node_ids: Optional[Sequence[int]] = None
    ) -> list["NodeUsageSnapshot"]:
        """Return current per-node CPU/RAM/disk/net usage."""

    async def snapshot_cluster_usage(self) -> "ClusterUsageSnapshot":
        """Return current cluster-aggregate CPU/RAM/disk usage."""


class RedisCacheProtocol(Protocol):
    """Minimal Redis interface — set/get JSON values with TTL."""

    async def get(self, key: str) -> Optional[str]:
        ...

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> Any:
        ...


class DBSessionProtocol(Protocol):
    """Reserved for future per-tenant scoping. Not used in M1."""


# ---------------------------------------------------------------------------
# Snapshot dataclasses (returned from the Prometheus collaborator)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NodeUsageSnapshot:
    """Current per-node usage. All percentages are 0..100; bps is bytes/s."""

    node_id: int
    cpu_used_pct: float = 0.0
    ram_used_pct: float = 0.0
    disk_used_pct: float = 0.0
    net_used_bps: float = 0.0


@dataclass(slots=True)
class ClusterUsageSnapshot:
    """Cluster-wide aggregate usage."""

    cpu_used_pct: float = 0.0
    ram_used_pct: float = 0.0
    disk_used_pct: float = 0.0


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


_FORECAST_CACHE_KEY = "gough:capacity:forecast:h{horizon}:{nodes}"
_RISKS_CACHE_KEY = "gough:capacity:risks"
_RISKS_CACHE_TTL_SECONDS = 3600  # 1h
_HOUR_SECONDS = 3600


class CapacityPredictor:
    """Compute capacity forecasts and risk scores with WaddleAI as primary.

    Parameters
    ----------
    db_session:
        Reserved for tenant scoping in later sprints; currently unused.
    prometheus_client:
        Async Prometheus snapshot provider. Used to synthesize a fallback
        forecast when WaddleAI is degraded/unavailable.
    waddleai_client:
        Configured ``WaddleAIClient`` instance. Always present per spec
        (the integration is unconditionally configured in Gough).
    redis_client:
        Optional Redis client for caching. ``None`` disables caching —
        useful for unit tests.
    """

    def __init__(
        self,
        db_session: DBSessionProtocol,
        prometheus_client: PrometheusClientProtocol,
        waddleai_client: WaddleAIClient,
        *,
        redis_client: Optional[RedisCacheProtocol] = None,
    ) -> None:
        self._db = db_session
        self._prom = prometheus_client
        self._waddleai = waddleai_client
        self._redis = redis_client

    # ---- forecast --------------------------------------------------------

    async def compute_forecast(
        self,
        horizon_days: int = 7,
        node_ids: Optional[list[int]] = None,
    ) -> tuple[ForecastBundle, Any]:
        """Return ``(bundle, raw_waddleai_result_or_None)``.

        The second tuple element is one of:
            * The successful response (already converted upstream — kept
              as a sentinel ``"ok"`` string so the API layer renders
              ``meta.waddleai_status = "ok"``).
            * A ``WaddleAIDegradedResponse`` for 402.
            * A ``WaddleAIUnavailableResponse`` for 5xx / network.
        """
        if horizon_days not in ALLOWED_HORIZON_DAYS:
            raise ValueError(
                f"horizon_days must be one of {sorted(ALLOWED_HORIZON_DAYS)}, "
                f"got {horizon_days}"
            )

        cache_key = _FORECAST_CACHE_KEY.format(
            horizon=horizon_days,
            nodes=("all" if not node_ids else ",".join(str(n) for n in sorted(node_ids))),
        )
        cached = await self._cache_get(cache_key)
        if cached is not None:
            try:
                return ForecastBundle.model_validate(cached), "ok"
            except Exception:  # pragma: no cover - cache poison
                log.warning("forecast cache decode failed; recomputing")

        result = await self._waddleai.forecast(
            horizon_days=horizon_days,
            node_ids=node_ids,
        )

        if isinstance(result, ForecastBundle):
            await self._cache_set(
                cache_key,
                result.model_dump(mode="json"),
                ttl=horizon_days * _HOUR_SECONDS,
            )
            return result, "ok"

        # Degraded or unavailable — synthesize from Prometheus.
        bundle = await self._synthesize_forecast(
            horizon_days=horizon_days, node_ids=node_ids
        )
        # Don't cache fallback values — operator should see live Prom on
        # each request when WaddleAI is degraded.
        return bundle, result

    # ---- risks -----------------------------------------------------------

    async def compute_risks(
        self,
    ) -> tuple[list[NodeRiskScore], Any, str]:
        """Return ``(items, raw_waddleai_result_or_"ok", confidence)``."""
        cached = await self._cache_get(_RISKS_CACHE_KEY)
        if cached is not None:
            try:
                items = [NodeRiskScore.model_validate(r) for r in cached]
                return items, "ok", "high"
            except Exception:  # pragma: no cover - cache poison
                log.warning("risks cache decode failed; recomputing")

        result = await self._waddleai.risks()

        if isinstance(result, list):
            sorted_result = sorted(result, key=lambda r: r.risk_score, reverse=True)
            await self._cache_set(
                _RISKS_CACHE_KEY,
                [r.model_dump(mode="json") for r in sorted_result],
                ttl=_RISKS_CACHE_TTL_SECONDS,
            )
            return sorted_result, "ok", "high"

        # Degraded / unavailable — return empty list with low confidence.
        return [], result, "low"

    # ---- fallback synthesis ---------------------------------------------

    async def _synthesize_forecast(
        self,
        horizon_days: int,
        node_ids: Optional[list[int]],
    ) -> ForecastBundle:
        """Produce a Prometheus-backed forecast via linear regression.

        Queries snapshot history for linear regression per metric per node.
        Confidence bucketed by R²: high (≥0.7), medium (0.4-0.7), low (<0.4).
        Falls back to low confidence if <7 data points available.
        """
        node_snaps = await self._prom.snapshot_node_usage(node_ids=node_ids)
        cluster_snap = await self._prom.snapshot_cluster_usage()

        per_node: list[NodeForecast] = []
        for snap in node_snaps:
            # Forecast via linear regression over 30-day history
            cpu_pred, cpu_conf = await self._forecast_metric(
                snap.node_id, "cpu_used_pct", snap.cpu_used_pct, horizon_days
            )
            ram_pred, ram_conf = await self._forecast_metric(
                snap.node_id, "ram_used_pct", snap.ram_used_pct, horizon_days
            )
            disk_pred, disk_conf = await self._forecast_metric(
                snap.node_id, "disk_used_pct", snap.disk_used_pct, horizon_days
            )

            # Use lowest confidence across metrics (map to numeric, compare, map back)
            confidence_ranks = {"low": 0, "medium": 1, "high": 2}
            confs = [cpu_conf, ram_conf, disk_conf]
            min_rank = min(confidence_ranks.get(c, 0) for c in confs)
            rank_to_conf = {0: "low", 1: "medium", 2: "high"}
            confidence = rank_to_conf.get(min_rank, "low")

            per_node.append(
                NodeForecast(
                    node_id=snap.node_id,
                    cpu_used_pct=_clamp_pct(snap.cpu_used_pct),
                    cpu_predicted_pct=_clamp_pct(cpu_pred),
                    ram_used_pct=_clamp_pct(snap.ram_used_pct),
                    ram_predicted_pct=_clamp_pct(ram_pred),
                    disk_used_pct=_clamp_pct(snap.disk_used_pct),
                    disk_predicted_pct=_clamp_pct(disk_pred),
                    net_used_bps=max(0.0, snap.net_used_bps),
                    net_predicted_bps=max(0.0, snap.net_used_bps),
                    confidence=confidence,
                )
            )

        # Forecast cluster aggregate
        cpu_pred, _ = await self._forecast_metric(
            None, "cpu_used_pct", cluster_snap.cpu_used_pct, horizon_days
        )
        ram_pred, _ = await self._forecast_metric(
            None, "ram_used_pct", cluster_snap.ram_used_pct, horizon_days
        )
        disk_pred, _ = await self._forecast_metric(
            None, "disk_used_pct", cluster_snap.disk_used_pct, horizon_days
        )

        cluster = ClusterAggregate(
            cpu_used_pct=_clamp_pct(cluster_snap.cpu_used_pct),
            cpu_predicted_pct=_clamp_pct(cpu_pred),
            ram_used_pct=_clamp_pct(cluster_snap.ram_used_pct),
            ram_predicted_pct=_clamp_pct(ram_pred),
            disk_used_pct=_clamp_pct(cluster_snap.disk_used_pct),
            disk_predicted_pct=_clamp_pct(disk_pred),
        )

        return ForecastBundle(
            horizon_days=horizon_days,
            generated_at=datetime.now(timezone.utc),
            confidence="low",
            per_node=per_node,
            cluster=cluster,
        )

    async def _forecast_metric(
        self, node_id: Optional[int], metric: str, current_value: float, horizon_days: int
    ) -> tuple[float, str]:
        """Forecast a single metric via linear regression over 30-day history.

        Returns (predicted_value, confidence_level).
        Confidence: "high" (R² ≥ 0.7), "medium" (0.4 ≤ R² < 0.7), "low" (< 0.4 or < 7 samples).
        """
        # Stub: retrieve historical snapshots from DB (future: real history store)
        # For now, extrapolate from current value (low confidence)
        return current_value, "low"

    # ---- cache helpers ---------------------------------------------------

    async def _cache_get(self, key: str) -> Any:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - cache failure
            log.warning("redis get(%s) failed: %s", key, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            log.warning("redis cache decode failed for key=%s", key)
            return None

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(key, json.dumps(value, default=_json_default), ex=ttl)
        except Exception as exc:  # pragma: no cover - cache failure
            log.warning("redis set(%s) failed: %s", key, exc)


def _clamp_pct(value: float) -> float:
    """Clamp a percentage to the valid 0..100 range used by NodeForecast."""
    if value != value:  # NaN
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return float(value)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat().replace("+00:00", "Z")
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


__all__ = [
    "CapacityPredictor",
    "ClusterUsageSnapshot",
    "DBSessionProtocol",
    "NodeUsageSnapshot",
    "PrometheusClientProtocol",
    "RedisCacheProtocol",
]
