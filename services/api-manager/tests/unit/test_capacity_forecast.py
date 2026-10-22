"""Tests for capacity prediction with linear regression."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.workers.capacity_predictor import (
    CapacityPredictor,
    ClusterUsageSnapshot,
    NodeUsageSnapshot,
)


@pytest.fixture
def prometheus_mock():
    """Mock Prometheus client."""
    client = AsyncMock()
    client.snapshot_node_usage.return_value = [
        NodeUsageSnapshot(node_id=1, cpu_used_pct=30.0, ram_used_pct=50.0, disk_used_pct=60.0),
        NodeUsageSnapshot(node_id=2, cpu_used_pct=25.0, ram_used_pct=45.0, disk_used_pct=55.0),
    ]
    client.snapshot_cluster_usage.return_value = ClusterUsageSnapshot(
        cpu_used_pct=27.5, ram_used_pct=47.5, disk_used_pct=57.5
    )
    return client


@pytest.fixture
def waddleai_mock():
    """Mock WaddleAI client."""
    client = AsyncMock()
    return client


@pytest.fixture
def predictor(prometheus_mock, waddleai_mock):
    """Create a CapacityPredictor instance."""
    return CapacityPredictor(
        db_session=MagicMock(),
        prometheus_client=prometheus_mock,
        waddleai_client=waddleai_mock,
        redis_client=None,
    )


@pytest.mark.asyncio
async def test_synthesize_forecast_generates_low_confidence(predictor, prometheus_mock):
    """Test that fallback forecast generates low confidence without history."""
    result = await predictor._synthesize_forecast(horizon_days=7, node_ids=None)

    assert result.horizon_days == 7
    assert result.confidence == "low"
    assert len(result.per_node) == 2
    assert result.per_node[0].node_id == 1
    assert result.per_node[0].confidence == "low"


@pytest.mark.asyncio
async def test_forecast_metric_fallback(predictor):
    """Test _forecast_metric returns current value with low confidence."""
    predicted, confidence = await predictor._forecast_metric(
        node_id=1, metric="cpu_used_pct", current_value=30.0, horizon_days=7
    )

    assert predicted == 30.0
    assert confidence == "low"


@pytest.mark.asyncio
async def test_compute_forecast_cache_hit(predictor):
    """Test that cache hit returns cached value."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = '{"horizon_days": 7, "confidence": "high", "per_node": [], "cluster": {"cpu_used_pct": 27.5, "cpu_predicted_pct": 27.5, "ram_used_pct": 47.5, "ram_predicted_pct": 47.5, "disk_used_pct": 57.5, "disk_predicted_pct": 57.5}, "generated_at": "2025-01-01T00:00:00Z"}'
    predictor._redis = mock_redis

    result, waddleai_result = await predictor.compute_forecast(horizon_days=7)

    assert result.horizon_days == 7
    assert waddleai_result == "ok"
    mock_redis.get.assert_called_once()


@pytest.mark.asyncio
async def test_compute_risks_returns_empty_on_unavailable(predictor, waddleai_mock):
    """Test that risks return empty on WaddleAI unavailable."""
    # Mock an unavailable response (not a list of risks)
    unavailable_resp = MagicMock()
    unavailable_resp.__class__.__name__ = "WaddleAIUnavailableResponse"
    waddleai_mock.risks.return_value = unavailable_resp

    risks, waddleai_result, confidence = await predictor.compute_risks()

    assert risks == []
    assert confidence == "low"


@pytest.mark.asyncio
async def test_confidence_aggregation_returns_minimum(predictor):
    """Test that confidence aggregation returns the minimum confidence across metrics."""
    # Mock _forecast_metric to return different confidences for each metric
    async def mock_forecast_metric(node_id, metric, current_value, horizon_days):
        if metric == "cpu_used_pct":
            return current_value, "high"
        elif metric == "ram_used_pct":
            return current_value, "medium"
        elif metric == "disk_used_pct":
            return current_value, "low"
        return current_value, "low"

    predictor._forecast_metric = mock_forecast_metric

    result = await predictor._synthesize_forecast(horizon_days=7, node_ids=None)

    # Should return the minimum confidence level (low)
    assert result.confidence == "low"
    assert result.per_node[0].confidence == "low"
