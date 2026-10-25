"""Tests for airgap Cilium rendering, the k8s-controller rename and alias, and route wiring.

Covers: mirror-only cilium install (no upstream hosts), loud failure on missing or
insecure mirror settings, the static guard that app/ never references
raw.githubusercontent.com or helm.cilium.io, the k8s-primary alias, the idempotent
rename migration (single head and data behaviour on SQLite), and LXD join
blueprint registration.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app import catalog
from app.api import lxd_cluster
from app.api.lxd_cluster import lxd_bp
from app.mirrors import MirrorConfig, MirrorConfigError, load_mirror_config
from app.workers.plan_compiler import _render_cni_install
from quart import Quart

APP_DIR = Path(__file__).resolve().parents[2] / "app"
VERSIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
UPSTREAM_MARKERS = ("raw.githubusercontent.com", "helm.cilium.io")

MIRROR = MirrorConfig(
    chart_mirror_url="https://mirror.airgap.test/charts",
    cilium_chart_sha256="a" * 64,
    cilium_crds_url="https://mirror.airgap.test/cilium/cilium-crds-v1.20.1.yaml",
    cilium_crds_sha256="b" * 64,
    gateway_api_crds_url="https://mirror.airgap.test/gateway/standard-install.yaml",
    gateway_api_crds_sha256="c" * 64,
    registry_mirror="https://registry.airgap.test:5000",
    mirrored_registries=("docker.io", "quay.io", "registry.k8s.io", "ghcr.io"),
)

MIRROR_ENV = {
    "GOUGH_CHART_MIRROR_URL": MIRROR.chart_mirror_url,
    "GOUGH_CILIUM_CHART_SHA256": MIRROR.cilium_chart_sha256,
    "GOUGH_CILIUM_CRDS_URL": MIRROR.cilium_crds_url,
    "GOUGH_CILIUM_CRDS_SHA256": MIRROR.cilium_crds_sha256,
    "GOUGH_GATEWAY_API_CRDS_URL": MIRROR.gateway_api_crds_url,
    "GOUGH_GATEWAY_API_CRDS_SHA256": MIRROR.gateway_api_crds_sha256,
    "GOUGH_REGISTRY_MIRROR": MIRROR.registry_mirror,
}


def _render(params: dict[str, object] | None = None, mirrors: MirrorConfig | None = MIRROR):  # type: ignore[no-untyped-def]
    """Render the cilium install for the control plane endpoint used in fixtures."""
    return _render_cni_install(
        "cilium", params or {}, "10.2.0.10:6443", mirror_config=mirrors
    )


def _load_migration(stem: str) -> ModuleType:
    """Import an alembic revision module from its file path (versions/ is not a package)."""
    path = VERSIONS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Airgap cilium rendering
# ---------------------------------------------------------------------------


def test_render_is_mirror_only() -> None:
    """Every artifact fetch targets the mirror; no upstream host appears in any emitted step."""
    write_files, runcmds, errors = _render()
    assert errors == []
    blob = "\n".join(runcmds) + "\n".join(f["content"] for f in write_files)
    for marker in (*UPSTREAM_MARKERS, "github.com", "pkgs.k8s.io"):
        assert marker not in blob, f"upstream host {marker!r} leaked into airgap render"
    assert "https://mirror.airgap.test/charts/cilium-1.20.1.tgz" in blob
    assert "https://mirror.airgap.test/cilium/cilium-crds-v1.20.1.yaml" in blob
    assert "https://mirror.airgap.test/gateway/standard-install.yaml" in blob


def test_render_verifies_every_artifact_before_apply() -> None:
    """Three artifacts (Gateway CRDs, Cilium CRDs, chart) each get a sha256 check before use."""
    _, runcmds, _ = _render()
    verify_steps = [cmd for cmd in runcmds if "sha256sum -c -" in cmd]
    assert len(verify_steps) == 3
    for sha in (
        MIRROR.gateway_api_crds_sha256,
        MIRROR.cilium_crds_sha256,
        MIRROR.cilium_chart_sha256,
    ):
        assert any(sha in cmd for cmd in verify_steps)
    # Each verify step must fail the run, never warn.
    assert all("|| exit 1" in cmd for cmd in verify_steps)


def test_render_installs_chart_from_local_tarball() -> None:
    """Helm installs from the verified local tgz for the pinned Cilium version."""
    _, runcmds, _ = _render()
    installs = [cmd for cmd in runcmds if cmd.startswith("helm install cilium")]
    assert installs == [
        "helm install cilium /var/lib/gough/airgap/cilium-1.20.1.tgz "
        "--namespace kube-system --create-namespace -f /etc/gough/cilium-values.yaml --wait --timeout 10m"
    ]
    assert not any("helm repo add" in cmd for cmd in runcmds)


def test_render_default_version_is_1_20_1() -> None:
    """Without an explicit version the catalog's Cilium release is rendered."""
    _, runcmds, _ = _render({})
    assert any("cilium-1.20.1.tgz" in cmd for cmd in runcmds)


