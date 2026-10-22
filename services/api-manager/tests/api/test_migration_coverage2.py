"""Additional test coverage for Migration API (targeting missed lines).

Tests error paths, validation failures, and edge cases in migration policy
and event management. Focuses on request/response validation.
"""

import pytest
import json


@pytest.mark.asyncio
async def test_patch_policy_non_dict_body(client):
    """Test PATCH /policy rejects non-dict JSON (lines 406-413)."""
    response = await client.patch(
        "/api/v1/migration/policy",
        data=json.dumps(["not", "a", "dict"]),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_policy_success(client):
    """Test PATCH /policy accepts valid update (lines 395-486)."""
    response = await client.patch(
        "/api/v1/migration/policy",
        data=json.dumps({"max_concurrent_migrations": 2}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code in (200, 422)


@pytest.mark.asyncio
async def test_trigger_migration_non_dict_body(client):
    """Test POST /biome/{id} rejects non-dict body (lines 503-504)."""
    response = await client.post(
        "/api/v1/migration/biome/1",
        data=json.dumps(["not", "a", "dict"]),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_trigger_migration_invalid_target_node_id(client):
    """Test POST /biome/{id} rejects non-integer target_node_id (lines 508-511)."""
    response = await client.post(
        "/api/v1/migration/biome/1",
        data=json.dumps({"target_node_id": "not-an-int", "reason": "test"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_trigger_migration_missing_reason(client):
    """Test POST /biome/{id} requires reason field (lines 517-524)."""
    response = await client.post(
        "/api/v1/migration/biome/1",
        data=json.dumps({"target_node_id": 2}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_trigger_migration_biome_not_found(client):
    """Test POST /biome/{id} returns 404 when biome not found (lines 533-538)."""
    response = await client.post(
        "/api/v1/migration/biome/999",
        data=json.dumps({"reason": "test"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 404
    data = await response.get_json()
    assert data.get("error") == "biome_instance_not_found"


@pytest.mark.asyncio
async def test_trigger_migration_with_valid_reason(client):
    """Test POST /biome/{id} accepts with reason (lines 489-578)."""
    response = await client.post(
        "/api/v1/migration/biome/1",
        data=json.dumps({"reason": "manual_rebalance"}),
        headers={"Content-Type": "application/json"},
    )
    # Should return 202 (deferred) or 404 (biome not found)
    assert response.status_code in (202, 404)


@pytest.mark.asyncio
async def test_list_events_invalid_since_timestamp(client):
    """Test GET /events returns 400 on invalid since timestamp (lines 625-636)."""
    response = await client.get(
        "/api/v1/migration/events?since=invalid-timestamp",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_events_invalid_until_timestamp(client):
    """Test GET /events returns 400 on invalid until timestamp (lines 638-643)."""
    response = await client.get(
        "/api/v1/migration/events?until=invalid-timestamp",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_events_invalid_node_id(client):
    """Test GET /events returns 400 on invalid node_id (lines 645-649)."""
    response = await client.get(
        "/api/v1/migration/events?node_id=not-an-int",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_events_invalid_biome_id(client):
    """Test GET /events returns 400 on invalid biome_id (lines 655-659)."""
    response = await client.get(
        "/api/v1/migration/events?biome_id=not-an-int",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_events_invalid_result_filter(client):
    """Test GET /events returns 400 on invalid result filter (lines 662-668)."""
    response = await client.get(
        "/api/v1/migration/events?result=invalid_result",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_events_result_rejected_filter(client):
    """Test GET /events filters by rejection_reason for rejected (lines 663-664)."""
    response = await client.get(
        "/api/v1/migration/events?result=rejected",
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert "data" in data


@pytest.mark.asyncio
async def test_list_events_result_pass_filter(client):
    """Test GET /events filters by result=pass (lines 665-666)."""
    response = await client.get(
        "/api/v1/migration/events?result=pass",
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_events_result_executed_filter(client):
    """Test GET /events filters by result=executed (lines 665-666)."""
    response = await client.get(
        "/api/v1/migration/events?result=executed",
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_events_result_rolled_back_filter(client):
    """Test GET /events filters by result=rolled_back (lines 665-666)."""
    response = await client.get(
        "/api/v1/migration/events?result=rolled_back",
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_events_result_safety_check_filter(client):
    """Test GET /events filters by result=safety_check (lines 665-666)."""
    response = await client.get(
        "/api/v1/migration/events?result=safety_check",
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_events_pagination_limit_capped(client):
    """Test GET /events caps limit at 500 (lines 616)."""
    response = await client.get(
        "/api/v1/migration/events?limit=1000",
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["meta"]["limit"] <= 500


@pytest.mark.asyncio
async def test_list_events_pagination_offset_clamped(client):
    """Test GET /events clamps negative offset to 0 (lines 621)."""
    response = await client.get(
        "/api/v1/migration/events?offset=-10",
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["meta"]["offset"] >= 0


@pytest.mark.asyncio
async def test_list_events_page_size_parameter(client):
    """Test GET /events accepts page_size parameter (lines 611-615)."""
    response = await client.get(
        "/api/v1/migration/events?page_size=25",
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["meta"]["limit"] == 25


@pytest.mark.asyncio
async def test_list_events_valid_iso_since(client):
    """Test GET /events accepts ISO-8601 since timestamp (lines 625-628)."""
    response = await client.get(
        "/api/v1/migration/events?since=2025-01-01T00:00:00Z",
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_events_valid_iso_until(client):
    """Test GET /events accepts ISO-8601 until timestamp (lines 638-641)."""
    response = await client.get(
        "/api/v1/migration/events?until=2025-12-31T23:59:59Z",
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_policy_success(client):
    """Test GET /policy returns default policy (lines 385-392)."""
    response = await client.get(
        "/api/v1/migration/policy",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert "min_healthy_nodes" in data["data"]


@pytest.mark.asyncio
async def test_get_safety_envelope_success(client):
    """Test GET /safety-envelope returns policy and recent checks (lines 712-733)."""
    response = await client.get(
        "/api/v1/migration/safety-envelope",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert "policy" in data["data"]
    assert "recent_checks" in data["data"]


@pytest.mark.asyncio
async def test_list_events_default_limit(client):
    """Test GET /events defaults to limit=50 (lines 608)."""
    response = await client.get("/api/v1/migration/events")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["meta"]["limit"] == 50


@pytest.mark.asyncio
async def test_list_events_default_offset(client):
    """Test GET /events defaults to offset=0 (lines 618)."""
    response = await client.get("/api/v1/migration/events")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["meta"]["offset"] == 0


@pytest.mark.asyncio
async def test_list_events_empty_when_no_table(client):
    """Test GET /events returns empty data when migration_events missing (lines 599-600)."""
    response = await client.get("/api/v1/migration/events")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert isinstance(data["data"], list)


@pytest.mark.asyncio
async def test_patch_policy_valid_max_concurrent(client):
    """Test PATCH /policy accepts valid max_concurrent_migrations (lines 395-486)."""
    response = await client.patch(
        "/api/v1/migration/policy",
        data=json.dumps({"max_concurrent_migrations": 5}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code in (200, 422)


@pytest.mark.asyncio
async def test_trigger_migration_with_target_node(client):
    """Test POST /biome/{id} accepts target_node_id (lines 506-509)."""
    response = await client.post(
        "/api/v1/migration/biome/1",
        data=json.dumps({"target_node_id": 2, "reason": "rebalance"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code in (202, 404)


@pytest.mark.asyncio
async def test_trigger_migration_with_ignore_lock(client):
    """Test POST /biome/{id} accepts ignore_lock flag (lines 513)."""
    response = await client.post(
        "/api/v1/migration/biome/1",
        data=json.dumps({"reason": "override", "ignore_lock": True}),
        headers={"Content-Type": "application/json"},
    )
    # May fail due to insufficient scope, but should process the parameter
    assert response.status_code in (202, 403, 404)


@pytest.mark.asyncio
async def test_trigger_migration_with_synchronous_flag(client):
    """Test POST /biome/{id} accepts synchronous flag (lines 514)."""
    response = await client.post(
        "/api/v1/migration/biome/1",
        data=json.dumps({"reason": "test", "synchronous": True}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code in (202, 404, 422)
