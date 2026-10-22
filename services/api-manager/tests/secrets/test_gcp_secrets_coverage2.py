"""Additional coverage for app/secrets/gcp_secrets.py.

Targets missed lines 52-53 (ImportError handling).
"""

from __future__ import annotations

import pytest
import sys
from unittest.mock import MagicMock, patch


class TestGCPSecretsManagerImportError:
    """Tests for GCP Secrets Manager import error handling."""

    def test_client_property_import_error(self, mock_current_app, monkeypatch):
        """Test client property when google-cloud-secret-manager not installed (line 52-56)."""
        from app.secrets.gcp_secrets import GCPSecretsManager, SecretsManagerError

        manager = GCPSecretsManager()

        # Mock secretmanager import to raise ImportError
        def mock_import(*args, **kwargs):
            if args[0] == "google.cloud":
                raise ImportError("No module named google.cloud")
            return MagicMock()

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(SecretsManagerError) as exc:
                _ = manager.client

            assert "google-cloud-secret-manager" in str(exc.value)
            assert "not installed" in str(exc.value)


class TestGCPSecretsManagerProjectId:
    """Tests for GCP project ID validation."""

    def test_project_id_missing(self, mock_current_app, monkeypatch):
        """Test project_id when GCP_PROJECT_ID not configured (line 63-64)."""
        from app.secrets.gcp_secrets import GCPSecretsManager, SecretsManagerError

        # Clear GCP_PROJECT_ID from config
        mock_current_app.config["GCP_PROJECT_ID"] = ""

        manager = GCPSecretsManager()

        with pytest.raises(SecretsManagerError) as exc:
            _ = manager.project_id

        assert "GCP_PROJECT_ID" in str(exc.value)
        assert "not configured" in str(exc.value)

    def test_project_id_set(self, mock_current_app):
        """Test project_id when properly configured."""
        from app.secrets.gcp_secrets import GCPSecretsManager

        mock_current_app.config["GCP_PROJECT_ID"] = "my-project"

        manager = GCPSecretsManager()
        assert manager.project_id == "my-project"


class TestGCPSecretsManagerNormalizeName:
    """Tests for _normalize_name path conversion."""

    def test_normalize_name_slashes(self, mock_current_app):
        """Test that forward slashes are converted to dashes."""
        from app.secrets.gcp_secrets import GCPSecretsManager

        manager = GCPSecretsManager()
        result = manager._normalize_name("cloud/aws/credentials")
        assert result == "cloud-aws-credentials"

    def test_normalize_name_dots(self, mock_current_app):
        """Test that dots are converted to dashes."""
        from app.secrets.gcp_secrets import GCPSecretsManager

        manager = GCPSecretsManager()
        result = manager._normalize_name("db.password.prod")
        assert result == "db-password-prod"

    def test_normalize_name_mixed(self, mock_current_app):
        """Test normalization with mixed slashes and dots."""
        from app.secrets.gcp_secrets import GCPSecretsManager

        manager = GCPSecretsManager()
        result = manager._normalize_name("cloud/aws.creds/production")
        assert result == "cloud-aws-creds-production"

    def test_normalize_name_no_special_chars(self, mock_current_app):
        """Test normalization with no special characters."""
        from app.secrets.gcp_secrets import GCPSecretsManager

        manager = GCPSecretsManager()
        result = manager._normalize_name("simple_name")
        assert result == "simple_name"
