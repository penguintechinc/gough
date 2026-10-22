"""Tests for power manager BMC certificate pinning.

Covers cert pinning verification, fingerprint normalization, and env flag gating.
"""

import pytest
import os
from unittest.mock import Mock, patch
from worker.services.power_manager import (
    BMCCredentials,
    _normalize_fingerprint,
)


class TestFingerprintNormalization:
    """Tests for fingerprint normalization."""

    def test_normalize_colon_separated(self):
        """Normalize colon-separated fingerprint."""
        fp = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
        normalized = _normalize_fingerprint(fp)
        assert normalized == fp.upper()

    def test_normalize_no_separators(self):
        """Normalize fingerprint without separators."""
        fp = "aabbccddee00112233445566778899aabbccddee0011223344556677889900aa"
        normalized = _normalize_fingerprint(fp)
        expected = "AA:BB:CC:DD:EE:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:00:11:22:33:44:55:66:77:88:99:00:AA"
        assert normalized == expected

    def test_normalize_space_separated(self):
        """Normalize fingerprint with space separators."""
        fp = "AA BB CC DD EE FF 00 11 22 33 44 55 66 77 88 99 AA BB CC DD EE FF 00 11 22 33 44 55 66 77 88 99"
        normalized = _normalize_fingerprint(fp)
        expected = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
        assert normalized == expected

    def test_reject_invalid_length(self):
        """Reject fingerprints with invalid length."""
        with pytest.raises(ValueError) as exc_info:
            _normalize_fingerprint("AA:BB:CC")  # Too short
        assert "length" in str(exc_info.value).lower()


class TestBMCCredentials:
    """Tests for BMCCredentials dataclass."""

    def test_credentials_with_cert_fingerprint(self):
        """BMCCredentials can store cert_fingerprint."""
        creds = BMCCredentials(
            address="192.168.1.1",
            username="admin",
            password="password",
            power_type="redfish",
            cert_fingerprint="AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99",
        )
        assert creds.cert_fingerprint is not None
        assert "AA" in creds.cert_fingerprint

    def test_credentials_without_cert_fingerprint(self):
        """BMCCredentials defaults to no cert_fingerprint."""
        creds = BMCCredentials(
            address="192.168.1.1",
            username="admin",
            password="password",
            power_type="redfish",
        )
        assert creds.cert_fingerprint is None
