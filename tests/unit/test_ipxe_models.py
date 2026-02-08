"""Unit tests for iPXE database models.

Tests:
- iPXE configuration table structure and constraints
- Machine inventory table structure and constraints
- Egg/deployable package table structure and constraints
- Boot image table structure and constraints
- Boot configuration table structure and constraints
- Deployment job table structure and constraints
- Boot event logging table structure and constraints
"""

import json
from datetime import datetime

import pytest


class TestIPXEConfig:
    """Tests for iPXE configuration table."""

    def test_create_ipxe_config(self, db):
        """Test creating an iPXE configuration."""
        config_id = db.ipxe_config.insert(
            name="prod-ipxe",
            dhcp_mode="proxy",
            dhcp_interface="eth0",
            dhcp_subnet="10.0.0.0/24",
            dhcp_range_start="10.0.0.100",
            dhcp_range_end="10.0.0.200",
            dhcp_gateway="10.0.0.1",
            dns_servers=json.dumps(["8.8.8.8", "8.8.4.4"]),
            tftp_enabled=True,
            http_boot_url="http://boot.local:8080",
            is_active=True
        )
        db.commit()

        config = db.ipxe_config(config_id)
        assert config is not None
        assert config.name == "prod-ipxe"
        assert config.dhcp_mode == "proxy"
        assert config.dhcp_interface == "eth0"
        assert config.tftp_enabled is True
        assert config.is_active is True

    def test_ipxe_config_unique_name(self, db):
        """Test that iPXE config names are unique."""
        db.ipxe_config.insert(
            name="ipxe-1",
            dhcp_mode="proxy",
            dhcp_interface="eth0"
        )
        db.commit()

        with pytest.raises(Exception):  # Constraint violation
            db.ipxe_config.insert(
                name="ipxe-1",
                dhcp_mode="full",
                dhcp_interface="eth1"
            )
            db.commit()

    def test_ipxe_config_dhcp_modes(self, db):
        """Test valid DHCP modes."""
        valid_modes = ["full", "proxy", "disabled"]

        for mode in valid_modes:
            config_id = db.ipxe_config.insert(
                name=f"ipxe-{mode}",
                dhcp_mode=mode,
                dhcp_interface="eth0"
            )
            db.commit()
            assert db.ipxe_config(config_id).dhcp_mode == mode

    def test_ipxe_config_timestamps(self, db):
        """Test that timestamps are automatically set."""
        config_id = db.ipxe_config.insert(
            name="ipxe-timestamps",
            dhcp_mode="proxy",
            dhcp_interface="eth0"
        )
        db.commit()

        config = db.ipxe_config(config_id)
        assert config.created_at is not None
        assert config.updated_at is not None
        assert isinstance(config.created_at, datetime)


class TestIPXEMachine:
    """Tests for iPXE machine inventory table."""

    def test_create_machine(self, db):
        """Test creating a machine record."""
        machine_id = db.ipxe_machines.insert(
            system_id="node-001",
            hostname="server-01.local",
            mac_address="00:1a:2b:3c:4d:5e",
            ip_address="10.0.0.100",
            status="discovered",
            boot_mode="uefi",
            architecture="amd64",
            cpu_count=8,
            memory_mb=16384,
            storage_gb=500,
            power_type="ipmi",
            zone="zone-a",
            pool="default"
        )
        db.commit()

        machine = db.ipxe_machines(machine_id)
        assert machine.system_id == "node-001"
        assert machine.hostname == "server-01.local"
        assert machine.mac_address == "00:1a:2b:3c:4d:5e"
        assert machine.status == "discovered"
        assert machine.cpu_count == 8
        assert machine.memory_mb == 16384

    def test_machine_unique_constraints(self, db):
        """Test unique constraints on system_id and mac_address."""
        db.ipxe_machines.insert(
            system_id="node-001",
            mac_address="00:1a:2b:3c:4d:5e"
        )
        db.commit()

        # Duplicate system_id
        with pytest.raises(Exception):
            db.ipxe_machines.insert(
                system_id="node-001",
                mac_address="00:1a:2b:3c:4d:5f"
            )
            db.commit()

    def test_machine_statuses(self, db):
        """Test valid machine statuses."""
        statuses = ["unknown", "discovered", "commissioning", "ready",
                   "deploying", "deployed", "failed"]

        for i, status in enumerate(statuses):
            machine_id = db.ipxe_machines.insert(
                system_id=f"node-{i}",
                mac_address=f"00:1a:2b:3c:4d:{i:02x}",
                status=status
            )
            db.commit()
            assert db.ipxe_machines(machine_id).status == status

    def test_machine_boot_modes(self, db):
        """Test valid boot modes."""
        modes = ["bios", "uefi", "uefi_http"]

        for i, mode in enumerate(modes):
            machine_id = db.ipxe_machines.insert(
                system_id=f"node-mode-{i}",
                mac_address=f"00:2a:2b:3c:4d:{i:02x}",
                boot_mode=mode
            )
            db.commit()
            assert db.ipxe_machines(machine_id).boot_mode == mode

    def test_machine_tags_storage(self, db):
        """Test storing tags as JSON."""
        tags = ["hypervisor", "storage-node", "prod"]
        machine_id = db.ipxe_machines.insert(
            system_id="tagged-node",
            mac_address="00:3a:2b:3c:4d:5e",
            tags=json.dumps(tags)
        )
        db.commit()

        machine = db.ipxe_machines(machine_id)
        stored_tags = json.loads(machine.tags) if machine.tags else []
        assert stored_tags == tags


