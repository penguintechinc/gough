"""Coverage tests for migration_engine.py - validation and safety envelope."""

from __future__ import annotations

import pytest

from app.workers.migration_engine import (
    validate_policy_patch,
    validate_combination,
    NodeSnapshot,
    BiomeSnapshot,
    ClusterState,
    MigrationPolicy,
    MigrationPlan,
    Violation,
    SafetyResult,
    evaluate,
    _violates_headroom,
    _ready_nodes,
    _candidate_nodes,
    violations_to_dicts,
    safety_result_to_dict,
)


# ============================================================================
# Validation Tests (lines 159-164, 167-173, 178-183, 196-201, 203)
# ============================================================================


def test_validate_policy_patch_unknown_field():
    """Validate patch with unknown field should return violation."""
    patch = {"unknown_field": 123}
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "unknown_field"
    assert violations[0].field == "unknown_field"


def test_validate_policy_patch_bool_type_error():
    """Validate bool field with non-bool should fail."""
    patch = {"enabled": "true"}  # string instead of bool
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "type_error"
    assert violations[0].field == "enabled"


def test_validate_policy_patch_int_type_error():
    """Validate int field with non-int should fail."""
    patch = {"evaluation_interval_seconds": "300"}  # string instead of int
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "type_error"


def test_validate_policy_patch_int_bool_type_error():
    """Validate int field with bool (which is int subclass) should fail."""
    patch = {"evaluation_interval_seconds": True}  # bool is not valid
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "type_error"


def test_validate_policy_patch_int_out_of_range():
    """Validate int field out of range should fail."""
    patch = {"evaluation_interval_seconds": 10}  # Below minimum 60
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "out_of_range"
    assert violations[0].actual == 10.0
    assert violations[0].required == 60.0


def test_validate_policy_patch_valid_int():
    """Validate valid int field should pass."""
    patch = {"evaluation_interval_seconds": 300}
    violations = validate_policy_patch(patch)

    assert len(violations) == 0


def test_validate_policy_patch_valid_bool():
    """Validate valid bool field should pass."""
    patch = {"enabled": True}
    violations = validate_policy_patch(patch)

    assert len(violations) == 0


def test_validate_policy_patch_waddleai_threshold_invalid_type():
    """Validate waddleai_risk_threshold with bool should fail."""
    patch = {"waddleai_risk_threshold": True}
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "type_error"


def test_validate_policy_patch_waddleai_threshold_out_of_range():
    """Validate waddleai_risk_threshold > 1.0 should fail."""
    patch = {"waddleai_risk_threshold": 1.5}
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "out_of_range"
    assert violations[0].actual == 1.5


def test_validate_policy_patch_waddleai_threshold_valid():
    """Validate valid waddleai_risk_threshold."""
    patch = {"waddleai_risk_threshold": 0.75}
    violations = validate_policy_patch(patch)

    assert len(violations) == 0


def test_validate_policy_patch_horizon_days_invalid():
    """Validate capacity_forecast_horizon_days with invalid value."""
    patch = {"capacity_forecast_horizon_days": 5}
    violations = validate_policy_patch(patch)

    assert len(violations) == 1
    assert violations[0].code == "out_of_range"


def test_validate_policy_patch_horizon_days_valid_1():
    """Validate capacity_forecast_horizon_days=1."""
    patch = {"capacity_forecast_horizon_days": 1}
    violations = validate_policy_patch(patch)

    assert len(violations) == 0


def test_validate_policy_patch_horizon_days_valid_7():
    """Validate capacity_forecast_horizon_days=7."""
    patch = {"capacity_forecast_horizon_days": 7}
    violations = validate_policy_patch(patch)

    assert len(violations) == 0


def test_validate_policy_patch_horizon_days_valid_30():
    """Validate capacity_forecast_horizon_days=30."""
    patch = {"capacity_forecast_horizon_days": 30}
    violations = validate_policy_patch(patch)

    assert len(violations) == 0


# ============================================================================
# Combination Validation Tests (lines 211-213, 237)
# ============================================================================


def test_validate_combination_min_healthy_exceeds_cluster():
    """Validate min_healthy_nodes exceeds cluster size."""
    merged = {"min_healthy_nodes": 10}
    violations = validate_combination(merged, cluster_size=5)

    assert len(violations) == 1
    assert violations[0].code == "impossible_floor"
    assert violations[0].field == "min_healthy_nodes"


def test_validate_combination_min_healthy_equals_cluster():
    """Validate min_healthy_nodes equals cluster size."""
    merged = {"min_healthy_nodes": 5}
    violations = validate_combination(merged, cluster_size=5)

    assert len(violations) == 0


def test_validate_combination_no_cluster_size():
    """Validate without cluster size should pass."""
    merged = {"min_healthy_nodes": 100}
    violations = validate_combination(merged, cluster_size=None)

    assert len(violations) == 0


def test_validate_combination_non_int_min_healthy():
    """Validate non-int min_healthy_nodes should skip check."""
    merged = {"min_healthy_nodes": "10"}
    violations = validate_combination(merged, cluster_size=5)

    # Should not fail for non-int
    assert len(violations) == 0


