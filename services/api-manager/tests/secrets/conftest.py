"""Fixtures for secrets module tests."""

from __future__ import annotations

import importlib
import sys
import pytest
from unittest.mock import MagicMock
from quart import Quart

# Mock out cloud provider modules that may not be installed in test environment
# Create proper exception classes for hvac and google.api_core
class MockException(Exception):
    pass

hvac_exceptions = MagicMock()
hvac_exceptions.InvalidRequest = type('InvalidRequest', (Exception,), {})
hvac_exceptions.VaultDown = type('VaultDown', (Exception,), {})
hvac_exceptions.InvalidPath = type('InvalidPath', (Exception,), {})
hvac_exceptions.Forbidden = type('Forbidden', (Exception,), {})
hvac_exceptions.VaultError = type('VaultError', (Exception,), {})

hvac_mock = MagicMock()
hvac_mock.exceptions = hvac_exceptions

# Google API Core exceptions
google_exceptions = MagicMock()
google_exceptions.NotFound = type('NotFound', (Exception,), {})
google_exceptions.PermissionDenied = type('PermissionDenied', (Exception,), {})

# Create google.api_core mock that properly exposes .exceptions
google_api_core_mock = MagicMock()
google_api_core_mock.exceptions = google_exceptions

azure_core_exceptions = MagicMock()
azure_core_exceptions.ResourceNotFoundError = type('ResourceNotFoundError', (Exception,), {})

def _stub_namespace(name, mock):
    """Stub a top-level namespace package only if it is genuinely absent.

    These stubs run at module import, so whatever they replace stays replaced
    for the entire pytest session. Replacing an installed top-level package is
    therefore not local to this directory: ``sys.modules["google"] = MagicMock()``
    made ``google`` a non-package, so every later
    ``from google.protobuf import descriptor`` inside the generated gRPC stubs
    raised "'google' is not a package" -- taking out tests/test_grpc_server.py at
    collection and producing the protobuf errors logged by app.api.primary. The
    same applies to ``azure``, whose real subpackages app/clouds/azure.py needs.

    Submodules below are still stubbed unconditionally: these tests drive them
    deliberately (simulating Vault/S3/KeyVault failures), and stubbing a leaf
    does not stop its siblings resolving from the real package.
    """
    if name not in sys.modules:
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules[name] = mock


azure_mock = MagicMock()
_stub_namespace("azure", azure_mock)
sys.modules["azure.core"] = MagicMock()
sys.modules["azure.core.exceptions"] = azure_core_exceptions
sys.modules["azure.keyvault"] = MagicMock()
sys.modules["azure.keyvault.secrets"] = MagicMock()
sys.modules["azure.identity"] = MagicMock()

_stub_namespace("google", MagicMock())
sys.modules["google.api_core"] = google_api_core_mock
sys.modules["google.api_core.exceptions"] = google_exceptions
sys.modules["google.cloud"] = MagicMock()
sys.modules["google.cloud.secretmanager"] = MagicMock()
sys.modules["hvac"] = hvac_mock
sys.modules["infisical_client"] = MagicMock()
sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()

# Create ClientError with proper response attribute
def _client_error_init(self, error_response=None, operation_name="Operation"):
    super(self.__class__, self).__init__(str(error_response) if error_response else "Unknown")
    self.response = error_response or {"Error": {"Code": "Unknown", "Message": ""}}

ClientError = type('ClientError', (Exception,), {
    '__init__': _client_error_init
})
NoCredentialsError = type('NoCredentialsError', (Exception,), {})

botocore_exceptions_module = type('module', (), {
    'ClientError': ClientError,
    'NoCredentialsError': NoCredentialsError,
})()
sys.modules["botocore.exceptions"] = botocore_exceptions_module


@pytest.fixture
def app():
    """Create a Quart app for tests.

    This provides an app instance that can be used to set config values.
    """
    quart_app = Quart(__name__)
    quart_app.config["TESTING"] = True
    quart_app.config["ENCRYPTION_KEY"] = ""
    return quart_app


@pytest.fixture
def mock_current_app(app, monkeypatch):
    """Mock current_app to return the test app config.

    Use this in tests that access current_app via current_app.config.
    """
    # Import modules before creating mock to avoid shadowing
    import app.secrets as secrets_module
    import app.secrets.encrypted_db as encrypted_db_module
    import app.secrets.aws_secrets as aws_module
    import app.secrets.gcp_secrets as gcp_module
    import app.secrets.azure_keyvault as azure_module
    import app.secrets.vault as vault_module
    import app.secrets.infisical as infisical_module

    # Create a mock object that has a .config attribute
    mock_app = MagicMock()
    mock_app.config = app.config

    # Patch current_app in all modules that use it
    monkeypatch.setattr(encrypted_db_module, "current_app", mock_app)
    monkeypatch.setattr(secrets_module, "current_app", mock_app)
    monkeypatch.setattr(aws_module, "current_app", mock_app)
    monkeypatch.setattr(gcp_module, "current_app", mock_app)
    monkeypatch.setattr(azure_module, "current_app", mock_app)
    monkeypatch.setattr(vault_module, "current_app", mock_app)
    monkeypatch.setattr(infisical_module, "current_app", mock_app)

    # Patch vault_module.hvac so vault.py's exception catch clauses use the mock
    # exception classes (vault.py imports hvac at the top level, so the binding
    # is fixed at first import time and doesn't update when sys.modules is replaced).
    monkeypatch.setattr(vault_module, "hvac", hvac_mock)

    return app
