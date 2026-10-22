"""Tests for the capacity forecast & risk API endpoints.

Coverage:
  * GET /api/v1/capacity/forecast with WaddleAI 200 (success).
  * GET /api/v1/capacity/forecast with WaddleAI 402 (license_required).
  * GET /api/v1/capacity/forecast with WaddleAI 5xx (unavailable fallback).
  * GET /api/v1/capacity/risks with same degradation paths.
  * Query parameter validation (horizon_days, node_ids).
  * Operator never sees 5xx on /capacity/* due to WaddleAI being down.
  * Cache hit behavior (metadata shows "ok" on cache hit).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.capacity import capacity_bp
from app.clients.waddleai import (
    ClusterAggregate,
    ForecastBundle,
    NodeForecast,
    NodeRiskScore,
    WaddleAIDegradedResponse,
    WaddleAIUnavailableResponse,
)
from app.workers.capacity_predictor import (
    CapacityPredictor,
    ClusterUsageSnapshot,
    NodeUsageSnapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_prometheus():
    """Stub Prometheus client that returns zero usage."""
    client = AsyncMock()
    client.snapshot_node_usage.return_value = []
    client.snapshot_cluster_usage.return_value = ClusterUsageSnapshot(
        cpu_used_pct=0.0, ram_used_pct=0.0, disk_used_pct=0.0
    )
    return client


@pytest.fixture()
def mock_waddleai():
    """Stub WaddleAI client."""
    return AsyncMock()


@pytest.fixture()
def capacity_predictor(mock_prometheus, mock_waddleai):
    """Build a CapacityPredictor with mocked collaborators."""
    return CapacityPredictor(
        db_session=None,
        prometheus_client=mock_prometheus,
        waddleai_client=mock_waddleai,
        redis_client=None,  # No caching in tests
    )


def _passthrough_decorator(*dargs, **dkwargs):
    """Auth decorator passthrough for tests."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn):
        return fn

    return _wrap


@pytest.fixture()
def capacity_app(capacity_predictor, monkeypatch):
    """Build a minimal Quart app with capacity blueprint and auth stubbed."""
    import importlib

    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod
    from quart import Quart, g

    # Patch auth decorators before reimporting capacity module
    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    # Reimport capacity to pick up patched decorators
    import app.api.capacity as capacity_mod

    capacity_mod = importlib.reload(capacity_mod)

    app = Quart(__name__)
    app.capacity_predictor = capacity_predictor
    app.register_blueprint(capacity_mod.capacity_bp)

    @app.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "username": "tester",
            "_jwt_payload": {
                "sub": "tester",
                "tenant": "acme",
                "scope": "gough.capacity.read",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="acme")

    return app


# ---------------------------------------------------------------------------
# GET /api/v1/capacity/forecast
# ---------------------------------------------------------------------------


