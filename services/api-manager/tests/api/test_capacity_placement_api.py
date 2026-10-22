"""Real-path API tests for the capacity guard: ingest, capacity view, placement.

No guard stubs here: every test drives the real route against a real SQLite DB
with seeded snapshots. Covers heartbeat ingest end-to-end (enroll -> link ->
heartbeat -> GET capacity), ingest refusals, LXD create placement, node deploy
placement and biome eligibility (fit / no-fit / stale).
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from quart import Quart, g

from app import capacity_guard as cg
from app.clouds.base import Machine, MachineState

GIB = 1024**3
SECRET = "test-secret-key"


def _passthrough(*dargs: Any, **dkwargs: Any) -> Any:
    """Decorator stand-in that leaves the wrapped function untouched."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


def _snapshot_body(collected: datetime, **overrides: Any) -> dict[str, Any]:
    """A healthy 16-core / 64 GiB host snapshot body (gough.host_resources.v1)."""
    body: dict[str, Any] = {
        "schema": "gough.host_resources.v1",
        "collected_at": collected.isoformat(),
        "cpu": {"logical_cores": 16, "usage_pct": 10.0, "load1": 1.0, "load5": 1.0, "load15": 1.0},
        "memory": {"total_bytes": 64 * GIB, "available_bytes": 40 * GIB},
        "pressure": None,
        "storage_pools": [
            {"name": "default", "driver": "dir", "total_bytes": 1000 * GIB, "used_bytes": 100 * GIB}
        ],
        "host_root_fs": {"total_bytes": 100 * GIB, "free_bytes": 60 * GIB},
        "committed": {
            "cpu_cores": 0.0, "memory_bytes": 0, "disk_bytes": {"default": 0},
            "instances": 0, "instances_without_limits": 0,
        },
        "errors": [],
    }
    body.update(overrides)
    return body


