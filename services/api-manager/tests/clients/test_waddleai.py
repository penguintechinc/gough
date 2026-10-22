"""Tests for the WaddleAI integration client.

Coverage:
- Pydantic v2 wire models (validation, enum constraints).
- Credential bundle materialization from Vault (success, missing key,
  Vault errors, malformed bundle).
- OIDC machine JWT minting via Vault transit signing.
- Request flow: 200, 402 (license absent), 5xx (retry+exhaust),
  network unreachable, 404 (no retry), 204 (no body).
- forecast(), risks(), ingest_metrics() — return types and degradation
  envelopes.
- Backoff schedule is monkeypatched to zero so tests run fast.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.clients import waddleai as waddleai_module
from app.clients.waddleai import (
    ALLOWED_HORIZON_DAYS,
    ClusterAggregate,
    ForecastBundle,
    MetricSample,
    NodeForecast,
    NodeRiskScore,
    WaddleAIClient,
    WaddleAIConfigError,
    WaddleAIDegradedResponse,
    WaddleAISigningError,
    WaddleAIUnavailableResponse,
)
from app.clients.vault import (
    VaultError,
    VaultKvReadResponse,
    VaultPermissionDenied,
    VaultSealedError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eliminate exponential backoff sleeps in tests."""

    async def _instant(_attempt: int) -> None:
        return None

    monkeypatch.setattr(WaddleAIClient, "_sleep_backoff", staticmethod(_instant))


@pytest.fixture
def vault_stub() -> MagicMock:
    """Stub Vault client that returns a valid bundle and a canned signature."""
    stub = MagicMock()
    stub.kv_read.return_value = VaultKvReadResponse(
        data={
            "sub": "spiffe://penguintech.io/dal2/api-manager",
            "audience": "spiffe://penguintech.io/dal2/waddleai",
            "scopes": ["waddleai.forecast.read", "waddleai.risks.read", "waddleai.ingest"],
            "transit_key_name": "gough-waddleai-machine",
            "issuer": "gough.api-manager.dal2",
        },
        metadata={"version": 3},
    )
    stub.transit_sign.return_value = "vault:v1:c2lnX2Jhc2U2NA"
    return stub