def test_render_writes_containerd_mirror_per_registry() -> None:
    """Each mirrored registry gets a hosts.toml that routes pulls to the mirror."""
    write_files, runcmds, _ = _render()
    hosts = {
        f["path"]: f["content"] for f in write_files if f["path"].endswith("hosts.toml")
    }
    assert set(hosts) == {
        "/etc/containerd/certs.d/docker.io/hosts.toml",
        "/etc/containerd/certs.d/quay.io/hosts.toml",
        "/etc/containerd/certs.d/registry.k8s.io/hosts.toml",
        "/etc/containerd/certs.d/ghcr.io/hosts.toml",
    }
    assert (
        'server = "https://registry-1.docker.io"'
        in hosts["/etc/containerd/certs.d/docker.io/hosts.toml"]
    )
    assert (
        f'[host."{MIRROR.registry_mirror}"]'
        in hosts["/etc/containerd/certs.d/quay.io/hosts.toml"]
    )
    assert any("config_path" in cmd and "FATAL" in cmd for cmd in runcmds)


def test_render_fails_loudly_without_mirror_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No mirror env means a validation error naming the missing key, not an upstream fallback."""
    for key in MIRROR_ENV:
        monkeypatch.delenv(key, raising=False)
    write_files, runcmds, errors = _render_cni_install("cilium", {}, "10.2.0.10:6443")
    assert write_files == [] and runcmds == []
    assert len(errors) == 1
    assert errors[0].code == "cloud_init_validation_error"
    assert "GOUGH_CHART_MIRROR_URL is required" in errors[0].message


def test_render_rejects_invalid_version() -> None:
    """A version that could inject shell or change the path is refused."""
    _, _, errors = _render({"version": "1.20.1;rm -rf /"})
    assert len(errors) == 1
    assert "Invalid Cilium version" in errors[0].message


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("GOUGH_CHART_MIRROR_URL", "http://mirror.airgap.test/charts", "https URL"),
        ("GOUGH_CILIUM_CRDS_SHA256", "not-hex", "64-character hex sha256"),
        ("GOUGH_REGISTRY_MIRROR", "https://x y", "https URL"),
    ],
)
def test_mirror_settings_validated(key: str, value: str, message: str) -> None:
    """Insecure or malformed mirror settings are rejected at load time with the key named."""
    env = {**MIRROR_ENV, key: value}
    with pytest.raises(MirrorConfigError, match=message):
        load_mirror_config(env)


def test_load_mirror_config_defaults_registries() -> None:
    """Mirrored registries default to the four upstream registries when unset."""
    config = load_mirror_config(MIRROR_ENV)
    assert config.mirrored_registries == (
        "docker.io",
        "quay.io",
        "registry.k8s.io",
        "ghcr.io",
    )
    assert (
        config.cilium_chart_url("1.20.1")
        == "https://mirror.airgap.test/charts/cilium-1.20.1.tgz"
    )


# ---------------------------------------------------------------------------
# Static guard: no upstream hosts in the hub code
# ---------------------------------------------------------------------------


def test_no_upstream_cilium_hosts_in_app() -> None:
    """app/ must never reference raw.githubusercontent.com or helm.cilium.io (airgap contract)."""
    scanned = sorted(APP_DIR.rglob("*.py"))
    assert scanned, "scanner pointed at the wrong root"
    offenders = [
        f"{path.relative_to(APP_DIR)}: {marker}"
        for path in scanned
        for marker in UPSTREAM_MARKERS
        if marker in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_plan_compiler_has_no_github_fetches() -> None:
    """The plan compiler renders no github.com URLs at all (Gateway CRDs come from the mirror)."""
    source = (APP_DIR / "workers" / "plan_compiler.py").read_text(encoding="utf-8")
    assert "github.com" not in source


# ---------------------------------------------------------------------------
# Catalog and k8s-controller rename
# ---------------------------------------------------------------------------


def test_builtin_controller_biome_renamed() -> None:
    """The built-in control plane biome is k8s-controller and the legacy name is gone."""
    names = [b["name"] for b in catalog.BUILTIN_BIOMES]
    assert "k8s-controller" in names
    assert "k8s-primary" not in names


def test_catalog_pins_cilium_1_20_1_with_mirror_chart_source() -> None:
    """Catalog Cilium matches the biomes repo and carries no upstream chart repository."""
    cni = next(b for b in catalog.BUILTIN_BIOMES if b["name"] == "k8s-controller")[
        "default_cni"
    ]
    assert cni["version"] == "1.20.1"
    assert cni["chart_source"] == "mirror"
    assert "chart_repo" not in cni
    digests = cni["image_digests"]
    assert digests["cilium"].endswith(
        "sha256:ae9ea21f7427fe24bc6ea7247eb552157a1b0a431744045d3f641545ca71d11b"
    )
    assert digests["operator_generic"].endswith(
        "sha256:6c3885fc7b629099fdbe2a5c87869c86feb825fa18fae299eac0f61918d16ecf"
    )
    assert digests["hubble_relay"].endswith(
        "sha256:59be0ae7d475ab9011a5e954618c0f27b5778b17140381425b308b55ba4917f4"
    )


def test_legacy_alias_maps_with_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """k8s-primary is accepted as input, mapped to k8s-controller, and logs a deprecation warning."""
    with caplog.at_level(logging.WARNING, logger="app.catalog"):
        assert catalog.canonical_biome_name("k8s-primary") == "k8s-controller"
    assert any("deprecated biome name" in rec.getMessage() for rec in caplog.records)


def test_non_alias_names_pass_through_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Names that are not aliases are unchanged and produce no warning."""
    with caplog.at_level(logging.WARNING, logger="app.catalog"):
        assert catalog.canonical_biome_name("k8s-worker") == "k8s-worker"
    assert caplog.records == []