@pytest.fixture()
def guard_app(dal, monkeypatch):
    """Quart app with agents, nodes and clouds blueprints on a real DAL + guard tables."""
    from penguin_dal import Field

    dal.define_table(
        "access_agents",
        Field("agent_id", "string", notnull=True),
        Field("hostname", "string"),
        Field("node_id", "integer"),
        Field("ip_address", "string"),
        Field("enrollment_key_hash", "string"),
        Field("enrollment_completed", "boolean", default=False),
        Field("status", "string", default="active"),
        Field("capabilities", "string"),
        Field("enrolled_at", "datetime"),
        Field("last_heartbeat", "datetime"),
        Field("updated_at", "datetime"),
        Field("created_at", "datetime"),
        migrate=True,
    )
    dal.define_table(
        "enrollment_keys",
        Field("key_hash", "string", notnull=True),
        Field("created_by", "string"),
        Field("expires_at", "datetime"),
        Field("is_used", "boolean", default=False),
        Field("revoked", "boolean", default=False),
        Field("used_by_agent", "integer"),
        Field("metadata", "string"),
        Field("created_at", "datetime"),
        migrate=True,
    )
    dal.define_table(
        "ssh_ca_config", Field("is_active", "boolean", default=True),
        Field("public_key", "string"), Field("private_key_vault_path", "string"),
        Field("created_at", "datetime"), migrate=True,
    )
    dal.define_table(
        "cloud_providers",
        Field("name", "string"), Field("provider_type", "string"), Field("config_data", "json"),
        Field("is_active", "boolean", default=True), Field("status", "string"),
        migrate=True,
    )
    dal.define_table(
        "cloud_machines",
        Field("provider_id", "integer"), Field("external_id", "string"), Field("hostname", "string"),
        Field("status", "string"), Field("zone", "string"), Field("os_image", "string"),
        Field("machine_type", "string"), Field("public_ips", "json"), Field("private_ips", "json"),
        Field("ip_address", "string"), Field("private_ip", "string"), Field("tags", "json"),
        Field("metadata", "json"), migrate=True,
    )
    dal.define_table(
        "biomes",
        Field("name", "string"), Field("version", "string"),
        Field("biome_kind", "string", default="custom"), Field("phase", "string", default="post_deploy"),
        Field("lock_to_host", "boolean", default=False),
        Field("requires_hardware_tags", "json"), Field("forbids_hardware_tags", "json"),
        Field("min_ram_mb", "integer"), Field("min_disk_gb", "integer"),
        Field("tenant_id", "string", default="__default__"), migrate=True,
    )
    dal.define_table(
        "node_egg_assignments",
        Field("node_id", "integer", notnull=True), Field("egg_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"), Field("phase", "string"),
        Field("status", "string", default="pending"), Field("depends_on_egg_instance_id", "integer"),
        Field("readiness_probe_state", "string", default="not_started"),
        Field("assigned_at", "datetime"), Field("deployed_at", "datetime"), Field("removed_at", "datetime"),
        Field("created_at", "datetime"), Field("updated_at", "datetime"), migrate=True,
    )
    dal.commit()

    import app.middleware as mw_mod
    import app.models as models_mod
    import app.security.scope_enforcement as scope_mod

    for name in ("auth_required", "roles_required", "roles_accepted"):
        monkeypatch.setattr(mw_mod, name, _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)
    monkeypatch.setattr(models_mod, "get_db", lambda: dal)
    import app.audit as audit_mod

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: None)

    import app.api.agents as agents_mod
    import app.api.clouds as clouds_mod
    import app.api.nodes as nodes_mod

    agents_mod = importlib.reload(agents_mod)
    nodes_mod = importlib.reload(nodes_mod)
    clouds_mod = importlib.reload(clouds_mod)
    for mod in (agents_mod, nodes_mod, clouds_mod):
        monkeypatch.setattr(mod, "get_db", lambda: dal)

    async def _flag_on(*_a: Any, **_k: Any) -> bool:
        return True

    async def _unlimited(*_a: Any, **_k: Any) -> float:
        return float("inf")

    monkeypatch.setattr(clouds_mod, "feature_enabled", _flag_on)
    monkeypatch.setattr(clouds_mod, "node_allowance", _unlimited)
    monkeypatch.setattr(nodes_mod, "node_allowance", _unlimited)
    monkeypatch.setattr(nodes_mod, "feature_enabled", lambda *_a, **_k: _flag_off())
    monkeypatch.setattr(agents_mod, "get_current_user", lambda: {"id": 1, "username": "admin"})

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = SECRET
    app.url_map.strict_slashes = False
    app.register_blueprint(agents_mod.agents_bp)
    app.register_blueprint(nodes_mod.nodes_bp)
    app.register_blueprint(clouds_mod.clouds_bp, url_prefix="/api/v1/clouds")

    @app.before_request
    async def _identity() -> None:
        g.current_user = {
            "id": 1, "username": "admin", "role": "admin",
            "_jwt_payload": {"sub": "admin", "tenant": "default", "scope": "gough.nodes.read"},
        }
        g.tenant_context = SimpleNamespace(tenant_id="default", cross_tenant=False)
        g.nats_client = None

    return SimpleNamespace(app=app, dal=dal, agents=agents_mod, nodes=nodes_mod, clouds=clouds_mod)


async def _flag_off() -> bool:
    """Feature-flag stand-in: every flag OFF (plan-compiler wiring stays dormant)."""
    return False


def _add_node(dal: Any, name: str, tenant: str = "default") -> int:
    """Insert a deployable node and return its id."""
    node_id = dal.nodes.insert(tenant_id=tenant, name=name, state="planned")
    dal.commit()
    return int(node_id)


def _seed(dal: Any, node_id: int, *, age_s: float = 0.0, **overrides: Any) -> None:
    """Persist a snapshot for ``node_id`` received ``age_s`` seconds ago."""
    when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    snap = cg.parse_snapshot(_snapshot_body(when, **overrides))
    with dal.engine.begin() as conn:
        assert cg.record_snapshot(conn, node_id, snap, when) is cg.SnapshotOutcome.STORED


def _agent_token(agent_id: str) -> str:
    """Mint an agent access JWT the way enrollment does."""
    return jwt.encode(
        {"sub": f"agent:{agent_id}", "type": "agent_access", "exp": datetime.utcnow() + timedelta(hours=1)},
        SECRET,
        algorithm="HS256",
    )


