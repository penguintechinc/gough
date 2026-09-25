"""Explicit output-projection helpers for Gough API responses.

Named ``_dto.py`` rather than ``_schemas.py`` because ``app/api/_schemas/``
already exists as a package of quart-schema/Pydantic request-body models
(nodes, biomes) -- a same-directory module and package sharing a name is a
real Python import collision, not just a style clash.

Per security.md "Output Validation (Response Shape)": every endpoint's
response must be scoped to an explicit schema/DTO before it goes out --
never a raw ORM row, never ``**row.as_dict()``. The failure mode this
prevents is exactly the ``app/api/clouds.py`` credential leak: a
``del row["config"]``-style denylist missed the real column name
(``config_data``), so every response kept carrying the provider's
credentials. A ``del``/pop-based denylist fails open -- a column added
later, or misspelled here, ships by default. An allow-list projection fails
closed -- a column not listed here simply never leaves the process, whether
it exists today or is added next migration.

Every projection in this module is a plain ``tuple[str, ...]`` of field
names plus a ``project()`` call -- reviewable in a diff, and any future
sensitive column must be deliberately added to the tuple to ever reach a
client.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Generic projection helper
# ---------------------------------------------------------------------------


def project(row: Any, fields: Sequence[str]) -> dict[str, Any]:
    """Project a penguin-dal ``Row``/dict onto an explicit allow-list.

    ``row`` may be a penguin-dal ``Row`` (dict-like via ``.as_dict()``), an
    already-plain ``dict`` (e.g. from ``executesql(as_dict=True)``), or any
    other attribute-bearing object (e.g. a ``SimpleNamespace`` test double)
    -- the ``getattr`` fallback keeps this usable against the lightweight
    row doubles this codebase's test suite already builds, without every
    caller needing its own bespoke serializer. Fields absent from ``row``
    are simply omitted rather than raising -- callers that need a
    guaranteed key set should assert on the return value in tests, not rely
    on this helper to fail loudly for an optional column.
    """
    if isinstance(row, dict):
        return {f: row[f] for f in fields if f in row}
    if hasattr(row, "as_dict"):
        data = row.as_dict()
        return {f: data[f] for f in fields if f in data}
    return {f: getattr(row, f) for f in fields if hasattr(row, f)}


def _iso(value: Any) -> str | None:
    """Render a datetime (or ISO string) as an ISO-8601 string, else None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# storage_config (app/api/storage.py)
# ---------------------------------------------------------------------------

#: Fields safe to return for an S3-compatible storage configuration.
#: ``credentials_path`` (pointer into the secrets manager -- internal
#: infrastructure detail, not just "not a secret") and ``config_data``
#: (provider-specific JSON that can itself carry inline credentials, e.g. a
#: GCS service-account key or Azure account key -- see the model's own
#: "Additional JSON configuration" docstring) are both deliberately excluded.
#: This is the same field/leak class as the fixed clouds.py provider bug --
#: see that module's ``_PROVIDER_PUBLIC_FIELDS`` for the sibling pattern.
STORAGE_CONFIG_PUBLIC_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "provider_type",
    "endpoint_url",
    "region",
    "bucket_name",
    "is_default",
    "is_active",
    "use_ssl",
    "created_at",
    "updated_at",
)


def serialize_storage_config(row: Any) -> dict[str, Any]:
    """Project a ``storage_config`` row onto its API-safe fields."""
    out = project(row, STORAGE_CONFIG_PUBLIC_FIELDS)
    out["created_at"] = _iso(out.get("created_at"))
    out["updated_at"] = _iso(out.get("updated_at"))
    return out


# ---------------------------------------------------------------------------
# elder_config (app/api/ipxe.py PUT /elder/config)
# ---------------------------------------------------------------------------

#: Fields safe to return for the Elder integration configuration.
#: ``api_key`` is the Elder service credential -- ``update_elder_config``
#: used to echo the just-submitted row (including this column) straight
#: back via ``.as_dict()``, the same raw-row-echo shape as the clouds.py
#: leak. ``last_error`` is also excluded: it's free-text populated from
#: exception messages on failed Elder calls and can carry internal
#: hostnames/paths, not just user-facing status.
ELDER_CONFIG_PUBLIC_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "elder_url",
    "timeout",
    "max_retries",
    "is_active",
    "last_sync_at",
    "created_at",
    "updated_at",
)


def serialize_elder_config(row: Any) -> dict[str, Any]:
    """Project an ``elder_config`` row onto its API-safe fields."""
    out = project(row, ELDER_CONFIG_PUBLIC_FIELDS)
    out["last_sync_at"] = _iso(out.get("last_sync_at"))
    out["created_at"] = _iso(out.get("created_at"))
    out["updated_at"] = _iso(out.get("updated_at"))
    return out
