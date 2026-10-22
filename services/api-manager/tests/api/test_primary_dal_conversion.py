"""Tests for the penguin-dal conversion of ``app.api.primary``'s DB access.

Covers the sites converted off raw ``db.query(...)`` (which never actually
worked -- ``penguin_dal.DB`` has no ``.query()`` method at all; every call
site here raised ``TableNotFoundError("query")`` immediately, caught by the
surrounding ``try/except`` and silently degraded. See each test class'
docstring for specifics.):

* ``_get_joiner_secrets`` -- relational SELECT over ``joiner_secrets``
  (former ``primary.py:209``), filter case + empty-result case.
* ``switch_frontend``'s primary-node-id lookup (former ``primary.py:759``)
  -- exercised through the real HTTP endpoint with the network-touching
  helpers (``_update_endpoint_on_nodes``/``_emit_gracious_arp``) stubbed out,
  so only the DB conversion is under test.
* ``_rotate_bootstrap_ca`` (former ``primary.py:916``) -- the
  ``db.executesql()`` escape hatch for the ``REGEXP_REPLACE`` UPDATE, proven
  against a scratch table (the real schema has no ``biome_templates`` table
  at all -- a separate, pre-existing, unrelated finding documented on the
  function itself and re-proven here).
"""

from __future__ import annotations

import importlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from quart import Quart, g

from app.api import primary as primary_module
from app.api.primary import _get_joiner_secrets, _rotate_bootstrap_ca


# =============================================================================
# Seed helpers
# =============================================================================


def _seed_biome(dal_db: Any, **overrides: Any) -> int:
    """Insert a minimal real ``biomes`` row; ``joiner_secrets.emitter_biome_id``
    is a NOT NULL FK to it."""
    base: dict[str, Any] = dict(name=f"biome-{uuid.uuid4().hex[:8]}")
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_joiner_secret(
    dal_db: Any, *, emitter_biome_id: Optional[int] = None, **overrides: Any
) -> str:
    """Insert a real ``joiner_secrets`` row via penguin-dal; return its id (str)."""
    if emitter_biome_id is None:
        emitter_biome_id = _seed_biome(dal_db)
    base: dict[str, Any] = dict(
        id=str(uuid.uuid4()),
        cluster_id=str(uuid.uuid4()),
        tenant_id="acme",
        biome_kind="vault",
        emitter_biome_id=emitter_biome_id,
        emitter_node_id=None,
        extractor_name="kubeadm_join_token",
        scope="cluster",
        ciphertext=b"secret-bytes",
        iv=b"IIIIIIIIIIII",
        auth_tag=b"TTTTTTTTTTTTTTTT",
        dek_wrapped=b"vault:v1:DEK",
        vault_kek_name="gough-joiner-dek-wrap",
        ttl_seconds=3600,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        rotation_class="vault-unseal",
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    base["id"] = str(base["id"])
    base["cluster_id"] = str(base["cluster_id"])
    return str(dal_db.joiner_secrets.insert(**base))


def _seed_node(dal_db: Any, **overrides: Any) -> int:
    """Insert a real ``nodes`` row. created_at/updated_at have no
    server-side DEFAULT (app.models_m1.Node sets them via SQLAlchemy
    ORM-side ``default=``, invisible to penguin-dal's reflected-table
    insert) -- supplied explicitly, same as every other direct-insert seed
    helper in this test suite."""
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        name=f"node-{uuid.uuid4().hex[:8]}", state="new", tenant_id="acme",
        created_at=now, updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.nodes.insert(**base))


# =============================================================================
# _get_joiner_secrets (former primary.py:209)
# =============================================================================


@pytest.mark.asyncio
class TestGetJoinerSecrets:
    """Direct calls -- this is a plain module-level helper, no HTTP needed."""

    async def test_returns_allowlisted_names_for_cluster(self, pg_db: Any) -> None:
        """Filter case: cluster_id match + allowlisted extractor_name."""
        cluster_id = str(uuid.uuid4())
        _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, extractor_name="kubeadm_join_token",
            ciphertext=b"tok",
        )
        _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, extractor_name="kubeadm_ca_hash",
            ciphertext=b"hash",
        )
        # Not in the allowlist -- must be excluded from the result.
        _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, extractor_name="not_relevant",
            ciphertext=b"x",
        )

        secrets = await _get_joiner_secrets(pg_db, cluster_id)

        assert {k: bytes(v) for k, v in secrets.items()} == {
            "kubeadm_join_token": b"tok",
            "kubeadm_ca_hash": b"hash",
        }

    async def test_excludes_revoked_and_expired(self, pg_db: Any) -> None:
        cluster_id = str(uuid.uuid4())
        _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, extractor_name="kubeadm_join_token",
            revoked_at=datetime.now(timezone.utc),
        )
        _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, extractor_name="kubeadm_ca_hash",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        secrets = await _get_joiner_secrets(pg_db, cluster_id)

        assert secrets == {}

    async def test_empty_result_for_unknown_cluster(self, pg_db: Any) -> None:
        """Empty-result case: rows exist, but not for this cluster_id."""
        _seed_joiner_secret(pg_db, cluster_id=str(uuid.uuid4()))

        secrets = await _get_joiner_secrets(pg_db, "no-such-cluster")

        assert secrets == {}


