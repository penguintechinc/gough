"""Cloud provider registry factory and OTEL env-var parsing helpers.

``get_cloud_provider`` must fail closed on unknown types (listing what is
available) and wrap constructor failures as CloudError. ``_parse_kv_csv`` and
``_build_resource`` turn ``OTEL_*`` env strings into the resource attributes the
exporters receive; malformed fragments must be dropped, never raise.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.clouds as clouds
from app import observability
from app.clouds.base import BaseCloud, CloudError


class _GoodCloud(BaseCloud):
    """Provider whose constructor records the config it received."""

    provider_type = "good"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)

    def authenticate(self) -> bool:
        return True

    def list_machines(self, filters: dict | None = None) -> list:
        return []

    def get_machine(self, machine_id: str) -> Any:
        raise NotImplementedError

    def create_machine(self, spec: Any) -> Any:
        raise NotImplementedError

    def destroy_machine(self, machine_id: str) -> bool:
        return True

    def start_machine(self, machine_id: str) -> bool:
        return True

    def stop_machine(self, machine_id: str) -> bool:
        return True


class _BrokenCloud(_GoodCloud):
    """Provider whose constructor fails, e.g. missing credentials."""

    def __init__(self, config: dict[str, Any]) -> None:
        raise KeyError("api_key")


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[BaseCloud]]:
    """Isolated registry holding only the test providers."""
    reg: dict[str, type[BaseCloud]] = {"good": _GoodCloud, "broken": _BrokenCloud}
    monkeypatch.setattr(clouds, "CLOUD_REGISTRY", reg)
    return reg


def test_factory_builds_registered_provider_with_config(registry: dict) -> None:
    """A known type (case-insensitive) is constructed with the supplied config."""
    provider = clouds.get_cloud_provider("GOOD", {"region": "dc1"})

    assert isinstance(provider, _GoodCloud)
    assert provider.config == {"region": "dc1"}


def test_factory_unknown_type_lists_available_providers(registry: dict) -> None:
    """An unknown provider type raises CloudError naming the type and the available set."""
    with pytest.raises(CloudError, match=r"Unknown cloud provider: nimbus\. Available: \['good', 'broken'\]"):
        clouds.get_cloud_provider("nimbus", {})


def test_factory_wraps_constructor_failure(registry: dict) -> None:
    """A constructor exception is re-raised as CloudError naming the provider."""
    with pytest.raises(CloudError, match="Failed to initialize broken provider"):
        clouds.get_cloud_provider("broken", {})


def test_register_cloud_decorator_adds_class_and_returns_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The register_cloud decorator records the class under the name and returns it unchanged."""
    reg: dict[str, type[BaseCloud]] = {}
    monkeypatch.setattr(clouds, "CLOUD_REGISTRY", reg)

    decorated = clouds.register_cloud("fresh")(_GoodCloud)

    assert decorated is _GoodCloud
    assert reg == {"fresh": _GoodCloud}


def test_parse_kv_csv_trims_and_skips_malformed_fragments() -> None:
    """Empty fragments and keyless pairs are dropped; keys and values are whitespace-trimmed."""
    raw = " team = core , ,=orphan, env=prod=eu ,  "

    assert observability._parse_kv_csv(raw) == {"team": "core", "env": "prod=eu"}


def test_parse_kv_csv_empty_input() -> None:
    """An empty string parses to an empty mapping."""
    assert observability._parse_kv_csv("") == {}


def test_build_resource_defaults_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without OTEL_SERVICE_NAME the resource is named gough-api-manager."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)

    attrs = dict(observability._build_resource().attributes)

    assert attrs["service.name"] == "gough-api-manager"


def test_build_resource_merges_resource_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """OTEL_RESOURCE_ATTRIBUTES extends the resource and can override the service name."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "custom")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=beta,team=infra")

    attrs = dict(observability._build_resource().attributes)

    assert attrs["service.name"] == "custom"
    assert attrs["deployment.environment"] == "beta"
    assert attrs["team"] == "infra"
