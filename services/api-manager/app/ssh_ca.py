"""SSH Certificate Authority implementation for signing SSH public keys.

This module provides CA functionality for issuing short-lived SSH certificates
with principal-based access control and configurable validity periods.
Uses PyDAL for database operations as required by CLAUDE.md.
"""

import asyncio
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from app.db.run_db import run_db
from app.models import get_db
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from quart import Quart, current_app


class SSHCAException(Exception):
    """Exception raised for SSH CA errors."""

    pass


class SSHCertificateAuthority:
    """SSH Certificate Authority for signing user SSH public keys.

    Generates and manages RSA 4096-bit CA key pairs, signs user public keys
    with configurable principals and validity periods.
    """

    DEFAULT_VALIDITY_SECONDS = 3600  # 1 hour
    MAX_VALIDITY_SECONDS = 28800  # 8 hours
    CA_KEY_SIZE = 4096

    def __init__(self, app: Optional[Quart] = None):
        """Initialize SSH CA.

        Args:
            app: Optional Quart application instance
        """
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Quart) -> None:
        """Initialize SSH CA with Quart application.

        Args:
            app: Quart application instance
        """
        self.app = app
        app.extensions = getattr(app, "extensions", {})
        app.extensions["ssh_ca"] = self

    async def ensure_ca_initialized(self) -> None:
        """Ensure CA is initialized, creating if needed."""
        db = get_db()
        if db is None:
            return

        # Regression: gh-22. Off the event loop via run_db() instead of
        # blocking the request coroutine inline.
        def _fetch_ca_config() -> Any:
            return db(db.ssh_ca_config.ca_type == "user").select().first()

        ca_config = await run_db(_fetch_ca_config)
        if not ca_config:
            current_app.logger.info("SSH CA not found, initializing new CA")
            await self.initialize_ca()

    async def initialize_ca(self) -> None:
        """Generate new RSA 4096-bit CA key pair.

        Stores public key in database (ssh_ca_config table) and private key
        path for Vault integration.
        """
        current_app.logger.info("Generating SSH CA RSA 4096-bit key pair")

        # Generate RSA private key in a thread to avoid blocking
        private_key = await asyncio.to_thread(
            rsa.generate_private_key,
            public_exponent=65537,
            key_size=self.CA_KEY_SIZE,
            backend=default_backend(),
        )

        # Serialize private key to OpenSSH format
        private_key_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Serialize public key to OpenSSH format
        public_key = private_key.public_key()
        public_key_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )

        # Store private key in temporary location (Vault integration pending)
        ca_dir = Path("/var/lib/gough/ssh_ca")
        await asyncio.to_thread(ca_dir.mkdir, parents=True, exist_ok=True)
        private_key_path = ca_dir / "ca_key"

        await asyncio.to_thread(private_key_path.write_bytes, private_key_bytes)
        await asyncio.to_thread(os.chmod, private_key_path, 0o600)

        current_app.logger.info(f"SSH CA private key stored at {private_key_path}")

        # Store public key and metadata in database using PyDAL.
        # Regression: gh-22. Read-then-write (check-existing, then
        # update-or-insert) + commit is one unit of work -- stays in one
        # run_db() closure per the house rule (see app/db/run_db.py).
        db = get_db()

        def _store_ca_config() -> None:
            # Check if user CA config already exists
            existing = db(db.ssh_ca_config.ca_type == "user").select().first()

            if existing:
                db(db.ssh_ca_config.id == existing.id).update(
                    public_key=public_key_bytes.decode("utf-8"),
                    private_key_vault_path=str(private_key_path),
                )
            else:
                db.ssh_ca_config.insert(
                    ca_name="gough-user-ca",
                    ca_type="user",
                    public_key=public_key_bytes.decode("utf-8"),
                    private_key_vault_path=str(private_key_path),
                    cert_validity_seconds=self.DEFAULT_VALIDITY_SECONDS,
                    max_validity_seconds=self.MAX_VALIDITY_SECONDS,
                    is_active=True,
                )

            db.commit()

        await run_db(_store_ca_config)
        current_app.logger.info("SSH CA initialized successfully")

    def get_ca_public_key(self) -> str:
        """Retrieve CA public key from database.

        Returns:
            CA public key in OpenSSH format

        Raises:
            RuntimeError: If CA is not initialized
        """
        db = get_db()
        ca_config = db(db.ssh_ca_config.ca_type == "user").select().first()

        if not ca_config:
            raise RuntimeError("SSH CA not initialized")

        return ca_config.public_key

    async def sign_user_key(
        self,
        public_key: str,
        principals: List[str],
        validity_seconds: int,
        key_id: str,
        host_ids: Optional[List[str]] = None,
        source_address: Optional[str] = None,
        force_command: Optional[str] = None,
        *,
        allow_root: bool = False,
    ) -> str:
        """Sign user SSH public key using ssh-keygen.

        Access agents only honour a certificate carrying ``host:<agent_id>`` for their
        own id (fail closed), so callers MUST pass ``host_ids``; ``source_address`` and
        ``force_command`` become the matching critical options.

        Args:
            public_key: User's SSH public key in OpenSSH format
            principals: List of principals (usernames) for the certificate
            validity_seconds: Certificate validity in seconds
            key_id: Unique identifier for the certificate

        Returns:
            Signed SSH certificate in OpenSSH format

        Raises:
            ValueError: If validity exceeds maximum or principals are invalid
            RuntimeError: If signing fails
        """
        if validity_seconds > self.MAX_VALIDITY_SECONDS:
            raise ValueError(
                f"Validity period {validity_seconds}s exceeds maximum "
                f"{self.MAX_VALIDITY_SECONDS}s"
            )

        if not principals:
            raise ValueError("At least one principal is required")

        # Reserved principals are owned by the hub: callers may never request
        # ``host:*`` / ``allow-root`` (or inject separators); the server adds
        # them from host_ids / allow_root after its own authorization checks.
        for p in principals:
            validate_principal_syntax(p)
        for h in host_ids or []:
            validate_principal_syntax(h)
        principals = [
            *principals,
            *(f"{HOST_PRINCIPAL_PREFIX}{h}" for h in (host_ids or [])),
            *([ALLOW_ROOT_PRINCIPAL] if allow_root else []),
        ]

        if not re.fullmatch(r"[A-Za-z0-9_.:@=+/-]{1,256}", key_id or ""):
            raise ValueError("invalid key_id")
        current_app.logger.info(
            f"Signing SSH key for principals {principals}, "
            f"validity {validity_seconds}s, key_id {key_id}"
        )

        # Get CA private key. Regression: gh-22. _get_private_key_path()
        # issues its own direct penguin-dal select -- off the event loop
        # via run_db() instead of blocking the request coroutine inline.
        ca_private_key_path = await run_db(self._get_private_key_path)

        # Create temporary files for user public key and certificate
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Write user public key to temp file
            user_key_path = tmpdir_path / "user_key.pub"
            await asyncio.to_thread(user_key_path.write_text, public_key)

            # Certificate output path
            cert_path = tmpdir_path / "user_key-cert.pub"

            # Build ssh-keygen command
            principals_str = ",".join(principals)
            cmd = [
                "ssh-keygen",
                "-s",
                str(ca_private_key_path),
                "-I",
                key_id,
                "-n",
                principals_str,
                "-V",
                f"+{validity_seconds}s",
                *(["-O", f"source-address={source_address}"] if source_address else []),
                *(["-O", f"force-command={force_command}"] if force_command else []),
                "-z",
                str(int(datetime.utcnow().timestamp())),
                str(user_key_path),
            ]

            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                current_app.logger.debug(f"ssh-keygen output: {result.stdout}")
            except subprocess.CalledProcessError as e:
                current_app.logger.error(f"ssh-keygen failed: {e.stderr}")
                raise RuntimeError(f"Failed to sign SSH key: {e.stderr}")
            except FileNotFoundError as e:
                raise RuntimeError(f"ssh-keygen binary not found: {e}") from e
            except subprocess.TimeoutExpired:
                raise RuntimeError("SSH key signing timed out")

            # Read signed certificate
            if not await asyncio.to_thread(cert_path.exists):
                raise RuntimeError("Certificate file not created")

            signed_cert = await asyncio.to_thread(cert_path.read_text)
            signed_cert = signed_cert.strip()
            current_app.logger.info(f"Successfully signed SSH certificate: {key_id}")

            return signed_cert

    def _get_private_key_path(self) -> Path:
        """Retrieve CA private key path from database.

        Returns:
            Path to CA private key file

        Raises:
            RuntimeError: If CA is not initialized
        """
        db = get_db()
        ca_config = db(db.ssh_ca_config.ca_type == "user").select().first()

        if not ca_config:
            raise RuntimeError("SSH CA not initialized")

        # Get path from database
        private_key_path = Path(ca_config.private_key_vault_path)

        if not private_key_path.exists():
            raise RuntimeError(f"CA private key not found at {private_key_path}")

        return private_key_path


