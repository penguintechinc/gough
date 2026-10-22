"""Unit tests for ``app.api._helpers`` (Sprint 2).

Covers:
- Hardware-tag eligibility: numeric-bucketed (mem/disk/nic-speed) >=
  match, categorical exact match, forbids any-present, resource
  violations folding, operator-tag override.
- Numeric-tag parsing edge cases (suffixes, malformed values).
- Pydantic ``validate_body`` returning ``validation_failed`` envelope
  details.
"""

from __future__ import annotations

import pytest

from app.api._helpers import (
    EligibilityResult,
    _coerce_numeric,
    _normalize_tags,
    _split_numeric_tag,
    check_tag_eligibility,
    node_effective_tags,
)


class TestCoerceNumeric:
    """``_coerce_numeric`` parses bare ints, decimals, and speed suffixes."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("512", 512.0),
            ("512g", 512000.0),
            ("100G", 100000.0),
            ("100gbps", 100000.0),
            ("500m", 500.0),
            ("500MBPS", 500.0),
            ("1.5", 1.5),
        ],
    )
    def test_parses_value(self, value: str, expected: float) -> None:
        assert _coerce_numeric(value) == expected

    @pytest.mark.parametrize("value", ["abc", "", "g100", "100xyz"])
    def test_rejects_malformed(self, value: str) -> None:
        assert _coerce_numeric(value) is None


class TestSplitNumericTag:
    """``_split_numeric_tag`` recognizes numeric-bucketed catalog tags."""

    def test_mem_total_gb(self) -> None:
        assert _split_numeric_tag("mem:total-gb:512") == ("mem:total-gb", 512.0)

    def test_nic_speed(self) -> None:
        assert _split_numeric_tag("nic:speed:100g") == ("nic:speed", 100000.0)

    def test_cpu_cores(self) -> None:
        assert _split_numeric_tag("cpu:cores:32") == ("cpu:cores", 32.0)

    def test_disk_total_gb(self) -> None:
        assert _split_numeric_tag("disk:total-gb:2048") == ("disk:total-gb", 2048.0)

    def test_dark_drives(self) -> None:
        assert _split_numeric_tag("disk:dark-drives:6") == ("disk:dark-drives", 6.0)

    def test_categorical_returns_none(self) -> None:
        assert _split_numeric_tag("gpu:nvidia") is None
        assert _split_numeric_tag("region:us-east") is None
        assert _split_numeric_tag("cpu:vendor:intel") is None

    def test_unknown_numeric_kind_returns_none(self) -> None:
        # ``foo:bar:42`` is not in _NUMERIC_TAG_KINDS, so it's treated
        # categorically.
        assert _split_numeric_tag("foo:bar:42") is None

    def test_short_tag(self) -> None:
        assert _split_numeric_tag("solo") is None


class TestNormalizeTags:
    def test_strings_passthrough(self) -> None:
        assert _normalize_tags(["a", "b"]) == ["a", "b"]

    def test_strips_whitespace_and_empties(self) -> None:
        assert _normalize_tags(["  a  ", "", None, "b"]) == ["a", "b"]

    def test_dict_form_operator_tags(self) -> None:
        assert _normalize_tags(
            [{"tag_key": "region", "tag_value": "us-east"}]
        ) == ["region:us-east"]
        assert _normalize_tags([{"key": "purpose", "value": "gpu-pool"}]) == [
            "purpose:gpu-pool"
        ]

    def test_none_returns_empty(self) -> None:
        assert _normalize_tags(None) == []
        assert _normalize_tags([]) == []


class TestCheckTagEligibilityCategorical:
    """Exact-match semantics for non-numeric tags."""

    def test_all_categorical_present(self) -> None:
        res = check_tag_eligibility(
            requires=["gpu:nvidia", "tpm:2.0"],
            forbids=None,
            node_tags=["gpu:nvidia", "tpm:2.0", "region:us-east"],
        )
        assert res.eligible
        assert res.missing_tags == []

    def test_missing_categorical(self) -> None:
        res = check_tag_eligibility(
            requires=["gpu:nvidia", "tpm:2.0"],
            forbids=None,
            node_tags=["gpu:nvidia"],
        )
        assert not res.eligible
        assert res.missing_tags == ["tpm:2.0"]

    def test_categorical_no_partial_match(self) -> None:
        # Biome requires exact match — 'cpu:vendor:intel' must not be
        # satisfied by just 'cpu:vendor'.
        res = check_tag_eligibility(
            requires=["cpu:vendor:intel"],
            forbids=None,
            node_tags=["cpu:vendor:amd"],
        )
        assert not res.eligible
        assert "cpu:vendor:intel" in res.missing_tags


class TestCheckTagEligibilityNumeric:
    """Numeric-bucketed >= matching."""

    def test_node_value_above_required_satisfies(self) -> None:
        res = check_tag_eligibility(
            requires=["mem:total-gb:512"],
            forbids=None,
            node_tags=["mem:total-gb:1024"],
        )
        assert res.eligible

    def test_node_value_equal_required_satisfies(self) -> None:
        res = check_tag_eligibility(
            requires=["mem:total-gb:512"],
            forbids=None,
            node_tags=["mem:total-gb:512"],
        )
        assert res.eligible

    def test_node_value_below_required_fails(self) -> None:
        res = check_tag_eligibility(
            requires=["mem:total-gb:512"],
            forbids=None,
            node_tags=["mem:total-gb:256"],
        )
        assert not res.eligible
        assert res.missing_tags == ["mem:total-gb:512"]

    def test_nic_speed_with_g_suffix_compares_correctly(self) -> None:
        # 100g (=100000) >= 25g (=25000)
        res = check_tag_eligibility(
            requires=["nic:speed:25g"],
            forbids=None,
            node_tags=["nic:speed:100g"],
        )
        assert res.eligible

    def test_nic_speed_below_required_fails(self) -> None:
        res = check_tag_eligibility(
            requires=["nic:speed:100g"],
            forbids=None,
            node_tags=["nic:speed:25g"],
        )
        assert not res.eligible
        assert res.missing_tags == ["nic:speed:100g"]

    def test_node_missing_numeric_kind_entirely(self) -> None:
        res = check_tag_eligibility(
            requires=["mem:total-gb:512"],
            forbids=None,
            node_tags=["region:us-east"],
        )
        assert not res.eligible
        assert "mem:total-gb:512" in res.missing_tags

    def test_max_of_repeated_numeric_tags_used(self) -> None:
        # If a node carries two values for the same prefix, the max
        # wins.
        res = check_tag_eligibility(
            requires=["mem:total-gb:512"],
            forbids=None,
            node_tags=["mem:total-gb:128", "mem:total-gb:1024"],
        )
        assert res.eligible


class TestCheckTagEligibilityForbids:
    def test_forbids_present_disqualifies(self) -> None:
        res = check_tag_eligibility(
            requires=["gpu:nvidia"],
            forbids=["lifecycle:playground"],
            node_tags=["gpu:nvidia", "lifecycle:playground"],
        )
        assert not res.eligible
        assert res.forbidden_tags_present == ["lifecycle:playground"]

    def test_forbids_absent_passes(self) -> None:
        res = check_tag_eligibility(
            requires=[],
            forbids=["confidential-compute:tdx"],
            node_tags=["gpu:nvidia"],
        )
        assert res.eligible
        assert res.forbidden_tags_present == []


class TestCheckTagEligibilityResources:
    def test_resource_violations_make_node_ineligible(self) -> None:
        res = check_tag_eligibility(
            requires=[],
            forbids=None,
            node_tags=["gpu:nvidia"],
            resource_violations=[
                {"resource": "memory_mb", "required": 16384, "available": 4096}
            ],
        )
        assert not res.eligible
        assert res.resource_violations[0]["resource"] == "memory_mb"

    def test_no_resource_violations_passes(self) -> None:
        res = check_tag_eligibility(
            requires=[],
            forbids=None,
            node_tags=[],
            resource_violations=[],
        )
        assert res.eligible


class TestEligibilityResultSerialization:
    def test_to_dict_shape(self) -> None:
        r = EligibilityResult(
            eligible=False,
            missing_tags=["tpm:2.0"],
            forbidden_tags_present=[],
            resource_violations=[{"resource": "memory_mb", "required": 16, "available": 8}],
        )
        d = r.to_dict()
        assert d == {
            "eligible": False,
            "missing_tags": ["tpm:2.0"],
            "forbidden_tags_present": [],
            "resource_violations": [
                {"resource": "memory_mb", "required": 16, "available": 8}
            ],
        }


class _FakeRow:
    """Minimal stand-in for a PyDAL row with attribute access."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getitem__(self, key):
        return self.__dict__[key]

    def get(self, key, default=None):
        return self.__dict__.get(key, default)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self):
        return self._rows


