"""Tests proving the count ratchet fails on regression and zero examined, and passes on equal."""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "quality_ratchet.py"
RUFF_TOML = Path(__file__).resolve().parent.parent / "ruff.toml"
TOOLS = "ruff_check,ruff_format"
CLEAN = "x = 1\n"
DIRTY = "import os\n"  # F401 unused import


def git(repo: Path, *args: str) -> None:
    """Run a git command in the temp repo."""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # nosec B603 B607


def ratchet(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the ratchet CLI inside the temp repo."""
    return subprocess.run(  # nosec B603
        [sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, check=False
    )


def check(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run `check` against HEAD as the base ref with only the ruff tools."""
    return ratchet(repo, "check", "--tools", TOOLS, "--base-ref", "HEAD", *extra)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed temp repo with one clean file, one dirty file and a baseline."""
    shutil.copy(RUFF_TOML, tmp_path / "ruff.toml")
    (tmp_path / "clean.py").write_text(CLEAN)
    (tmp_path / "dirty.py").write_text(DIRTY)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    assert ratchet(tmp_path, "update", "--tools", TOOLS).returncode == 0
    return tmp_path


def baseline(repo: Path) -> dict:
    """Read the temp repo's baseline."""
    return json.loads((repo / ".quality-baseline.json").read_text())


def test_equal_counts_pass(repo: Path) -> None:
    """Unchanged counts pass and report what was examined."""
    proc = check(repo)
    assert proc.returncode == 0, proc.stdout
    assert baseline(repo)["tools"]["ruff_check"]["findings"] == 1
    assert "examined=2" in proc.stdout


def test_count_increase_fails(repo: Path) -> None:
    """A new finding in an existing file raises the count and fails the gate."""
    (repo / "dirty.py").write_text("import os\nimport sys\n")
    proc = check(repo)
    assert proc.returncode == 1
    assert "regression" in proc.stdout


def test_count_decrease_passes_and_asks_to_lower(repo: Path) -> None:
    """A lower count passes, says so, and `update` lowers the baseline."""
    (repo / "dirty.py").write_text(CLEAN)
    proc = check(repo)
    assert proc.returncode == 0
    assert "DECREASED" in proc.stdout
    assert ratchet(repo, "update", "--tools", TOOLS).returncode == 0
    assert baseline(repo)["tools"]["ruff_check"]["findings"] == 0


def test_update_never_raises_baseline(repo: Path) -> None:
    """`update` leaves a baseline untouched when the current count is higher."""
    (repo / "dirty.py").write_text("import os\nimport sys\n")
    ratchet(repo, "update", "--tools", TOOLS)
    assert baseline(repo)["tools"]["ruff_check"]["findings"] == 1


def test_dirty_new_file_fails_even_with_matching_baseline(repo: Path) -> None:
    """A new file with a finding fails although the total equals a hand-raised baseline."""
    (repo / "added.py").write_text(DIRTY)
    data = baseline(repo)
    data["tools"]["ruff_check"]["findings"] = 2
    (repo / ".quality-baseline.json").write_text(json.dumps(data))
    proc = check(repo)
    assert proc.returncode == 1
    assert "new file added.py" in proc.stdout


def test_clean_new_file_passes(repo: Path) -> None:
    """A new clean file does not trip the gate."""
    (repo / "added.py").write_text(CLEAN)
    assert check(repo).returncode == 0


def test_zero_examined_fails(tmp_path: Path) -> None:
    """A repo with no Python files examines nothing, which is a failure."""
    shutil.copy(RUFF_TOML, tmp_path / "ruff.toml")
    git(tmp_path, "init", "-q")
    (tmp_path / "readme.txt").write_text("x\n")
    (tmp_path / ".quality-baseline.json").write_text(json.dumps({"tools": {"ruff_check": {"findings": 0}}}))
    git(tmp_path, "add", "-A")
    proc = ratchet(tmp_path, "check", "--tools", "ruff_check", "--skip-new-files")
    assert proc.returncode == 1
    assert "0 files examined" in proc.stdout


def test_staged_new_dirty_file_fails_and_worse_modified_fails(repo: Path) -> None:
    """Staged mode rejects a dirty new file and a modified file that got worse, allows equal."""
    (repo / "dirty.py").write_text(DIRTY)  # same findings as HEAD
    git(repo, "add", "-A")
    (repo / "dirty.py").write_text("import os\n")
    assert ratchet(repo, "staged").returncode == 0  # nothing staged differs
    (repo / "dirty.py").write_text("import os\nimport sys\n")
    (repo / "new.py").write_text(DIRTY)
    git(repo, "add", "-A")
    proc = ratchet(repo, "staged")
    assert proc.returncode == 1
    assert "dirty.py" in proc.stdout and "new.py" in proc.stdout
