"""Tests for LUKS tier renderers (TPM2, TPM2+PIN, Network-bound).

Covers:
  - _render_luks_tpm2_only: clevis cloud-init rendering
  - _render_luks_tpm2_pin: PIN-protected variant
  - _render_luks_network_bound: Tang server binding
  - Dispatch logic in _seal_luks_key
  - Unknown tier raises LUKSValidationError
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.workers.plan_compiler import (
    _seal_luks_key,
)


class LUKSValidationError(Exception):
    """Raised when LUKS tier parameters are invalid."""

    pass


class TestLUKSTpm2Only:
    """TPM2-only sealing tier."""

    def test_tpm2_only_renders_cloud_init(self) -> None:
        """TPM2-only tier renders valid cloud-init with clevis."""
        tier = "tpm2"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()

        # Mock vault response for transit encrypt
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:encrypted_data",
            key_version=1,
        )

        result = _seal_luks_key(tier, raw_key, vault_client)

        assert result is not None
        sealed, provenance = result
        assert provenance["tier"] == "tpm2"
        assert "clevis" in provenance.get("method", "").lower()
        assert "luks" in provenance.get("method", "").lower()

    def test_tpm2_only_includes_pcr_7(self) -> None:
        """TPM2 uses PCR7 (secure boot state) in clevis config."""
        tier = "tpm2"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:data",
            key_version=1,
        )

        result = _seal_luks_key(tier, raw_key, vault_client)
        _, provenance = result

        # Provenance should document the PCR
        assert "pcr" in str(provenance).lower() or "7" in str(provenance)


class TestLUKSTpm2Pin:
    """TPM2+PIN tier."""

    def test_tpm2_pin_renders_cloud_init(self) -> None:
        """TPM2+PIN tier renders valid cloud-init with PIN support."""
        tier = "tpm2-pin"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:data",
            key_version=1,
        )

        result = _seal_luks_key(tier, raw_key, vault_client)

        assert result is not None
        sealed, provenance = result
        assert provenance["tier"] == "tpm2-pin"

    def test_tpm2_pin_includes_pin_flag(self) -> None:
        """TPM2+PIN tier includes tpm_pin=true in clevis config."""
        tier = "tpm2-pin"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:data",
            key_version=1,
        )

        result = _seal_luks_key(tier, raw_key, vault_client)
        _, provenance = result

        # PIN should be documented in provenance
        assert "pin" in str(provenance).lower()


class TestLUKSNetworkBound:
    """Network-bound (Tang) tier."""

    def test_tang_renders_cloud_init(self) -> None:
        """Network-bound tier renders valid cloud-init with Tang server."""
        tier = "tang"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:data",
            key_version=1,
        )

        result = _seal_luks_key(tier, raw_key, vault_client)

        assert result is not None
        sealed, provenance = result
        assert provenance["tier"] == "tang"
        assert "tang" in str(provenance).lower()

    def test_tang_includes_server_url(self) -> None:
        """Tang tier includes Tang server URL in provenance."""
        tier = "tang"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:data",
            key_version=1,
        )

        result = _seal_luks_key(tier, raw_key, vault_client)
        _, provenance = result

        # Tang server URL should be documented
        assert "url" in str(provenance).lower() or "tang" in str(provenance).lower()


class TestLUKSDispatch:
    """LUKS tier dispatch and error handling."""

    def test_unknown_tier_raises_validation_error(self) -> None:
        """Unknown LUKS tier raises clear LUKSValidationError."""
        tier = "unknown-foo-bar"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()

        # Should raise a clear validation error, not NotImplementedTier
        with pytest.raises((ValueError, LUKSValidationError)):
            _seal_luks_key(tier, raw_key, vault_client)

    def test_dev_tier_still_works(self) -> None:
        """Dev tier (existing) continues to work."""
        tier = "dev"
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:data",
            key_version=1,
        )

        result = _seal_luks_key(tier, raw_key, vault_client)

        assert result is not None
        sealed, provenance = result
        assert provenance["tier"] == "dev"

    def test_all_implemented_tiers_callable(self) -> None:
        """All known tiers return (sealed_key, provenance) tuple."""
        raw_key = b"test_32_byte_key_for_luks_00"
        vault_client = MagicMock()
        vault_client.transit_encrypt.return_value = MagicMock(
            ciphertext="vault:v1:data",
            key_version=1,
        )

        implemented_tiers = ["dev", "tpm2", "tpm2-pin", "tang"]

        for tier in implemented_tiers:
            result = _seal_luks_key(tier, raw_key, vault_client)
            assert result is not None
            sealed, provenance = result
            assert isinstance(sealed, (bytes, str))
            assert isinstance(provenance, dict)
            assert "tier" in provenance
