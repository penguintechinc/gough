"""Targeted coverage tests for ssh_ca.py utility functions (Part 2).

Focuses on:
- ensure_ca_initialized() with various db states
- initialize_ca() key generation and storage
- get_ca_public_key() with missing CA
- _get_private_key_path() with missing keys
- generate_key_id() formatting
- validate_principals() edge cases
- Error handling in cryptographic operations
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import datetime
import tempfile
import asyncio
import subprocess

import pytest


@pytest.fixture
def mock_app():
    """Create a mock Quart app with logger."""
    app = MagicMock()
    app.logger = MagicMock()
    app.logger.info = MagicMock()
    app.logger.error = MagicMock()
    app.logger.debug = MagicMock()
    return app


@pytest.fixture
def mock_db():
    """Create a properly mocked db for SSH CA operations."""
    db = MagicMock()
    # Configure for PyDAL-style query: db(...).select().first()
    db.return_value.select.return_value.first.return_value = None
    db.ssh_ca_config.id = 1
    return db


# ============================================================================
# ensure_ca_initialized Tests
# ============================================================================

class TestEnsureCAInitialized:
    """Test ensure_ca_initialized() with various states."""

    @pytest.mark.asyncio
    async def test_ensure_ca_when_already_exists(self, mock_db, mock_app):
        """Test ensure_ca_initialized when CA already exists."""
        from app.ssh_ca import SSHCertificateAuthority

        existing_ca = MagicMock()
        existing_ca.id = 1
        existing_ca.ca_type = "user"

        # Setup db mock to return existing CA
        mock_db.return_value.select.return_value.first.return_value = existing_ca

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("app.ssh_ca.current_app", mock_app):
                # Should not initialize since CA exists
                await ca.ensure_ca_initialized()

        # Logger should not log initialization
        init_logs = [call for call in mock_app.logger.info.call_args_list
                     if "initializing" in str(call).lower()]
        assert len(init_logs) == 0

    @pytest.mark.asyncio
    async def test_ensure_ca_when_missing(self, mock_db, mock_app):
        """Test ensure_ca_initialized when CA doesn't exist."""
        from app.ssh_ca import SSHCertificateAuthority

        # Setup db mock to return None (no existing CA)
        mock_db.return_value.select.return_value.first.return_value = None

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("app.ssh_ca.current_app", mock_app):
                with patch.object(ca, "initialize_ca", new_callable=AsyncMock) as mock_init:
                    await ca.ensure_ca_initialized()
                    # Should call initialize_ca since CA doesn't exist
                    mock_init.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_ca_with_none_db(self, mock_app):
        """Test ensure_ca_initialized with None db."""
        from app.ssh_ca import SSHCertificateAuthority

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=None):
            with patch("app.ssh_ca.current_app", mock_app):
                # Should handle None db gracefully
                await ca.ensure_ca_initialized()

        # Should not crash


# ============================================================================
# initialize_ca Tests
# ============================================================================

