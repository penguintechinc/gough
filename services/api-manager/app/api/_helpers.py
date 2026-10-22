"""Shared helpers for Gough API endpoints (Sprint 2).

Provides:
- Response envelope helpers (success/error) per spec section
  "Standard error codes" / "Response envelope".
- Hardware capability tag eligibility checker, implementing the
  "Selector Semantics" rules from the spec:
    * Numeric-bucketed tags (key:numeric_kind:N) — node satisfies
      iff its value for key:numeric_kind is >= requested.
    * Categorical tags — exact string match.
- A small Pydantic v2 validator wrapper that turns ValidationError
  into the standard ``validation_failed`` error envelope.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ValidationError
from quart import jsonify, request

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


def _request_id() -> str:
    rid = request.headers.get("X-Request-ID") if request else None
    return rid or str(uuid.uuid4())


def _meta(**extra: Any) -> dict:
    base = {
        "version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "request_id": _request_id(),
    }
    for k, v in extra.items():
        if v is not None:
            base[k] = v
    return base


def envelope_success(
    data: Any,
    status_code: int = 200,
    *,
    next_cursor: Optional[str] = None,
    extra_meta: Optional[dict] = None,
):
    body = {
        "status": "success",
        "data": data,
        "meta": _meta(next_cursor=next_cursor, **(extra_meta or {})),
    }
    return jsonify(body), status_code


def envelope_error(
    code: str,
    message: str,
    status_code: int,
    *,
    details: Optional[dict] = None,
):
    body = {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
        "meta": _meta(),
    }
    return jsonify(body), status_code


# Convenience shortcuts for the common error slugs from the spec table.

def err_bad_request(message: str = "Malformed request body or content-type"):
    return envelope_error("bad_request", message, 400)


def err_validation(message: str, *, violations: Optional[list] = None):
    return envelope_error(
        "validation_failed",
        message,
        422,
        details={"violations": violations or []},
    )


def err_not_found(message: str = "Resource not found"):
    return envelope_error("not_found", message, 404)


def err_conflict(message: str, *, details: Optional[dict] = None):
    return envelope_error("conflict", message, 409, details=details)


def err_forbidden_mfa(message: str = "MFA required for this action in compliance lanes"):
    return envelope_error("forbidden_mfa_required", message, 403)


def err_forbidden(message: str = "Forbidden"):
    return envelope_error("forbidden", message, 403)


def err_internal(message: str = "Unexpected internal error"):
    return envelope_error("internal_error", message, 500)


def err_license_required(message: str, *, details: Optional[dict] = None):
    """402 for a request refused on license entitlement grounds.

    Distinct from 403: the caller is authenticated and authorized, the
    deployment simply has no allowance left. ``details`` should carry the
    counts that produced the refusal so an operator can act on it.
    """
    return envelope_error("license_required", message, 402, details=details)


def err_feature_disabled(message: str, *, details: Optional[dict] = None):
    """404 for a surface switched off by a feature flag.

    404 rather than 403 so a disabled surface is indistinguishable from one
    that does not exist -- a flag that is off should not advertise itself.
    """
    return envelope_error("feature_disabled", message, 404, details=details)


def validate_body(model: type[BaseModel], payload: Any):
    """Validate ``payload`` against ``model``.

    Returns ``(instance, None)`` on success or
    ``(None, response_tuple)`` where the response is a fully-formed
    422 ``validation_failed`` envelope tuple ready to be returned.
    """
    try:
        return model.model_validate(payload), None
    except ValidationError as exc:
        violations = [
            {
                "field": ".".join(str(p) for p in err["loc"]),
                "code": err["type"],
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        return None, err_validation("Request body failed schema validation", violations=violations)


# ---------------------------------------------------------------------------
# Hardware tag eligibility
# ---------------------------------------------------------------------------


# Numeric-bucketed kinds — full tag form is ``<key>:<numeric_kind>:<N>``.
# Per the spec catalog, numeric-bucketed tags are those whose value is a
# decimal integer that the operator wants compared with >=. Adding a kind
# here makes the eligibility checker treat it numerically.
_NUMERIC_TAG_KINDS: frozenset[str] = frozenset(
    {
        # Memory/CPU/disk capacity
        "mem:total-gb",
        "mem:numa-nodes",
        "cpu:cores",
        "cpu:threads",
        "cpu:sockets",
        "disk:total-gb",
        "disk:dark-drives",
        "disk:dpwd",
        # Network speed buckets — encoded as ``nic:speed:<n>g`` in the spec,
        # but operators may also use plain integer values. We strip a
        # trailing ``g``/``gbps`` suffix when comparing.
        "nic:speed",
        # PSU / power
        "psu:rated-watts",
        "power-budget:bmc-watts-cap",
        # Multi-GPU counts (spec: ``gpu:vendor:nvidia:count:<n>``)
        "gpu:vendor:nvidia:count",
    }
)


_SPEED_SUFFIX_RE = re.compile(r"^(\d+(?:\.\d+)?)(g|gbps|m|mbps)?$", re.IGNORECASE)


def _coerce_numeric(value: str) -> Optional[float]:
    """Parse ``value`` into a comparable float.

    Accepts integers, decimals, or values with a trailing ``g``/``gbps``/
    ``m``/``mbps`` unit (used by ``nic:speed`` tags).  ``g``/``gbps`` is
    treated as 1000x ``m``/``mbps``.  Returns ``None`` if the value is
    not numeric.
    """
    m = _SPEED_SUFFIX_RE.match(value.strip())
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("g", "gbps"):
        return n * 1000.0
    return n


def _split_numeric_tag(tag: str) -> Optional[tuple[str, float]]:
    """If ``tag`` is a numeric-bucketed tag of the form
    ``<key>:<numeric_kind>:<N>``, return ``(prefix, value)`` where
    ``prefix`` is ``<key>:<numeric_kind>``.  Otherwise return ``None``.
    """
    # Try every prefix length: the kind itself may contain colons.
    parts = tag.split(":")
    if len(parts) < 2:
        return None
    # Walk from longest to shortest possible prefix to avoid greedy false
    # matches; spec catalog has at most 4 colon-separated segments before
    # the numeric value.
    for cut in range(len(parts) - 1, 0, -1):
        prefix = ":".join(parts[:cut])
        if prefix in _NUMERIC_TAG_KINDS:
            value = _coerce_numeric(":".join(parts[cut:]))
            if value is not None:
                return prefix, value
            return None
    return None


@dataclass(slots=True)
class EligibilityResult:
    """Outcome of evaluating a biome's hardware-tag eligibility against a node.

    Attributes mirror the ``no_eligible_node`` error structure from the
    spec ``Plan Compiler Integration`` section.
    """

    eligible: bool
    missing_tags: list[str] = field(default_factory=list)
    forbidden_tags_present: list[str] = field(default_factory=list)
    resource_violations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "missing_tags": list(self.missing_tags),
            "forbidden_tags_present": list(self.forbidden_tags_present),
            "resource_violations": list(self.resource_violations),
        }


def _normalize_tags(tags: Optional[Iterable[Any]]) -> list[str]:
    if not tags:
        return []
    out: list[str] = []
    for t in tags:
        if t is None:
            continue
        if isinstance(t, str):
            s = t.strip()
            if s:
                out.append(s)
        else:
            # Tolerate operator-set tags as objects ({"key":..,"value":..})
            try:
                k = t.get("tag_key") or t.get("key")
                v = t.get("tag_value") or t.get("value")
            except AttributeError:
                continue
            if k and v is not None:
                out.append(f"{k}:{v}")
    return out


def _node_tag_index(node_tags: list[str]) -> tuple[set[str], dict[str, float]]:
    """Build a (categorical_set, numeric_max_by_prefix) view of a node's
    effective tag list.

    For numeric-bucketed tags, repeated declarations keep the max value
    seen so a node tagged ``mem:total-gb:512`` and ``mem:total-gb:1024``
    is treated as 1024.
    """
    cats: set[str] = set()
    nums: dict[str, float] = {}
    for tag in node_tags:
        split = _split_numeric_tag(tag)
        if split is None:
            cats.add(tag)
            continue
        prefix, value = split
        prev = nums.get(prefix)
        if prev is None or value > prev:
            nums[prefix] = value
        # Also keep the literal in the categorical set so an exact-match
        # required tag still works (e.g. a biome requiring exactly
        # ``nic:speed:100g`` and a node tagged the same).
        cats.add(tag)
    return cats, nums


def check_tag_eligibility(
    requires: Optional[Iterable[str]],
    forbids: Optional[Iterable[str]],
    node_tags: Optional[Iterable[Any]],
    *,
    resource_violations: Optional[list[dict[str, Any]]] = None,
) -> EligibilityResult:
    """Evaluate biome requirements against a node's effective tags.

    Args:
        requires: ``requires_hardware_tags`` (AND).
        forbids: ``forbids_hardware_tags`` (any present → ineligible).
        node_tags: Node's effective tag list. Strings or
            ``{tag_key, tag_value}`` dicts accepted.
        resource_violations: Optional pre-computed list of resource
            violations to fold into the result (e.g. ``min_ram_mb``
            checked by the caller).

    Returns:
        ``EligibilityResult``.
    """
    req_list = _normalize_tags(requires)
    fbd_list = _normalize_tags(forbids)
    node_list = _normalize_tags(node_tags)

    cats, nums = _node_tag_index(node_list)

    missing: list[str] = []
    for req in req_list:
        split = _split_numeric_tag(req)
        if split is None:
            # Categorical tag — exact match.
            if req not in cats:
                missing.append(req)
            continue
        prefix, want = split
        have = nums.get(prefix)
        if have is None or have < want:
            missing.append(req)

    forbidden_present: list[str] = []
    for fbd in fbd_list:
        # ``forbids`` semantics from the spec: "Any present → node ineligible".
        # Always exact match — numeric forbids are not bucketed.
        if fbd in cats:
            forbidden_present.append(fbd)

    eligible = (
        not missing
        and not forbidden_present
        and not (resource_violations or [])
    )

    return EligibilityResult(
        eligible=eligible,
        missing_tags=missing,
        forbidden_tags_present=forbidden_present,
        resource_violations=list(resource_violations or []),
    )


def node_effective_tags(db, node) -> list[str]:
    """Return a node's effective tag list (auto + operator-defined).

    The schema stores auto-discovered tags as a JSON list on
    ``nodes.hardware_tags`` and operator-set tags as rows in the
    ``node_tags_operator`` table (key/value).  Per the spec, the
    effective set is their union with operator winning on conflict.
    """
    auto = node.hardware_tags if hasattr(node, "hardware_tags") else None
    if auto is None and isinstance(node, dict):
        auto = node.get("hardware_tags")
    auto_list = _normalize_tags(auto)

    operator_rows: list[Any] = []
    try:
        nid = node.id if hasattr(node, "id") else node["id"]
        operator_rows = list(
            db(db.node_tags_operator.node_id == nid).select()
        )
    except Exception:  # noqa: BLE001 — table may be absent on minimal test DBs
        operator_rows = []

    operator_tags: list[str] = []
    operator_keys: set[str] = set()
    for row in operator_rows:
        try:
            k = row.tag_key
            v = row.tag_value
        except AttributeError:
            k = row.get("tag_key")
            v = row.get("tag_value")
        if k:
            operator_tags.append(f"{k}:{v}")
            operator_keys.add(k)

    # Operator wins on conflict — drop any auto-tag whose key (the bit
    # before the final ``:``) collides with an operator-set key.
    merged: list[str] = []
    for t in auto_list:
        head = ":".join(t.split(":")[:-1]) or t
        if head not in operator_keys:
            merged.append(t)
    merged.extend(operator_tags)
    return merged
