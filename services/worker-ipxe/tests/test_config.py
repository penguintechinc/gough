"""Behaviour tests for worker.config: env loading, casting, validation and boot URL resolution."""

import netifaces
import pytest
from worker.config import WorkerConfig

# Every variable read by WorkerConfig.from_env(); cleared so the host environment cannot leak in.
_ENV_KEYS = (
    "API_MANAGER_URL",
    "WORKER_API_KEY",
    "WORKER_ID",
    "DHCP_MODE",
    "DHCP_INTERFACE",
    "DHCP_SUBNET",
    "DHCP_RANGE_START",
    "DHCP_RANGE_END",
    "DHCP_GATEWAY",
    "DHCP_DNS_SERVERS",
    "TFTP_ENABLED",
    "TFTP_PORT",
    "TFTP_ROOT",
    "HTTP_PORT",
    "HTTP_BOOT_URL",
    "STORAGE_ENDPOINT",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "STORAGE_BUCKET_BOOT_IMAGES",
    "STORAGE_BUCKET_BIOMES",
    "STORAGE_USE_SSL",
    "HEARTBEAT_INTERVAL",
    "ENROLLMENT_RETRY_INTERVAL",
    "MAX_ENROLLMENT_RETRIES",
    "LOG_LEVEL",
    "METRICS_ENABLED",
    "METRICS_PORT",
    "REDIS_URL",
)
GOOD_KEY = "k" * 32


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove all worker env vars so each test starts from defaults."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOSTNAME", "host-from-env")
    monkeypatch.setenv("WORKER_API_KEY", GOOD_KEY)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    return monkeypatch


def _cfg(**overrides: object) -> WorkerConfig:
    """Build a minimal WorkerConfig with overridable fields."""
    base: dict[str, object] = {
        "api_manager_url": "http://api.test",
        "worker_api_key": "key",
        "worker_id": "w1",
        "dhcp_mode": "proxy",
        "dhcp_interface": "eth-does-not-exist0",
        "http_port": 8080,
    }
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


def test_defaults_when_env_empty(clean_env: pytest.MonkeyPatch) -> None:
    """With no env set, documented defaults apply and worker_id falls back to HOSTNAME."""
    cfg = WorkerConfig.from_env()

    assert cfg.api_manager_url == "http://api-manager:5000"
    assert cfg.worker_api_key == GOOD_KEY
    assert cfg.redis_url == "redis://redis:6379/0"
    assert cfg.worker_id == "host-from-env"
    assert cfg.dhcp_mode == "proxy"
    assert cfg.dhcp_dns_servers == "8.8.8.8,8.8.4.4"
    assert cfg.tftp_enabled is True
    assert cfg.tftp_port == 69
    assert cfg.http_port == 8080
    assert cfg.heartbeat_interval == 30
    assert cfg.max_enrollment_retries == 60
    assert cfg.metrics_enabled is True
    assert cfg.metrics_port == 9090
    assert cfg.storage_bucket_boot == "boot-images"
    assert cfg.storage_use_ssl is False


def test_missing_worker_api_key_fails_startup(clean_env: pytest.MonkeyPatch) -> None:
    """With WORKER_API_KEY unset there is no default: startup fails loudly."""
    clean_env.delenv("WORKER_API_KEY")

    with pytest.raises(ValueError, match="WORKER_API_KEY must be set"):
        WorkerConfig.from_env()


@pytest.mark.parametrize("key", ["", "worker-secret", "k" * 31])
def test_short_worker_api_key_fails_startup_without_echoing_it(
    clean_env: pytest.MonkeyPatch, key: str
) -> None:
    """Keys under 32 characters (incl. the old default) are refused and never appear in the error."""
    clean_env.setenv("WORKER_API_KEY", key)

    with pytest.raises(ValueError) as excinfo:
        WorkerConfig.from_env()

    assert "at least 32" in str(excinfo.value)
    if key:
        assert key not in str(excinfo.value)


def test_exactly_32_char_key_is_accepted(clean_env: pytest.MonkeyPatch) -> None:
    """The 32-character boundary is inclusive."""
    clean_env.setenv("WORKER_API_KEY", "x" * 32)

    assert WorkerConfig.from_env().worker_api_key == "x" * 32


def test_missing_redis_url_fails_startup(clean_env: pytest.MonkeyPatch) -> None:
    """Boot tokens need a shared store, so REDIS_URL is mandatory."""
    clean_env.delenv("REDIS_URL")

    with pytest.raises(ValueError, match="REDIS_URL must be set"):
        WorkerConfig.from_env()


def test_secrets_are_not_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    """repr() (used by loggers/tracebacks) must not expose the API key, Redis URL or storage secret."""
    clean_env.setenv("STORAGE_SECRET_KEY", "storage-secret-value")

    text = repr(WorkerConfig.from_env())

    assert GOOD_KEY not in text
    assert "redis://" not in text
    assert "storage-secret-value" not in text


