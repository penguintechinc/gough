"""Secrets Management API Endpoints.

Provides REST API for managing secrets across multiple backends.
Admin-only access for all operations.
"""

from __future__ import annotations

import logging

from quart import Blueprint, current_app, jsonify, request

from ..middleware import auth_required, roles_required
from ..secrets import (
    BACKEND_REGISTRY,
    get_secrets_manager,
    SecretNotFoundError,
    SecretsManagerError,
)

log = logging.getLogger(__name__)

secrets_bp = Blueprint("secrets", __name__)


@secrets_bp.route("/backends", methods=["GET"])
@auth_required
@roles_required("admin")
async def list_backends():
    """List available and configured secrets backends.

    Returns:
        200: List of backends with availability status
    """
    configured_backend = current_app.config.get("SECRETS_BACKEND", "encrypted_db")

    backends = []
    for name, backend_class in BACKEND_REGISTRY.items():
        backend_info = {
            "name": name,
            "description": backend_class.__doc__.split("\n")[0] if backend_class.__doc__ else "",
            "is_configured": name == configured_backend,
        }

        # Add configuration hints
        if name == "encrypted_db":
            backend_info["config_required"] = ["ENCRYPTION_KEY"]
        elif name == "vault":
            backend_info["config_required"] = [
                "VAULT_ADDR",
                "VAULT_TOKEN or (VAULT_ROLE_ID + VAULT_SECRET_ID)",
            ]
        elif name == "infisical":
            backend_info["config_required"] = [
                "INFISICAL_CLIENT_ID",
                "INFISICAL_CLIENT_SECRET",
                "INFISICAL_PROJECT_ID",
            ]
        elif name == "aws":
            backend_info["config_required"] = [
                "AWS_REGION",
                "AWS_ACCESS_KEY_ID (optional if using IAM role)",
            ]
        elif name == "gcp":
            backend_info["config_required"] = [
                "GCP_PROJECT_ID",
                "GCP_CREDENTIALS_FILE or GOOGLE_APPLICATION_CREDENTIALS",
            ]
        elif name == "azure":
            backend_info["config_required"] = [
                "AZURE_VAULT_URL",
                "AZURE_CLIENT_ID (optional for managed identity)",
            ]

        backends.append(backend_info)

    return jsonify({
        "current_backend": configured_backend,
        "backends": backends,
    }), 200


@secrets_bp.route("/backends", methods=["POST"])
@auth_required
@roles_required("admin")
async def configure_backend():
    """Configure the active secrets backend.

    Note: This only validates the configuration. Actual backend
    switching requires environment variable changes and restart.

    Request Body:
        backend: Name of the backend to validate
        config: Configuration options to validate

    Returns:
        200: Configuration is valid
        400: Invalid configuration
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    backend_name = data.get("backend", "").lower()

    if not backend_name:
        return jsonify({"error": "Backend name required"}), 400

    if backend_name not in BACKEND_REGISTRY:
        return jsonify({
            "error": f"Unknown backend: {backend_name}",
            "available": list(BACKEND_REGISTRY.keys()),
        }), 400

    # Validate by attempting to instantiate and connect
    try:
        backend_class = BACKEND_REGISTRY[backend_name]
        manager = backend_class()

        # Try a simple operation to verify connectivity
        # For most backends, listing is a safe test
        manager.list_secrets("")

        return jsonify({
            "message": f"Backend '{backend_name}' configuration is valid",
            "backend": backend_name,
            "status": "connected",
        }), 200

    except SecretsManagerError as e:
        return jsonify({
            "error": f"Backend configuration error: {e}",
            "backend": backend_name,
            "status": "error",
        }), 400
    except Exception as e:
        log.exception(f"Unexpected error validating backend {backend_name}")
        return jsonify({
            "error": f"Unexpected error: {e}",
            "backend": backend_name,
            "status": "error",
        }), 500


@secrets_bp.route("/<path:secret_path>", methods=["GET"])
@auth_required
@roles_required("admin")
async def get_secret(secret_path: str):
    """Retrieve a secret by path.

    Args:
        secret_path: Path to the secret (e.g., "cloud/aws/credentials")

    Returns:
        200: Secret data
        404: Secret not found
        500: Backend error
    """
    if not secret_path:
        return jsonify({"error": "Secret path required"}), 400

    try:
        manager = await get_secrets_manager()
        secret_data = await manager.get_secret(secret_path)

        return jsonify({
            "path": secret_path,
            "data": secret_data,
        }), 200

    except SecretNotFoundError:
        return jsonify({"error": f"Secret not found: {secret_path}"}), 404
    except SecretsManagerError as e:
        log.error(f"Error retrieving secret {secret_path}: {e}")
        return jsonify({"error": str(e)}), 500


@secrets_bp.route("/<path:secret_path>", methods=["POST", "PUT"])
@auth_required
@roles_required("admin")
async def set_secret(secret_path: str):
    """Create or update a secret.

    Args:
        secret_path: Path to the secret

    Request Body:
        data: Secret data (dict)

    Returns:
        200: Secret updated
        201: Secret created
        400: Invalid request
        500: Backend error
    """
    if not secret_path:
        return jsonify({"error": "Secret path required"}), 400

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    secret_data = data.get("data")

    if secret_data is None:
        return jsonify({"error": "Secret data required"}), 400

    if not isinstance(secret_data, dict):
        return jsonify({"error": "Secret data must be a dictionary"}), 400

    try:
        manager = await get_secrets_manager()

        # Check if secret exists to determine status code
        exists = False
        try:
            await manager.get_secret(secret_path)
            exists = True
        except SecretNotFoundError:
            pass

        # Store the secret
        await manager.set_secret(secret_path, secret_data)

        log.info(f"Secret {'updated' if exists else 'created'} at path: {secret_path}")

        return jsonify({
            "message": f"Secret {'updated' if exists else 'created'} successfully",
            "path": secret_path,
        }), 200 if exists else 201

    except SecretsManagerError as e:
        log.error(f"Error storing secret {secret_path}: {e}")
        return jsonify({"error": str(e)}), 500


@secrets_bp.route("/<path:secret_path>", methods=["DELETE"])
@auth_required
@roles_required("admin")
async def delete_secret(secret_path: str):
    """Delete a secret.

    Args:
        secret_path: Path to the secret

    Returns:
        200: Secret deleted
        404: Secret not found
        500: Backend error
    """
    if not secret_path:
        return jsonify({"error": "Secret path required"}), 400

    try:
        manager = await get_secrets_manager()
        deleted = await manager.delete_secret(secret_path)

        if not deleted:
            return jsonify({"error": f"Secret not found: {secret_path}"}), 404

        log.info(f"Secret deleted at path: {secret_path}")

        return jsonify({
            "message": "Secret deleted successfully",
            "path": secret_path,
        }), 200

    except SecretsManagerError as e:
        log.error(f"Error deleting secret {secret_path}: {e}")
        return jsonify({"error": str(e)}), 500


@secrets_bp.route("/", methods=["GET"])
@auth_required
@roles_required("admin")
async def list_secrets():
    """List secrets at a given path.

    Query Parameters:
        path: Optional path prefix to filter secrets

    Returns:
        200: List of secret paths
        500: Backend error
    """
    path = (await request.args).get("path", "")

    try:
        manager = await get_secrets_manager()
        secrets = await manager.list_secrets(path)

        return jsonify({
            "path": path or "/",
            "secrets": secrets,
            "count": len(secrets),
        }), 200

    except SecretsManagerError as e:
        log.error(f"Error listing secrets at {path}: {e}")
        return jsonify({"error": str(e)}), 500
