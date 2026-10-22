"""Additional unit tests for ``app.api.audit`` helper functions.

Focus on missed line coverage: buffer types, ISO8601 parsing, error paths.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from dataclasses import dataclass, field

import pytest

from app.api.audit import _serialize_audit_event


@dataclass
class FakeAuditRow:
    id: uuid.UUID
    ts: datetime
    cluster_id: str
    tenant_id: Optional[str]
    actor_sub: str
    actor_scope: list[str]
    action: str
    resource_kind: str
    resource_id: Optional[str] = None
    before_json: Optional[dict] = None
    after_json: Optional[dict] = None
    request_id: Optional[str] = None
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None
    prev_hash: bytes = field(default=b"\x00" * 32)
    hash: bytes = field(default=b"\x11" * 32)
    signature: Optional[bytes] = None


def _make_row(**overrides: Any) -> FakeAuditRow:
    base = dict(
        id=uuid.uuid4(),
        ts=datetime.now(timezone.utc),
        cluster_id="test-cluster",
        tenant_id="acme",
        actor_sub="alice@acme",
        actor_scope=["gough.audit.read"],
        action="joiner.secret.emit",
        resource_kind="joiner_secret",
        resource_id=str(uuid.uuid4()),
    )
    base.update(overrides)
    return FakeAuditRow(**base)


# =============================================================================
# Tests: _serialize_audit_event buffer handling (lines 154, 157)
# =============================================================================


def test_serialize_audit_event_memoryview_hash():
    """_serialize_audit_event handles memoryview for hash."""
    row = _make_row(hash=memoryview(b"\x11" * 32))
    result = _serialize_audit_event(row)
    assert result["hash_b64"] is not None
    assert isinstance(result["hash_b64"], str)
    assert len(result["hash_b64"]) > 0


def test_serialize_audit_event_memoryview_prev_hash():
    """_serialize_audit_event handles memoryview for prev_hash."""
    row = _make_row(prev_hash=memoryview(b"\x00" * 32))
    result = _serialize_audit_event(row)
    assert result["prev_hash_b64"] is not None
    assert isinstance(result["prev_hash_b64"], str)


def test_serialize_audit_event_bytearray_hash():
    """_serialize_audit_event handles bytearray for hash."""
    row = _make_row(hash=bytearray(b"\x11" * 32))
    result = _serialize_audit_event(row)
    assert result["hash_b64"] is not None
    assert isinstance(result["hash_b64"], str)
    # Verify it's valid base64
    decoded = base64.b64decode(result["hash_b64"])
    assert len(decoded) == 32


def test_serialize_audit_event_bytearray_signature():
    """_serialize_audit_event handles bytearray for signature."""
    row = _make_row(signature=bytearray(b"sig"))
    result = _serialize_audit_event(row)
    assert result["signature_b64"] is not None


def test_serialize_audit_event_none_hash():
    """_serialize_audit_event handles None hash values."""
    row = _make_row(hash=None, prev_hash=None, signature=None)
    result = _serialize_audit_event(row)
    assert result["hash_b64"] is None
    assert result["prev_hash_b64"] is None
    assert result["signature_b64"] is None


def test_serialize_audit_event_string_signature():
    """_serialize_audit_event converts string signature to base64."""
    row = _make_row(signature="string-signature")
    result = _serialize_audit_event(row)
    assert result["signature_b64"] is not None
    assert isinstance(result["signature_b64"], str)
    # Verify roundtrip
    decoded = base64.b64decode(result["signature_b64"])
    assert decoded == b"string-signature"


def test_serialize_audit_event_empty_bytes():
    """_serialize_audit_event handles empty bytes."""
    row = _make_row(hash=b"", prev_hash=b"")
    result = _serialize_audit_event(row)
    # Empty bytes encodes to empty string
    assert result["hash_b64"] == ""
    assert result["prev_hash_b64"] == ""


def test_serialize_audit_event_none_ts():
    """_serialize_audit_event handles None timestamp."""
    row = _make_row(ts=None)
    result = _serialize_audit_event(row)
    assert result["ts"] is None


def test_serialize_audit_event_datetime_isoformat():
    """_serialize_audit_event renders datetime as ISO8601."""
    now = datetime(2025, 6, 15, 14, 30, 45, 123456, timezone.utc)
    row = _make_row(ts=now)
    result = _serialize_audit_event(row)
    assert result["ts"] == "2025-06-15T14:30:45.123456+00:00"


def test_serialize_audit_event_empty_actor_scope():
    """_serialize_audit_event handles empty actor_scope."""
    row = _make_row(actor_scope=[])
    result = _serialize_audit_event(row)
    assert result["actor_scope"] == []


def test_serialize_audit_event_multiple_scopes():
    """_serialize_audit_event preserves multiple scopes."""
    row = _make_row(actor_scope=["scope1", "scope2", "scope3"])
    result = _serialize_audit_event(row)
    assert result["actor_scope"] == ["scope1", "scope2", "scope3"]


# =============================================================================
# Tests: Helper function behavior (lines 71-110, 126-131)
# =============================================================================


def test_serialize_audit_event_null_fields():
    """_serialize_audit_event handles all None optional fields."""
    row = FakeAuditRow(
        id=uuid.uuid4(),
        ts=datetime.now(timezone.utc),
        cluster_id="test",
        tenant_id="acme",
        actor_sub="alice",
        actor_scope=[],
        action="test",
        resource_kind="test",
        resource_id=None,
        before_json=None,
        after_json=None,
        request_id=None,
        source_ip=None,
        user_agent=None,
        hash=b"\x11" * 32,
        prev_hash=b"\x00" * 32,
        signature=None,
    )
    result = _serialize_audit_event(row)
    assert result["resource_id"] is None
    assert result["before_json"] is None
    assert result["after_json"] is None
    assert result["request_id"] is None
    assert result["source_ip"] is None
    assert result["user_agent"] is None
    assert result["signature_b64"] is None


def test_serialize_audit_event_all_fields_populated():
    """_serialize_audit_event serializes all populated fields."""
    now = datetime.now(timezone.utc)
    request_id = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())

    row = _make_row(
        before_json={"old": "value"},
        after_json={"new": "value"},
        request_id=request_id,
        source_ip="192.0.2.1",
        user_agent="test-agent/1.0",
        resource_id=resource_id,
    )
    result = _serialize_audit_event(row)

    assert result["before_json"] == {"old": "value"}
    assert result["after_json"] == {"new": "value"}
    assert result["request_id"] == request_id
    assert result["source_ip"] == "192.0.2.1"
    assert result["user_agent"] == "test-agent/1.0"
    assert result["resource_id"] == resource_id
    assert result["id"] == str(row.id)


def test_serialize_audit_event_uuid_strings():
    """_serialize_audit_event converts UUIDs to strings."""
    rid = uuid.uuid4()
    row = _make_row(resource_id=str(rid))
    result = _serialize_audit_event(row)
    assert result["id"] == str(row.id)
    assert isinstance(result["id"], str)


# =============================================================================
# Tests: Base64 encoding correctness
# =============================================================================


def test_serialize_audit_event_base64_roundtrip():
    """_serialize_audit_event encodes correctly for decoding."""
    original_hash = b"\xaa\xbb\xcc\xdd" * 8
    row = _make_row(hash=original_hash)
    result = _serialize_audit_event(row)

    decoded_hash = base64.b64decode(result["hash_b64"])
    assert decoded_hash == original_hash


def test_serialize_audit_event_mixed_buffer_types():
    """_serialize_audit_event handles mix of buffer types."""
    row = FakeAuditRow(
        id=uuid.uuid4(),
        ts=datetime.now(timezone.utc),
        cluster_id="test",
        tenant_id="acme",
        actor_sub="alice",
        actor_scope=[],
        action="test",
        resource_kind="test",
        hash=bytes([0x01, 0x02, 0x03, 0x04] * 8),
        prev_hash=memoryview(b"\xaa" * 32),
        signature=bytearray(b"sig"),
    )
    result = _serialize_audit_event(row)

    assert result["hash_b64"] is not None
    assert result["prev_hash_b64"] is not None
    assert result["signature_b64"] is not None

    # All should be decodable
    base64.b64decode(result["hash_b64"])
    base64.b64decode(result["prev_hash_b64"])
    base64.b64decode(result["signature_b64"])
