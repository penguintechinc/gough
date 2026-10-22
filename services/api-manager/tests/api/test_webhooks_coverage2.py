"""Additional unit tests for ``app.api.webhooks`` helper functions.

Focus on missed line coverage: URL validation, filter validation, retry policy.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from penguin_dal.query import Row

from app.api.webhooks import (
    MAX_EVENT_FILTER_PATTERNS,
    MAX_URL_LENGTH,
    _validate_url,
    _validate_event_filter,
    _validate_retry_policy,
    _row_to_dict,
)


# =============================================================================
# Tests: _validate_url (lines 93-102)
# =============================================================================


def test_validate_url_empty_string():
    """_validate_url rejects empty string."""
    ok, err = _validate_url("")
    assert not ok
    assert "required" in err


def test_validate_url_none():
    """_validate_url rejects None."""
    ok, err = _validate_url(None)
    assert not ok
    assert "required" in err


def test_validate_url_non_string():
    """_validate_url rejects non-string (123)."""
    ok, err = _validate_url(123)
    assert not ok
    assert "required" in err


def test_validate_url_too_long():
    """_validate_url rejects URLs exceeding MAX_URL_LENGTH."""
    long_url = "https://example.com/" + "x" * MAX_URL_LENGTH
    ok, err = _validate_url(long_url)
    assert not ok
    assert "exceeds" in err


def test_validate_url_invalid_scheme():
    """_validate_url rejects non-HTTP schemes."""
    ok, err = _validate_url("ftp://example.com/webhook")
    assert not ok
    assert "http" in err.lower()


def test_validate_url_no_netloc():
    """_validate_url rejects URL without host."""
    ok, err = _validate_url("https://")
    assert not ok
    assert "host" in err.lower()


def test_validate_url_http_valid():
    """_validate_url accepts HTTP."""
    ok, err = _validate_url("http://example.com/webhook")
    assert ok
    assert err == ""


def test_validate_url_https_valid():
    """_validate_url accepts HTTPS."""
    ok, err = _validate_url("https://example.com/webhook")
    assert ok
    assert err == ""


def test_validate_url_with_port():
    """_validate_url accepts URL with port."""
    ok, err = _validate_url("https://example.com:8443/webhook")
    assert ok


def test_validate_url_with_path():
    """_validate_url accepts URL with path."""
    ok, err = _validate_url("https://example.com/api/v1/webhook")
    assert ok


def test_validate_url_with_query():
    """_validate_url accepts URL with query params."""
    ok, err = _validate_url("https://example.com/webhook?token=abc")
    assert ok


def test_validate_url_exact_max_length():
    """_validate_url accepts URL at exact MAX_URL_LENGTH."""
    url = "https://example.com/" + "x" * (MAX_URL_LENGTH - 20)
    ok, err = _validate_url(url)
    assert ok


# =============================================================================
# Tests: _validate_event_filter (lines 105-118)
# =============================================================================


def test_validate_event_filter_none():
    """_validate_event_filter accepts None."""
    ok, err, patterns = _validate_event_filter(None)
    assert ok
    assert err == ""
    assert patterns == []


def test_validate_event_filter_empty_list():
    """_validate_event_filter accepts empty list."""
    ok, err, patterns = _validate_event_filter([])
    assert ok
    assert patterns == []


def test_validate_event_filter_valid_patterns():
    """_validate_event_filter accepts valid patterns."""
    ok, err, patterns = _validate_event_filter(["gough.*", "event.test", "pattern_123"])
    assert ok
    assert patterns == ["gough.*", "event.test", "pattern_123"]


def test_validate_event_filter_not_list():
    """_validate_event_filter rejects non-list."""
    ok, err, patterns = _validate_event_filter("not-a-list")
    assert not ok
    assert "must be a list" in err
    assert patterns == []


def test_validate_event_filter_too_many():
    """_validate_event_filter rejects too many patterns."""
    patterns_list = ["pattern" + str(i) for i in range(MAX_EVENT_FILTER_PATTERNS + 1)]
    ok, err, patterns = _validate_event_filter(patterns_list)
    assert not ok
    assert "too many" in err


def test_validate_event_filter_invalid_chars():
    """_validate_event_filter rejects invalid characters."""
    ok, err, patterns = _validate_event_filter(["valid", "invalid@char"])
    assert not ok
    assert "invalid" in err


def test_validate_event_filter_wildcard():
    """_validate_event_filter accepts wildcard."""
    ok, err, patterns = _validate_event_filter(["prefix*", "*.suffix", "*"])
    assert ok
    assert patterns == ["prefix*", "*.suffix", "*"]


def test_validate_event_filter_dots_dashes_underscores():
    """_validate_event_filter accepts dots, dashes, underscores."""
    ok, err, patterns = _validate_event_filter(["foo.bar-baz_qux"])
    assert ok
    assert patterns == ["foo.bar-baz_qux"]


def test_validate_event_filter_numbers():
    """_validate_event_filter accepts numbers."""
    ok, err, patterns = _validate_event_filter(["event123", "test456"])
    assert ok


def test_validate_event_filter_empty_string_item():
    """_validate_event_filter rejects empty string in list."""
    ok, err, patterns = _validate_event_filter(["valid", ""])
    assert not ok


def test_validate_event_filter_mixed_valid_invalid():
    """_validate_event_filter rejects if any item invalid."""
    ok, err, patterns = _validate_event_filter(["valid.pattern", "invalid@pattern"])
    assert not ok


def test_validate_event_filter_max_exact():
    """_validate_event_filter accepts at MAX_EVENT_FILTER_PATTERNS."""
    patterns_list = ["p" + str(i) for i in range(MAX_EVENT_FILTER_PATTERNS)]
    ok, err, patterns = _validate_event_filter(patterns_list)
    assert ok
    assert len(patterns) == MAX_EVENT_FILTER_PATTERNS


def test_validate_event_filter_non_string_item():
    """_validate_event_filter rejects non-string items."""
    ok, err, patterns = _validate_event_filter([123])
    assert not ok


# =============================================================================
# Tests: _validate_retry_policy (lines 120-134)
# =============================================================================


def test_validate_retry_policy_none():
    """_validate_retry_policy accepts None with defaults."""
    ok, err, policy = _validate_retry_policy(None)
    assert ok
    assert policy == {"mode": "standard"}


def test_validate_retry_policy_empty_dict():
    """_validate_retry_policy accepts empty dict with defaults."""
    ok, err, policy = _validate_retry_policy({})
    assert ok
    assert policy["mode"] == "standard"
    assert policy["max_attempts"] == 8


def test_validate_retry_policy_standard_mode():
    """_validate_retry_policy accepts 'standard' mode."""
    ok, err, policy = _validate_retry_policy({"mode": "standard"})
    assert ok
    assert policy["mode"] == "standard"


def test_validate_retry_policy_none_mode():
    """_validate_retry_policy accepts 'none' mode."""
    ok, err, policy = _validate_retry_policy({"mode": "none"})
    assert ok
    assert policy["mode"] == "none"


def test_validate_retry_policy_invalid_mode():
    """_validate_retry_policy rejects invalid mode."""
    ok, err, policy = _validate_retry_policy({"mode": "aggressive"})
    assert not ok
    assert "standard" in err


def test_validate_retry_policy_not_dict():
    """_validate_retry_policy rejects non-dict."""
    ok, err, policy = _validate_retry_policy("not-a-dict")
    assert not ok
    assert "must be an object" in err


def test_validate_retry_policy_max_attempts_valid():
    """_validate_retry_policy accepts valid max_attempts."""
    for n in [1, 2, 4, 8]:
        ok, err, policy = _validate_retry_policy({"max_attempts": n})
        assert ok
        assert policy["max_attempts"] == n


def test_validate_retry_policy_max_attempts_zero():
    """_validate_retry_policy rejects max_attempts=0."""
    ok, err, policy = _validate_retry_policy({"max_attempts": 0})
    assert not ok
    assert "1..8" in err


def test_validate_retry_policy_max_attempts_too_high():
    """_validate_retry_policy rejects max_attempts > 8."""
    ok, err, policy = _validate_retry_policy({"max_attempts": 10})
    assert not ok
    assert "1..8" in err


def test_validate_retry_policy_max_attempts_negative():
    """_validate_retry_policy rejects negative max_attempts."""
    ok, err, policy = _validate_retry_policy({"max_attempts": -1})
    assert not ok


def test_validate_retry_policy_max_attempts_string():
    """_validate_retry_policy rejects non-int max_attempts."""
    ok, err, policy = _validate_retry_policy({"max_attempts": "5"})
    assert not ok


def test_validate_retry_policy_max_attempts_float():
    """_validate_retry_policy rejects float max_attempts."""
    ok, err, policy = _validate_retry_policy({"max_attempts": 5.5})
    assert not ok


def test_validate_retry_policy_combined():
    """_validate_retry_policy validates mode and max_attempts."""
    ok, err, policy = _validate_retry_policy({
        "mode": "none",
        "max_attempts": 3
    })
    assert ok
    assert policy["mode"] == "none"
    assert policy["max_attempts"] == 3


# =============================================================================
# Tests: _row_to_dict (lines 141-157)
#
# ``_row_to_dict`` now reads a penguin-dal ``Row`` (attribute access), not a
# positional SQLAlchemy Core tuple -- built here via the real ``Row`` class
# rather than a stand-in, since it's the exact object ``db(...).select()``
# returns. event_filter/retry_policy are native JSONB columns, so penguin-dal
# reflects them as already-deserialized Python list/dict in the normal case
# (test_row_to_dict_all_fields); the isinstance(str) fallback is exercised
# separately since it's a defensive path, not the common one.
# =============================================================================


def _make_row(**overrides):
    base = dict(
        id="webhook-id",
        tenant_id="acme",
        url="https://example.com/webhook",
        signing_mode="ed25519",
        active=True,
        event_filter=["gough.event"],
        retry_policy={"mode": "standard", "max_attempts": 3},
    )
    base.update(overrides)
    return Row(base)


def test_row_to_dict_valid_json_string_fallback():
    """_row_to_dict parses valid JSON strings (defensive fallback path)."""
    row = _make_row(
        event_filter=json.dumps(["gough.event"]),
        retry_policy=json.dumps({"mode": "standard", "max_attempts": 3}),
    )
    result = _row_to_dict(row)
    assert result["event_filter"] == ["gough.event"]
    assert result["retry_policy"]["mode"] == "standard"
    assert result["retry_policy"]["max_attempts"] == 3


def test_row_to_dict_invalid_event_filter_json():
    """_row_to_dict handles invalid JSON in event_filter."""
    row = _make_row(event_filter="{invalid json", retry_policy={"mode": "standard"})
    result = _row_to_dict(row)
    assert result["event_filter"] == []


def test_row_to_dict_invalid_retry_policy_json():
    """_row_to_dict handles invalid JSON in retry_policy."""
    row = _make_row(event_filter=[], retry_policy="{bad json}")
    result = _row_to_dict(row)
    assert result["retry_policy"] == {}


def test_row_to_dict_null_event_filter():
    """_row_to_dict handles None event_filter."""
    row = _make_row(event_filter=None, retry_policy={})
    result = _row_to_dict(row)
    assert result["event_filter"] == []


def test_row_to_dict_null_retry_policy():
    """_row_to_dict handles None retry_policy."""
    row = _make_row(event_filter=[], retry_policy=None)
    result = _row_to_dict(row)
    assert result["retry_policy"] == {}


def test_row_to_dict_all_fields():
    """_row_to_dict converts all row fields correctly -- native JSONB shape
    (already-deserialized list/dict), the normal penguin-dal case."""
    row = _make_row(
        id="webhook-id-123",
        tenant_id="tenant-acme",
        url="https://webhook.example.com/hook",
        signing_mode="ecdsa_p256_sha256",
        active=False,
        event_filter=["pattern1", "pattern2"],
        retry_policy={"mode": "none", "max_attempts": 1},
    )
    result = _row_to_dict(row)
    assert result["id"] == "webhook-id-123"
    assert result["tenant_id"] == "tenant-acme"
    assert result["url"] == "https://webhook.example.com/hook"
    assert result["signing_mode"] == "ecdsa_p256_sha256"
    assert result["active"] is False
    assert result["event_filter"] == ["pattern1", "pattern2"]
    assert result["retry_policy"]["mode"] == "none"
