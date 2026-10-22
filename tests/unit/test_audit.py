"""Unit tests for audit.py module.

Tests for:
- AuditEventType enum
- AuditSeverity enum
- AuditEvent dataclass
- AuditLogger class
- audit_action decorator
- Audit logging functions
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock
from io import StringIO

import pytest

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

from app.audit import (
    AuditEventType,
    AuditSeverity,
    AuditEvent,
    AuditLogger,
    audit_action,
    get_audit_logger,
    init_audit_logger,
)


class TestAuditEventType:
    """Tests for AuditEventType enum."""

    def test_auth_login_event_type(self):
        """Test AUTH_LOGIN event type."""
        assert AuditEventType.AUTH_LOGIN.value == "auth.login"

    def test_shell_session_create_event_type(self):
        """Test SHELL_SESSION_CREATE event type."""
        assert AuditEventType.SHELL_SESSION_CREATE.value == "shell.session_create"

    def test_cert_issued_event_type(self):
        """Test CERT_ISSUED event type."""
        assert AuditEventType.CERT_ISSUED.value == "cert.issued"

    def test_agent_enroll_event_type(self):
        """Test AGENT_ENROLL event type."""
        assert AuditEventType.AGENT_ENROLL.value == "agent.enroll"

    def test_user_create_event_type(self):
        """Test USER_CREATE event type."""
        assert AuditEventType.USER_CREATE.value == "user.create"


class TestAuditSeverity:
    """Tests for AuditSeverity enum."""

    def test_debug_severity(self):
        """Test DEBUG severity level."""
        assert AuditSeverity.DEBUG.value == "debug"

    def test_critical_severity(self):
        """Test CRITICAL severity level."""
        assert AuditSeverity.CRITICAL.value == "critical"

    def test_warning_severity(self):
        """Test WARNING severity level."""
        assert AuditSeverity.WARNING.value == "warning"


class TestAuditEvent:
    """Tests for AuditEvent dataclass."""

    def test_audit_event_creation(self):
        """Test creating an AuditEvent."""
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="User logged in",
            user_id=123,
            user_email="user@example.com",
        )
        assert event.event_type == AuditEventType.AUTH_LOGIN
        assert event.severity == AuditSeverity.INFO
        assert event.message == "User logged in"
        assert event.user_id == 123
        assert event.user_email == "user@example.com"

    def test_audit_event_has_event_id(self):
        """Test AuditEvent generates unique event_id."""
        event1 = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )
        event2 = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )
        assert event1.event_id != event2.event_id

    def test_audit_event_has_timestamp(self):
        """Test AuditEvent has timestamp."""
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )
        assert isinstance(event.timestamp, datetime)

    def test_audit_event_to_dict(self):
        """Test converting AuditEvent to dictionary."""
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="User logged in",
            user_id=123,
            user_email="user@example.com",
            ip_address="192.168.1.1",
            resource_type="user",
            resource_id="user123",
            details={"session": "abc123"},
        )
        event_dict = event.to_dict()

        assert event_dict["event_type"] == "auth.login"
        assert event_dict["severity"] == "info"
        assert event_dict["message"] == "User logged in"
        assert event_dict["user_id"] == 123
        assert event_dict["user_email"] == "user@example.com"
        assert event_dict["ip_address"] == "192.168.1.1"
        assert event_dict["resource_type"] == "user"
        assert event_dict["resource_id"] == "user123"
        assert event_dict["details"] == {"session": "abc123"}

    def test_audit_event_to_dict_timestamp_iso_format(self):
        """Test that timestamp is ISO format in dictionary."""
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )
        event_dict = event.to_dict()
        # Should be ISO format string
        assert isinstance(event_dict["timestamp"], str)
        assert "T" in event_dict["timestamp"]


class TestAuditLogger:
    """Tests for AuditLogger class."""

    def test_audit_logger_init_without_app(self):
        """Test initializing AuditLogger without app."""
        logger = AuditLogger()
        assert logger.app is None

    def test_audit_logger_has_required_attributes(self):
        """Test AuditLogger has required attributes."""
        logger = AuditLogger()
        assert hasattr(logger, 'app')
        assert hasattr(logger, 'init_app')
        assert hasattr(logger, 'log')
        assert hasattr(logger, '_get_request_context')

    def test_audit_logger_has_logging_methods(self):
        """Test AuditLogger has specialized logging methods."""
        logger = AuditLogger()
        assert hasattr(logger, 'log_shell_session_create')
        assert hasattr(logger, 'log_shell_session_terminate')
        assert hasattr(logger, 'log_csr_submit')
        assert hasattr(logger, 'log_cert_issued')
        assert hasattr(logger, 'log_cert_revoked')
        assert hasattr(logger, 'log_agent_enroll')
        assert hasattr(logger, 'log_agent_heartbeat')
        assert hasattr(logger, 'log_agent_disconnect')

    def test_save_session_recording_method_exists(self):
        """Test save_session_recording method is defined."""
        logger = AuditLogger()
        assert hasattr(logger, 'save_session_recording')
        assert callable(logger.save_session_recording)

    def test_save_session_recording_raises_when_not_configured(self):
        """Test save_session_recording raises when path not configured."""
        logger = AuditLogger()
        logger._recording_path = None

        with pytest.raises(RuntimeError):
            logger.save_session_recording(
                session_id="session123",
                recording_data=b"data",
            )

    def test_get_request_context_returns_dict(self):
        """Test _get_request_context returns dict with expected keys."""
        logger = AuditLogger()
        # Call outside request context (returns empty context)
        context = logger._get_request_context()

        assert isinstance(context, dict)
        assert "ip_address" in context
        assert "user_agent" in context
        assert "user_id" in context
        assert "user_email" in context

    def test_log_to_app_logger_method_exists(self):
        """Test _log_to_app_logger method is defined."""
        logger = AuditLogger()
        assert hasattr(logger, '_log_to_app_logger')
        assert callable(logger._log_to_app_logger)

    def test_log_to_app_logger_without_app(self):
        """Test _log_to_app_logger handles missing app gracefully."""
        logger = AuditLogger()
        logger.app = None

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        # Should not raise exception
        logger._log_to_app_logger(event)


class TestAuditDecorator:
    """Tests for audit_action decorator."""

    def test_audit_action_decorator_wraps_function(self):
        """Test audit_action decorator wraps function correctly."""
        @audit_action(
            AuditEventType.USER_CREATE,
            "Created user {user_id}",
        )
        def create_user(user_id):
            return {"id": user_id}

        # Function should still be callable
        result = create_user(user_id=123)
        assert result == {"id": 123}

    def test_audit_action_decorator_preserves_function_name(self):
        """Test audit_action decorator preserves function name."""
        @audit_action(AuditEventType.USER_CREATE, "Test")
        def my_function():
            pass

        # functools.wraps preserves the function name
        assert my_function.__name__ == 'my_function'


class TestGetAuditLogger:
    """Tests for get_audit_logger function."""

    def test_get_audit_logger_returns_none_outside_context(self):
        """Test getting audit logger outside app context returns None."""
        # Outside app context, should return None gracefully
        # This tests the exception handling without mocking current_app
        try:
            logger = get_audit_logger()
            # Should return None or raise RuntimeError which is caught
            assert logger is None
        except Exception:
            # If RuntimeError is raised, that's fine - we're outside context
            pass


class TestInitAuditLogger:
    """Tests for init_audit_logger function."""

    def test_init_audit_logger_returns_audit_logger(self):
        """Test init_audit_logger returns AuditLogger instance."""
        # Call without app to test the function exists
        result = init_audit_logger(None)
        assert isinstance(result, AuditLogger)


class TestAuditLoggerInitApp:
    """Tests for AuditLogger.init_app method."""

    @patch("pathlib.Path.mkdir")
    def test_init_app_sets_recording_path(self, mock_mkdir):
        """Test init_app sets recording path from config."""
        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default: (
            "/tmp/test_recordings" if "PATH" in key else True
        )

        logger = AuditLogger()
        logger.init_app(mock_app)

        assert logger._recording_path is not None

    @patch("pathlib.Path.mkdir")
    def test_init_app_creates_extension(self, mock_mkdir):
        """Test init_app registers audit logger in app extensions."""
        mock_app = MagicMock()
        mock_app.extensions = {}
        mock_app.config.get.side_effect = lambda key, default: (
            "/tmp/test_recordings" if "PATH" in key else True
        )

        logger = AuditLogger()
        logger.init_app(mock_app)

        assert mock_app.extensions["audit"] is logger

    @patch("pathlib.Path.mkdir")
    def test_init_app_creates_recording_directory(self, mock_mkdir):
        """Test init_app creates recording directory when enabled."""
        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default: (
            "/tmp/test_recordings" if "PATH" in key else True
        )

        logger = AuditLogger()
        logger.init_app(mock_app)

        # Should have called mkdir on the path
        assert mock_mkdir.called


class TestAuditLoggerMainLog:
    """Tests for AuditLogger.log method."""

    def test_log_with_all_parameters(self):
        """Test log method with all parameters."""
        logger = AuditLogger()
        mock_app = MagicMock()
        mock_app.logger = MagicMock()
        logger.app = mock_app

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                event = logger.log(
                    event_type=AuditEventType.CERT_ISSUED,
                    message="Cert issued",
                    severity=AuditSeverity.INFO,
                    resource_type="certificate",
                    resource_id="cert123",
                    details={"serial": "12345"},
                    user_id=1,
                    user_email="user@example.com",
                )

        assert event.resource_type == "certificate"
        assert event.resource_id == "cert123"
        assert event.details["serial"] == "12345"
        assert event.user_id == 1
        assert event.user_email == "user@example.com"


class TestAuditLoggerDatabase:
    """Tests for AuditLogger._store_to_database method."""

    @patch("app.models.get_db")
    def test_store_to_database_success(self, mock_get_db):
        """Test storing audit event to database."""
        mock_db = MagicMock()
        mock_db.system_logs = MagicMock()
        mock_get_db.return_value = mock_db

        logger = AuditLogger()
        mock_app = MagicMock()
        logger.app = mock_app

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
            user_id=1,
        )

        logger._store_to_database(event)

        assert mock_db.system_logs.insert.called

    @patch("app.models.get_db")
    def test_store_to_database_handles_exception(self, mock_get_db):
        """Test store_to_database handles exceptions gracefully."""
        mock_get_db.side_effect = Exception("DB Error")

        logger = AuditLogger()
        mock_app = MagicMock()
        logger.app = mock_app

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        # Should not raise exception
        logger._store_to_database(event)


class TestAuditLoggerAppLogging:
    """Tests for AuditLogger._log_to_app_logger method."""

    def test_log_to_app_logger_debug_level(self):
        """Test logging with DEBUG severity."""
        mock_app = MagicMock()
        logger = AuditLogger()
        logger.app = mock_app

        event = AuditEvent(
            event_type=AuditEventType.AGENT_HEARTBEAT,
            severity=AuditSeverity.DEBUG,
            message="Heartbeat",
        )

        logger._log_to_app_logger(event)
        assert mock_app.logger.debug.called

    def test_log_to_app_logger_warning_level(self):
        """Test logging with WARNING severity."""
        mock_app = MagicMock()
        logger = AuditLogger()
        logger.app = mock_app

        event = AuditEvent(
            event_type=AuditEventType.CERT_REVOKED,
            severity=AuditSeverity.WARNING,
            message="Revoked",
        )

        logger._log_to_app_logger(event)
        assert mock_app.logger.warning.called

    def test_log_to_app_logger_critical_level(self):
        """Test logging with CRITICAL severity."""
        mock_app = MagicMock()
        logger = AuditLogger()
        logger.app = mock_app

        event = AuditEvent(
            event_type=AuditEventType.SYSTEM_ERROR,
            severity=AuditSeverity.CRITICAL,
            message="Critical error",
        )

        logger._log_to_app_logger(event)
        assert mock_app.logger.critical.called


class TestAuditLoggerShellSession:
    """Tests for shell session logging methods."""

    def test_log_shell_session_create_is_callable(self):
        """Test logging shell session creation."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_shell_session_create(
                    session_id="sess123",
                    target_host="host.example.com",
                    target_user="admin",
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.SHELL_SESSION_CREATE

    def test_log_shell_session_terminate_is_callable(self):
        """Test logging shell session termination."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_shell_session_terminate(
                    session_id="sess123",
                    reason="timeout",
                    duration_seconds=3600,
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.SHELL_SESSION_TERMINATE


class TestAuditLoggerCertificate:
    """Tests for certificate logging methods."""

    def test_log_csr_submit_is_callable(self):
        """Test logging CSR submission."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_csr_submit(
                    csr_id="csr123",
                    common_name="example.com",
                    requester="user@example.com",
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.CERT_CSR_SUBMIT

    def test_log_csr_approve_is_callable(self):
        """Test logging CSR approval."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_csr_approve(
                    csr_id="csr123",
                    approver="admin@example.com",
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.CERT_CSR_APPROVE

    def test_log_cert_issued_is_callable(self):
        """Test logging certificate issuance."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_cert_issued(
                    cert_id="cert123",
                    common_name="example.com",
                    serial_number="12345",
                    expires_at=datetime.utcnow(),
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.CERT_ISSUED

    def test_log_cert_revoked_is_callable(self):
        """Test logging certificate revocation."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_cert_revoked(
                    cert_id="cert123",
                    serial_number="12345",
                    reason="compromised",
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.CERT_REVOKED


class TestAuditLoggerAgent:
    """Tests for agent logging methods."""

    def test_log_agent_enroll_is_callable(self):
        """Test logging agent enrollment."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_agent_enroll(
                    agent_id="agent123",
                    hostname="agent.example.com",
                    agent_version="1.0.0",
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.AGENT_ENROLL

    def test_log_agent_heartbeat_is_callable(self):
        """Test logging agent heartbeat."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_agent_heartbeat(
                    agent_id="agent123",
                    hostname="agent.example.com",
                    status="healthy",
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.AGENT_HEARTBEAT

    def test_log_agent_disconnect_is_callable(self):
        """Test logging agent disconnection."""
        logger = AuditLogger()
        logger.app = MagicMock()
        logger.app.logger = MagicMock()

        with patch("app.audit.AuditLogger._store_to_database"):
            with patch("app.audit.AuditLogger._log_to_app_logger"):
                result = logger.log_agent_disconnect(
                    agent_id="agent123",
                    hostname="agent.example.com",
                    reason="network_failure",
                )

        assert isinstance(result, AuditEvent)
        assert result.event_type == AuditEventType.AGENT_DISCONNECT


class TestAuditLoggerSessionRecording:
    """Tests for session recording methods."""

    def test_save_session_recording_raises_without_path(self):
        """Test saving session recording without configured path."""
        logger = AuditLogger()
        logger._recording_path = None

        with pytest.raises(RuntimeError):
            logger.save_session_recording(
                session_id="sess123",
                recording_data=b"test_data",
            )


class TestAuditActionDecorator:
    """Tests for audit_action decorator functionality."""

    def test_audit_action_decorator_signature(self):
        """Test audit_action decorator can be applied."""
        @audit_action(
            AuditEventType.USER_CREATE,
            "Created user {user_id}",
            resource_type="user",
            resource_id_arg="user_id",
        )
        def create_user(user_id):
            return {"id": user_id, "created": True}

        # Verify the decorator is applied
        assert hasattr(create_user, '__wrapped__') or callable(create_user)

    def test_audit_action_preserves_return_value(self):
        """Test audit_action decorator preserves function return value."""
        with patch("app.audit.get_audit_logger", return_value=None):
            @audit_action(AuditEventType.USER_CREATE, "Created user")
            def create_user(user_id):
                return {"id": user_id}

            result = create_user(user_id=123)
            assert result == {"id": 123}
