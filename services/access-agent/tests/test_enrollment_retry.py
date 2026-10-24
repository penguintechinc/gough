"""Retry/backoff behaviour of enrollment: transient failures only, bounded, jittered."""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.config import AgentConfig
from agent.enrollment import AgentEnrollment, EnrollmentError, parse_retry_after
from tests.test_enrollment import enroll_ok_body, make_enrollment


def _enr(
    tmp_path: Path, responses: list[Any], **cfg: Any
) -> tuple[AgentEnrollment, list[float], list[int]]:
    """Enrollment whose server replays ``responses`` (Response or Exception) in order."""
    sleeps: list[float] = []
    calls: list[int] = []
    queue = list(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    enr = make_enrollment(tmp_path, handler, **cfg)
    enr._sleep = sleeps.append
    enr._uniform = lambda lo, hi: hi  # deterministic: the jitter ceiling
    enr._get_primary_ip = lambda: None  # type: ignore[method-assign]
    return enr, sleeps, calls


def ok() -> httpx.Response:
    """A successful enrollment response."""
    return httpx.Response(201, json=enroll_ok_body())


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_transient_status_is_retried_then_succeeds(tmp_path: Path, status: int) -> None:
    """Retryable statuses are retried with exponential (jitter-ceiling) delays."""
    enr, sleeps, calls = _enr(tmp_path, [httpx.Response(status), httpx.Response(status), ok()])
    enr.enroll()
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_network_errors_are_retried(tmp_path: Path) -> None:
    """Transport failures are retried and eventually succeed."""
    enr, sleeps, calls = _enr(tmp_path, [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ok()])
    enr.enroll()
    assert len(calls) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_client_errors_are_never_retried(tmp_path: Path, status: int) -> None:
    """4xx (esp. 401/403) is a definitive answer: exactly one attempt, no sleep."""
    enr, sleeps, calls = _enr(tmp_path, [httpx.Response(status, json={"error": "no"})])
    with pytest.raises(EnrollmentError):
        enr.enroll()
    assert len(calls) == 1 and sleeps == []


def test_attempts_are_bounded_and_last_error_reported(tmp_path: Path) -> None:
    """After max attempts the last 5xx surfaces as EnrollmentError."""
    enr, sleeps, calls = _enr(
        tmp_path, [httpx.Response(503, json={"error": "busy"})], enroll_max_attempts=4
    )
    with pytest.raises(EnrollmentError, match="Enrollment failed: busy"):
        enr.enroll()
    assert len(calls) == 4 and len(sleeps) == 3


def test_network_error_after_exhaustion_is_wrapped(tmp_path: Path) -> None:
    """Persistent transport failure ends in the wrapped network EnrollmentError."""
    enr, _, calls = _enr(tmp_path, [httpx.ConnectError("down")], enroll_max_attempts=3)
    with pytest.raises(EnrollmentError, match="Network error during enrollment"):
        enr.enroll()
    assert len(calls) == 3


def test_delay_is_capped_by_max_delay(tmp_path: Path) -> None:
    """Backoff doubles but never exceeds enroll_max_delay_s."""
    enr, sleeps, _ = _enr(
        tmp_path, [httpx.Response(500)], enroll_max_attempts=6, enroll_max_delay_s=3.0
    )
    with pytest.raises(EnrollmentError):
        enr.enroll()
    assert sleeps == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_full_jitter_draws_from_zero_to_ceiling(tmp_path: Path) -> None:
    """The jitter range is [0, ceiling] (full jitter), not a fixed delay."""
    enr, _, _ = _enr(tmp_path, [httpx.Response(500), ok()])
    ranges: list[tuple[float, float]] = []

    def uniform(lo: float, hi: float) -> float:
        ranges.append((lo, hi))
        return 0.25

    enr._uniform = uniform
    enr.enroll()
    assert ranges == [(0.0, 1.0)]


def test_retry_after_seconds_is_honoured_on_429_and_503(tmp_path: Path) -> None:
    """Retry-After raises the delay to at least the server-requested value."""
    enr, sleeps, _ = _enr(
        tmp_path,
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503, headers={"Retry-After": "9"}),
            ok(),
        ],
    )
    enr._uniform = lambda lo, hi: 0.0
    enr.enroll()
    assert sleeps == [7.0, 9.0]


