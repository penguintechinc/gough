"""Coverage improvement tests for app/clouds/__init__.py and app/clouds/base.py.

Tests for missed coverage lines focusing on:
- Cloud provider registration and factory
- Provider availability handling
- Error cases and edge conditions
- Machine state and spec dataclasses
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from app.clouds import (
    register_cloud,
    get_cloud_provider,
    list_available_providers,
    CLOUD_REGISTRY,
    CloudError,
)
from app.clouds.base import (
    BaseCloud,
    Machine,
    MachineSpec,
    MachineState,
    CloudAuthError,
    CloudNotFoundError,
    CloudQuotaError,
)


# ============================================================================
# Cloud Provider Registration Tests (lines 32-76)
# ============================================================================


class TestRegisterCloud:
    """Tests for register_cloud() decorator - lines 32-37."""

    def test_register_cloud_decorator(self):
        """Register a cloud provider using decorator."""
        @register_cloud("test_provider")
        class TestCloud(BaseCloud):
            """Test cloud provider."""
            pass

        assert "test_provider" in CLOUD_REGISTRY
        assert CLOUD_REGISTRY["test_provider"] == TestCloud

    def test_register_cloud_returns_class(self):
        """Decorator returns the class unmodified."""
        @register_cloud("test_provider2")
        class TestCloud2(BaseCloud):
            """Test cloud provider."""
            def authenticate(self):
                return True
            def list_machines(self, filters=None):
                return []
            def get_machine(self, machine_id):
                return None
            def create_machine(self, spec):
                return None
            def destroy_machine(self, machine_id):
                pass
            def start_machine(self, machine_id):
                pass
            def stop_machine(self, machine_id):
                pass

        # Should be able to instantiate
        instance = TestCloud2({"test": "config"})
        assert isinstance(instance, BaseCloud)


# ============================================================================
# get_cloud_provider Factory Tests (lines 78-103)
# ============================================================================


class TestGetCloudProvider:
    """Tests for get_cloud_provider() - lines 78-103."""

    def test_get_cloud_provider_unknown_type(self):
        """Raise CloudError for unknown provider type - line 93-95."""
        with pytest.raises(CloudError, match="Unknown cloud provider"):
            get_cloud_provider("unknown_provider", {})

    def test_get_cloud_provider_case_insensitive(self):
        """Provider type lookup is case-insensitive - line 91."""
        # Register a test provider
        @register_cloud("lowercase_test")
        class LowercaseTestCloud(BaseCloud):
            provider_type = "lowercase_test"

            def authenticate(self):
                return True

            def list_machines(self, filters=None):
                return []

            def get_machine(self, machine_id):
                return None

            def create_machine(self, spec):
                return None

            def destroy_machine(self, machine_id):
                pass

            def start_machine(self, machine_id):
                pass

            def stop_machine(self, machine_id):
                pass

        # Should work with uppercase
        provider = get_cloud_provider("LOWERCASE_TEST", {})
        assert isinstance(provider, LowercaseTestCloud)

    def test_get_cloud_provider_initialization_error(self):
        """Raise CloudError when provider initialization fails - line 102-103."""
        @register_cloud("failing_provider")
        class FailingCloud(BaseCloud):
            provider_type = "failing_provider"

            def __init__(self, config):
                raise ValueError("Configuration error")

            def authenticate(self):
                return True

            def list_machines(self, filters=None):
                return []

        with pytest.raises(CloudError, match="Failed to initialize"):
            get_cloud_provider("failing_provider", {})

    def test_get_cloud_provider_success(self):
        """Successfully create cloud provider instance - line 100-101."""
        @register_cloud("working_provider")
        class WorkingCloud(BaseCloud):
            provider_type = "working_provider"

            def authenticate(self):
                return True

            def list_machines(self, filters=None):
                return []

            def get_machine(self, machine_id):
                return None

            def create_machine(self, spec):
                return None

            def destroy_machine(self, machine_id):
                pass

            def start_machine(self, machine_id):
                pass

            def stop_machine(self, machine_id):
                pass

        provider = get_cloud_provider("working_provider", {"key": "value"})

        assert isinstance(provider, WorkingCloud)
        assert provider.config == {"key": "value"}


# ============================================================================
# list_available_providers Tests (lines 106-119)
# ============================================================================


class TestListAvailableProviders:
    """Tests for list_available_providers() - lines 106-119."""

    def test_list_available_providers_returns_list(self):
        """Return list of available providers - line 107-119."""
        # Clear registry first to avoid interference from other tests
        original_registry = CLOUD_REGISTRY.copy()

        try:
            CLOUD_REGISTRY.clear()

            @register_cloud("provider1")
            class Provider1(BaseCloud):
                """First provider description."""
                supports_cloud_init = True

                def authenticate(self):
                    pass

                def list_machines(self, filters=None):
                    pass

                def get_machine(self, machine_id):
                    pass

                def create_machine(self, spec):
                    pass

                def destroy_machine(self, machine_id):
                    pass

                def start_machine(self, machine_id):
                    pass

                def stop_machine(self, machine_id):
                    pass

            @register_cloud("provider2")
            class Provider2(BaseCloud):
                """Second provider description."""
                supports_cloud_init = False

                def authenticate(self):
                    pass

                def list_machines(self, filters=None):
                    pass

                def get_machine(self, machine_id):
                    pass

                def create_machine(self, spec):
                    pass

                def destroy_machine(self, machine_id):
                    pass

                def start_machine(self, machine_id):
                    pass

                def stop_machine(self, machine_id):
                    pass

            providers = list_available_providers()

            assert isinstance(providers, list)
            assert len(providers) >= 2

            # Check structure
            provider_names = [p["name"] for p in providers]
            assert "provider1" in provider_names
            assert "provider2" in provider_names

            # Check provider info dict
            p1 = next(p for p in providers if p["name"] == "provider1")
            assert p1["supports_cloud_init"] is True
            assert "First provider" in p1["description"]

            p2 = next(p for p in providers if p["name"] == "provider2")
            assert p2["supports_cloud_init"] is False

        finally:
            # Restore original registry
            CLOUD_REGISTRY.clear()
            CLOUD_REGISTRY.update(original_registry)

    def test_list_available_providers_handles_no_docstring(self):
        """Handle providers without docstring - line 116."""
        original_registry = CLOUD_REGISTRY.copy()

        try:
            CLOUD_REGISTRY.clear()

            @register_cloud("nodoc_provider")
            class NodocProvider(BaseCloud):
                # No docstring
                def authenticate(self):
                    pass

                def list_machines(self, filters=None):
                    pass

                def get_machine(self, machine_id):
                    pass

                def create_machine(self, spec):
                    pass

                def destroy_machine(self, machine_id):
                    pass

                def start_machine(self, machine_id):
                    pass

                def stop_machine(self, machine_id):
                    pass

            providers = list_available_providers()
            nodoc = next(p for p in providers if p["name"] == "nodoc_provider")

            assert nodoc["description"] == ""
            assert "supports_cloud_init" in nodoc

        finally:
            CLOUD_REGISTRY.clear()
            CLOUD_REGISTRY.update(original_registry)


# ============================================================================
# Machine Dataclass Tests (lines 49-85)
# ============================================================================


class TestMachineDataclass:
    """Tests for Machine dataclass - lines 49-85."""

    def test_machine_creation_basic(self):
        """Create Machine with required fields."""
        machine = Machine(
            id="i-123456",
            name="test-machine",
            state=MachineState.RUNNING,
            provider="aws",
            provider_id="provider-123"
        )

        assert machine.id == "i-123456"
        assert machine.name == "test-machine"
        assert machine.state == MachineState.RUNNING
        assert machine.provider == "aws"
        assert machine.provider_id == "provider-123"

    def test_machine_creation_full(self):
        """Create Machine with all fields."""
        now = datetime.now(timezone.utc)
        machine = Machine(
            id="i-123456",
            name="test-machine",
            state=MachineState.RUNNING,
            provider="aws",
            provider_id="provider-123",
            region="us-east-1",
            image="ami-123456",
            size="t3.medium",
            public_ips=["192.0.2.1"],
            private_ips=["10.0.0.1"],
            created_at=now,
            updated_at=now,
            tags={"env": "prod"},
            extra={"key": "value"}
        )

        assert machine.region == "us-east-1"
        assert machine.image == "ami-123456"
        assert machine.size == "t3.medium"
        assert machine.public_ips == ["192.0.2.1"]
        assert machine.private_ips == ["10.0.0.1"]
        assert machine.created_at == now
        assert machine.updated_at == now
        assert machine.tags == {"env": "prod"}
        assert machine.extra == {"key": "value"}

    def test_machine_to_dict(self):
        """Convert Machine to dictionary - lines 68-85."""
        now = datetime.now(timezone.utc)
        machine = Machine(
            id="i-123456",
            name="test-machine",
            state=MachineState.RUNNING,
            provider="aws",
            provider_id="provider-123",
            region="us-east-1",
            image="ami-123456",
            size="t3.medium",
            public_ips=["192.0.2.1"],
            private_ips=["10.0.0.1"],
            created_at=now,
            updated_at=now,
            tags={"env": "prod"},
            extra={"key": "value"}
        )

        result = machine.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == "i-123456"
        assert result["name"] == "test-machine"
        assert result["state"] == "running"  # Enum value
        assert result["provider"] == "aws"
        assert result["region"] == "us-east-1"
        assert result["public_ips"] == ["192.0.2.1"]
        assert result["created_at"] == now.isoformat()
        assert result["updated_at"] == now.isoformat()

    def test_machine_to_dict_none_dates(self):
        """to_dict handles None timestamps - line 81."""
        machine = Machine(
            id="i-123456",
            name="test-machine",
            state=MachineState.PENDING,
            provider="aws",
            provider_id="provider-123",
            created_at=None,
            updated_at=None
        )

        result = machine.to_dict()

        assert result["created_at"] is None
        assert result["updated_at"] is None


# ============================================================================
# MachineSpec Dataclass Tests (lines 33-46)
# ============================================================================


class TestMachineSpecDataclass:
    """Tests for MachineSpec dataclass - lines 33-46."""

    def test_machine_spec_creation_basic(self):
        """Create MachineSpec with required fields."""
        spec = MachineSpec(
            name="new-machine",
            image="ubuntu-22.04",
            size="t3.medium"
        )

        assert spec.name == "new-machine"
        assert spec.image == "ubuntu-22.04"
        assert spec.size == "t3.medium"
        assert spec.region == ""
        assert spec.cloud_init == ""
        assert spec.ssh_keys == []
        assert spec.networks == []
        assert spec.storage_gb == 0
        assert spec.tags == {}
        assert spec.extra == {}

    def test_machine_spec_creation_full(self):
        """Create MachineSpec with all fields."""
        spec = MachineSpec(
            name="new-machine",
            image="ubuntu-22.04",
            size="t3.medium",
            region="us-east-1",
            cloud_init="#!/bin/bash\necho hello",
            ssh_keys=["ssh-rsa AAAA..."],
            networks=["vpc-123"],
            storage_gb=100,
            tags={"env": "test"},
            extra={"monitoring": True}
        )

        assert spec.region == "us-east-1"
        assert spec.cloud_init == "#!/bin/bash\necho hello"
        assert spec.ssh_keys == ["ssh-rsa AAAA..."]
        assert spec.networks == ["vpc-123"]
        assert spec.storage_gb == 100
        assert spec.tags == {"env": "test"}
        assert spec.extra == {"monitoring": True}


# ============================================================================
# MachineState Enum Tests (lines 16-30)
# ============================================================================


class TestMachineStateEnum:
    """Tests for MachineState enum - lines 16-30."""

    def test_machine_state_values(self):
        """Verify all machine states are defined."""
        expected_states = [
            "pending", "running", "stopped", "terminated", "error", "unknown",
            "commissioning", "deploying", "ready", "allocated"
        ]

        for state_name in expected_states:
            state = MachineState(state_name)
            assert state.value == state_name

    def test_machine_state_enum_members(self):
        """Verify specific enum members."""
        assert MachineState.RUNNING.value == "running"
        assert MachineState.STOPPED.value == "stopped"
        assert MachineState.ERROR.value == "error"
        assert MachineState.COMMISSIONING.value == "commissioning"
        assert MachineState.READY.value == "ready"


# ============================================================================
# Cloud Exception Tests
# ============================================================================


class TestCloudExceptions:
    """Tests for cloud provider exception classes."""

    def test_cloud_error(self):
        """CloudError is base exception."""
        error = CloudError("Test error")
        assert str(error) == "Test error"

    def test_cloud_auth_error(self):
        """CloudAuthError for auth failures."""
        error = CloudAuthError("Auth failed")
        assert isinstance(error, CloudError)
        assert str(error) == "Auth failed"

    def test_cloud_not_found_error(self):
        """CloudNotFoundError for missing resources."""
        error = CloudNotFoundError("Machine not found")
        assert isinstance(error, CloudError)
        assert str(error) == "Machine not found"

    def test_cloud_quota_error(self):
        """CloudQuotaError for quota exceeded."""
        error = CloudQuotaError("Quota exceeded")
        assert isinstance(error, CloudError)
        assert str(error) == "Quota exceeded"


# ============================================================================
# BaseCloud Abstract Tests
# ============================================================================


class TestBaseCloud:
    """Tests for BaseCloud abstract class."""

    def test_base_cloud_initialization(self):
        """BaseCloud stores config and initializes authenticated flag."""
        @register_cloud("abstract_test")
        class TestCloud(BaseCloud):
            def authenticate(self):
                return True

            def list_machines(self, filters=None):
                return []

            def get_machine(self, machine_id):
                return None

            def create_machine(self, spec):
                return None

            def destroy_machine(self, machine_id):
                pass

            def start_machine(self, machine_id):
                pass

            def stop_machine(self, machine_id):
                pass

        cloud = TestCloud({"key": "value"})

        assert cloud.config == {"key": "value"}
        assert cloud._authenticated is False

    def test_base_cloud_class_attributes(self):
        """BaseCloud has class-level attributes."""
        @register_cloud("attr_test")
        class TestCloud(BaseCloud):
            def authenticate(self):
                return True

            def list_machines(self, filters=None):
                return []

            def get_machine(self, machine_id):
                return None

            def create_machine(self, spec):
                return None

            def destroy_machine(self, machine_id):
                pass

            def start_machine(self, machine_id):
                pass

            def stop_machine(self, machine_id):
                pass

        assert hasattr(TestCloud, "provider_type")
        assert hasattr(TestCloud, "supports_cloud_init")
        assert TestCloud.supports_cloud_init is True
