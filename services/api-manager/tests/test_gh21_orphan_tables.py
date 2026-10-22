"""Real-Postgres insert/select coverage for the gh-21 orphan tables that are
NOT RLS-enabled (cluster_config, ipxe_machines, ipxe_images,
ipxe_boot_configs, ipxe_config, boot_events, deployment_logs,
resource_permissions).

RLS-enabled orphan tables (clusters, storage_quotas, storage_quota_requests,
biome_groups) have their own two-tenant isolation sections in
``tests/test_rls_isolation.py``; the app-level IDOR regression for clusters
lives in ``tests/api/test_clusters_pg.py``. This file exists to prove the
remaining eight tables are reachable (grants correct) and round-trip data
correctly as the real scoped ``api-manager-rw`` role -- none of them carry a
tenant_id column, so there is no isolation semantic to test here, only
grant/schema correctness.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from penguin_dal import DB


def _seed_cluster(owner_db: DB, *, cluster_id: str) -> str:
    now = datetime.now(timezone.utc)
    owner_db.clusters.insert(
        id=cluster_id, tenant_id="acme", name=cluster_id, created_at=now, updated_at=now,
    )
    return cluster_id


class TestClusterConfig:
    def test_insert_and_select_as_scoped_role(self, pg_db: DB, pg_db_scoped: DB) -> None:
        cluster_id = _seed_cluster(pg_db, cluster_id="cc-cluster")

        row_id = pg_db_scoped.cluster_config.insert(
            cluster_id=cluster_id, key="network_pools", value_json=[{"name": "mgmt"}],
        )
        rows = pg_db_scoped(pg_db_scoped.cluster_config.id == row_id).select()

        assert len(rows) == 1
        assert rows[0].cluster_id == cluster_id
        assert rows[0].key == "network_pools"
        assert rows[0].value_json == [{"name": "mgmt"}]
        assert rows[0].created_at is not None

    def test_unique_constraint_on_cluster_id_and_key(self, pg_db: DB, pg_db_scoped: DB) -> None:
        from sqlalchemy.exc import IntegrityError

        cluster_id = _seed_cluster(pg_db, cluster_id="cc-cluster-2")
        pg_db_scoped.cluster_config.insert(cluster_id=cluster_id, key="k1", value_json={})

        with pytest.raises(IntegrityError):
            pg_db_scoped.cluster_config.insert(cluster_id=cluster_id, key="k1", value_json={})


class TestIpxeImages:
    def test_insert_and_select_as_scoped_role(self, pg_db_scoped: DB) -> None:
        image_id = pg_db_scoped.ipxe_images.insert(
            name="ubuntu-24.04", display_name="Ubuntu 24.04", os_version="24.04",
            architecture="amd64", kernel_path="minio://kernel", initrd_path="minio://initrd",
        )
        rows = pg_db_scoped(pg_db_scoped.ipxe_images.id == image_id).select()

        assert len(rows) == 1
        assert rows[0].os_name == "ubuntu"
        assert rows[0].image_type == "minimal"
        assert rows[0].is_active is True
        assert rows[0].created_at is not None
        assert rows[0].updated_at is not None

    def test_delete_as_scoped_role(self, pg_db_scoped: DB) -> None:
        image_id = pg_db_scoped.ipxe_images.insert(
            name="delete-me", display_name="Delete Me", os_version="24.04",
            architecture="amd64", kernel_path="k", initrd_path="i",
        )
        pg_db_scoped(pg_db_scoped.ipxe_images.id == image_id).delete()
        rows = pg_db_scoped(pg_db_scoped.ipxe_images.id == image_id).select()
        assert len(rows) == 0


class TestIpxeBootConfigs:
    def test_insert_and_select_as_scoped_role(self, pg_db_scoped: DB) -> None:
        from app.db.rls import install_rls_events, set_current_tenant

        image_id = pg_db_scoped.ipxe_images.insert(
            name="img-for-bootcfg", display_name="Img", os_version="24.04",
            architecture="amd64", kernel_path="k", initrd_path="i",
        )
        # biome_groups is RLS-enabled; current handlers never set tenant_id
        # (server_default='__default__'), so seeding it here needs the GUC
        # set to '__default__' to satisfy the RLS WITH CHECK on INSERT --
        # see app.models_m1.BiomeGroup's docstring for why.
        install_rls_events(pg_db_scoped.engine)
        set_current_tenant("__default__")
        try:
            group_id = pg_db_scoped.biome_groups.insert(
                name="bg-for-bootcfg", display_name="BG", biomes=[],
            )
        finally:
            set_current_tenant(None)
        cfg_id = pg_db_scoped.ipxe_boot_configs.insert(
            name="default-boot", boot_order=[], default_image_id=image_id,
            assigned_biome_group_id=group_id,
        )
        rows = pg_db_scoped(pg_db_scoped.ipxe_boot_configs.id == cfg_id).select()

        assert len(rows) == 1
        assert rows[0].timeout_seconds == 30
        assert rows[0].default_image_id == image_id
        assert rows[0].assigned_biome_group_id == group_id


class TestIpxeMachines:
    def test_insert_and_select_as_scoped_role(self, pg_db_scoped: DB) -> None:
        cfg_id = pg_db_scoped.ipxe_boot_configs.insert(name="mach-boot-cfg", boot_order=[])
        machine_id = pg_db_scoped.ipxe_machines.insert(
            system_id="sys-gh21-1", mac_address="deadbeef0001", status="unknown",
            boot_config_id=cfg_id,
        )
        rows = pg_db_scoped(pg_db_scoped.ipxe_machines.id == machine_id).select()

        assert len(rows) == 1
        assert rows[0].system_id == "sys-gh21-1"
        assert rows[0].boot_config_id == cfg_id
        assert rows[0].created_at is not None

    def test_update_and_delete_as_scoped_role(self, pg_db_scoped: DB) -> None:
        machine_id = pg_db_scoped.ipxe_machines.insert(
            system_id="sys-gh21-2", mac_address="deadbeef0002", status="unknown",
        )
        pg_db_scoped(pg_db_scoped.ipxe_machines.id == machine_id).update(status="ready")
        row = pg_db_scoped(pg_db_scoped.ipxe_machines.id == machine_id).select().first()
        assert row.status == "ready"

        pg_db_scoped(pg_db_scoped.ipxe_machines.id == machine_id).delete()
        assert pg_db_scoped(pg_db_scoped.ipxe_machines.id == machine_id).select().first() is None


class TestIpxeConfig:
    def test_insert_and_select_as_scoped_role(self, pg_db_scoped: DB) -> None:
        cfg_id = pg_db_scoped.ipxe_config.insert(name="default")
        rows = pg_db_scoped(pg_db_scoped.ipxe_config.id == cfg_id).select()

        assert len(rows) == 1
        assert rows[0].dhcp_mode == "proxy"
        assert rows[0].tftp_enabled is True
        assert rows[0].is_active is True
        assert rows[0].created_at is not None


class TestBootEvents:
    def test_insert_and_select_as_scoped_role(self, pg_db_scoped: DB) -> None:
        machine_id = pg_db_scoped.ipxe_machines.insert(
            system_id="sys-gh21-be", mac_address="deadbeef0003", status="unknown",
        )
        event_id = pg_db_scoped.boot_events.insert(
            machine_id=machine_id, mac_address="deadbeef0003",
            event_type="dhcp_request", details={"action": "discover"},
        )
        rows = pg_db_scoped(pg_db_scoped.boot_events.id == event_id).select()

        assert len(rows) == 1
        assert rows[0].event_type == "dhcp_request"
        assert rows[0].details == {"action": "discover"}
        assert rows[0].created_at is not None, (
            "created_at must come from server_default=func.now() -- "
            "app.api.ipxe._log_boot_event never passes it explicitly"
        )

    def test_discovery_event_with_no_machine_id(self, pg_db_scoped: DB) -> None:
        """Discovery events fire before a machine is registered -- machine_id nullable."""
        event_id = pg_db_scoped.boot_events.insert(
            machine_id=None, mac_address="deadbeef0004",
            event_type="dhcp_request", details={},
        )
        row = pg_db_scoped(pg_db_scoped.boot_events.id == event_id).select().first()
        assert row is not None
        assert row.machine_id is None


class TestDeploymentLogs:
    def test_insert_and_select_as_scoped_role(self, pg_db: DB, pg_db_scoped: DB) -> None:
        now = datetime.now(timezone.utc)
        biome_id = int(pg_db.biomes.insert(name="probe-biome-dl", tenant_id="acme"))
        node_id = int(
            pg_db.nodes.insert(
                name="probe-node-dl", tenant_id="acme", created_at=now, updated_at=now,
            )
        )
        pg_db.deployments.insert(
            id="dep-gh21-dl", biome_id=biome_id, node_id=node_id,
            created_at=now, updated_at=now,
        )

        log_id = pg_db_scoped.deployment_logs.insert(
            deployment_id="dep-gh21-dl", message="starting deploy", level="info",
        )
        rows = pg_db_scoped(pg_db_scoped.deployment_logs.id == log_id).select()

        assert len(rows) == 1
        assert rows[0].deployment_id == "dep-gh21-dl"
        assert rows[0].level == "info"
        assert rows[0].created_at is not None


class TestResourcePermissions:
    def test_insert_and_select_as_scoped_role(self, pg_db: DB, pg_db_scoped: DB) -> None:
        user_id = int(
            pg_db.auth_user.insert(
                email="gh21-rp-probe@example.com",
                fs_uniquifier=str(uuid.uuid4()),
                active=True,
            )
        )

        rp_id = pg_db_scoped.resource_permissions.insert(
            user_id=user_id, resource_type="lxd_cluster", resource_id=1,
            permission="read,write",
        )
        rows = pg_db_scoped(pg_db_scoped.resource_permissions.id == rp_id).select()

        assert len(rows) == 1
        assert rows[0].permission == "read,write"

    def test_unique_constraint_user_resource_type_resource_id(
        self, pg_db: DB, pg_db_scoped: DB,
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        user_id = int(
            pg_db.auth_user.insert(
                email="gh21-rp-probe2@example.com",
                fs_uniquifier=str(uuid.uuid4()),
                active=True,
            )
        )
        pg_db_scoped.resource_permissions.insert(
            user_id=user_id, resource_type="lxd_cluster", resource_id=5, permission="read",
        )

        with pytest.raises(IntegrityError):
            pg_db_scoped.resource_permissions.insert(
                user_id=user_id, resource_type="lxd_cluster", resource_id=5,
                permission="write",
            )
