"""Storage Management API Endpoints.

Provides REST API for managing S3-compatible storage configurations and operations.
Admin-only access for configuration management.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional, Any

from quart import Blueprint, jsonify, request

from ..auth import require_auth, require_role
from ..db.run_db import run_db
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
from ._dto import serialize_storage_config
from ._helpers import envelope_success, err_bad_request, err_not_found

log = logging.getLogger(__name__)

storage_bp = Blueprint("storage", __name__)


def _user_has_scope(scope: str) -> bool:
    """Check if current user has a specific scope."""
    from ..middleware import get_current_user
    user = get_current_user() or {}
    payload = user.get("_jwt_payload") or {}
    raw = payload.get("scope", "")
    if isinstance(raw, list):
        scopes = {s for s in raw if isinstance(s, str)}
    elif isinstance(raw, str):
        scopes = set(raw.split())
    else:
        scopes = set()
    return scope in scopes


@storage_bp.route("/configs", methods=["GET"])
@require_auth
@require_role("admin", "maintainer")
async def list_storage_configs():
    """List all storage configurations.

    Returns:
        200: List of storage configurations
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    rows = await run_db(
        lambda: db(db.storage_config).select(orderby=~db.storage_config.is_default)
    )

    configs = [serialize_storage_config(row) for row in rows]

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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    existing = await run_db(lambda: db(db.storage_config.name == name).select().first())
    if existing:
        return jsonify({"error": f"Storage configuration '{name}' already exists"}), 409

    config_data_json = json.dumps(config_data) if config_data else None
    created_by = request.user.id

    # Regression: gh-22. Clearing the old default + insert + commit + the
    # post-commit refetch is one unit of work -- stays in one run_db()
    # closure per the house rule (see app/db/run_db.py).
    def _create() -> Any:
        if is_default:
            db(db.storage_config).update(is_default=False)

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
            created_by=created_by,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()

        return db(db.storage_config.id == config_id).select().first()

    config_row = await run_db(_create)

    return jsonify(serialize_storage_config(config_row)), 201


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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    config_row = await run_db(lambda: db(db.storage_config.id == config_id).select().first())

    if not config_row:
        return jsonify({"error": "Storage configuration not found"}), 404

    # Explicit allow-list projection -- never the raw row. Regression: audit
    # output-validation. This handler used to echo ``credentials_path`` (a
    # secrets-manager pointer) and ``config_data`` (provider-specific JSON
    # that can itself carry inline credentials, e.g. a GCS service-account
    # key) straight from the row, the same raw-row-echo shape as the fixed
    # clouds.py provider leak. See ``app.api._dto.STORAGE_CONFIG_PUBLIC_FIELDS``.
    return jsonify(serialize_storage_config(config_row)), 200


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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    config_row = await run_db(lambda: db(db.storage_config.id == config_id).select().first())

    if not config_row:
        return jsonify({"error": "Storage configuration not found"}), 404

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    update_fields = {"updated_at": datetime.utcnow()}

    if "name" in data:
        name = data["name"].strip()
        if name:
            # Regression: gh-22. Off the event loop via run_db() instead
            # of blocking the request coroutine inline.
            existing = await run_db(
                lambda: db(
                    (db.storage_config.name == name)
                    & (db.storage_config.id != config_id)
                ).select().first()
            )
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

    # Regression: gh-22. Update + commit + the post-commit refetch is one
    # unit of work -- stays in one run_db() closure per the house rule
    # (see app/db/run_db.py).
    def _apply_update() -> Any:
        db(db.storage_config.id == config_id).update(**update_fields)
        db.commit()
        return db(db.storage_config.id == config_id).select().first()

    config_row = await run_db(_apply_update)

    return jsonify(serialize_storage_config(config_row)), 200


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

    # Regression: gh-22. Fetch + (conditional) delete + commit is one unit
    # of work -- stays in one run_db() closure per the house rule (see
    # app/db/run_db.py).
    def _delete() -> str:
        config_row = db(db.storage_config.id == config_id).select().first()
        if not config_row:
            return "not_found"
        if config_row.is_default:
            return "is_default"
        db(db.storage_config.id == config_id).delete()
        db.commit()
        return "ok"

    status = await run_db(_delete)
    if status == "not_found":
        return jsonify({"error": "Storage configuration not found"}), 404
    if status == "is_default":
        return (
            jsonify(
                {
                    "error": "Cannot delete default storage configuration. Set another as default first."
                }
            ),
            409,
        )

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

    # Regression: gh-22. Fetch + (conditional) clear-old-default +
    # set-new-default + commit is one unit of work -- stays in one
    # run_db() closure per the house rule (see app/db/run_db.py).
    def _set_default() -> Any:
        config_row = db(db.storage_config.id == config_id).select().first()
        if not config_row:
            return None
        db(db.storage_config).update(is_default=False)
        db(db.storage_config.id == config_id).update(is_default=True)
        db.commit()
        return config_row

    config_row = await run_db(_set_default)
    if not config_row:
        return jsonify({"error": "Storage configuration not found"}), 404

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


