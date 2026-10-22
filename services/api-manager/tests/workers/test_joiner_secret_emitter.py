"""Tests for joiner_secret_emitter worker.

Per spec constraints:
- Mock control tunnel per source-type (exec, file, http_api, lxd_config)
- Verify ciphertext stored has correct GCM tag + IV; DEK is wrapped (not bare)
- Audit chain row appended; NATS event published with NO secret data
- Verify memory zeroing (assert ctypes buffer is zero post-call)
- Verify auth-tag-mismatch → AuthTagMismatchError
- ≥90% coverage

Runs against real Postgres (``pg_db``) rather than an in-memory SQLite
SQLAlchemy session -- ``JoinerSecretEmitter`` now takes a penguin-dal ``DB``
(see ``app.workers.joiner_secret_emitter`` conversion), and its write path
(``_persist`` -> ``persist_extracted_material``) issues real parameterized
SQL through a ``db.transaction()``, which SQLite-in-memory can't exercise
faithfully (no ``%s`` psycopg2 paramstyle, no real RLS).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock, patch

import pytest

from app.clients.vault import VaultClient, VaultTransitDecryptResponse, VaultTransitEncryptResponse
from app.security.joiner_envelope import IV_BYTES, TAG_BYTES
from app.workers.joiner_secret_emitter import (
    ControlTunnelExtractionError,
    EmitterStateError,
    ExtractorSpecError,
    JoinerSecretEmitter,
    VAULT_KEK_NAME,
)

pytestmark = pytest.mark.asyncio


# =============================================================================
# Seed helpers (real penguin-dal inserts against pg_db)
# =============================================================================


def _seed_node(dal_db: Any, **overrides: Any) -> int:
    """``created_at``/``updated_at`` have no server-side DEFAULT (ORM-side
    ``default=`` only, invisible to penguin-dal's reflected-table insert) --
    supplied explicitly, matching every other direct-insert seed helper in
    this test suite (see ``tests/api/test_primary_dal_conversion.py``)."""
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        tenant_id="test-tenant",
        name=f"node-{uuid.uuid4().hex[:8]}",
        state="ready",
        dmi_uuid=f"dmi-{uuid.uuid4().hex[:8]}",
        primary_nic_mac="aa:bb:cc:dd:ee:ff",
        ipv4="192.168.1.10",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.nodes.insert(**base))


def _seed_biome(dal_db: Any, **overrides: Any) -> int:
    base: dict[str, Any] = dict(
        tenant_id="test-tenant",
        name=f"biome-{uuid.uuid4().hex[:8]}",
        version="1.0.0",
        biome_kind="k8s-primary",
        phase="phase_1_initial",
        workload_type="lxc",
        emits_joiner_secrets=False,
    )
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_assignment(dal_db: Any, *, node_id: int, egg_id: int, **overrides: Any) -> int:
    """``phase``/``assigned_at``/``created_at``/``updated_at`` are NOT NULL
    with no (or Python-only) defaults -- supplied explicitly."""
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        node_id=node_id,
        egg_id=egg_id,
        tenant_id="test-tenant",
        phase="phase_1_initial",
        status="ready",
        readiness_probe_state="passed",
        assigned_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.node_egg_assignments.insert(**base))


_K8S_EMIT_SPEC = {
    "extractors": [
        {
            "name": "kubeadm_join_token",
            "source": "exec",
            "command": ["kubeadm", "token", "create", "--print-join-command"],
            "ttl_seconds": 86400,
            "rotation": "single_use_per_node",
            "scope": "cluster",
        },
        {
            "name": "kubeadm_ca_hash",
            "source": "exec",
            "command": ["sh", "-c", "openssl x509 -in /etc/kubernetes/pki/ca.crt"],
            "ttl_seconds": 0,
            "rotation": "on_ca_rotation",
            "scope": "cluster",
        },
    ]
}


@pytest.fixture
def mock_vault_client() -> Any:
    """Mock VaultClient for transit encryption/decryption."""
    client = Mock(spec=VaultClient)
    client._wrapped_keys = {}

    def mock_encrypt(key_name: str, plaintext: bytes) -> VaultTransitEncryptResponse:
        wrapped = f"vault:v1:{uuid.uuid4().hex}"
        client._wrapped_keys[wrapped] = plaintext
        return VaultTransitEncryptResponse(ciphertext=wrapped, key_version=1)

    def mock_decrypt(key_name: str, ciphertext: str) -> VaultTransitDecryptResponse:
        if ciphertext not in client._wrapped_keys:
            raise ValueError(f"Unknown wrapped key: {ciphertext}")
        return VaultTransitDecryptResponse(plaintext=client._wrapped_keys[ciphertext])

    client.transit_encrypt = mock_encrypt
    client.transit_decrypt = mock_decrypt
    return client


@pytest.fixture
def mock_control_tunnel() -> Any:
    """Mock control tunnel client for LXD instance communication."""
    client = Mock()
    client.run_command = Mock(return_value=b"kubeadm-token-12345")
    client.read_file = Mock(return_value=b"file-contents")
    client.http_api = Mock(return_value=b"api-response")
    client.lxd_config = Mock(return_value=b"config-value")
    return client


@pytest.fixture
def mock_nats() -> Any:
    """Mock NATS publisher."""
    client = Mock()
    client.publish = Mock()
    return client


@pytest.fixture
def test_cluster_id() -> str:
    """Test cluster UUID."""
    return str(uuid.uuid4())


@pytest.fixture
def test_assignment(pg_db: Any) -> int:
    """Ready assignment for a k8s-primary biome that emits joiner secrets."""
    node_id = _seed_node(pg_db)
    biome_id = _seed_biome(pg_db, emits_joiner_secrets=True, joiner_emit_spec=_K8S_EMIT_SPEC)
    return _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)


class TestJoinerSecretEmitterEmit:
    """Test the main emit() entry point."""

    async def test_emit_success_exec_source(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Happy path: emit succeeds with exec source."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.return_value = b"secret-token-xyz"

        results = emitter.emit(test_assignment)

        assert len(results) == 2  # Two extractors in the spec
        assert results[0].extractor_name == "kubeadm_join_token"
        assert results[0].scope == "cluster"
        assert results[0].ttl_seconds == 86400
        assert results[0].rotation_class == "single_use_per_node"
        assert results[0].joiner_secret_id is not None

        stored = (
            pg_db(pg_db.joiner_secrets.id == results[0].joiner_secret_id)
            .select()
            .first()
        )
        assert stored is not None
        assert stored.biome_kind == "k8s-primary"
        assert stored.extractor_name == "kubeadm_join_token"
        assert stored.scope == "cluster"
        assert len(bytes(stored.ciphertext)) > 0
        assert len(bytes(stored.iv)) == IV_BYTES
        assert len(bytes(stored.auth_tag)) == TAG_BYTES
        assert len(bytes(stored.dek_wrapped)) > 0
        assert stored.vault_kek_name == VAULT_KEK_NAME

    async def test_emit_with_ttl(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Verify TTL calculation sets expires_at."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.return_value = b"token"

        before = datetime.now(timezone.utc)
        results = emitter.emit(test_assignment)

        stored = (
            pg_db(pg_db.joiner_secrets.id == results[0].joiner_secret_id)
            .select()
            .first()
        )

        assert stored.ttl_seconds == 86400
        assert stored.expires_at is not None
        expected_expires = before + timedelta(seconds=86400)
        assert abs((stored.expires_at - expected_expires).total_seconds()) < 5

    async def test_emit_audit_logged(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Verify audit chain entry appended."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.return_value = b"token"

        results = emitter.emit(test_assignment)

        audit_event = (
            pg_db(pg_db.audit_events.action == "joiner.secret.emit")
            .select()
            .first()
        )
        assert audit_event is not None
        assert audit_event.resource_kind == "joiner_secret"
        # Note: audit event should NOT contain secret ciphertext.
        assert "ciphertext" not in (audit_event.after_json or {})
        assert str(audit_event.resource_id) == str(results[0].joiner_secret_id)

    async def test_emit_nats_event_published(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Verify NATS event published (no secret payload)."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.return_value = b"token"

        emitter.emit(test_assignment)

        assert mock_nats.publish.call_count > 0

        calls = mock_nats.publish.call_args_list
        subject, payload = calls[0][0]
        assert f"gough.joiner.{test_cluster_id}" in subject
        assert "emitted" in subject

        payload_dict = json.loads(payload)
        assert "ciphertext" not in payload_dict
        assert "dek_wrapped" not in payload_dict

    async def test_emit_missing_assignment(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
    ) -> None:
        """Raise EmitterStateError when assignment not found."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        with pytest.raises(EmitterStateError):
            emitter.emit(999999)

    async def test_emit_egg_not_ready(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
    ) -> None:
        """Raise EmitterStateError when biome assignment not in ready state."""
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, emits_joiner_secrets=True, joiner_emit_spec=_K8S_EMIT_SPEC)
        assignment_id = _seed_assignment(
            pg_db,
            node_id=node_id,
            egg_id=biome_id,
            status="pending",
            readiness_probe_state="not_started",
        )

        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        with pytest.raises(EmitterStateError):
            emitter.emit(assignment_id)

    async def test_emit_egg_does_not_emit(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
    ) -> None:
        """Raise error when biome doesn't have emits_joiner_secrets=true."""
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, biome_kind="k8s-worker", emits_joiner_secrets=False)
        assignment_id = _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)

        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        with pytest.raises(EmitterStateError):
            emitter.emit(assignment_id)


