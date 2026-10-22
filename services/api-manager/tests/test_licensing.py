"""Tests for app/licensing.py -- feature flags and node-count entitlement.

Covers the three properties that matter for a licensing layer: it fails soft
(never raises into a request), it fails *closed* (an unresolvable lookup lands
on the narrowest allowance, not an unbounded one), and it meters activation
rather than inventory.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from quart import Quart

from app import licensing as L


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Isolate every test from ambient env and cached lookups."""
    for var in (
        "LICENSE_KEY",
        "LICENSE_SERVER_URL",
        "POSTHOG_KEY",
        "POSTHOG_HOST",
        "PRODUCT_NAME",
        "GOUGH_CLUSTER_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    L.reset_cache()
    yield
    L.reset_cache()


class TestRequiresLicense:
    """Domain-based bypass -- the only sanctioned bypass mechanism."""

    @pytest.mark.parametrize(
        "host",
        [
            "gough.penguincloud.io",
            "gough.penguintech.cloud",
            "gough.localhost.local",
            "penguincloud.io",
            "penguintech.cloud",
            "GOUGH.PENGUINCLOUD.IO",
            "gough.penguintech.cloud:8080",
            "gough.penguincloud.io.",
        ],
    )
    def test_bypass_domains_are_exempt(self, host):
        assert L.requires_license(host) is False

    @pytest.mark.parametrize(
        "host",
        [
            "gough.acme.com",
            "localhost",
            "10.0.0.5",
            "gough.penguincloud.io.attacker.com",
            "evil-penguincloud.io",
            "notpenguintech.cloud.evil.net",
        ],
    )
    def test_customer_domains_are_enforced(self, host):
        assert L.requires_license(host) is True

    def test_missing_host_enforces_rather_than_bypasses(self):
        # Fail closed: omitting the Host header must not buy a free bypass.
        assert L.requires_license("") is True
        assert L.requires_license(None) is True  # type: ignore[arg-type]


class TestParseAllowance:
    """Translating a validate response into a node allowance."""

    def test_professional_uses_max_servers(self):
        assert (
            L._parse_allowance(
                {"valid": True, "tier": "professional", "limits": {"max_servers": 25}}
            )
            == 25
        )

    def test_enterprise_is_also_metered(self):
        assert (
            L._parse_allowance(
                {"valid": True, "tier": "enterprise", "limits": {"max_servers": 500}}
            )
            == 500
        )

    @pytest.mark.parametrize("sentinel", ["unlimited", "inf", "-1", -1])
    def test_unlimited_sentinels(self, sentinel):
        """-1 is the license schema's documented 'unlimited'."""
        assert (
            L._parse_allowance(
                {
                    "valid": True,
                    "tier": "enterprise",
                    "limits": {"max_servers": sentinel},
                }
            )
            == math.inf
        )

    @pytest.mark.parametrize(
        "payload",
        [
            # "community" is the license server's name for the free tier.
            {"valid": True, "tier": "community", "limits": {"max_servers": 99}},
            {"valid": False, "tier": "enterprise", "limits": {"max_servers": 99}},
            {"valid": True, "tier": "professional"},
            {"valid": True, "tier": "professional", "limits": {}},
            {"valid": True, "tier": "professional", "limits": None},
            {
                "valid": True,
                "tier": "professional",
                "limits": {"max_servers": "not-a-number"},
            },
            {"valid": True, "tier": "bogus-tier", "limits": {"max_servers": 99}},
            # max_nodes is NOT the contract -- must not be honoured.
            {"valid": True, "tier": "professional", "max_nodes": 99},
            {},
            "not-a-dict",
        ],
    )
    def test_every_unresolvable_shape_falls_back_to_free(self, payload):
        # Fail closed: a malformed or unlicensed response must never widen
        # the allowance beyond the free tier.
        assert L._parse_allowance(payload) == L.FREE_TIER_NODE_LIMIT

    def test_tier_matching_is_case_insensitive(self):
        assert (
            L._parse_allowance(
                {"valid": True, "tier": "Professional", "limits": {"max_servers": 7}}
            )
            == 7
        )


@pytest.mark.asyncio
class TestNodeAllowance:
    """End-to-end allowance resolution."""

    async def test_bypass_domain_is_unlimited(self):
        assert await L.node_allowance("gough.penguincloud.io") == math.inf

    async def test_unlicensed_gets_free_limit(self):
        assert await L.node_allowance("acme.com") == L.FREE_TIER_NODE_LIMIT

    async def test_licensed_reads_entitlement(self, monkeypatch):
        monkeypatch.setenv("LICENSE_KEY", "PENG-AAAA-BBBB-CCCC-DDDD-EEEE")
        resp = MagicMock()
        resp.json.return_value = {
            "valid": True,
            "tier": "professional",
            "limits": {"max_servers": 12},
        }
        resp.raise_for_status.return_value = None
        with patch("requests.post", return_value=resp) as post:
            assert await L.node_allowance("acme.com") == 12
        kwargs = post.call_args.kwargs
        # Key rides in the Authorization header, never the body -- keeps it
        # out of access logs and proxy traces.
        assert kwargs["headers"]["Authorization"] == (
            "Bearer PENG-AAAA-BBBB-CCCC-DDDD-EEEE"
        )
        assert "license_key" not in kwargs["json"]
        assert kwargs["json"]["product"] == "gough"

    async def test_server_unreachable_with_no_cache_falls_back_to_free(
        self, monkeypatch
    ):
        monkeypatch.setenv("LICENSE_KEY", "PENG-AAAA-BBBB-CCCC-DDDD-EEEE")
        with patch("requests.post", side_effect=OSError("connection refused")):
            assert await L.node_allowance("acme.com") == L.FREE_TIER_NODE_LIMIT

    async def test_server_unreachable_reuses_last_known_allowance(self, monkeypatch):
        """A licensing outage must not demote a paying customer to 3 nodes."""
        monkeypatch.setenv("LICENSE_KEY", "PENG-AAAA-BBBB-CCCC-DDDD-EEEE")
        resp = MagicMock()
        resp.json.return_value = {
            "valid": True,
            "tier": "enterprise",
            "limits": {"max_servers": 40},
        }
        resp.raise_for_status.return_value = None
        with patch("requests.post", return_value=resp):
            assert await L.node_allowance("acme.com") == 40

        # Expire the TTL, then fail the refresh: last-known value must survive.
        for entry in L._cache.values():
            entry.fetched_at -= L._CACHE_TTL_SECONDS + 1
        with patch("requests.post", side_effect=OSError("boom")):
            assert await L.node_allowance("acme.com") == 40

    async def test_result_is_cached_across_calls(self, monkeypatch):
        monkeypatch.setenv("LICENSE_KEY", "PENG-AAAA-BBBB-CCCC-DDDD-EEEE")
        resp = MagicMock()
        resp.json.return_value = {
            "valid": True,
            "tier": "professional",
            "limits": {"max_servers": 9},
        }
        resp.raise_for_status.return_value = None
        with patch("requests.post", return_value=resp) as post:
            await L.node_allowance("acme.com")
            await L.node_allowance("acme.com")
        assert post.call_count == 1


@pytest.mark.asyncio
class TestFeatureEnabled:
    """PostHog flag evaluation. Every failure path must read as OFF."""

    async def test_unconfigured_client_is_off(self):
        assert await L.feature_enabled(L.FLAG_MULTI_CLOUD) is False

    async def test_flag_on(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_KEY", "phc_test")
        client = MagicMock()
        client.feature_enabled.return_value = True
        monkeypatch.setattr(L, "_get_posthog", lambda: client)
        assert await L.feature_enabled(L.FLAG_MULTI_CLOUD, "cluster-1") is True

    async def test_unknown_flag_returns_none_and_reads_off(self, monkeypatch):
        """PostHog returns Optional[bool]; None means no such flag."""
        monkeypatch.setenv("POSTHOG_KEY", "phc_test")
        client = MagicMock()
        client.feature_enabled.return_value = None
        monkeypatch.setattr(L, "_get_posthog", lambda: client)
        assert await L.feature_enabled("gough.never-seen", "cluster-1") is False

    async def test_lookup_error_defaults_off(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_KEY", "phc_test")
        client = MagicMock()
        client.feature_enabled.side_effect = RuntimeError("posthog down")
        monkeypatch.setattr(L, "_get_posthog", lambda: client)
        assert await L.feature_enabled(L.FLAG_MULTI_CLOUD, "cluster-1") is False

    async def test_lookup_error_reuses_last_known_value(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_KEY", "phc_test")
        client = MagicMock()
        client.feature_enabled.return_value = True
        monkeypatch.setattr(L, "_get_posthog", lambda: client)
        assert await L.feature_enabled(L.FLAG_MULTI_CLOUD, "c1") is True

        for entry in L._cache.values():
            entry.fetched_at -= L._CACHE_TTL_SECONDS + 1
        client.feature_enabled.side_effect = RuntimeError("posthog down")
        assert await L.feature_enabled(L.FLAG_MULTI_CLOUD, "c1") is True

    async def test_client_init_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_KEY", "phc_test")
        with patch.dict("sys.modules", {"posthog": None}):
            assert await L.feature_enabled(L.FLAG_MULTI_CLOUD) is False


class TestManagedTag:
    """Telling gough-provisioned machines from a synced-in fleet."""

    def test_stamp_adds_marker_and_preserves_existing(self):
        assert L.stamp_managed_tag({"env": "prod"}) == {
            "env": "prod",
            L.MANAGED_TAG_KEY: L.MANAGED_TAG_VALUE,
        }

    def test_stamp_handles_none(self):
        assert L.stamp_managed_tag(None) == {L.MANAGED_TAG_KEY: L.MANAGED_TAG_VALUE}

    def test_stamp_does_not_mutate_caller_dict(self):
        original = {"env": "prod"}
        L.stamp_managed_tag(original)
        assert original == {"env": "prod"}

    @pytest.mark.parametrize(
        "tags",
        [
            {L.MANAGED_TAG_KEY: "true"},
            '{"gough-managed": "true"}',
            ["gough-managed=true", "env=prod"],
            {L.MANAGED_TAG_KEY: "TRUE"},
        ],
    )
    def test_recognises_managed_machines(self, tags):
        assert L._machine_is_gough_managed(SimpleNamespace(tags=tags)) is True

    @pytest.mark.parametrize(
        "tags",
        [
            None,
            "",
            {},
            [],
            {"env": "prod"},
            '{"env": "prod"}',
            ["env=prod"],
        ],
    )
    def test_unmanaged_machines_are_free_inventory(self, tags):
        assert L._machine_is_gough_managed(SimpleNamespace(tags=tags)) is False

    def test_reads_tags_from_dict_rows(self):
        assert (
            L._machine_is_gough_managed({"tags": {L.MANAGED_TAG_KEY: "true"}}) is True
        )


class TestCountActiveNodes:
    """Activation accounting across both registries."""

    @staticmethod
    def _db(node_count=0, machines=()):
        """Build a penguin-dal stand-in returning fixed counts/rows."""
        db = MagicMock()
        db.nodes = MagicMock()
        db.cloud_machines = MagicMock()

        def call(query):
            result = MagicMock()
            # The nodes query is a .belongs(); the machines query is a ~.belongs().
            result.count.return_value = node_count
            result.select.return_value = list(machines)
            return result

        db.side_effect = call
        return db

    def test_counts_active_bare_metal_and_managed_cloud(self):
        db = self._db(
            node_count=2,
            machines=[
                SimpleNamespace(tags={L.MANAGED_TAG_KEY: "true"}),
                SimpleNamespace(tags={L.MANAGED_TAG_KEY: "true"}),
            ],
        )
        assert L.count_active_nodes(db) == 4

    def test_synced_in_fleet_does_not_count(self):
        """An operator's pre-existing EC2 inventory is free."""
        db = self._db(
            node_count=0,
            machines=[
                SimpleNamespace(tags={"Name": "legacy-web-01"}),
                SimpleNamespace(tags=None),
                SimpleNamespace(tags={L.MANAGED_TAG_KEY: "true"}),
            ],
        )
        assert L.count_active_nodes(db) == 1

    def test_missing_tables_count_as_zero(self):
        db = SimpleNamespace()  # no .nodes, no .cloud_machines
        assert L.count_active_nodes(db) == 0

    def test_query_failure_does_not_raise(self):
        """A counting failure must not wedge deployment."""
        db = MagicMock()
        db.nodes = MagicMock()
        db.cloud_machines = MagicMock()
        db.side_effect = RuntimeError("db gone")
        assert L.count_active_nodes(db) == 0

    def test_active_states_exclude_inventory_and_terminal(self):
        # Guards the "inventory is free" contract against a careless edit.
        assert "new" not in L.ACTIVE_NODE_STATES
        assert "probed" not in L.ACTIVE_NODE_STATES
        assert "planned" not in L.ACTIVE_NODE_STATES
        assert "decommissioned" not in L.ACTIVE_NODE_STATES
        assert "rejected" not in L.ACTIVE_NODE_STATES
        assert {"ready", "deploying", "configuring"} <= L.ACTIVE_NODE_STATES

    def test_free_tier_limit_is_three(self):
        assert L.FREE_TIER_NODE_LIMIT == 3


async def _flag_on(*_a, **_k):
    return True


async def _flag_off(*_a, **_k):
    return False


@pytest.fixture()
def clouds_client():
    """A test client with only ``clouds_bp`` mounted.

    The blueprint is resolved from ``sys.modules`` at fixture time rather than
    imported at module scope: several cloud test modules reload
    ``app.api.clouds`` for their own decorator stubbing, which replaces the
    module object. A module-level ``from ... import clouds_bp`` would then hold
    a blueprint whose ``_gate_multi_cloud`` closes over the *old* module
    globals, while ``patch("app.api.clouds.feature_enabled")`` resolves the
    *new* one -- so the patch would silently miss and the gate tests would fail
    depending on which files ran first.
    """
    import importlib

    import app.api.clouds as clouds_mod

    # Reload before use. Several cloud test modules (test_clouds_api,
    # test_clouds_extended, test_clouds_coverage4) reload this module with
    # auth_required/roles_* monkeypatched to passthroughs and never restore it:
    # monkeypatch undoes the attributes on app.middleware, but the already-
    # reloaded clouds module keeps the stubs baked into its decorators. A gate
    # test that asserts "the flag opens, then auth rejects" would then see no
    # auth at all and fail purely on file ordering. Reloading here rebuilds the
    # blueprint against the real decorators, which is also the correct global
    # state to leave behind.
    clouds_mod = importlib.reload(clouds_mod)

    app = Quart(__name__)
    app.register_blueprint(clouds_mod.clouds_bp, url_prefix="/api/v1/clouds")
    return app.test_client()


@pytest.mark.asyncio
class TestCloudBlueprintGate:
    """The multi-cloud surface is switched off wholesale by its flag."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/clouds/"),
            ("get", "/api/v1/clouds/1"),
            ("post", "/api/v1/clouds/1/machines"),
            ("post", "/api/v1/clouds/1/machines/m-1/start"),
            ("get", "/api/v1/clouds/1/images"),
        ],
    )
    async def test_every_route_is_gated_when_flag_off(
        self, clouds_client, method, path
    ):
        with patch("app.api.clouds.feature_enabled", new=_flag_off):
            resp = await getattr(clouds_client, method)(path)
        body = await resp.get_json()
        assert resp.status_code == 404
        assert body["error"]["code"] == "feature_disabled"

    async def test_gate_opens_when_flag_on(self, clouds_client):
        """Flag on: the gate stops short-circuiting and auth takes over."""
        with patch("app.api.clouds.feature_enabled", new=_flag_on):
            resp = await clouds_client.get("/api/v1/clouds/")
        assert b"feature_disabled" not in await resp.get_data()
        assert resp.status_code == 401  # rejected by auth, not by the flag

    async def test_disabled_surface_is_indistinguishable_from_absent(
        self, clouds_client
    ):
        """404 not 403 -- a flagged-off surface must not advertise itself."""
        with patch("app.api.clouds.feature_enabled", new=_flag_off):
            resp = await clouds_client.get("/api/v1/clouds/")
        assert resp.status_code == 404
