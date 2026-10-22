"""Coverage for app/secrets/__init__.py backend registration (lines 91-92, 97-98, 103-104, 109-110, 115-116)."""

from __future__ import annotations

import pytest
import sys
from unittest.mock import MagicMock, patch, AsyncMock


class TestBackendRegistration:
    """Tests for backend registration in _ensure_backends_registered."""

    def test_ensure_backends_early_exit_on_reentry(self, mock_current_app):
        """Test early exit when backends already registered (line 81-82)."""
        from app.secrets import _ensure_backends_registered, _BACKENDS

        # First call populates backends
        _BACKENDS.clear()
        _ensure_backends_registered()
        backend_count_1 = len(_BACKENDS)

        # Second call returns early, count stays same
        _ensure_backends_registered()
        backend_count_2 = len(_BACKENDS)

        assert backend_count_1 == backend_count_2
        assert backend_count_1 > 0

    def test_register_backend_function(self, mock_current_app):
        """Test register_backend function registers a backend."""
        from app.secrets import register_backend, _BACKENDS

        class DummyBackend:
            pass

        _BACKENDS.clear()
        register_backend("test_dummy", DummyBackend)

        assert "test_dummy" in _BACKENDS
        assert _BACKENDS["test_dummy"] is DummyBackend




class TestGetSecretsManager:
    """Tests for get_secrets_manager function."""

    @pytest.mark.asyncio
    async def test_get_secrets_manager_unknown_backend_error_message(self, mock_current_app):
        """Test get_secrets_manager error message format (line 70-72)."""
        from app.secrets import get_secrets_manager, _ensure_backends_registered, _BACKENDS

        # First populate _BACKENDS so we get the proper error message
        _ensure_backends_registered()

        with pytest.raises(ValueError) as exc:
            await get_secrets_manager(backend="nonexistent_backend_xyz")

        error_str = str(exc.value)
        assert "Unknown secrets backend" in error_str
        assert "Available:" in error_str


class TestGetBackendRegistry:
    """Tests for get_backend_registry function."""

    def test_get_backend_registry(self, mock_current_app):
        """Test get_backend_registry returns registry dict."""
        from app.secrets import get_backend_registry, _BACKENDS

        _BACKENDS.clear()  # Force re-registration

        registry = get_backend_registry()
        assert isinstance(registry, dict)
        assert "encrypted_db" in registry