# ============================================================================
# Storage Quotas (Plan 5)
# ============================================================================


@storage_bp.route("/quotas", methods=["GET"])
@require_auth
async def list_storage_quotas():
    """List storage quotas.

    Spec: ``GET /api/v1/storage/quotas``
    Scope: ``gough.storage.read``

    Query Parameters:
        tenant_id: Filter by tenant (optional)

    Returns:
        200: List of storage quotas with pagination metadata
    """
    if not _user_has_scope("gough.storage.read"):
        return jsonify({"error": "Insufficient scope: gough.storage.read required"}), 403

    db = get_db()

    # Parse query parameters
    tenant_id_filter = request.args.get("tenant_id", None)

    # Build query
    query = db(db.storage_quotas)
    if tenant_id_filter:
        query = query(db.storage_quotas.tenant_id == tenant_id_filter)

    # Fetch all quotas. Regression: gh-22 -- now off the event loop via
    # run_db() instead of blocking the request coroutine inline.
    quotas_rows = await run_db(lambda: query.select(orderby=~db.storage_quotas.created_at))

    quotas = []
    for row in quotas_rows:
        quotas.append({
            "id": str(row.id),
            "tenant_id": row.tenant_id,
            "resource_type": row.resource_type,
            "limit_value": float(row.limit_value) if row.limit_value else None,
            "used_value": float(row.used_value) if row.used_value else None,
            "unit": row.unit,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })

    return envelope_success({
        "quotas": quotas,
        "total": len(quotas),
    })


@storage_bp.route("/quota-request", methods=["POST"])
@require_auth
async def request_storage_quota():
    """Request a storage quota increase.

    Spec: ``POST /api/v1/storage/quota-request``
    Scope: ``gough.storage.write``

    Request Body:
        tenant_id: Tenant ID (required)
        resource_type: Type of resource (e.g., "storage", "bandwidth", "buckets") (required)
        requested_value: Requested quota value (required)
        unit: Unit of measurement (e.g., "GB", "TB", "count") (required)
        justification: Reason for the request (required)

    Returns:
        201: Quota request created
        400: Invalid request
    """
    if not _user_has_scope("gough.storage.write"):
        return jsonify({"error": "Insufficient scope: gough.storage.write required"}), 403

    data = await request.get_json(silent=True)
    if not data:
        return err_bad_request("JSON body required")

    # Validate required fields
    required_fields = ["tenant_id", "resource_type", "requested_value", "unit", "justification"]
    for field in required_fields:
        if field not in data or not data.get(field):
            return err_bad_request(f"Required field missing: {field}")

    tenant_id = data.get("tenant_id", "").strip()
    resource_type = data.get("resource_type", "").strip()
    requested_value = data.get("requested_value")
    unit = data.get("unit", "").strip()
    justification = data.get("justification", "").strip()

    # Validate requested_value is numeric
    try:
        requested_value = float(requested_value)
    except (ValueError, TypeError):
        return err_bad_request("requested_value must be a number")

    db = get_db()
    request_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Regression: gh-22. Insert + commit is one unit of work -- stays in
    # one run_db() closure per the house rule (see app/db/run_db.py),
    # single rollback point inside the closure.
    def _insert() -> tuple[bool, Any]:
        try:
            db.storage_quota_requests.insert(
                id=request_id,
                tenant_id=tenant_id,
                resource_type=resource_type,
                requested_value=requested_value,
                unit=unit,
                justification=justification,
                status="pending",
                created_at=now,
                updated_at=now,
            )
            db.commit()
            return True, None
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            return False, exc

    ok, exc = await run_db(_insert)
    if not ok:
        log.exception("Error creating storage quota request: %s", exc)
        return jsonify({"error": f"Internal server error: {str(exc)}"}), 500

    return envelope_success(
        {
            "id": request_id,
            "tenant_id": tenant_id,
            "resource_type": resource_type,
            "requested_value": requested_value,
            "unit": unit,
            "justification": justification,
            "status": "pending",
            "created_at": now.isoformat(),
        },
        status_code=201,
    )