def _add_agent(dal: Any, agent_id: str, *, node_id: int | None, status: str = "active",
               hostname: str = "self-reported") -> None:
    """Insert an enrolled agent with an optional server-side node link."""
    dal.access_agents.insert(agent_id=agent_id, hostname=hostname, node_id=node_id, status=status,
                             enrolled_at=datetime.utcnow())
    dal.commit()


async def _heartbeat(client: Any, agent_id: str, snapshot: dict[str, Any]) -> Any:
    """POST a heartbeat carrying ``snapshot`` as the agent."""
    return await client.post(
        "/api/v1/agents/heartbeat",
        data=json.dumps({"agent_id": agent_id, "status": "active", "resource_snapshot": snapshot}),
        headers={"Authorization": f"Bearer {_agent_token(agent_id)}", "Content-Type": "application/json"},
    )


# --- enrollment link + heartbeat ingest end-to-end ---------------------------------


@pytest.mark.asyncio
async def test_enroll_heartbeat_capacity_end_to_end(guard_app):
    """Enroll via a key bound to host-a -> heartbeat snapshot persists -> capacity view shows it."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    key = "ENROLL-AAAA-BBBB-CCCC-DDDD"
    dal.enrollment_keys.insert(
        key_hash=hashlib.sha256(key.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        metadata=json.dumps({"purpose": "node-join", "intended_hostname": "host-a", "tenant_id": "default"}),
    )
    dal.commit()
    async with guard_app.app.test_client() as client:
        enrolled = await client.post(
            "/api/v1/agents/enroll", headers={"X-Enrollment-Key": key},
            json={"hostname": "host-a", "capabilities": ["ssh"]},
        )
        assert enrolled.status_code == 201
        body = await enrolled.get_json()
        row = dal(dal.access_agents.agent_id == body["agent_id"]).select().first()
        assert row.node_id == node_id

        beat = await client.post(
            "/api/v1/agents/heartbeat",
            data=json.dumps({"status": "active", "resource_snapshot": _snapshot_body(datetime.now(timezone.utc))}),
            headers={"Authorization": f"Bearer {body['access_token']}", "Content-Type": "application/json"},
        )
        assert beat.status_code == 200

        cap = await client.get(f"/api/v1/nodes/{node_id}/capacity")
        assert cap.status_code == 200
        view = (await cap.get_json())["data"]["capacity"]
    assert view["has_snapshot"] is True and view["fresh"] is True
    assert view["capacity"]["logical_cores"] == 16
    assert view["allocatable"]["cpu_cores"] == pytest.approx(56.0)


@pytest.mark.asyncio
async def test_enroll_refused_when_hostname_differs_from_key_intent(guard_app):
    """A key bound to host-a cannot enroll an agent that claims host-b."""
    dal = guard_app.dal
    _add_node(dal, "host-a")
    key = "ENROLL-1111-2222-3333-4444"
    dal.enrollment_keys.insert(
        key_hash=hashlib.sha256(key.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        metadata=json.dumps({"intended_hostname": "host-a"}),
    )
    dal.commit()
    async with guard_app.app.test_client() as client:
        resp = await client.post("/api/v1/agents/enroll", headers={"X-Enrollment-Key": key},
                                 json={"hostname": "host-b"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_spoofed_hostname_cannot_write_another_nodes_snapshot(guard_app):
    """An agent linked to host-a that reports hostname 'host-b' only ever touches host-a."""
    dal = guard_app.dal
    node_a = _add_node(dal, "host-a")
    node_b = _add_node(dal, "host-b")
    _add_agent(dal, "ag-1", node_id=node_a, hostname="host-b")
    async with guard_app.app.test_client() as client:
        resp = await _heartbeat(client, "ag-1", _snapshot_body(datetime.now(timezone.utc)))
    assert resp.status_code == 200
    table = cg.NodeResourceSnapshot.__table__
    with dal.engine.connect() as conn:
        nodes_with_snapshot = {r[0] for r in conn.execute(table.select().with_only_columns(table.c.node_id))}
    assert nodes_with_snapshot == {node_a} and node_b not in nodes_with_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        ("unlinked", "agent_not_linked_to_node"),
        ("inactive", "agent_not_active"),
        ("shared", "node_has_multiple_active_agents"),
    ],
)
async def test_ingest_refusals_return_400_and_store_nothing(guard_app, setup, needle):
    """Unlinked, inactive and node-sharing agents get 400 and no snapshot row."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    if setup == "unlinked":
        _add_agent(dal, "ag-1", node_id=None)
    elif setup == "inactive":
        _add_agent(dal, "ag-1", node_id=node_id, status="revoked")
    else:
        _add_agent(dal, "ag-1", node_id=node_id)
        _add_agent(dal, "ag-2", node_id=node_id)
    async with guard_app.app.test_client() as client:
        resp = await _heartbeat(client, "ag-1", _snapshot_body(datetime.now(timezone.utc)))
    assert resp.status_code == 400
    assert needle in (await resp.get_json())["error"]
    with dal.engine.connect() as conn:
        assert conn.execute(cg.NodeResourceSnapshot.__table__.select()).first() is None


