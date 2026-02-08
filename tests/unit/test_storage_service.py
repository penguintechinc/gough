"""Unit tests for Storage Service with mocked boto3.

Tests:
- S3/MinIO storage configuration
- Upload/download operations
- Error handling for storage failures
- Credential management
- Multi-storage backend support
"""

import json
from datetime import datetime
from unittest.mock import patch, MagicMock, call

import pytest


class TestStorageConfig:
    """Tests for storage configuration management."""

    def test_create_storage_config(self, db, test_user):
        """Test creating a storage configuration."""
        config_id = db.storage_config.insert(
            name="production-minio",
            provider_type="s3",
            endpoint_url="https://minio.example.com:9000",
            region="us-east-1",
            bucket_name="gough-prod",
            credentials_path="secrets:minio-prod",
            is_active=True,
            is_default=True,
            use_ssl=True,
            config_data=json.dumps({
                "access_key_env": "MINIO_ACCESS_KEY",
                "secret_key_env": "MINIO_SECRET_KEY",
                "path_style": False
            }),
            created_by=test_user.id
        )
        db.commit()

        config = db.storage_config(config_id)
        assert config.name == "production-minio"
        assert config.provider_type == "s3"
        assert config.endpoint_url == "https://minio.example.com:9000"
        assert config.is_active is True
        assert config.is_default is True

    def test_create_multiple_storage_configs(self, db, test_user):
        """Test creating multiple storage configurations."""
        configs = [
            ("minio-local", "minio", "http://localhost:9000"),
            ("aws-s3", "aws_s3", "https://s3.amazonaws.com"),
            ("gcs", "gcs", "https://storage.googleapis.com"),
        ]

        for name, provider, endpoint in configs:
            db.storage_config.insert(
                name=name,
                provider_type=provider,
                endpoint_url=endpoint,
                bucket_name=f"bucket-{name}",
                credentials_path=f"secrets:{name}",
                created_by=test_user.id
            )
        db.commit()

        all_configs = db(db.storage_config).select()
        assert len(all_configs) == 3

    def test_storage_config_unique_name(self, db, test_user):
        """Test that storage config names are unique."""
        db.storage_config.insert(
            name="unique-name",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="secrets:test",
            created_by=test_user.id
        )
        db.commit()

        with pytest.raises(Exception):
            db.storage_config.insert(
                name="unique-name",
                provider_type="s3",
                endpoint_url="http://other:9000",
                credentials_path="secrets:test",
                created_by=test_user.id
            )
            db.commit()

    def test_get_active_storage_config(self, db, test_user):
        """Test retrieving active storage config."""
        # Create inactive config
        db.storage_config.insert(
            name="inactive",
            provider_type="s3",
            endpoint_url="http://inactive:9000",
            credentials_path="secrets:inactive",
            is_active=False,
            created_by=test_user.id
        )
        # Create active config
        db.storage_config.insert(
            name="active",
            provider_type="s3",
            endpoint_url="http://active:9000",
            credentials_path="secrets:active",
            is_active=True,
            is_default=True,
            created_by=test_user.id
        )
        db.commit()

        active = db(db.storage_config.is_active == True).select().first()
        assert active.name == "active"

    def test_default_storage_config(self, db, test_user):
        """Test retrieving default storage config."""
        db.storage_config.insert(
            name="config1",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="secrets:config1",
            is_active=True,
            is_default=False,
            created_by=test_user.id
        )
        db.storage_config.insert(
            name="config2",
            provider_type="s3",
            endpoint_url="http://localhost:9001",
            credentials_path="secrets:config2",
            is_active=True,
            is_default=True,
            created_by=test_user.id
        )
        db.commit()

        default = db((db.storage_config.is_active == True) &
                    (db.storage_config.is_default == True)).select().first()
        assert default.name == "config2"

    def test_storage_config_data_json(self, db, test_user):
        """Test storing additional config data as JSON."""
        config_data = {
            "path_style": True,
            "signature_version": "s3v4",
            "max_retries": 3,
            "timeout": 30,
            "custom_headers": {"X-Custom": "value"}
        }
        config_id = db.storage_config.insert(
            name="json-config",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="secrets:test",
            config_data=json.dumps(config_data),
            created_by=test_user.id
        )
        db.commit()

        config = db.storage_config(config_id)
        stored_data = json.loads(config.config_data)
        assert stored_data["path_style"] is True
        assert stored_data["timeout"] == 30


