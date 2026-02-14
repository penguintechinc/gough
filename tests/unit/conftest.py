"""Pytest fixtures for Gough unit tests.

Provides shared fixtures for testing:
- Database initialization and cleanup
- Test client creation
- Mock users and authentication
- Mock storage configurations
- Mock cloud provider configurations
"""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

import pytest
from quart import Quart

# Try importing from api-manager
try:
    import sys
    sys.path.insert(0, '/home/penguin/code/Gough/services/api-manager')
    from app import create_app
    from app.config import Config
    from app.models import init_db, get_db
    from app.models.ipxe import define_ipxe_tables
except ImportError:
    # Fallback for testing without full app
    pass


class TestConfig:
    """Test configuration."""

    TESTING = True
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret-key"
    JWT_SECRET = "test-jwt-secret"
    SECURITY_PASSWORD_SALT = "test-salt"
    DATABASE_URL = "sqlite:///:memory:"
    DB_POOL_SIZE = 5
    DB_TYPE = "sqlite"


@pytest.fixture(scope="function")
def test_config():
    """Provide test configuration."""
    return TestConfig()


@pytest.fixture(scope="function")
def app(test_config):
    """Create and configure test Quart application."""
    app = Quart(__name__)
    app.config.from_object(test_config)

    with app.app_context():
        # Initialize database
        db = init_db(app)
        define_ipxe_tables(db)
        db.commit()
        yield app


@pytest.fixture(scope="function")
def db(app):
    """Provide test database connection."""
    with app.app_context():
        return get_db()


@pytest.fixture(scope="function")
def test_client(app):
    """Create test client for async requests."""
    return app.test_client()


@pytest.fixture(scope="function")
def test_user(app, db):
    """Create a test user for authentication."""
    from app.models import VALID_ROLES

    # Ensure admin role exists
    admin_role = db(db.auth_role.name == "admin").select().first()
    if not admin_role:
        role_id = db.auth_role.insert(
            name="admin",
            description="Administrator",
            permissions=json.dumps(["all"])
        )
        db.commit()
    else:
        role_id = admin_role.id

    # Create test user
    user_email = "testuser@example.com"
    db(db.auth_user.email == user_email).delete()
    db.commit()

    user_id = db.auth_user.insert(
        email=user_email,
        password="hashed_password",
        active=True,
        fs_uniquifier="test-uniquifier-001",
        confirmed_at=datetime.utcnow(),
        full_name="Test User"
    )
    db.commit()

    # Assign admin role
    db.auth_user_roles.insert(
        user_id=user_id,
        role_id=role_id
    )
    db.commit()

    return db.auth_user(user_id)


@pytest.fixture(scope="function")
def test_storage_config(app, db, test_user):
    """Create a test S3 storage configuration."""
    config_id = db.storage_config.insert(
        name="test-minio",
        provider_type="s3",
        endpoint_url="http://localhost:9000",
        region="us-east-1",
        bucket_name="gough-storage",
        credentials_path="secrets:minio",
        is_active=True,
        is_default=True,
        use_ssl=False,
        config_data=json.dumps({
            "access_key": "minioadmin",
            "secret_key": "minioadmin"
        }),
        created_by=test_user.id
    )
    db.commit()
    return db.storage_config(config_id)


@pytest.fixture(scope="function")
def test_cloud_provider(app, db):
    """Create a test cloud provider configuration."""
    provider_id = db.cloud_providers.insert(
        name="test-maas",
        provider_type="maas",
        description="Test MaaS provider",
        region="us-east-1",
        credentials_path="secrets:maas",
        config_data=json.dumps({
            "url": "http://localhost:5240/MAAS",
            "api_key": "test-api-key"
        }),
        is_active=True
    )
    db.commit()
    return db.cloud_providers(provider_id)


@pytest.fixture(scope="function")
def test_egg(app, db):
    """Create a test egg for provisioning."""
    egg_id = db.eggs.insert(
        name="test-nginx",
        display_name="Test Nginx",
        description="Test Nginx egg",
        egg_type="snap",
        version="1.0",
        category="webserver",
        snap_name="nginx",
        snap_channel="stable",
        snap_classic=False,
        is_active=True,
        is_default=False,
        required_architecture="any"
    )
    db.commit()
    return db.eggs(egg_id)


