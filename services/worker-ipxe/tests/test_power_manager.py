"""Tests for power manager BMC certificate pinning.

Covers cert pinning verification, fingerprint normalization, and env flag gating.
"""

import pytest
import os
from unittest.mock import Mock, patch
from worker.services.power_manager import (
    BMCCredentials,
    _get_bmc_cert_fingerprint,
    _normalize_fingerprint,
    _verify_bmc_cert,
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


class TestBMCCertVerification:
    """Tests for BMC certificate verification with pinning."""

    def test_verify_with_matching_pin(self):
        """Verification succeeds when pin matches live cert."""
        expected_fp = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"

        with patch("worker.services.power_manager._get_bmc_cert_fingerprint") as mock_get:
            mock_get.return_value = expected_fp
            result = _verify_bmc_cert("192.168.1.1", expected_fp)
            assert result is True

    def test_verify_with_mismatched_pin_raises(self):
        """Verification fails with mismatched pin."""
        expected_fp = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
        actual_fp = "99:88:77:66:55:44:33:22:11:00:FF:EE:DD:CC:BB:AA:99:88:77:66:55:44:33:22:11:00:FF:EE:DD:CC:BB:AA"

        with patch("worker.services.power_manager._get_bmc_cert_fingerprint") as mock_get:
            mock_get.return_value = actual_fp
            with pytest.raises(RuntimeError) as exc_info:
                _verify_bmc_cert("192.168.1.1", expected_fp)
            assert "mismatch" in str(exc_info.value).lower()

    def test_verify_no_pin_without_env_flag_raises(self, monkeypatch):
        """Verification fails without pin and without env flag."""
        monkeypatch.delenv("BMC_ALLOW_INSECURE_TLS", raising=False)

        with pytest.raises(RuntimeError) as exc_info:
            _verify_bmc_cert("192.168.1.1", None)
        assert "BMC_ALLOW_INSECURE_TLS" in str(exc_info.value)

    def test_verify_no_pin_with_env_flag_true_succeeds(self, monkeypatch):
        """Verification succeeds with no pin when env flag is true."""
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")

        with patch("worker.services.power_manager._get_bmc_cert_fingerprint") as mock_get:
            mock_get.return_value = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
            result = _verify_bmc_cert("192.168.1.1", None)
            assert result is True

    def test_verify_no_pin_with_env_flag_1_succeeds(self, monkeypatch):
        """Verification succeeds with no pin when env flag is 1."""
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "1")

        with patch("worker.services.power_manager._get_bmc_cert_fingerprint") as mock_get:
            mock_get.return_value = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
            result = _verify_bmc_cert("192.168.1.1", None)
            assert result is True

    def test_verify_no_pin_with_env_flag_yes_succeeds(self, monkeypatch):
        """Verification succeeds with no pin when env flag is yes."""
        monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "yes")

        with patch("worker.services.power_manager._get_bmc_cert_fingerprint") as mock_get:
            mock_get.return_value = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
            result = _verify_bmc_cert("192.168.1.1", None)
            assert result is True

    def test_verify_tls_failure_raises(self):
        """TLS connection failure is propagated."""
        with patch("worker.services.power_manager._get_bmc_cert_fingerprint") as mock_get:
            mock_get.side_effect = RuntimeError("TLS handshake failed")
            with pytest.raises(RuntimeError) as exc_info:
                _verify_bmc_cert("192.168.1.1", "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99")
            assert "handshake" in str(exc_info.value).lower()


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
