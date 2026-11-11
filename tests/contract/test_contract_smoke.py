"""First contract smoke tests against the real api-manager (no mocks).

Strict xfails record where the server DISAGREES with the standards/CLI; they
flip to failures (XPASS strict) the moment the server is fixed, forcing the
marker's removal.
"""

import base64
import json
import re
import uuid

import pytest

pytestmark = pytest.mark.contract

REQUIRED_CLAIMS = {"sub", "iss", "aud", "iat", "exp", "scope", "tenant", "teams", "roles"}


def _claims(token: str) -> dict:
    """Decode (without verifying) the JWT payload for shape assertions."""
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_version_is_anonymous(anon):
    """GET /api/v1/version needs no auth and returns the version document."""
    r = anon.request("GET", "/api/v1/version")
    assert r.status_code == 200
    assert {"version", "build_sha", "openapi_version", "milestone", "cluster_id"} <= set(r.json())


def test_nodes_requires_auth(anon):
    """Unauthenticated read of a scoped route is rejected at the gate."""
    assert anon.request("GET", "/api/v1/nodes/").status_code == 401


def test_nodes_list_envelope(admin):
    """GET /api/v1/nodes/ with an admin JWT returns the success envelope."""
    r = admin.request("GET", "/api/v1/nodes/")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert set(body["data"]) >= {"nodes", "total"}
    assert isinstance(body["data"]["nodes"], list)
    assert body["data"]["total"] == len(body["data"]["nodes"])
    assert {"request_id", "timestamp", "version"} <= set(body["meta"])


def test_nodes_without_trailing_slash_redirects(admin):
    """The slashless path the CLI may use is a 308 to the canonical slash path."""
    r = admin.request("GET", "/api/v1/nodes")
    assert r.status_code == 308
    assert r.headers["Location"].endswith("/api/v1/nodes/")


def test_scope_enforced_for_viewer(viewer):
    """A viewer token can read nodes but is refused an admin-scoped route."""
    assert viewer.request("GET", "/api/v1/nodes/").status_code == 200
    assert viewer.request("GET", "/api/v1/webhooks").status_code == 403


def test_minted_jwt_has_required_claims(server):
    """Every role's token carries the standards-required claim set."""
    for role, token in server["tokens"].items():
        assert REQUIRED_CLAIMS <= set(_claims(token)), role


@pytest.mark.xfail(reason="server issues numeric auth_user ids as JWT sub / login user.id "
                          "(standards: UUID public_id on the wire)")
def test_wire_ids_are_uuids(server, admin):
    """JWT sub must be a UUID, never a sequential integer."""
    sub = _claims(server["tokens"]["admin"])["sub"]
    uuid.UUID(sub)


def test_wire_ids_current_shape_is_integer(server):
    """Characterisation: today's sub is a numeric string (flips when xfail above passes)."""
    assert re.fullmatch(r"\d+", _claims(server["tokens"]["admin"])["sub"])


@pytest.mark.xfail(reason="CLI device-flow endpoint /api/v1/auth/device is not served "
                          "(dead code in app/auth.py shadowed by app/auth/ package) and is "
                          "not anonymous -> 401")
def test_device_flow_endpoint_reachable_anonymously(anon):
    """OIDC device authorization must be callable without a bearer token."""
    r = anon.request("POST", "/api/v1/auth/device", json={})
    assert r.status_code not in (401, 403, 404)
