"""Regression tests for SSH certificate signature verification.

These tests verify that the certificate validator properly validates
that certificates were signed by the trusted CA, not by an attacker's CA.
This is a CRITICAL SECURITY TEST: Test #2 proves the authentication bypass
vulnerability has been closed.
"""

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agent.cert_validator import CertificateValidator, CertificateValidationError


@pytest.fixture
def temp_dir():
    """Create a temporary directory for key generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def generate_ca_keypair(tmpdir: Path, name: str) -> tuple[str, str]:
    """Generate a CA keypair using ssh-keygen.

    Args:
        tmpdir: Temporary directory
        name: Name for the CA (e.g., "ca-a")

    Returns:
        Tuple of (private_key_path, public_key_text)

    Raises:
        pytest.skip: If ssh-keygen is unavailable
    """
    try:
        ca_priv = tmpdir / f"{name}_rsa"
        ca_pub = tmpdir / f"{name}_rsa.pub"

        # Generate RSA keypair with ssh-keygen
        result = subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "rsa",
                "-b",
                "2048",
                "-N",
                "",  # No passphrase
                "-f",
                str(ca_priv),
                "-C",
                f"ca-{name}@test.local",
            ],
            capture_output=True,
            timeout=10,
        )

        if result.returncode != 0:
            pytest.skip(f"ssh-keygen failed: {result.stderr}")

        # Read public key
        pub_key_text = ca_pub.read_text().strip()
        return str(ca_priv), pub_key_text

    except FileNotFoundError:
        pytest.skip("ssh-keygen not available")
    except subprocess.TimeoutExpired:
        pytest.skip("ssh-keygen timed out")


def sign_certificate(
    tmpdir: Path,
    ca_priv: str,
    principal: str,
    cert_name: str,
) -> str:
    """Sign a certificate using the CA private key.

    Args:
        tmpdir: Temporary directory
        ca_priv: Path to CA private key
        principal: Principal to sign for (e.g., "gough-agent")
        cert_name: Name for output cert

    Returns:
        Certificate text in OpenSSH format

    Raises:
        pytest.skip: If ssh-keygen fails
    """
    try:
        # Generate a user keypair to sign
        user_key = tmpdir / f"user_{cert_name}_rsa"
        result = subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "rsa",
                "-b",
                "2048",
                "-N",
                "",
                "-f",
                str(user_key),
                "-C",
                f"user-{cert_name}@test.local",
            ],
            capture_output=True,
            timeout=10,
        )

        if result.returncode != 0:
            pytest.skip(f"Failed to generate user key: {result.stderr}")

        # Sign the public key with the CA
        # ssh-keygen creates {user_key}-cert.pub
        result = subprocess.run(
            [
                "ssh-keygen",
                "-s",
                ca_priv,
                "-I",
                f"test-cert-{cert_name}",
                "-n",
                principal,
                "-V",
                "+1h",  # Valid for 1 hour
                "-O",
                "permit-pty",
                "-O",
                "permit-user-rc",
                str(user_key.with_suffix(".pub")),
            ],
            capture_output=True,
            timeout=10,
        )

        if result.returncode != 0:
            pytest.skip(f"Failed to sign certificate: {result.stderr}")

        # ssh-keygen creates the cert as {user_key}-cert.pub
        cert_path = Path(str(user_key) + "-cert.pub")
        if not cert_path.exists():
            pytest.skip(f"Certificate file not created: {cert_path}")

        # Read and return the certificate
        cert_text = cert_path.read_text().strip()
        return cert_text

    except subprocess.TimeoutExpired:
        pytest.skip("ssh-keygen timed out during certificate signing")


class TestCertificateValidation:
    """Test SSH certificate signature validation."""

    def test_valid_cert_from_trusted_ca(self, temp_dir):
        """Test that a certificate signed by trusted CA is accepted.

        This is a baseline test: a properly signed certificate should validate.
        """
        # Generate CA-A
        ca_a_priv, ca_a_pub = generate_ca_keypair(temp_dir, "ca-a")

        # Sign a certificate with CA-A for principal "gough-agent"
        cert = sign_certificate(temp_dir, ca_a_priv, "gough-agent", "valid")

        # Create validator trusting CA-A
        validator = CertificateValidator(ca_a_pub)

        # Should succeed: cert signed by trusted CA
        try:
            cert_info = validator.validate_certificate(
                cert, expected_principals=["gough-agent"]
            )
            assert cert_info.key_id == "test-cert-valid"
            assert "gough-agent" in cert_info.principals
        finally:
            validator.cleanup()

    def test_cert_from_untrusted_ca_rejected(self, temp_dir):
        """Test that a certificate from an attacker's CA is REJECTED.

        CRITICAL SECURITY TEST: This test proves the authentication bypass
        vulnerability is closed. An attacker cannot forge certificates using
        their own CA if the validator properly checks the signing CA.

        This is the test that would have FAILED with the old, broken
        implementation that didn't verify the signing CA.
        """
        # Generate CA-A (the trusted CA)
        ca_a_priv, ca_a_pub = generate_ca_keypair(temp_dir, "ca-a")

        # Generate CA-B (attacker's CA)
        ca_b_priv, ca_b_pub = generate_ca_keypair(temp_dir, "ca-b")

        # Attacker signs a certificate with their CA-B for "gough-agent"
        attacker_cert = sign_certificate(
            temp_dir, ca_b_priv, "gough-agent", "attacker"
        )

        # Create validator trusting only CA-A
        validator = CertificateValidator(ca_a_pub)

        # Should FAIL: certificate signed by CA-B, not the trusted CA-A
        with pytest.raises(
            CertificateValidationError,
            match="not signed by the configured trusted CA",
        ):
            validator.validate_certificate(
                attacker_cert, expected_principals=["gough-agent"]
            )

        validator.cleanup()

    def test_corrupted_cert_rejected(self, temp_dir):
        """Test that a corrupted certificate is rejected.

        If an attacker modifies even one byte of a valid certificate,
        the cryptographic signature should become invalid.
        """
        # Generate CA-A
        ca_a_priv, ca_a_pub = generate_ca_keypair(temp_dir, "ca-a")

        # Sign a valid certificate
        cert = sign_certificate(temp_dir, ca_a_priv, "gough-agent", "original")

        # Corrupt the certificate by modifying a byte in the base64 data
        # (not the type prefix or newline)
        cert_parts = cert.split()
        cert_type = cert_parts[0]
        cert_data_b64 = cert_parts[1]

        # Flip one character in the middle of the base64
        corrupted_b64 = (
            cert_data_b64[:100] + ("X" if cert_data_b64[100] != "X" else "Y") + cert_data_b64[101:]
        )
        corrupted_cert = f"{cert_type} {corrupted_b64}"

        # Create validator trusting CA-A
        validator = CertificateValidator(ca_a_pub)

        # Should fail: corrupted data will fail signature verification
        with pytest.raises(CertificateValidationError):
            validator.validate_certificate(
                corrupted_cert, expected_principals=["gough-agent"]
            )

        validator.cleanup()

    def test_spoofed_signing_key_rejected(self, temp_dir):
        """Test that a cert whose signing-key is spoofed to the trusted CA is REJECTED.

        This is the decisive test that distinguishes real signature verification
        from fingerprint-only comparison. An attacker signs a cert with their own
        CA-B, then rewrites the certificate's embedded *signing-key* field to the
        trusted CA-A's public key while keeping CA-B's signature. Fingerprint-only
        verification (ssh-keygen -L) would read the embedded key and conclude the
        cert was signed by CA-A. Real verification recomputes the signature over the
        tampered body and rejects it because CA-B's signature is not valid for CA-A.
        """
        import base64

        from paramiko.message import Message

        ca_a_priv, ca_a_pub = generate_ca_keypair(temp_dir, "ca-a")
        ca_b_priv, _ca_b_pub = generate_ca_keypair(temp_dir, "ca-b")
        cert_b = sign_certificate(temp_dir, ca_b_priv, "gough-agent", "spoof")

        # Rebuild the CA-B cert with the signing-key field replaced by CA-A's key.
        blob = base64.b64decode(cert_b.split()[1])
        m = Message(blob)
        out = Message()
        out.add_string(m.get_text())                       # cert type
        out.add_string(m.get_string())                     # nonce
        out.add_mpint(m.get_mpint())                        # rsa e
        out.add_mpint(m.get_mpint())                        # rsa n
        out.add_int64(m.get_int64())                        # serial
        out.add_int(m.get_int())                            # type
        out.add_string(m.get_string())                     # key id
        out.add_string(m.get_string())                     # valid principals
        out.add_int64(m.get_int64())                        # valid after
        out.add_int64(m.get_int64())                        # valid before
        out.add_string(m.get_string())                     # critical options
        out.add_string(m.get_string())                     # extensions
        out.add_string(m.get_string())                     # reserved
        m.get_string()                                     # original signing key (CA-B) — drop
        orig_sig = m.get_string()                          # CA-B signature
        out.add_string(base64.b64decode(ca_a_pub.split()[1]))  # spoof signing key = CA-A
        out.add_string(orig_sig)                           # keep CA-B signature
        spoofed_cert = "ssh-rsa-cert-v01@openssh.com " + base64.b64encode(
            out.asbytes()
        ).decode()

        validator = CertificateValidator(ca_a_pub)
        with pytest.raises(CertificateValidationError):
            validator.validate_certificate(
                spoofed_cert, expected_principals=["gough-agent"]
            )
        validator.cleanup()
