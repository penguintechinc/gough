"""Regression test: every gRPC pb2 stub `grpc_runner.serve()` registers
must import and parse cleanly.

``identity_pb2.py`` was hand-fabricated rather than buf-generated
(commit 0c1a04f) and its embedded ``FileDescriptorProto`` was internally
inconsistent -- every import of it raised
``TypeError: Couldn't parse file content!``. This went undetected in every
deployment because ``app.__init__._start_grpc()`` wrapped the whole
``grpc_runner`` import in a bare ``try/except Exception`` and only logged a
WARNING (fixed separately, see gh-22), and the only existing test exercising
these servicers (``tests/test_grpc_server.py``) installed a
``sys.modules['gough.identity_pb2']`` fake specifically to route around the
broken import rather than prove it worked.

This is the missing regression coverage: it imports the real generated
modules with no fake/shim in the way, so it fails loudly the moment any of
them stops importing again.
"""

from __future__ import annotations

import importlib

import pytest

# The five services app/grpc_runner.py registers on the aio server via
# add_*Servicer_to_server() -- one pb2 (messages) + one pb2_grpc (service
# stub/servicer base) module per service. Importing app.grpc_server (below)
# already puts app/grpc on sys.path the same way grpc_runner.py does.
_SERVED_PB2_MODULES = (
    "gough.ipxe_pb2",
    "gough.joiner_pb2",
    "gough.biomes_pb2",
    "gough.audit_pb2",
    "gough.identity_pb2",
)

_SERVED_PB2_GRPC_MODULES = (
    "gough.ipxe_pb2_grpc",
    "gough.joiner_pb2_grpc",
    "gough.biomes_pb2_grpc",
    "gough.audit_pb2_grpc",
    "gough.identity_pb2_grpc",
)


@pytest.fixture(autouse=True)
def _grpc_pkg_on_path() -> None:
    """Import app.grpc_server once per test to install the sys.path shim
    (app/grpc -> importable as top-level `gough`) that all the generated
    *_pb2_grpc.py files rely on via their bare `from gough import ...`.
    """
    import app.grpc_server  # noqa: F401


@pytest.mark.parametrize("module_name", _SERVED_PB2_MODULES)
def test_served_pb2_module_imports(module_name: str) -> None:
    """Each pb2 message module grpc_runner.serve() registers must import.

    regression: gh-22
    """
    module = importlib.import_module(module_name)
    assert module is not None


@pytest.mark.parametrize("module_name", _SERVED_PB2_GRPC_MODULES)
def test_served_pb2_grpc_module_imports(module_name: str) -> None:
    """Each pb2_grpc servicer/stub module grpc_runner.serve() registers
    must import.

    regression: gh-22
    """
    module = importlib.import_module(module_name)
    assert module is not None


def test_identity_pb2_descriptor_is_parseable_and_constructible() -> None:
    """Prove identity_pb2's descriptor actually parses and its message
    types are constructible -- not just that the module object exists.
    The historical bug was an unparseable embedded descriptor raised at
    import time, so a bare "module imported" assertion alone would not
    have caught it if some other code path had pre-populated sys.modules.

    regression: gh-22
    """
    from gough import identity_pb2

    req = identity_pb2.IssueSVIDRequest(
        service_name="test-service", ttl_seconds=60, csr_pem="test-pem"
    )
    assert req.service_name == "test-service"
    assert req.ttl_seconds == 60

    resp = identity_pb2.VerifyOTPNResponse(valid=True, node_id="node-1")
    assert resp.valid is True
    assert resp.node_id == "node-1"


def test_grpc_runner_module_imports() -> None:
    """app.grpc_runner -- the actual module app.__init__._start_grpc()
    imports at startup -- must import end-to-end with no fakes involved.

    regression: gh-22
    """
    import app.grpc_runner  # noqa: F401

    assert hasattr(app.grpc_runner, "serve")