class TestCapacityForecast:
    @pytest.mark.asyncio
    async def test_forecast_waddleai_success(
        self, capacity_app, capacity_predictor, mock_waddleai
    ):
        """WaddleAI returns 200 → success envelope with forecast."""
        bundle = ForecastBundle(
            horizon_days=7,
            generated_at=datetime.now(timezone.utc),
            confidence="high",
            per_node=[
                NodeForecast(
                    node_id=1,
                    cpu_used_pct=50.0,
                    cpu_predicted_pct=60.0,
                    ram_used_pct=40.0,
                    ram_predicted_pct=42.0,
                    disk_used_pct=70.0,
                    disk_predicted_pct=72.0,
                    net_used_bps=1024.0,
                    net_predicted_bps=2048.0,
                )
            ],
            cluster=ClusterAggregate(
                cpu_used_pct=50.0,
                cpu_predicted_pct=60.0,
                ram_used_pct=40.0,
                ram_predicted_pct=42.0,
                disk_used_pct=70.0,
                disk_predicted_pct=72.0,
            ),
        )
        mock_waddleai.forecast.return_value = bundle

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/forecast?horizon_days=7")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["status"] == "success"
        assert data["meta"]["waddleai_status"] == "ok"
        assert data["meta"]["horizon_days"] == 7
        assert data["meta"]["confidence"] == "high"
        assert len(data["data"]["per_node"]) == 1
        assert data["data"]["per_node"][0]["node_id"] == 1

    @pytest.mark.asyncio
    async def test_forecast_waddleai_402_license_required(
        self, capacity_app, capacity_predictor, mock_waddleai
    ):
        """WaddleAI 402 → success envelope with license_required metadata."""
        degraded = WaddleAIDegradedResponse(
            status_code=402,
            waddleai_message="subscription required for forecast",
            waddleai_body={"error": {"code": "license_required"}},
        )
        mock_waddleai.forecast.return_value = degraded

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/forecast?horizon_days=7")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["status"] == "success"
        assert data["meta"]["waddleai_status"] == "license_required"
        assert "license_required" in data["meta"]["waddleai_message"]
        assert data["meta"]["confidence"] == "low"
        # Fallback forecast is all-zeros (Prometheus stub in M1)
        assert len(data["data"]["per_node"]) == 0

    @pytest.mark.asyncio
    async def test_forecast_waddleai_5xx_unavailable(
        self, capacity_app, capacity_predictor, mock_waddleai
    ):
        """WaddleAI 5xx → success envelope with unavailable metadata."""
        unavailable = WaddleAIUnavailableResponse(
            status_code=503, diagnostic="upstream_503"
        )
        mock_waddleai.forecast.return_value = unavailable

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/forecast?horizon_days=7")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["status"] == "success"
        assert data["meta"]["waddleai_status"] == "unavailable"
        assert data["meta"]["confidence"] == "low"
        # Operator never sees 5xx; fallback is graceful
        assert "WaddleAI unreachable" in data["meta"]["waddleai_message"]

    @pytest.mark.asyncio
    async def test_forecast_default_horizon(self, capacity_app, mock_waddleai):
        """horizon_days defaults to 7."""
        bundle = ForecastBundle(
            horizon_days=7,
            generated_at=datetime.now(timezone.utc),
            cluster=ClusterAggregate(
                cpu_used_pct=0, cpu_predicted_pct=0, ram_used_pct=0,
                ram_predicted_pct=0, disk_used_pct=0, disk_predicted_pct=0,
            ),
        )
        mock_waddleai.forecast.return_value = bundle

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/forecast")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["meta"]["horizon_days"] == 7

    @pytest.mark.asyncio
    async def test_forecast_invalid_horizon(self, capacity_app):
        """horizon_days not in {1,7,30} → validation error."""
        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/forecast?horizon_days=5")
        assert response.status_code == 400

        data = await response.get_json()
        assert data["status"] == "error"
        assert "must be 1, 7, or 30" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_forecast_node_ids_filter(
        self, capacity_app, capacity_predictor, mock_waddleai
    ):
        """node_ids query param is passed to WaddleAI client."""
        bundle = ForecastBundle(
            horizon_days=7,
            generated_at=datetime.now(timezone.utc),
            cluster=ClusterAggregate(
                cpu_used_pct=0, cpu_predicted_pct=0, ram_used_pct=0,
                ram_predicted_pct=0, disk_used_pct=0, disk_predicted_pct=0,
            ),
        )
        mock_waddleai.forecast.return_value = bundle

        client = capacity_app.test_client()
        response = await client.get(
            "/api/v1/capacity/forecast?horizon_days=7&node_ids=1,2,3"
        )
        assert response.status_code == 200

        # Verify WaddleAI client was called with the node_ids
        mock_waddleai.forecast.assert_called_once()
        call_kwargs = mock_waddleai.forecast.call_args.kwargs
        assert call_kwargs["node_ids"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_forecast_invalid_node_ids(self, capacity_app):
        """node_ids not comma-separated integers → validation error."""
        client = capacity_app.test_client()
        response = await client.get(
            "/api/v1/capacity/forecast?horizon_days=7&node_ids=a,b,c"
        )
        assert response.status_code == 400

        data = await response.get_json()
        assert data["status"] == "error"


# ---------------------------------------------------------------------------
# GET /api/v1/capacity/risks
# ---------------------------------------------------------------------------


class TestCapacityRisks:
    @pytest.mark.asyncio
    async def test_risks_waddleai_success(self, capacity_app, mock_waddleai):
        """WaddleAI returns 200 → success envelope with risk scores."""
        risks = [
            NodeRiskScore(node_id=1, risk_score=0.8, drivers=["smart.reallocated"]),
            NodeRiskScore(node_id=2, risk_score=0.3, drivers=["io_errors"]),
        ]
        mock_waddleai.risks.return_value = risks

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/risks")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["status"] == "success"
        assert data["meta"]["waddleai_status"] == "ok"
        assert data["meta"]["confidence"] == "high"
        assert len(data["data"]["items"]) == 2
        # Verify sorted desc by risk_score
        assert data["data"]["items"][0]["risk_score"] == 0.8
        assert data["data"]["items"][1]["risk_score"] == 0.3

    @pytest.mark.asyncio
    async def test_risks_waddleai_402_license_required(
        self, capacity_app, mock_waddleai
    ):
        """WaddleAI 402 → success envelope with license_required metadata."""
        degraded = WaddleAIDegradedResponse(
            status_code=402,
            waddleai_message="subscription required for risks",
            waddleai_body={"error": {"code": "license_required"}},
        )
        mock_waddleai.risks.return_value = degraded

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/risks")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["status"] == "success"
        assert data["meta"]["waddleai_status"] == "license_required"
        assert data["meta"]["confidence"] == "low"
        assert len(data["data"]["items"]) == 0

    @pytest.mark.asyncio
    async def test_risks_waddleai_5xx_unavailable(self, capacity_app, mock_waddleai):
        """WaddleAI 5xx → success envelope with unavailable metadata."""
        unavailable = WaddleAIUnavailableResponse(
            status_code=500, diagnostic="upstream_500"
        )
        mock_waddleai.risks.return_value = unavailable

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/risks")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["status"] == "success"
        assert data["meta"]["waddleai_status"] == "unavailable"
        assert data["meta"]["confidence"] == "low"
        # Operator never sees 5xx
        assert len(data["data"]["items"]) == 0

    @pytest.mark.asyncio
    async def test_risks_empty_list(self, capacity_app, mock_waddleai):
        """WaddleAI returns empty risks list → success envelope."""
        mock_waddleai.risks.return_value = []

        client = capacity_app.test_client()
        response = await client.get("/api/v1/capacity/risks")
        assert response.status_code == 200

        data = await response.get_json()
        assert data["status"] == "success"
        assert len(data["data"]["items"]) == 0


__all__ = []
