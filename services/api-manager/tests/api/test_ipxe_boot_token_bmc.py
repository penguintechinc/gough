"""Hub iPXE cmdline wiring: worker boot-token propagation and optional BMC params.

Covers the booted machine's ability to authenticate worker /boot-event callbacks
(``X-Boot-Token``) and the ``gough_bmc`` / ``gough_bmc_sha256`` emission rules.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.api.ipxe import (
    _bmc_cmdline_params,
    _render_deploy_ipxe_script,
    _render_helper_ipxe_script,
)

MAC = "aa:bb:cc:dd:ee:ff"
PIN_HEX = "ab" * 32
PIN_COLON = ":".join(["AB"] * 32)


def _db_with_bmc(row, tables=("nodes", "node_bmc", "ipxe_machines")):
    """Build a DAL stand-in whose node_bmc lookup returns ``row``."""
    db = MagicMock()
    db.tables = list(tables)
    db.return_value.select.return_value.first.return_value = row
    return db


class TestBootTokenCmdline:
    """The booted machine must receive ${boot_token} and the worker URL on its cmdline."""

    def test_helper_script_carries_boot_token_and_worker(self) -> None:
        script = _render_helper_ipxe_script(MAC, "tok", "https://p.example", firmware="uefi")
        assert "gough_boot_token=${boot_token}" in script
        assert "gough_worker=${worker_url}" in script

    def test_deploy_script_carries_boot_token_and_worker(self) -> None:
        script = _render_deploy_ipxe_script(MAC, "tok", "https://p.example")
        assert "gough_boot_token=${boot_token}" in script
        assert "gough_worker=${worker_url}" in script

    def test_no_bmc_params_without_extra(self) -> None:
        for script in (
            _render_helper_ipxe_script(MAC, "tok", "https://p.example", firmware="bios"),
            _render_deploy_ipxe_script(MAC, "tok", "https://p.example"),
        ):
            assert "gough_bmc" not in script

    def test_extra_cmdline_appended_on_kernel_line(self) -> None:
        extra = f"gough_bmc=10.0.0.9 gough_bmc_sha256={PIN_HEX}"
        helper = _render_helper_ipxe_script(
            MAC, "tok", "https://p.example", firmware="uefi", extra_cmdline=extra
        )
        deploy = _render_deploy_ipxe_script(MAC, "tok", "https://p.example", extra)
        for script in (helper, deploy):
            kernel = next(ln for ln in script.splitlines() if ln.startswith("kernel "))
            assert extra in kernel


class TestBmcCmdlineParams:
    """gough_bmc/gough_bmc_sha256 only when the hub has them for the machine."""

    def test_none_record(self) -> None:
        assert _bmc_cmdline_params(None) == ""

    def test_node_without_bmc_row(self) -> None:
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(None)):
            assert _bmc_cmdline_params({"source": "nodes", "id": 1}) == ""

    def test_node_without_node_bmc_table(self) -> None:
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(None, tables=("nodes",))):
            assert _bmc_cmdline_params({"source": "nodes", "id": 1}) == ""

    def test_node_with_endpoint_and_pin(self) -> None:
        row = SimpleNamespace(endpoint="https://10.1.2.3", cert_fingerprint=PIN_COLON)
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(row)):
            out = _bmc_cmdline_params({"source": "nodes", "id": 1})
        assert out == f"gough_bmc=10.1.2.3 gough_bmc_sha256={PIN_HEX}"

    def test_node_with_endpoint_no_pin(self) -> None:
        row = SimpleNamespace(endpoint="bmc01.lab.example", cert_fingerprint=None)
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(row)):
            out = _bmc_cmdline_params({"source": "nodes", "id": 1})
        assert out == "gough_bmc=bmc01.lab.example"
        assert "sha256" not in out

    def test_malformed_pin_dropped(self) -> None:
        row = SimpleNamespace(endpoint="10.1.2.3", cert_fingerprint="AB:CD")
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(row)):
            assert _bmc_cmdline_params({"source": "nodes", "id": 1}) == "gough_bmc=10.1.2.3"

    @pytest.mark.parametrize("endpoint", ["10.0.0.1 root=/dev/sda", "bad host", "-x", "", "http://"])
    def test_unsafe_or_empty_endpoint_dropped(self, endpoint: str) -> None:
        row = SimpleNamespace(endpoint=endpoint, cert_fingerprint=PIN_COLON)
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(row)):
            assert _bmc_cmdline_params({"source": "nodes", "id": 1}) == ""

    def test_legacy_machine_bmc_address(self) -> None:
        rec = {"source": "ipxe_machines", "id": 4, "raw": {"bmc_address": "192.0.2.7"}}
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(None)):
            assert _bmc_cmdline_params(rec) == "gough_bmc=192.0.2.7"

    def test_legacy_machine_without_bmc(self) -> None:
        rec = {"source": "ipxe_machines", "id": 4, "raw": {"bmc_address": None}}
        with patch("app.api.ipxe.get_db", return_value=_db_with_bmc(None)):
            assert _bmc_cmdline_params(rec) == ""
