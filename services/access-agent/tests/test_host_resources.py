"""Tests for the gough.host_resources.v1 snapshot collector."""

import asyncio
import json
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent.config import AgentConfig
from agent.heartbeat import HeartbeatService
from agent.host_resources import (
    SCHEMA,
    collect_host_resources,
    compute_committed,
    parse_byte_size,
    parse_cpu_allowance,
    parse_cpu_count,
    parse_psi,
    pool_from_metadata,
    read_pressure,
)

PSI_CPU = "some avg10=1.50 avg60=2.25 avg300=0.5 total=1000\n"
PSI_MEMORY = (
    "some avg10=0.10 avg60=0.20 avg300=0.3 total=10\n"
    "full avg10=0.05 avg60=0.07 avg300=0.1 total=5\n"
)
PSI_IO = (
    "some avg10=3.00 avg60=4.00 avg300=1.0 total=20\n"
    "full avg10=1.00 avg60=1.50 avg300=0.5 total=8\n"
)

TOP_KEYS = {
    "schema",
    "collected_at",
    "cpu",
    "memory",
    "pressure",
    "storage_pools",
    "host_root_fs",
    "committed",
    "errors",
}
CPU_KEYS = {"logical_cores", "usage_pct", "load1", "load5", "load15"}
MEMORY_KEYS = {"total_bytes", "available_bytes", "swap_total_bytes", "swap_used_bytes"}
PRESSURE_KEYS = {"cpu", "memory", "io"}
COMMITTED_KEYS = {
    "cpu_cores",
    "memory_bytes",
    "disk_bytes",
    "instances",
    "instances_without_limits",
}
POOL_KEYS = {"name", "driver", "total_bytes", "used_bytes"}
ROOTFS_KEYS = {"total_bytes", "free_bytes"}


def _write_psi(
    proc_root: Path, cpu: str = PSI_CPU, memory: str = PSI_MEMORY, io: str = PSI_IO
) -> None:
    """Create /proc/pressure/{cpu,memory,io} fixtures under proc_root."""
    base = proc_root / "pressure"
    base.mkdir(parents=True)
    (base / "cpu").write_text(cpu)
    (base / "memory").write_text(memory)
    (base / "io").write_text(io)