# ============================================================================
# Headroom Violation Tests (lines 281, 294, 307, 347-357)
# ============================================================================


def test_violates_headroom_insufficient_cpu():
    """Check insufficient CPU headroom returns violation."""
    node = NodeSnapshot(
        node_id=1,
        state="ready",
        cpu_free_pct=10.0,
        mem_free_pct=50.0,
        disk_free_pct=50.0,
        forecast_cpu_free_pct=10.0,
        forecast_mem_free_pct=50.0,
        forecast_disk_free_pct=50.0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=2,
        lock_to_host=False,
        expected_cpu_load_pct=15.0,  # Would exceed headroom
        expected_mem_load_pct=10.0,
        expected_disk_load_pct=10.0,
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,  # Need 5% left
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=True,
        forbid_migration_during_maintenance=True,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    violation = _violates_headroom(node, biome, policy)

    assert violation is not None
    assert violation.code == "insufficient_cpu_headroom"
    assert violation.node_id == 1


def test_violates_headroom_insufficient_memory():
    """Check insufficient memory headroom returns violation."""
    node = NodeSnapshot(
        node_id=1,
        state="ready",
        cpu_free_pct=50.0,
        mem_free_pct=10.0,
        disk_free_pct=50.0,
        forecast_cpu_free_pct=50.0,
        forecast_mem_free_pct=10.0,
        forecast_disk_free_pct=50.0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=2,
        lock_to_host=False,
        expected_cpu_load_pct=10.0,
        expected_mem_load_pct=15.0,  # Would exceed headroom
        expected_disk_load_pct=10.0,
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=True,
        forbid_migration_during_maintenance=True,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    violation = _violates_headroom(node, biome, policy)

    assert violation is not None
    assert violation.code == "insufficient_mem_headroom"


def test_violates_headroom_insufficient_disk():
    """Check insufficient disk headroom returns violation."""
    node = NodeSnapshot(
        node_id=1,
        state="ready",
        cpu_free_pct=50.0,
        mem_free_pct=50.0,
        disk_free_pct=10.0,
        forecast_cpu_free_pct=50.0,
        forecast_mem_free_pct=50.0,
        forecast_disk_free_pct=10.0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=2,
        lock_to_host=False,
        expected_cpu_load_pct=10.0,
        expected_mem_load_pct=10.0,
        expected_disk_load_pct=15.0,  # Would exceed headroom
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=True,
        forbid_migration_during_maintenance=True,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    violation = _violates_headroom(node, biome, policy)

    assert violation is not None
    assert violation.code == "insufficient_disk_headroom"


def test_violates_headroom_sufficient():
    """Check sufficient headroom returns None."""
    node = NodeSnapshot(
        node_id=1,
        state="ready",
        cpu_free_pct=50.0,
        mem_free_pct=50.0,
        disk_free_pct=50.0,
        forecast_cpu_free_pct=50.0,
        forecast_mem_free_pct=50.0,
        forecast_disk_free_pct=50.0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=2,
        lock_to_host=False,
        expected_cpu_load_pct=10.0,
        expected_mem_load_pct=10.0,
        expected_disk_load_pct=10.0,
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=True,
        forbid_migration_during_maintenance=True,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    violation = _violates_headroom(node, biome, policy)

    assert violation is None


# ============================================================================
# Evaluation Tests (lines 367-377, 388-393, 402-407, 417-425, 440-450)
# ============================================================================


def test_evaluate_cluster_unhealthy():
    """Evaluate should reject unhealthy cluster."""
    nodes = (
        NodeSnapshot(
            node_id=1,
            state="failed",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
    )
    cluster = ClusterState(
        cluster_id="c1",
        nodes=nodes,
        in_flight_migrations=0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=1,
        lock_to_host=False,
    )
    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=None,
        ignore_lock=False,
        reason="test",
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=False,
        forbid_migration_during_maintenance=False,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    result = evaluate(plan, cluster, policy)

    assert result.verdict == "rejected"
    assert result.reason == "cluster_unhealthy"


def test_evaluate_too_many_concurrent():
    """Evaluate should reject if too many migrations in flight."""
    nodes = (
        NodeSnapshot(
            node_id=1,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
        NodeSnapshot(
            node_id=2,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
    )
    cluster = ClusterState(
        cluster_id="c1",
        nodes=nodes,
        in_flight_migrations=5,  # At max
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=1,
        lock_to_host=False,
    )
    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=None,
        ignore_lock=False,
        reason="test",
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=False,
        forbid_migration_during_maintenance=False,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    result = evaluate(plan, cluster, policy)

    assert result.verdict == "rejected"
    assert result.reason == "too_many_concurrent_migrations"


def test_evaluate_network_partition_forbidden():
    """Evaluate should reject during network partition if forbidden."""
    nodes = (
        NodeSnapshot(
            node_id=1,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
        NodeSnapshot(
            node_id=2,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
    )
    cluster = ClusterState(
        cluster_id="c1",
        nodes=nodes,
        in_flight_migrations=0,
        network_partition_active=True,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=1,
        lock_to_host=False,
    )
    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=None,
        ignore_lock=False,
        reason="test",
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=True,
        forbid_migration_during_maintenance=False,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    result = evaluate(plan, cluster, policy)

    assert result.verdict == "rejected"
    assert result.reason == "network_partition_active"


def test_evaluate_maintenance_window_forbidden():
    """Evaluate should reject during maintenance if forbidden."""
    nodes = (
        NodeSnapshot(
            node_id=1,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
        NodeSnapshot(
            node_id=2,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
    )
    cluster = ClusterState(
        cluster_id="c1",
        nodes=nodes,
        in_flight_migrations=0,
        maintenance_window_active=True,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=1,
        lock_to_host=False,
    )
    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=None,
        ignore_lock=False,
        reason="test",
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=False,
        forbid_migration_during_maintenance=True,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    result = evaluate(plan, cluster, policy)

    assert result.verdict == "rejected"
    assert result.reason == "maintenance_window_active"


def test_evaluate_biome_locked():
    """Evaluate should reject biome locked to host."""
    nodes = (
        NodeSnapshot(
            node_id=1,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
        NodeSnapshot(
            node_id=2,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
    )
    cluster = ClusterState(
        cluster_id="c1",
        nodes=nodes,
        in_flight_migrations=0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=1,
        lock_to_host=True,
    )
    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=None,
        ignore_lock=False,
        reason="test",
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=False,
        forbid_migration_during_maintenance=False,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    result = evaluate(plan, cluster, policy)

    assert result.verdict == "rejected"
    assert result.reason == "biome_locked_to_host"


def test_evaluate_biome_locked_ignore():
    """Evaluate should allow migration if ignore_lock=true."""
    nodes = (
        NodeSnapshot(
            node_id=1,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
        NodeSnapshot(
            node_id=2,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
    )
    cluster = ClusterState(
        cluster_id="c1",
        nodes=nodes,
        in_flight_migrations=0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=1,
        lock_to_host=True,
    )
    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=None,
        ignore_lock=True,
        reason="override",
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=2,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=False,
        forbid_migration_during_maintenance=False,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    result = evaluate(plan, cluster, policy)

    # Should progress past lock check
    assert result.reason != "biome_locked_to_host"


def test_evaluate_no_viable_candidates():
    """Evaluate should reject if no candidate nodes."""
    nodes = (
        NodeSnapshot(
            node_id=1,
            state="ready",
            cpu_free_pct=50.0,
            mem_free_pct=50.0,
            disk_free_pct=50.0,
            forecast_cpu_free_pct=50.0,
            forecast_mem_free_pct=50.0,
            forecast_disk_free_pct=50.0,
        ),
    )
    cluster = ClusterState(
        cluster_id="c1",
        nodes=nodes,
        in_flight_migrations=0,
    )
    biome = BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="vm",
        src_node_id=1,  # Same as only node
        lock_to_host=False,
    )
    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=None,
        ignore_lock=False,
        reason="test",
    )
    policy = MigrationPolicy(
        enabled=True,
        evaluation_interval_seconds=60,
        min_healthy_nodes=1,
        max_concurrent_migrations=5,
        require_target_capacity_headroom_cpu_pct=5,
        require_target_capacity_headroom_mem_pct=5,
        require_target_capacity_headroom_disk_pct=5,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=False,
        forbid_migration_during_maintenance=False,
        waddleai_risk_threshold=0.5,
        capacity_forecast_horizon_days=7,
    )

    result = evaluate(plan, cluster, policy)

    assert result.verdict == "rejected"
    assert result.reason == "no_viable_candidates"


# ============================================================================
# Serialization Tests (lines 459-466, 481, 484, 515)
# ============================================================================


def test_violations_to_dicts_complete():
    """Serialize violation with all fields."""
    violation = Violation(
        code="insufficient_cpu",
        message="CPU low",
        node_id=1,
        field="cpu",
        actual=5.0,
        required=10.0,
    )

    result = violations_to_dicts([violation])

    assert len(result) == 1
    d = result[0]
    assert d["code"] == "insufficient_cpu"
    assert d["message"] == "CPU low"
    assert d["node_id"] == 1
    assert d["field"] == "cpu"
    assert d["actual"] == 5.0
    assert d["required"] == 10.0


def test_violations_to_dicts_minimal():
    """Serialize violation with minimal fields."""
    violation = Violation(
        code="error",
        message="Error message",
    )

    result = violations_to_dicts([violation])

    assert len(result) == 1
    d = result[0]
    assert d["code"] == "error"
    assert d["message"] == "Error message"
    assert "node_id" not in d
    assert "field" not in d
    assert "actual" not in d
    assert "required" not in d


def test_safety_result_to_dict():
    """Serialize safety result."""
    result = SafetyResult(
        verdict="pass",
        reason=None,
        candidates_considered=3,
        chosen_target_node_id=2,
        violations=tuple(),
    )

    d = safety_result_to_dict(result)

    assert d["verdict"] == "pass"
    assert d["reason"] is None
    assert d["candidates_considered"] == 3
    assert d["chosen_target_node_id"] == 2
    assert d["violations"] == []