class _FakeColumn:
    """Captures comparisons so ``col == value`` produces a marker the fake
    DB can dispatch on.
    """

    def __eq__(self, other):  # type: ignore[override]
        return ("eq", other)


class _FakeTable:
    def __init__(self):
        self.node_id = _FakeColumn()


class _FakeDb:
    """Minimal stand-in for a PyDAL ``DB`` supporting
    ``db(db.node_tags_operator.node_id == n).select()``.
    """

    def __init__(self, operator_rows: dict[int, list[_FakeRow]]):
        self._operator_rows = operator_rows
        self.node_tags_operator = _FakeTable()

    def __call__(self, query):
        # query is ``("eq", node_id)`` from _FakeColumn.__eq__
        if isinstance(query, tuple) and query[0] == "eq":
            return _FakeQuery(self._operator_rows.get(query[1], []))
        return _FakeQuery([])


class TestNodeEffectiveTags:
    def test_merges_auto_and_operator_tags(self) -> None:
        node = _FakeRow(
            id=1,
            hardware_tags=["gpu:nvidia", "cpu:vendor:intel"],
        )
        db = _FakeDb(
            operator_rows={
                1: [
                    _FakeRow(tag_key="region", tag_value="us-east"),
                    _FakeRow(tag_key="purpose", tag_value="gpu-pool"),
                ]
            }
        )
        merged = node_effective_tags(db, node)
        assert "gpu:nvidia" in merged
        assert "cpu:vendor:intel" in merged
        assert "region:us-east" in merged
        assert "purpose:gpu-pool" in merged

    def test_operator_tag_wins_on_key_conflict(self) -> None:
        # ``region:us-west`` auto-tag should be replaced by operator's
        # ``region:us-east``.
        node = _FakeRow(
            id=1,
            hardware_tags=["region:us-west", "gpu:nvidia"],
        )
        db = _FakeDb(
            operator_rows={
                1: [_FakeRow(tag_key="region", tag_value="us-east")]
            }
        )
        merged = node_effective_tags(db, node)
        assert "region:us-east" in merged
        assert "region:us-west" not in merged
        assert "gpu:nvidia" in merged

    def test_no_operator_tags_returns_auto_only(self) -> None:
        node = _FakeRow(id=1, hardware_tags=["gpu:nvidia"])
        db = _FakeDb(operator_rows={})
        assert node_effective_tags(db, node) == ["gpu:nvidia"]

    def test_node_without_hardware_tags(self) -> None:
        node = _FakeRow(id=1, hardware_tags=None)
        db = _FakeDb(operator_rows={})
        assert node_effective_tags(db, node) == []