def _instance(
    name: str,
    config: dict[str, str] | None = None,
    devices: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build an instance object shaped like LXD expanded_config/expanded_devices."""
    return {
        "name": name,
        "expanded_config": config or {},
        "expanded_devices": devices or {},
    }


ROOT_DISK = {"type": "disk", "path": "/", "pool": "default", "size": "10GiB"}


async def _fake_lxd(
    sock_path: str, routes: dict[str, Any], hang_paths: frozenset[str] = frozenset()
) -> asyncio.Server:
    """Serve LXD-style JSON envelopes over a unix socket; hang_paths never respond."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Answer one HTTP GET from the routes table."""
        request_line = (await reader.readline()).decode()
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        path = request_line.split()[1]
        try:
            if path in hang_paths:
                await asyncio.sleep(30)
                return
            body = json.dumps(
                {"type": "sync", "status": "Success", "metadata": routes[path]}
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_unix_server(handle, path=sock_path)


LXD_ROUTES: dict[str, Any] = {
    "/1.0/storage-pools?recursion=1": [{"name": "default", "driver": "zfs"}],
    "/1.0/storage-pools/default/resources": {
        "space": {"total": 500_000_000_000, "used": 120_000_000_000},
        "inodes": {"total": 1, "used": 1},
    },
    "/1.0/instances?recursion=1": [
        _instance(
            "web-1",
            config={"limits.cpu": "2", "limits.memory": "2GiB"},
            devices={"root": ROOT_DISK},
        ),
        # limits inherited from a profile appear only in expanded_config
        _instance(
            "db-1",
            config={"limits.cpu": "0-3", "limits.memory": "50%"},
            devices={
                "root": {"type": "disk", "path": "/", "pool": "default", "size": "20GB"}
            },
        ),
        _instance("bare-1", config={}, devices={"root": ROOT_DISK}),
    ],
}


@pytest.fixture
def proc_root(tmp_path: Path) -> Path:
    """Fake /proc with PSI files present."""
    root = tmp_path / "proc"
    _write_psi(root)
    return root


@pytest.fixture
def lxd_socket() -> Iterator[str]:
    """Unix socket path in a short private dir (AF_UNIX paths cap near 108 bytes).

    tmp_path can exceed that limit under a long TMPDIR, so the socket lives in
    a short mkdtemp directory that is removed at teardown.
    """
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="ga")
    try:
        yield str(Path(sock_dir) / "lxd.sock")
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_parse_psi_normal() -> None:
    """Parses some/full windows from a PSI file."""
    parsed = parse_psi(PSI_MEMORY)
    assert parsed["some"]["avg10"] == 0.10
    assert parsed["some"]["avg60"] == 0.20
    assert parsed["full"]["avg60"] == 0.07


def test_parse_psi_rejects_missing_some_line() -> None:
    """A PSI file without a some line is a parse error."""
    with pytest.raises(ValueError):
        parse_psi("full avg10=0.0 avg60=0.0\n")


def test_read_pressure_normal(proc_root: Path) -> None:
    """Maps the three PSI files onto the pressure section."""
    pressure = read_pressure(proc_root)
    assert pressure.cpu.some_avg10 == 1.5
    assert pressure.memory.full_avg60 == 0.07
    assert pressure.io.full_avg60 == 1.5


def test_read_pressure_missing_file_raises(tmp_path: Path) -> None:
    """A kernel without PSI raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        read_pressure(tmp_path / "no-proc")


@pytest.mark.parametrize(
    ("value", "host_total", "expected"),
    [
        ("2GiB", None, 2 * 1024**3),
        ("512MiB", None, 512 * 1024**2),
        ("1GB", None, 10**9),
        ("500MB", None, 500 * 10**6),
        ("4kB", None, 4000),
        ("8KiB", None, 8192),
        ("1024", None, 1024),
        ("25%", 8 * 1024**3, 2 * 1024**3),
        ("1.5GiB", None, int(1.5 * 1024**3)),
    ],
)
def test_parse_byte_size_table(
    value: str, host_total: int | None, expected: int
) -> None:
    """Units, bare bytes and percentages of host RAM parse to bytes."""
    assert parse_byte_size(value, host_total) == expected


@pytest.mark.parametrize("value", ["", "2XB", "GiB", "abc", "50%"])
def test_parse_byte_size_rejects_bad_input(value: str) -> None:
    """Unknown units, missing numbers and percent without host total raise."""
    with pytest.raises(ValueError):
        parse_byte_size(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("4", 4), ("0-3", 4), ("1,3", 2), ("0-3,2", 4), ("0-1, 4-5", 4), (" 2 ", 2)],
)
def test_parse_cpu_count_table(value: str, expected: int) -> None:
    """Integers, ranges and lists count CPUs; duplicates count once."""
    assert parse_cpu_count(value) == expected


@pytest.mark.parametrize("value", ["", "0", "3-1", "x", "1,,a", "-1"])
def test_parse_cpu_count_rejects_bad_input(value: str) -> None:
    """Empty, zero, inverted or malformed CPU specs raise ValueError."""
    with pytest.raises(ValueError):
        parse_cpu_count(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("50%", 0.5), ("25ms/100ms", 0.25), ("100%", 1.0)],
)
def test_parse_cpu_allowance_table(value: str, expected: float) -> None:
    """Percent and time-slice allowance forms become a CPU fraction."""
    assert parse_cpu_allowance(value) == expected


def test_parse_cpu_allowance_rejects_zero_period() -> None:
    """A zero period in an ms/ms allowance is rejected."""
    with pytest.raises(ValueError):
        parse_cpu_allowance("10ms/0ms")


def test_pool_from_metadata() -> None:
    """Pool metadata and resources map onto StoragePool fields."""
    pool = pool_from_metadata(
        {"name": "default", "driver": "zfs"},
        {"space": {"total": 100, "used": 40}},
    )
    assert (pool.name, pool.driver, pool.total_bytes, pool.used_bytes) == (
        "default",
        "zfs",
        100,
        40,
    )


def test_compute_committed_aggregates_expanded_limits() -> None:
    """Sums expanded limits, root disk per pool, and counts unlimited instances."""
    instances = [
        _instance(
            "web-1",
            config={"limits.cpu": "2", "limits.memory": "2GiB"},
            devices={"root": ROOT_DISK},
        ),
        # profile-inherited limits show up in expanded_config
        _instance(
            "db-1",
            config={"limits.cpu": "0-3", "limits.memory": "50%"},
            devices={
                "root": {"type": "disk", "path": "/", "pool": "fast", "size": "20GB"}
            },
        ),
        _instance("bare-1", config={}, devices={"root": ROOT_DISK}),
    ]
    committed, errors = compute_committed(
        instances, ["default", "fast"], 8 * 1024**3, 8
    )
    assert errors == []
    assert committed.cpu_cores == 6.0
    assert committed.memory_bytes == 2 * 1024**3 + 4 * 1024**3
    assert committed.disk_bytes == {"default": 2 * 10 * 1024**3, "fast": 20 * 10**9}
    assert committed.instances == 3
    assert committed.instances_without_limits == 1


def test_compute_committed_allowance_and_non_root_disks() -> None:
    """Allowance scales cores (host cores when no limits.cpu); non-root disks are ignored."""
    instances = [
        _instance(
            "half",
            config={
                "limits.cpu": "4",
                "limits.cpu.allowance": "50%",
                "limits.memory": "1GiB",
            },
        ),
        _instance(
            "host-share",
            config={"limits.cpu.allowance": "25ms/100ms", "limits.memory": "1GiB"},
        ),
        _instance(
            "data-mount",
            config={"limits.cpu": "1", "limits.memory": "1GiB"},
            devices={
                "data": {
                    "type": "disk",
                    "path": "/data",
                    "pool": "default",
                    "size": "5GiB",
                }
            },
        ),
    ]
    committed, errors = compute_committed(instances, ["default"], 8 * 1024**3, 8)
    assert errors == []
    assert committed.cpu_cores == pytest.approx(2.0 + 2.0 + 1.0)
    assert committed.disk_bytes == {"default": 0}
    # host-share has no limits.cpu (allowance alone), so it counts as without limits
    assert committed.instances_without_limits == 1


def test_compute_committed_counts_unparseable_limits() -> None:
    """Unparseable limits produce an error entry and count as without limits."""
    instances = [
        _instance("broken", config={"limits.cpu": "x", "limits.memory": "2GiB"})
    ]
    committed, errors = compute_committed(instances, [], 8 * 1024**3, 8)
    assert errors == ["limit_unparsed:broken:limits.cpu"]
    assert committed.instances_without_limits == 1
    assert committed.cpu_cores == 0.0


async def test_collect_happy_path_matches_schema(
    proc_root: Path, lxd_socket: str, tmp_path: Path
) -> None:
    """Full collection with fake LXD yields the exact v1 field set and values."""
    server = await _fake_lxd(lxd_socket, LXD_ROUTES)
    try:
        snapshot = await collect_host_resources(
            timeout_s=5.0, lxd_socket=lxd_socket, proc_root=proc_root
        )
    finally:
        server.close()
        await server.wait_closed()

    doc = snapshot.to_dict()
    assert doc["schema"] == SCHEMA
    assert set(doc) == TOP_KEYS
    assert set(doc["cpu"]) == CPU_KEYS
    assert set(doc["memory"]) == MEMORY_KEYS
    assert set(doc["pressure"]) == PRESSURE_KEYS
    assert set(doc["committed"]) == COMMITTED_KEYS
    assert set(doc["host_root_fs"]) == ROOTFS_KEYS
    assert all(set(pool) == POOL_KEYS for pool in doc["storage_pools"])
    assert doc["errors"] == []
    assert doc["collected_at"].endswith("+00:00")
    assert doc["storage_pools"] == [
        {
            "name": "default",
            "driver": "zfs",
            "total_bytes": 500_000_000_000,
            "used_bytes": 120_000_000_000,
        }
    ]
    assert doc["committed"]["instances"] == 3
    assert doc["committed"]["instances_without_limits"] == 1
    assert doc["committed"]["cpu_cores"] == 6.0


async def test_collect_missing_psi_and_lxd_socket_reports_errors(
    tmp_path: Path, lxd_socket: str
) -> None:
    """Missing PSI sets pressure to null; missing LXD socket is named in errors."""
    snapshot = await collect_host_resources(
        timeout_s=5.0, lxd_socket=lxd_socket, proc_root=tmp_path / "no-proc"
    )
    doc = snapshot.to_dict()
    assert doc["pressure"] is None
    assert "psi_unavailable" in doc["errors"]
    assert "lxd_unavailable" in doc["errors"]
    assert doc["storage_pools"] == []
    assert doc["committed"]["instances"] == 0
    assert doc["cpu"]["logical_cores"] > 0


async def test_collect_timeout_returns_partial_snapshot(
    proc_root: Path, lxd_socket: str
) -> None:
    """A hung LXD call is cut at the budget; host and PSI sections still report."""
    server = await _fake_lxd(
        lxd_socket,
        LXD_ROUTES,
        hang_paths=frozenset(
            {"/1.0/storage-pools?recursion=1", "/1.0/instances?recursion=1"}
        ),
    )
    started = time.monotonic()
    try:
        snapshot = await collect_host_resources(
            timeout_s=0.5, lxd_socket=lxd_socket, proc_root=proc_root
        )
        # measured before teardown: the fake server's hung handler would block wait_closed
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert elapsed < 3.0
    assert "lxd_timeout" in snapshot.errors
    assert snapshot.pressure is not None
    assert snapshot.cpu.logical_cores > 0
    assert snapshot.storage_pools == []
    assert snapshot.committed.instances == 0


async def test_heartbeat_body_carries_resource_snapshot(
    proc_root: Path, lxd_socket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat POST body includes resource_snapshot with the v1 schema tag."""
    import agent.host_resources as host_resources

    monkeypatch.setattr(
        host_resources,
        "read_pressure",
        lambda _root: host_resources.read_pressure(proc_root),
    )
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the heartbeat JSON body and acknowledge it."""
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    class _Auth:
        """Stub auth returning no headers."""

        def get_auth_headers(self) -> dict[str, str]:
            """Return an empty header set."""
            return {}

    config = AgentConfig(lxd_socket=lxd_socket, snapshot_timeout_s=0.5)
    service = HeartbeatService(config, _Auth())  # type: ignore[arg-type]
    service._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gough.test"
    )
    try:
        await service._send_heartbeat()
    finally:
        await service._http_client.aclose()

    snapshot = captured["body"]["resource_snapshot"]
    assert snapshot["schema"] == SCHEMA
    assert set(snapshot) == TOP_KEYS
    assert "lxd_unavailable" in snapshot["errors"]
    assert "resource_usage" in captured["body"]