class TestJoinerSecretEmitterExtraction:
    """Test extraction from different sources."""

    async def test_extract_exec_source(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Verify exec source calls control tunnel correctly."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.return_value = b"exec-output"
        emitter.emit(test_assignment)

        mock_control_tunnel.run_command.assert_called()

    async def test_extract_file_source(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
    ) -> None:
        """Verify file source calls file_read correctly."""
        emit_spec = {
            "extractors": [
                {
                    "name": "ceph_keyring",
                    "source": "file",
                    "path": "/etc/ceph/ceph.client.admin.keyring",
                    "ttl_seconds": 0,
                    "rotation": "manual",
                    "scope": "cluster",
                }
            ]
        }
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(
            pg_db, biome_kind="ceph-mon", emits_joiner_secrets=True, joiner_emit_spec=emit_spec
        )
        assignment_id = _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)

        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.read_file.return_value = b"keyring-data"
        results = emitter.emit(assignment_id)

        mock_control_tunnel.read_file.assert_called()
        assert results[0].extractor_name == "ceph_keyring"

    async def test_extract_http_api_source(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
    ) -> None:
        """Verify http_api source calls http_api_call correctly."""
        emit_spec = {
            "extractors": [
                {
                    "name": "nest_cluster_token",
                    "source": "http_api",
                    "url": "http://localhost:5000/api/v1/token",
                    "ttl_seconds": 3600,
                    "rotation": "time_based",
                    "scope": "cluster",
                }
            ]
        }
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(
            pg_db, biome_kind="nest-primary", emits_joiner_secrets=True, joiner_emit_spec=emit_spec
        )
        assignment_id = _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)

        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.http_api.return_value = b'{"token": "abc123"}'
        results = emitter.emit(assignment_id)

        mock_control_tunnel.http_api.assert_called()
        assert results[0].extractor_name == "nest_cluster_token"

    async def test_extract_lxd_config_source(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
    ) -> None:
        """Verify lxd_config source calls lxd_config_read correctly."""
        emit_spec = {
            "extractors": [
                {
                    "name": "cluster_certificate",
                    "source": "lxd_config",
                    "config_key": "core.trust_password",
                    "ttl_seconds": 0,
                    "rotation": "manual",
                    "scope": "cluster",
                }
            ]
        }
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(
            pg_db,
            biome_kind="lxd-cluster-member",
            emits_joiner_secrets=True,
            joiner_emit_spec=emit_spec,
        )
        assignment_id = _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)

        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.lxd_config.return_value = b"cert-value"
        results = emitter.emit(assignment_id)

        mock_control_tunnel.lxd_config.assert_called()
        assert results[0].extractor_name == "cluster_certificate"


