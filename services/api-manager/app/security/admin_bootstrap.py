"""First-administrator credential policy (no default passwords, ever).

Regression: security fix HIGH -- api-manager used to seed ``admin@gough.local``
with the hardcoded password ``admin`` and carried a dead second seeding path
falling back to ``changeme123``. This module is the single source of the
bootstrap credential: an operator-supplied secret (``ADMIN_PASSWORD_FILE``
preferred, ``ADMIN_PASSWORD`` accepted) that passes a strength policy, or a
cryptographically random one-time password written exactly once to a 0600
file. It never logs a password value and never falls back silently.

DB-free on purpose: the actual row insert lives with the other schema seeding
in ``app.models_sqlalchemy.seed_initial_admin`` (SQLAlchemy is confined to
schema-authority modules, see tests/security/test_no_direct_sqlalchemy_runtime.py).
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_ADMIN_PASSWORD_LENGTH = 12
DEFAULT_ADMIN_EMAIL = "admin@gough.local"
DEFAULT_CREDENTIAL_PATH = "/var/gough/bootstrap/admin-initial-password"
GENERATED_PASSWORD_BYTES = 24  # token_urlsafe(24) -> 32 chars, ~192 bits

ENV_EMAIL = "ADMIN_EMAIL"
ENV_PASSWORD = "ADMIN_PASSWORD"
ENV_PASSWORD_FILE = "ADMIN_PASSWORD_FILE"
ENV_CREDENTIAL_PATH = "ADMIN_BOOTSTRAP_CREDENTIAL_PATH"

# Exact (case-insensitive) matches that are always refused.
_DENY_EXACT = frozenset({"admin", "changeme", "changeme123", "password", "gough"})
# Placeholder markers refused anywhere in the value ("alpha-changeme123").
_DENY_SUBSTRING = frozenset({"changeme", "password"})


class AdminBootstrapError(RuntimeError):
    """Fatal bootstrap misconfiguration -- startup must abort, never degrade."""


@dataclass(slots=True, frozen=True)
class AdminCredential:
    """The resolved first-admin credential; ``password`` is kept out of repr."""

    email: str
    password: str = field(repr=False)
    generated: bool


def check_password_strength(password: str, email: str, source: str) -> None:
    """Raise ``AdminBootstrapError`` if ``password`` is weak; never echo the value.

    Weak = shorter than ``MIN_ADMIN_PASSWORD_LENGTH``, a deny-listed default,
    containing a placeholder marker, or equal to the email local part.
    ``source`` names the variable in the error so operators know what to fix.
    """
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise AdminBootstrapError(
            f"{source} is too short: the bootstrap admin password must be at "
            f"least {MIN_ADMIN_PASSWORD_LENGTH} characters"
        )
    normalized = password.strip().lower()
    local_part = email.split("@", 1)[0].strip().lower()
    deny = _DENY_EXACT | ({local_part} if local_part else frozenset())
    if normalized in deny or any(marker in normalized for marker in _DENY_SUBSTRING):
        raise AdminBootstrapError(
            f"{source} is a known default or guessable value (deny-listed "
            "default, placeholder marker, or the admin email local part); "
            "set a unique random password"
        )


def _read_password_file(path_value: str) -> str:
    """Read a mounted secret file, stripping only the trailing newline."""
    try:
        raw = Path(path_value).read_text(encoding="utf-8")
    except OSError as exc:
        raise AdminBootstrapError(
            f"{ENV_PASSWORD_FILE} could not be read ({exc.strerror or type(exc).__name__}): "
            f"{path_value}"
        ) from None
    return raw.rstrip("\r\n")


def resolve_admin_credential(env: Mapping[str, str]) -> AdminCredential:
    """Resolve the first-admin credential from ``env`` or generate a one-time one.

    Exactly one of ``ADMIN_PASSWORD_FILE`` / ``ADMIN_PASSWORD`` may be set; a
    set-but-weak value is fatal. Neither set -> random one-time password with
    ``generated=True`` (the caller must force a change at first login).
    """
    email = (env.get(ENV_EMAIL) or DEFAULT_ADMIN_EMAIL).strip().lower()
    if "@" not in email:
        raise AdminBootstrapError(f"{ENV_EMAIL} must be an email address")

    has_file = bool(env.get(ENV_PASSWORD_FILE))
    has_env = ENV_PASSWORD in env
    if has_file and has_env:
        raise AdminBootstrapError(
            f"set only one of {ENV_PASSWORD_FILE} (preferred) or {ENV_PASSWORD}, not both"
        )
    if has_file:
        password = _read_password_file(env[ENV_PASSWORD_FILE])
        check_password_strength(password, email, ENV_PASSWORD_FILE)
        return AdminCredential(email=email, password=password, generated=False)
    if has_env:
        password = env[ENV_PASSWORD]
        check_password_strength(password, email, ENV_PASSWORD)
        return AdminCredential(email=email, password=password, generated=False)

    return AdminCredential(
        email=email,
        password=secrets.token_urlsafe(GENERATED_PASSWORD_BYTES),
        generated=True,
    )


def credential_output_path(env: Mapping[str, str]) -> Path:
    """Where a generated one-time password is written (``ADMIN_BOOTSTRAP_CREDENTIAL_PATH``)."""
    return Path(env.get(ENV_CREDENTIAL_PATH) or DEFAULT_CREDENTIAL_PATH)


def write_one_time_credential(path: Path, credential: AdminCredential) -> Path:
    """Write the generated credential once, mode 0600, refusing to overwrite.

    ``O_EXCL`` makes a stale file a loud failure instead of a silent clobber;
    ``O_NOFOLLOW`` refuses a planted symlink. Logs the path, never the value.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        raise AdminBootstrapError(
            f"{ENV_CREDENTIAL_PATH} already exists ({path}) but no users exist; "
            "refusing to overwrite it -- remove the stale file and restart"
        ) from None
    except OSError as exc:
        raise AdminBootstrapError(
            f"cannot write the one-time admin password to {ENV_CREDENTIAL_PATH} "
            f"({path}): {exc.strerror or type(exc).__name__}"
        ) from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"email={credential.email}\npassword={credential.password}\n")
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise AdminBootstrapError(
            f"failed writing the one-time admin password to {path}: "
            f"{exc.strerror or type(exc).__name__}"
        ) from None
    logger.warning(
        "Bootstrap admin created with a generated one-time password; it was "
        "written to %s (mode 0600) and must be changed at first login. Read it "
        "once, then delete the file.",
        path,
    )
    return path
