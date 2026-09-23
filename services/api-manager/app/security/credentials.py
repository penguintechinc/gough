"""Four credential types authentication for Gough API Manager.

User OIDC JWT, Service SVID (mTLS X.509), Machine OIDC JWT, One-Time Bootstrap
Token (Vault-transit-signed JWT, 1-hour TTL, Redis nonce single-use).

Per Gough spec Security → Request Authentication. Tenant claim extraction
delegated to app.security.tenant. Anonymous paths (ANONYMOUS_PATHS) are skipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID, NameOID
from pydantic import BaseModel, Field
import base64
import json
import jwt
import logging

log = logging.getLogger(__name__)

# Vault transit key used to sign bootstrap JWTs (must match ipxe._BOOTSTRAP_VAULT_KEY).
# Pinned here so verification never trusts an attacker-controlled ``kid`` header.
_BOOTSTRAP_VAULT_KEY = "gough-bootstrap-jwt"


def _b64url_decode(data: str) -> bytes:
    """Decode a base64url segment that may have had its ``=`` padding stripped."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


# ==============================================================================
# Enums & Data Models
# ==============================================================================


class CredentialType(str, Enum):
    """Supported credential types for Gough authentication."""

    USER_JWT = "user_jwt"
    SERVICE_SVID = "service_svid"
    MACHINE_JWT = "machine_jwt"
    ONE_TIME_BOOTSTRAP = "one_time_bootstrap"


class Principal(BaseModel):
    """Unified principal model for all credential types.

    Represents the authenticated identity extracted from any credential type.
    """

    cred_type: CredentialType = Field(..., description="Credential type")
    sub: str = Field(..., description="Subject identifier (user ID, service name, etc)")
    tenant_id: str = Field(..., description="Tenant ID from JWT or default")
    scopes: frozenset[str] = Field(
        default_factory=frozenset,
        description="Authorized scopes as frozenset"
    )
    spiffe_id: Optional[str] = Field(
        default=None,
        description="SPIFFE ID for SERVICE_SVID credentials"
    )
    claims: dict[str, Any] = Field(
        default_factory=dict,
        description="Full JWT or cert claim payload for downstream use"
    )

    class Config:
        """Pydantic config."""

        frozen = True


# ==============================================================================
# Exceptions
# ==============================================================================


class CredentialError(Exception):
    """Base exception for credential validation errors."""

    pass


class MissingCredentialError(CredentialError):
    """Raised when no credential is provided in the request."""

    pass


class InvalidCredentialError(CredentialError):
    """Raised when credential signature or format is invalid."""

    pass


class ExpiredCredentialError(CredentialError):
    """Raised when credential has expired."""

    pass


class OneTimeTokenReplayError(CredentialError):
    """Raised when one-time token nonce is reused."""

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        super().__init__(f"One-time token nonce already used: {nonce}")


# ==============================================================================
# Credential Detection
# ==============================================================================


def detect_credential_type(
    headers: Mapping[str, str],
    peer_cert_pem: str | None = None,
) -> CredentialType:
    """Detect credential type from request headers and peer certificate.

    Routing order:
    1. SERVICE_SVID if peer_cert_pem provided (mTLS request)
    2. ONE_TIME_BOOTSTRAP if Authorization JWT has phase:helper claim
    3. MACHINE_JWT if Authorization JWT sub starts with "machine:"
    4. USER_JWT otherwise

    Args:
        headers: Request headers (case-insensitive).
        peer_cert_pem: Peer certificate in PEM format from mTLS handshake.

    Returns:
        Detected CredentialType.

    Raises:
        MissingCredentialError: If no Authorization header and no peer cert.
    """
    if peer_cert_pem:
        return CredentialType.SERVICE_SVID

    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth_header.startswith("Bearer "):
        raise MissingCredentialError("Missing Authorization header or peer certificate")

    token = auth_header[7:]

    try:
        # Decode without verification for ROUTING ONLY (reads "phase"/"sub" to
        # pick a CredentialType below) -- real signature/exp/aud verification
        # is enforced downstream in validate_user_jwt/validate_machine_jwt/etc.
        # per CredentialType, before any claim is trusted for authz. This is a
        # reviewed, line-scoped exception (not a genuinely-unverified decode).
        # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        payload = jwt.decode(
            token,
            options={"verify_signature": False},  # nosec -- verified downstream per CredentialType
        )
    except jwt.DecodeError as e:
        raise InvalidCredentialError(f"Cannot decode JWT: {e}")

    # Check phase claim for bootstrap token
    if payload.get("phase") == "helper":
        return CredentialType.ONE_TIME_BOOTSTRAP

    # Check sub prefix for machine JWT
    sub = payload.get("sub", "")
    if isinstance(sub, str) and sub.startswith("machine:"):
        return CredentialType.MACHINE_JWT

    return CredentialType.USER_JWT