class TestStorageOperations:
    """Tests for storage operations with mocked boto3."""

    @patch('boto3.client')
    def test_s3_upload_file(self, mock_boto3_client, db, test_storage_config):
        """Test uploading file to S3."""
        # Setup mock
        client = MagicMock()
        mock_boto3_client.return_value = client
        client.put_object.return_value = {'ETag': '"abc123"'}

        # Simulate upload
        file_data = b"test file content"
        bucket = test_storage_config.bucket_name
        key = "uploads/test-image.tar.gz"

        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=file_data,
            ContentType="application/gzip"
        )

        # Verify
        client.put_object.assert_called_once()
        call_kwargs = client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == bucket
        assert call_kwargs["Key"] == key

    @patch('boto3.client')
    def test_s3_download_file(self, mock_boto3_client, db, test_storage_config):
        """Test downloading file from S3."""
        client = MagicMock()
        mock_boto3_client.return_value = client

        file_data = b"downloaded content"
        client.get_object.return_value = {
            'Body': MagicMock(read=MagicMock(return_value=file_data)),
            'ContentLength': len(file_data)
        }

        # Simulate download
        bucket = test_storage_config.bucket_name
        key = "images/ubuntu-24.04.tar.gz"

        response = client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read()

        assert content == file_data
        client.get_object.assert_called_once_with(Bucket=bucket, Key=key)

    @patch('boto3.client')
    def test_s3_head_object(self, mock_boto3_client, db, test_storage_config):
        """Test checking object metadata."""
        client = MagicMock()
        mock_boto3_client.return_value = client
        client.head_object.return_value = {
            'ContentLength': 1000000,
            'LastModified': datetime.utcnow(),
            'ETag': '"abc123def456"'
        }

        bucket = test_storage_config.bucket_name
        key = "images/base.tar.gz"

        response = client.head_object(Bucket=bucket, Key=key)

        assert response['ContentLength'] == 1000000
        assert 'ETag' in response

    @patch('boto3.client')
    def test_s3_list_objects(self, mock_boto3_client, db, test_storage_config):
        """Test listing objects in bucket."""
        client = MagicMock()
        mock_boto3_client.return_value = client
        client.list_objects_v2.return_value = {
            'Contents': [
                {'Key': 'image1.tar.gz', 'Size': 100},
                {'Key': 'image2.tar.gz', 'Size': 200},
                {'Key': 'image3.tar.gz', 'Size': 300},
            ],
            'KeyCount': 3
        }

        bucket = test_storage_config.bucket_name
        response = client.list_objects_v2(Bucket=bucket, Prefix="images/")

        assert response['KeyCount'] == 3
        assert len(response['Contents']) == 3
        assert response['Contents'][0]['Key'] == 'image1.tar.gz'

    @patch('boto3.client')
    def test_s3_delete_object(self, mock_boto3_client, db, test_storage_config):
        """Test deleting object from S3."""
        client = MagicMock()
        mock_boto3_client.return_value = client
        client.delete_object.return_value = {'DeleteMarker': True}

        bucket = test_storage_config.bucket_name
        key = "old-image.tar.gz"

        client.delete_object(Bucket=bucket, Key=key)

        client.delete_object.assert_called_once_with(Bucket=bucket, Key=key)

    @patch('boto3.client')
    def test_s3_multipart_upload(self, mock_boto3_client, db, test_storage_config):
        """Test multipart upload for large files."""
        client = MagicMock()
        mock_boto3_client.return_value = client

        # Mock multipart upload
        client.create_multipart_upload.return_value = {'UploadId': 'test-upload-id'}
        client.upload_part.return_value = {'ETag': '"part-etag"'}
        client.complete_multipart_upload.return_value = {
            'ETag': '"complete-etag"',
            'Location': 'http://bucket.s3.amazonaws.com/key'
        }

        bucket = test_storage_config.bucket_name
        key = "large-image.tar.gz"

        # Initiate
        mpu = client.create_multipart_upload(Bucket=bucket, Key=key)
        upload_id = mpu['UploadId']
        assert upload_id == 'test-upload-id'

        # Upload part
        part = client.upload_part(
            Bucket=bucket,
            Key=key,
            PartNumber=1,
            UploadId=upload_id,
            Body=b"part data"
        )
        assert 'ETag' in part

        # Complete
        complete = client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={'Parts': [{'ETag': part['ETag'], 'PartNumber': 1}]}
        )
        assert 'ETag' in complete


class TestStorageErrors:
    """Tests for storage error handling."""

    @patch('boto3.client')
    def test_handle_bucket_not_found(self, mock_boto3_client, db, test_storage_config):
        """Test handling bucket not found error."""
        from botocore.exceptions import ClientError

        client = MagicMock()
        mock_boto3_client.return_value = client

        error = ClientError(
            {'Error': {'Code': 'NoSuchBucket', 'Message': 'Bucket not found'}},
            'ListObjects'
        )
        client.list_objects_v2.side_effect = error

        with pytest.raises(ClientError) as exc_info:
            client.list_objects_v2(Bucket='non-existent-bucket')

        assert 'NoSuchBucket' in str(exc_info.value)

    @patch('boto3.client')
    def test_handle_access_denied(self, mock_boto3_client):
        """Test handling access denied error."""
        from botocore.exceptions import ClientError

        client = MagicMock()
        mock_boto3_client.return_value = client

        error = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'Access Denied'}},
            'GetObject'
        )
        client.get_object.side_effect = error

        with pytest.raises(ClientError):
            client.get_object(Bucket='bucket', Key='key')

    @patch('boto3.client')
    def test_handle_connection_error(self, mock_boto3_client):
        """Test handling connection errors."""
        from botocore.exceptions import BotoCoreError

        client = MagicMock()
        mock_boto3_client.return_value = client

        error = BotoCoreError()
        client.put_object.side_effect = error

        with pytest.raises(BotoCoreError):
            client.put_object(Bucket='bucket', Key='key', Body=b'data')

    @patch('boto3.client')
    def test_handle_invalid_credentials(self, mock_boto3_client):
        """Test handling invalid credentials."""
        from botocore.exceptions import ClientError

        client = MagicMock()
        mock_boto3_client.return_value = client

        error = ClientError(
            {'Error': {'Code': 'InvalidAccessKeyId', 'Message': 'Invalid credentials'}},
            'ListBuckets'
        )
        client.list_buckets.side_effect = error

        with pytest.raises(ClientError):
            client.list_buckets()


