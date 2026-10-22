"""SSH Certificate Validator for Gough Access Agent.

Validates SSH certificates signed by the Gough SSH CA.
"""

import base64
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import paramiko
from paramiko.message import Message

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CertificateInfo:
    """Parsed SSH certificate information."""

    key_id: str
    serial: int
    principals: List[str]
    valid_after: int  # Unix timestamp
    valid_before: int  # Unix timestamp
    cert_type: str  # "user" or "host"

    @property
    def is_valid(self) -> bool:
        """Check if certificate is currently valid (time-wise)."""
        now = int(time.time())
        return self.valid_after <= now <= self.valid_before

    @property
    def remaining_seconds(self) -> int:
        """Get remaining validity in seconds."""
        return max(0, self.valid_before - int(time.time()))


class CertificateValidationError(Exception):
    """Raised when certificate validation fails."""
    pass


class CertificateValidator:
    """Validates SSH certificates against CA public key."""

    def __init__(self, ca_public_key: str):
        """Initialize validator with CA public key.

        Args:
            ca_public_key: SSH CA public key in OpenSSH format
        """
        self.ca_public_key = ca_public_key
        self._ca_key_file: Optional[Path] = None

    def _ensure_ca_key_file(self) -> Path:
        """Ensure CA public key is written to temp file."""
        if self._ca_key_file and self._ca_key_file.exists():
            return self._ca_key_file

        # Write CA key to temp file (using NamedTemporaryFile for security)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_ca.pub", delete=False
        ) as f:
            f.write(self.ca_public_key)
            self._ca_key_file = Path(f.name)
        return self._ca_key_file

    def validate_certificate(
        self,
        certificate: str,
        expected_principals: Optional[List[str]] = None,
    ) -> CertificateInfo:
        """Validate SSH certificate.

        Args:
            certificate: SSH certificate in OpenSSH format
            expected_principals: Optional list of expected principals

        Returns:
            CertificateInfo with parsed certificate details

        Raises:
            CertificateValidationError: If validation fails
        """
        # Parse certificate
        cert_info = self._parse_certificate(certificate)

        # Check time validity
        if not cert_info.is_valid:
            if cert_info.valid_after > int(time.time()):
                raise CertificateValidationError("Certificate not yet valid")
            else:
                raise CertificateValidationError("Certificate expired")

        # Check principals if specified
        if expected_principals:
            matching = [p for p in expected_principals if p in cert_info.principals]
            if not matching:
                raise CertificateValidationError(
                    f"Certificate principals {cert_info.principals} do not match "
                    f"expected {expected_principals}"
                )

        # Verify signature with CA public key
        self._verify_signature(certificate)

        log.info(
            f"Validated certificate {cert_info.key_id} for "
            f"principals {cert_info.principals}"
        )

        return cert_info

    def _parse_certificate(self, certificate: str) -> CertificateInfo:
        """Parse SSH certificate using ssh-keygen.

        Args:
            certificate: SSH certificate in OpenSSH format

        Returns:
            CertificateInfo with parsed details

        Raises:
            CertificateValidationError: If parsing fails
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="-cert.pub", delete=False
        ) as f:
            f.write(certificate)
            cert_path = f.name

        try:
            result = subprocess.run(
                ["ssh-keygen", "-L", "-f", cert_path],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                raise CertificateValidationError(
                    f"Failed to parse certificate: {result.stderr}"
                )

            return self._parse_keygen_output(result.stdout)

        except subprocess.TimeoutExpired:
            raise CertificateValidationError("Certificate parsing timed out")
        finally:
            Path(cert_path).unlink(missing_ok=True)

    def _parse_keygen_output(self, output: str) -> CertificateInfo:
        """Parse ssh-keygen -L output.

        Args:
            output: Output from ssh-keygen -L

        Returns:
            CertificateInfo
        """
        key_id = ""
        serial = 0
        principals: List[str] = []
        valid_after = 0
        valid_before = 0
        cert_type = "user"

        lines = output.split("\n")
        in_principals = False

        for line in lines:
            line = line.strip()

            if line.startswith("Type:"):
                if "host" in line.lower():
                    cert_type = "host"
                else:
                    cert_type = "user"

            elif line.startswith("Key ID:"):
                # Extract key ID from quotes
                start = line.find('"')
                end = line.rfind('"')
                if start != -1 and end != -1:
                    key_id = line[start + 1:end]

            elif line.startswith("Serial:"):
                try:
                    serial = int(line.split(":")[1].strip())
                except (ValueError, IndexError):
                    pass

            elif line.startswith("Principals:"):
                in_principals = True

            elif in_principals:
                if line and not line.startswith("Critical") and not line.startswith("Valid"):
                    principals.append(line)
                else:
                    in_principals = False

            elif line.startswith("Valid:"):
                # Parse validity period
                # Format: "Valid: from 2024-01-01T00:00:00 to 2024-01-02T00:00:00"
                try:
                    parts = line.split(" ")
                    from_idx = parts.index("from") + 1
                    to_idx = parts.index("to") + 1

                    from_str = parts[from_idx]
                    to_str = parts[to_idx]

                    # Parse ISO timestamps
                    from datetime import datetime
                    valid_after = int(
                        datetime.fromisoformat(from_str).timestamp()
                    )
                    valid_before = int(
                        datetime.fromisoformat(to_str).timestamp()
                    )
                except (ValueError, IndexError):
                    # Alternative format: forever or timestamp
                    pass

        return CertificateInfo(
            key_id=key_id,
            serial=serial,
            principals=principals,
            valid_after=valid_after,
            valid_before=valid_before,
            cert_type=cert_type,
        )

    @staticmethod
    def _load_pubkey_from_blob(blob: bytes) -> paramiko.PKey:
        """Build a paramiko public key from a raw SSH public-key blob.

        Used to reconstruct the certificate's signing-CA key so its signature
        over the certificate body can be cryptographically verified.
        """
        key_type = Message(blob).get_text()
        if key_type == "ssh-rsa":
            return paramiko.RSAKey(msg=Message(blob))
        if key_type == "ssh-ed25519":
            return paramiko.Ed25519Key(msg=Message(blob))
        if key_type.startswith("ecdsa-sha2-"):
            return paramiko.ECDSAKey(msg=Message(blob))
        raise CertificateValidationError(f"Unsupported CA key type: {key_type}")

    def _verify_signature(self, certificate: str) -> None:
        """Cryptographically verify the certificate's CA signature.

        Sound verification requires BOTH: (a) the certificate's embedded CA
        signature validates over the signed body using the embedded signing
        key, AND (b) that signing key is byte-identical to the configured
        trusted CA. Checking the signing-CA fingerprint alone is insufficient —
        an attacker can embed the trusted CA's public key while signing with a
        different key, and ``ssh-keygen -L`` does not verify the signature.
        Fails closed on any parse/verify/mismatch error.

        Args:
            certificate: SSH certificate in OpenSSH format ("<type> <b64> [comment]").

        Raises:
            CertificateValidationError: If the signature is invalid or was not
                produced by the configured trusted CA.
        """
        try:
            parts = certificate.split()
            if len(parts) < 2:
                raise CertificateValidationError("Malformed certificate: missing key blob")
            cert_blob = base64.b64decode(parts[1])

            m = Message(cert_blob)
            cert_type = m.get_text()
            m.get_string()  # nonce
            if cert_type.startswith("ssh-rsa-cert"):
                m.get_mpint()  # e
                m.get_mpint()  # n
            elif cert_type.startswith("ssh-ed25519-cert"):
                m.get_string()  # public key
            elif cert_type.startswith("ecdsa-sha2-") and "-cert-" in cert_type:
                m.get_string()  # curve
                m.get_string()  # ec_point
            else:
                raise CertificateValidationError(f"Unsupported certificate type: {cert_type}")
            m.get_int64()   # serial
            m.get_int()     # type
            m.get_string()  # key id
            m.get_string()  # valid principals
            m.get_int64()   # valid after
            m.get_int64()   # valid before
            m.get_string()  # critical options
            m.get_string()  # extensions
            m.get_string()  # reserved
            signing_key_blob = m.get_string()  # CA public key
            signature_blob = m.get_string()    # CA signature (final field)

            # The CA signs everything up to (but not including) the signature field.
            signed_data = cert_blob[: -(4 + len(signature_blob))]

            # (a) The signature must validate against the embedded signing key.
            ca_key = self._load_pubkey_from_blob(signing_key_blob)
            if not ca_key.verify_ssh_sig(signed_data, Message(signature_blob)):
                raise CertificateValidationError(
                    "Certificate signature is not valid for its signing key"
                )

            # (b) The signing key must be the configured trusted CA.
            trusted_ca_blob = base64.b64decode(self.ca_public_key.split()[1])
            if signing_key_blob != trusted_ca_blob:
                raise CertificateValidationError(
                    "Certificate was not signed by the configured trusted CA"
                )

            log.info("Certificate signature cryptographically verified against trusted CA")

        except CertificateValidationError:
            raise
        except Exception as e:  # noqa: BLE001 - fail closed on any parse/verify error
            raise CertificateValidationError(
                f"Certificate signature verification failed: {e}"
            )


    def cleanup(self) -> None:
        """Clean up temporary files."""
        if self._ca_key_file:
            self._ca_key_file.unlink(missing_ok=True)
            self._ca_key_file = None
