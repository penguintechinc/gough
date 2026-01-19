"""S3-Compatible Storage Service for Gough.

Provides unified interface for S3-compatible object storage including:
- AWS S3
- MinIO
- Google Cloud Storage (via S3 compatibility)
- Azure Blob Storage (via S3 compatibility)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from ..models import get_db

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Base exception for storage operations."""

    pass


class StorageConfigNotFoundError(StorageError):
    """Raised when storage configuration is not found."""

    def __init__(self, config_id: int | None = None, config_name: str | None = None):
        self.config_id = config_id
        self.config_name = config_name
        if config_id:
            msg = f"Storage configuration not found: ID {config_id}"
        elif config_name:
            msg = f"Storage configuration not found: {config_name}"
        else:
            msg = "No storage configuration found"
        super().__init__(msg)


class StorageAccessError(StorageError):
    """Raised when storage access fails."""

    pass


class StorageValidationError(StorageError):
    """Raised when storage configuration is invalid."""

    pass


@dataclass(slots=True)
class StorageConfig:
    """Storage configuration data class."""

    id: int
    name: str
    provider_type: str
    endpoint_url: str | None
    region: str | None
    bucket_name: str | None
    credentials_path: str | None
    is_default: bool
    is_active: bool
    use_ssl: bool
    config_data: dict[str, Any]
    created_by: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row) -> StorageConfig:
        """Create StorageConfig from database row."""
        config_data = {}
        if row.config_data:
            try:
                config_data = json.loads(row.config_data)
            except json.JSONDecodeError:
                log.warning(f"Invalid JSON in config_data for storage {row.id}")

        return cls(
            id=row.id,
            name=row.name,
            provider_type=row.provider_type,
            endpoint_url=row.endpoint_url,
            region=row.region,
            bucket_name=row.bucket_name,
            credentials_path=row.credentials_path,
            is_default=row.is_default,
            is_active=row.is_active,
            use_ssl=row.use_ssl,
            config_data=config_data,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class StorageService:
    """Service for S3-compatible object storage operations.

    Supports any S3-compatible backend including MinIO, AWS S3, GCS, Azure Blob.
    All boto3 operations are wrapped with asyncio.to_thread() for async compatibility.
    """

    def __init__(self, config: StorageConfig, credentials: dict[str, Any]):
        """Initialize storage service with configuration.

        Args:
            config: StorageConfig instance
            credentials: Dict with access_key_id and secret_access_key
        """
        self.config = config
        self.credentials = credentials
        self._client = None

    def _get_client(self):
        """Get or create boto3 S3 client (synchronous)."""
        if self._client is None:
            session_config = BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            )

            client_kwargs = {
                "service_name": "s3",
                "aws_access_key_id": self.credentials.get("access_key_id"),
                "aws_secret_access_key": self.credentials.get("secret_access_key"),
                "config": session_config,
            }

            if self.config.endpoint_url:
                client_kwargs["endpoint_url"] = self.config.endpoint_url

            if self.config.region:
                client_kwargs["region_name"] = self.config.region

            if not self.config.use_ssl:
                client_kwargs["use_ssl"] = False

            self._client = boto3.client(**client_kwargs)

        return self._client

    async def upload_file(
        self,
        file_path: str,
        object_key: str,
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Upload file to S3-compatible storage.

        Args:
            file_path: Local file path to upload
            object_key: Object key (path) in bucket
            bucket: Bucket name (uses config default if not specified)
            metadata: Optional metadata key-value pairs

        Returns:
            Dict with upload result including etag, version_id

        Raises:
            StorageError: If upload fails
        """

        def _upload():
            client = self._get_client()
            bucket_name = bucket or self.config.bucket_name

            if not bucket_name:
                raise StorageValidationError("No bucket specified")

            extra_args = {}
            if metadata:
                extra_args["Metadata"] = metadata

            try:
                response = client.upload_file(
                    file_path, bucket_name, object_key, ExtraArgs=extra_args or None
                )
                head = client.head_object(Bucket=bucket_name, Key=object_key)
                return {
                    "bucket": bucket_name,
                    "key": object_key,
                    "etag": head.get("ETag"),
                    "version_id": head.get("VersionId"),
                    "size": head.get("ContentLength"),
                    "last_modified": head.get("LastModified"),
                }
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"Upload failed: {e}")

        return await asyncio.to_thread(_upload)

    async def download_file(
        self, object_key: str, file_path: str, bucket: str | None = None
    ) -> dict[str, Any]:
        """Download file from S3-compatible storage.

        Args:
            object_key: Object key (path) in bucket
            file_path: Local file path to save to
            bucket: Bucket name (uses config default if not specified)

        Returns:
            Dict with download result

        Raises:
            StorageError: If download fails
        """

        def _download():
            client = self._get_client()
            bucket_name = bucket or self.config.bucket_name

            if not bucket_name:
                raise StorageValidationError("No bucket specified")

            try:
                client.download_file(bucket_name, object_key, file_path)
                head = client.head_object(Bucket=bucket_name, Key=object_key)
                return {
                    "bucket": bucket_name,
                    "key": object_key,
                    "local_path": file_path,
                    "size": head.get("ContentLength"),
                    "last_modified": head.get("LastModified"),
                }
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"Download failed: {e}")

        return await asyncio.to_thread(_download)

    async def get_presigned_url(
        self,
        object_key: str,
        bucket: str | None = None,
        expiration: int = 3600,
        http_method: str = "get_object",
    ) -> str:
        """Generate presigned URL for object access.

        Args:
            object_key: Object key (path) in bucket
            bucket: Bucket name (uses config default if not specified)
            expiration: URL expiration in seconds (default 1 hour)
            http_method: S3 operation (get_object, put_object, etc.)

        Returns:
            Presigned URL string

        Raises:
            StorageError: If URL generation fails
        """

        def _generate_url():
            client = self._get_client()
            bucket_name = bucket or self.config.bucket_name

            if not bucket_name:
                raise StorageValidationError("No bucket specified")

            try:
                url = client.generate_presigned_url(
                    http_method,
                    Params={"Bucket": bucket_name, "Key": object_key},
                    ExpiresIn=expiration,
                )
                return url
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"URL generation failed: {e}")

        return await asyncio.to_thread(_generate_url)

    async def list_objects(
        self,
        prefix: str = "",
        bucket: str | None = None,
        max_keys: int = 1000,
    ) -> list[dict[str, Any]]:
        """List objects in bucket with optional prefix.

        Args:
            prefix: Object key prefix to filter
            bucket: Bucket name (uses config default if not specified)
            max_keys: Maximum number of keys to return

        Returns:
            List of object metadata dicts

        Raises:
            StorageError: If listing fails
        """

        def _list():
            client = self._get_client()
            bucket_name = bucket or self.config.bucket_name

            if not bucket_name:
                raise StorageValidationError("No bucket specified")

            try:
                response = client.list_objects_v2(
                    Bucket=bucket_name, Prefix=prefix, MaxKeys=max_keys
                )

                objects = []
                for obj in response.get("Contents", []):
                    objects.append(
                        {
                            "key": obj.get("Key"),
                            "size": obj.get("Size"),
                            "last_modified": obj.get("LastModified"),
                            "etag": obj.get("ETag"),
                            "storage_class": obj.get("StorageClass"),
                        }
                    )

                return objects
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"List objects failed: {e}")

        return await asyncio.to_thread(_list)

    async def delete_object(
        self, object_key: str, bucket: str | None = None
    ) -> dict[str, Any]:
        """Delete object from bucket.

        Args:
            object_key: Object key (path) in bucket
            bucket: Bucket name (uses config default if not specified)

        Returns:
            Dict with deletion result

        Raises:
            StorageError: If deletion fails
        """

        def _delete():
            client = self._get_client()
            bucket_name = bucket or self.config.bucket_name

            if not bucket_name:
                raise StorageValidationError("No bucket specified")

            try:
                response = client.delete_object(Bucket=bucket_name, Key=object_key)
                return {
                    "bucket": bucket_name,
                    "key": object_key,
                    "delete_marker": response.get("DeleteMarker"),
                    "version_id": response.get("VersionId"),
                }
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"Delete failed: {e}")

        return await asyncio.to_thread(_delete)

    async def create_bucket(self, bucket_name: str) -> dict[str, Any]:
        """Create new bucket.

        Args:
            bucket_name: Name of bucket to create

        Returns:
            Dict with creation result

        Raises:
            StorageError: If creation fails
        """

        def _create():
            client = self._get_client()

            try:
                create_config = {}
                if self.config.region and self.config.region != "us-east-1":
                    create_config["CreateBucketConfiguration"] = {
                        "LocationConstraint": self.config.region
                    }

                if create_config:
                    response = client.create_bucket(
                        Bucket=bucket_name, **create_config
                    )
                else:
                    response = client.create_bucket(Bucket=bucket_name)

                return {
                    "bucket": bucket_name,
                    "location": response.get("Location"),
                }
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"Bucket creation failed: {e}")

        return await asyncio.to_thread(_create)

    async def list_buckets(self) -> list[dict[str, Any]]:
        """List all buckets.

        Returns:
            List of bucket metadata dicts

        Raises:
            StorageError: If listing fails
        """

        def _list():
            client = self._get_client()

            try:
                response = client.list_buckets()
                buckets = []
                for bucket in response.get("Buckets", []):
                    buckets.append(
                        {
                            "name": bucket.get("Name"),
                            "creation_date": bucket.get("CreationDate"),
                        }
                    )
                return buckets
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"List buckets failed: {e}")

        return await asyncio.to_thread(_list)

    async def test_connection(self) -> dict[str, Any]:
        """Test storage connection and credentials.

        Returns:
            Dict with test results

        Raises:
            StorageError: If connection test fails
        """

        def _test():
            client = self._get_client()

            try:
                response = client.list_buckets()
                bucket_count = len(response.get("Buckets", []))

                return {
                    "success": True,
                    "provider": self.config.provider_type,
                    "endpoint": self.config.endpoint_url or "default",
                    "bucket_count": bucket_count,
                }
            except (BotoCoreError, ClientError) as e:
                raise StorageAccessError(f"Connection test failed: {e}")

        return await asyncio.to_thread(_test)


async def get_storage_service(
    config_id: int | None = None, config_name: str | None = None
) -> StorageService:
    """Get StorageService instance from database configuration.

    Args:
        config_id: Storage configuration ID
        config_name: Storage configuration name (if config_id not provided)

    Returns:
        StorageService instance

    Raises:
        StorageConfigNotFoundError: If configuration not found
        StorageAccessError: If credentials cannot be loaded
    """
    from ..secrets import get_secrets_manager

    db = get_db()

    if config_id:
        config_row = db(
            (db.storage_config.id == config_id) & (db.storage_config.is_active == True)
        ).select().first()
    elif config_name:
        config_row = db(
            (db.storage_config.name == config_name)
            & (db.storage_config.is_active == True)
        ).select().first()
    else:
        config_row = db(
            (db.storage_config.is_default == True)
            & (db.storage_config.is_active == True)
        ).select().first()

    if not config_row:
        raise StorageConfigNotFoundError(config_id=config_id, config_name=config_name)

    config = StorageConfig.from_row(config_row)

    if not config.credentials_path:
        raise StorageAccessError(
            f"No credentials path configured for storage {config.name}"
        )

    try:
        secrets_manager = await get_secrets_manager()
        credentials = await secrets_manager.get_secret(config.credentials_path)
    except Exception as e:
        raise StorageAccessError(
            f"Failed to load credentials from {config.credentials_path}: {e}"
        )

    return StorageService(config, credentials)