def test_trusted_proxies_parsed_and_defaults_empty(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """TRUSTED_PROXIES is a comma-separated CIDR list; unset means no proxy is trusted."""
    assert WorkerConfig.from_env().trusted_proxies == ()

    clean_env.setenv("TRUSTED_PROXIES", "10.0.0.0/8, 192.168.1.0/24,")

    assert WorkerConfig.from_env().trusted_proxies == ("10.0.0.0/8", "192.168.1.0/24")


def test_invalid_trusted_proxy_cidr_fails_startup(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A malformed CIDR fails loudly instead of silently trusting nothing or everything."""
    clean_env.setenv("TRUSTED_PROXIES", "not-a-cidr")

    with pytest.raises(ValueError):
        WorkerConfig.from_env()


def test_explicit_env_overrides_defaults_and_casts(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Env values are cast to bool/int and override defaults; WORKER_ID beats HOSTNAME."""
    clean_env.setenv("WORKER_ID", "explicit-id")
    clean_env.setenv("TFTP_ENABLED", "False")
    clean_env.setenv("TFTP_PORT", "6969")
    clean_env.setenv("HTTP_PORT", "9000")
    clean_env.setenv("STORAGE_USE_SSL", "True")
    clean_env.setenv("HEARTBEAT_INTERVAL", "5")
    clean_env.setenv("MAX_ENROLLMENT_RETRIES", "3")
    clean_env.setenv("METRICS_ENABLED", "False")
    clean_env.setenv("DHCP_MODE", "disabled")

    cfg = WorkerConfig.from_env()

    assert cfg.worker_id == "explicit-id"
    assert cfg.tftp_enabled is False
    assert cfg.tftp_port == 6969
    assert cfg.http_port == 9000
    assert cfg.storage_use_ssl is True
    assert cfg.heartbeat_interval == 5
    assert cfg.max_enrollment_retries == 3
    assert cfg.metrics_enabled is False
    assert cfg.dhcp_mode == "disabled"


@pytest.mark.parametrize("bad_mode", ["dhcp", "FULL", "", "relay"])
def test_invalid_dhcp_mode_rejected(
    clean_env: pytest.MonkeyPatch, bad_mode: str
) -> None:
    """Only full, proxy and disabled are accepted; anything else raises ValueError naming the value."""
    clean_env.setenv("DHCP_MODE", bad_mode)

    with pytest.raises(ValueError, match="Invalid DHCP_MODE"):
        WorkerConfig.from_env()


def test_full_mode_requires_all_dhcp_fields(clean_env: pytest.MonkeyPatch) -> None:
    """DHCP_MODE=full with any of subnet/range/gateway missing fails validation."""
    clean_env.setenv("DHCP_MODE", "full")
    clean_env.setenv("DHCP_SUBNET", "10.0.0.0/24")
    clean_env.setenv("DHCP_RANGE_START", "10.0.0.100")
    clean_env.setenv("DHCP_RANGE_END", "10.0.0.200")
    # DHCP_GATEWAY deliberately missing

    with pytest.raises(ValueError, match="DHCP_MODE=full requires"):
        WorkerConfig.from_env()


def test_full_mode_accepts_complete_dhcp_fields(clean_env: pytest.MonkeyPatch) -> None:
    """DHCP_MODE=full loads when all four DHCP fields are present."""
    clean_env.setenv("DHCP_MODE", "full")
    clean_env.setenv("DHCP_SUBNET", "10.0.0.0/24")
    clean_env.setenv("DHCP_RANGE_START", "10.0.0.100")
    clean_env.setenv("DHCP_RANGE_END", "10.0.0.200")
    clean_env.setenv("DHCP_GATEWAY", "10.0.0.1")

    cfg = WorkerConfig.from_env()

    assert cfg.dhcp_mode == "full"
    assert cfg.dhcp_subnet == "10.0.0.0/24"
    assert cfg.dhcp_gateway == "10.0.0.1"


def test_non_integer_port_raises(clean_env: pytest.MonkeyPatch) -> None:
    """A non-numeric integer setting fails loudly instead of defaulting."""
    clean_env.setenv("HTTP_PORT", "eighty")

    with pytest.raises(ValueError):
        WorkerConfig.from_env()


def test_boot_url_prefers_explicit_http_boot_url() -> None:
    """HTTP_BOOT_URL, when set, is returned verbatim without probing interfaces."""
    cfg = _cfg(http_boot_url="https://boot.example.com")

    assert cfg.get_boot_url() == "https://boot.example.com"


def test_boot_url_uses_interface_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit URL, the interface's first IPv4 address and HTTP port form the URL."""
    monkeypatch.setattr(
        netifaces,
        "ifaddresses",
        lambda iface: {netifaces.AF_INET: [{"addr": "10.9.8.7"}]},
    )
    cfg = _cfg(dhcp_interface="eth0", http_port=8123)

    assert cfg.get_boot_url() == "http://10.9.8.7:8123"


def test_boot_url_falls_back_to_localhost_when_interface_has_no_ipv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interface with no IPv4 address falls back to localhost."""
    monkeypatch.setattr(netifaces, "ifaddresses", lambda iface: {})
    cfg = _cfg(http_port=8081)

    assert cfg.get_boot_url() == "http://localhost:8081"


def test_boot_url_falls_back_to_localhost_when_interface_missing() -> None:
    """A nonexistent interface raises inside netifaces; the error is swallowed and localhost is used."""
    cfg = _cfg(dhcp_interface="definitely-not-a-nic", http_port=8082)

    assert cfg.get_boot_url() == "http://localhost:8082"


def test_worker_config_is_slotted() -> None:
    """WorkerConfig is a slots dataclass, so unknown attributes cannot be set."""
    cfg = _cfg()

    with pytest.raises(AttributeError):
        cfg.unknown_field = "x"  # type: ignore[attr-defined]