# =============================================================================
# switch_frontend primary-node lookup (former primary.py:759)
# =============================================================================


def _passthrough_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    """Stub for auth_required (webhooks/primary both genuinely need a real
    Authorization header + DB user lookup otherwise -- see app.middleware).
    """
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn: Any) -> Any:
        return fn

    return _wrap


def _build_app(*, dal_db: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Quart, Any]:
    """Build a Quart app with a freshly-reloaded ``primary_bp`` registered.

    ``auth_required`` (from ``..middleware``) is a real decorator requiring a
    bearer token + DB user lookup -- monkeypatching it after
    ``app.api.primary`` was already imported (at collection time, by every
    other test module) has no effect since decorators bind at import/reload
    time, not call time. Reload the module after patching, mirroring
    ``tests/api/conftest.py``'s existing ``client`` fixture for this same
    blueprint. Returns the reloaded module too, since
    ``_update_endpoint_on_nodes``/``_emit_gracious_arp`` monkeypatches must
    target *that* module object's globals (what ``switch_frontend`` actually
    looks up at call time), not the one imported at the top of this file.
    """
    import app.middleware as mw_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)

    primary_mod = importlib.reload(primary_module)
    monkeypatch.setattr(primary_mod, "get_db", lambda: dal_db)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["CLUSTER_ID"] = "test-cluster"
    app.url_map.strict_slashes = False
    app.register_blueprint(primary_mod.primary_bp, url_prefix="/api/v1/primary")

    @app.before_request
    async def _inject_auth() -> None:
        g.current_user = {
            "_jwt_payload": {
                "tenant": "default",
                "scope": (
                    "gough.cluster.read gough.cluster.admin "
                    "gough.cluster.superadmin"
                ),
                "sub": "test-operator",
            }
        }
        g.mfa_verified = True

    return app, primary_mod