@pytest.mark.asyncio
async def test_inactive_agent_cannot_reactivate_itself_via_heartbeat(guard_app):
    """A revoked agent claiming status 'active' in its heartbeat stays revoked."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _add_agent(dal, "ag-1", node_id=node_id, status="revoked")
    async with guard_app.app.test_client() as client:
        await _heartbeat(client, "ag-1", _snapshot_body(datetime.now(timezone.utc)))
    assert dal(dal.access_agents.agent_id == "ag-1").select().first().status == "revoked"


@pytest.mark.asyncio
async def test_future_dated_snapshot_rejected_via_route(guard_app):
    """collected_at an hour ahead is 400 and leaves the stored snapshot untouched."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _add_agent(dal, "ag-1", node_id=node_id)
    async with guard_app.app.test_client() as client:
        ok = await _heartbeat(client, "ag-1", _snapshot_body(datetime.now(timezone.utc)))
        assert ok.status_code == 200
        bad = await _heartbeat(
            client, "ag-1", _snapshot_body(datetime.now(timezone.utc) + timedelta(hours=1))
        )
    assert bad.status_code == 400
    assert "future_dated" in (await bad.get_json())["error"]


@pytest.mark.asyncio
async def test_admin_link_endpoint(guard_app):
    """PUT /agents/{id}/node binds an agent; unknown node 404; second agent on same node 409."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _add_agent(dal, "ag-1", node_id=None)
    _add_agent(dal, "ag-2", node_id=None)
    async with guard_app.app.test_client() as client:
        assert (await client.put("/api/v1/agents/ag-1/node", json={"node_id": node_id})).status_code == 200
        assert (await client.put("/api/v1/agents/ag-1/node", json={"node_id": 999})).status_code == 404
        assert (await client.put("/api/v1/agents/ag-2/node", json={"node_id": node_id})).status_code == 409
        assert (await client.put("/api/v1/agents/ag-2/node", json={"node_id": "x"})).status_code == 400
    assert dal(dal.access_agents.agent_id == "ag-1").select().first().node_id == node_id


# --- LXD create placement ----------------------------------------------------------


class _FakeLXD:
    """Stand-in LXD provider: records the spec the route hands to create_machine."""

    created: list[Any] = []
    members: tuple[list[str], bool] = (["host-a", "host-b"], True)

    def authenticate(self) -> bool:
        """Pretend to authenticate."""
        return True

    def list_cluster_members(self) -> tuple[list[str], bool]:
        """Return the configured cluster members."""
        return type(self).members

    def create_machine(self, spec: Any) -> Machine:
        """Record the spec and return a machine."""
        type(self).created.append(spec)
        return Machine(id=spec.name, name=spec.name, state=MachineState.RUNNING, provider="lxd",
                       provider_id="1", size=spec.size, image=spec.image)


@pytest.fixture()
def lxd_provider(guard_app, monkeypatch):
    """Register an LXD provider row and route get_cloud_provider to the fake."""
    _FakeLXD.created = []
    _FakeLXD.members = (["host-a", "host-b"], True)
    monkeypatch.setattr(guard_app.clouds, "get_cloud_provider", lambda *_a, **_k: _FakeLXD())
    pid = guard_app.dal.cloud_providers.insert(name="lxd1", provider_type="lxd", config_data={}, is_active=True)
    guard_app.dal.commit()
    return int(pid)


_CREATE = {"name": "web-1", "image": "ubuntu:22.04", "size": "2/4GiB", "storage_gb": 20}


@pytest.mark.asyncio
async def test_lxd_create_fits_and_targets_chosen_member(guard_app, lxd_provider):
    """With fresh snapshots the route picks a member, sets target, reserves and confirms."""
    dal = guard_app.dal
    a, b = _add_node(dal, "host-a"), _add_node(dal, "host-b")
    _seed(dal, a)
    _seed(dal, b, memory={"total_bytes": 64 * GIB, "available_bytes": 60 * GIB})
    async with guard_app.app.test_client() as client:
        resp = await client.post(f"/api/v1/clouds/{lxd_provider}/machines",
                                 json={**_CREATE, "extra": {"target": "host-a"}})
    assert resp.status_code == 201
    (spec,) = _FakeLXD.created
    assert spec.extra["target"] == "host-b"  # client-supplied target discarded; roomiest member
    assert spec.extra["capacity_guarded"] is True
    table = cg.CapacityReservation.__table__
    with dal.engine.connect() as conn:
        rows = conn.execute(table.select()).mappings().all()
    assert len(rows) == 1 and rows[0]["node_id"] == b and rows[0]["status"] == 4  # CONFIRMED


@pytest.mark.asyncio
async def test_lxd_create_no_fit_returns_409_with_reasons(guard_app, lxd_provider):
    """A request bigger than any member's allocatable CPU is refused with per-member reasons."""
    dal = guard_app.dal
    _seed(dal, _add_node(dal, "host-a"))
    _seed(dal, _add_node(dal, "host-b"))
    async with guard_app.app.test_client() as client:
        resp = await client.post(f"/api/v1/clouds/{lxd_provider}/machines", json={**_CREATE, "size": "64/4GiB"})
    assert resp.status_code == 409
    body = await resp.get_json()
    assert body["error"] == "insufficient_capacity"
    assert "insufficient_cpu" in body["reasons"]["host-a"]
    assert _FakeLXD.created == []