def test_retry_after_ignored_on_other_statuses(tmp_path: Path) -> None:
    """Retry-After on a 500 does not override the computed backoff."""
    enr, sleeps, _ = _enr(tmp_path, [httpx.Response(500, headers={"Retry-After": "50"}), ok()])
    enr.enroll()
    assert sleeps == [1.0]


def test_total_time_budget_stops_retrying(tmp_path: Path) -> None:
    """A Retry-After beyond the remaining total budget gives up instead of sleeping."""
    enr, sleeps, calls = _enr(
        tmp_path,
        [httpx.Response(503, headers={"Retry-After": "500"}, json={"error": "later"})],
        enroll_max_total_s=60.0,
    )
    with pytest.raises(EnrollmentError, match="later"):
        enr.enroll()
    assert len(calls) == 1 and sleeps == []


def test_total_time_budget_with_network_errors(tmp_path: Path) -> None:
    """Budget exhaustion during transport failures raises the wrapped network error."""
    enr, sleeps, calls = _enr(tmp_path, [httpx.ConnectError("x")], enroll_max_total_s=0.5)
    with pytest.raises(EnrollmentError, match="Network error during enrollment"):
        enr.enroll()
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12", 12.0),
        ("-5", 0.0),
        (None, None),
        ("", None),
        ("garbage", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    """Retry-After parsing handles seconds, dates (past => 0) and junk."""
    assert parse_retry_after(value) == expected


def test_parse_retry_after_future_http_date_and_naive_date() -> None:
    """A future HTTP date yields a positive wait; a tz-less date is treated as UTC."""
    future = datetime.now(timezone.utc) + timedelta(seconds=100)
    assert 90 < (parse_retry_after(format_datetime(future, usegmt=True)) or 0) <= 100
    naive = format_datetime(future.replace(tzinfo=None))
    assert (parse_retry_after(naive) or 0) > 90


def test_config_from_env_reads_retry_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry bounds are env-configurable."""
    monkeypatch.setenv("GOUGH_ENROLL_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("GOUGH_ENROLL_BASE_DELAY_S", "0.5")
    monkeypatch.setenv("GOUGH_ENROLL_MAX_DELAY_S", "11")
    monkeypatch.setenv("GOUGH_ENROLL_MAX_TOTAL_S", "99")
    cfg = AgentConfig.from_env()
    assert (cfg.enroll_max_attempts, cfg.enroll_base_delay_s) == (9, 0.5)
    assert (cfg.enroll_max_delay_s, cfg.enroll_max_total_s) == (11.0, 99.0)


def test_real_sleep_and_jitter_defaults_are_wired(tmp_path: Path) -> None:
    """Without injection the real time.sleep/random.uniform are used."""
    enr = make_enrollment(tmp_path, lambda r: ok())
    assert enr._sleep is time.sleep and enr._uniform is random.uniform


def test_token_save_is_atomic_and_leaves_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tokens land via rename (0600, no stray temp); a failed write keeps the old file."""
    enr = make_enrollment(tmp_path, lambda r: ok())
    enr.config.agent_id, enr.config.access_token, enr.config.refresh_token = "a", "t", "r"
    enr.save_tokens()
    path = Path(enr.config.token_file)
    assert sorted(p.name for p in path.parent.iterdir()) == ["tokens.json"]
    old = path.read_text()

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    enr.config.access_token = "new"
    with pytest.raises(OSError, match="disk full"):
        enr.save_tokens()
    assert path.read_text() == old
    assert sorted(p.name for p in path.parent.iterdir()) == ["tokens.json"]
