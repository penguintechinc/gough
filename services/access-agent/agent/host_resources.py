"""Host resource snapshot collector for the Gough Access Agent.

Builds the ``gough.host_resources.v1`` document attached to every heartbeat so
the hub can refuse placements that would overload the host. Collection is
time-boxed; a failing or slow section is reported in ``errors`` instead of
failing the heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, TypeVar
from urllib.parse import quote

import httpx
import psutil

log = logging.getLogger(__name__)

SCHEMA = "gough.host_resources.v1"
DEFAULT_LXD_SOCKET = "/var/snap/lxd/common/lxd/unix.socket"

_T = TypeVar("_T")

_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "B": 1,
    "kB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
    "TiB": 2**40,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")
_PERCENT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
_ALLOWANCE_MS_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*ms\s*/\s*(\d+(?:\.\d+)?)\s*ms\s*$"
)


@dataclass(slots=True)
class HostSample:
    """Raw psutil/loadavg readings taken in a worker thread."""

    logical_cores: int
    usage_pct: float
    load1: float
    load5: float
    load15: float
    mem_total: int
    mem_available: int
    swap_total: int
    swap_used: int
    disk_total: int
    disk_free: int


@dataclass(slots=True)
class CpuStats:
    """CPU section of the snapshot."""

    logical_cores: int
    usage_pct: float
    load1: float
    load5: float
    load15: float


@dataclass(slots=True)
class MemoryStats:
    """Memory section of the snapshot."""

    total_bytes: int
    available_bytes: int
    swap_total_bytes: int
    swap_used_bytes: int


@dataclass(slots=True)
class CpuPressure:
    """PSI cpu window averages (percent stalled)."""

    some_avg10: float
    some_avg60: float


@dataclass(slots=True)
class MemoryPressure:
    """PSI memory window averages (percent stalled)."""

    some_avg10: float
    some_avg60: float
    full_avg60: float


@dataclass(slots=True)
class IoPressure:
    """PSI io window averages (percent stalled)."""

    some_avg60: float
    full_avg60: float


@dataclass(slots=True)
class PressureSection:
    """Pressure Stall Information for cpu, memory and io."""

    cpu: CpuPressure
    memory: MemoryPressure
    io: IoPressure


@dataclass(slots=True)
class StoragePool:
    """One LXD storage pool and its space usage."""

    name: str
    driver: str
    total_bytes: int
    used_bytes: int


@dataclass(slots=True)
class RootFs:
    """Free and total space on the host root filesystem."""

    total_bytes: int
    free_bytes: int


@dataclass(slots=True)
class Committed:
    """Resources promised to instances, summed from LXD limits."""

    cpu_cores: float
    memory_bytes: int
    disk_bytes: dict[str, int]
    instances: int
    instances_without_limits: int


@dataclass(slots=True)
class HostResourceSnapshot:
    """Full gough.host_resources.v1 document; serialise with to_dict()."""

    collected_at: str
    cpu: CpuStats
    memory: MemoryStats
    pressure: PressureSection | None
    storage_pools: list[StoragePool]
    host_root_fs: RootFs
    committed: Committed
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Return the wire document with the schema identifier included."""
        return {"schema": SCHEMA, **asdict(self)}


def parse_byte_size(value: str, host_total: int | None = None) -> int:
    """Parse an LXD size such as ``2GiB``, ``512MB``, ``50%`` or ``1024`` into bytes.

    Percentages require ``host_total`` (host RAM); other forms ignore it.
    Raises ValueError on unknown units or malformed input.
    """
    pct = _PERCENT_RE.match(value)
    if pct:
        if host_total is None:
            raise ValueError(f"percentage needs host total: {value!r}")
        return int(host_total * float(pct.group(1)) / 100)
    match = _SIZE_RE.match(value)
    if match is None or match.group(2) not in _SIZE_UNITS:
        raise ValueError(f"unparseable size: {value!r}")
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2)])


def parse_cpu_count(value: str) -> int:
    """Count CPUs in an LXD ``limits.cpu`` value.

    A bare integer is a vCPU count; ``0-3`` ranges and ``1,3`` lists name CPU
    ids and are counted as a set. Raises ValueError on malformed input.
    """
    text = value.strip()
    if text.isdigit():
        count = int(text)
        if count < 1:
            raise ValueError(f"cpu count must be >= 1: {value!r}")
        return count
    cpus: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if "-" in token:
            low_s, _, high_s = token.partition("-")
            low, high = int(low_s), int(high_s)
            if high < low:
                raise ValueError(f"inverted cpu range: {value!r}")
            cpus.update(range(low, high + 1))
        else:
            cpus.add(int(token))
    if not cpus:
        raise ValueError(f"empty cpu set: {value!r}")
    return len(cpus)


def parse_cpu_allowance(value: str) -> float:
    """Return the CPU fraction of ``limits.cpu.allowance`` (``50%`` or ``25ms/100ms``)."""
    pct = _PERCENT_RE.match(value)
    if pct:
        return float(pct.group(1)) / 100
    ms = _ALLOWANCE_MS_RE.match(value)
    if ms:
        period = float(ms.group(2))
        if period <= 0:
            raise ValueError(f"allowance period must be > 0: {value!r}")
        return float(ms.group(1)) / period
    raise ValueError(f"unparseable cpu allowance: {value!r}")


