"""Capacity Forecast & Risk API (Sprint 5).

Implements the spec section "API Surface → Capacity":

* ``GET /api/v1/capacity/forecast?horizon_days={1,7,30}`` — forecast for
  the chosen horizon (default 7).
* ``GET /api/v1/capacity/risks`` — outage-precursor risk scores, sorted
  desc by ``risk_score``.

Both endpoints share the WaddleAI degradation behavior described in the
spec: when WaddleAI returns 402 (license absent on WaddleAI's side) the
endpoint still returns ``status: success`` with a Prometheus-backed forecast
via linear regression and the WaddleAI message surfaced verbatim in
``meta.waddleai_status`` / ``meta.waddleai_message``. When WaddleAI is
unreachable or returning 5xx we apply the same shape with
``waddleai_status = "unavailable"``. Operators thus never see a hard
5xx on these endpoints due to WaddleAI being down.

The CapacityPredictor worker is the source of truth for both paths:
the blueprint simply delegates and renders the response envelope.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from quart import Blueprint, current_app, request

from ..clients.waddleai import (
    WaddleAIDegradedResponse,
    WaddleAIUnavailableResponse,
)
from ..middleware import auth_required
from ..security.scope_enforcement import require_scopes
from ._helpers import envelope_error, envelope_success, err_bad_request, err_validation

log = logging.getLogger(__name__)

capacity_bp = Blueprint("capacity", __name__, url_prefix="/api/v1/capacity")


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


_ALLOWED_HORIZONS: frozenset[int] = frozenset({1, 7, 30})


def _parse_horizon(raw: Optional[str]) -> int | tuple:
    """Parse and validate the ``horizon_days`` query parameter.

    Returns the int on success, or a Quart response tuple on validation
    failure (so the caller can ``return`` it directly).
    """
    if raw is None or raw == "":
        return 7
    try:
        value = int(raw)
    except ValueError:
        return err_bad_request(
            "horizon_days must be an integer in {1,7,30}",
        )
    if value not in _ALLOWED_HORIZONS:
        return err_bad_request(
            "horizon_days must be 1, 7, or 30",
        )
    return value


def _get_predictor():
    """Resolve the CapacityPredictor instance from the app context.

    The app factory attaches it as ``app.capacity_predictor``. We look it
    up dynamically so unit tests can monkeypatch it without touching the
    factory.
    """
    predictor = getattr(current_app, "capacity_predictor", None)
    if predictor is None:
        raise RuntimeError(
            "capacity_predictor is not initialized on the Quart app"
        )
    return predictor


def _meta_for_waddleai(
    result: Any,
) -> dict[str, Any]:
    """Build the WaddleAI status fields for the response envelope's meta."""
    if isinstance(result, WaddleAIDegradedResponse):
        return {
            "waddleai_status": "license_required",
            "waddleai_message": f"license_required: {result.waddleai_message}",
        }
    if isinstance(result, WaddleAIUnavailableResponse):
        return {
            "waddleai_status": "unavailable",
            "waddleai_message": (
                f"WaddleAI unreachable (upstream={result.status_code}, "
                f"diag={result.diagnostic})"
            ),
        }
    return {"waddleai_status": "ok"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@capacity_bp.route("/forecast", methods=["GET"])
@auth_required
@require_scopes("gough.capacity.read")
async def get_capacity_forecast():
    """Return a per-node + cluster-aggregate forecast for the horizon.

    Query params:
        horizon_days (int, optional, default 7) — one of {1, 7, 30}.
        node_ids (csv int, optional) — restrict forecast to these nodes.
    """
    parsed = _parse_horizon(request.args.get("horizon_days"))
    if not isinstance(parsed, int):
        return parsed  # validation error tuple
    horizon: int = parsed

    node_ids: Optional[list[int]] = None
    raw_nodes = request.args.get("node_ids")
    if raw_nodes:
        try:
            node_ids = [int(x) for x in raw_nodes.split(",") if x.strip()]
        except ValueError:
            return err_bad_request(
                "node_ids must be a comma-separated list of integers",
            )

    predictor = _get_predictor()
    bundle, raw_result = await predictor.compute_forecast(
        horizon_days=horizon,
        node_ids=node_ids,
    )

    extra_meta = _meta_for_waddleai(raw_result)
    extra_meta["horizon_days"] = horizon
    extra_meta["confidence"] = bundle.confidence

    data = bundle.model_dump(mode="json")
    return envelope_success(data, extra_meta=extra_meta)


@capacity_bp.route("/risks", methods=["GET"])
@auth_required
@require_scopes("gough.capacity.read")
async def get_capacity_risks():
    """Return outage-precursor risk scores sorted desc by ``risk_score``."""
    predictor = _get_predictor()
    items, raw_result, confidence = await predictor.compute_risks()

    extra_meta = _meta_for_waddleai(raw_result)
    extra_meta["confidence"] = confidence

    data = {
        "items": [item.model_dump(mode="json") for item in items],
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    return envelope_success(data, extra_meta=extra_meta)


__all__ = ["capacity_bp"]