class TestEgg:
    """Tests for deployable eggs table."""

    def test_create_snap_egg(self, db):
        """Test creating a snap-type egg."""
        egg_id = db.eggs.insert(
            name="postgresql",
            display_name="PostgreSQL Database",
            description="PostgreSQL database snap",
            egg_type="snap",
            version="15.0",
            category="database",
            snap_name="postgresql",
            snap_channel="stable",
            snap_classic=False,
            is_active=True,
            required_architecture="amd64"
        )
        db.commit()

        egg = db.eggs(egg_id)
        assert egg.name == "postgresql"
        assert egg.egg_type == "snap"
        assert egg.snap_name == "postgresql"
        assert egg.snap_channel == "stable"

    def test_create_cloud_init_egg(self, db):
        """Test creating a cloud-init egg."""
        cloud_init = """#cloud-config
packages:
  - curl
  - git
runcmd:
  - echo "Setup complete"
"""
        egg_id = db.eggs.insert(
            name="base-ubuntu",
            display_name="Base Ubuntu",
            egg_type="cloud_init",
            cloud_init_content=cloud_init,
            is_active=True
        )
        db.commit()

        egg = db.eggs(egg_id)
        assert egg.egg_type == "cloud_init"
        assert "packages:" in egg.cloud_init_content
        assert "curl" in egg.cloud_init_content

    def test_create_lxd_container_egg(self, db):
        """Test creating an LXD container egg."""
        egg_id = db.eggs.insert(
            name="ubuntu-container",
            display_name="Ubuntu Container",
            egg_type="lxd_container",
            lxd_image_alias="ubuntu-24.04",
            lxd_image_url="images://ubuntu/24.04/cloud",
            lxd_profiles=json.dumps(["default", "network"]),
            is_active=True
        )
        db.commit()

        egg = db.eggs(egg_id)
        assert egg.egg_type == "lxd_container"
        assert egg.lxd_image_alias == "ubuntu-24.04"
        profiles = json.loads(egg.lxd_profiles)
        assert "default" in profiles

    def test_egg_unique_name(self, db):
        """Test that egg names are unique."""
        db.eggs.insert(
            name="unique-egg",
            display_name="Unique Egg",
            egg_type="snap"
        )
        db.commit()

        with pytest.raises(Exception):
            db.eggs.insert(
                name="unique-egg",
                display_name="Another Egg",
                egg_type="snap"
            )
            db.commit()

    def test_egg_dependencies(self, db):
        """Test storing egg dependencies."""
        egg1_id = db.eggs.insert(
            name="base",
            display_name="Base",
            egg_type="snap"
        )
        db.commit()

        egg2_id = db.eggs.insert(
            name="app",
            display_name="App",
            egg_type="snap",
            dependencies=json.dumps([egg1_id])
        )
        db.commit()

        egg = db.eggs(egg2_id)
        deps = json.loads(egg.dependencies) if egg.dependencies else []
        assert egg1_id in deps

    def test_egg_resource_requirements(self, db):
        """Test storing resource requirements."""
        egg_id = db.eggs.insert(
            name="heavy-app",
            display_name="Heavy App",
            egg_type="snap",
            min_ram_mb=4096,
            min_disk_gb=100,
            required_architecture="arm64"
        )
        db.commit()

        egg = db.eggs(egg_id)
        assert egg.min_ram_mb == 4096
        assert egg.min_disk_gb == 100
        assert egg.required_architecture == "arm64"


