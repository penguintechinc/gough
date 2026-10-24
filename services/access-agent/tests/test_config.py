"""Tests for environment-driven agent configuration."""

from pathlib import Path

import pytest

from agent.config import AgentConfig


def test_token_file_defaults_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default token path is used when GOUGH_TOKEN_FILE is absent."""
    monkeypatch.delenv("GOUGH_TOKEN_FILE", raising=False)
    assert AgentConfig.from_env().token_file == "/var/lib/gough-agent/tokens.json"


def test_token_file_honors_gough_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GOUGH_TOKEN_FILE (set by the installer's agent.env) overrides the default."""
    target = tmp_path / "custom-tokens.json"
    monkeypatch.setenv("GOUGH_TOKEN_FILE", str(target))
    assert AgentConfig.from_env().token_file == str(target)