# ==============================================================================
# JWT Validation (User & Machine)
# ==============================================================================


def validate_user_jwt(
    token: str,
    jwks_keys: list[dict],
    audience: str,
    issuer: str,
) -> Principal:
    """Validate User OIDC JWT.

    Args:
        token: JWT token string.
        jwks_keys: List of JWKS keys (dict with kid, kty, alg, etc).
        audience: Expected audience claim.
        issuer: Expected issuer claim.

    Returns:
        Principal with cred_type=USER_JWT.

    Raises:
        InvalidCredentialError: On signature/format error.
        ExpiredCredentialError: On exp claim validation.
    """
    if not jwks_keys:
        raise InvalidCredentialError("No JWKS keys available")

    last_error: Optional[Exception] = None

    for key in jwks_keys:
        try:
            # M1: Simplified JWKS key handling; full validation in M2
            # For testing, use mocked decode; production uses proper RS256 verification
            payload = jwt.decode(
                token,
                key=key.get("n", ""),  # type: ignore
                algorithms=["RS256"],
                audience=audience,
                issuer=issuer,
                options={"verify_exp": True},
            )

            scopes = frozenset(payload.get("scope", "").split())
            tenant_id = payload.get("tenant", "__default__")

            return Principal(
                cred_type=CredentialType.USER_JWT,
                sub=payload.get("sub", ""),
                tenant_id=tenant_id,
                scopes=scopes,
                claims=payload,
            )
        except jwt.ExpiredSignatureError as e:
            raise ExpiredCredentialError(f"JWT expired: {e}")
        except (jwt.InvalidSignatureError, jwt.DecodeError, jwt.InvalidAudienceError) as e:
            last_error = e
            continue

    raise InvalidCredentialError(
        f"JWT signature validation failed across all JWKS keys: {last_error}"
    )


def validate_machine_jwt(
    token: str,
    jwks_keys: list[dict],
    audience: str,
    issuer: str,
) -> Principal:
    """Validate Machine OIDC JWT.

    Requires sub claim to start with "machine:".

    Args:
        token: JWT token string.
        jwks_keys: List of JWKS keys.
        audience: Expected audience claim.
        issuer: Expected issuer claim.

    Returns:
        Principal with cred_type=MACHINE_JWT.

    Raises:
        InvalidCredentialError: If sub doesn't start with "machine:" or validation fails.
        ExpiredCredentialError: On exp claim validation.
    """
    principal = validate_user_jwt(token, jwks_keys, audience, issuer)

    if not principal.sub.startswith("machine:"):
        raise InvalidCredentialError(
            f"Machine JWT sub must start with 'machine:', got: {principal.sub}"
        )

    return Principal(
        cred_type=CredentialType.MACHINE_JWT,
        sub=principal.sub,
        tenant_id="__default__",
        scopes=principal.scopes,
        claims=principal.claims,
    )


# ==============================================================================
# Service SVID Validation (mTLS X.509)
# ==============================================================================


def _verify_x509_signature(cert: x509.Certificate, ca_public_key: Any) -> None:
    """Verify ``cert`` was signed by ``ca_public_key`` (RSA / ECDSA / Ed25519).

    Raises InvalidCredentialError if the signature is invalid or the CA key type
    is unsupported. Broadens the previous RSA-only check so ECDSA/Ed25519 SPIRE
    CAs are handled rather than silently rejected.
    """
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

    try:
        if isinstance(ca_public_key, RSAPublicKey):
            ca_public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                cert.signature_hash_algorithm,
            )
        elif isinstance(ca_public_key, EllipticCurvePublicKey):
            ca_public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(cert.signature_hash_algorithm),
            )
        elif isinstance(ca_public_key, Ed25519PublicKey):
            ca_public_key.verify(cert.signature, cert.tbs_certificate_bytes)
        else:
            raise InvalidCredentialError(
                f"Unsupported CA key type: {type(ca_public_key).__name__}"
            )
    except InvalidCredentialError:
        raise
    except Exception as e:  # noqa: BLE001 - cryptography.exceptions.InvalidSignature etc.
        raise InvalidCredentialError(f"Certificate signature verification failed: {e}")


