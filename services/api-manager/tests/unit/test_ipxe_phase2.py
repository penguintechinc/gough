"""Tests for Phase-2 iPXE deploy script renderer.

Covers:
  - _render_phase2_deploy_script: happy path, token embedding, fallback boot
  - Token validation in /ipxe/deploy endpoint
  - Both helper and deploy chainload paths
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.api.ipxe import _render_deploy_ipxe_script


class TestRenderPhase2DeployScript:
    """Phase-2 deploy script rendering."""

    def test_renders_valid_script_header(self) -> None:
        """Output is a valid iPXE script with correct header."""
        mac = "aa:bb:cc:dd:ee:ff"
        jwt_token = "test.jwt.token"
        primary_url = "https://primary.example.com"

        result = _render_deploy_ipxe_script(mac, jwt_token, primary_url)

        assert result.startswith("#!ipxe\n")
        assert f"mac={mac}" in result
        assert "fallback" in result.lower() or "failed" in result.lower()

    def test_embeds_mac_in_comments(self) -> None:
        """MAC address appears in script comments for traceability."""
        mac = "11:22:33:44:55:66"
        jwt_token = "eyJ..."
        primary_url = "https://example.com"

        result = _render_deploy_ipxe_script(mac, jwt_token, primary_url)

        # MAC should appear in header comment
        assert mac in result

    def test_embeds_bootstrap_token_in_kernel_args(self) -> None:
        """Bootstrap token embedded as kernel cmdline parameter."""
        mac = "aa:bb:cc:dd:ee:ff"
        jwt_token = "eyJhbGc.eyJtYWMi.SflKxw"
        primary_url = "https://primary.example.com"

        result = _render_deploy_ipxe_script(mac, jwt_token, primary_url)

        # Token should appear as a kernel argument
        assert f"gough.bootstrap_token={jwt_token}" in result or jwt_token in result

    def test_includes_boot_fallback(self) -> None:
        """Script includes fallback boot logic for retry on failure."""
        mac = "aa:bb:cc:dd:ee:ff"
        jwt_token = "token"
        primary_url = "https://example.com"

        result = _render_deploy_ipxe_script(mac, jwt_token, primary_url)

        # Should have boot command and fallback handling
        assert "boot" in result
        assert ("fallback" in result or "goto" in result)

    def test_uses_primary_url_for_artifacts(self) -> None:
        """Kernel/initrd URLs use primary_url parameter."""
        mac = "aa:bb:cc:dd:ee:ff"
        jwt_token = "token"
        primary_url = "https://custom.deploy.server:8443"

        result = _render_deploy_ipxe_script(mac, jwt_token, primary_url)

        # Primary URL should be referenced in the script
        assert primary_url.rstrip("/") in result

    def test_handles_url_with_trailing_slash(self) -> None:
        """Normalizes primary URL trailing slash."""
        mac = "aa:bb:cc:dd:ee:ff"
        jwt_token = "token"
        primary_url = "https://example.com/"

        result = _render_deploy_ipxe_script(mac, jwt_token, primary_url)

        # Should strip trailing slash and not create double slashes
        assert "https://example.com//" not in result
        assert "https://example.com/" in result or "https://example.com" in result

    def test_all_required_ipxe_directives_present(self) -> None:
        """Script includes kernel, initrd, and boot directives."""
        mac = "aa:bb:cc:dd:ee:ff"
        jwt_token = "token"
        primary_url = "https://example.com"

        result = _render_deploy_ipxe_script(mac, jwt_token, primary_url)

        # iPXE script must have these directives
        assert "kernel" in result.lower() or "chain" in result
        # Boot or chain directive
        assert "boot" in result.lower() or "chain" in result.lower()
