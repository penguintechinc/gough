"""Tests for ``app.api.disks`` — every endpoint × success + error paths.

Coverage targets ≥90% on the disks module: list / plan (commit + dry-run +
422 collisions / capacity / validation) / patch (toggle, tier, validation) /
smart-recheck (NATS path + queue fallback).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

# All tests use the disks_app fixture which already monkey-patches auth.


@pytest.fixture()
def client(disks_app):
    return disks_app.test_client()


# ---------------------------------------------------------------------------
# Pydantic-level model validation (cheap, exercises every validator branch)
# ---------------------------------------------------------------------------


def test_partition_spec_rejects_unknown_fs(disks_app):
    from app.api.disks import PartitionSpec

    with pytest.raises(Exception):
        PartitionSpec(index=1, mount_point="/", size_mb=100, fs_type="reiser")


def test_partition_spec_rejects_unknown_encryption(disks_app):
    from app.api.disks import PartitionSpec

    with pytest.raises(Exception):
        PartitionSpec(
            index=1, mount_point="/", size_mb=100, fs_type="ext4", encryption="aes"
        )


def test_partition_spec_rejects_relative_mount(disks_app):
    from app.api.disks import PartitionSpec

    with pytest.raises(Exception):
        PartitionSpec(index=1, mount_point="boot", size_mb=100, fs_type="ext4")


def test_partition_spec_accepts_swap_mount(disks_app):
    from app.api.disks import PartitionSpec

    p = PartitionSpec(index=1, mount_point="swap", size_mb=512, fs_type="swap")
    assert p.mount_point == "swap"


def test_disk_patch_request_rejects_bad_tier(disks_app):
    from app.api.disks import DiskPatchRequest

    with pytest.raises(Exception):
        DiskPatchRequest(tier="hyper")


# ---------------------------------------------------------------------------
# render_sgdisk_script + collision detection helpers
# ---------------------------------------------------------------------------


def test_render_sgdisk_script_emits_zap_then_partitions(disks_app):
    from app.api.disks import PartitionSpec, render_sgdisk_script

    fake_disk = SimpleNamespace(device_path="/dev/sdz")
    parts = [
        PartitionSpec(index=1, mount_point="/", size_mb=10240, fs_type="ext4"),
        PartitionSpec(index=2, mount_point="swap", size_mb=2048, fs_type="swap"),
    ]
    script = render_sgdisk_script(fake_disk, parts)
    assert "sgdisk --zap-all /dev/sdz" in script
    # Order is partition-index ascending.
    idx_p1 = script.index("--new=1:")
    idx_p2 = script.index("--new=2:")
    assert idx_p1 < idx_p2
    assert "8200" in script  # swap typecode
    assert "8300" in script  # linux fs typecode
    assert script.endswith("\n")


def test_render_sgdisk_script_includes_luks_marker(disks_app):
    from app.api.disks import PartitionSpec, render_sgdisk_script

    fake_disk = SimpleNamespace(device_path="/dev/sda")
    parts = [
        PartitionSpec(
            index=1, mount_point="/", size_mb=1024, fs_type="ext4", encryption="luks2"
        )
    ]
    out = render_sgdisk_script(fake_disk, parts)
    assert "encryption=luks2" in out
    assert "/dev/sda1" in out


def test_detect_write_files_collision_detects_internal_dup(dal, seed_node):
    from app.api.disks import detect_write_files_collision

    found = detect_write_files_collision(
        dal, seed_node.node_id, seed_node.sda, ["/", "/", "/var"], "acme"
    )
    assert found == "/"


def test_detect_write_files_collision_skips_swap(dal, seed_node):
    from app.api.disks import detect_write_files_collision

    found = detect_write_files_collision(
        dal, seed_node.node_id, seed_node.sda, ["swap", "swap"], "acme"
    )
    assert found is None


def test_detect_write_files_collision_against_other_disk_plan(dal, seed_node):
    from app.api.disks import detect_write_files_collision
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    dal.disk_plans.insert(
        node_id=seed_node.node_id,
        disk_id=seed_node.sdb,
        tenant_id="acme",
        partition_index=1,
        mount_point="/data",
        size_bytes=1024 * 1024 * 1024,
        fs_type="xfs",
        encryption="none",
        created_at=now,
        updated_at=now,
    )
    dal.commit()

    found = detect_write_files_collision(
        dal, seed_node.node_id, seed_node.sda, ["/data"], "acme"
    )
    assert found == "/data"


# ---------------------------------------------------------------------------
# GET /nodes/{id}/disks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_disks_returns_seeded_two(client, seed_node):
    resp = await client.get(f"/api/v1/nodes/{seed_node.node_id}/disks")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["status"] == "success"
    assert body["meta"]["count"] == 2
    devices = sorted(d["device_path"] for d in body["data"]["disks"])
    assert devices == ["/dev/sda", "/dev/sdb"]
    # tier + reserved_for_storage surfaced
    sda = next(d for d in body["data"]["disks"] if d["device_path"] == "/dev/sda")
    assert sda["tier"] == "fast"
    assert sda["reserved_for_storage"] is False
    assert sda["smart_status"] == "passed"


@pytest.mark.asyncio
async def test_list_disks_404_for_unknown_node(client):
    resp = await client.get("/api/v1/nodes/9999/disks")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /nodes/{id}/disks/{disk_id}/plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_dry_run_returns_script_no_rows_written(client, seed_node, dal):
    body = {
        "partitions": [
            {"index": 1, "mount_point": "/", "size_mb": 8192, "fs_type": "ext4"},
            {
                "index": 2,
                "mount_point": "swap",
                "size_mb": 1024,
                "fs_type": "swap",
                "encryption": "none",
            },
        ]
    }
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan?dry-run=true",
        json=body,
    )
    assert resp.status_code == 200
    payload = await resp.get_json()
    assert payload["data"]["dry_run"] is True
    assert "sgdisk --zap-all /dev/sda" in payload["data"]["sgdisk_script"]
    # Verify no disk plans were written in dry-run mode
    # Use a truthy column filter to count all rows
    from sqlalchemy import literal
    assert dal(dal.disk_plans.id != literal(None)).count() == 0


@pytest.mark.asyncio
async def test_plan_commit_writes_rows(client, seed_node, dal):
    body = {
        "partitions": [
            {"index": 1, "mount_point": "/", "size_mb": 8192, "fs_type": "ext4"},
            {"index": 2, "mount_point": "/var", "size_mb": 4096, "fs_type": "xfs"},
        ]
    }
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan", json=body
    )
    assert resp.status_code == 201
    payload = await resp.get_json()
    assert payload["data"]["dry_run"] is False
    assert len(payload["data"]["plans"]) == 2
    assert dal(dal.disk_plans.disk_id == seed_node.sda).count() == 2


@pytest.mark.asyncio
async def test_plan_replan_replaces_previous(client, seed_node, dal):
    body1 = {"partitions": [{"index": 1, "mount_point": "/", "size_mb": 100, "fs_type": "ext4"}]}
    body2 = {"partitions": [{"index": 1, "mount_point": "/srv", "size_mb": 200, "fs_type": "xfs"}]}
    r1 = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan", json=body1
    )
    assert r1.status_code == 201
    r2 = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan", json=body2
    )
    assert r2.status_code == 201
    rows = dal(dal.disk_plans.disk_id == seed_node.sda).select()
    assert [r.mount_point for r in rows] == ["/srv"]


@pytest.mark.asyncio
async def test_plan_rejects_collision_with_other_disk(client, seed_node, dal):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    dal.disk_plans.insert(
        node_id=seed_node.node_id, disk_id=seed_node.sdb, tenant_id="acme",
        partition_index=1, mount_point="/data", size_bytes=1, fs_type="xfs",
        encryption="none", created_at=now, updated_at=now,
    )
    dal.commit()

    body = {"partitions": [{"index": 1, "mount_point": "/data", "size_mb": 100, "fs_type": "ext4"}]}
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan", json=body
    )
    assert resp.status_code == 422
    j = await resp.get_json()
    assert j["error"] == "write_files_path_collision"
    assert j["path"] == "/data"


@pytest.mark.asyncio
async def test_plan_rejects_duplicate_partition_indexes(client, seed_node):
    body = {
        "partitions": [
            {"index": 1, "mount_point": "/", "size_mb": 100, "fs_type": "ext4"},
            {"index": 1, "mount_point": "/var", "size_mb": 100, "fs_type": "ext4"},
        ]
    }
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan", json=body
    )
    assert resp.status_code == 422
    j = await resp.get_json()
    assert j["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_plan_rejects_oversize_partitions(client, seed_node):
    # disk capacity is 512 GB; plan asks for 1 TB
    body = {
        "partitions": [
            {"index": 1, "mount_point": "/", "size_mb": 1024 * 1024, "fs_type": "ext4"}
        ]
    }
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan", json=body
    )
    assert resp.status_code == 422
    j = await resp.get_json()
    assert "exceeds disk capacity" in j["details"]


@pytest.mark.asyncio
async def test_plan_404_unknown_disk(client, seed_node):
    body = {"partitions": [{"index": 1, "mount_point": "/", "size_mb": 1, "fs_type": "ext4"}]}
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/9999/plan", json=body
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plan_400_missing_body(client, seed_node):
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan",
        data="",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plan_422_validation_error(client, seed_node):
    body = {"partitions": [{"index": 0, "mount_point": "/", "size_mb": 1, "fs_type": "ext4"}]}
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/plan", json=body
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /nodes/{id}/disks/{disk_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_dark_drive_toggle(client, seed_node, dal):
    resp = await client.patch(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}",
        json={"reserved_for_storage": True},
    )
    assert resp.status_code == 200
    j = await resp.get_json()
    assert j["data"]["reserved_for_storage"] is True
    row = dal(dal.disks.id == seed_node.sda).select().first()
    assert row.reserved_for_storage is True


@pytest.mark.asyncio
async def test_patch_tier_classification(client, seed_node, dal):
    resp = await client.patch(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sdb}",
        json={"tier": "fast"},
    )
    assert resp.status_code == 200
    j = await resp.get_json()
    assert j["data"]["tier"] == "fast"


@pytest.mark.asyncio
async def test_patch_storage_backend_assignment(client, seed_node, dal):
    resp = await client.patch(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}",
        json={"storage_backend": "nest"},
    )
    assert resp.status_code == 200
    j = await resp.get_json()
    assert j["data"]["storage_backend"] == "nest"


@pytest.mark.asyncio
async def test_patch_empty_body_422(client, seed_node):
    resp = await client.patch(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}", json={}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_invalid_tier_422(client, seed_node):
    resp = await client.patch(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}",
        json={"tier": "hyper"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_unknown_disk_404(client, seed_node):
    resp = await client.patch(
        f"/api/v1/nodes/{seed_node.node_id}/disks/9999",
        json={"tier": "fast"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_missing_body_400(client, seed_node):
    resp = await client.patch(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}",
        data="",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /nodes/{id}/disks/{disk_id}/smart-recheck
# ---------------------------------------------------------------------------


class _FakeNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode("utf-8"))))


@pytest.mark.asyncio
async def test_smart_recheck_publishes_to_nats(client, disks_app, seed_node):
    fake = _FakeNats()
    disks_app.nats_client = fake
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/smart-recheck"
    )
    assert resp.status_code == 202
    j = await resp.get_json()
    assert j["data"]["dispatch"] == "nats"
    assert fake.published
    subject, payload = fake.published[0]
    assert subject == "gough.smart.recheck"
    assert payload["node_id"] == seed_node.node_id
    assert payload["disk_id"] == seed_node.sda


@pytest.mark.asyncio
async def test_smart_recheck_falls_back_to_queue(client, disks_app, seed_node, dal):
    # No nats client attached → queue path.
    if hasattr(disks_app, "nats_client"):
        del disks_app.nats_client
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sdb}/smart-recheck"
    )
    assert resp.status_code == 202
    j = await resp.get_json()
    assert j["data"]["dispatch"] == "queue"
    assert "smart_recheck_queue" in dal.tables
    rows = dal(dal.smart_recheck_queue.disk_id == seed_node.sdb).select()
    assert len(rows) == 1
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_smart_recheck_404_unknown_disk(client, seed_node):
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/9999/smart-recheck"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_smart_recheck_falls_back_when_nats_publish_raises(
    client, disks_app, seed_node, dal
):
    class BoomNats:
        def publish(self, subject, payload):  # noqa: D401
            raise RuntimeError("nats down")

    disks_app.nats_client = BoomNats()
    resp = await client.post(
        f"/api/v1/nodes/{seed_node.node_id}/disks/{seed_node.sda}/smart-recheck"
    )
    assert resp.status_code == 202
    j = await resp.get_json()
    assert j["data"]["dispatch"] == "queue"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_in_other_tenant_is_invisible(client, dal, seed_node):
    """A disk on a different tenant must not be returnable via this tenant's
    routes — even if the URL guesses the disk_id correctly."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    other_node = dal.nodes.insert(
        tenant_id="rival", name="rival-1", state="ready",
        dmi_uuid="11111111-1111-1111-1111-111111111111",
        primary_nic_mac="aa:bb:cc:dd:ee:99",
    )
    other_disk = dal.disks.insert(
        node_id=other_node, tenant_id="rival", device_path="/dev/sda",
        capacity_bytes=1, smart_status="passed",
        created_at=now, updated_at=now,
    )
    dal.commit()
    resp = await client.patch(
        f"/api/v1/nodes/{int(other_node)}/disks/{int(other_disk)}",
        json={"tier": "fast"},
    )
    assert resp.status_code == 404