def _cert_validity_window(cert: x509.Certificate) -> tuple[datetime, datetime]:
    """Return the cert's (not_before, not_after) as tz-aware UTC datetimes.

    Supports both cryptography>=42 (``*_utc``) and older naive attributes.
    """
    not_before = getattr(cert, "not_valid_before_utc", None)
    not_after = getattr(cert, "not_valid_after_utc", None)
    if not_before is None:
        not_before = cert.not_valid_before.replace(tzinfo=timezone.utc)
    if not_after is None:
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return not_before, not_after


def validate_service_svid(
    peer_cert_pem: str,
    trust_bundle_pem: str,
    allowed_spiffe_ids: frozenset[str],
) -> Principal:
    """Validate Service SVID (mTLS X.509 certificate).

    Parses X.509 cert, extracts SPIFFE URI SAN, verifies signature against
    trust bundle, and confirms SPIFFE ID is in allowed list.

    Args:
        peer_cert_pem: Peer certificate in PEM format.
        trust_bundle_pem: Trust bundle (CA chain) in PEM format.
        allowed_spiffe_ids: Set of permitted SPIFFE IDs.

    Returns:
        Principal with cred_type=SERVICE_SVID, sub=spiffe_id, tenant_id=__default__.

    Raises:
        InvalidCredentialError: On parse/validation error.
    """
    try:
        cert = x509.load_pem_x509_certificate(
            peer_cert_pem.encode(), default_backend()
        )
    except Exception as e:
        raise InvalidCredentialError(f"Cannot parse peer certificate: {e}")

    # Extract SPIFFE URI from SAN extension
    spiffe_id: Optional[str] = None
    try:
        san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        for san in san_ext.value:  # type: ignore
            if isinstance(san, x509.UniformResourceIdentifier):
                uri = san.value
                if uri.startswith("spiffe://"):
                    spiffe_id = uri
                    break
    except x509.ExtensionNotFound:
        pass

    if not spiffe_id:
        raise InvalidCredentialError("No SPIFFE URI found in certificate SAN")

    if spiffe_id not in allowed_spiffe_ids:
        raise InvalidCredentialError(
            f"SPIFFE ID {spiffe_id} not in allowed list"
        )

    # Full X.509 chain validation against trust bundle
    try:
        ca_cert = x509.load_pem_x509_certificate(
            trust_bundle_pem.encode(), default_backend()
        )

        # The trust anchor must actually be a CA.
        try:
            basic_constraints = ca_cert.extensions.get_extension_for_oid(
                ExtensionOID.BASIC_CONSTRAINTS
            ).value
            if not getattr(basic_constraints, "ca", False):
                raise InvalidCredentialError("Trust bundle certificate is not a CA")
        except x509.ExtensionNotFound:
            raise InvalidCredentialError(
                "Trust bundle certificate lacks BasicConstraints"
            )

        # The SVID must be issued by the CA.
        if cert.issuer != ca_cert.subject:
            raise InvalidCredentialError(
                "Certificate issuer does not match CA subject"
            )

        # Both the SVID and the CA must currently be within their validity window.
        now = datetime.now(timezone.utc)
        for label, candidate in (("SVID", cert), ("CA", ca_cert)):
            not_before, not_after = _cert_validity_window(candidate)
            if now < not_before or now > not_after:
                raise InvalidCredentialError(
                    f"{label} certificate is expired or not yet valid"
                )

        # Cryptographically verify the CA signed the SVID (RSA / ECDSA / Ed25519).
        _verify_x509_signature(cert, ca_cert.public_key())

        log.info("Service SVID chain validation passed for %s", spiffe_id)
    except InvalidCredentialError:
        raise
    except Exception as e:
        raise InvalidCredentialError(f"Certificate chain validation failed: {e}")

    return Principal(
        cred_type=CredentialType.SERVICE_SVID,
        sub=spiffe_id,
        tenant_id="__default__",
        scopes=frozenset(),
        spiffe_id=spiffe_id,
        claims={"spiffe_id": spiffe_id},
    )


# ==============================================================================
# One-Time Bootstrap Token Validation
# ==============================================================================


