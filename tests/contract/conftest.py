"""Fixtures: start the REAL api-manager via scripts/api-test-server, always stop it."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "api-test-server"


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    """Run a harness script, raising with its stderr on non-zero exit."""
    return subprocess.run(
        [str(SCRIPTS / script)], env=env, capture_output=True, text=True, timeout=180, check=True
    )


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """Start the server + seed tokens; teardown stops it even when tests fail."""
    state = tmp_path_factory.mktemp("api-test-server")
    env = {**os.environ, "API_TEST_STATE_DIR": str(state)}
    python = os.environ.get("API_TEST_PYTHON", str(REPO / ".venv" / "bin" / "python"))
    env["API_TEST_PYTHON"] = python
    try:
        started = _run("start.sh", env)
        base = started.stdout.strip().split("BASE_URL=")[-1]
        subprocess.run(
            [python, str(SCRIPTS / "seed.py")], env=env, check=True, capture_output=True, text=True
        )
        tokens = json.loads((state / "tokens.json").read_text())
        yield {"base": base, "tokens": tokens, "state": state}
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"harness failed: {exc.cmd}\n{exc.stdout}\n{exc.stderr}")
    finally:
        subprocess.run([str(SCRIPTS / "stop.sh")], env=env, check=False, timeout=60)


class Api:
    """Thin requests wrapper bound to the server base URL and a bearer token."""

    def __init__(self, base: str, token: str | None = None):
        self.base, self.token = base, token

    def request(self, method: str, path: str, **kw) -> requests.Response:
        """Issue a request without following redirects (wire behaviour is the contract)."""
        headers = dict(kw.pop("headers", {}))
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        kw.setdefault("allow_redirects", False)
        return requests.request(method, self.base + path, headers=headers, timeout=15, **kw)


@pytest.fixture
def anon(server):
    """Unauthenticated client."""
    return Api(server["base"])


@pytest.fixture
def admin(server):
    """Client with an admin-role JWT minted through the app's own login."""
    return Api(server["base"], server["tokens"]["admin"])


@pytest.fixture
def viewer(server):
    """Client with a viewer-role JWT (read scopes only)."""
    return Api(server["base"], server["tokens"]["viewer"])
