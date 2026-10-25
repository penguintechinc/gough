"""Behaviour tests for AgentConfig env parsing, YAML loading and directory setup."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.config import AgentConfig
from agent.host_resources import DEFAULT_LXD_SOCKET

ENV_KEYS = [
    "GOUGH_MANAGEMENT_SERVER",
    "GOUGH_ENROLLMENT_KEY",
    "GOUGH_HOSTNAME",
    "GOUGH_RSSH_HOST",
    "GOUGH_RSSH_PORT",
    "GOUGH_HEARTBEAT_INTERVAL",
    "GOUGH_SNAPSHOT_TIMEOUT_S",
    "GOUGH_LXD_SOCKET",
    "GOUGH_VERIFY_SSL",
    "GOUGH_LOG_LEVEL",
    "GOUGH_CAPABILITIES",
    "GOUGH_TOKEN_FILE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every GOUGH_* variable so each test starts from defaults."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_when_env_is_empty() -> None:
    """With no GOUGH_* variables the documented defaults apply."""
    cfg = AgentConfig.from_env()
    assert cfg.management_server_url == "https://gough.local"
    assert cfg.enrollment_key is None
    assert cfg.rssh_listen_host == "0.0.0.0"
    assert cfg.rssh_listen_port == 2222
    assert cfg.heartbeat_interval == 30
    assert cfg.snapshot_timeout_s == 5.0
    assert cfg.lxd_socket == DEFAULT_LXD_SOCKET
    assert cfg.verify_ssl is True
    assert cfg.log_level == "INFO"
    assert cfg.capabilities == ["ssh"]


def test_all_overrides_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every supported GOUGH_* variable is mapped onto its config field with the right type."""
    monkeypatch.setenv("GOUGH_MANAGEMENT_SERVER", "https://mgmt.example")
    monkeypatch.setenv("GOUGH_ENROLLMENT_KEY", "ek-1")
    monkeypatch.setenv("GOUGH_HOSTNAME", "box-7")
    monkeypatch.setenv("GOUGH_RSSH_HOST", "127.0.0.1")
    monkeypatch.setenv("GOUGH_RSSH_PORT", "2299")
    monkeypatch.setenv("GOUGH_HEARTBEAT_INTERVAL", "12")
    monkeypatch.setenv("GOUGH_SNAPSHOT_TIMEOUT_S", "1.5")
    monkeypatch.setenv("GOUGH_LXD_SOCKET", "/run/lxd.sock")
    monkeypatch.setenv("GOUGH_VERIFY_SSL", "TRUE")
    monkeypatch.setenv("GOUGH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("GOUGH_CAPABILITIES", "ssh,metrics")
    monkeypatch.setenv("GOUGH_TOKEN_FILE", "/tmp/x/tokens.json")

    cfg = AgentConfig.from_env()
    assert cfg.management_server_url == "https://mgmt.example"
    assert cfg.enrollment_key == "ek-1"
    assert cfg.hostname == "box-7"
    assert cfg.rssh_listen_host == "127.0.0.1"
    assert cfg.rssh_listen_port == 2299
    assert cfg.heartbeat_interval == 12
    assert cfg.snapshot_timeout_s == 1.5
    assert cfg.lxd_socket == "/run/lxd.sock"
    assert cfg.verify_ssl is True
    assert cfg.log_level == "DEBUG"
    assert cfg.capabilities == ["ssh", "metrics"]
    assert cfg.token_file == "/tmp/x/tokens.json"


@pytest.mark.parametrize("raw", ["false", "0", "no", "FALSE-ish"])
def test_verify_ssl_only_true_string_enables_verification(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Anything other than 'true' (case-insensitive) disables TLS verification."""
    monkeypatch.setenv("GOUGH_VERIFY_SSL", raw)
    assert AgentConfig.from_env().verify_ssl is False


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("GOUGH_RSSH_PORT", "not-a-port"),
        ("GOUGH_HEARTBEAT_INTERVAL", "thirty"),
        ("GOUGH_SNAPSHOT_TIMEOUT_S", "soon"),
    ],
)
def test_invalid_numeric_values_raise(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    """Non-numeric values for numeric settings fail loudly at load time."""
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError):
        AgentConfig.from_env()


def test_from_file_missing_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing YAML file defers to environment loading."""
    monkeypatch.setenv("GOUGH_HOSTNAME", "from-env")
    cfg = AgentConfig.from_file(str(tmp_path / "absent.yaml"))
    assert cfg.hostname == "from-env"


def test_from_file_empty_document_uses_defaults(tmp_path: Path) -> None:
    """An empty YAML file loads as an empty mapping and yields the built-in defaults."""
    path = tmp_path / "agent.yaml"
    path.write_text("")
    cfg = AgentConfig.from_file(str(path))
    assert cfg.management_server_url == "https://gough.local"
    assert cfg.capabilities == ["ssh"]
    assert cfg.heartbeat_interval == 30


def test_from_file_reads_yaml_values(tmp_path: Path) -> None:
    """YAML keys override defaults for the fields the file loader understands."""
    path = tmp_path / "agent.yaml"
    path.write_text(
        "management_server_url: https://yaml.example\n"
        "enrollment_key: yaml-key\n"
        "hostname: yaml-host\n"
        "rssh_listen_port: 2400\n"
        "heartbeat_interval: 60\n"
        "verify_ssl: false\n"
        "log_level: WARNING\n"
        "capabilities: [ssh, files]\n"
    )
    cfg = AgentConfig.from_file(str(path))
    assert cfg.management_server_url == "https://yaml.example"
    assert cfg.enrollment_key == "yaml-key"
    assert cfg.hostname == "yaml-host"
    assert cfg.rssh_listen_port == 2400
    assert cfg.heartbeat_interval == 60
    assert cfg.verify_ssl is False
    assert cfg.log_level == "WARNING"
    assert cfg.capabilities == ["ssh", "files"]


def test_from_file_uses_env_for_absent_yaml_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keys missing from YAML fall back to the matching GOUGH_* env variable."""
    monkeypatch.setenv("GOUGH_MANAGEMENT_SERVER", "https://env.example")
    monkeypatch.setenv("GOUGH_ENROLLMENT_KEY", "env-key")
    path = tmp_path / "agent.yaml"
    path.write_text("hostname: only-host\n")
    cfg = AgentConfig.from_file(str(path))
    assert cfg.management_server_url == "https://env.example"
    assert cfg.enrollment_key == "env-key"
    assert cfg.hostname == "only-host"


def test_ensure_directories_creates_parents(tmp_path: Path) -> None:
    """ensure_directories creates the parent directory of every managed file path."""
    cfg = AgentConfig(
        token_file=str(tmp_path / "a" / "tokens.json"),
        ca_public_key_file=str(tmp_path / "b" / "ca.pub"),
        host_key_file=str(tmp_path / "c" / "host_key"),
        log_file=str(tmp_path / "d" / "agent.log"),
    )
    cfg.ensure_directories()
    for parent in ("a", "b", "c", "d"):
        assert (tmp_path / parent).is_dir()


def test_ensure_directories_skips_unset_log_file(tmp_path: Path) -> None:
    """A disabled (None) log_file is skipped without error."""
    cfg = AgentConfig(
        token_file=str(tmp_path / "t" / "tokens.json"),
        ca_public_key_file=str(tmp_path / "t" / "ca.pub"),
        host_key_file=str(tmp_path / "t" / "host_key"),
        log_file=None,
    )
    cfg.ensure_directories()
    assert (tmp_path / "t").is_dir()
