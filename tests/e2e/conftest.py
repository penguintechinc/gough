"""Pytest fixtures for E2E tests."""

import os
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Generator, Dict, Any
import pytest
import requests
from datetime import datetime, timedelta


@pytest.fixture(scope="session")
def primary_cluster() -> Generator[Dict[str, Any], None, None]:
    """
    Bootstrap a primary Gough cluster (api-manager) for E2E tests.

    Starts the api-manager as a subprocess, configures it to use the CI Postgres,
    and polls /health until ready.

    Yields:
        Dict with cluster config: api_url, admin_token, cluster_id, etc.
    """
    print("[primary_cluster] Bootstrapping primary cluster...")

    cluster_id = f"e2e-{datetime.now().strftime('%s')}"
    temp_dir = tempfile.mkdtemp(prefix=f"gough-primary-{cluster_id}-")

    # Prepare environment: map CI Postgres vars to api-manager config
    api_env = os.environ.copy()
    api_env.update({
        "DB_HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "DB_PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "DB_NAME": os.environ.get("POSTGRES_DB", "gough_test"),
        "DB_USER": os.environ.get("POSTGRES_USER", "postgres"),
        "DB_PASS": os.environ.get("POSTGRES_PASSWORD", "test"),
        "REDIS_URL": f"redis://{os.environ.get('REDIS_HOST', 'localhost')}:{os.environ.get('REDIS_PORT', '6379')}/0",
        # Config.DEBUG is read from QUART_DEBUG (not DEBUG) and Config.TESTING is
        # hard-coded False, so set QUART_DEBUG=true to make validate_secrets() treat
        # this as dev and permit dev-default secrets. Also pass real (non-default)
        # secrets as belt-and-suspenders so startup succeeds even if config selection changes.
        "QUART_DEBUG": "true",
        "SECRET_KEY": "e2e-test-secret-key-not-for-production-0000000000",
        "JWT_SECRET_KEY": "e2e-test-jwt-secret-not-for-production-0000000000",
        "SECURITY_PASSWORD_SALT": "e2e-test-salt-not-for-production-0000000000",
        # run.py reads QUART_HOST/QUART_PORT; bind where this fixture polls.
        "QUART_HOST": "127.0.0.1",
        "QUART_PORT": "8080",
        "DB_TYPE": "postgres",
    })

    print(f"[primary_cluster] Cluster ID: {cluster_id}")
    print(f"[primary_cluster] Starting api-manager (hypercorn) from services/api-manager...")

    # Start api-manager as subprocess using hypercorn
    api_manager_dir = Path(__file__).resolve().parents[2] / "services" / "api-manager"
    # Launch via the app's own entrypoint (run.py): it awaits the async
    # create_app() factory, waits for the DB, and serves on QUART_HOST/QUART_PORT.
    proc = subprocess.Popen(
        ["python3", "run.py"],
        cwd=str(api_manager_dir),
        env=api_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    print(f"[primary_cluster] api-manager process PID: {proc.pid}")

    config = {
        "cluster_id": cluster_id,
        "api_url": "http://localhost:8080",
        "admin_token": "test-token-" + os.urandom(16).hex(),
        "cluster_config_dir": temp_dir,
        "vault_unseal_keys": [],
        "primary_ip": "127.0.0.1"
    }

    # Poll /health until ready, with process death detection
    max_retries = 90
    for i in range(max_retries):
        # Check if process is still alive
        if proc.poll() is not None:
            # Process died; capture stderr and stdout for error reporting
            stderr_text = proc.stderr.read() if proc.stderr else "(no stderr)"
            stdout_text = proc.stdout.read() if proc.stdout else "(no stdout)"
            raise RuntimeError(
                f"api-manager process died (exit code {proc.returncode}). "
                f"stdout:\n{stdout_text[-4000:]}\nstderr:\n{stderr_text[-4000:]}"
            )

        try:
            resp = requests.get(f"{config['api_url']}/health", timeout=2)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "healthy":
                    print("[primary_cluster] Cluster is healthy")
                    break
        except requests.RequestException:
            pass

        if i == max_retries - 1:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise TimeoutError(
                f"Primary cluster did not become healthy within {max_retries}s"
            )

        time.sleep(1)

    yield config

    # Cleanup
    print(f"[primary_cluster] Tearing down cluster {cluster_id}")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print("[primary_cluster] Forcefully killing api-manager")
        proc.kill()
        proc.wait()

    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def sim_node_factory(primary_cluster) -> Generator:
    """
    Factory fixture that spins up a node-sim QEMU VM with given params.

    Skips if QEMU/KVM is unavailable (e.g., CI without hardware virtualization).

    Returns:
        Callable that accepts kwargs: firmware, ipv6, secure_boot, dmi_uuid, smart_pattern, with_bmc
    """
    # Check for QEMU/KVM availability on CI
    if os.getenv("GOUGH_TEST_ENV") == "ci" and not os.path.exists("/dev/kvm"):
        pytest.skip("QEMU/KVM not available on CI (tests 02-16 require hardware simulation)")

    sim_nodes = []

    def _launch_sim(**kwargs) -> Dict[str, Any]:
        """Launch a node-sim and return its metadata."""
        script_dir = Path(__file__).parent.parent.parent / "dev-tools" / "node-sim"
        launch_script = script_dir / "launch-sim.sh"

        cmd = [str(launch_script)]

        # Build command from kwargs
        if "firmware" in kwargs:
            cmd.extend(["--firmware", kwargs["firmware"]])
        if "ipv6" in kwargs:
            cmd.extend(["--ipv6", kwargs["ipv6"]])
        if kwargs.get("secure_boot"):
            cmd.append("--secure-boot")
        if "dmi_uuid" in kwargs:
            cmd.extend(["--dmi-uuid", kwargs["dmi_uuid"]])
        if "smart_pattern" in kwargs:
            cmd.extend(["--smart-pattern", kwargs["smart_pattern"]])
        if kwargs.get("with_bmc"):
            cmd.append("--with-bmc")

        # Execute launch-sim.sh
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to launch sim: {result.stderr}")

        # Parse output for PID file and other metadata
        # Expected output: "[launch-sim] QEMU PID: <pid>" etc.
        pid = None
        for line in result.stdout.split("\n"):
            if "QEMU PID:" in line:
                pid = int(line.split(":")[-1].strip())
                break

        if not pid:
            raise RuntimeError("Could not parse QEMU PID from launch-sim output")

        metadata = {
            "pid": pid,
            "dmi_uuid": kwargs.get("dmi_uuid", ""),
            "firmware": kwargs.get("firmware", "bios"),
            "ipv6_mode": kwargs.get("ipv6", "disabled"),
            "smart_pattern": kwargs.get("smart_pattern", "healthy"),
            "with_bmc": kwargs.get("with_bmc", False),
            "startup_time": time.time()
        }

        sim_nodes.append(metadata)
        return metadata

    yield _launch_sim

    # Cleanup
    print("[sim_node_factory] Cleaning up sim nodes...")
    cleanup_script = Path(__file__).parent.parent.parent / "dev-tools" / "node-sim" / "cleanup.sh"
    subprocess.run([str(cleanup_script)], capture_output=True)


@pytest.fixture
def gough_cli_token(primary_cluster) -> str:
    """
    Provide an authenticated CLI token for API calls.

    In CI, this would use OIDC mock to issue a real JWT.
    For local testing, returns a pre-shared admin token.
    """
    return primary_cluster["admin_token"]


@pytest.fixture
def api_client(primary_cluster, gough_cli_token):
    """
    HTTP client pre-configured with auth headers for API calls.
    """
    class GoughAPIClient:
        def __init__(self, base_url: str, token: str):
            self.base_url = base_url.rstrip("/")
            self.token = token
            self.session = requests.Session()
            self.session.headers.update({
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            })

        def get(self, path: str, **kwargs) -> requests.Response:
            return self.session.get(f"{self.base_url}{path}", **kwargs)

        def post(self, path: str, data: Dict = None, **kwargs) -> requests.Response:
            return self.session.post(f"{self.base_url}{path}", json=data, **kwargs)

        def put(self, path: str, data: Dict = None, **kwargs) -> requests.Response:
            return self.session.put(f"{self.base_url}{path}", json=data, **kwargs)

        def delete(self, path: str, **kwargs) -> requests.Response:
            return self.session.delete(f"{self.base_url}{path}", **kwargs)

    return GoughAPIClient(primary_cluster["api_url"], gough_cli_token)
