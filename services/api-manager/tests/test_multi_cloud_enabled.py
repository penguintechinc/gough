"""Multi-cloud surface with ``gough.multi-cloud`` switched ON.

``tests/test_licensing.py::TestCloudBlueprintGate`` proves only that the gate
*opens*: with the flag on, a request gets past ``_gate_multi_cloud`` and is then
rejected by auth. An opened gate is not a working surface. These tests drive the
opened surface end to end with PostHog and the cloud provider both mocked, so a
pass means an operator who enables the flag gets a functioning cloud surface
rather than a 500.

Two things separate this module from ``tests/api/test_clouds_*.py``:

1. The flag is explicitly forced on, so what is exercised is the post-gate
   behaviour rather than the gate.
2. The database is the schema ``models_sqlalchemy.create_all_tables()``
   actually builds -- reached through penguin-dal reflection exactly as
   ``app.models.init_db`` wires it in production. The older cloud suites call
   ``define_table()`` with a hand-written shape that matches what
   ``app/api/clouds.py`` assumes, which is not the shape that ships, so they
   cannot see a drift between the two.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from quart import Quart, g

from app.clouds import CLOUD_REGISTRY
from app.clouds.base import BaseCloud, Machine, MachineSpec, MachineState

FAKE_PROVIDER_TYPE = "fakecloud"

#: Every provider the product advertises. ``azure`` is the interesting one --
#: it was unloadable until gh-35 because ``AZURE_AVAILABLE`` was always False.
EXPECTED_PROVIDERS = {"aws", "azure", "gcp", "lxd", "maas", "vultr"}


def _passthrough(*dargs: Any, **dkwargs: Any):
    """Stand in for auth_required / roles_required / roles_accepted.

    Returns the decorated function untouched so a test exercises the route
    body rather than the JWT chain, in both bare and called-with-args forms.
    """
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


class FakeCloud(BaseCloud):
    """In-memory cloud provider standing in for a real one.

    Records the ``MachineSpec`` it was handed so a test can assert on what the
    route sent it -- notably gough's managed tag -- without any network, SDK or
    credential in play.
    """

    provider_type = FAKE_PROVIDER_TYPE
    supports_cloud_init = True

    #: Specs seen by :meth:`create_machine`, newest last. Class-level so a test
    #: can read it back without holding the instance the route constructed.
    created_specs: ClassVar[list[MachineSpec]] = []

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.machines: dict[str, Machine] = {}

    def authenticate(self) -> bool:
        self._authenticated = True
        return True

    def _machine(self, machine_id: str, spec: MachineSpec | None = None) -> Machine:
        return Machine(
            id=machine_id,
            name=spec.name if spec else "existing-01",
            state=MachineState.RUNNING,
            provider=FAKE_PROVIDER_TYPE,
            provider_id="1",
            region=spec.region if spec else "us-test-1",
            image=spec.image if spec else "ubuntu-22.04",
            size=spec.size if spec else "small",
            public_ips=["203.0.113.10"],
            private_ips=["10.0.0.10"],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            tags=dict(spec.tags) if spec else {},
        )

    def create_machine(self, spec: MachineSpec) -> Machine:
        type(self).created_specs.append(spec)
        machine = self._machine("m-fake-001", spec)
        self.machines[machine.id] = machine
        return machine

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        return [self._machine("m-fake-001")]

    def get_machine(self, machine_id: str) -> Machine:
        return self._machine(machine_id)

    def destroy_machine(self, machine_id: str) -> bool:
        return True

    def start_machine(self, machine_id: str) -> bool:
        return True

    def stop_machine(self, machine_id: str) -> bool:
        return True

    def reboot_machine(self, machine_id: str) -> bool:
        return True

    def list_images(self, filters: dict | None = None) -> list[dict]:
        return [{"id": "ubuntu-22.04", "name": "Ubuntu 22.04 LTS"}]

    def list_sizes(self, filters: dict | None = None) -> list[dict]:
        return [{"id": "small", "vcpus": 2, "memory_mb": 4096}]

    def list_regions(self) -> list[dict]:
        return [{"id": "us-test-1", "name": "Test Region"}]


async def _flag_on(*_a: Any, **_k: Any) -> bool:
    return True


async def _flag_off(*_a: Any, **_k: Any) -> bool:
    return False


@pytest.fixture()
def shipped_schema_db():
    """penguin-dal bound to the schema the product actually creates.

    Mirrors ``app.models.init_db``: SQLAlchemy builds the physical schema, then
    penguin-dal connects over it by reflection with no table definitions of its
    own. Whatever columns exist here are the columns a deployment has.
    """
    from penguin_dal import DB

    from app.models_sqlalchemy import convert_pydal_to_sqlalchemy_uri, create_all_tables

    tmpdir = tempfile.mkdtemp(prefix="gough-multicloud-")
    pydal_uri = f"sqlite://{os.path.join(tmpdir, 'gough.db')}"
    # create_all_tables() seeds roles and a default admin, all on stdout.
    with contextlib.redirect_stdout(io.StringIO()):
        create_all_tables(pydal_uri)
    return DB(convert_pydal_to_sqlalchemy_uri(pydal_uri), pool_size=1)


@pytest.fixture()
def fake_provider():
    """Register :class:`FakeCloud` in the registry for the duration of a test."""
    FakeCloud.created_specs = []
    CLOUD_REGISTRY[FAKE_PROVIDER_TYPE] = FakeCloud
    yield FakeCloud
    CLOUD_REGISTRY.pop(FAKE_PROVIDER_TYPE, None)


@contextlib.contextmanager
def _clouds_module_with_auth_stubbed():
    """Yield ``app.api.clouds`` reloaded with its auth decorators stubbed out.

    The decorators bind at import time, so stubbing ``app.middleware`` only
    takes effect on a module reloaded afterwards -- and by the same token,
    putting the real decorators back is not enough either: the reloaded module
    still holds the stubs. Both the module attributes and the reload have to be
    undone, in that order, or every later test module inherits a clouds
    blueprint with no authentication on it.

    ``monkeypatch`` cannot express that ordering (its undo runs after a
    fixture's teardown, not before), which is why this is done by hand.
    """
    import app.api.clouds as clouds_mod
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    targets = (
        (mw_mod, "auth_required"),
        (mw_mod, "roles_required"),
        (mw_mod, "roles_accepted"),
        (scope_mod, "require_scopes"),
    )
    saved = [(mod, name, getattr(mod, name)) for mod, name in targets]
    for mod, name in targets:
        setattr(mod, name, _passthrough)
    try:
        yield importlib.reload(clouds_mod)
    finally:
        for mod, name, original in saved:
            setattr(mod, name, original)
        importlib.reload(clouds_mod)


def _build_client(db, *, flag=_flag_on):
    """Mount ``clouds_bp`` with auth stubbed and the multi-cloud flag forced."""
    with _clouds_module_with_auth_stubbed() as clouds_mod:
        clouds_mod.get_db = lambda: db
        clouds_mod.feature_enabled = flag

        app = Quart(__name__)
        app.config["TESTING"] = True
        # Return the 500 a deployment would return rather than re-raising into
        # the test, so a broken route is observed the way an operator sees it.
        app.config["PROPAGATE_EXCEPTIONS"] = False
        app.url_map.strict_slashes = False
        app.register_blueprint(clouds_mod.clouds_bp, url_prefix="/api/v1/clouds")

        @app.before_request
        async def _inject_identity():
            g.current_user = {"id": 1, "username": "admin", "role": "admin"}
            g.tenant_context = SimpleNamespace(tenant_id="default")

        yield app.test_client()


@pytest.fixture()
def cloud_client(shipped_schema_db, fake_provider):
    """Client with the multi-cloud flag ON."""
    yield from _build_client(shipped_schema_db)


@pytest.fixture()
def cloud_client_flag_off(shipped_schema_db, fake_provider):
    """Same client with the flag OFF, for contrast."""
    yield from _build_client(shipped_schema_db, flag=_flag_off)


def _seed_provider(db) -> int:
    """Insert an enabled provider row directly, bypassing the create route.

    Lets the machine-facing tests run independently of whether ``POST /`` can
    persist a provider.
    """
    table = db.cloud_providers
    columns = {str(c.name) for c in table._table.columns}
    row = {"name": "fake-1", "provider_type": FAKE_PROVIDER_TYPE}
    # The route reads provider.config and provider.enabled; supply whichever
    # of those the shipped schema actually has.
    for candidate, value in (
        ("config", {}),
        ("config_data", "{}"),
        ("enabled", True),
        ("is_active", True),
        ("status", "connected"),
    ):
        if candidate in columns:
            row[candidate] = value
    provider_id = table.insert(**row)
    db.commit()
    return provider_id


# ---------------------------------------------------------------------------
# Build: the provider surface loads at all
# ---------------------------------------------------------------------------


class TestProvidersLoad:
    """All six advertised providers import and register (gh-35)."""

    def test_every_advertised_provider_is_registered(self):
        assert EXPECTED_PROVIDERS <= set(CLOUD_REGISTRY)

    def test_azure_sdk_imports_resolve(self):
        """The gh-35 regression: AzureCloud used to never be importable."""
        from app.clouds import azure

        assert azure.AZURE_AVAILABLE is True


# ---------------------------------------------------------------------------
# The flag is what gates the surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFlagControlsSurface:
    """The same request 404s with the flag off and is admitted with it on."""

    async def test_flag_off_hides_provider_list(self, cloud_client_flag_off):
        resp = await cloud_client_flag_off.get("/api/v1/clouds/")
        body = await resp.get_json()
        assert resp.status_code == 404
        assert body["error"]["code"] == "feature_disabled"

    async def test_flag_on_admits_the_request(self, cloud_client):
        """Flag on: the gate stops short-circuiting and the route body runs.

        Deliberately asserts admission rather than success -- the route beyond
        the gate is broken (see :data:`XFAIL_PROVIDER_LIST_QUERY`), and this
        test's job is to show the flag, not the route, decided the outcome.
        """
        resp = await cloud_client.get("/api/v1/clouds/")
        assert b"feature_disabled" not in await resp.get_data()

    async def test_flag_on_serves_provider_list(self, cloud_client):
        resp = await cloud_client.get("/api/v1/clouds/")
        assert resp.status_code == 200

    async def test_flag_on_advertises_every_provider_type(self, cloud_client):
        resp = await cloud_client.get("/api/v1/clouds/")
        body = await resp.get_json()
        names = {p["name"] for p in body["available_types"]}
        assert EXPECTED_PROVIDERS <= names


# ---------------------------------------------------------------------------
# The opened surface, driven against the shipped schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOpenedSurfaceReadPaths:
    """Read-only routes. Each instantiates a provider, so each needs config."""

    async def test_list_images(self, cloud_client, shipped_schema_db):
        pid = _seed_provider(shipped_schema_db)
        resp = await cloud_client.get(f"/api/v1/clouds/{pid}/images")
        assert resp.status_code == 200
        assert (await resp.get_json())["images"][0]["id"] == "ubuntu-22.04"

    async def test_list_sizes(self, cloud_client, shipped_schema_db):
        pid = _seed_provider(shipped_schema_db)
        resp = await cloud_client.get(f"/api/v1/clouds/{pid}/sizes")
        assert resp.status_code == 200
        assert (await resp.get_json())["sizes"][0]["id"] == "small"

    async def test_list_regions(self, cloud_client, shipped_schema_db):
        pid = _seed_provider(shipped_schema_db)
        resp = await cloud_client.get(f"/api/v1/clouds/{pid}/regions")
        assert resp.status_code == 200
        assert (await resp.get_json())["regions"][0]["id"] == "us-test-1"

    async def test_get_machine(self, cloud_client, shipped_schema_db):
        pid = _seed_provider(shipped_schema_db)
        resp = await cloud_client.get(f"/api/v1/clouds/{pid}/machines/m-fake-001")
        assert resp.status_code == 200
        assert (await resp.get_json())["id"] == "m-fake-001"

    async def test_machine_lifecycle_actions(self, cloud_client, shipped_schema_db):
        pid = _seed_provider(shipped_schema_db)
        for action in ("start", "stop", "reboot"):
            resp = await cloud_client.post(
                f"/api/v1/clouds/{pid}/machines/m-fake-001/{action}"
            )
            assert resp.status_code == 200, f"{action} returned {resp.status_code}"


@pytest.mark.asyncio
class TestOpenedSurfaceWritePaths:
    """Routes that persist. Provisioning is the point of multi-cloud, so these
    decide whether enabling the flag gives an operator a usable surface."""

    async def test_add_provider_persists(self, cloud_client):
        resp = await cloud_client.post(
            "/api/v1/clouds/",
            json={
                "name": "fake-1",
                "provider_type": FAKE_PROVIDER_TYPE,
                "config": {},
                "enabled": True,
            },
        )
        assert resp.status_code == 201

    async def test_create_machine_provisions(self, cloud_client, shipped_schema_db):
        pid = _seed_provider(shipped_schema_db)
        resp = await cloud_client.post(
            f"/api/v1/clouds/{pid}/machines",
            json={"name": "web-01", "image": "ubuntu-22.04", "size": "small"},
        )
        assert resp.status_code == 201

    async def test_create_machine_stamps_managed_tag(
        self, cloud_client, shipped_schema_db, fake_provider
    ):
        """gough's managed marker must reach the provider on every machine it
        provisions -- it is what keeps the machine attributable to gough across
        the inventory syncs that later overwrite its local row."""
        pid = _seed_provider(shipped_schema_db)
        await cloud_client.post(
            f"/api/v1/clouds/{pid}/machines",
            json={"name": "web-01", "image": "ubuntu-22.04", "size": "small"},
        )
        assert fake_provider.created_specs, "provider was never asked to create a machine"
        assert fake_provider.created_specs[-1].tags.get("gough-managed") == "true"


@pytest.mark.asyncio
class TestLicenseMeteringStillApplies:
    """The flag governs availability; the licence governs how much.

    The meter sits after the ``provider.enabled`` read, so while that read
    raises, this refusal is unreachable in a real deployment -- the caller gets
    a 500 rather than an actionable 402.
    """

    async def test_allowance_exhausted_returns_402(
        self, cloud_client, shipped_schema_db, monkeypatch
    ):
        import app.api.clouds as clouds_mod

        async def _no_allowance(*_a, **_k):
            return 0

        monkeypatch.setattr(clouds_mod, "node_allowance", _no_allowance)
        pid = _seed_provider(shipped_schema_db)
        resp = await cloud_client.post(
            f"/api/v1/clouds/{pid}/machines",
            json={"name": "web-01", "image": "ubuntu-22.04", "size": "small"},
        )
        body = await resp.get_json()
        assert resp.status_code == 402
        assert body["error"]["code"] == "license_required"


# ---------------------------------------------------------------------------
# Schema drift, asserted directly
# ---------------------------------------------------------------------------


class TestShippedSchemaMatchesCloudApi:
    """Pin the merged column contract, independently of any route.

    The route tests above each fail on the first missing column they happen to
    touch, which makes a partial schema regression look like a single broken
    endpoint. These name the whole contract in one place: every column the API
    writes, and every column kept for a feature that has no reader yet.
    """

    #: Columns app/api/clouds.py writes on cloud_machines.
    MACHINE_API_COLUMNS = frozenset({
        "provider_id", "external_id", "hostname", "status", "zone", "os_image",
        "machine_type", "public_ips", "private_ips", "ip_address", "private_ip",
        "tags", "metadata",
    })

    #: Columns with no reader today, kept because they are the schema side of
    #: planned capacity and LXD/FleetDM work. Dropping them to match the API
    #: would delete those features quietly, so they are asserted, not assumed.
    MACHINE_RESERVED_COLUMNS = frozenset({
        "architecture", "cpu_count", "memory_mb", "storage_gb",
        "lxd_cluster_id", "fleet_host_id",
    })

    PROVIDER_API_COLUMNS = frozenset({
        "name", "provider_type", "config_data", "status", "is_active",
    })

    PROVIDER_RESERVED_COLUMNS = frozenset({
        "description", "region", "credentials_path", "last_sync_at",
    })

    @staticmethod
    def _columns(db, table_name: str) -> set[str]:
        return {str(c.name) for c in getattr(db, table_name)._table.columns}

    def test_cloud_machines_has_every_column_the_api_writes(self, shipped_schema_db):
        columns = self._columns(shipped_schema_db, "cloud_machines")
        missing = self.MACHINE_API_COLUMNS - columns
        assert not missing, f"cloud_machines missing: {sorted(missing)}"

    def test_cloud_machines_keeps_its_reserved_feature_columns(self, shipped_schema_db):
        columns = self._columns(shipped_schema_db, "cloud_machines")
        missing = self.MACHINE_RESERVED_COLUMNS - columns
        assert not missing, f"cloud_machines lost reserved columns: {sorted(missing)}"

    def test_cloud_providers_has_every_column_the_api_writes(self, shipped_schema_db):
        columns = self._columns(shipped_schema_db, "cloud_providers")
        missing = self.PROVIDER_API_COLUMNS - columns
        assert not missing, f"cloud_providers missing: {sorted(missing)}"

    def test_cloud_providers_keeps_its_reserved_feature_columns(self, shipped_schema_db):
        columns = self._columns(shipped_schema_db, "cloud_providers")
        missing = self.PROVIDER_RESERVED_COLUMNS - columns
        assert not missing, f"cloud_providers lost reserved columns: {sorted(missing)}"


@pytest.mark.asyncio
class TestProvisionedMachineRoundTrips:
    """What the provider reported must survive the trip into the database.

    The merge exists so the cloud abstraction's richer fields have somewhere to
    land. Asserting the status code only proves the INSERT was accepted; these
    assert the values came back intact, which is what "no features lost"
    actually means.
    """

    async def test_full_machine_record_persists(self, cloud_client, shipped_schema_db):
        pid = _seed_provider(shipped_schema_db)
        resp = await cloud_client.post(
            f"/api/v1/clouds/{pid}/machines",
            json={
                "name": "web-01",
                "image": "ubuntu-22.04",
                "size": "small",
                "region": "us-test-1",
                "tags": {"role": "web"},
            },
        )
        assert resp.status_code == 201

        row = shipped_schema_db(
            shipped_schema_db.cloud_machines.external_id == "m-fake-001"
        ).select().first()
        assert row is not None, "machine was not persisted"

        assert row.hostname == "web-01"
        assert row.status == "running"
        assert row.zone == "us-test-1"
        assert row.os_image == "ubuntu-22.04"
        assert row.machine_type == "small"

    async def test_address_lists_survive_as_lists(self, cloud_client, shipped_schema_db):
        """The reason public_ips/private_ips exist: a scalar column cannot hold
        what the provider reports."""
        pid = _seed_provider(shipped_schema_db)
        await cloud_client.post(
            f"/api/v1/clouds/{pid}/machines",
            json={"name": "web-01", "image": "ubuntu-22.04", "size": "small"},
        )
        row = shipped_schema_db(
            shipped_schema_db.cloud_machines.external_id == "m-fake-001"
        ).select().first()

        assert row.public_ips == ["203.0.113.10"]
        assert row.private_ips == ["10.0.0.10"]
        # ...and the denormalised primaries agree with the lists.
        assert row.ip_address == "203.0.113.10"
        assert row.private_ip == "10.0.0.10"

    async def test_tags_persist_as_a_dict_licensing_can_read(
        self, cloud_client, shipped_schema_db
    ):
        """count_active_nodes() reads the gough-managed marker off this column,
        so it has to come back as a dict, not a stringified one."""
        from app.licensing import _machine_is_gough_managed

        pid = _seed_provider(shipped_schema_db)
        await cloud_client.post(
            f"/api/v1/clouds/{pid}/machines",
            json={
                "name": "web-01", "image": "ubuntu-22.04", "size": "small",
                "tags": {"role": "web"},
            },
        )
        row = shipped_schema_db(
            shipped_schema_db.cloud_machines.external_id == "m-fake-001"
        ).select().first()

        assert isinstance(row.tags, dict)
        assert row.tags["role"] == "web", "operator tag was dropped"
        assert row.tags["gough-managed"] == "true"
        assert _machine_is_gough_managed(row) is True

    async def test_provisioned_machine_counts_against_the_allowance(
        self, cloud_client, shipped_schema_db
    ):
        """The whole point of the managed tag: a machine gough created is
        metered, where synced-in inventory is not."""
        from app.licensing import count_active_nodes

        pid = _seed_provider(shipped_schema_db)
        assert count_active_nodes(shipped_schema_db) == 0

        await cloud_client.post(
            f"/api/v1/clouds/{pid}/machines",
            json={"name": "web-01", "image": "ubuntu-22.04", "size": "small"},
        )
        assert count_active_nodes(shipped_schema_db) == 1


@pytest.mark.asyncio
class TestProviderCredentialsNeverLeave:
    """Provider credentials must not appear in any provider response.

    Regression: the redaction on these two routes deleted a key named "config".
    The reflected row has no such key -- the column is ``config_data`` -- so the
    delete was a no-op and both routes returned the provider's credentials to
    any caller holding the viewer role. Asserting the exact field set rather
    than the absence of one name, per security.md "Output Validation".
    """

    SECRETS = ("config_data", "config", "credentials_path")

    @staticmethod
    def _seed_with_credentials(db) -> int:
        provider_id = db.cloud_providers.insert(
            name="aws-prod",
            provider_type="aws",
            config_data={
                "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            },
            credentials_path="/var/gough/secrets/aws.json",
            status="connected",
            is_active=True,
        )
        db.commit()
        return provider_id

    async def test_get_provider_omits_credentials(
        self, cloud_client, shipped_schema_db
    ):
        pid = self._seed_with_credentials(shipped_schema_db)
        resp = await cloud_client.get(f"/api/v1/clouds/{pid}")
        assert resp.status_code == 200
        body = await resp.get_json()

        leaked = [k for k in self.SECRETS if k in body]
        assert not leaked, f"credential fields returned: {leaked}"
        assert "AKIAIOSFODNN7EXAMPLE" not in await resp.get_data(as_text=True)
        # The route still has to be useful.
        assert body["name"] == "aws-prod"
        assert body["provider_type"] == "aws"

    async def test_list_providers_omits_credentials(
        self, cloud_client, shipped_schema_db
    ):
        self._seed_with_credentials(shipped_schema_db)
        resp = await cloud_client.get("/api/v1/clouds/")
        assert resp.status_code == 200
        body = await resp.get_json()

        assert body["providers"], "seeded provider was not listed"
        for provider in body["providers"]:
            leaked = [k for k in self.SECRETS if k in provider]
            assert not leaked, f"credential fields returned: {leaked}"
        assert "wJalrXUtnFEMI" not in await resp.get_data(as_text=True)

    async def test_provider_response_is_an_exact_allow_list(
        self, cloud_client, shipped_schema_db
    ):
        """A column added to cloud_providers later must not reach the API just
        by existing -- the projection has to be updated deliberately."""
        from app.api.clouds import _PROVIDER_PUBLIC_FIELDS

        pid = self._seed_with_credentials(shipped_schema_db)
        resp = await cloud_client.get(f"/api/v1/clouds/{pid}")
        body = await resp.get_json()
        assert set(body) <= set(_PROVIDER_PUBLIC_FIELDS)
