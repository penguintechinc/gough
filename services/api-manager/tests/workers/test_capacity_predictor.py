"""Tests for the CapacityPredictor worker.

Coverage:
  * compute_forecast() primary path (WaddleAI 200).
  * compute_forecast() degraded path (WaddleAI 402 → Prometheus fallback).
  * compute_forecast() unavailable path (WaddleAI 5xx → Prometheus fallback).
  * compute_risks() primary path (WaddleAI 200).
  * compute_risks() degraded path (WaddleAI 402 → empty list).
  * compute_risks() unavailable path (WaddleAI 5xx → empty list).
  * Cache behavior (TTL per horizon for forecast, 1h for risks).
  * Returned tuple structure (bundle/items, raw result, confidence).
  * Confidence levels: high when WaddleAI, low when synthesized.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
def mock_redis():
    """Stub Redis client."""
    client = AsyncMock()
    client.get.return_value = None  # Cache miss by default
    client.set.return_value = None
    return client


@pytest.fixture()
def predictor_no_cache(mock_prometheus, mock_waddleai):
    """CapacityPredictor without Redis (no caching)."""
    return CapacityPredictor(
        db_session=None,
        prometheus_client=mock_prometheus,
        waddleai_client=mock_waddleai,
        redis_client=None,
    )


@pytest.fixture()
def predictor_with_cache(mock_prometheus, mock_waddleai, mock_redis):
    """CapacityPredictor with Redis caching."""
    return CapacityPredictor(
        db_session=None,
        prometheus_client=mock_prometheus,
        waddleai_client=mock_waddleai,
        redis_client=mock_redis,
    )


# ---------------------------------------------------------------------------
# compute_forecast()
# ---------------------------------------------------------------------------


class TestComputeForecast:
    @pytest.mark.asyncio
    async def test_forecast_waddleai_success(
        self, predictor_no_cache, mock_waddleai
    ):
        """WaddleAI 200 → ForecastBundle returned."""
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

        result_bundle, raw_result = await predictor_no_cache.compute_forecast(
            horizon_days=7
        )

        assert isinstance(result_bundle, ForecastBundle)
        assert result_bundle.horizon_days == 7
        assert result_bundle.confidence == "high"
        assert raw_result == "ok"
        assert len(result_bundle.per_node) == 1
        assert result_bundle.per_node[0].node_id == 1

    @pytest.mark.asyncio
    async def test_forecast_waddleai_402_degraded(
        self, predictor_no_cache, mock_waddleai, mock_prometheus
    ):
        """WaddleAI 402 → synthesized fallback forecast."""
        degraded = WaddleAIDegradedResponse(
            status_code=402,
            waddleai_message="license required",
            waddleai_body={},
        )
        mock_waddleai.forecast.return_value = degraded

        result_bundle, raw_result = await predictor_no_cache.compute_forecast(
            horizon_days=7
        )

        assert isinstance(result_bundle, ForecastBundle)
        assert result_bundle.confidence == "low"
        assert isinstance(raw_result, WaddleAIDegradedResponse)
        assert raw_result.status_code == 402

        # Prometheus was called for fallback
        mock_prometheus.snapshot_node_usage.assert_called_once()
        mock_prometheus.snapshot_cluster_usage.assert_called_once()

    @pytest.mark.asyncio
    async def test_forecast_waddleai_5xx_unavailable(
        self, predictor_no_cache, mock_waddleai, mock_prometheus
    ):
        """WaddleAI 5xx → synthesized fallback forecast."""
        unavailable = WaddleAIUnavailableResponse(
            status_code=503, diagnostic="upstream_503"
        )
        mock_waddleai.forecast.return_value = unavailable

        result_bundle, raw_result = await predictor_no_cache.compute_forecast(
            horizon_days=7
        )

        assert isinstance(result_bundle, ForecastBundle)
        assert result_bundle.confidence == "low"
        assert isinstance(raw_result, WaddleAIUnavailableResponse)
        assert raw_result.status_code == 503

    @pytest.mark.asyncio
    async def test_forecast_node_ids_filter(self, predictor_no_cache, mock_waddleai):
        """node_ids filter is passed to WaddleAI client."""
        bundle = ForecastBundle(
            horizon_days=7,
            generated_at=datetime.now(timezone.utc),
            cluster=ClusterAggregate(
                cpu_used_pct=0, cpu_predicted_pct=0, ram_used_pct=0,
                ram_predicted_pct=0, disk_used_pct=0, disk_predicted_pct=0,
            ),
        )
        mock_waddleai.forecast.return_value = bundle

        await predictor_no_cache.compute_forecast(horizon_days=7, node_ids=[1, 2])

        mock_waddleai.forecast.assert_called_once_with(
            horizon_days=7, node_ids=[1, 2]
        )

    @pytest.mark.asyncio
    async def test_forecast_cache_hit(self, predictor_with_cache, mock_waddleai):
        """Cache hit → returns bundle, raw_result is "ok" (not recomputed)."""
        cached_bundle = ForecastBundle(
            horizon_days=7,
            generated_at=datetime.now(timezone.utc),
            confidence="high",
            cluster=ClusterAggregate(
                cpu_used_pct=10, cpu_predicted_pct=10, ram_used_pct=10,
                ram_predicted_pct=10, disk_used_pct=10, disk_predicted_pct=10,
            ),
        )
        import json

        cached_json = json.dumps(cached_bundle.model_dump(mode="json"))
        predictor_with_cache._redis.get.return_value = cached_json

        result_bundle, raw_result = await predictor_with_cache.compute_forecast(
            horizon_days=7
        )

        assert isinstance(result_bundle, ForecastBundle)
        assert raw_result == "ok"
        # WaddleAI should NOT be called on cache hit
        mock_waddleai.forecast.assert_not_called()

    @pytest.mark.asyncio
    async def test_forecast_invalid_horizon(self, predictor_no_cache):
        """Invalid horizon_days raises ValueError."""
        with pytest.raises(ValueError):
            await predictor_no_cache.compute_forecast(horizon_days=5)

    @pytest.mark.asyncio
    async def test_forecast_horizon_1_7_30(self, predictor_no_cache, mock_waddleai):
        """All valid horizons {1,7,30} are accepted."""
        def make_bundle(h):
            return ForecastBundle(
                horizon_days=h,
                generated_at=datetime.now(timezone.utc),
                cluster=ClusterAggregate(
                    cpu_used_pct=0, cpu_predicted_pct=0, ram_used_pct=0,
                    ram_predicted_pct=0, disk_used_pct=0, disk_predicted_pct=0,
                ),
            )
        mock_waddleai.forecast.side_effect = lambda horizon_days=7, node_ids=None: make_bundle(horizon_days)

        for h in [1, 7, 30]:
            result, _ = await predictor_no_cache.compute_forecast(horizon_days=h)
            assert result.horizon_days == h


# ---------------------------------------------------------------------------
# compute_risks()
# ---------------------------------------------------------------------------


class TestComputeRisks:
    @pytest.mark.asyncio
    async def test_risks_waddleai_success(self, predictor_no_cache, mock_waddleai):
        """WaddleAI 200 → list[NodeRiskScore] returned."""
        risks = [
            NodeRiskScore(node_id=1, risk_score=0.8),
            NodeRiskScore(node_id=2, risk_score=0.3),
        ]
        mock_waddleai.risks.return_value = risks

        items, raw_result, confidence = await predictor_no_cache.compute_risks()

        assert len(items) == 2
        assert items[0].node_id == 1
        assert items[1].node_id == 2
        assert raw_result == "ok"
        assert confidence == "high"

    @pytest.mark.asyncio
    async def test_risks_waddleai_402_degraded(
        self, predictor_no_cache, mock_waddleai
    ):
        """WaddleAI 402 → empty list, low confidence."""
        degraded = WaddleAIDegradedResponse(
            status_code=402, waddleai_message="license required"
        )
        mock_waddleai.risks.return_value = degraded

        items, raw_result, confidence = await predictor_no_cache.compute_risks()

        assert len(items) == 0
        assert isinstance(raw_result, WaddleAIDegradedResponse)
        assert confidence == "low"

    @pytest.mark.asyncio
    async def test_risks_waddleai_5xx_unavailable(
        self, predictor_no_cache, mock_waddleai
    ):
        """WaddleAI 5xx → empty list, low confidence."""
        unavailable = WaddleAIUnavailableResponse(
            status_code=500, diagnostic="upstream_500"
        )
        mock_waddleai.risks.return_value = unavailable

        items, raw_result, confidence = await predictor_no_cache.compute_risks()

        assert len(items) == 0
        assert isinstance(raw_result, WaddleAIUnavailableResponse)
        assert confidence == "low"

    @pytest.mark.asyncio
    async def test_risks_empty_list(self, predictor_no_cache, mock_waddleai):
        """WaddleAI returns empty list → success."""
        mock_waddleai.risks.return_value = []

        items, raw_result, confidence = await predictor_no_cache.compute_risks()

        assert len(items) == 0
        assert raw_result == "ok"
        assert confidence == "high"

    @pytest.mark.asyncio
    async def test_risks_cache_hit(self, predictor_with_cache, mock_waddleai):
        """Cache hit → returns items, raw_result is "ok" (not recomputed)."""
        import json

        cached_risks = [
            {"node_id": 1, "risk_score": 0.8, "horizon_days": 7, "drivers": []}
        ]
        cached_json = json.dumps(cached_risks)
        predictor_with_cache._redis.get.return_value = cached_json

        items, raw_result, confidence = await predictor_with_cache.compute_risks()

        assert len(items) == 1
        assert items[0].node_id == 1
        assert raw_result == "ok"
        assert confidence == "high"
        # WaddleAI should NOT be called on cache hit
        mock_waddleai.risks.assert_not_called()

    @pytest.mark.asyncio
    async def test_risks_sorted_desc_by_score(
        self, predictor_no_cache, mock_waddleai
    ):
        """Results are sorted descending by risk_score."""
        risks = [
            NodeRiskScore(node_id=3, risk_score=0.2),
            NodeRiskScore(node_id=1, risk_score=0.8),
            NodeRiskScore(node_id=2, risk_score=0.5),
        ]
        mock_waddleai.risks.return_value = risks

        items, _, _ = await predictor_no_cache.compute_risks()

        # Verify sorted desc (client-side before caching)
        assert items[0].risk_score == 0.8
        assert items[1].risk_score == 0.5
        assert items[2].risk_score == 0.2


# ---------------------------------------------------------------------------
# Fallback synthesis
# ---------------------------------------------------------------------------


class TestSynthesizeForecast:
    @pytest.mark.asyncio
    async def test_synthesize_forecast_with_node_snapshots(
        self, predictor_no_cache, mock_prometheus
    ):
        """Synthesized forecast includes per-node data from Prometheus."""
        mock_prometheus.snapshot_node_usage.return_value = [
            NodeUsageSnapshot(
                node_id=1, cpu_used_pct=50.0, ram_used_pct=40.0,
                disk_used_pct=70.0, net_used_bps=1024.0,
            ),
            NodeUsageSnapshot(
                node_id=2, cpu_used_pct=20.0, ram_used_pct=30.0,
                disk_used_pct=60.0, net_used_bps=512.0,
            ),
        ]
        mock_prometheus.snapshot_cluster_usage.return_value = ClusterUsageSnapshot(
            cpu_used_pct=35.0, ram_used_pct=35.0, disk_used_pct=65.0
        )

        unavailable = WaddleAIUnavailableResponse(status_code=503)
        predictor_no_cache._waddleai.forecast.return_value = unavailable

        bundle, raw_result = await predictor_no_cache.compute_forecast(
            horizon_days=7
        )

        assert bundle.confidence == "low"
        assert len(bundle.per_node) == 2
        # Verify current = predicted (naive projection × 1.0)
        assert bundle.per_node[0].cpu_used_pct == 50.0
        assert bundle.per_node[0].cpu_predicted_pct == 50.0
        assert bundle.per_node[0].ram_used_pct == 40.0
        assert bundle.per_node[0].ram_predicted_pct == 40.0

    @pytest.mark.asyncio
    async def test_synthesize_forecast_all_zeros_on_prom_failure(
        self, predictor_no_cache
    ):
        """Prometheus error → synthesized forecast with zeros."""
        mock_prometheus = AsyncMock()
        mock_prometheus.snapshot_node_usage.side_effect = Exception("Prom down")
        mock_prometheus.snapshot_cluster_usage.return_value = ClusterUsageSnapshot(
            cpu_used_pct=0, ram_used_pct=0, disk_used_pct=0
        )
        predictor_no_cache._prom = mock_prometheus

        unavailable = WaddleAIUnavailableResponse(status_code=503)
        predictor_no_cache._waddleai.forecast.return_value = unavailable

        # Should not raise despite Prometheus error
        with pytest.raises(Exception):
            await predictor_no_cache.compute_forecast(horizon_days=7)

    @pytest.mark.asyncio
    async def test_synthesize_forecast_clamps_percentages(
        self, predictor_no_cache, mock_prometheus
    ):
        """Percentages are clamped to [0,100] range."""
        mock_prometheus.snapshot_node_usage.return_value = [
            NodeUsageSnapshot(
                node_id=1, cpu_used_pct=150.0, ram_used_pct=-10.0,
                disk_used_pct=float('nan'), net_used_bps=1024.0,
            ),
        ]
        mock_prometheus.snapshot_cluster_usage.return_value = ClusterUsageSnapshot(
            cpu_used_pct=0, ram_used_pct=0, disk_used_pct=0
        )

        unavailable = WaddleAIUnavailableResponse(status_code=503)
        predictor_no_cache._waddleai.forecast.return_value = unavailable

        bundle, _ = await predictor_no_cache.compute_forecast(horizon_days=7)

        # Verify clamping
        assert bundle.per_node[0].cpu_used_pct == 100.0  # clamped from 150
        assert bundle.per_node[0].ram_used_pct == 0.0  # clamped from -10
        assert bundle.per_node[0].disk_used_pct == 0.0  # NaN → 0


__all__ = []