@pytest.fixture
def make_client(vault_stub: MagicMock):
    """Factory: build a WaddleAIClient bound to a programmable httpx.MockTransport."""

    def _build(
        handler,
        *,
        endpoint: str = "https://waddleai.cluster.svc:8443",
        cluster_id: str = "dal2",
    ) -> WaddleAIClient:
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        return WaddleAIClient(
            endpoint=endpoint,
            vault_client=vault_stub,
            cluster_id=cluster_id,
            http_client=http,
        )

    return _build


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TestModels:
    def test_node_forecast_round_trip(self) -> None:
        nf = NodeForecast(
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
        assert nf.confidence == "high"
        out = nf.model_dump()
        assert out["node_id"] == 1

    def test_node_forecast_pct_bounds(self) -> None:
        with pytest.raises(Exception):
            NodeForecast(
                node_id=1,
                cpu_used_pct=200.0,
                cpu_predicted_pct=0.0,
                ram_used_pct=0.0,
                ram_predicted_pct=0.0,
                disk_used_pct=0.0,
                disk_predicted_pct=0.0,
                net_used_bps=0.0,
                net_predicted_bps=0.0,
            )

    def test_forecast_bundle_horizon_validation(self) -> None:
        cluster = ClusterAggregate(
            cpu_used_pct=10,
            cpu_predicted_pct=10,
            ram_used_pct=10,
            ram_predicted_pct=10,
            disk_used_pct=10,
            disk_predicted_pct=10,
        )
        with pytest.raises(Exception):
            ForecastBundle(
                horizon_days=5,
                generated_at=datetime.now(timezone.utc),
                cluster=cluster,
            )
        bundle = ForecastBundle(
            horizon_days=7,
            generated_at=datetime.now(timezone.utc),
            cluster=cluster,
        )
        assert bundle.horizon_days == 7
        assert bundle.confidence == "high"

    def test_node_risk_score_bounds(self) -> None:
        with pytest.raises(Exception):
            NodeRiskScore(node_id=1, risk_score=1.5)
        nr = NodeRiskScore(node_id=1, risk_score=0.5)
        assert 0.0 <= nr.risk_score <= 1.0

    def test_confidence_enum(self) -> None:
        with pytest.raises(Exception):
            NodeRiskScore(node_id=1, risk_score=0.1, confidence="bogus")

    def test_metric_sample(self) -> None:
        s = MetricSample(
            node_id=1,
            metric_name="cpu_pct",
            value=42.0,
            timestamp=datetime.now(timezone.utc),
            labels={"core": "0"},
        )
        d = s.model_dump(mode="json")
        assert d["metric_name"] == "cpu_pct"


# ---------------------------------------------------------------------------
# Construction & credential loading
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_empty_endpoint(self, vault_stub: MagicMock) -> None:
        with pytest.raises(ValueError):
            WaddleAIClient(endpoint="", vault_client=vault_stub, cluster_id="dal2")

    def test_rejects_empty_cluster(self, vault_stub: MagicMock) -> None:
        with pytest.raises(ValueError):
            WaddleAIClient(endpoint="https://x", vault_client=vault_stub, cluster_id="")

    def test_strips_trailing_slash(self, vault_stub: MagicMock) -> None:
        c = WaddleAIClient(
            endpoint="https://x/",
            vault_client=vault_stub,
            cluster_id="dal2",
        )
        assert c._endpoint == "https://x"


class TestCredentials:
    @pytest.mark.asyncio
    async def test_credentials_missing_path(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        vault_stub.kv_read.return_value = VaultKvReadResponse(data={})
        c = make_client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(WaddleAIConfigError):
            await c.forecast()

    @pytest.mark.asyncio
    async def test_credentials_permission_denied(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        vault_stub.kv_read.side_effect = VaultPermissionDenied("forbidden")
        c = make_client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(WaddleAIConfigError):
            await c.forecast()

    @pytest.mark.asyncio
    async def test_credentials_vault_sealed(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        vault_stub.kv_read.side_effect = VaultSealedError("sealed")
        c = make_client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(WaddleAIConfigError):
            await c.forecast()

    @pytest.mark.asyncio
    async def test_credentials_other_vault_error(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        vault_stub.kv_read.side_effect = VaultError("boom")
        c = make_client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(WaddleAIConfigError):
            await c.forecast()

    @pytest.mark.asyncio
    async def test_credentials_malformed_missing_field(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        vault_stub.kv_read.return_value = VaultKvReadResponse(
            data={"sub": "x", "audience": "y"}  # missing transit_key_name
        )
        c = make_client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(WaddleAIConfigError):
            await c.forecast()

    @pytest.mark.asyncio
    async def test_credentials_scopes_must_be_list(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        vault_stub.kv_read.return_value = VaultKvReadResponse(
            data={
                "sub": "x",
                "audience": "y",
                "transit_key_name": "k",
                "scopes": "not-a-list",
            }
        )
        c = make_client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(WaddleAIConfigError):
            await c.forecast()


# ---------------------------------------------------------------------------
# JWT minting
# ---------------------------------------------------------------------------


class TestJwtMinting:
    @pytest.mark.asyncio
    async def test_jwt_attached_to_request(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["cluster"] = request.headers.get("x-gough-cluster-id")
            return httpx.Response(
                200,
                json=_sample_forecast_payload(7),
            )

        c = make_client(handler)
        result = await c.forecast(horizon_days=7)
        assert isinstance(result, ForecastBundle)
        assert captured["auth"].startswith("Bearer ")
        assert captured["cluster"] == "dal2"

        token = captured["auth"][len("Bearer "):]
        parts = token.split(".")
        assert len(parts) == 3
        header_json = json.loads(_b64url_decode(parts[0]))
        payload_json = json.loads(_b64url_decode(parts[1]))
        assert header_json["alg"] == "EdDSA"
        assert header_json["typ"] == "JWT"
        assert payload_json["aud"] == "spiffe://penguintech.io/dal2/waddleai"
        assert payload_json["iss"] == "gough.api-manager.dal2"
        assert "waddleai.forecast.read" in payload_json["scope"]
        assert "exp" in payload_json and "iat" in payload_json

        # Vault was called with the configured transit key name.
        vault_stub.transit_sign.assert_called()
        call = vault_stub.transit_sign.call_args
        assert call.kwargs["key_name"] == "gough-waddleai-machine"

    @pytest.mark.asyncio
    async def test_signing_failure_returns_unavailable(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        vault_stub.transit_sign.side_effect = VaultError("oops")
        c = make_client(lambda r: httpx.Response(200, json={}))
        result = await c.forecast()
        assert isinstance(result, WaddleAIUnavailableResponse)
        assert "jwt_signing_failed" in result.diagnostic

    @pytest.mark.asyncio
    async def test_jwt_strips_vault_prefix(
        self, make_client, vault_stub: MagicMock
    ) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["authorization"]
            return httpx.Response(200, json=_sample_forecast_payload(7))

        c = make_client(handler)
        await c.forecast()
        token = captured["auth"][len("Bearer "):]
        sig = token.split(".")[2]
        # Original Vault output: vault:v1:c2lnX2Jhc2U2NA → strip prefix.
        assert sig == "c2lnX2Jhc2U2NA"


# ---------------------------------------------------------------------------
# forecast()
# ---------------------------------------------------------------------------


class TestForecast:
    @pytest.mark.asyncio
    async def test_happy_path(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/forecast"
            assert request.url.params["horizon_days"] == "7"
            return httpx.Response(200, json=_sample_forecast_payload(7))

        c = make_client(handler)
        result = await c.forecast(horizon_days=7)
        assert isinstance(result, ForecastBundle)
        assert result.horizon_days == 7
        assert len(result.per_node) == 1

    @pytest.mark.asyncio
    async def test_horizon_validation(self, make_client) -> None:
        c = make_client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(ValueError):
            await c.forecast(horizon_days=2)

    @pytest.mark.asyncio
    async def test_node_ids_passed(self, make_client) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["node_ids"] = request.url.params.get("node_ids", "")
            return httpx.Response(200, json=_sample_forecast_payload(7))

        c = make_client(handler)
        await c.forecast(horizon_days=7, node_ids=[3, 1, 2])
        assert captured["node_ids"] == "3,1,2"

    @pytest.mark.asyncio
    async def test_402_license_required(self, make_client) -> None:
        body = {
            "error": {
                "code": "license_required",
                "message": "WaddleAI returned 402: subscription required for forecast.7d",
            }
        }

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(402, json=body)

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIDegradedResponse)
        assert result.status_code == 402
        assert "subscription required" in result.waddleai_message
        assert result.waddleai_body == body
        # 402 is never retried.
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_402_with_top_level_message(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"message": "no license"})

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIDegradedResponse)
        assert result.waddleai_message == "no license"

    @pytest.mark.asyncio
    async def test_402_with_non_json_body(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, text="raw text body")

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIDegradedResponse)
        assert "raw text body" in result.waddleai_message
        assert result.waddleai_body == {"raw": "raw text body"}

    @pytest.mark.asyncio
    async def test_5xx_retried_then_exhausts(self, make_client) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(503, text="Service Unavailable")

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIUnavailableResponse)
        assert result.status_code == 503
        assert attempts["n"] == 4  # 1 initial + 3 retries

    @pytest.mark.asyncio
    async def test_5xx_then_recovers(self, make_client) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(500)
            return httpx.Response(200, json=_sample_forecast_payload(7))

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, ForecastBundle)
        assert attempts["n"] == 3

    @pytest.mark.asyncio
    async def test_network_unreachable(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIUnavailableResponse)
        assert result.status_code == 0
        assert "transport" in result.diagnostic

    @pytest.mark.asyncio
    async def test_timeout(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIUnavailableResponse)
        assert "transport" in result.diagnostic

    @pytest.mark.asyncio
    async def test_404_no_retry(self, make_client) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(404)

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIUnavailableResponse)
        assert result.status_code == 404
        assert attempts["n"] == 1

    @pytest.mark.asyncio
    async def test_invalid_json_body(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})

        c = make_client(handler)
        result = await c.forecast()
        assert isinstance(result, WaddleAIUnavailableResponse)
        assert result.diagnostic == "invalid_json_body"


# ---------------------------------------------------------------------------
# risks()
# ---------------------------------------------------------------------------


class TestRisks:
    @pytest.mark.asyncio
    async def test_happy_sorted(self, make_client) -> None:
        body = [
            {"node_id": 1, "risk_score": 0.2},
            {"node_id": 2, "risk_score": 0.9},
            {"node_id": 3, "risk_score": 0.5},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        c = make_client(handler)
        result = await c.risks()
        assert isinstance(result, list)
        assert [r.node_id for r in result] == [2, 3, 1]

    @pytest.mark.asyncio
    async def test_402(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"message": "no license"})

        c = make_client(handler)
        result = await c.risks()
        assert isinstance(result, WaddleAIDegradedResponse)

    @pytest.mark.asyncio
    async def test_5xx(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502)

        c = make_client(handler)
        result = await c.risks()
        assert isinstance(result, WaddleAIUnavailableResponse)


# ---------------------------------------------------------------------------
# ingest_metrics()
# ---------------------------------------------------------------------------


class TestIngestMetrics:
    @pytest.mark.asyncio
    async def test_empty_batch_noop(self, make_client) -> None:
        c = make_client(lambda r: httpx.Response(500))  # not called
        result = await c.ingest_metrics([])
        assert result is None

    @pytest.mark.asyncio
    async def test_204_success(self, make_client) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(204)

        c = make_client(handler)
        sample = MetricSample(
            node_id=1,
            metric_name="cpu_pct",
            value=10.0,
            timestamp=datetime.now(timezone.utc),
        )
        result = await c.ingest_metrics([sample])
        assert result is None
        assert captured["body"]["samples"][0]["node_id"] == 1

    @pytest.mark.asyncio
    async def test_402_returns_degraded(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"message": "no license"})

        c = make_client(handler)
        sample = MetricSample(
            node_id=1,
            metric_name="cpu",
            value=1.0,
            timestamp=datetime.now(timezone.utc),
        )
        result = await c.ingest_metrics([sample])
        assert isinstance(result, WaddleAIDegradedResponse)

    @pytest.mark.asyncio
    async def test_unavailable_returns_envelope(self, make_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        c = make_client(handler)
        sample = MetricSample(
            node_id=1,
            metric_name="cpu",
            value=1.0,
            timestamp=datetime.now(timezone.utc),
        )
        result = await c.ingest_metrics([sample])
        assert isinstance(result, WaddleAIUnavailableResponse)


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_closes_owned_client(self, vault_stub: MagicMock) -> None:
        c = WaddleAIClient(
            endpoint="https://x",
            vault_client=vault_stub,
            cluster_id="dal2",
        )
        # Lazy-init the client.
        c._ensure_http()
        await c.aclose()
        assert c._http is None

    @pytest.mark.asyncio
    async def test_aclose_no_op_when_not_owned(
        self, vault_stub: MagicMock
    ) -> None:
        http = httpx.AsyncClient()
        c = WaddleAIClient(
            endpoint="https://x",
            vault_client=vault_stub,
            cluster_id="dal2",
            http_client=http,
        )
        await c.aclose()
        # Did not close it; we don't own it.
        assert c._http is http
        await http.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sample_forecast_payload(horizon_days: int) -> dict[str, Any]:
    return {
        "horizon_days": horizon_days,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "confidence": "high",
        "per_node": [
            {
                "node_id": 1,
                "cpu_used_pct": 10.0,
                "cpu_predicted_pct": 12.0,
                "ram_used_pct": 20.0,
                "ram_predicted_pct": 22.0,
                "disk_used_pct": 30.0,
                "disk_predicted_pct": 32.0,
                "net_used_bps": 100.0,
                "net_predicted_bps": 110.0,
                "confidence": "high",
            }
        ],
        "cluster": {
            "cpu_used_pct": 10.0,
            "cpu_predicted_pct": 12.0,
            "ram_used_pct": 20.0,
            "ram_predicted_pct": 22.0,
            "disk_used_pct": 30.0,
            "disk_predicted_pct": 32.0,
        },
    }