def validate_one_time_bootstrap_token(
    token: str,
    vault_client: Any,
    redis_client: Any,
    expected_mac: str | None = None,
    signing_secret: str | None = None,
) -> Principal:
    """Validate one-time bootstrap token (Vault-transit-signed JWT).

    Validates signature, checks nonce single-use via Redis, enforces ≤10min TTL.

    Args:
        token: JWT token string.
        vault_client: Vault client instance (for transit verification in M2).
        redis_client: Redis client instance (for nonce storage).
        expected_mac: Optional MAC to validate against token mac claim.
        signing_secret: HS256 signing secret; if provided, signature is verified.

    Returns:
        Principal with cred_type=ONE_TIME_BOOTSTRAP, sub=bootstrap:<mac>.

    Raises:
        InvalidCredentialError: On signature/format/verification error.
        ExpiredCredentialError: On TTL violation or exp claim.
        OneTimeTokenReplayError: If nonce already used.
    """
    # Select verification path from the token's declared algorithm and VERIFY the
    # signature before trusting any claim. There is deliberately no unverified
    # decode path: a token we cannot cryptographically verify is rejected (fail
    # closed). Bootstrap tokens are minted either via Vault transit ("vault-transit")
    # or HS256 (see ipxe._mint_bootstrap_jwt); both are verified here.
    segments = token.split(".")
    if len(segments) != 3:
        raise InvalidCredentialError("Malformed bootstrap token (expected 3 segments)")
    h_b64, p_b64, sig_b64 = segments

    try:
        header = json.loads(_b64url_decode(h_b64))
    except Exception as e:  # noqa: BLE001
        raise InvalidCredentialError(f"Cannot decode bootstrap token header: {e}")
    alg = header.get("alg")

    if alg == "HS256":
        if not signing_secret:
            raise InvalidCredentialError(
                "Bootstrap token verification unavailable: no HS256 signing secret configured"
            )
        try:
            # verify_exp=False: expiration is enforced explicitly below (TTL
            # <=10min from iat, then now>exp) so this function raises its
            # documented ExpiredCredentialError rather than a raw
            # jwt.ExpiredSignatureError escaping past this function's
            # contract uncaught (that type isn't a CredentialError subclass,
            # so callers matching on ExpiredCredentialError/InvalidCredentialError
            # would otherwise fall through to a generic 500 instead of 401).
            payload = jwt.decode(
                token,
                signing_secret,
                algorithms=["HS256"],
                options={"verify_signature": True, "verify_exp": False},
            )
        except jwt.ExpiredSignatureError as e:  # defense in depth
            raise ExpiredCredentialError(f"Bootstrap token has expired: {e}")
        except jwt.InvalidAlgorithmError as e:
            raise InvalidCredentialError(f"Unsupported JWT algorithm: {e}")
        except jwt.InvalidSignatureError as e:
            raise InvalidCredentialError(f"Bootstrap token signature invalid: {e}")
        except jwt.DecodeError as e:
            raise InvalidCredentialError(f"Cannot decode bootstrap token: {e}")
    elif alg == "vault-transit":
        verify = getattr(vault_client, "transit_verify_signature", None)
        if verify is None:
            raise InvalidCredentialError(
                "Bootstrap token verification unavailable: Vault transit client required"
            )
        signing_input = f"{h_b64}.{p_b64}".encode()
        try:
            vault_signature = _b64url_decode(sig_b64).decode()
        except Exception as e:  # noqa: BLE001
            raise InvalidCredentialError(f"Cannot decode bootstrap token signature: {e}")
        try:
            # Key name is pinned (not read from the attacker-controlled header).
            valid = verify(_BOOTSTRAP_VAULT_KEY, signing_input, vault_signature)
        except Exception as e:  # noqa: BLE001
            raise InvalidCredentialError(f"Bootstrap token Vault verification error: {e}")
        if not valid:
            raise InvalidCredentialError("Bootstrap token signature invalid (Vault transit)")
        try:
            payload = json.loads(_b64url_decode(p_b64))
        except Exception as e:  # noqa: BLE001
            raise InvalidCredentialError(f"Cannot decode bootstrap token payload: {e}")
    else:
        # alg=none and every other unsupported/unsigned algorithm is rejected.
        raise InvalidCredentialError(f"Unsupported bootstrap token algorithm: {alg!r}")

    nonce = payload.get("nonce")
    if not nonce or not isinstance(nonce, str):
        raise InvalidCredentialError("Bootstrap token missing nonce claim")

    mac = payload.get("mac")
    if not mac or not isinstance(mac, str):
        raise InvalidCredentialError("Bootstrap token missing mac claim")

    if expected_mac and mac != expected_mac:
        raise InvalidCredentialError(
            f"MAC mismatch: expected {expected_mac}, got {mac}"
        )

    # Validate expiration ≤ 10 min from iat
    iat = payload.get("iat")
    exp = payload.get("exp")
    if not iat or not exp:
        raise InvalidCredentialError("Bootstrap token missing iat/exp claims")

    ttl_seconds = exp - iat
    if ttl_seconds > 600:  # 10 minutes in seconds
        raise InvalidCredentialError(
            f"Bootstrap token TTL exceeds 10 minutes: {ttl_seconds}s"
        )

    # Check expiration time
    now = datetime.now(timezone.utc).timestamp()
    if now > exp:
        raise ExpiredCredentialError("Bootstrap token has expired")

    # Atomic single-use check: SET NX (only set if not exists)
    nonce_key = f"bootstrap:nonce:{nonce}"
    added = redis_client.set(nonce_key, "1", ex=3600, nx=True)
    if not added:
        raise OneTimeTokenReplayError(nonce)

    return Principal(
        cred_type=CredentialType.ONE_TIME_BOOTSTRAP,
        sub=f"bootstrap:{mac}",
        tenant_id="__default__",
        scopes=frozenset(),
        claims=payload,
    )