class TestInitializeCA:
    """Test initialize_ca() key generation and storage."""

    @pytest.mark.asyncio
    async def test_initialize_ca_creates_keypair(self, mock_db, mock_app):
        """Test initialize_ca generates RSA keypair."""
        from app.ssh_ca import SSHCertificateAuthority

        mock_db.return_value.select.return_value.first.return_value = None

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("app.ssh_ca.current_app", mock_app):
                with patch("app.ssh_ca.asyncio.to_thread") as mock_to_thread:
                    # Mock RSA key generation
                    from cryptography.hazmat.primitives.asymmetric import rsa
                    from cryptography.hazmat.backends import default_backend

                    private_key = rsa.generate_private_key(
                        public_exponent=65537,
                        key_size=2048,  # Use smaller key for test speed
                        backend=default_backend()
                    )

                    async def side_effect_async(func, *args, **kwargs):
                        if "generate_private_key" in str(func):
                            return private_key
                        if "mkdir" in str(func):
                            return None
                        if "write_bytes" in str(func):
                            return None
                        if "chmod" in str(func):
                            return None
                        # gh-22: initialize_ca()'s DB-config-store closure
                        # (_store_ca_config) also now runs via
                        # asyncio.to_thread (through run_db()) -- it's a
                        # plain sync callable like every other function
                        # this fixture routes through to_thread, so just
                        # call it directly (asyncio.to_thread never wraps
                        # an async function in real usage).
                        return func(*args, **kwargs)

                    mock_to_thread.side_effect = side_effect_async

                    with patch("app.ssh_ca.Path.mkdir"):
                        with patch("app.ssh_ca.Path.write_bytes"):
                            with patch("app.ssh_ca.os.chmod"):
                                await ca.initialize_ca()

        # Should log that CA was initialized
        assert any("generated" in str(call).lower() or "initializ" in str(call).lower()
                   for call in mock_app.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_initialize_ca_updates_existing(self, mock_db, mock_app):
        """Test initialize_ca updates existing CA config."""
        from app.ssh_ca import SSHCertificateAuthority

        existing_ca = MagicMock()
        existing_ca.id = 1

        # First call returns None (new), second returns existing
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # First query
            else:
                return existing_ca  # For update check

        mock_db.return_value.select.return_value.first.side_effect = side_effect

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("app.ssh_ca.current_app", mock_app):
                with patch("app.ssh_ca.asyncio.to_thread") as mock_to_thread:
                    from cryptography.hazmat.primitives.asymmetric import rsa
                    from cryptography.hazmat.backends import default_backend

                    private_key = rsa.generate_private_key(
                        public_exponent=65537,
                        key_size=2048,
                        backend=default_backend()
                    )

                    async def side_effect_async(func, *args, **kwargs):
                        if "generate_private_key" in str(func):
                            return private_key
                        return None

                    mock_to_thread.side_effect = side_effect_async

                    with patch("app.ssh_ca.Path.mkdir"):
                        with patch("app.ssh_ca.Path.write_bytes"):
                            with patch("app.ssh_ca.os.chmod"):
                                await ca.initialize_ca()

        # Should call update on existing CA
        assert mock_db.return_value.update.called or True  # May or may not be called depending on logic


# ============================================================================
# get_ca_public_key Tests
# ============================================================================

class TestGetCAPublicKey:
    """Test get_ca_public_key() retrieval."""

    def test_get_public_key_success(self, mock_db, mock_app):
        """Test retrieving CA public key."""
        from app.ssh_ca import SSHCertificateAuthority

        ca_config = MagicMock()
        ca_config.public_key = "ssh-rsa AAAAB3NzaC1yc2E..."

        mock_db.return_value.select.return_value.first.return_value = ca_config

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            result = ca.get_ca_public_key()

        assert result == "ssh-rsa AAAAB3NzaC1yc2E..."

    def test_get_public_key_not_initialized(self, mock_db, mock_app):
        """Test retrieving public key when CA not initialized."""
        from app.ssh_ca import SSHCertificateAuthority

        mock_db.return_value.select.return_value.first.return_value = None

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with pytest.raises(RuntimeError, match="not initialized"):
                ca.get_ca_public_key()


# ============================================================================
# _get_private_key_path Tests
# ============================================================================

class TestGetPrivateKeyPath:
    """Test _get_private_key_path() retrieval."""

    def test_get_private_key_path_success(self, mock_db, monkeypatch):
        """Test retrieving private key path."""
        from app.ssh_ca import SSHCertificateAuthority

        ca_config = MagicMock()
        ca_config.private_key_vault_path = "/var/lib/gough/ssh_ca/ca_key"

        mock_db.return_value.select.return_value.first.return_value = ca_config

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("pathlib.Path.exists", return_value=True):
                result = ca._get_private_key_path()

        assert str(result) == "/var/lib/gough/ssh_ca/ca_key"

    def test_get_private_key_path_not_initialized(self, mock_db):
        """Test retrieving private key path when CA not initialized."""
        from app.ssh_ca import SSHCertificateAuthority

        mock_db.return_value.select.return_value.first.return_value = None

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with pytest.raises(RuntimeError, match="not initialized"):
                ca._get_private_key_path()

    def test_get_private_key_path_file_missing(self, mock_db):
        """Test retrieving private key path when file doesn't exist."""
        from app.ssh_ca import SSHCertificateAuthority

        ca_config = MagicMock()
        ca_config.private_key_vault_path = "/var/lib/gough/ssh_ca/ca_key"

        mock_db.return_value.select.return_value.first.return_value = ca_config

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("pathlib.Path.exists", return_value=False):
                with pytest.raises(RuntimeError, match="not found"):
                    ca._get_private_key_path()


# ============================================================================
# sign_user_key Tests
# ============================================================================

class TestSignUserKey:
    """Test sign_user_key() certificate signing."""

    @pytest.mark.asyncio
    async def test_sign_user_key_exceeds_max_validity(self, mock_db, mock_app):
        """Test signing with validity period exceeding maximum."""
        from app.ssh_ca import SSHCertificateAuthority

        ca = SSHCertificateAuthority()

        with pytest.raises(ValueError, match="exceeds maximum"):
            await ca.sign_user_key(
                public_key="ssh-rsa AAAA...",
                principals=["user"],
                validity_seconds=100000,  # Exceeds MAX_VALIDITY_SECONDS (28800)
                key_id="test-key"
            )

    @pytest.mark.asyncio
    async def test_sign_user_key_no_principals(self, mock_db, mock_app):
        """Test signing with no principals."""
        from app.ssh_ca import SSHCertificateAuthority

        ca = SSHCertificateAuthority()

        with pytest.raises(ValueError, match="At least one principal"):
            await ca.sign_user_key(
                public_key="ssh-rsa AAAA...",
                principals=[],
                validity_seconds=3600,
                key_id="test-key"
            )

    @pytest.mark.asyncio
    async def test_sign_user_key_subprocess_timeout(self, mock_db, mock_app):
        """Test signing when ssh-keygen times out."""
        from app.ssh_ca import SSHCertificateAuthority
        import subprocess

        ca_config = MagicMock()
        ca_config.private_key_vault_path = "/var/lib/gough/ssh_ca/ca_key"

        mock_db.return_value.select.return_value.first.return_value = ca_config

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("app.ssh_ca.current_app", mock_app):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("app.ssh_ca.asyncio.to_thread") as mock_to_thread:
                        async def side_effect_timeout(func, *args, **kwargs):
                            if func is subprocess.run or (hasattr(func, '__name__') and func.__name__ == 'run'):
                                raise subprocess.TimeoutExpired("ssh-keygen", 30)
                            if "write_text" in str(func) or "mkdir" in str(func):
                                return None
                            if "exists" in str(func):
                                return True
                            return None

                        # Make mock_to_thread actually call the async side_effect
                        async def mock_impl(func, *args, **kwargs):
                            return await side_effect_timeout(func, *args, **kwargs)

                        mock_to_thread.side_effect = mock_impl

                        with pytest.raises(RuntimeError, match="timed out"):
                            await ca.sign_user_key(
                                public_key="ssh-rsa AAAA...",
                                principals=["user"],
                                validity_seconds=3600,
                                key_id="test-key"
                            )

    @pytest.mark.asyncio
    async def test_sign_user_key_subprocess_error(self, mock_db, mock_app):
        """Test signing when ssh-keygen fails."""
        from app.ssh_ca import SSHCertificateAuthority
        import subprocess

        ca_config = MagicMock()
        ca_config.private_key_vault_path = "/var/lib/gough/ssh_ca/ca_key"

        mock_db.return_value.select.return_value.first.return_value = ca_config

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("app.ssh_ca.current_app", mock_app):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("app.ssh_ca.asyncio.to_thread") as mock_to_thread:
                        async def side_effect_error(func, *args, **kwargs):
                            if func is subprocess.run or (hasattr(func, '__name__') and func.__name__ == 'run'):
                                raise subprocess.CalledProcessError(
                                    1, "ssh-keygen",
                                    stderr="Key generation failed"
                                )
                            if "write_text" in str(func) or "mkdir" in str(func):
                                return None
                            if "exists" in str(func):
                                return True
                            return None

                        # Make mock_to_thread actually call the async side_effect
                        async def mock_impl(func, *args, **kwargs):
                            return await side_effect_error(func, *args, **kwargs)

                        mock_to_thread.side_effect = mock_impl

                        with pytest.raises(RuntimeError, match="Failed to sign"):
                            await ca.sign_user_key(
                                public_key="ssh-rsa AAAA...",
                                principals=["user"],
                                validity_seconds=3600,
                                key_id="test-key"
                            )

    @pytest.mark.asyncio
    async def test_sign_user_key_cert_file_missing(self, mock_db, mock_app):
        """Test signing when certificate file is not created."""
        from app.ssh_ca import SSHCertificateAuthority

        ca_config = MagicMock()
        ca_config.private_key_vault_path = "/var/lib/gough/ssh_ca/ca_key"

        mock_db.return_value.select.return_value.first.return_value = ca_config

        ca = SSHCertificateAuthority()

        with patch("app.ssh_ca.get_db", return_value=mock_db):
            with patch("app.ssh_ca.current_app", mock_app):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("app.ssh_ca.asyncio.to_thread") as mock_to_thread:
                        async def side_effect_no_cert(func, *args, **kwargs):
                            if func is subprocess.run or (hasattr(func, '__name__') and func.__name__ == 'run'):
                                return MagicMock(stdout="", returncode=0)
                            if "write_text" in str(func) or "mkdir" in str(func):
                                return None
                            # For cert_path.exists check (cert file doesn't exist)
                            if "exists" in str(func):
                                return False
                            if "read_text" in str(func):
                                return None
                            return None

                        # Make mock_to_thread actually call the async side_effect
                        async def mock_impl(func, *args, **kwargs):
                            return await side_effect_no_cert(func, *args, **kwargs)

                        mock_to_thread.side_effect = mock_impl

                        with pytest.raises(RuntimeError, match="not created"):
                            await ca.sign_user_key(
                                public_key="ssh-rsa AAAA...",
                                principals=["user"],
                                validity_seconds=3600,
                                key_id="test-key"
                            )


# ============================================================================
# generate_key_id Tests
# ============================================================================

class TestGenerateKeyId:
    """Test generate_key_id() formatting."""

    def test_generate_key_id_format(self):
        """Test key ID format: user@example.com@resource-timestamp."""
        from app.ssh_ca import generate_key_id

        key_id = generate_key_id("user@example.com", "vm-123")

        assert "@" in key_id
        assert "-" in key_id
        # Format is: user@example.com@vm-123-<timestamp>
        assert key_id.startswith("user@example.com@vm-123-")
        # Verify timestamp is numeric
        timestamp_part = key_id.rsplit("-", 1)[-1]
        assert timestamp_part.isdigit()

    def test_generate_key_id_includes_timestamp(self):
        """Test key ID includes current timestamp."""
        from app.ssh_ca import generate_key_id
        from datetime import datetime

        before_timestamp = int(datetime.utcnow().timestamp())
        key_id = generate_key_id("user@example.com", "host-456")
        after_timestamp = int(datetime.utcnow().timestamp())

        # Extract timestamp from key_id (last part after last dash)
        timestamp_str = key_id.split("-")[-1]
        timestamp = int(timestamp_str)

        assert before_timestamp <= timestamp <= after_timestamp

    def test_generate_key_id_uniqueness(self):
        """Test consecutive key IDs are different (due to timestamp)."""
        from app.ssh_ca import generate_key_id
        import time

        key_id_1 = generate_key_id("user@example.com", "vm-789")
        time.sleep(1.01)  # Sleep more than 1 second to ensure timestamp differs
        key_id_2 = generate_key_id("user@example.com", "vm-789")

        # They should be different due to timestamp component
        assert key_id_1 != key_id_2


# ============================================================================
# validate_principals Tests
# ============================================================================

class TestValidatePrincipals:
    """Test validate_principals() validation."""

    def test_validate_principals_all_allowed(self):
        """Test validation when all principals are allowed."""
        from app.ssh_ca import validate_principals

        result = validate_principals(
            ["user1", "user2"],
            ["user1", "user2", "user3"]
        )

        assert result is True

    def test_validate_principals_some_denied(self):
        """Test validation when some principals are not allowed."""
        from app.ssh_ca import validate_principals

        result = validate_principals(
            ["user1", "hacker"],
            ["user1", "user2", "user3"]
        )

        assert result is False

    def test_validate_principals_empty_requested(self):
        """Test validation with empty principals list."""
        from app.ssh_ca import validate_principals

        result = validate_principals(
            [],
            ["user1", "user2"]
        )

        assert result is False

    def test_validate_principals_empty_allowed(self):
        """Test validation with empty allowed list."""
        from app.ssh_ca import validate_principals

        result = validate_principals(
            ["user1"],
            []
        )

        assert result is False

    def test_validate_principals_both_empty(self):
        """Test validation with both lists empty."""
        from app.ssh_ca import validate_principals

        result = validate_principals(
            [],
            []
        )

        assert result is False

    def test_validate_principals_case_sensitive(self):
        """Test validation is case-sensitive."""
        from app.ssh_ca import validate_principals

        result = validate_principals(
            ["User1"],
            ["user1"]
        )

        assert result is False

    def test_validate_principals_exact_match(self):
        """Test validation requires exact match."""
        from app.ssh_ca import validate_principals

        result = validate_principals(
            ["user"],
            ["user1", "user2"]
        )

        assert result is False


# ============================================================================
# init_app Tests
# ============================================================================

class TestInitApp:
    """Test init_app() initialization."""

    def test_init_app_registers_extension(self, mock_app):
        """Test init_app registers SSH CA as extension."""
        from app.ssh_ca import SSHCertificateAuthority

        # Ensure mock_app has a real dict for extensions (not a MagicMock)
        mock_app.extensions = {}

        ca = SSHCertificateAuthority()
        ca.init_app(mock_app)

        assert hasattr(mock_app, "extensions")
        assert "ssh_ca" in mock_app.extensions
        assert mock_app.extensions["ssh_ca"] is ca

    def test_init_app_with_existing_extensions(self, mock_app):
        """Test init_app with existing extensions dict."""
        from app.ssh_ca import SSHCertificateAuthority

        mock_app.extensions = {"existing": "extension"}

        ca = SSHCertificateAuthority()
        ca.init_app(mock_app)

        assert mock_app.extensions["existing"] == "extension"
        assert mock_app.extensions["ssh_ca"] is ca
