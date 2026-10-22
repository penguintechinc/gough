"""Extended coverage tests for joiner_secret_emitter.py missed lines.

``TestJoinerSecretEmitterLoadContext``/``TestJoinerSecretEmitterResolveClusterId``
run against real Postgres (``pg_db``) rather than mocking a SQLAlchemy
``.query().filter().one_or_none()`` chain -- ``_load_context``/
``_resolve_cluster_id`` are now penguin-dal ``db(...).select().first()``
calls (see the ``joiner_secret_emitter.py`` conversion), whose fluent
``__call__``/``QuerySet`` API doesn't map onto the old chain shape at all;
mocking it convincingly would just re-implement penguin-dal's query builder
with MagicMocks. The remaining classes below (``_derive_lxd_instance``,
``_validate_spec``, ``_extract_secret``, ``_publish_emitted_event``) never
touch ``self.db``, so they only needed the ``db_session=`` -> ``db=``
constructor kwarg rename.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from unittest.mock import MagicMock

from app.workers.joiner_secret_emitter import (
    JoinerSecretEmitter,
    ExtractorSpecError,
    ControlTunnelExtractionError,
    EmitterStateError,
    _mask,
)

pytestmark = pytest.mark.asyncio


# =============================================================================
# Seed helpers (real penguin-dal inserts against pg_db) -- same shapes as
# tests/workers/test_joiner_secret_emitter.py
# =============================================================================


def _seed_node(dal_db: Any, **overrides: Any) -> int:
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        tenant_id="test-tenant",
        name=f"node-{uuid.uuid4().hex[:8]}",
        state="ready",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.nodes.insert(**base))


def _seed_biome(dal_db: Any, **overrides: Any) -> int:
    base: dict[str, Any] = dict(
        tenant_id="test-tenant",
        name=f"biome-{uuid.uuid4().hex[:8]}",
        biome_kind="k8s-primary",
    )
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_assignment(dal_db: Any, *, node_id: int, egg_id: int, **overrides: Any) -> int:
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        node_id=node_id,
        egg_id=egg_id,
        tenant_id="test-tenant",
        phase="phase_1_initial",
        status="ready",
        assigned_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.node_egg_assignments.insert(**base))


class TestMaskFunction:
    """Test _mask helper for sanitized logging."""

    async def test_mask_none(self) -> None:
        assert _mask(None) == "<none>"

    async def test_mask_bytes(self) -> None:
        result = _mask(b"secret data")
        assert result == "<bytes len=11>"

    async def test_mask_bytearray(self) -> None:
        result = _mask(bytearray(b"secret"))
        assert result == "<bytes len=6>"

    async def test_mask_str(self) -> None:
        result = _mask("secret string")
        assert result == "<str len=13>"

    async def test_mask_other_type(self) -> None:
        result = _mask(12345)
        assert result == "<int>"


class TestJoinerSecretEmitterLoadContext:
    """Test context loading and validation (real Postgres)."""

    async def test_load_context_assignment_not_found(self, pg_db: Any) -> None:
        emitter = JoinerSecretEmitter(
            db=pg_db, vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        with pytest.raises(EmitterStateError, match="not found"):
            emitter._load_context(999999)

    async def test_load_context_assignment_not_ready(self, pg_db: Any) -> None:
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db)
        assignment_id = _seed_assignment(
            pg_db, node_id=node_id, egg_id=biome_id, status="provisioning"
        )

        emitter = JoinerSecretEmitter(
            db=pg_db, vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        with pytest.raises(EmitterStateError, match="status is"):
            emitter._load_context(assignment_id)

    async def test_load_context_biome_not_emitting(self, pg_db: Any) -> None:
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, emits_joiner_secrets=False)
        assignment_id = _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)

        emitter = JoinerSecretEmitter(
            db=pg_db, vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        with pytest.raises(EmitterStateError, match="emits_joiner_secrets=false"):
            emitter._load_context(assignment_id)

    async def test_load_context_success(self, pg_db: Any) -> None:
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, emits_joiner_secrets=True)
        assignment_id = _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)

        emitter = JoinerSecretEmitter(
            db=pg_db, vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        assignment, biome, node = emitter._load_context(assignment_id)
        assert assignment.id == assignment_id
        assert biome.id == biome_id
        assert node.id == node_id


class TestJoinerSecretEmitterResolveClusterId:
    """Test cluster ID resolution (real Postgres)."""

    async def test_resolve_cluster_id_from_constructor(self, pg_db: Any) -> None:
        """_resolve_cluster_id uses constructor override without touching the DB."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=MagicMock(),
            control_tunnel_client=MagicMock(),
            cluster_id="test-cluster",
        )

        node_id = _seed_node(pg_db)
        node = pg_db(pg_db.nodes.id == node_id).select().first()
        result = emitter._resolve_cluster_id(node)
        assert result == "test-cluster"

    async def test_resolve_cluster_id_from_storage_backend(self, pg_db: Any) -> None:
        """_resolve_cluster_id queries storage_backends when not set at construction."""
        node_id = _seed_node(pg_db)
        node = pg_db(pg_db.nodes.id == node_id).select().first()

        cluster_uuid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        pg_db.storage_backends.insert(
            id=str(uuid.uuid4()),
            cluster_id=cluster_uuid,
            tenant_id=node.tenant_id,
            kind="ceph",
            name="primary-backend",
            created_at=now,
            updated_at=now,
        )

        emitter = JoinerSecretEmitter(
            db=pg_db, vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        result = emitter._resolve_cluster_id(node)
        assert result == cluster_uuid

    async def test_resolve_cluster_id_storage_backend_missing(self, pg_db: Any) -> None:
        """_resolve_cluster_id raises when no storage_backends row exists for the tenant."""
        node_id = _seed_node(pg_db, tenant_id="lonely-tenant")
        node = pg_db(pg_db.nodes.id == node_id).select().first()

        emitter = JoinerSecretEmitter(
            db=pg_db, vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        with pytest.raises(EmitterStateError, match="cluster_id not resolvable"):
            emitter._resolve_cluster_id(node)


class TestJoinerSecretEmitterDeriveLxdInstance:
    """Test LXD instance name derivation."""

    async def test_derive_lxd_instance_host_workload(self) -> None:
        """_derive_lxd_instance uses node.name for host workloads."""
        mock_node = MagicMock()
        mock_node.name = "node-1"

        mock_biome = MagicMock()
        mock_biome.workload_type = "host"

        mock_assignment = MagicMock()

        result = JoinerSecretEmitter._derive_lxd_instance(
            mock_node, mock_biome, mock_assignment
        )
        assert result == "node-1"

    async def test_derive_lxd_instance_lxd_workload(self) -> None:
        """_derive_lxd_instance uses biome kind and assignment ID."""
        mock_node = MagicMock()
        mock_biome = MagicMock()
        mock_biome.biome_kind = "ubuntu"
        mock_biome.workload_type = "lxd"

        mock_assignment = MagicMock()
        mock_assignment.id = 42

        result = JoinerSecretEmitter._derive_lxd_instance(
            mock_node, mock_biome, mock_assignment
        )
        assert result == "ubuntu-42"


class TestJoinerSecretEmitterValidateSpec:
    """Test joiner_emit_spec validation."""

    async def test_validate_spec_missing(self) -> None:
        """_validate_spec returns empty list when spec is None."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.joiner_emit_spec = None

        result = emitter._validate_spec(mock_biome)
        assert result == []

    async def test_validate_spec_not_dict(self) -> None:
        """_validate_spec raises when spec is not dict."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.id = 1
        mock_biome.joiner_emit_spec = "invalid"

        with pytest.raises(ExtractorSpecError, match="must be an object"):
            emitter._validate_spec(mock_biome)

    async def test_validate_spec_extractors_not_list(self) -> None:
        """_validate_spec raises when extractors is not list."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.id = 1
        mock_biome.joiner_emit_spec = {"extractors": "not_a_list"}

        with pytest.raises(ExtractorSpecError, match="must be a list"):
            emitter._validate_spec(mock_biome)

    async def test_validate_spec_extractor_not_dict(self) -> None:
        """_validate_spec raises when extractor entry not dict."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.id = 1
        mock_biome.joiner_emit_spec = {"extractors": ["not_a_dict"]}

        with pytest.raises(ExtractorSpecError, match="must be an object"):
            emitter._validate_spec(mock_biome)

    async def test_validate_spec_invalid_source(self) -> None:
        """_validate_spec raises on invalid source."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.id = 1
        mock_biome.joiner_emit_spec = {
            "extractors": [{"name": "test", "source": "invalid_source"}]
        }

        with pytest.raises(ExtractorSpecError, match="unsupported source"):
            emitter._validate_spec(mock_biome)

    async def test_validate_spec_invalid_scope(self) -> None:
        """_validate_spec raises on invalid scope."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.id = 1
        mock_biome.joiner_emit_spec = {
            "extractors": [
                {
                    "name": "test",
                    "source": "exec",
                    "command": ["test"],
                    "scope": "invalid_scope",
                }
            ]
        }

        with pytest.raises(ExtractorSpecError, match="unsupported scope"):
            emitter._validate_spec(mock_biome)

    async def test_validate_spec_negative_ttl(self) -> None:
        """_validate_spec raises on negative ttl_seconds."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.id = 1
        mock_biome.joiner_emit_spec = {
            "extractors": [
                {"name": "test", "source": "exec", "command": ["test"], "ttl_seconds": -1}
            ]
        }

        with pytest.raises(ExtractorSpecError, match="ttl_seconds must be >= 0"):
            emitter._validate_spec(mock_biome)

    async def test_validate_spec_success(self) -> None:
        """_validate_spec returns validated extractors."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        mock_biome = MagicMock()
        mock_biome.id = 1
        mock_biome.joiner_emit_spec = {
            "extractors": [
                {
                    "name": "test_exec",
                    "source": "exec",
                    "command": ["echo", "hello"],
                    "scope": "cluster",
                    "ttl_seconds": 3600,
                }
            ]
        }

        result = emitter._validate_spec(mock_biome)
        assert len(result) == 1
        assert result[0].name == "test_exec"
        assert result[0].source == "exec"


class TestJoinerSecretEmitterExtractSecret:
    """Test secret extraction via control tunnel."""

    async def test_extract_secret_missing_command(self) -> None:
        """_extract_secret raises when exec source missing command."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        from app.workers.joiner_secret_emitter import _ExtractorSpec

        spec = _ExtractorSpec(name="test", source="exec")

        with pytest.raises(ExtractorSpecError, match="requires command"):
            emitter._extract_secret(1, "instance", spec)

    async def test_extract_secret_missing_path(self) -> None:
        """_extract_secret raises when file source missing path."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        from app.workers.joiner_secret_emitter import _ExtractorSpec

        spec = _ExtractorSpec(name="test", source="file")

        with pytest.raises(ExtractorSpecError, match="requires path"):
            emitter._extract_secret(1, "instance", spec)

    async def test_extract_secret_missing_url(self) -> None:
        """_extract_secret raises when http source missing url."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        from app.workers.joiner_secret_emitter import _ExtractorSpec

        spec = _ExtractorSpec(name="test", source="http_api")

        with pytest.raises(ExtractorSpecError, match="requires url"):
            emitter._extract_secret(1, "instance", spec)

    async def test_extract_secret_missing_config_key(self) -> None:
        """_extract_secret raises when lxd source missing config_key."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        from app.workers.joiner_secret_emitter import _ExtractorSpec

        spec = _ExtractorSpec(name="test", source="lxd_config")

        with pytest.raises(ExtractorSpecError, match="requires config_key"):
            emitter._extract_secret(1, "instance", spec)

    async def test_extract_secret_unsupported_source(self) -> None:
        """_extract_secret raises on unsupported source."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=MagicMock()
        )

        from app.workers.joiner_secret_emitter import _ExtractorSpec

        spec = _ExtractorSpec(name="test", source="unsupported")

        with pytest.raises(ExtractorSpecError, match="unsupported source"):
            emitter._extract_secret(1, "instance", spec)

    async def test_extract_secret_tunnel_failure(self) -> None:
        """_extract_secret raises ControlTunnelExtractionError on tunnel failure."""
        mock_tunnel = MagicMock()
        mock_tunnel.run_command.side_effect = Exception("Tunnel error")

        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=mock_tunnel
        )

        from app.workers.joiner_secret_emitter import _ExtractorSpec

        spec = _ExtractorSpec(name="test", source="exec", command=["echo", "test"])

        with pytest.raises(ControlTunnelExtractionError):
            emitter._extract_secret(1, "instance", spec)

    async def test_extract_secret_non_bytes_output(self) -> None:
        """_extract_secret raises when output is not bytes."""
        mock_tunnel = MagicMock()
        mock_tunnel.run_command.return_value = "not bytes"  # Invalid

        emitter = JoinerSecretEmitter(
            db=MagicMock(), vault_client=MagicMock(), control_tunnel_client=mock_tunnel
        )

        from app.workers.joiner_secret_emitter import _ExtractorSpec

        spec = _ExtractorSpec(name="test", source="exec", command=["echo", "test"])

        with pytest.raises(ControlTunnelExtractionError, match="non-bytes"):
            emitter._extract_secret(1, "instance", spec)


class TestJoinerSecretEmitterPublishEvent:
    """Test NATS event publishing."""

    async def test_publish_emitted_event_no_nats(self) -> None:
        """_publish_emitted_event returns early when no NATS client."""
        emitter = JoinerSecretEmitter(
            db=MagicMock(),
            vault_client=MagicMock(),
            control_tunnel_client=MagicMock(),
            nats_client=None,
        )

        # Should not raise
        emitter._publish_emitted_event(
            cluster_id="cluster1",
            joiner_secret_id="secret1",
            biome_kind="ubuntu",
            extractor_name="test",
            scope="cluster",
        )