def test_biome_create_schema_applies_alias() -> None:
    """The biome create DTO maps the legacy name, so the API stores k8s-controller."""
    from app.api._biome_schema import BiomeCreate

    fields = {
        "name": "k8s-primary",
        "display_name": "K8s Controller",
        "biome_type": "lxd_container",
        "version": "1.0.0",
    }
    assert BiomeCreate.model_validate(fields).name == "k8s-controller"


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_rename_migration_chains_after_lxd_head() -> None:
    """The rename migration follows the LXD join revision (single-head test covers the rest)."""
    module = _load_migration("20261022_1300_rename_k8s_primary_biome")
    assert module.down_revision == "20261022_1200_lxd_join_issuance"


def test_bundle_cache_migration_is_the_head() -> None:
    """The bundle cache revision is the single head after the rename revision."""
    module = _load_migration("20261022_1400_biome_bundle_cache")
    assert module.down_revision == "20261022_1300_rename_k8s_primary_biome"


def _sqlite_biomes(conn: sa.Connection) -> None:
    """Create a minimal biomes table and seed legacy and built-in rows."""
    conn.execute(
        sa.text(
            "CREATE TABLE biomes (id INTEGER PRIMARY KEY, name VARCHAR(255), egg_kind VARCHAR(64))"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO biomes (name, egg_kind) VALUES "
            "('k8s-primary', 'k8s-primary'), ('nest-agent', NULL)"
        )
    )


