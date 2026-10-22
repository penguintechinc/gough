"""Tests for PrometheusClient."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import Response

from app.clients.prometheus import PrometheusClient
from app.workers.capacity_predictor import ClusterUsageSnapshot, NodeUsageSnapshot


@pytest.fixture
def prometheus_client():
    """Create a PrometheusClient instance."""
    return PrometheusClient(endpoint="http://prometheus:9090", timeout=10.0)


@pytest.mark.asyncio
async def test_snapshot_node_usage_success(prometheus_client):
    """Test successful node usage snapshot."""
    with patch("app.clients.prometheus.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client

        # Mock responses for all metric queries
        mock_client.get.side_effect = [
            # CPU response
            MagicMock(json=lambda: {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"instance": "192.168.1.42:9100"},
                            "value": [1234567890, "25.5"],
                        },
                    ]
                },
            }),
            # RAM response
            MagicMock(json=lambda: {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"instance": "192.168.1.42:9100"},
                            "value": [1234567890, "50.0"],
                        },
                    ]
                },
            }),
            # Disk response
            MagicMock(json=lambda: {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"instance": "192.168.1.42:9100"},
                            "value": [1234567890, "60.0"],
                        },
                    ]
                },
            }),
            # Network response
            MagicMock(json=lambda: {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"instance": "192.168.1.42:9100"},
                            "value": [1234567890, "1000000"],
                        },
                    ]
                },
            }),
        ]

        mock_client_class.return_value = mock_client
        result = await prometheus_client.snapshot_node_usage()
        assert len(result) == 1
        # node_id extracted from IP "192.168.1.42" -> last octet 42
        assert result[0].node_id == 42
        assert result[0].cpu_used_pct == 25.5


@pytest.mark.asyncio
async def test_snapshot_node_usage_failure(prometheus_client):
    """Test graceful degradation on Prometheus failure."""
    with patch("app.clients.prometheus.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client_class.return_value = mock_client

        result = await prometheus_client.snapshot_node_usage()
        assert result == []


@pytest.mark.asyncio
async def test_snapshot_cluster_usage_success(prometheus_client):
    """Test successful cluster usage snapshot."""
    with patch("app.clients.prometheus.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client

        # Mock scalar responses
        mock_client.get.side_effect = [
            MagicMock(json=lambda: {
                "status": "success",
                "data": {"type": "scalar", "value": [1234567890, "35.2"]},
            }),
            MagicMock(json=lambda: {
                "status": "success",
                "data": {"type": "scalar", "value": [1234567890, "45.3"]},
            }),
            MagicMock(json=lambda: {
                "status": "success",
                "data": {"type": "scalar", "value": [1234567890, "55.4"]},
            }),
        ]

        mock_client_class.return_value = mock_client
        result = await prometheus_client.snapshot_cluster_usage()
        assert isinstance(result, ClusterUsageSnapshot)
        assert result.cpu_used_pct == 35.2


def test_parse_query_response_success(prometheus_client):
    """Test parsing valid Prometheus response."""
    resp = MagicMock()
    resp.json.return_value = {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"instance": "node1:9100"},
                    "value": [1234567890, "25.5"],
                },
            ]
        },
    }
    result = prometheus_client._parse_query_response(resp)
    assert "node1:9100" in result
    assert result["node1:9100"]["value"] == 25.5


def test_extract_scalar_success(prometheus_client):
    """Test extracting scalar value from response."""
    resp = MagicMock()
    resp.json.return_value = {
        "status": "success",
        "data": {"type": "scalar", "value": [1234567890, "42.5"]},
    }
    result = prometheus_client._extract_scalar(resp)
    assert result == 42.5


def test_extract_scalar_failure(prometheus_client):
    """Test extracting scalar from failed response."""
    resp = MagicMock()
    resp.json.return_value = {"status": "error"}
    result = prometheus_client._extract_scalar(resp)
    assert result == 0.0


def test_extract_scalar_vector_type(prometheus_client):
    """Test extracting scalar value from vector result type (common for aggregates)."""
    resp = MagicMock()
    resp.json.return_value = {
        "status": "success",
        "data": {
            "type": "vector",
            "result": [
                {
                    "metric": {},
                    "value": [1234567890, "35.7"],
                }
            ],
        },
    }
    result = prometheus_client._extract_scalar(resp)
    assert result == 35.7


def test_extract_scalar_vector_empty(prometheus_client):
    """Test extracting scalar from empty vector result."""
    resp = MagicMock()
    resp.json.return_value = {
        "status": "success",
        "data": {
            "type": "vector",
            "result": [],
        },
    }
    result = prometheus_client._extract_scalar(resp)
    assert result == 0.0