HOST_PRINCIPAL_PREFIX = "host:"
ALLOW_ROOT_PRINCIPAL = "allow-root"
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}$")


def validate_principal_syntax(principal: str) -> None:
    """Reject reserved (``host:``/``allow-root``) or malformed principals.

    Raises:
        ValueError: if the principal is reserved, or contains commas, whitespace,
            control characters or anything outside a conservative login charset.
    """
    if not isinstance(principal, str):
        raise ValueError("principal must be a string")
    if principal.startswith(HOST_PRINCIPAL_PREFIX) or principal == ALLOW_ROOT_PRINCIPAL:
        raise ValueError(f"principal {principal!r} is reserved")
    if not _PRINCIPAL_RE.match(principal):
        raise ValueError("principal contains invalid characters")


def generate_key_id(user_email: str, resource_id: str) -> str:
    """Generate unique certificate key ID.

    Args:
        user_email: User's email address
        resource_id: Resource identifier (e.g., VM ID, host ID)

    Returns:
        Unique key ID in format: user@resource-timestamp
    """
    timestamp = int(datetime.utcnow().timestamp())
    return f"{user_email}@{resource_id}-{timestamp}"


def validate_principals(principals: List[str], allowed_principals: List[str]) -> bool:
    """Validate principals against allowed list.

    Args:
        principals: Requested principals
        allowed_principals: List of allowed principals

    Returns:
        True if all principals are allowed, False otherwise
    """
    if not principals:
        return False

    if not allowed_principals:
        return False

    # Check if all requested principals are in allowed list
    return all(p in allowed_principals for p in principals)
