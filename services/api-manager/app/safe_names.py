"""Strict identifier validators for values that reach external CLIs or URLs.

Names that originate in requests or the database (biome, container, node
hostname, SPIFFE ids) must never be able to smuggle a CLI flag or a path
segment. These validators fail closed with ``ValueError``.
"""

from __future__ import annotations

import re

_RFC1123_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_RFC1123_RE = re.compile(rf"^{_RFC1123_LABEL}(?:\.{_RFC1123_LABEL})*$")
_SPIFFE_RE = re.compile(r"^spiffe://[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~%=@+-]+)*$")
_MAX_NAME = 253


def validate_rfc1123_name(value: str, *, field: str = "name") -> str:
    """Return ``value`` if it is a lowercase RFC1123 DNS name, else raise.

    A leading ``-`` (CLI flag), slashes, whitespace and uppercase are all
    rejected, so the result is safe as an argv element or URL path segment.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_NAME:
        raise ValueError(f"invalid {field}: must be 1-{_MAX_NAME} chars")
    if not _RFC1123_RE.match(value):
        raise ValueError(f"invalid {field}: not a valid RFC1123 name")
    return value


def validate_spiffe_id(value: str, *, field: str = "spiffe_id") -> str:
    """Return ``value`` if it is a well-formed ``spiffe://`` URI, else raise."""
    if not isinstance(value, str) or len(value) > 2048 or not _SPIFFE_RE.match(value):
        raise ValueError(f"invalid {field}: not a valid SPIFFE ID")
    return value