class TestValidateBody:
    """``validate_body`` formats Pydantic ValidationError as 422 envelope."""

    def test_valid_body_returns_instance(self) -> None:
        from app.api._biome_schema import BiomeSignRequest
        from app.api._helpers import validate_body

        instance, err = validate_body(
            BiomeSignRequest, {"key_id": "vault-key-1", "reason": "release-build"}
        )
        assert err is None
        assert instance is not None
        assert instance.key_id == "vault-key-1"

    def test_invalid_body_returns_422_with_violations(self) -> None:
        # Run inside a Quart test request context so ``jsonify`` /
        # ``request`` work.
        import asyncio

        from quart import Quart

        from app.api._biome_schema import BiomeSignRequest
        from app.api._helpers import validate_body

        app = Quart(__name__)

        async def runner() -> None:
            async with app.test_request_context("/", method="POST"):
                instance, err = validate_body(BiomeSignRequest, {"key_id": ""})
                assert instance is None
                assert err is not None
                response, status = err
                assert status == 422
                body = await response.get_json()
                assert body["status"] == "error"
                assert body["error"]["code"] == "validation_failed"
                assert "violations" in body["error"]["details"]
                # Empty key_id violates min_length=1.
                fields = {v["field"] for v in body["error"]["details"]["violations"]}
                assert "key_id" in fields

        asyncio.run(runner())