def _names(conn: sa.Connection) -> list[tuple[str, str | None]]:
    """Return (name, egg_kind) rows ordered by id."""
    rows = conn.execute(sa.text("SELECT name, egg_kind FROM biomes ORDER BY id")).all()
    return [(r[0], r[1]) for r in rows]


def test_rename_migration_renames_rows_and_is_idempotent() -> None:
    """Upgrade renames legacy rows; a second run is a no-op."""
    module = _load_migration("20261022_1300_rename_k8s_primary_biome")
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _sqlite_biomes(conn)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
        assert _names(conn) == [
            ("k8s-controller", "k8s-controller"),
            ("nest-agent", None),
        ]


def test_rename_migration_never_overwrites_existing_controller_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When both names exist the legacy row is left untouched and a warning is logged."""
    module = _load_migration("20261022_1300_rename_k8s_primary_biome")
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _sqlite_biomes(conn)
        conn.execute(
            sa.text(
                "INSERT INTO biomes (name, egg_kind) VALUES ('k8s-controller', NULL)"
            )
        )
        with Operations.context(MigrationContext.configure(conn)), caplog.at_level(
            logging.WARNING
        ):
            module.upgrade()
        assert _names(conn)[0] == ("k8s-primary", "k8s-primary")
        assert any("biome rename skipped" in rec.getMessage() for rec in caplog.records)


def test_rename_migration_downgrade_restores_legacy_name() -> None:
    """Downgrade restores the legacy name on canonical rows."""
    module = _load_migration("20261022_1300_rename_k8s_primary_biome")
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _sqlite_biomes(conn)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.downgrade()
        assert _names(conn)[0] == ("k8s-primary", "k8s-primary")


def test_bundle_cache_migration_creates_and_drops_tables() -> None:
    """The bundle cache migration creates both tables on an empty schema and drops them on downgrade."""
    module = _load_migration("20261022_1400_biome_bundle_cache")
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            tables = set(sa.inspect(conn).get_table_names())
            assert {"biome_bundle_imports", "biome_bundle_files"} <= tables
            module.downgrade()
            tables_after = set(sa.inspect(conn).get_table_names())
            assert "biome_bundle_imports" not in tables_after


# ---------------------------------------------------------------------------
# LXD join blueprint wiring
# ---------------------------------------------------------------------------


def test_lxd_join_route_registered_on_app() -> None:
    """Registering lxd_bp exposes POST /api/v1/lxd/cluster-join-tokens."""
    app = Quart(__name__)
    app.register_blueprint(lxd_bp)
    rules = [
        r
        for r in app.url_map.iter_rules()
        if r.rule == "/api/v1/lxd/cluster-join-tokens"
    ]
    assert rules, "join token route missing from url_map"
    assert "POST" in rules[0].methods


def test_lxd_bootstrap_seed_hook_wired_before_serving() -> None:
    """The blueprint's bootstrap seed runs before serving, so the token is seeded at startup."""
    app = Quart(__name__)
    app.register_blueprint(lxd_bp)
    hook_names = {getattr(fn, "__name__", "") for fn in app.before_serving_funcs}
    assert lxd_cluster.seed_bootstrap_token_on_startup.__name__ in hook_names