@pytest.mark.asyncio
class TestFrontendSwitchPrimaryNodeLookup:
    """Exercises the converted primary-node-id query through the real HTTP
    endpoint, with the network-touching helpers stubbed so only the DB
    conversion (``db(db.nodes.state == "primary").select(db.nodes.id)``) is
    under test -- not etcd/gRPC/ARP, which are out of scope here.
    """

    async def test_queries_only_primary_state_nodes(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Filter case: only state='primary' nodes are selected."""
        primary_a = _seed_node(pg_db, name="primary-a", state="primary")
        primary_b = _seed_node(pg_db, name="primary-b", state="primary")
        _seed_node(pg_db, name="worker-a", state="ready")

        captured: dict[str, Any] = {}

        async def fake_update_endpoint(new_endpoint: str, node_ids: list) -> dict:
            captured["node_ids"] = node_ids
            # Real shape: dict[node_id] -> {"success": bool, ...}. The stub used
            # to return {"ok": True}, which only worked while the caller tested
            # the dict for truthiness -- a bug that reported all-nodes-failed as
            # success. Mirror the real contract so this test isolates the DB
            # conversion without also pinning the broken one.
            return {nid: {"success": True, "error": None} for nid in node_ids}

        async def fake_arp(*args: Any, **kwargs: Any) -> dict:
            return {}

        app, primary_mod = _build_app(dal_db=pg_db, monkeypatch=monkeypatch)
        monkeypatch.setattr(primary_mod, "_update_endpoint_on_nodes", fake_update_endpoint)
        monkeypatch.setattr(primary_mod, "_emit_gracious_arp", fake_arp)

        client = app.test_client()
        resp = await client.post(
            "/api/v1/primary/frontend-switch",
            json={"target_mode": "kube-vip", "new_endpoint": "10.0.0.1:6443"},
        )
        assert resp.status_code == 200
        assert sorted(captured["node_ids"]) == sorted([primary_a, primary_b])

    async def test_empty_result_when_no_primary_nodes(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty-result case: nodes exist, but none in state='primary'."""
        _seed_node(pg_db, name="worker-only", state="ready")

        captured: dict[str, Any] = {}

        async def fake_update_endpoint(new_endpoint: str, node_ids: list) -> dict:
            captured["node_ids"] = node_ids
            return {nid: {"success": True, "error": None} for nid in node_ids}

        async def fake_arp(*args: Any, **kwargs: Any) -> dict:
            return {}

        app, primary_mod = _build_app(dal_db=pg_db, monkeypatch=monkeypatch)
        monkeypatch.setattr(primary_mod, "_update_endpoint_on_nodes", fake_update_endpoint)
        monkeypatch.setattr(primary_mod, "_emit_gracious_arp", fake_arp)

        client = app.test_client()
        resp = await client.post(
            "/api/v1/primary/frontend-switch",
            json={"target_mode": "kube-vip", "new_endpoint": "10.0.0.1:6443"},
        )
        assert resp.status_code == 200
        assert captured["node_ids"] == []

    async def test_emits_gracious_arp_with_primary_node_ids(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: gh-22 -- the kube-vip branch of ``switch_frontend``
        used to call ``_emit_gracious_arp(vip)`` with no ``node_ids``
        argument at all, a guaranteed ``TypeError`` against the real
        ``_emit_gracious_arp(vip: str, node_ids: list[int])`` signature
        (former ``primary.py:797``).

        Unlike the permissive ``async def fake_arp(*args, **kwargs)``
        stand-ins used by the two tests above (which would silently accept
        any call shape, including the buggy one-arg call, and never catch
        this), this fake intentionally mirrors the real function's exact
        two-positional-argument signature -- so a regression back to the
        one-arg call raises the very same ``TypeError`` a real,
        un-stubbed call would.
        """
        primary_a = _seed_node(pg_db, name="primary-a", state="primary")
        primary_b = _seed_node(pg_db, name="primary-b", state="primary")
        _seed_node(pg_db, name="worker-a", state="ready")

        captured: dict[str, Any] = {}

        async def fake_update_endpoint(
            new_endpoint: str, node_ids: list[int]
        ) -> dict[int, dict[str, Any]]:
            return {nid: {"success": True, "error": None} for nid in node_ids}

        async def strict_fake_arp(vip: str, node_ids: list[int]) -> dict[str, Any]:
            captured["vip"] = vip
            captured["node_ids"] = node_ids
            return {}

        app, primary_mod = _build_app(dal_db=pg_db, monkeypatch=monkeypatch)
        monkeypatch.setattr(
            primary_mod, "_update_endpoint_on_nodes", fake_update_endpoint
        )
        monkeypatch.setattr(primary_mod, "_emit_gracious_arp", strict_fake_arp)

        client = app.test_client()
        resp = await client.post(
            "/api/v1/primary/frontend-switch",
            json={"target_mode": "kube-vip", "new_endpoint": "10.0.0.1:6443"},
        )
        body = await resp.get_data(as_text=True)
        assert resp.status_code == 200, body
        assert captured["vip"] == "10.0.0.1"
        assert sorted(captured["node_ids"]) == sorted([primary_a, primary_b])


# =============================================================================
# _rotate_bootstrap_ca (former primary.py:916)
# =============================================================================


class TestRotateBootstrapCA:
    """``db.executesql()`` escape hatch replacing the old (never-functional
    -- ``penguin_dal.DB`` has no ``.query()`` method) ``db.query(...)``
    REGEXP_REPLACE UPDATE.

    ``biome_templates`` does not exist anywhere in the real schema (see the
    function's own docstring) -- proven two ways below: (1) the SQL itself
    is correct, demonstrated against a scratch table with the same shape;
    (2) against the real (tableless) schema it still raises, exactly
    matching pre-conversion behaviour, caught by the caller
    (``rotate_ipxe_ca``) with a logged warning, not a 500.
    """

    def test_updates_matching_biome_kind_rows(self, pg_db: Any) -> None:
        pg_db.executesql("DROP TABLE IF EXISTS biome_templates")
        try:
            pg_db.executesql(
                "CREATE TABLE biome_templates ("
                "id SERIAL PRIMARY KEY, biome_kind VARCHAR(64), "
                "cloud_init_content TEXT)"
            )
            pg_db.executesql(
                "INSERT INTO biome_templates (biome_kind, cloud_init_content) "
                "VALUES (%s, %s)",
                ("k8s-helper", "ca: ---CA_CERT---\nother: unchanged"),
            )
            pg_db.executesql(
                "INSERT INTO biome_templates (biome_kind, cloud_init_content) "
                "VALUES (%s, %s)",
                ("other-kind", "ca: ---CA_CERT---"),
            )

            _rotate_bootstrap_ca(pg_db, "NEW-CERT-PEM")

            rows = pg_db.executesql(
                "SELECT biome_kind, cloud_init_content FROM biome_templates "
                "ORDER BY id",
                as_dict=True,
            )
            assert rows[0]["cloud_init_content"] == "ca: NEW-CERT-PEM\nother: unchanged"
            # Wrong biome_kind -- untouched.
            assert rows[1]["cloud_init_content"] == "ca: ---CA_CERT---"
        finally:
            pg_db.executesql("DROP TABLE IF EXISTS biome_templates")

    def test_raises_against_real_schema_missing_table(self, pg_db: Any) -> None:
        pg_db.executesql("DROP TABLE IF EXISTS biome_templates")
        with pytest.raises(Exception):
            _rotate_bootstrap_ca(pg_db, "NEW-CERT-PEM")
