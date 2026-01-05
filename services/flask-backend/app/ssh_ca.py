"""
SSH Certificate Authority implementation for signing SSH public keys.

This module provides CA functionality for issuing short-lived SSH certificates
with principal-based access control and configurable validity periods.
"""

import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, current_app

from app.models import db, SSHCAConfig


class SSHCertificateAuthority:
    """
    SSH Certificate Authority for signing user SSH public keys.

    Generates and manages RSA 4096-bit CA key pairs, signs user public keys
    with configurable principals and validity periods.
    """

    DEFAULT_VALIDITY_SECONDS = 3600  # 1 hour
    MAX_VALIDITY_SECONDS = 28800  # 8 hours
    CA_KEY_SIZE = 4096

    def __init__(self, app: Optional[Flask] = None):
        """
        Initialize SSH CA.

        Args:
            app: Optional Flask application instance
        """
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """
        Initialize SSH CA with Flask application.

        Args:
            app: Flask application instance
        """
        self.app = app
        app.extensions = getattr(app, 'extensions', {})
        app.extensions['ssh_ca'] = self

        with app.app_context():
            # Check if CA exists, initialize if needed
            ca_config = SSHCAConfig.query.first()
            if not ca_config:
                app.logger.info("SSH CA not found, initializing new CA")
                self.initialize_ca()

    def initialize_ca(self) -> None:
        """
        Generate new RSA 4096-bit CA key pair.

        Stores public key in database (ssh_ca_config table) and private key
        path for Vault integration. Creates temporary private key file for now.
        """
        current_app.logger.info("Generating SSH CA RSA 4096-bit key pair")

        # Generate RSA private key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=self.CA_KEY_SIZE,
            backend=default_backend()
        )

        # Serialize private key to OpenSSH format
        private_key_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption()
        )

        # Serialize public key to OpenSSH format
        public_key = private_key.public_key()
        public_key_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH
        )

        # Store private key in temporary location (Vault integration pending)
        ca_dir = Path("/var/lib/gough/ssh_ca")
        ca_dir.mkdir(parents=True, exist_ok=True)
        private_key_path = ca_dir / "ca_key"

        private_key_path.write_bytes(private_key_bytes)
        os.chmod(private_key_path, 0o600)

        current_app.logger.info(
            f"SSH CA private key stored at {private_key_path}"
        )

        # Store public key and metadata in database
        ca_config = SSHCAConfig.query.first()
        if ca_config:
            ca_config.public_key = public_key_bytes.decode('utf-8')
            ca_config.private_key_path = str(private_key_path)
            ca_config.created_at = datetime.utcnow()
        else:
            ca_config = SSHCAConfig(
                public_key=public_key_bytes.decode('utf-8'),
                private_key_path=str(private_key_path),
                created_at=datetime.utcnow()
            )
            db.session.add(ca_config)

        db.session.commit()
        current_app.logger.info("SSH CA initialized successfully")

    def get_ca_public_key(self) -> str:
        """
        Retrieve CA public key from database.

        Returns:
            CA public key in OpenSSH format

        Raises:
            RuntimeError: If CA is not initialized
        """
        ca_config = SSHCAConfig.query.first()
        if not ca_config:
            raise RuntimeError("SSH CA not initialized")

        return ca_config.public_key

    def sign_user_key(
        self,
        public_key: str,
        principals: List[str],
        validity_seconds: int,
        key_id: str
    ) -> str:
        """
        Sign user SSH public key using ssh-keygen.

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

        current_app.logger.info(
            f"Signing SSH key for principals {principals}, "
            f"validity {validity_seconds}s, key_id {key_id}"
        )

        # Get CA private key
        ca_private_key_path = self._get_private_key_from_vault()

        # Create temporary files for user public key and certificate
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Write user public key to temp file
            user_key_path = tmpdir_path / "user_key.pub"
            user_key_path.write_text(public_key)

            # Certificate output path
            cert_path = tmpdir_path / "user_key-cert.pub"

            # Build ssh-keygen command
            principals_str = ",".join(principals)
            cmd = [
                "ssh-keygen",
                "-s", str(ca_private_key_path),
                "-I", key_id,
                "-n", principals_str,
                "-V", f"+{validity_seconds}s",
                "-z", str(int(datetime.utcnow().timestamp())),
                str(user_key_path)
            ]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30
                )
                current_app.logger.debug(f"ssh-keygen output: {result.stdout}")
            except subprocess.CalledProcessError as e:
                current_app.logger.error(
                    f"ssh-keygen failed: {e.stderr}"
                )
                raise RuntimeError(f"Failed to sign SSH key: {e.stderr}")
            except subprocess.TimeoutExpired:
                raise RuntimeError("SSH key signing timed out")

            # Read signed certificate
            if not cert_path.exists():
                raise RuntimeError("Certificate file not created")

            signed_cert = cert_path.read_text().strip()
            current_app.logger.info(
                f"Successfully signed SSH certificate: {key_id}"
            )

            return signed_cert

    def _get_private_key_from_vault(self) -> Path:
        """
        Retrieve CA private key from Vault.

        Placeholder for Vault integration. Currently returns temporary
        file path.

        Returns:
            Path to CA private key file

        Raises:
            RuntimeError: If CA is not initialized
        """
        ca_config = SSHCAConfig.query.first()
        if not ca_config:
            raise RuntimeError("SSH CA not initialized")

        # TODO: Integrate with HashiCorp Vault for secure key storage
        # For now, use local file path
        private_key_path = Path(ca_config.private_key_path)

        if not private_key_path.exists():
            raise RuntimeError(
                f"CA private key not found at {private_key_path}"
            )

        return private_key_path


def generate_key_id(user_email: str, resource_id: str) -> str:
    """
    Generate unique certificate key ID.

    Args:
        user_email: User's email address
        resource_id: Resource identifier (e.g., VM ID, host ID)

    Returns:
        Unique key ID in format: user@resource-timestamp
    """
    timestamp = int(datetime.utcnow().timestamp())
    return f"{user_email}@{resource_id}-{timestamp}"


def validate_principals(
    principals: List[str],
    allowed_principals: List[str]
) -> bool:
    """
    Validate principals against allowed list.

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