@pytest.mark.asyncio
async def test_lxd_create_stale_snapshot_returns_409(guard_app, lxd_provider):
    """A snapshot older than the freshness window refuses placement (fail closed)."""
    dal = guard_app.dal
    _seed(dal, _add_node(dal, "host-a"), age_s=600)
    _seed(dal, _add_node(dal, "host-b"), age_s=600)
    async with guard_app.app.test_client() as client:
        resp = await client.post(f"/api/v1/clouds/{lxd_provider}/machines", json=_CREATE)
    assert resp.status_code == 409
    assert "stale_or_missing_snapshot" in (await resp.get_json())["reasons"]["host-a"]
    assert _FakeLXD.created == []


@pytest.mark.asyncio
async def test_lxd_create_unmapped_members_refused(guard_app, lxd_provider):
    """Cluster members the hub has no node row for can never be placed on."""
    _FakeLXD.members = (["ghost-1"], True)
    async with guard_app.app.test_client() as client:
        resp = await client.post(f"/api/v1/clouds/{lxd_provider}/machines", json=_CREATE)
    assert resp.status_code == 409
    assert (await resp.get_json())["reasons"] == {"": ["no_candidate_nodes"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["2", "2/50%", "x/4GiB", "0/4GiB"])
async def test_lxd_create_unparseable_limits_400(guard_app, lxd_provider, size):
    """Limits the guard cannot account for are a 400, not a guess."""
    async with guard_app.app.test_client() as client:
        resp = await client.post(f"/api/v1/clouds/{lxd_provider}/machines", json={**_CREATE, "size": size})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_lxd_create_failure_releases_reservation(guard_app, lxd_provider, monkeypatch):
    """If LXD creation raises, the reservation is released so headroom returns."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _FakeLXD.members = (["host-a"], True)
    _seed(dal, node_id)

    def _boom(self: Any, spec: Any) -> Any:
        raise RuntimeError("lxd down")

    monkeypatch.setattr(_FakeLXD, "create_machine", _boom)
    async with guard_app.app.test_client() as client:
        with pytest.raises(RuntimeError, match="lxd down"):
            await client.post(f"/api/v1/clouds/{lxd_provider}/machines", json=_CREATE)
    table = cg.CapacityReservation.__table__
    with dal.engine.connect() as conn:
        statuses = [r[0] for r in conn.execute(table.select().with_only_columns(table.c.status))]
    assert statuses == [2]  # RELEASED


@pytest.mark.asyncio
async def test_standalone_lxd_omits_target(guard_app, lxd_provider):
    """A non-clustered LXD server is still guarded but gets no ``target``."""
    dal = guard_app.dal
    _FakeLXD.members = (["host-a"], False)
    _seed(dal, _add_node(dal, "host-a"))
    async with guard_app.app.test_client() as client:
        resp = await client.post(f"/api/v1/clouds/{lxd_provider}/machines", json=_CREATE)
    assert resp.status_code == 201
    assert "target" not in _FakeLXD.created[0].extra


# --- node deploy placement ---------------------------------------------------------


def _add_biome(dal: Any, *, ram_mb: int, disk_gb: int) -> int:
    """Insert a biome with declared minimums and return its id."""
    biome_id = dal.biomes.insert(name="b1", version="1", min_ram_mb=ram_mb, min_disk_gb=disk_gb)
    dal.commit()
    return int(biome_id)


async def _deploy(client: Any, node_id: int, biome_id: int) -> Any:
    """POST a deploy for one biome."""
    return await client.post(
        f"/api/v1/nodes/{node_id}/deploy",
        json={"biome_assignments": [{"biome_id": biome_id}]},
        headers={"X-Idempotency-Key": "k1"},
    )


@pytest.mark.asyncio
async def test_deploy_fits_returns_202_and_holds_reservation(guard_app):
    """A fresh snapshot with room: 202, assignment created, reservation confirmed."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _seed(dal, node_id)
    biome_id = _add_biome(dal, ram_mb=4096, disk_gb=20)
    async with guard_app.app.test_client() as client:
        resp = await _deploy(client, node_id, biome_id)
    assert resp.status_code == 202
    table = cg.CapacityReservation.__table__
    with dal.engine.connect() as conn:
        row = conn.execute(table.select()).mappings().one()
    assert row["memory_bytes"] == 4096 * 1024**2 and row["status"] == 4


@pytest.mark.asyncio
async def test_deploy_no_fit_409_with_reasons_and_no_assignment(guard_app):
    """A biome larger than allocatable memory is refused 409 before any assignment exists."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _seed(dal, node_id)
    biome_id = _add_biome(dal, ram_mb=60 * 1024, disk_gb=10)
    async with guard_app.app.test_client() as client:
        resp = await _deploy(client, node_id, biome_id)
    assert resp.status_code == 409
    assert "insufficient_memory" in (await resp.get_json())["error"]["details"]["reasons"]
    assert dal(dal.node_egg_assignments.id > 0).select().first() is None


@pytest.mark.asyncio
async def test_deploy_stale_or_missing_snapshot_409(guard_app):
    """Stale and never-reported nodes both refuse deploys."""
    dal = guard_app.dal
    stale, missing = _add_node(dal, "host-a"), _add_node(dal, "host-b")
    _seed(dal, stale, age_s=600)
    biome_id = _add_biome(dal, ram_mb=1024, disk_gb=1)
    async with guard_app.app.test_client() as client:
        for node_id in (stale, missing):
            resp = await _deploy(client, node_id, biome_id)
            assert resp.status_code == 409
            assert (await resp.get_json())["error"]["details"]["reasons"] == ["stale_or_missing_snapshot"]


@pytest.mark.asyncio
async def test_deploy_assignment_failure_releases_reservation(guard_app):
    """A valid biome plus an unknown id: the 404 path releases the hold taken for the valid one."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _seed(dal, node_id)
    biome_id = _add_biome(dal, ram_mb=1024, disk_gb=1)
    async with guard_app.app.test_client() as client:
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/deploy",
            json={"biome_assignments": [{"biome_id": biome_id}, {"biome_id": 4242}]},
            headers={"X-Idempotency-Key": "k2"},
        )
    assert resp.status_code == 404
    table = cg.CapacityReservation.__table__
    with dal.engine.connect() as conn:
        assert [r[0] for r in conn.execute(table.select().with_only_columns(table.c.status))] == [2]


