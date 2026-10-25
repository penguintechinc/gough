"""Tests for migration_engine async execution functions (M2 live migration).

Full flag-ON lifecycle (drain -> move -> verify -> executed/rolled_back)
with LXD/NATS mocked lives in
``tests/workers/test_migration_engine_live_execution.py`` -- this file
covers the two entry points' top-level contracts in isolation (no Quart
app context, no DB wiring), which is exactly the environment these
functions must degrade gracefully in rather than crash.
"""

from __future__ import annotations

import pytest
from app.workers.migration_engine import evaluate_safety, execute_migration


class TestEvaluateSafety:
    """Tests for evaluate_safety() async function."""

    @pytest.mark.asyncio
    async def test_evaluate_safety_returns_dict(self) -> None:
        """evaluate_safety() returns a dict with safe, note, violations keys."""
        result = await evaluate_safety(node_id=1)
        assert isinstance(result, dict)
        assert "safe" in result
        assert "note" in result
        assert "violations" in result

    @pytest.mark.asyncio
    async def test_evaluate_safety_fails_closed_without_app_context(self) -> None:
        """No Quart app context (no DB reachable) -> fails closed, not a stub.

        Real implementation: it tries to load live cluster/node state and,
        unable to (no app context here to resolve a DB connection through),
        reports safe=False with the load failure as the reason -- never a
        silent default-true, and never an unhandled crash either.
        """
        result = await evaluate_safety(node_id=1)
        assert result["safe"] is False
        assert "Failed to load" in result["note"]
        assert isinstance(result["violations"], list)


class TestExecuteMigration:
    """Tests for execute_migration() async function."""

    @pytest.mark.asyncio
    async def test_execute_migration_returns_dict(self) -> None:
        """execute_migration() returns a dict with status, duration_ms, verdict."""
        result = await execute_migration(
            biome_instance_id=1,
            src_node_id=1,
            dst_node_id=2,
            live=True,
            reason="test migration",
        )
        assert isinstance(result, dict)
        assert "status" in result
        assert "duration_ms" in result
        assert "verdict" in result

    @pytest.mark.asyncio
    async def test_execute_migration_disabled_by_default(self) -> None:
        """FLAG_LIVE_MIGRATION defaults OFF -- execute_migration short-circuits
        before touching DB/LXD/NATS, every environment, until a license/flag
        server explicitly turns it on (critical-rules.md Feature Flags)."""
        result = await execute_migration(
            biome_instance_id=1,
            src_node_id=1,
            dst_node_id=2,
            live=False,
            reason="test",
        )
        assert result["status"] == "disabled"
        assert result["verdict"] == "feature_flag_disabled"
        assert result["duration_ms"] == 0

    @pytest.mark.asyncio
    async def test_execute_migration_live_flag(self) -> None:
        """execute_migration() accepts live flag (True for lxc move --live)."""
        result_live = await execute_migration(
            biome_instance_id=1,
            src_node_id=1,
            dst_node_id=2,
            live=True,
            reason="live test",
        )
        result_stop = await execute_migration(
            biome_instance_id=2,
            src_node_id=1,
            dst_node_id=2,
            live=False,
            reason="stop/move test",
        )
        # Both should return valid result dicts
        assert "status" in result_live
        assert "status" in result_stop
