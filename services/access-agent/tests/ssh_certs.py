"""Test helpers that mint SSH CAs and user certificates.

Keys are generated with the ``cryptography`` library; certificates are signed
with ``ssh-keygen -s`` (the only OpenSSH-format certificate signer available,
and the same binary the validator under test shells out to).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

KeyKind = Literal["ed25519", "rsa", "ecdsa"]


@dataclass(slots=True, frozen=True)
class SSHKeyPair:
    """A private key file on disk plus its OpenSSH public-key line."""

    name: str
    private_path: Path
    public_line: str

    @property
    def public_path(self) -> Path:
        """Return the path of the sibling ``.pub`` file used as signing input."""
        return self.private_path.with_name(f"{self.name}.pub")


def generate_keypair(directory: Path, name: str, kind: KeyKind = "ed25519") -> SSHKeyPair:
    """Generate an unencrypted OpenSSH keypair with ``cryptography``.

    Args:
        directory: Directory to write the key files into.
        name: Base file name for the key pair.
        kind: Key algorithm to generate.

    Returns:
        The key pair description with the private key chmod'ed to 0600.
    """
    if kind == "ed25519":
        key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey = (
            ed25519.Ed25519PrivateKey.generate()
        )
    elif kind == "rsa":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        key = ec.generate_private_key(ec.SECP256R1())

    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public_line = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()

    private_path = directory / name
    private_path.write_bytes(private_pem)
    private_path.chmod(0o600)
    pair = SSHKeyPair(name=name, private_path=private_path, public_line=public_line)
    pair.public_path.write_text(f"{public_line} {name}\n")
    return pair


def sign_user_certificate(
    ca: SSHKeyPair,
    user: SSHKeyPair,
    principals: list[str],
    validity: str = "-1h:+1h",
    key_id: str = "test-user",
) -> str:
    """Sign a user public key with a CA and return the certificate line.

    Args:
        ca: CA key pair whose private key signs the certificate.
        user: User key pair whose public key is certified.
        principals: Principals to embed in the certificate.
        validity: ``ssh-keygen -V`` validity window (e.g. ``-1h:+1h``).
        key_id: Certificate key identifier.

    Returns:
        The OpenSSH certificate line (``<type>-cert-v01@openssh.com <b64>``).
    """
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-s",
            str(ca.private_path),
            "-I",
            key_id,
            "-n",
            ",".join(principals),
            "-V",
            validity,
            str(user.public_path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    cert_path = user.private_path.with_name(f"{user.name}-cert.pub")
    return cert_path.read_text().strip()


def cert_blob_b64(certificate_line: str) -> str:
    """Return the base64 blob field of an OpenSSH certificate line."""
    return certificate_line.split()[1]
