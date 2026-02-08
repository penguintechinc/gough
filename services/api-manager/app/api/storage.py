"""Storage Management API Endpoints.

Provides REST API for managing S3-compatible storage configurations and operations.
Admin-only access for configuration management.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from quart import Blueprint, jsonify, request

from ..auth import require_auth, require_role
from ..models import get_db
from ..services.storage import (
    StorageConfig,
    StorageService,
    StorageError,
    StorageConfigNotFoundError,
    StorageAccessError,
    StorageValidationError,
    get_storage_service,
)

log = logging.getLogger(__name__)

storage_bp = Blueprint("storage", __name__)


@storage_bp.route("/configs", methods=["GET"])
@require_auth
@require_role("admin", "maintainer")
async def list_storage_configs():
    """List all storage configurations.

    Returns:
        200: List of storage configurations
    """
    db = get_db()

    configs = []
    for row in db(db.storage_config).select(orderby=~db.storage_config.is_default):
        configs.append(
            {
                "id": row.id,
                "name": row.name,
                "provider_type": row.provider_type,
                "endpoint_url": row.endpoint_url,
                "region": row.region,
                "bucket_name": row.bucket_name,
                "is_default": row.is_default,
                "is_active": row.is_active,
                "use_ssl": row.use_ssl,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    return jsonify({"configs": configs}), 200


@storage_bp.route("/configs", methods=["POST"])
@require_auth
@require_role("admin")
async def create_storage_config():
    """Create new storage configuration.

    Request Body:
        name: Configuration name (required)
        provider_type: Provider type (s3, minio, gcs, azure_blob) (required)
        endpoint_url: S3 endpoint URL (required for non-AWS)
        region: AWS region or provider region
        bucket_name: Default bucket name
        credentials_path: Path to credentials in secrets manager (required)
        use_ssl: Use SSL/TLS (default true)
        is_default: Set as default storage (default false)
        config_data: Additional JSON configuration

    Returns:
        201: Storage configuration created
        400: Invalid request
        409: Configuration name already exists
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name", "").strip()
    provider_type = data.get("provider_type", "").strip().lower()
    endpoint_url = data.get("endpoint_url", "").strip() or None
    region = data.get("region", "").strip() or None
    bucket_name = data.get("bucket_name", "").strip() or None
    credentials_path = data.get("credentials_path", "").strip()
    use_ssl = data.get("use_ssl", True)
    is_default = data.get("is_default", False)
    config_data = data.get("config_data", {})

    if not name:
        return jsonify({"error": "Name is required"}), 400

    if provider_type not in ["s3", "minio", "gcs", "azure_blob"]:
        return jsonify(
            {"error": "Invalid provider_type. Must be: s3, minio, gcs, azure_blob"}
        ), 400

    if not credentials_path:
        return jsonify({"error": "credentials_path is required"}), 400

    if provider_type != "s3" and not endpoint_url:
        return jsonify(
            {"error": "endpoint_url required for non-AWS providers"}
        ), 400

    db = get_db()

    existing = db(db.storage_config.name == name).select().first()
    if existing:
        return jsonify({"error": f"Storage configuration '{name}' already exists"}), 409

    if is_default:
        db(db.storage_config).update(is_default=False)

    config_data_json = json.dumps(config_data) if config_data else None

    config_id = db.storage_config.insert(
        name=name,
        provider_type=provider_type,
        endpoint_url=endpoint_url,
        region=region,
        bucket_name=bucket_name,
        credentials_path=credentials_path,
        is_default=is_default,
        is_active=True,
        use_ssl=use_ssl,
        config_data=config_data_json,
        created_by=request.user.id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.commit()

    config_row = db(db.storage_config.id == config_id).select().first()

    return (
        jsonify(
            {
                "id": config_row.id,
                "name": config_row.name,
                "provider_type": config_row.provider_type,
                "endpoint_url": config_row.endpoint_url,
                "region": config_row.region,
                "bucket_name": config_row.bucket_name,
                "is_default": config_row.is_default,
                "is_active": config_row.is_active,
                "use_ssl": config_row.use_ssl,
                "created_at": config_row.created_at.isoformat(),
            }
        ),
        201,
    )


@storage_bp.route("/configs/<int:config_id>", methods=["GET"])
@require_auth
@require_role("admin", "maintainer")
async def get_storage_config(config_id: int):
    """Get storage configuration by ID.

    Args:
        config_id: Storage configuration ID

    Returns:
        200: Storage configuration
        404: Configuration not found
    """
    db = get_db()

    config_row = db(db.storage_config.id == config_id).select().first()

    if not config_row:
        return jsonify({"error": "Storage configuration not found"}), 404

    config_data = {}
    if config_row.config_data:
        try:
            config_data = json.loads(config_row.config_data)
        except json.JSONDecodeError:
            log.warning(f"Invalid JSON in config_data for storage {config_id}")

    return (
        jsonify(
            {
                "id": config_row.id,
                "name": config_row.name,
                "provider_type": config_row.provider_type,
                "endpoint_url": config_row.endpoint_url,
                "region": config_row.region,
                "bucket_name": config_row.bucket_name,
                "credentials_path": config_row.credentials_path,
                "is_default": config_row.is_default,
                "is_active": config_row.is_active,
                "use_ssl": config_row.use_ssl,
                "config_data": config_data,
                "created_at": config_row.created_at.isoformat()
                if config_row.created_at
                else None,
                "updated_at": config_row.updated_at.isoformat()
                if config_row.updated_at
                else None,
            }
        ),
        200,
    )


@storage_bp.route("/configs/<int:config_id>", methods=["PUT"])
@require_auth
@require_role("admin")
async def update_storage_config(config_id: int):
    """Update storage configuration.

    Args:
        config_id: Storage configuration ID

    Request Body:
        name: Configuration name
        endpoint_url: S3 endpoint URL
        region: AWS region or provider region
        bucket_name: Default bucket name
        credentials_path: Path to credentials in secrets manager
        use_ssl: Use SSL/TLS
        is_active: Active status
        config_data: Additional JSON configuration

    Returns:
        200: Storage configuration updated
        400: Invalid request
        404: Configuration not found
    """
    db = get_db()

    config_row = db(db.storage_config.id == config_id).select().first()

    if not config_row:
        return jsonify({"error": "Storage configuration not found"}), 404

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    update_fields = {"updated_at": datetime.utcnow()}

    if "name" in data:
        name = data["name"].strip()
        if name:
            existing = db(
                (db.storage_config.name == name)
                & (db.storage_config.id != config_id)
            ).select().first()
            if existing:
                return (
                    jsonify({"error": f"Storage configuration '{name}' already exists"}),
                    409,
                )
            update_fields["name"] = name

    if "endpoint_url" in data:
        update_fields["endpoint_url"] = data["endpoint_url"].strip() or None

    if "region" in data:
        update_fields["region"] = data["region"].strip() or None

    if "bucket_name" in data:
        update_fields["bucket_name"] = data["bucket_name"].strip() or None

    if "credentials_path" in data:
        creds_path = data["credentials_path"].strip()
        if creds_path:
            update_fields["credentials_path"] = creds_path

    if "use_ssl" in data:
        update_fields["use_ssl"] = bool(data["use_ssl"])

    if "is_active" in data:
        update_fields["is_active"] = bool(data["is_active"])

    if "config_data" in data:
        update_fields["config_data"] = json.dumps(data["config_data"])

    db(db.storage_config.id == config_id).update(**update_fields)
    db.commit()

    config_row = db(db.storage_config.id == config_id).select().first()

    return (
        jsonify(
            {
                "id": config_row.id,
                "name": config_row.name,
                "provider_type": config_row.provider_type,
                "endpoint_url": config_row.endpoint_url,
                "region": config_row.region,
                "bucket_name": config_row.bucket_name,
                "is_default": config_row.is_default,
                "is_active": config_row.is_active,
                "use_ssl": config_row.use_ssl,
                "updated_at": config_row.updated_at.isoformat(),
            }
        ),
        200,
    )


@storage_bp.route("/configs/<int:config_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
async def delete_storage_config(config_id: int):
    """Delete storage configuration.

    Args:
        config_id: Storage configuration ID

    Returns:
        204: Storage configuration deleted
        404: Configuration not found
        409: Cannot delete default configuration
    """
    db = get_db()

    config_row = db(db.storage_config.id == config_id).select().first()

    if not config_row:
        return jsonify({"error": "Storage configuration not found"}), 404

    if config_row.is_default:
        return (
            jsonify(
                {
                    "error": "Cannot delete default storage configuration. Set another as default first."
                }
            ),
            409,
        )

    db(db.storage_config.id == config_id).delete()
    db.commit()

    return "", 204


@storage_bp.route("/configs/<int:config_id>/test", methods=["POST"])
@require_auth
@require_role("admin", "maintainer")
async def test_storage_config(config_id: int):
    """Test storage configuration connectivity.

    Args:
        config_id: Storage configuration ID

    Returns:
        200: Test successful with connection details
        400: Test failed with error message
        404: Configuration not found
    """
    try:
        storage_service = await get_storage_service(config_id=config_id)
        result = await storage_service.test_connection()
        return jsonify(result), 200
    except StorageConfigNotFoundError:
        return jsonify({"error": "Storage configuration not found"}), 404
    except StorageError as e:
        return jsonify({"error": str(e), "success": False}), 400


@storage_bp.route("/configs/<int:config_id>/set-default", methods=["POST"])
@require_auth
@require_role("admin")
async def set_default_storage_config(config_id: int):
    """Set storage configuration as default.

    Args:
        config_id: Storage configuration ID

    Returns:
        200: Default storage updated
        404: Configuration not found
    """
    db = get_db()

    config_row = db(db.storage_config.id == config_id).select().first()

    if not config_row:
        return jsonify({"error": "Storage configuration not found"}), 404

    db(db.storage_config).update(is_default=False)
    db(db.storage_config.id == config_id).update(is_default=True)
    db.commit()

    return jsonify({"message": f"Storage '{config_row.name}' set as default"}), 200


@storage_bp.route("/buckets", methods=["GET"])
@require_auth
@require_role("admin", "maintainer")
async def list_buckets():
    """List all buckets using default or specified storage configuration.

    Query Parameters:
        config_id: Storage configuration ID (optional, uses default)

    Returns:
        200: List of buckets
        400: Storage error
        404: Configuration not found
    """
    config_id = request.args.get("config_id", type=int)

    try:
        storage_service = await get_storage_service(config_id=config_id)
        buckets = await storage_service.list_buckets()
        return (
            jsonify(
                {
                    "buckets": buckets,
                    "provider": storage_service.config.provider_type,
                    "config_name": storage_service.config.name,
                }
            ),
            200,
        )
    except StorageConfigNotFoundError:
        return jsonify({"error": "Storage configuration not found"}), 404
    except StorageError as e:
        return jsonify({"error": str(e)}), 400


@storage_bp.route("/buckets", methods=["POST"])
@require_auth
@require_role("admin")
async def create_bucket():
    """Create new bucket using default or specified storage configuration.

    Request Body:
        bucket_name: Name of bucket to create (required)
        config_id: Storage configuration ID (optional, uses default)

    Returns:
        201: Bucket created
        400: Invalid request or storage error
        404: Configuration not found
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    bucket_name = data.get("bucket_name", "").strip()
    config_id = data.get("config_id")

    if not bucket_name:
        return jsonify({"error": "bucket_name is required"}), 400

    try:
        storage_service = await get_storage_service(config_id=config_id)
        result = await storage_service.create_bucket(bucket_name)
        return jsonify(result), 201
    except StorageConfigNotFoundError:
        return jsonify({"error": "Storage configuration not found"}), 404
    except StorageError as e:
        return jsonify({"error": str(e)}), 400


@storage_bp.route("/objects", methods=["GET"])
@require_auth
@require_role("admin", "maintainer", "viewer")
async def list_objects():
    """List objects in bucket with optional prefix filter.

    Query Parameters:
        bucket: Bucket name (optional if config has default bucket)
        prefix: Object key prefix filter (optional)
        max_keys: Maximum keys to return (default 1000)
        config_id: Storage configuration ID (optional, uses default)

    Returns:
        200: List of objects
        400: Storage error
        404: Configuration not found
    """
    bucket = request.args.get("bucket")
    prefix = request.args.get("prefix", "")
    max_keys = request.args.get("max_keys", type=int, default=1000)
    config_id = request.args.get("config_id", type=int)

    try:
        storage_service = await get_storage_service(config_id=config_id)
        objects = await storage_service.list_objects(
            prefix=prefix, bucket=bucket, max_keys=max_keys
        )
        return (
            jsonify(
                {
                    "objects": objects,
                    "bucket": bucket or storage_service.config.bucket_name,
                    "config_name": storage_service.config.name,
                }
            ),
            200,
        )
    except StorageConfigNotFoundError:
        return jsonify({"error": "Storage configuration not found"}), 404
    except StorageError as e:
        return jsonify({"error": str(e)}), 400


@storage_bp.route("/objects/<path:object_key>/presigned-url", methods=["POST"])
@require_auth
@require_role("admin", "maintainer", "viewer")
async def get_presigned_url(object_key: str):
    """Generate presigned URL for object access.

    Args:
        object_key: Object key (path) in bucket

    Request Body:
        bucket: Bucket name (optional if config has default bucket)
        expiration: URL expiration in seconds (default 3600)
        http_method: HTTP method (get_object or put_object, default get_object)
        config_id: Storage configuration ID (optional, uses default)

    Returns:
        200: Presigned URL
        400: Storage error
        404: Configuration not found
    """
    data = await request.get_json() or {}

    bucket = data.get("bucket")
    expiration = data.get("expiration", 3600)
    http_method = data.get("http_method", "get_object")
    config_id = data.get("config_id")

    try:
        storage_service = await get_storage_service(config_id=config_id)
        url = await storage_service.get_presigned_url(
            object_key=object_key,
            bucket=bucket,
            expiration=expiration,
            http_method=http_method,
        )
        return (
            jsonify(
                {
                    "url": url,
                    "expires_in": expiration,
                    "object_key": object_key,
                    "bucket": bucket or storage_service.config.bucket_name,
                }
            ),
            200,
        )
    except StorageConfigNotFoundError:
        return jsonify({"error": "Storage configuration not found"}), 404
    except StorageError as e:
        return jsonify({"error": str(e)}), 400


@storage_bp.route("/objects/<path:object_key>", methods=["DELETE"])
@require_auth
@require_role("admin", "maintainer")
async def delete_object(object_key: str):
    """Delete object from bucket.

    Args:
        object_key: Object key (path) in bucket

    Query Parameters:
        bucket: Bucket name (optional if config has default bucket)
        config_id: Storage configuration ID (optional, uses default)

    Returns:
        204: Object deleted
        400: Storage error
        404: Configuration not found
    """
    bucket = request.args.get("bucket")
    config_id = request.args.get("config_id", type=int)

    try:
        storage_service = await get_storage_service(config_id=config_id)
        await storage_service.delete_object(object_key=object_key, bucket=bucket)
        return "", 204
    except StorageConfigNotFoundError:
        return jsonify({"error": "Storage configuration not found"}), 404
    except StorageError as e:
        return jsonify({"error": str(e)}), 400
