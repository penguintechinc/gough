"""Hardware-tag eligibility and error-envelope helpers in app/api/_helpers.py.

Pure logic that decides whether a node can host a biome: categorical exact
match, numeric ``>=`` buckets (with speed-unit suffixes), forbids, operator
tags overriding auto-discovered tags, and the error envelope shapes returned
to clients. Assertions are on the returned values, not on call execution.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel
from quart import Quart

from app.api import _helpers as h


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("mem:total-gb:512", ("mem:total-gb", 512.0)),
        ("nic:speed:100g", ("nic:speed", 100000.0)),
        ("nic:speed:25gbps", ("nic:speed", 25000.0)),
        ("nic:speed:10m", ("nic:speed", 10.0)),
        ("gpu:vendor:nvidia:count:4", ("gpu:vendor:nvidia:count", 4.0)),
    ],
)
def test_numeric_tags_split_into_prefix_and_normalised_value(tag: str, expected: tuple) -> None:
    """Numeric-bucketed tags yield their kind prefix and a value in comparable units."""
    assert h._split_numeric_tag(tag) == expected


@pytest.mark.parametrize("tag", ["rack:a1", "mem:total-gb:lots", "unknown:kind:4", "nope"])
def test_non_numeric_or_unknown_kind_tags_are_categorical(tag: str) -> None:
    """Tags without a numeric kind or with a non-numeric value are not bucketed."""
    assert h._split_numeric_tag(tag) is None


def test_normalize_tags_handles_strings_dicts_and_junk() -> None:
    """Strings are trimmed, key/value dicts become k:v, and None or non-mapping junk is dropped."""
    tags = [" rack:a1 ", "", None, {"tag_key": "site", "tag_value": "dc1"}, {"key": "x", "value": "y"}, 42, {"tag_key": "", "value": "z"}]

    assert h._normalize_tags(tags) == ["rack:a1", "site:dc1", "x:y"]


def test_normalize_tags_empty_input() -> None:
    """None and empty iterables normalise to an empty list."""
    assert h._normalize_tags(None) == []
    assert h._normalize_tags([]) == []


def test_node_tag_index_keeps_max_numeric_value() -> None:
    """Repeated numeric declarations keep the largest value; categorical tags stay in the set."""
    cats, nums = h._node_tag_index(["mem:total-gb:512", "mem:total-gb:1024", "rack:a1"])

    assert nums == {"mem:total-gb": 1024.0}
    assert "rack:a1" in cats


def test_eligible_when_all_requirements_met() -> None:
    """Node satisfies categorical and numeric requirements and has no forbidden tags."""
    result = h.check_tag_eligibility(
        requires=["rack:a1", "mem:total-gb:256"],
        forbids=["maintenance"],
        node_tags=["rack:a1", "mem:total-gb:512"],
    )

    assert result.eligible is True
    assert result.missing_tags == []
    assert result.forbidden_tags_present == []


def test_missing_categorical_and_undersized_numeric_reported() -> None:
    """Missing exact tags and numeric values below the threshold are both reported as missing."""
    result = h.check_tag_eligibility(
        requires=["gpu:present", "cpu:cores:64"],
        forbids=None,
        node_tags=["cpu:cores:32"],
    )

    assert result.eligible is False
    assert result.missing_tags == ["gpu:present", "cpu:cores:64"]


def test_absent_numeric_kind_is_missing() -> None:
    """A numeric requirement for a kind the node never declares is missing."""
    result = h.check_tag_eligibility(requires=["psu:rated-watts:800"], forbids=None, node_tags=[])

    assert result.eligible is False
    assert result.missing_tags == ["psu:rated-watts:800"]


def test_forbidden_tag_present_makes_node_ineligible() -> None:
    """Any present forbidden tag makes the node ineligible even when requirements pass."""
    result = h.check_tag_eligibility(
        requires=["rack:a1"],
        forbids=["maintenance:on"],
        node_tags=["rack:a1", "maintenance:on"],
    )

    assert result.eligible is False
    assert result.forbidden_tags_present == ["maintenance:on"]


def test_resource_violations_fail_eligibility_and_are_echoed() -> None:
    """Caller-supplied resource violations (e.g. RAM) make the node ineligible and are returned."""
    violation = {"resource": "ram_mb", "required": 4096, "actual": 2048}

    result = h.check_tag_eligibility(
        requires=[], forbids=[], node_tags=[], resource_violations=[violation]
    )

    assert result.eligible is False
    assert result.resource_violations == [violation]
    assert result.to_dict()["resource_violations"] == [violation]


def test_node_effective_tags_operator_overrides_auto_tag_on_same_key() -> None:
    """Operator-set tag replaces the auto-discovered tag sharing its key; other auto tags survive."""
    rows = [SimpleNamespace(tag_key="rack", tag_value="b7")]
    db = MagicMock()
    db.node_tags_operator = SimpleNamespace(node_id=None)
    db.return_value.select.return_value = rows
    node = SimpleNamespace(id=5, hardware_tags=["rack:a1", "cpu:cores:16"])

    tags = h.node_effective_tags(db, node)

    assert "rack:a1" not in tags
    assert "rack:b7" in tags
    assert "cpu:cores:16" in tags


def test_node_effective_tags_tolerates_missing_operator_table() -> None:
    """If the operator tag table is absent the node's auto tags are returned unchanged."""
    db = MagicMock()
    db.node_tags_operator = SimpleNamespace(node_id=None)
    db.side_effect = RuntimeError("no such table")
    node = {"id": 9, "hardware_tags": ["mem:total-gb:128"]}

    assert h.node_effective_tags(db, node) == ["mem:total-gb:128"]


def test_node_effective_tags_accepts_dict_operator_rows() -> None:
    """Operator rows returned as mappings (not attribute objects) are read correctly."""
    db = MagicMock()
    db.node_tags_operator = SimpleNamespace(node_id=None)
    db.return_value.select.return_value = [{"tag_key": "site", "tag_value": "dc2"}]
    node = SimpleNamespace(id=1, hardware_tags=[])

    assert h.node_effective_tags(db, node) == ["site:dc2"]


async def test_error_envelope_shape() -> None:
    """Error helpers return the standard envelope with the given status code."""
    app = Quart(__name__)
    async with app.app_context():
        resp, status = h.err_forbidden_mfa()
        body = await resp.get_json()

    assert status == 403
    assert body["status"] == "error"
    assert body["error"]["code"] == "forbidden_mfa_required"
    assert body["error"]["details"] == {}


async def test_validate_body_returns_422_with_field_violations() -> None:
    """Schema failures produce a 422 validation_failed envelope listing each field."""

    class _Model(BaseModel):
        name: str
        count: int

    app = Quart(__name__)
    async with app.app_context():
        instance, failure = h.validate_body(_Model, {"name": 3, "count": "x"})
        assert instance is None
        resp, status = failure
        body = await resp.get_json()

    assert status == 422
    fields = {v["field"] for v in body["error"]["details"]["violations"]}
    assert fields == {"name", "count"}


def test_validate_body_success_returns_instance() -> None:
    """Valid payloads return the parsed model and no failure tuple."""

    class _Model(BaseModel):
        name: str

    instance, failure = h.validate_body(_Model, {"name": "ok"})

    assert failure is None
    assert instance.name == "ok"