@pytest.fixture(scope="function")
def test_cloud_init_template(app, db):
    """Create a test cloud-init template."""
    template_id = db.cloud_init_templates.insert(
        name="test-basic",
        description="Test basic cloud-init template",
        template_content="""#cloud-config
packages:
  - curl
  - git
runcmd:
  - echo "Hello World"
""",
        template_type="user-data",
        is_default=True,
        version="1.0"
    )
    db.commit()
    return db.cloud_init_templates(template_id)


@pytest.fixture(scope="function")
def test_ipxe_config(app, db):
    """Create a test iPXE configuration."""
    config_id = db.ipxe_config.insert(
        name="test-ipxe",
        dhcp_mode="proxy",
        dhcp_interface="eth0",
        dhcp_subnet="192.168.1.0/24",
        dhcp_range_start="192.168.1.100",
        dhcp_range_end="192.168.1.200",
        dhcp_gateway="192.168.1.1",
        dns_servers=json.dumps(["8.8.8.8", "8.8.4.4"]),
        tftp_enabled=True,
        http_boot_url="http://localhost:8080",
        is_active=True
    )
    db.commit()
    return db.ipxe_config(config_id)


@pytest.fixture(scope="function")
def test_machine(app, db, test_ipxe_config):
    """Create a test iPXE machine."""
    machine_id = db.ipxe_machines.insert(
        system_id="test-machine-001",
        hostname="test-machine.local",
        mac_address="00:11:22:33:44:55",
        ip_address="192.168.1.100",
        status="discovered",
        boot_mode="uefi",
        architecture="amd64",
        cpu_count=4,
        memory_mb=8192,
        storage_gb=100,
        power_type="ipmi",
        zone="default",
        pool="default",
        tags=json.dumps(["test", "demo"]),
        last_seen_at=datetime.utcnow()
    )
    db.commit()
    return db.ipxe_machines(machine_id)


@pytest.fixture(scope="function")
def test_image(app, db):
    """Create a test boot image."""
    image_id = db.ipxe_images.insert(
        name="ubuntu-24.04-amd64",
        display_name="Ubuntu 24.04 LTS (amd64)",
        os_name="ubuntu",
        os_version="24.04",
        architecture="amd64",
        kernel_path="/images/ubuntu-24.04/kernel",
        initrd_path="/images/ubuntu-24.04/initrd",
        squashfs_path="/images/ubuntu-24.04/filesystem.squashfs",
        kernel_params="ro quiet splash",
        image_type="minimal",
        is_default=True,
        is_active=True,
        checksum="abc123def456",
        size_bytes=500000000
    )
    db.commit()
    return db.ipxe_images(image_id)


@pytest.fixture(scope="function")
def test_boot_config(app, db, test_image):
    """Create a test boot configuration."""
    config_id = db.ipxe_boot_configs.insert(
        name="test-boot-config",
        description="Test boot configuration",
        ipxe_script="#!ipxe\necho Boot test",
        kernel_params="ro quiet",
        boot_order=json.dumps(["pxe", "disk"]),
        timeout_seconds=30,
        default_image_id=test_image.id,
        is_default=True
    )
    db.commit()
    return db.ipxe_boot_configs(config_id)


@pytest.fixture(scope="function")
def test_egg_group(app, db, test_egg):
    """Create a test egg group."""
    group_id = db.egg_groups.insert(
        name="test-group",
        display_name="Test Group",
        description="Test egg group",
        eggs=json.dumps([{"egg_id": test_egg.id, "order": 1}]),
        is_default=False
    )
    db.commit()
    return db.egg_groups(group_id)


@pytest.fixture(scope="function")
def mock_boto3_client():
    """Mock boto3 S3 client."""
    with patch('boto3.client') as mock_client:
        client = MagicMock()
        mock_client.return_value = client
        client.head_object.return_value = {
            'ContentLength': 1000,
            'ETag': '"abc123"',
            'LastModified': datetime.utcnow()
        }
        client.get_object.return_value = {
            'Body': MagicMock(read=MagicMock(return_value=b'test data')),
            'ContentLength': 1000
        }
        client.put_object.return_value = {
            'ETag': '"abc123"'
        }
        yield client


@pytest.fixture(scope="function")
def mock_storage_service(mock_boto3_client):
    """Mock storage service with boto3."""
    with patch('app.services.storage.boto3.client', return_value=mock_boto3_client):
        yield mock_boto3_client
