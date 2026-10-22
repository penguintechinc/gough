"""Reusable test doubles for webhook tests.

The real Vault transit engine is unavailable in unit tests, but the spec
demands cryptographic correctness. ``FakeVaultClient`` therefore implements
the *behavioral* contract of ``hvac.Client.secrets.transit`` and ``...kv.v2``
using real keys from the ``cryptography`` library, so signatures produced
in tests can be verified end-to-end.
"""

from __future__ import annotations

import base64
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Transit
# ---------------------------------------------------------------------------
@dataclass
class _TransitVersion:
    private_key: Any
    public_pem: str
    creation_time: str = field(default_factory=_now_iso)


@dataclass
class _TransitKey:
    key_type: str
    versions: dict[int, _TransitVersion] = field(default_factory=dict)
    latest_version: int = 0


class _Transit:
    def __init__(self) -> None:
        self.keys: dict[str, _TransitKey] = {}

    def create_key(
        self, *, name: str, key_type: str, mount_point: str = "transit", **_: Any
    ) -> dict:
        if name in self.keys:
            return {"data": {}}
        key = _TransitKey(key_type=key_type)
        self._add_version(key)
        self.keys[name] = key
        return {"data": {"latest_version": key.latest_version}}

    def rotate_key(self, *, name: str, mount_point: str = "transit") -> dict:
        key = self.keys[name]
        self._add_version(key)
        return {"data": {"latest_version": key.latest_version}}

    def read_key(self, *, name: str, mount_point: str = "transit") -> dict:
        if name not in self.keys:
            raise KeyError(name)
        key = self.keys[name]
        return {
            "data": {
                "latest_version": key.latest_version,
                "type": key.key_type,
                "keys": {
                    str(v): {
                        "public_key": ver.public_pem,
                        "creation_time": ver.creation_time,
                    }
                    for v, ver in key.versions.items()
                },
            }
        }

    def sign_data(
        self,
        *,
        name: str,
        mount_point: str = "transit",
        hash_input: Optional[str] = None,
        input: Optional[str] = None,
        hash_algorithm: Optional[str] = None,
        prehashed: bool = False,
        marshaling_algorithm: Optional[str] = None,
        **_: Any,
    ) -> dict:
        key = self.keys[name]
        version = key.versions[key.latest_version]
        raw_input = base64.b64decode(hash_input or input or "")

        if key.key_type == "ed25519":
            sig = version.private_key.sign(raw_input)
        elif key.key_type == "ecdsa-p256":
            sig = version.private_key.sign(raw_input, ec.ECDSA(hashes.SHA256()))
        else:  # pragma: no cover
            raise RuntimeError(f"unsupported key_type: {key.key_type}")

        return {
            "data": {
                "signature": f"vault:v{key.latest_version}:{_b64(sig)}",
            }
        }

    @staticmethod
    def _add_version(key: _TransitKey) -> None:
        if key.key_type == "ed25519":
            priv = ed25519.Ed25519PrivateKey.generate()
        elif key.key_type == "ecdsa-p256":
            priv = ec.generate_private_key(ec.SECP256R1())
        else:  # pragma: no cover
            raise ValueError(f"unsupported key_type: {key.key_type}")

        public_pem = priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        key.latest_version += 1
        key.versions[key.latest_version] = _TransitVersion(
            private_key=priv, public_pem=public_pem
        )


# ---------------------------------------------------------------------------
# KV v2
# ---------------------------------------------------------------------------
class _KVv2:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], dict] = {}

    def create_or_update_secret(
        self, *, path: str, secret: dict, mount_point: str = "secret"
    ) -> dict:
        self.store[(mount_point, path)] = dict(secret)
        return {"data": {"version": 1}}

    def read_secret_version(
        self, *, path: str, mount_point: str = "secret"
    ) -> dict:
        if (mount_point, path) not in self.store:
            raise KeyError(path)
        return {"data": {"data": dict(self.store[(mount_point, path)])}}


class _KV:
    def __init__(self) -> None:
        self.v2 = _KVv2()


class _Secrets:
    def __init__(self) -> None:
        self.transit = _Transit()
        self.kv = _KV()


class FakeVaultClient:
    """Minimal hvac.Client lookalike for webhook tests."""

    def __init__(self) -> None:
        self.secrets = _Secrets()