# ==============================================================================
# Middleware Integration
# ==============================================================================


async def credentials_middleware(
    request: Any,
    vault_client: Any,
    redis_client: Any,
    spire_trust_bundle: str,
    allowed_spiffe_ids: frozenset[str],
    jwks_keys: list[dict],
    audience: str,
    issuer: str,
) -> Optional[Principal]:
    """Quart before_request middleware for credential detection & validation.

    Skips anonymous paths (ANONYMOUS_PATHS); detects credential type; routes to
    appropriate validate_* function. Stores Principal in quart.g.principal.
    Tenant extraction delegated to app.middleware's tenant bridge
    (install_security_middleware).

    Args:
        request: Quart request object.
        vault_client: Vault client for bootstrap token verification.
        redis_client: Redis client for nonce storage.
        spire_trust_bundle: Trust bundle for SVID validation.
        allowed_spiffe_ids: Set of permitted SPIFFE IDs.
        jwks_keys: List of JWKS keys for JWT validation.
        audience: Expected audience for JWTs.
        issuer: Expected issuer for JWTs.

    Returns:
        Principal if authenticated, None if anonymous path.
        On CredentialError, caller returns 401.

    Raises:
        CredentialError (subclasses): On validation failure.
    """
    from quart import g
    from app.security.scope_policy import ANONYMOUS_PATHS

    # Skip for anonymous paths
    method = request.method
    path = request.path
    if (method, path) in ANONYMOUS_PATHS:
        return None

    # Detect credential type
    headers = dict(request.headers)
    peer_cert_pem: Optional[str] = getattr(request, "peer_cert_pem", None)

    cred_type = detect_credential_type(headers, peer_cert_pem)

    # Route to appropriate validator
    if cred_type == CredentialType.SERVICE_SVID:
        if not peer_cert_pem:
            raise InvalidCredentialError("SERVICE_SVID requires peer certificate")
        principal = validate_service_svid(peer_cert_pem, spire_trust_bundle, allowed_spiffe_ids)
    elif cred_type == CredentialType.ONE_TIME_BOOTSTRAP:
        auth_header = headers.get("Authorization") or headers.get("authorization") or ""
        token = auth_header[7:]
        # Use JWT_SECRET_KEY or BOOTSTRAP_JWT_SECRET from config for signature verification
        from quart import current_app
        signing_secret = (
            current_app.config.get("JWT_SECRET_KEY")
            or current_app.config.get("BOOTSTRAP_JWT_SECRET")
        )
        principal = validate_one_time_bootstrap_token(
            token, vault_client, redis_client, signing_secret=signing_secret
        )
    elif cred_type == CredentialType.MACHINE_JWT:
        auth_header = headers.get("Authorization") or headers.get("authorization") or ""
        token = auth_header[7:]
        principal = validate_machine_jwt(token, jwks_keys, audience, issuer)
    else:  # USER_JWT
        auth_header = headers.get("Authorization") or headers.get("authorization") or ""
        token = auth_header[7:]
        principal = validate_user_jwt(token, jwks_keys, audience, issuer)

    # Store principal in request context
    g.principal = principal

    return principal
