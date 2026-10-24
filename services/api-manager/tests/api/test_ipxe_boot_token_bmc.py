"""Hub iPXE cmdline wiring: worker boot-token propagation, no BMC data on anonymous routes."""

from __future__ import annotations

from app.api.ipxe import (
    _render_deploy_ipxe_script,
    _render_helper_ipxe_script,
)

MAC = "aa:bb:cc:dd:ee:ff"


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

    def test_renderers_never_emit_bmc_params(self) -> None:
        for script in (
            _render_helper_ipxe_script(MAC, "tok", "https://p.example", firmware="bios"),
            _render_deploy_ipxe_script(MAC, "tok", "https://p.example"),
        ):
            assert "gough_bmc" not in script
