"""Real-Postgres tests for ``app.api.ipxe.list_machines`` / ``list_images``
pagination (gh-22).

Regression: gh-22. Both endpoints used to run an unbounded ``SELECT`` over
their full table, synchronously, inline in the request coroutine. They now
run the SELECT off the event loop via ``run_db()`` and accept ``page_size``
/``cursor`` query params. This module proves, against real Postgres:

- filter application still works (status/zone/pool; architecture/os_version)
- empty-result filters still 200 with an empty list
- pagination actually bounds the result set and returns a usable
  ``next_cursor`` for a subsequent page
- invalid ``page_size``/``cursor`` values are rejected with 400

Mirrors ``tests/api/test_clusters_pg.py``'s ``_build_app``/
``_passthrough_decorator`` pattern: build an ephemeral Quart app with a
freshly-reloaded ``ipxe_bp``, ``auth_required`` stubbed to a passthrough
(bound at import/reload time, so must be monkeypatched before reloading the
module), and ``get_db`` pointed at a real-Postgres ``penguin_dal.DB``
fixture.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from quart import Quart, g

pytestmark = pytest.mark.asyncio


def _passthrough_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn: Any) -> Any:
        return fn

    return _wrap


@pytest.fixture(autouse=True)
def _restore_ipxe_module():
    """Undo the in-place ``importlib.reload`` of ``app.api.ipxe`` after each test.

    ``_build_app`` stubs the auth decorators and reloads ``app.api.ipxe`` in
    place to pick them up. That mutation outlives the test and leaked the
    stubbed module into whatever ipxe test ran next in the same process
    (regression: test-isolation ipxe_pagination_pg poisoned test_ipxe). Snapshot
    the real decorators before the test, then restore them and reload the module
    clean afterwards. The restore is done here (not left to monkeypatch) so the
    reload always binds the real decorators regardless of finalizer ordering.
    """
    import app.middleware as mw_mod

    names = ("auth_required", "admin_required", "maintainer_or_admin_required")
    saved = {n: getattr(mw_mod, n) for n in names}
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(mw_mod, n, v)
        import app.api.ipxe as ipxe_mod

        importlib.reload(ipxe_mod)


def _build_app(*, monkeypatch: pytest.MonkeyPatch, dal_db: Any) -> Quart:
    """Build an ephemeral Quart app with a freshly-reloaded ``ipxe_bp``."""
    import app.middleware as mw_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "admin_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "maintainer_or_admin_required", _passthrough_decorator)

    import app.api.ipxe as ipxe_mod

    ipxe_mod = importlib.reload(ipxe_mod)
    monkeypatch.setattr(ipxe_mod, "get_db", lambda: dal_db)

    application = Quart(__name__)
    application.config["TESTING"] = True
    application.register_blueprint(ipxe_mod.ipxe_bp, url_prefix="/api/v1/ipxe")

    @application.before_request
    async def inject_user() -> None:
        g.current_user = {
            "id": 1,
            "_jwt_payload": {
                "tenant": "default",
                "scope": "gough.cluster.admin",
                "sub": "user-123",
            },
        }

    return application


def _seed_machine(
    db: Any,
    *,
    system_id: str,
    mac_address: str,
    status: str = "ready",
    zone: str | None = None,
    pool: str | None = None,
    last_seen_at: datetime | None = None,
) -> int:
    machine_id = db.ipxe_machines.insert(
        system_id=system_id,
        mac_address=mac_address,
        status=status,
        zone=zone,
        pool=pool,
        last_seen_at=last_seen_at,
    )
    db.commit()
    return int(machine_id)


def _seed_image(
    db: Any,
    *,
    name: str,
    architecture: str = "amd64",
    os_version: str = "24.04",
) -> int:
    image_id = db.ipxe_images.insert(
        name=name,
        display_name=name,
        os_version=os_version,
        architecture=architecture,
        kernel_path=f"/{name}/kernel",
        initrd_path=f"/{name}/initrd",
    )
    db.commit()
    return int(image_id)


class TestListMachinesFilters:
    async def test_filters_by_status(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_machine(pg_db, system_id="m-ready", mac_address="aa:bb:cc:dd:ee:01", status="ready")
        _seed_machine(pg_db, system_id="m-failed", mac_address="aa:bb:cc:dd:ee:02", status="failed")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines?status=ready")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1
        assert body["machines"][0]["system_id"] == "m-ready"

    async def test_filters_by_zone_and_pool(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_machine(
            pg_db, system_id="m-a", mac_address="aa:bb:cc:dd:ee:03",
            zone="dal2", pool="default",
        )
        _seed_machine(
            pg_db, system_id="m-b", mac_address="aa:bb:cc:dd:ee:04",
            zone="dal3", pool="default",
        )

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines?zone=dal2&pool=default")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1
        assert body["machines"][0]["system_id"] == "m-a"

    async def test_no_match_returns_empty_list(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_machine(pg_db, system_id="m-ready", mac_address="aa:bb:cc:dd:ee:05", status="ready")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines?status=broken")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body == {"machines": [], "count": 0, "next_cursor": None}

    async def test_empty_table_returns_empty_list(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body == {"machines": [], "count": 0, "next_cursor": None}


class TestListMachinesPagination:
    async def test_page_size_bounds_result_and_yields_next_cursor(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: gh-22

        5 machines, page_size=2 -- first page must return exactly 2 rows
        plus a usable ``next_cursor`` (previously this endpoint had no
        pagination at all and always returned every row).
        """
        now = datetime.now(timezone.utc)
        for i in range(5):
            _seed_machine(
                pg_db,
                system_id=f"m-{i}",
                mac_address=f"aa:bb:cc:dd:ee:{i:02d}",
                last_seen_at=now - timedelta(minutes=i),
            )

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines?page_size=2")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 2
        assert body["next_cursor"] is not None

    async def test_default_page_size_covers_small_table(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default page_size (500) is generous enough that a small table's
        callers see identical results to the pre-sweep, un-paginated
        endpoint -- no ``next_cursor`` when everything fit on one page.
        """
        for i in range(3):
            _seed_machine(pg_db, system_id=f"m-{i}", mac_address=f"aa:bb:cc:dd:ee:1{i}")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 3
        assert body["next_cursor"] is None

    async def test_invalid_page_size_returns_400(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines?page_size=not-a-number")
        assert resp.status_code == 400

    async def test_invalid_cursor_returns_400(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/machines?cursor=not-valid-base64!!!")
        assert resp.status_code == 400


class TestListImagesFilters:
    async def test_filters_by_architecture_and_os_version(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_image(pg_db, name="img-arm", architecture="arm64", os_version="24.04")
        _seed_image(pg_db, name="img-amd", architecture="amd64", os_version="24.04")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/images?architecture=arm64&os_version=24.04")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1
        assert body["images"][0]["name"] == "img-arm"

    async def test_no_match_returns_empty_list(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_image(pg_db, name="img-amd", architecture="amd64")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/images?architecture=riscv64")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body == {"images": [], "count": 0, "next_cursor": None}

    async def test_empty_table_returns_empty_list(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/images")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body == {"images": [], "count": 0, "next_cursor": None}


class TestListImagesPagination:
    async def test_page_size_bounds_result_and_yields_next_cursor(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: gh-22

        4 images, page_size=1 -- proves the same bounded-fetch + cursor
        behavior as ``list_machines`` applies to ``list_images`` too.
        """
        for i in range(4):
            _seed_image(pg_db, name=f"img-{i}")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/images?page_size=1")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1
        assert body["next_cursor"] is not None

    async def test_non_numeric_page_size_returns_400(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/images?page_size=not-a-number")
        assert resp.status_code == 400

    async def test_negative_page_size_falls_back_to_default(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-positive ``page_size`` is treated as "unset", not an error --
        same fallback behavior as ``app.api.nodes.list_nodes``."""
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/images?page_size=-5")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 0

    async def test_invalid_cursor_returns_400(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.get("/api/v1/ipxe/images?cursor=not-valid-base64!!!")
        assert resp.status_code == 400