class TestIPXEImage:
    """Tests for boot image table."""

    def test_create_image(self, db):
        """Test creating a boot image."""
        image_id = db.ipxe_images.insert(
            name="ubuntu-24.04",
            display_name="Ubuntu 24.04 LTS",
            os_name="ubuntu",
            os_version="24.04",
            architecture="amd64",
            kernel_path="/images/ubuntu-24.04/kernel",
            initrd_path="/images/ubuntu-24.04/initrd.gz",
            squashfs_path="/images/ubuntu-24.04/filesystem.squashfs",
            kernel_params="ro quiet splash vga=normal",
            image_type="minimal",
            is_default=True,
            is_active=True,
            checksum="sha256:abc123def456",
            size_bytes=700000000
        )
        db.commit()

        image = db.ipxe_images(image_id)
        assert image.name == "ubuntu-24.04"
        assert image.os_name == "ubuntu"
        assert image.os_version == "24.04"
        assert image.architecture == "amd64"
        assert image.size_bytes == 700000000

    def test_image_types(self, db):
        """Test valid image types."""
        types = ["live", "install", "minimal"]

        for i, img_type in enumerate(types):
            image_id = db.ipxe_images.insert(
                name=f"image-{img_type}",
                display_name=f"Image {img_type}",
                os_name="ubuntu",
                image_type=img_type
            )
            db.commit()
            assert db.ipxe_images(image_id).image_type == img_type

    def test_image_architecture(self, db):
        """Test valid architectures for images."""
        archs = ["amd64", "arm64"]

        for arch in archs:
            image_id = db.ipxe_images.insert(
                name=f"ubuntu-{arch}",
                display_name=f"Ubuntu {arch}",
                os_name="ubuntu",
                architecture=arch
            )
            db.commit()
            assert db.ipxe_images(image_id).architecture == arch


class TestBootConfig:
    """Tests for boot configuration table."""

    def test_create_boot_config(self, db, test_image):
        """Test creating a boot configuration."""
        config_id = db.ipxe_boot_configs.insert(
            name="standard-boot",
            description="Standard boot configuration",
            ipxe_script="#!ipxe\necho Booting...",
            kernel_params="ro quiet splash",
            boot_order=json.dumps(["pxe", "disk"]),
            timeout_seconds=30,
            default_image_id=test_image.id,
            is_default=True
        )
        db.commit()

        config = db.ipxe_boot_configs(config_id)
        assert config.name == "standard-boot"
        assert config.timeout_seconds == 30
        assert config.default_image_id == test_image.id
        boot_order = json.loads(config.boot_order)
        assert "pxe" in boot_order

    def test_boot_config_unique_name(self, db, test_image):
        """Test that boot config names are unique."""
        db.ipxe_boot_configs.insert(
            name="config-1",
            default_image_id=test_image.id
        )
        db.commit()

        with pytest.raises(Exception):
            db.ipxe_boot_configs.insert(
                name="config-1",
                default_image_id=test_image.id
            )
            db.commit()