# --- biome eligibility (real guard) ------------------------------------------------


def test_biome_eligibility_real_guard(dal):
    """Eligibility is true with a fresh fitting snapshot, false with reasons otherwise."""
    from penguin_dal import Field

    from app.api.biomes import _eval_node_eligibility

    dal.define_table(
        "biomes", Field("name", "string"), Field("min_ram_mb", "integer"),
        Field("min_disk_gb", "integer"), Field("requires_hardware_tags", "json"),
        Field("forbids_hardware_tags", "json"), migrate=True,
    )
    fit = _add_node(dal, "fit")
    big = _add_node(dal, "big")
    stale = _add_node(dal, "stale")
    none = _add_node(dal, "none")
    _seed(dal, fit)
    _seed(dal, big)
    _seed(dal, stale, age_s=600)
    biome = SimpleNamespace(id=1, name="b", min_ram_mb=2048, min_disk_gb=10, required_architecture=None,
                            requires_hardware_tags=[], forbids_hardware_tags=[])
    huge = SimpleNamespace(id=2, name="h", min_ram_mb=60 * 1024, min_disk_gb=10, required_architecture=None,
                           requires_hardware_tags=[], forbids_hardware_tags=[])

    def _eval(b: Any, node_id: int) -> Any:
        return _eval_node_eligibility(dal, b, dal(dal.nodes.id == node_id).select().first())

    assert _eval(biome, fit).eligible is True
    refused = _eval(huge, big)
    assert refused.eligible is False
    assert any(v.get("reason") == "insufficient_memory" for v in refused.resource_violations)
    for node_id in (stale, none):
        res = _eval(biome, node_id)
        assert res.eligible is False
        assert any(v.get("reason") == "stale_or_missing_snapshot" for v in res.resource_violations)


