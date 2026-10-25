"""Cloud-KMS client for LUKS key sealing on cloud-VM nodes.

Cloud VMs have no TPM, so the TPM2 / TPM2+PIN / Tang sealing tiers in
``app.workers.plan_compiler`` don't apply to them. This client envelope-
encrypts the raw LUKS disk-encryption key directly with the VM's own
cloud provider's native key-management service -- AWS KMS, GCP Cloud
KMS, or Azure Key Vault -- selected via ``GOUGH_CLOUD_KMS_PROVIDER`` --
instead of falling back to the weaker "dev" (Vault-transit-only) tier
that cloud VMs previously received.

AWS uses boto3's KMS client directly (``boto3`` is already a pinned
runtime dependency for other cloud-provisioning code). GCP and Azure
call their REST encrypt endpoints directly, authenticated via the
already-pinned ``google-auth`` and ``azure-identity`` SDKs, rather than
adding the heavier ``google-cloud-kms`` (which pulls a protobuf major
version bump across the whole dependency tree) or ``azure-keyvault-keys``
packages for what is a single HTTP call.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

_SUPPORTED_PROVIDERS = frozenset({"aws", "gcp", "azure"})

_GCP_KMS_SCOPE = "https://www.googleapis.com/auth/cloudkms"
_AZURE_KEYVAULT_SCOPE = "https://vault.azure.net/.default"
_AZURE_KEYVAULT_API_VERSION = "7.4"


class CloudKMSError(Exception):
    """Raised on cloud-KMS configuration or provider-API failure."""

    pass


@dataclass(slots=True)
class CloudKMSEncryptResult:
    """Result of a single cloud-KMS envelope-encrypt call."""

    ciphertext: bytes
    provider: str
    key_id: str
    key_version: str = ""


class CloudKMSClient:
    """Dispatches LUKS-key envelope encryption to the configured cloud KMS."""

    def __init__(
        self,
        provider: str,
        key_id: str,
        *,
        region: str | None = None,
        vault_url: str | None = None,
        timeout: float = 10.0,
        boto3_client: Any = None,
        session: Any = None,
        azure_token: str | None = None,
    ) -> None:
        """Initialise with an explicit provider and key identifier.

        Args:
            provider: One of ``"aws"``, ``"gcp"``, ``"azure"``.
            key_id: Provider-specific key identifier -- AWS KMS key ID/ARN,
                GCP CryptoKey resource name
                (``projects/*/locations/*/keyRings/*/cryptoKeys/*``), or
                Azure Key Vault key name.
            region: AWS region. Ignored for gcp/azure.
            vault_url: Azure Key Vault base URL
                (``https://<vault>.vault.azure.net``). Required for azure.
            timeout: HTTP timeout in seconds for the GCP/Azure REST calls.
            boto3_client: Injected boto3 KMS client -- tests only.
            session: Injected authenticated HTTP session -- tests only.
                For gcp, a ``google.auth.transport.requests.AuthorizedSession``;
                for azure, a ``requests``-compatible session.
            azure_token: Injected Azure AD bearer token -- tests only;
                bypasses ``DefaultAzureCredential``.

        Raises:
            CloudKMSError: If *provider* is unsupported or required
                identifiers are missing.
        """
        provider = provider.strip().lower()
        if provider not in _SUPPORTED_PROVIDERS:
            raise CloudKMSError(f"Unsupported cloud KMS provider: {provider!r}")
        if not key_id:
            raise CloudKMSError("key_id is required for cloud-kms sealing")
        if provider == "azure" and not vault_url:
            raise CloudKMSError("vault_url is required for azure cloud-kms sealing")

        self.provider = provider
        self.key_id = key_id
        self.region = region
        self.vault_url = (vault_url or "").rstrip("/")
        self.timeout = timeout
        self._boto3_client = boto3_client
        self._session = session
        self._azure_token = azure_token

    @classmethod
    def from_env(cls) -> CloudKMSClient:
        """Build a client from ``GOUGH_CLOUD_KMS_*`` environment variables.

        Raises:
            CloudKMSError: If required configuration is missing or invalid.
        """
        provider = os.getenv("GOUGH_CLOUD_KMS_PROVIDER", "").strip().lower()
        key_id = os.getenv("GOUGH_CLOUD_KMS_KEY_ID", "").strip()
        if not provider or not key_id:
            raise CloudKMSError(
                "GOUGH_CLOUD_KMS_PROVIDER and GOUGH_CLOUD_KMS_KEY_ID must both be "
                "set for cloud-kms LUKS sealing"
            )
        return cls(
            provider=provider,
            key_id=key_id,
            region=os.getenv("AWS_REGION")
            or os.getenv("GOUGH_CLOUD_KMS_REGION")
            or None,
            vault_url=os.getenv("GOUGH_AZURE_KEY_VAULT_URL") or None,
        )

    def encrypt(self, plaintext: bytes) -> CloudKMSEncryptResult:
        """Envelope-encrypt *plaintext* with the configured cloud KMS.

        Raises:
            CloudKMSError: On any provider configuration or API failure.
        """
        if self.provider == "aws":
            return self._encrypt_aws(plaintext)
        if self.provider == "gcp":
            return self._encrypt_gcp(plaintext)
        return self._encrypt_azure(plaintext)

    # ------------------------------------------------------------------
    # AWS KMS
    # ------------------------------------------------------------------

    def _get_boto3_client(self) -> Any:
        if self._boto3_client is None:
            import boto3

            self._boto3_client = boto3.client("kms", region_name=self.region)
        return self._boto3_client

    def _encrypt_aws(self, plaintext: bytes) -> CloudKMSEncryptResult:
        client = self._get_boto3_client()
        try:
            resp = client.encrypt(KeyId=self.key_id, Plaintext=plaintext)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — boto3 raises provider-specific exceptions
            raise CloudKMSError(f"AWS KMS encrypt failed: {exc}") from exc

        ciphertext = resp.get("CiphertextBlob")
        if not ciphertext:
            raise CloudKMSError("AWS KMS encrypt returned no CiphertextBlob")
        return CloudKMSEncryptResult(
            ciphertext=bytes(ciphertext),
            provider="aws",
            key_id=str(resp.get("KeyId", self.key_id)),
        )

    # ------------------------------------------------------------------
    # GCP Cloud KMS (REST, authenticated via google-auth)
    # ------------------------------------------------------------------

    def _get_gcp_session(self) -> Any:
        if self._session is None:
            import google.auth
            import google.auth.transport.requests

            credentials, _ = google.auth.default(scopes=[_GCP_KMS_SCOPE])
            # google-auth ships no py.typed marker, so AuthorizedSession
            # resolves as untyped under --strict.
            self._session = google.auth.transport.requests.AuthorizedSession(  # type: ignore[no-untyped-call]
                credentials
            )
        return self._session

    def _encrypt_gcp(self, plaintext: bytes) -> CloudKMSEncryptResult:
        session = self._get_gcp_session()
        url = f"https://cloudkms.googleapis.com/v1/{self.key_id}:encrypt"
        payload = {"plaintext": base64.b64encode(plaintext).decode("ascii")}
        try:
            resp = session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except (
            Exception
        ) as exc:  # noqa: BLE001 — requests/google-auth transport exceptions
            raise CloudKMSError(f"GCP Cloud KMS encrypt failed: {exc}") from exc

        data = resp.json()
        ciphertext_b64 = data.get("ciphertext")
        if not ciphertext_b64:
            raise CloudKMSError("GCP Cloud KMS encrypt returned no ciphertext")
        return CloudKMSEncryptResult(
            ciphertext=base64.b64decode(ciphertext_b64),
            provider="gcp",
            key_id=str(data.get("name", self.key_id)),
        )

    # ------------------------------------------------------------------
    # Azure Key Vault (REST, authenticated via azure-identity)
    # ------------------------------------------------------------------

    def _get_azure_token(self) -> str:
        if self._azure_token is not None:
            return self._azure_token

        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        token = credential.get_token(_AZURE_KEYVAULT_SCOPE)
        self._azure_token = str(token.token)
        return self._azure_token

    def _encrypt_azure(self, plaintext: bytes) -> CloudKMSEncryptResult:
        import requests

        session = self._session or requests
        try:
            token = self._get_azure_token()
        except Exception as exc:  # noqa: BLE001 — azure-identity credential errors
            raise CloudKMSError(f"Azure AD token acquisition failed: {exc}") from exc

        url = (
            f"{self.vault_url}/keys/{self.key_id}/encrypt"
            f"?api-version={_AZURE_KEYVAULT_API_VERSION}"
        )
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "alg": "RSA-OAEP-256",
            "value": base64.urlsafe_b64encode(plaintext).decode("ascii").rstrip("="),
        }
        try:
            resp = session.post(
                url, json=payload, headers=headers, timeout=self.timeout
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — requests transport exceptions
            raise CloudKMSError(f"Azure Key Vault encrypt failed: {exc}") from exc

        data = resp.json()
        value_b64 = data.get("value")
        if not value_b64:
            raise CloudKMSError("Azure Key Vault encrypt returned no value")
        padded = value_b64 + "=" * (-len(value_b64) % 4)
        return CloudKMSEncryptResult(
            ciphertext=base64.urlsafe_b64decode(padded),
            provider="azure",
            key_id=str(data.get("kid", self.key_id)),
        )