def parse_psi(text: str) -> dict[str, dict[str, float]]:
    """Parse a /proc/pressure file into ``{"some"|"full": {"avg10": f, ...}}``.

    Raises ValueError when the file has no ``some`` line or an unknown line kind.
    """
    parsed: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        if kind not in ("some", "full"):
            raise ValueError(f"unexpected PSI line: {line!r}")
        window_values: dict[str, float] = {}
        for pair in parts[1:]:
            key, sep, raw = pair.partition("=")
            if sep and key in ("avg10", "avg60", "avg300"):
                window_values[key] = float(raw)
        parsed[kind] = window_values
    if "some" not in parsed:
        raise ValueError("PSI file has no 'some' line")
    return parsed


def _psi_value(parsed: dict[str, dict[str, float]], kind: str, window: str) -> float:
    """Fetch one PSI average, raising ValueError if the kernel omitted it."""
    try:
        return parsed[kind][window]
    except KeyError as exc:
        raise ValueError(f"missing PSI {kind} {window}") from exc


def read_pressure(proc_root: Path) -> PressureSection:
    """Read cpu, memory and io PSI from ``<proc_root>/pressure``.

    Raises FileNotFoundError when the kernel lacks PSI and ValueError on parse failure.
    """
    base = proc_root / "pressure"
    cpu = parse_psi((base / "cpu").read_text(encoding="utf-8"))
    mem = parse_psi((base / "memory").read_text(encoding="utf-8"))
    io = parse_psi((base / "io").read_text(encoding="utf-8"))
    return PressureSection(
        cpu=CpuPressure(
            some_avg10=_psi_value(cpu, "some", "avg10"),
            some_avg60=_psi_value(cpu, "some", "avg60"),
        ),
        memory=MemoryPressure(
            some_avg10=_psi_value(mem, "some", "avg10"),
            some_avg60=_psi_value(mem, "some", "avg60"),
            full_avg60=_psi_value(mem, "full", "avg60"),
        ),
        io=IoPressure(
            some_avg60=_psi_value(io, "some", "avg60"),
            full_avg60=_psi_value(io, "full", "avg60"),
        ),
    )


def pool_from_metadata(meta: dict[str, Any], resources: dict[str, Any]) -> StoragePool:
    """Build a StoragePool from LXD pool metadata and its /resources payload."""
    space = resources["space"]
    return StoragePool(
        name=str(meta["name"]),
        driver=str(meta.get("driver", "")),
        total_bytes=int(space["total"]),
        used_bytes=int(space.get("used", 0)),
    )


def _instance_cores(config: dict[str, Any], host_cores: int) -> float:
    """Return committed cores for one instance from expanded limits (0.0 if unset)."""
    cpu_raw = str(config.get("limits.cpu", "")).strip()
    allowance_raw = str(config.get("limits.cpu.allowance", "")).strip()
    if not cpu_raw and not allowance_raw:
        return 0.0
    base = parse_cpu_count(cpu_raw) if cpu_raw else host_cores
    if allowance_raw:
        return base * parse_cpu_allowance(allowance_raw)
    return float(base)


def compute_committed(
    instances: list[dict[str, Any]],
    pool_names: list[str],
    host_ram_total: int,
    host_cores: int,
) -> tuple[Committed, list[str]]:
    """Sum committed CPU, memory and root-disk limits over expanded instance configs.

    Uses expanded_config/expanded_devices so profile-inherited limits count. An
    instance missing limits.cpu or limits.memory, or with an unparseable limit,
    is counted in instances_without_limits. Returns the aggregate and error strings.
    """
    errors: list[str] = []
    cores = 0.0
    memory = 0
    without_limits = 0
    disk: dict[str, int] = {name: 0 for name in pool_names}
    for inst in instances:
        name = str(inst.get("name", "?"))
        config: dict[str, Any] = inst.get("expanded_config") or {}
        devices: dict[str, Any] = inst.get("expanded_devices") or {}
        unbounded = not str(config.get("limits.cpu", "")).strip()
        unbounded |= not str(config.get("limits.memory", "")).strip()
        try:
            cores += _instance_cores(config, host_cores)
        except ValueError:
            errors.append(f"limit_unparsed:{name}:limits.cpu")
            unbounded = True
        mem_raw = str(config.get("limits.memory", "")).strip()
        if mem_raw:
            try:
                memory += parse_byte_size(mem_raw, host_ram_total)
            except ValueError:
                errors.append(f"limit_unparsed:{name}:limits.memory")
                unbounded = True
        if unbounded:
            without_limits += 1
        for device in devices.values():
            if device.get("type") != "disk" or device.get("path") != "/":
                continue
            pool = str(device.get("pool", "")).strip()
            size = str(device.get("size", "")).strip()
            if not pool or not size:
                continue
            try:
                disk[pool] = disk.get(pool, 0) + parse_byte_size(size)
            except ValueError:
                errors.append(f"device_unparsed:{name}:size")
    committed = Committed(
        cpu_cores=round(cores, 3),
        memory_bytes=memory,
        disk_bytes=disk,
        instances=len(instances),
        instances_without_limits=without_limits,
    )
    return committed, errors


