"""Test suite for Storage Service (app/services/storage.py).

Coverage targets:
- StorageConfig dataclass and from_row() method
- StorageError exception hierarchy
- StorageService client initialization
- All async storage operations (upload, download, presigned URLs, list, delete, create_bucket)
- get_storage_service() function with different configurations
"""

import pytest
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock, AsyncMock, patch
from types import SimpleNamespace
from dataclasses import asdict

from app.services.storage import (
    StorageConfig,
    StorageService,
    StorageError,
    StorageConfigNotFoundError,
    StorageAccessError,
    StorageValidationError,
    get_storage_service,
)


class TestStorageConfigDataclass:
    """Test StorageConfig dataclass and from_row() factory method."""

    def test_storage_config_instantiation(self):
        """Test StorageConfig can be instantiated with all fields."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test-storage",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="my-bucket",
            credentials_path="/vault/s3-creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={"acl": "private"},
            created_by=123,
            created_at=now,
            updated_at=now,
        )
        assert config.id == 1
        assert config.name == "test-storage"
        assert config.provider_type == "s3"
        assert config.is_active is True

    def test_storage_config_from_row_valid_json(self):
        """Test from_row() parses valid config_data JSON."""
        now = datetime.now(timezone.utc)
        row = SimpleNamespace(
            id=2,
            name="minio-storage",
            provider_type="minio",
            endpoint_url="http://minio:9000",
            region="us-east-1",
            bucket_name="data",
            credentials_path="/vault/minio-creds",
            is_default=False,
            is_active=True,
            use_ssl=False,
            config_data='{"storage_class": "STANDARD"}',
            created_by=456,
            created_at=now,
            updated_at=now,
        )
        config = StorageConfig.from_row(row)
        assert config.id == 2
        assert config.name == "minio-storage"
        assert config.config_data == {"storage_class": "STANDARD"}

    def test_storage_config_from_row_invalid_json(self):
        """Test from_row() handles invalid JSON gracefully."""
        now = datetime.now(timezone.utc)
        row = Mock(
            id=3,
            name="broken-storage",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=False,
            is_active=True,
            use_ssl=True,
            config_data="{invalid json}",
            created_by=789,
            created_at=now,
            updated_at=now,
        )
        config = StorageConfig.from_row(row)
        assert config.id == 3
        assert config.config_data == {}

    def test_storage_config_from_row_empty_config_data(self):
        """Test from_row() handles None config_data."""
        now = datetime.now(timezone.utc)
        row = Mock(
            id=4,
            name="empty-config",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-east-1",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=False,
            is_active=True,
            use_ssl=True,
            config_data=None,
            created_by=None,
            created_at=now,
            updated_at=now,
        )
        config = StorageConfig.from_row(row)
        assert config.config_data == {}


class TestStorageExceptions:
    """Test StorageError exception hierarchy."""

    def test_storage_error_base(self):
        """Test StorageError base exception."""
        exc = StorageError("test error")
        assert str(exc) == "test error"

    def test_storage_config_not_found_by_id(self):
        """Test StorageConfigNotFoundError with config_id."""
        exc = StorageConfigNotFoundError(config_id=123)
        assert "Storage configuration not found: ID 123" in str(exc)
        assert exc.config_id == 123

    def test_storage_config_not_found_by_name(self):
        """Test StorageConfigNotFoundError with config_name."""
        exc = StorageConfigNotFoundError(config_name="my-storage")
        assert "Storage configuration not found: my-storage" in str(exc)
        assert exc.config_name == "my-storage"

    def test_storage_config_not_found_empty(self):
        """Test StorageConfigNotFoundError with no parameters."""
        exc = StorageConfigNotFoundError()
        assert "No storage configuration found" in str(exc)

    def test_storage_access_error(self):
        """Test StorageAccessError exception."""
        exc = StorageAccessError("connection failed")
        assert str(exc) == "connection failed"
        assert isinstance(exc, StorageError)

    def test_storage_validation_error(self):
        """Test StorageValidationError exception."""
        exc = StorageValidationError("invalid config")
        assert str(exc) == "invalid config"
        assert isinstance(exc, StorageError)


class TestStorageServiceInitialization:
    """Test StorageService initialization and client creation."""

    def test_storage_service_init(self):
        """Test StorageService initialization."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)
        assert service.config == config
        assert service.credentials == creds
        assert service._client is None

    @patch("app.services.storage.boto3")
    def test_storage_service_get_client_aws_s3(self, mock_boto3):
        """Test _get_client() creates AWS S3 client."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="aws-s3",
            provider_type="s3",
            endpoint_url=None,
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
        service = StorageService(config, creds)
        client = service._get_client()
        assert client is not None
        mock_boto3.client.assert_called_once()

    @patch("app.services.storage.boto3")
    def test_storage_service_get_client_minio(self, mock_boto3):
        """Test _get_client() creates MinIO client with custom endpoint."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=2,
            name="minio",
            provider_type="minio",
            endpoint_url="http://minio:9000",
            region="us-east-1",
            bucket_name="data",
            credentials_path="/vault/creds",
            is_default=False,
            is_active=True,
            use_ssl=False,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "minioadmin", "secret_access_key": "minioadmin"}
        service = StorageService(config, creds)
        client = service._get_client()
        assert client is not None
        mock_boto3.client.assert_called_once()
        call_kwargs = mock_boto3.client.call_args[1]
        assert call_kwargs.get("endpoint_url") == "http://minio:9000"
        assert call_kwargs.get("use_ssl") is False

    @patch("app.services.storage.boto3")
    def test_storage_service_get_client_caches(self, mock_boto3):
        """Test _get_client() caches the boto3 client."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)
        client1 = service._get_client()
        client2 = service._get_client()
        assert client1 is client2
        mock_boto3.client.assert_called_once()


class TestStorageServiceOperations:
    """Test async storage operations."""

    @pytest.mark.asyncio
    async def test_upload_file_success(self):
        """Test upload_file() success path."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        # Mock the client
        mock_client = MagicMock()
        mock_client.upload_file.return_value = None
        mock_client.head_object.return_value = {
            "ETag": '"abc123"',
            "VersionId": "v1",
            "ContentLength": 1024,
            "LastModified": now,
        }
        service._client = mock_client

        result = await service.upload_file("/tmp/file.txt", "key/file.txt")
        assert result["bucket"] == "bucket"
        assert result["key"] == "key/file.txt"
        assert result["etag"] == '"abc123"'

    @pytest.mark.asyncio
    async def test_upload_file_no_bucket(self):
        """Test upload_file() raises error when no bucket specified."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name=None,  # No bucket
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)
        service._client = MagicMock()

        with pytest.raises(StorageValidationError, match="No bucket specified"):
            await service.upload_file("/tmp/file.txt", "key/file.txt")

    @pytest.mark.asyncio
    async def test_download_file_success(self):
        """Test download_file() success path."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        mock_client = MagicMock()
        mock_client.download_file.return_value = None
        mock_client.head_object.return_value = {
            "ContentLength": 2048,
            "LastModified": now,
        }
        service._client = mock_client

        result = await service.download_file("key/file.txt", "/tmp/downloaded.txt")
        assert result["bucket"] == "bucket"
        assert result["key"] == "key/file.txt"
        assert result["local_path"] == "/tmp/downloaded.txt"

    @pytest.mark.asyncio
    async def test_presigned_url_success(self):
        """Test get_presigned_url() success."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://s3.example.com/bucket/key/file.txt?expires=..."
        service._client = mock_client

        url = await service.get_presigned_url("key/file.txt", expiration=7200)
        assert "https://s3.example.com" in url

    @pytest.mark.asyncio
    async def test_list_objects_success(self):
        """Test list_objects() returns list of objects."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "file1.txt", "Size": 100, "ETag": '"abc"', "LastModified": now, "StorageClass": "STANDARD"},
                {"Key": "file2.txt", "Size": 200, "ETag": '"def"', "LastModified": now, "StorageClass": "STANDARD"},
            ]
        }
        service._client = mock_client

        objects = await service.list_objects(prefix="")
        assert len(objects) == 2
        assert objects[0]["key"] == "file1.txt"
        assert objects[1]["key"] == "file2.txt"

    @pytest.mark.asyncio
    async def test_delete_object_success(self):
        """Test delete_object() success."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        mock_client = MagicMock()
        mock_client.delete_object.return_value = {"DeleteMarker": True, "VersionId": "v1"}
        service._client = mock_client

        result = await service.delete_object("key/file.txt")
        assert result["bucket"] == "bucket"
        assert result["key"] == "key/file.txt"
        assert result["delete_marker"] is True

    @pytest.mark.asyncio
    async def test_create_bucket_us_east_1(self):
        """Test create_bucket() for us-east-1 region."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-east-1",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        mock_client = MagicMock()
        mock_client.create_bucket.return_value = {"Location": "/new-bucket"}
        service._client = mock_client

        result = await service.create_bucket("new-bucket")
        assert result["bucket"] == "new-bucket"

    @pytest.mark.asyncio
    async def test_list_buckets_success(self):
        """Test list_buckets() returns list of buckets."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        mock_client = MagicMock()
        mock_client.list_buckets.return_value = {
            "Buckets": [
                {"Name": "bucket1", "CreationDate": now},
                {"Name": "bucket2", "CreationDate": now},
            ]
        }
        service._client = mock_client

        buckets = await service.list_buckets()
        assert len(buckets) == 2
        assert buckets[0]["name"] == "bucket1"

    @pytest.mark.asyncio
    async def test_test_connection_success(self):
        """Test test_connection() verifies connectivity."""
        now = datetime.now(timezone.utc)
        config = StorageConfig(
            id=1,
            name="test",
            provider_type="s3",
            endpoint_url="https://s3.example.com",
            region="us-west-2",
            bucket_name="bucket",
            credentials_path="/vault/creds",
            is_default=True,
            is_active=True,
            use_ssl=True,
            config_data={},
            created_by=1,
            created_at=now,
            updated_at=now,
        )
        creds = {"access_key_id": "key", "secret_access_key": "secret"}
        service = StorageService(config, creds)

        mock_client = MagicMock()
        mock_client.list_buckets.return_value = {
            "Buckets": [{"Name": "bucket1", "CreationDate": now}]
        }
        service._client = mock_client

        result = await service.test_connection()
        assert result["success"] is True
        assert result["bucket_count"] == 1


@pytest.mark.asyncio
async def test_get_storage_service_by_id(monkeypatch, tmp_path):
    """Test get_storage_service() retrieves config by ID."""
    from penguin_dal import DB, Field

    # Create in-memory DB
    db_url = f"sqlite:///{tmp_path}/test.db"
    db = DB(db_url, pool_size=1, reflect=False, migrate=True)
    db.define_table(
        "storage_config",
        Field("name", "string"),
        Field("provider_type", "string"),
        Field("endpoint_url", "string"),
        Field("region", "string"),
        Field("bucket_name", "string"),
        Field("credentials_path", "string"),
        Field("is_default", "boolean"),
        Field("is_active", "boolean"),
        Field("use_ssl", "boolean"),
        Field("config_data", "json"),
        Field("created_by", "integer"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )

    now = datetime.now(timezone.utc)
    db.storage_config.insert(
        name="test-s3",
        provider_type="s3",
        endpoint_url="https://s3.example.com",
        region="us-west-2",
        bucket_name="bucket",
        credentials_path="/vault/s3-creds",
        is_default=True,
        is_active=True,
        use_ssl=True,
        config_data="{}",
        created_by=1,
        created_at=now,
        updated_at=now,
    )
    db.commit()

    # Mock get_db and secrets manager
    def _get_db():
        return db

    monkeypatch.setattr("app.services.storage.get_db", _get_db)

    async def _mock_get_secrets_manager():
        mgr = AsyncMock()
        mgr.get_secret.return_value = {"access_key_id": "key", "secret_access_key": "secret"}
        return mgr

    # get_secrets_manager is imported inside the function, patch at source module
    with patch("app.secrets.get_secrets_manager", _mock_get_secrets_manager):
        # This should work - config exists and is active
        service = await get_storage_service(config_id=1)
    assert service.config.name == "test-s3"


@pytest.mark.asyncio
async def test_get_storage_service_not_found(monkeypatch, tmp_path):
    """Test get_storage_service() raises error when config not found."""
    from penguin_dal import DB, Field

    db_url = f"sqlite:///{tmp_path}/test2.db"
    db = DB(db_url, pool_size=1, reflect=False, migrate=True)
    db.define_table(
        "storage_config",
        Field("name", "string"),
        Field("provider_type", "string"),
        Field("endpoint_url", "string"),
        Field("region", "string"),
        Field("bucket_name", "string"),
        Field("credentials_path", "string"),
        Field("is_default", "boolean"),
        Field("is_active", "boolean"),
        Field("use_ssl", "boolean"),
        Field("config_data", "json"),
        Field("created_by", "integer"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )

    def _get_db():
        return db

    monkeypatch.setattr("app.services.storage.get_db", _get_db)

    with pytest.raises(StorageConfigNotFoundError):
        await get_storage_service(config_id=999)
