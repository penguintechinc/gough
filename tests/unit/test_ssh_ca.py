"""Unit tests for ssh_ca.py module - SSH Certificate Authority.

Tests for:
- SSHCAException
- SSHCertificateAuthority class
- generate_key_id function
- validate_principals function
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, Mock
from datetime import datetime

import pytest

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

from app.ssh_ca import (
    SSHCAException,
    SSHCertificateAuthority,
    generate_key_id,
    validate_principals,
)


class TestSSHCAException:
    """Tests for SSHCAException."""

    def test_ssh_ca_exception_creation(self):
        """Test creating SSHCAException."""
        exc = SSHCAException("Test error")
        assert str(exc) == "Test error"

    def test_ssh_ca_exception_inheritance(self):
        """Test SSHCAException is Exception subclass."""
        exc = SSHCAException("Test")
        assert isinstance(exc, Exception)


class TestSSHCertificateAuthority:
    """Tests for SSHCertificateAuthority class."""

    def test_ssh_ca_constants(self):
        """Test SSH CA constants."""
        assert SSHCertificateAuthority.DEFAULT_VALIDITY_SECONDS == 3600
        assert SSHCertificateAuthority.MAX_VALIDITY_SECONDS == 28800
        assert SSHCertificateAuthority.CA_KEY_SIZE == 4096

    def test_ssh_ca_init_without_app(self):
        """Test initializing SSHCertificateAuthority without app."""
        ca = SSHCertificateAuthority()
        assert ca.app is None

    def test_ssh_ca_init_with_app(self):
        """Test initializing SSHCertificateAuthority with app."""
        mock_app = MagicMock()
        mock_app.extensions = {}

        ca = SSHCertificateAuthority(mock_app)
        assert ca.app is not None

    def test_ssh_ca_init_app(self):
        """Test init_app method."""
        mock_app = MagicMock()
        mock_app.extensions = {}

        ca = SSHCertificateAuthority()
        ca.init_app(mock_app)

        assert ca.app is mock_app
        assert mock_app.extensions["ssh_ca"] is ca

    def test_ensure_ca_initialized_structure(self):
        """Test ensure_ca_initialized is defined correctly."""
        mock_app = MagicMock()
        ca = SSHCertificateAuthority(mock_app)

        # Check method exists and is async
        assert hasattr(ca, 'ensure_ca_initialized')
        assert callable(ca.ensure_ca_initialized)

class TestGenerateKeyId:
    """Tests for generate_key_id function."""

    def test_generate_key_id_format(self):
        """Test generate_key_id generates proper format."""
        key_id = generate_key_id("user@example.com", "vm-12345")

        assert "user@example.com" in key_id
        assert "vm-12345" in key_id
        assert "@" in key_id
        assert "-" in key_id

    def test_generate_key_id_includes_timestamp(self):
        """Test generate_key_id includes timestamp."""
        before = int(datetime.utcnow().timestamp())
        key_id = generate_key_id("user@example.com", "resource-1")
        after = int(datetime.utcnow().timestamp())

        # Extract timestamp from key_id (last component after last dash)
        timestamp_str = key_id.split("-")[-1]
        timestamp = int(timestamp_str)

        assert before <= timestamp <= after

    def test_generate_key_id_consistency(self):
        """Test generate_key_id produces valid format consistently."""
        key_id_1 = generate_key_id("user@example.com", "resource-1")
        key_id_2 = generate_key_id("user@example.com", "resource-1")

        # Both should have the required format components
        assert "user@example.com" in key_id_1
        assert "resource-1" in key_id_1
        assert "@" in key_id_1
        assert "-" in key_id_1

        assert "user@example.com" in key_id_2
        assert "resource-1" in key_id_2


class TestValidatePrincipals:
    """Tests for validate_principals function."""

    def test_validate_principals_all_allowed(self):
        """Test validate_principals returns True when all allowed."""
        result = validate_principals(
            principals=["user", "admin"],
            allowed_principals=["user", "admin", "service"],
        )
        assert result is True

    def test_validate_principals_subset_allowed(self):
        """Test validate_principals returns True for subset."""
        result = validate_principals(
            principals=["user"],
            allowed_principals=["user", "admin", "service"],
        )
        assert result is True

    def test_validate_principals_not_allowed(self):
        """Test validate_principals returns False when not allowed."""
        result = validate_principals(
            principals=["user", "forbidden"],
            allowed_principals=["user", "admin"],
        )
        assert result is False

    def test_validate_principals_single_not_allowed(self):
        """Test validate_principals returns False for single forbidden principal."""
        result = validate_principals(
            principals=["forbidden"],
            allowed_principals=["user", "admin"],
        )
        assert result is False

    def test_validate_principals_empty_principals(self):
        """Test validate_principals returns False with empty principals."""
        result = validate_principals(
            principals=[],
            allowed_principals=["user", "admin"],
        )
        assert result is False

    def test_validate_principals_empty_allowed(self):
        """Test validate_principals returns False with empty allowed list."""
        result = validate_principals(
            principals=["user"],
            allowed_principals=[],
        )
        assert result is False

    def test_validate_principals_exact_match(self):
        """Test validate_principals with exact match."""
        result = validate_principals(
            principals=["user"],
            allowed_principals=["user"],
        )
        assert result is True


class TestSSHCAAsyncMethods:
    """Tests for async methods structure."""

    def test_ensure_ca_initialized_is_defined(self):
        """Test ensure_ca_initialized method exists."""
        ca = SSHCertificateAuthority()
        assert hasattr(ca, "ensure_ca_initialized")
        assert callable(ca.ensure_ca_initialized)

    def test_initialize_ca_is_defined(self):
        """Test initialize_ca method exists."""
        ca = SSHCertificateAuthority()
        assert hasattr(ca, "initialize_ca")
        assert callable(ca.initialize_ca)

    def test_sign_user_key_is_defined(self):
        """Test sign_user_key method exists."""
        ca = SSHCertificateAuthority()
        assert hasattr(ca, "sign_user_key")
        assert callable(ca.sign_user_key)


class TestSSHCAPublicKey:
    """Tests for get_ca_public_key method."""

    def test_get_ca_public_key_is_defined(self):
        """Test get_ca_public_key method exists."""
        ca = SSHCertificateAuthority()
        assert hasattr(ca, "get_ca_public_key")
        assert callable(ca.get_ca_public_key)


class TestSSHCASignKey:
    """Tests for sign_user_key async method."""

    def test_sign_user_key_validity_exceeds_max(self):
        """Test sign_user_key rejects validity exceeding maximum."""
        ca = SSHCertificateAuthority()

        with pytest.raises(ValueError) as exc_info:
            asyncio.run(ca.sign_user_key(
                public_key="ssh-rsa AAAAB3...",
                principals=["user"],
                validity_seconds=999999,  # Exceeds MAX_VALIDITY_SECONDS
                key_id="key123",
            ))

        assert "exceeds maximum" in str(exc_info.value)

    def test_sign_user_key_no_principals(self):
        """Test sign_user_key rejects empty principals."""
        ca = SSHCertificateAuthority()

        with pytest.raises(ValueError) as exc_info:
            asyncio.run(ca.sign_user_key(
                public_key="ssh-rsa AAAAB3...",
                principals=[],  # Empty
                validity_seconds=3600,
                key_id="key123",
            ))

        assert "At least one principal" in str(exc_info.value)

    @patch("app.ssh_ca.SSHCertificateAuthority._get_private_key_path")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    @patch("subprocess.run")
    def test_sign_user_key_success(
        self,
        mock_run,
        mock_exists,
        mock_read_text,
        mock_write_text,
        mock_get_key_path,
    ):
        """Test sign_user_key successfully signs a key."""
        mock_get_key_path.return_value = Path("/var/lib/gough/ssh_ca/ca_key")
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_exists.return_value = True
        mock_read_text.return_value = "ssh-rsa-cert-v01@openssh.com..."

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        ca = SSHCertificateAuthority()
        ca.app = mock_app

        with patch("app.ssh_ca.current_app", mock_app):
            result = asyncio.run(ca.sign_user_key(
                public_key="ssh-rsa AAAAB3...",
                principals=["user"],
                validity_seconds=3600,
                key_id="key123",
            ))

        assert isinstance(result, str)
        assert mock_run.called

    @patch("app.ssh_ca.SSHCertificateAuthority._get_private_key_path")
    @patch("pathlib.Path.write_text")
    @patch("subprocess.run")
    def test_sign_user_key_subprocess_error(
        self, mock_run, mock_write_text, mock_get_key_path
    ):
        """Test sign_user_key handles subprocess errors."""
        mock_get_key_path.return_value = Path("/var/lib/gough/ssh_ca/ca_key")
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "ssh-keygen", stderr="Key format error"
        )

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        ca = SSHCertificateAuthority()
        ca.app = mock_app

        with patch("app.ssh_ca.current_app", mock_app):
            with pytest.raises(RuntimeError) as exc_info:
                asyncio.run(ca.sign_user_key(
                    public_key="ssh-rsa AAAAB3...",
                    principals=["user"],
                    validity_seconds=3600,
                    key_id="key123",
                ))

            assert "Failed to sign SSH key" in str(exc_info.value)

    @patch("app.ssh_ca.SSHCertificateAuthority._get_private_key_path")
    @patch("pathlib.Path.write_text")
    @patch("subprocess.run")
    def test_sign_user_key_timeout(
        self, mock_run, mock_write_text, mock_get_key_path
    ):
        """Test sign_user_key handles timeout."""
        mock_get_key_path.return_value = Path("/var/lib/gough/ssh_ca/ca_key")
        mock_run.side_effect = subprocess.TimeoutExpired("ssh-keygen", 30)

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        ca = SSHCertificateAuthority()
        ca.app = mock_app

        with patch("app.ssh_ca.current_app", mock_app):
            with pytest.raises(RuntimeError) as exc_info:
                asyncio.run(ca.sign_user_key(
                    public_key="ssh-rsa AAAAB3...",
                    principals=["user"],
                    validity_seconds=3600,
                    key_id="key123",
                ))

            assert "timed out" in str(exc_info.value)

    @patch("app.ssh_ca.SSHCertificateAuthority._get_private_key_path")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.exists")
    @patch("subprocess.run")
    def test_sign_user_key_cert_not_created(
        self, mock_run, mock_exists, mock_write_text, mock_get_key_path
    ):
        """Test sign_user_key handles missing certificate file."""
        mock_get_key_path.return_value = Path("/var/lib/gough/ssh_ca/ca_key")
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_exists.return_value = False  # Cert file doesn't exist

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        ca = SSHCertificateAuthority()
        ca.app = mock_app

        with patch("app.ssh_ca.current_app", mock_app):
            with pytest.raises(RuntimeError) as exc_info:
                asyncio.run(ca.sign_user_key(
                    public_key="ssh-rsa AAAAB3...",
                    principals=["user"],
                    validity_seconds=3600,
                    key_id="key123",
                ))

            assert "not created" in str(exc_info.value)


class TestSSHCAPrivateKeyPath:
    """Tests for _get_private_key_path method."""

    def test_get_private_key_path_is_defined(self):
        """Test _get_private_key_path method exists."""
        ca = SSHCertificateAuthority()
        assert hasattr(ca, "_get_private_key_path")
        assert callable(ca._get_private_key_path)