def _sample_host_blocking() -> HostSample:
    """Take psutil and loadavg readings; runs in a worker thread (blocks ~0.1s)."""
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()
    return HostSample(
        logical_cores=psutil.cpu_count(logical=True) or os.cpu_count() or 1,
        usage_pct=psutil.cpu_percent(interval=0.1),
        load1=load1,
        load5=load5,
        load15=load15,
        mem_total=vm.total,
        mem_available=vm.available,
        swap_total=swap.total,
        swap_used=swap.used,
        disk_total=disk.total,
        disk_free=disk.free,
    )


def _empty_host() -> HostSample:
    """Zeroed host sample used when host collection fails."""
    return HostSample(0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, 0, 0)


class _LxdApi:
    """Minimal GET-only client for the LXD REST API over a unix socket."""

    def __init__(self, socket_path: str, timeout_s: float) -> None:
        """Create a client bound to ``socket_path`` with a per-request timeout."""
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://lxd",
            timeout=timeout_s,
        )

    async def get_json(self, path: str) -> Any:
        """GET ``path`` and return the LXD ``metadata`` payload; raises on error envelopes."""
        response = await self._http.get(path)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("type") == "error":
            raise RuntimeError(f"LXD error envelope for {path}")
        return body.get("metadata")

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()


async def _section(
    name: str, aw: Awaitable[_T], deadline: float, errors: list[str]
) -> _T | None:
    """Await one collector under the shared deadline; map failures to error strings.

    Returns None and appends ``<name>_timeout|_unavailable|_error`` on failure.
    """
    try:
        async with asyncio.timeout_at(deadline):
            return await aw
    except TimeoutError:
        errors.append(f"{name}_timeout")
    except httpx.TimeoutException:
        errors.append(f"{name}_timeout")
    except (OSError, httpx.TransportError):
        errors.append(f"{name}_unavailable")
    except Exception:
        log.warning("host snapshot section %s failed", name, exc_info=True)
        errors.append(f"{name}_error")
    return None


async def _read_pools(api: _LxdApi) -> list[StoragePool]:
    """List LXD storage pools and fetch each pool's space resources."""
    listing = await api.get_json("/1.0/storage-pools?recursion=1")
    pools: list[StoragePool] = []
    for meta in listing:
        name = str(meta["name"])
        resources = await api.get_json(
            f"/1.0/storage-pools/{quote(name, safe='')}/resources"
        )
        pools.append(pool_from_metadata(meta, resources))
    return pools


async def _collect_lxd(
    socket_path: str, deadline: float, host: HostSample, errors: list[str]
) -> tuple[list[StoragePool], Committed]:
    """Collect storage pools and committed limits from LXD; sections fail independently."""
    loop = asyncio.get_running_loop()
    api = _LxdApi(socket_path, timeout_s=max(deadline - loop.time(), 0.01))
    try:
        pools = await _section("lxd", _read_pools(api), deadline, errors)
        instances = await _section(
            "lxd", api.get_json("/1.0/instances?recursion=1"), deadline, errors
        )
    finally:
        await api.aclose()
    pool_list = pools or []
    names = [pool.name for pool in pool_list]
    if instances is None:
        return pool_list, Committed(0.0, 0, {n: 0 for n in names}, 0, 0)
    committed, limit_errors = compute_committed(
        instances, names, host.mem_total, host.logical_cores
    )
    errors.extend(limit_errors)
    return pool_list, committed


def _dedupe(items: list[str]) -> list[str]:
    """Drop repeated error strings while keeping first-seen order."""
    return list(dict.fromkeys(items))


async def collect_host_resources(
    *,
    timeout_s: float = 5.0,
    lxd_socket: str = DEFAULT_LXD_SOCKET,
    proc_root: Path = Path("/proc"),
) -> HostResourceSnapshot:
    """Collect a full host snapshot within ``timeout_s``; never raises.

    Failed or timed-out sections fall back to zero/null values and are named in
    ``errors``. Host metrics run first; PSI and LXD then run concurrently.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_s, 0.0)
    errors: list[str] = []
    host = await _section(
        "host", asyncio.to_thread(_sample_host_blocking), deadline, errors
    )
    host = host or _empty_host()
    pressure, lxd_result = await asyncio.gather(
        _section("psi", asyncio.to_thread(read_pressure, proc_root), deadline, errors),
        _collect_lxd(lxd_socket, deadline, host, errors),
    )
    pools, committed = lxd_result
    return HostResourceSnapshot(
        collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        cpu=CpuStats(
            host.logical_cores, host.usage_pct, host.load1, host.load5, host.load15
        ),
        memory=MemoryStats(
            host.mem_total, host.mem_available, host.swap_total, host.swap_used
        ),
        pressure=pressure,
        storage_pools=pools,
        host_root_fs=RootFs(host.disk_total, host.disk_free),
        committed=committed,
        errors=_dedupe(errors),
    )
