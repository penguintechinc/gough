"""Test suite for app/clients/prometheus.py.

Coverage targets:
- PrometheusClient initialization
- snapshot_node_usage() stub
- snapshot_cluster_usage() stub
"""

import pytest
from app.clients.prometheus import PrometheusClient


class TestPrometheusClientInitialization:
    """Test PrometheusClient initialization."""

    def test_prometheus_client_valid_endpoint(self):
        """Test PrometheusClient accepts valid endpoint."""
        client = PrometheusClient(endpoint="http://prometheus:9090")
        assert client._endpoint == "http://prometheus:9090"
        assert client._timeout == 10.0

    def test_prometheus_client_strips_trailing_slash(self):
        """Test PrometheusClient strips trailing slash from endpoint."""
        client = PrometheusClient(endpoint="http://prometheus:9090/")
        assert client._endpoint == "http://prometheus:9090"

    def test_prometheus_client_custom_timeout(self):
        """Test PrometheusClient accepts custom timeout."""
        client = PrometheusClient(endpoint="http://prometheus:9090", timeout=30.0)
        assert client._timeout == 30.0

    def test_prometheus_client_empty_endpoint_raises(self):
        """Test PrometheusClient raises ValueError for empty endpoint."""
        with pytest.raises(ValueError, match="endpoint must be a non-empty URL"):
            PrometheusClient(endpoint="")

    def test_prometheus_client_none_endpoint_raises(self):
        """Test PrometheusClient raises ValueError for None endpoint."""
        with pytest.raises(ValueError):
            PrometheusClient(endpoint=None)


class TestPrometheusClientSnapshotMethods:
    """Test snapshot methods (M1 stubs)."""

    @pytest.mark.asyncio
    async def test_snapshot_node_usage_returns_empty_list(self):
        """Test snapshot_node_usage() returns empty list in M1."""
        client = PrometheusClient(endpoint="http://prometheus:9090")
        result = await client.snapshot_node_usage()
        assert result == []
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_snapshot_node_usage_with_filter(self):
        """Test snapshot_node_usage() accepts node_ids filter (stub)."""
        client = PrometheusClient(endpoint="http://prometheus:9090")
        result = await client.snapshot_node_usage(node_ids=[1, 2, 3])
        assert result == []

    @pytest.mark.asyncio
    async def test_snapshot_node_usage_empty_filter(self):
        """Test snapshot_node_usage() accepts empty node_ids (stub)."""
        client = PrometheusClient(endpoint="http://prometheus:9090")
        result = await client.snapshot_node_usage(node_ids=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_snapshot_cluster_usage_returns_zeros(self):
        """Test snapshot_cluster_usage() returns all-zero snapshot."""
        client = PrometheusClient(endpoint="http://prometheus:9090")
        result = await client.snapshot_cluster_usage()

        assert result is not None
        assert hasattr(result, "cpu_used_pct")
        assert hasattr(result, "ram_used_pct")
        assert hasattr(result, "disk_used_pct")

        assert result.cpu_used_pct == 0.0
        assert result.ram_used_pct == 0.0
        assert result.disk_used_pct == 0.0


class TestPrometheusClientMultipleInstances:
    """Test multiple PrometheusClient instances."""

    def test_prometheus_client_multiple_endpoints(self):
        """Test creating multiple clients for different endpoints."""
        client1 = PrometheusClient(endpoint="http://prometheus-1:9090")
        client2 = PrometheusClient(endpoint="http://prometheus-2:9090", timeout=20.0)

        assert client1._endpoint == "http://prometheus-1:9090"
        assert client2._endpoint == "http://prometheus-2:9090"
        assert client1._timeout == 10.0
        assert client2._timeout == 20.0

    @pytest.mark.asyncio
    async def test_prometheus_client_concurrent_snapshots(self):
        """Test concurrent snapshot requests across multiple clients."""
        import asyncio

        client1 = PrometheusClient(endpoint="http://prometheus-1:9090")
        client2 = PrometheusClient(endpoint="http://prometheus-2:9090")

        # Run concurrent snapshots
        results = await asyncio.gather(
            client1.snapshot_cluster_usage(),
            client2.snapshot_cluster_usage(),
        )

        assert len(results) == 2
        assert all(r.cpu_used_pct == 0.0 for r in results)


class TestPrometheusClientEdgeCases:
    """Test edge cases."""

    def test_prometheus_client_various_url_formats(self):
        """Test PrometheusClient accepts various URL formats."""
        valid_urls = [
            "http://localhost:9090",
            "https://prometheus.example.com",
            "http://192.168.1.1:9090",
            "https://prometheus.svc.cluster.local:9090/",
        ]

        for url in valid_urls:
            client = PrometheusClient(endpoint=url)
            assert client._endpoint is not None

    def test_prometheus_client_timeout_values(self):
        """Test PrometheusClient with various timeout values."""
        timeouts = [0.1, 1.0, 10.0, 60.0, 300.0]
        for timeout in timeouts:
            client = PrometheusClient(
                endpoint="http://prometheus:9090",
                timeout=timeout,
            )
            assert client._timeout == timeout

    @pytest.mark.asyncio
    async def test_snapshot_methods_are_async(self):
        """Test snapshot methods are properly async."""
        import inspect

        client = PrometheusClient(endpoint="http://prometheus:9090")
        assert inspect.iscoroutinefunction(client.snapshot_node_usage)
        assert inspect.iscoroutinefunction(client.snapshot_cluster_usage)
