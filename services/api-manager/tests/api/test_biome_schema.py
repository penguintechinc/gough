"""Unit tests for ``app.api._biome_schema`` (Sprint 2).

Validates the Pydantic v2 models that back ``POST /api/v1/biomes``,
``POST /api/v1/biomes/{id}/sign``, ``POST /api/v1/biomes/{id}/upgrade``,
and ``POST /api/v1/nodes/{id}/biomes``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api._biome_schema import (
    BiomeCreate,
    BiomeSignRequest,
    BiomeUpgradeRequest,
    NodeBiomeAssignRequest,
    ReadinessProbe,
    StorageRequirements,
)


class TestBiomeCreateMinimal:
    def test_minimal_required_fields(self) -> None:
        b = BiomeCreate.model_validate(
            {
                "name": "k8s-primary",
                "display_name": "Kubernetes Primary",
                "biome_type": "lxd_container",
                "version": "1.31.0",
            }
        )
        assert b.name == "k8s-primary"
        assert b.biome_kind == "custom"  # default
        assert b.phase == "post_deploy"
        assert b.workload_type == "lxc"
        assert b.lock_to_host is False

    def test_missing_name_fails(self) -> None:
        with pytest.raises(ValidationError):
            BiomeCreate.model_validate(
                {"display_name": "x", "biome_type": "snap", "version": "1.0.0"}
            )

    def test_invalid_biome_kind(self) -> None:
        with pytest.raises(ValidationError):
            BiomeCreate.model_validate(
                {
                    "name": "x",
                    "display_name": "x",
                    "biome_type": "snap",
                    "version": "1",
                    "biome_kind": "weather",
                }
            )

    def test_invalid_phase(self) -> None:
        with pytest.raises(ValidationError):
            BiomeCreate.model_validate(
                {
                    "name": "x",
                    "display_name": "x",
                    "biome_type": "snap",
                    "version": "1",
                    "phase": "phase_x",
                }
            )

    def test_invalid_workload_type(self) -> None:
        with pytest.raises(ValidationError):
            BiomeCreate.model_validate(
                {
                    "name": "x",
                    "display_name": "x",
                    "biome_type": "snap",
                    "version": "1",
                    "workload_type": "container",
                }
            )

    def test_invalid_biome_type(self) -> None:
        with pytest.raises(ValidationError):
            BiomeCreate.model_validate(
                {
                    "name": "x",
                    "display_name": "x",
                    "biome_type": "vm",
                    "version": "1",
                }
            )


class TestBiomeCreateFullSpec:
    """Authoring conventions per spec — full ``biome.yaml`` body."""

    def test_full_spec(self) -> None:
        body = {
            "name": "k8s-primary",
            "display_name": "MicroK8s Primary",
            "description": "Bootstraps the cluster control plane.",
            "biome_type": "lxd_container",
            "version": "1.31.0",
            "biome_kind": "k8s",
            "phase": "post_deploy",
            "workload_type": "lxc",
            "lock_to_host": True,
            "auto_join_cluster": False,
            "upgrade_strategy": "rolling",
            "requires_hardware_tags": ["tpm:2.0", "mem:total-gb:32"],
            "prefers_hardware_tags": ["region:us-east"],
            "forbids_hardware_tags": ["lifecycle:playground"],
            "storage_requirements": {
                "min_ram_mb": 4096,
                "min_disk_gb": 50,
                "needs_gpu": False,
            },
            "readiness_probe": {
                "httpGet": {"path": "/healthz", "port": 16443, "scheme": "HTTPS"},
                "timeoutSeconds": 5,
                "periodSeconds": 2,
                "failureThreshold": 30,
            },
            "emits_joiner_secrets": True,
            "joiner_emit_spec": {"extractor": "kubeadm-token", "ttl_seconds": 3600},
        }
        e = BiomeCreate.model_validate(body)
        assert e.biome_kind == "k8s"
        assert e.lock_to_host is True
        assert e.requires_hardware_tags == ["tpm:2.0", "mem:total-gb:32"]
        assert e.storage_requirements is not None
        assert e.storage_requirements.min_ram_mb == 4096
        assert e.readiness_probe is not None
        assert e.readiness_probe.http_get is not None
        assert e.readiness_probe.http_get.port == 16443


class TestReadinessProbe:
    def test_http_get_only_passes(self) -> None:
        ReadinessProbe.model_validate(
            {"httpGet": {"path": "/", "port": 80}}
        )

    def test_grpc_only_passes(self) -> None:
        ReadinessProbe.model_validate({"grpc": {"service": "Health", "port": 50051}})

    def test_exec_only_passes(self) -> None:
        ReadinessProbe.model_validate({"exec": {"command": ["true"]}})

    def test_lxd_exec_only_passes(self) -> None:
        ReadinessProbe.model_validate(
            {"lxdExec": {"instance": "k8s-1", "command": ["snap", "status"]}}
        )

    def test_two_kinds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReadinessProbe.model_validate(
                {
                    "httpGet": {"path": "/", "port": 80},
                    "grpc": {"service": "h", "port": 1},
                }
            )

    def test_zero_kinds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReadinessProbe.model_validate({})

    def test_default_thresholds(self) -> None:
        p = ReadinessProbe.model_validate({"exec": {"command": ["true"]}})
        assert p.timeout_seconds == 5
        assert p.period_seconds == 2
        assert p.failure_threshold == 30
        assert p.success_threshold == 1


class TestStorageRequirements:
    def test_defaults(self) -> None:
        s = StorageRequirements.model_validate({})
        assert s.min_ram_mb == 0
        assert s.min_disk_gb == 0
        assert s.needs_gpu is False

    def test_negative_ram_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StorageRequirements.model_validate({"min_ram_mb": -1})


class TestBiomeSignRequest:
    def test_valid(self) -> None:
        body = BiomeSignRequest.model_validate(
            {"key_id": "vault-transit-prod-cosign", "reason": "release"}
        )
        assert body.key_id.startswith("vault-")

    def test_missing_key_id(self) -> None:
        with pytest.raises(ValidationError):
            BiomeSignRequest.model_validate({"reason": "x"})

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BiomeSignRequest.model_validate(
                {"key_id": "k", "reason": "r", "extra": "no"}
            )

    def test_empty_key_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BiomeSignRequest.model_validate({"key_id": "", "reason": "r"})


class TestBiomeUpgradeRequest:
    def test_minimal(self) -> None:
        u = BiomeUpgradeRequest.model_validate({"target_version": "1.31.1"})
        assert u.rollout_plan == "auto"
        assert u.approval_token is None

    def test_full(self) -> None:
        u = BiomeUpgradeRequest.model_validate(
            {
                "target_version": "1.31.1",
                "approval_token": "chatops-approval-abc123",
                "rollout_plan": "canary",
            }
        )
        assert u.rollout_plan == "canary"

    def test_invalid_rollout_plan(self) -> None:
        with pytest.raises(ValidationError):
            BiomeUpgradeRequest.model_validate(
                {"target_version": "1.0", "rollout_plan": "blast-radius"}
            )


class TestNodeBiomeAssignRequest:
    def test_minimal(self) -> None:
        a = NodeBiomeAssignRequest.model_validate({"egg_id": 42})
        assert a.phase == "post_deploy"
        assert a.depends_on_biome_instance_id is None

    def test_with_dependency(self) -> None:
        a = NodeBiomeAssignRequest.model_validate(
            {"egg_id": 42, "phase": "phase2_initial", "depends_on_egg_instance_id": 7}
        )
        assert a.depends_on_biome_instance_id == 7

    def test_zero_egg_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NodeBiomeAssignRequest.model_validate({"egg_id": 0})

    def test_negative_dependency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NodeBiomeAssignRequest.model_validate(
                {"egg_id": 1, "depends_on_egg_instance_id": -1}
            )