class TestJoinerSecretEmitterEncryption:
    """Test encryption layers."""

    async def test_envelope_encryption_correct(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Verify ciphertext, IV, auth_tag, and wrapped DEK are stored correctly."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        secret_value = b"kubeadm-token-xyz"
        mock_control_tunnel.run_command.return_value = secret_value

        results = emitter.emit(test_assignment)

        stored = (
            pg_db(pg_db.joiner_secrets.id == results[0].joiner_secret_id)
            .select()
            .first()
        )

        assert isinstance(bytes(stored.ciphertext), bytes) and len(bytes(stored.ciphertext)) > 0
        assert len(bytes(stored.iv)) == IV_BYTES
        assert len(bytes(stored.auth_tag)) == TAG_BYTES
        assert len(bytes(stored.dek_wrapped)) > 0
        assert stored.vault_kek_name == VAULT_KEK_NAME

    async def test_memory_zeroing_after_persist(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Verify raw_bytes are zeroed after persistence.

        Defensive testing: intercepts the ``zero_bytes`` call and verifies
        it was invoked (actual memory zeroing is verified in
        test_joiner_envelope.py).
        """
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        secret_value = b"secret-bytes"
        mock_control_tunnel.run_command.return_value = secret_value

        with patch("app.workers.joiner_secret_emitter.zero_bytes") as mock_zero:
            emitter.emit(test_assignment)
            assert mock_zero.call_count > 0


class TestJoinerSecretEmitterErrors:
    """Test error handling."""

    async def test_extraction_error_propagates(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Propagate control tunnel extraction errors."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.side_effect = ControlTunnelExtractionError(
            "Connection refused"
        )

        with pytest.raises(ControlTunnelExtractionError):
            emitter.emit(test_assignment)

    async def test_vault_error_propagates(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """Propagate Vault transit errors."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.return_value = b"secret"
        mock_vault_client.transit_encrypt = Mock(side_effect=Exception("Vault sealed"))

        with pytest.raises(Exception):
            emitter.emit(test_assignment)

    async def test_vault_error_rolls_back_no_partial_row(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
        test_assignment: int,
    ) -> None:
        """A mid-transaction Vault failure must leave no joiner_secrets row
        behind -- proves ``_persist``'s ``db.transaction()`` genuinely rolls
        back on exception, the same regression class task 6a guarded for
        ``rotate_joiner_secret``."""
        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        mock_control_tunnel.run_command.return_value = b"secret"
        mock_vault_client.transit_encrypt = Mock(side_effect=Exception("Vault sealed"))

        with pytest.raises(Exception):
            emitter.emit(test_assignment)

        assert pg_db(pg_db.joiner_secrets.id > "").count() == 0
        assert pg_db(pg_db.audit_events.action == "joiner.secret.emit").count() == 0

    async def test_malformed_spec_raises_error(
        self,
        pg_db: Any,
        mock_vault_client: Any,
        mock_control_tunnel: Any,
        mock_nats: Any,
        test_cluster_id: str,
    ) -> None:
        """Raise ExtractorSpecError for malformed joiner_emit_spec."""
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(
            pg_db, biome_kind="bad", emits_joiner_secrets=True, joiner_emit_spec="not-json"
        )
        assignment_id = _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id)

        emitter = JoinerSecretEmitter(
            db=pg_db,
            vault_client=mock_vault_client,
            control_tunnel_client=mock_control_tunnel,
            nats_client=mock_nats,
            cluster_id=test_cluster_id,
        )

        with pytest.raises(ExtractorSpecError):
            emitter.emit(assignment_id)