# --- tenant scoping of the agent<->node link --------------------------------------


def _enroll_key(dal: Any, key: str, meta: dict[str, Any]) -> None:
    """Insert an unused enrollment key with ``meta`` as its server-side metadata."""
    dal.enrollment_keys.insert(
        key_hash=hashlib.sha256(key.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        metadata=json.dumps(meta),
    )
    dal.commit()


async def _enroll(guard_app: Any, key: str, hostname: str) -> Any:
    """Enroll an agent with ``key`` and return its stored access_agents row."""
    async with guard_app.app.test_client() as client:
        resp = await client.post("/api/v1/agents/enroll", headers={"X-Enrollment-Key": key},
                                 json={"hostname": hostname})
        assert resp.status_code == 201
        agent_id = (await resp.get_json())["agent_id"]
    return guard_app.dal(guard_app.dal.access_agents.agent_id == agent_id).select().first()


@pytest.mark.asyncio
async def test_key_without_tenant_enrolls_unlinked(guard_app):
    """A key lacking tenant_id never triggers an unscoped lookup: the agent enrolls unlinked."""
    dal = guard_app.dal
    _add_node(dal, "host-a")
    _enroll_key(dal, "ENROLL-NOTENANT-0001", {"intended_hostname": "host-a"})
    row = await _enroll(guard_app, "ENROLL-NOTENANT-0001", "host-a")
    assert row.node_id is None


@pytest.mark.asyncio
async def test_cross_tenant_name_collision_links_only_own_tenant(guard_app):
    """Same node name in two tenants: each key links its own tenant's node; a third tenant gets none."""
    dal = guard_app.dal
    mine = _add_node(dal, "host-a", tenant="tenant-a")
    theirs = _add_node(dal, "host-a", tenant="tenant-b")
    _enroll_key(dal, "ENROLL-TENANT-A-0001", {"intended_hostname": "host-a", "tenant_id": "tenant-a"})
    _enroll_key(dal, "ENROLL-TENANT-B-0001", {"intended_hostname": "host-a", "tenant_id": "tenant-b"})
    _enroll_key(dal, "ENROLL-TENANT-C-0001", {"intended_hostname": "host-a", "tenant_id": "tenant-c"})
    assert (await _enroll(guard_app, "ENROLL-TENANT-A-0001", "host-a")).node_id == mine
    assert (await _enroll(guard_app, "ENROLL-TENANT-B-0001", "host-a")).node_id == theirs
    assert (await _enroll(guard_app, "ENROLL-TENANT-C-0001", "host-a")).node_id is None


@pytest.mark.asyncio
async def test_key_creation_stamps_tenant_from_token_not_body(guard_app):
    """POST /enrollment-keys overrides any body tenant_id with the admin token's tenant."""
    async with guard_app.app.test_client() as client:
        resp = await client.post(
            "/api/v1/agents/enrollment-keys",
            json={"metadata": {"intended_hostname": "host-a", "tenant_id": "evil"}},
        )
    assert resp.status_code == 201
    row = guard_app.dal(guard_app.dal.enrollment_keys.id > 0).select().first()
    meta = json.loads(row.metadata)
    assert meta["tenant_id"] == "default" and meta["intended_hostname"] == "host-a"


@pytest.mark.asyncio
async def test_admin_link_rejects_other_tenants_node(guard_app):
    """A tenant admin cannot link an agent to a node of another tenant (reported as 404)."""
    dal = guard_app.dal
    foreign = _add_node(dal, "host-x", tenant="tenant-b")
    _add_agent(dal, "ag-1", node_id=None)
    async with guard_app.app.test_client() as client:
        resp = await client.put("/api/v1/agents/ag-1/node", json={"node_id": foreign})
    assert resp.status_code == 404
    assert dal(dal.access_agents.agent_id == "ag-1").select().first().node_id is None


# --- deploy reservation lifecycle --------------------------------------------------


def _reservation_statuses(dal: Any) -> list[int]:
    """Return every reservation's status code."""
    table = cg.CapacityReservation.__table__
    with dal.engine.connect() as conn:
        return [r[0] for r in conn.execute(table.select().with_only_columns(table.c.status))]


@pytest.mark.asyncio
async def test_duplicate_deploy_holds_no_reservation(guard_app):
    """Re-deploying a biome already assigned inserts nothing and reserves nothing new."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _seed(dal, node_id)
    biome_id = _add_biome(dal, ram_mb=4096, disk_gb=20)
    async with guard_app.app.test_client() as client:
        assert (await _deploy(client, node_id, biome_id)).status_code == 202
        assert (await _deploy(client, node_id, biome_id)).status_code == 202
    assert _reservation_statuses(dal) == [4]  # only the first deploy's CONFIRMED hold


@pytest.mark.asyncio
async def test_duplicate_deploy_not_refused_for_capacity(guard_app):
    """A no-op re-deploy is not blocked by capacity: nothing new would be placed."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _seed(dal, node_id)
    biome_id = _add_biome(dal, ram_mb=4096, disk_gb=20)
    async with guard_app.app.test_client() as client:
        assert (await _deploy(client, node_id, biome_id)).status_code == 202
    with dal.engine.begin() as conn:  # snapshot goes stale afterwards
        conn.execute(cg.NodeResourceSnapshot.__table__.update().values(
            received_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    async with guard_app.app.test_client() as client:
        assert (await _deploy(client, node_id, biome_id)).status_code == 202


@pytest.mark.asyncio
async def test_exception_mid_deploy_releases_reservation(guard_app, monkeypatch):
    """An exception while creating assignments releases the hold instead of leaking it."""
    dal = guard_app.dal
    node_id = _add_node(dal, "host-a")
    _seed(dal, node_id)
    biome_id = _add_biome(dal, ram_mb=4096, disk_gb=20)

    async def _boom(fn: Any) -> Any:
        if getattr(fn, "__name__", "") == "_create_assignments":
            raise RuntimeError("db exploded")
        return await real_run_db(fn)

    real_run_db = guard_app.nodes.run_db
    monkeypatch.setattr(guard_app.nodes, "run_db", _boom)
    async with guard_app.app.test_client() as client:
        with pytest.raises(RuntimeError, match="db exploded"):
            await _deploy(client, node_id, biome_id)
    assert _reservation_statuses(dal) == [2]  # RELEASED