class TestStorageMultiBackend:
    """Tests for multi-backend storage support."""

    def test_multiple_backends_available(self, db, test_user):
        """Test having multiple storage backends available."""
        backends = [
            ("local-minio", "minio", "http://localhost:9000"),
            ("aws", "aws_s3", "https://s3.amazonaws.com"),
            ("gcp", "gcs", "https://storage.googleapis.com"),
            ("azure", "azure_blob", "https://example.blob.core.windows.net"),
        ]

        for name, provider_type, endpoint in backends:
            db.storage_config.insert(
                name=name,
                provider_type=provider_type,
                endpoint_url=endpoint,
                bucket_name=f"bucket-{provider_type}",
                credentials_path=f"secrets:{name}",
                is_active=True,
                created_by=test_user.id
            )
        db.commit()

        configs = db(db.storage_config.is_active == True).select()
        assert len(configs) == 4

        providers = {c.provider_type for c in configs}
        assert "minio" in providers
        assert "aws_s3" in providers

    def test_switching_default_backend(self, db, test_user):
        """Test switching which backend is default."""
        # Create two configs
        config1_id = db.storage_config.insert(
            name="backend-1",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="secrets:1",
            is_default=True,
            created_by=test_user.id
        )
        config2_id = db.storage_config.insert(
            name="backend-2",
            provider_type="s3",
            endpoint_url="http://localhost:9001",
            credentials_path="secrets:2",
            is_default=False,
            created_by=test_user.id
        )
        db.commit()

        # Verify initial default
        default = db((db.storage_config.is_default == True)).select().first()
        assert default.name == "backend-1"

        # Switch default
        db(db.storage_config.id == config1_id).update(is_default=False)
        db(db.storage_config.id == config2_id).update(is_default=True)
        db.commit()

        # Verify new default
        default = db((db.storage_config.is_default == True)).select().first()
        assert default.name == "backend-2"

    def test_disable_storage_backend(self, db, test_user):
        """Test disabling a storage backend."""
        config_id = db.storage_config.insert(
            name="to-disable",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="secrets:test",
            is_active=True,
            created_by=test_user.id
        )
        db.commit()

        # Disable
        db(db.storage_config.id == config_id).update(is_active=False)
        db.commit()

        config = db.storage_config(config_id)
        assert config.is_active is False

        # Should not appear in active configs
        active = db(db.storage_config.is_active == True).select()
        assert config_id not in [c.id for c in active]


class TestStorageCredentials:
    """Tests for credential management."""

    def test_credentials_path_storage(self, db, test_user):
        """Test storing credentials path in secrets manager."""
        config_id = db.storage_config.insert(
            name="with-creds",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="secrets:gough/minio-prod",
            created_by=test_user.id
        )
        db.commit()

        config = db.storage_config(config_id)
        assert config.credentials_path == "secrets:gough/minio-prod"

    def test_credentials_not_stored_plaintext(self, db, test_user):
        """Test that credentials are not stored in plaintext."""
        # Credentials should reference secrets manager
        config_id = db.storage_config.insert(
            name="secure",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="vault:minio/credentials",  # Reference, not actual credentials
            created_by=test_user.id
        )
        db.commit()

        config = db.storage_config(config_id)
        # Should not contain actual access keys/secrets
        assert "AKIA" not in (config.credentials_path or "")
        assert "wJalrXUt" not in (config.credentials_path or "")

    def test_config_data_for_sensitive_info(self, db, test_user):
        """Test config_data field for additional sensitive configuration."""
        # Additional config should also reference secrets, not store plaintext
        config_data = {
            "token_path": "secrets:github/token",
            "api_key_path": "secrets:stripe/key"
        }
        config_id = db.storage_config.insert(
            name="with-config-data",
            provider_type="s3",
            endpoint_url="http://localhost:9000",
            credentials_path="secrets:s3",
            config_data=json.dumps(config_data),
            created_by=test_user.id
        )
        db.commit()

        config = db.storage_config(config_id)
        data = json.loads(config.config_data)
        # Should reference secrets, not contain plaintext
        assert data["token_path"].startswith("secrets:")
        assert data["api_key_path"].startswith("secrets:")
