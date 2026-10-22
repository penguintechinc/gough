"""CLI behaviour of ``app.openapi_export.main`` (write mode and ``--check`` drift gate).

Runs the real spec build into a temporary directory so the committed
``docs/api`` copy is never touched. Asserts exit codes, the files written, and
that ``--check`` fails closed on drift and on a missing spec file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import openapi_export


def test_main_writes_json_and_yaml_and_exits_zero(tmp_path: Path) -> None:
    """Default mode writes openapi.json and openapi.yaml into --out-dir and returns 0."""
    rc = openapi_export.main(["--out-dir", str(tmp_path)])

    assert rc == 0
    assert (tmp_path / "openapi.json").stat().st_size > 0
    assert (tmp_path / "openapi.yaml").stat().st_size > 0


def test_check_passes_immediately_after_write(tmp_path: Path) -> None:
    """--check is green when the on-disk JSON is exactly the regenerated spec."""
    assert openapi_export.main(["--out-dir", str(tmp_path)]) == 0

    assert openapi_export.main(["--out-dir", str(tmp_path), "--check"]) == 0


def test_check_fails_on_drifted_spec(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A hand-edited spec makes --check return 1 and tell the operator to regenerate."""
    assert openapi_export.main(["--out-dir", str(tmp_path)]) == 0
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(spec_path.read_text(encoding="utf-8") + "\n// drift", encoding="utf-8")

    rc = openapi_export.main(["--out-dir", str(tmp_path), "--check"])

    assert rc == 1
    assert "spec on disk is out of date" in capsys.readouterr().err


def test_check_fails_closed_when_spec_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No committed spec at all is treated as drift (empty on-disk copy), never a pass."""
    rc = openapi_export.main(["--out-dir", str(tmp_path), "--check"])

    assert rc == 1
    assert "make openapi" in capsys.readouterr().err
    assert not (tmp_path / "openapi.json").exists()  # --check must not write


def test_verbose_flag_still_writes_spec(tmp_path: Path) -> None:
    """-v raises log verbosity without changing the outcome."""
    assert openapi_export.main(["-v", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "openapi.json").exists()


def test_default_out_dir_points_at_repo_docs() -> None:
    """The default output location is <repo-root>/docs/api, four levels above the module."""
    out = openapi_export._default_out_dir()

    assert out.parts[-2:] == ("docs", "api")
    assert out.parents[0].name == "docs"