class TestDeploymentJob:
    """Tests for deployment job tracking table."""

    def test_create_deployment_job(self, db, test_machine, test_image):
        """Test creating a deployment job."""
        job_id = db.deployment_jobs.insert(
            job_id="deploy-001",
            machine_id=test_machine.id,
            image_id=test_image.id,
            eggs_to_deploy=json.dumps([1, 2]),
            status="pending",
            progress_percent=0
        )
        db.commit()

        job = db.deployment_jobs(job_id)
        assert job.job_id == "deploy-001"
        assert job.machine_id == test_machine.id
        assert job.status == "pending"
        assert job.progress_percent == 0

    def test_deployment_job_statuses(self, db, test_machine, test_image):
        """Test valid deployment statuses."""
        statuses = ["pending", "power_on", "pxe_boot", "os_install",
                   "egg_deploy", "complete", "failed"]

        for i, status in enumerate(statuses):
            job_id = db.deployment_jobs.insert(
                job_id=f"deploy-{i}",
                machine_id=test_machine.id,
                image_id=test_image.id,
                status=status
            )
            db.commit()
            assert db.deployment_jobs(job_id).status == status

    def test_deployment_job_progress_tracking(self, db, test_machine, test_image):
        """Test tracking deployment progress."""
        job_id = db.deployment_jobs.insert(
            job_id="deploy-progress",
            machine_id=test_machine.id,
            image_id=test_image.id,
            status="os_install",
            progress_percent=45
        )
        db.commit()

        # Update progress
        db(db.deployment_jobs.id == job_id).update(progress_percent=75)
        db.commit()

        job = db.deployment_jobs(job_id)
        assert job.progress_percent == 75


class TestBootEvent:
    """Tests for boot event logging table."""

    def test_create_boot_event(self, db, test_machine):
        """Test creating a boot event."""
        event_id = db.boot_events.insert(
            machine_id=test_machine.id,
            mac_address="00:1a:2b:3c:4d:5e",
            ip_address="10.0.0.100",
            event_type="dhcp_request",
            details=json.dumps({"dhcp_server": "10.0.0.1"}),
            status="success"
        )
        db.commit()

        event = db.boot_events(event_id)
        assert event.machine_id == test_machine.id
        assert event.event_type == "dhcp_request"
        assert event.status == "success"

    def test_boot_event_types(self, db, test_machine):
        """Test valid boot event types."""
        types = ["dhcp_request", "tftp_request", "boot_start",
                "os_installed", "egg_started", "egg_complete",
                "deployment_complete", "error"]

        for i, event_type in enumerate(types):
            event_id = db.boot_events.insert(
                machine_id=test_machine.id,
                mac_address=f"00:1a:2b:3c:4d:{i:02x}",
                event_type=event_type
            )
            db.commit()
            assert db.boot_events(event_id).event_type == event_type

    def test_boot_event_details_storage(self, db, test_machine):
        """Test storing detailed event information."""
        details = {
            "dhcp_server": "10.0.0.1",
            "offered_ip": "10.0.0.100",
            "transaction_id": "0x12345678"
        }
        event_id = db.boot_events.insert(
            machine_id=test_machine.id,
            mac_address="00:1a:2b:3c:4d:5e",
            event_type="dhcp_request",
            details=json.dumps(details)
        )
        db.commit()

        event = db.boot_events(event_id)
        stored_details = json.loads(event.details) if event.details else {}
        assert stored_details["dhcp_server"] == "10.0.0.1"


class TestEggGroup:
    """Tests for egg grouping table."""

    def test_create_egg_group(self, db, test_egg):
        """Test creating an egg group."""
        group_id = db.egg_groups.insert(
            name="hypervisor-stack",
            display_name="Hypervisor Stack",
            description="Complete hypervisor deployment",
            eggs=json.dumps([{"egg_id": test_egg.id, "order": 1}]),
            is_default=False
        )
        db.commit()

        group = db.egg_groups(group_id)
        assert group.name == "hypervisor-stack"
        eggs = json.loads(group.eggs)
        assert len(eggs) == 1
        assert eggs[0]["egg_id"] == test_egg.id

    def test_egg_group_multiple_eggs(self, db):
        """Test egg group with multiple eggs."""
        egg_ids = []
        for i in range(3):
            egg_id = db.eggs.insert(
                name=f"app-{i}",
                display_name=f"App {i}",
                egg_type="snap"
            )
            egg_ids.append(egg_id)
        db.commit()

        eggs_data = [
            {"egg_id": eid, "order": i+1}
            for i, eid in enumerate(egg_ids)
        ]
        group_id = db.egg_groups.insert(
            name="multi-app",
            display_name="Multi App",
            eggs=json.dumps(eggs_data)
        )
        db.commit()

        group = db.egg_groups(group_id)
        eggs = json.loads(group.eggs)
        assert len(eggs) == 3
        assert all(e["egg_id"] in egg_ids for e in eggs)
