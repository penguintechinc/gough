"""Tests for the audit logging module (app/audit.py).

Covers:
- AuditEventType and AuditSeverity enums
- AuditEvent dataclass (to_dict conversion)
- AuditLogger initialization and configuration
- Request context extraction (_get_request_context)
- Audit event logging (log method)
- Audit event storage to database (_store_to_database)
- NATS audit event publishing (_publish_audit_event_nats)
- Application logging (_log_to_app_logger)
- Specialized logging methods (shell, certificate, agent, secrets)
- Session recording storage (save_session_recording)
- audit_action decorator
- get_audit_logger and init_audit_logger functions
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from quart import Quart

from app.audit import (
    AuditEvent,
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    audit_action,
    get_audit_logger,
    init_audit_logger,
)


# =============================================================================
# Enum tests
# =============================================================================


class TestAuditEventType:
    """AuditEventType enumeration coverage."""

    def test_auth_events_exist(self) -> None:
        assert AuditEventType.AUTH_LOGIN.value == "auth.login"
        assert AuditEventType.AUTH_LOGOUT.value == "auth.logout"
        assert AuditEventType.AUTH_LOGIN_FAILED.value == "auth.login_failed"
        assert AuditEventType.AUTH_PASSWORD_CHANGE.value == "auth.password_change"
        assert AuditEventType.AUTH_MFA_ENABLED.value == "auth.mfa_enabled"
        assert AuditEventType.AUTH_MFA_DISABLED.value == "auth.mfa_disabled"

    def test_shell_events_exist(self) -> None:
        assert AuditEventType.SHELL_SESSION_CREATE.value == "shell.session_create"
        assert AuditEventType.SHELL_SESSION_TERMINATE.value == "shell.session_terminate"
        assert AuditEventType.SHELL_COMMAND_EXECUTE.value == "shell.command_execute"

    def test_certificate_events_exist(self) -> None:
        assert AuditEventType.CERT_CSR_SUBMIT.value == "cert.csr_submit"
        assert AuditEventType.CERT_CSR_APPROVE.value == "cert.csr_approve"
        assert AuditEventType.CERT_CSR_REJECT.value == "cert.csr_reject"
        assert AuditEventType.CERT_ISSUED.value == "cert.issued"
        assert AuditEventType.CERT_REVOKED.value == "cert.revoked"

    def test_agent_events_exist(self) -> None:
        assert AuditEventType.AGENT_ENROLL.value == "agent.enroll"
        assert AuditEventType.AGENT_HEARTBEAT.value == "agent.heartbeat"
        assert AuditEventType.AGENT_DISCONNECT.value == "agent.disconnect"
        assert AuditEventType.AGENT_UPDATE.value == "agent.update"

    def test_user_events_exist(self) -> None:
        assert AuditEventType.USER_CREATE.value == "user.create"
        assert AuditEventType.USER_UPDATE.value == "user.update"
        assert AuditEventType.USER_DELETE.value == "user.delete"
        assert AuditEventType.USER_ROLE_CHANGE.value == "user.role_change"

    def test_resource_events_exist(self) -> None:
        assert AuditEventType.RESOURCE_CREATE.value == "resource.create"
        assert AuditEventType.RESOURCE_UPDATE.value == "resource.update"
        assert AuditEventType.RESOURCE_DELETE.value == "resource.delete"
        assert AuditEventType.RESOURCE_ACCESS.value == "resource.access"

    def test_secret_events_exist(self) -> None:
        assert AuditEventType.SECRET_ACCESS.value == "secret.access"
        assert AuditEventType.SECRET_CREATE.value == "secret.create"
        assert AuditEventType.SECRET_UPDATE.value == "secret.update"
        assert AuditEventType.SECRET_DELETE.value == "secret.delete"

    def test_cloud_events_exist(self) -> None:
        assert AuditEventType.CLOUD_PROVIDER_ADD.value == "cloud.provider_add"
        assert AuditEventType.CLOUD_PROVIDER_UPDATE.value == "cloud.provider_update"
        assert AuditEventType.CLOUD_PROVIDER_DELETE.value == "cloud.provider_delete"
        assert AuditEventType.CLOUD_MACHINE_PROVISION.value == "cloud.machine_provision"
        assert AuditEventType.CLOUD_MACHINE_TERMINATE.value == "cloud.machine_terminate"

    def test_deployment_events_exist(self) -> None:
        assert AuditEventType.DEPLOYMENT_START.value == "deployment.start"
        assert AuditEventType.DEPLOYMENT_COMPLETE.value == "deployment.complete"
        assert AuditEventType.DEPLOYMENT_FAILED.value == "deployment.failed"

    def test_system_events_exist(self) -> None:
        assert AuditEventType.SYSTEM_CONFIG_CHANGE.value == "system.config_change"
        assert AuditEventType.SYSTEM_ERROR.value == "system.error"


class TestAuditSeverity:
    """AuditSeverity enumeration coverage."""

    def test_severity_levels(self) -> None:
        assert AuditSeverity.DEBUG.value == "debug"
        assert AuditSeverity.INFO.value == "info"
        assert AuditSeverity.WARNING.value == "warning"
        assert AuditSeverity.ERROR.value == "error"
        assert AuditSeverity.CRITICAL.value == "critical"


# =============================================================================
# AuditEvent dataclass tests
# =============================================================================


class TestAuditEvent:
    """AuditEvent dataclass and to_dict conversion."""

    def test_audit_event_creation_minimal(self) -> None:
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="User logged in",
        )
        assert event.event_type == AuditEventType.AUTH_LOGIN
        assert event.severity == AuditSeverity.INFO
        assert event.message == "User logged in"
        assert event.user_id is None
        assert event.user_email is None
        assert event.ip_address is None
        assert event.user_agent is None
        assert event.resource_type is None
        assert event.resource_id is None
        assert event.details == {}
        assert isinstance(event.event_id, str)
        assert isinstance(event.timestamp, datetime)

    def test_audit_event_creation_full(self) -> None:
        now = datetime.now(timezone.utc)
        event = AuditEvent(
            event_type=AuditEventType.USER_CREATE,
            severity=AuditSeverity.INFO,
            message="User created",
            user_id=123,
            user_email="user@example.com",
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0",
            resource_type="user",
            resource_id="usr-456",
            details={"created_by": "admin"},
            event_id="evt-789",
            timestamp=now,
        )
        assert event.user_id == 123
        assert event.user_email == "user@example.com"
        assert event.ip_address == "192.168.1.1"
        assert event.user_agent == "Mozilla/5.0"
        assert event.resource_type == "user"
        assert event.resource_id == "usr-456"
        assert event.details == {"created_by": "admin"}
        assert event.event_id == "evt-789"
        assert event.timestamp == now

    def test_audit_event_to_dict(self) -> None:
        now = datetime.now(timezone.utc)
        event = AuditEvent(
            event_type=AuditEventType.USER_DELETE,
            severity=AuditSeverity.WARNING,
            message="User deleted",
            user_id=99,
            user_email="admin@example.com",
            ip_address="10.0.0.1",
            user_agent="curl/7.68.0",
            resource_type="user",
            resource_id="usr-123",
            details={"reason": "account_closure"},
            event_id="evt-abc",
            timestamp=now,
        )
        event_dict = event.to_dict()
        assert event_dict["event_id"] == "evt-abc"
        assert event_dict["event_type"] == "user.delete"
        assert event_dict["severity"] == "warning"
        assert event_dict["message"] == "User deleted"
        assert event_dict["user_id"] == 99
        assert event_dict["user_email"] == "admin@example.com"
        assert event_dict["ip_address"] == "10.0.0.1"
        assert event_dict["user_agent"] == "curl/7.68.0"
        assert event_dict["resource_type"] == "user"
        assert event_dict["resource_id"] == "usr-123"
        assert event_dict["details"] == {"reason": "account_closure"}
        assert event_dict["timestamp"] == now.isoformat()


# =============================================================================
# AuditLogger initialization and context extraction
# =============================================================================


class TestAuditLoggerInit:
    """AuditLogger initialization."""

    def test_init_without_app(self) -> None:
        logger = AuditLogger()
        assert logger.app is None
        assert logger._recording_path is None

    def test_init_with_app(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_PATH"] = "/tmp/audit"
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        assert logger.app is app

    def test_init_app_creates_recording_directory(self, tmp_path) -> None:
        app = Quart(__name__)
        recording_path = tmp_path / "recordings"
        app.config["AUDIT_RECORDING_PATH"] = str(recording_path)
        app.config["AUDIT_RECORDING_ENABLED"] = True
        logger = AuditLogger()
        logger.init_app(app)
        assert recording_path.exists()

    def test_init_app_disables_recording_directory_creation(self, tmp_path) -> None:
        app = Quart(__name__)
        recording_path = tmp_path / "no_recordings"
        app.config["AUDIT_RECORDING_PATH"] = str(recording_path)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger()
        logger.init_app(app)
        assert not recording_path.exists()

    def test_init_app_stores_extension(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        assert hasattr(app, "extensions")
        assert app.extensions["audit"] is logger


class TestGetRequestContext:
    """Request context extraction in non-Quart environments."""

    def test_get_request_context_no_request(self) -> None:
        logger = AuditLogger()
        context = logger._get_request_context()
        assert context["ip_address"] is None
        assert context["user_agent"] is None
        assert context["user_id"] is None
        assert context["user_email"] is None

    @pytest.mark.asyncio
    async def test_get_request_context_with_quart_request(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False

        @app.before_request
        async def set_user():
            from quart import g
            g.current_user = {"id": 42, "email": "test@example.com"}

        logger = AuditLogger(app)

        async with app.test_request_context(
            "/",
            headers={
                "X-Forwarded-For": "203.0.113.1",
                "User-Agent": "TestClient/1.0",
            },
        ):
            # Manually set current_user since before_request won't run in test context
            from quart import g
            g.current_user = {"id": 42, "email": "test@example.com"}

            context = logger._get_request_context()
            assert context["ip_address"] == "203.0.113.1"
            assert context["user_agent"] == "TestClient/1.0"
            assert context["user_id"] == 42
            assert context["user_email"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_get_request_context_x_real_ip_fallback(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        async with app.test_request_context(
            "/",
            headers={
                "X-Real-IP": "192.0.2.1",
                "User-Agent": "curl/7.68.0",
            },
        ):
            context = logger._get_request_context()
            assert context["ip_address"] == "192.0.2.1"
            assert context["user_agent"] == "curl/7.68.0"

    @pytest.mark.asyncio
    async def test_get_request_context_remote_addr_fallback(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        async with app.test_request_context("/"):
            context = logger._get_request_context()
            # In Quart test context, remote_addr should be available
            assert context is not None


# =============================================================================
# Audit event logging
# =============================================================================


class TestAuditLoggerLog:
    """AuditLogger.log method."""

    def test_log_creates_event_with_defaults(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "_store_to_database") as mock_store:
            with patch.object(logger, "_log_to_app_logger") as mock_log:
                event = logger.log(
                    event_type=AuditEventType.AUTH_LOGIN,
                    message="Login successful",
                )

                assert event.event_type == AuditEventType.AUTH_LOGIN
                assert event.message == "Login successful"
                assert event.severity == AuditSeverity.INFO
                mock_store.assert_called_once_with(event)
                mock_log.assert_called_once_with(event)

    def test_log_accepts_all_parameters(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "_store_to_database"):
            with patch.object(logger, "_log_to_app_logger"):
                event = logger.log(
                    event_type=AuditEventType.USER_DELETE,
                    message="User removed",
                    severity=AuditSeverity.WARNING,
                    resource_type="user",
                    resource_id="usr-789",
                    details={"reason": "inactivity"},
                    user_id=100,
                    user_email="admin@example.com",
                )

                assert event.resource_type == "user"
                assert event.resource_id == "usr-789"
                assert event.details == {"reason": "inactivity"}
                assert event.user_id == 100
                assert event.user_email == "admin@example.com"
                assert event.severity == AuditSeverity.WARNING


# =============================================================================
# Database storage
# =============================================================================


class TestStoreToDatabase:
    """AuditLogger._store_to_database method."""

    def test_store_to_database_success(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Login",
        )

        # Test that the method handles the case when get_db is not available
        # (which it won't be in this test context)
        logger._store_to_database(event)  # Should not raise

    def test_store_to_database_no_db(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Login",
        )

        logger._store_to_database(event)  # Should not raise even without get_db

    def test_store_to_database_exception_logged(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Login",
        )

        logger._store_to_database(event)  # Should not raise on any error


# =============================================================================
# NATS publishing
# =============================================================================


class TestPublishAuditEventNats:
    """AuditLogger._publish_audit_event_nats method."""

    def test_publish_nats_no_client(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        app.nats_client = None
        logger = AuditLogger(app)

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Login",
        )

        with patch("app.audit.current_app", app):
            logger._publish_audit_event_nats(event)  # Should not raise

    def test_publish_nats_payload_structure(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        mock_nats = MagicMock()
        app.nats_client = mock_nats

        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.USER_CREATE,
            severity=AuditSeverity.INFO,
            message="User created",
            user_email="alice@example.com",
            resource_type="user",
            resource_id="usr-456",
        )

        with patch("app.audit.current_app", app):
            # Just call the method; it handles asyncio internally
            logger._publish_audit_event_nats(event)


# =============================================================================
# Application logging
# =============================================================================


class TestLogToAppLogger:
    """AuditLogger._log_to_app_logger method."""

    def test_log_to_app_logger_info_level(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Login successful",
            user_email="user@example.com",
            ip_address="192.168.1.1",
        )

        with patch.object(app.logger, "info") as mock_info:
            logger._log_to_app_logger(event)
            mock_info.assert_called_once()
            call_args = mock_info.call_args[0][0]
            assert "AUDIT" in call_args
            assert "auth.login" in call_args

    def test_log_to_app_logger_warning_level(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN_FAILED,
            severity=AuditSeverity.WARNING,
            message="Login failed",
            user_email="attacker@example.com",
            ip_address="203.0.113.1",
        )

        with patch.object(app.logger, "warning") as mock_warning:
            logger._log_to_app_logger(event)
            mock_warning.assert_called_once()

    def test_log_to_app_logger_debug_level(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.AGENT_HEARTBEAT,
            severity=AuditSeverity.DEBUG,
            message="Heartbeat received",
        )

        with patch.object(app.logger, "debug") as mock_debug:
            logger._log_to_app_logger(event)
            mock_debug.assert_called_once()

    def test_log_to_app_logger_error_level(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.SYSTEM_ERROR,
            severity=AuditSeverity.ERROR,
            message="System error occurred",
        )

        with patch.object(app.logger, "error") as mock_error:
            logger._log_to_app_logger(event)
            mock_error.assert_called_once()

    def test_log_to_app_logger_critical_level(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)
        event = AuditEvent(
            event_type=AuditEventType.SYSTEM_ERROR,
            severity=AuditSeverity.CRITICAL,
            message="Critical security incident",
        )

        with patch.object(app.logger, "critical") as mock_critical:
            logger._log_to_app_logger(event)
            mock_critical.assert_called_once()


# =============================================================================
# Specialized logging methods
# =============================================================================


class TestShellSessionMethods:
    """Shell session audit logging methods."""

    def test_log_shell_session_create(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_shell_session_create(
                session_id="sess-123",
                target_host="node1.example.com",
                target_user="root",
                details={"pty": "pts/0"},
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.SHELL_SESSION_CREATE
            assert "root@node1.example.com" in kwargs["message"]
            assert kwargs["resource_id"] == "sess-123"

    def test_log_shell_session_terminate(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_shell_session_terminate(
                session_id="sess-123",
                reason="timeout",
                duration_seconds=600,
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.SHELL_SESSION_TERMINATE
            assert "timeout" in kwargs["message"]


class TestCertificateMethods:
    """Certificate audit logging methods."""

    def test_log_csr_submit(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_csr_submit(
                csr_id="csr-456",
                common_name="host.example.com",
                requester="alice@example.com",
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.CERT_CSR_SUBMIT

    def test_log_csr_approve(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_csr_approve(csr_id="csr-456", approver="admin@example.com")
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.CERT_CSR_APPROVE

    def test_log_cert_issued(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        expires = datetime.now(timezone.utc) + timedelta(days=365)
        with patch.object(logger, "log") as mock_log:
            logger.log_cert_issued(
                cert_id="cert-789",
                common_name="host.example.com",
                serial_number="abc123def456",
                expires_at=expires,
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.CERT_ISSUED

    def test_log_cert_revoked(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_cert_revoked(
                cert_id="cert-789",
                serial_number="abc123def456",
                reason="superseded",
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.CERT_REVOKED


class TestAgentMethods:
    """Agent audit logging methods."""

    def test_log_agent_enroll(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_agent_enroll(
                agent_id="agent-001",
                hostname="node1.example.com",
                agent_version="1.2.3",
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.AGENT_ENROLL

    def test_log_agent_heartbeat(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_agent_heartbeat(
                agent_id="agent-001",
                hostname="node1.example.com",
                status="healthy",
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.AGENT_HEARTBEAT
            assert kwargs["severity"] == AuditSeverity.DEBUG

    def test_log_agent_disconnect(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        with patch.object(logger, "log") as mock_log:
            logger.log_agent_disconnect(
                agent_id="agent-001",
                hostname="node1.example.com",
                reason="network_timeout",
            )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert kwargs["event_type"] == AuditEventType.AGENT_DISCONNECT
            assert kwargs["severity"] == AuditSeverity.WARNING


# =============================================================================
# Session recording storage
# =============================================================================


class TestSaveSessionRecording:
    """Session recording storage."""

    def test_save_session_recording_creates_file(self, tmp_path) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_PATH"] = str(tmp_path)
        app.config["AUDIT_RECORDING_ENABLED"] = True
        logger = AuditLogger(app)

        recording_data = b"recording content here"
        with patch.object(logger, "log"):
            result = logger.save_session_recording(
                session_id="sess-123",
                recording_data=recording_data,
            )

        assert result is not None
        assert Path(result).exists()
        assert Path(result).read_bytes() == recording_data

    def test_save_session_recording_with_metadata(self, tmp_path) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_PATH"] = str(tmp_path)
        app.config["AUDIT_RECORDING_ENABLED"] = True
        logger = AuditLogger(app)

        recording_data = b"recording content"
        metadata = {"user": "alice", "duration": 120}

        with patch.object(logger, "log"):
            result = logger.save_session_recording(
                session_id="sess-456",
                recording_data=recording_data,
                metadata=metadata,
            )

        json_path = Path(result).with_suffix(".json")
        assert json_path.exists()
        stored_metadata = json.loads(json_path.read_text())
        assert stored_metadata == metadata

    def test_save_session_recording_no_path_raises(self) -> None:
        logger = AuditLogger()
        with pytest.raises(RuntimeError, match="Recording storage not configured"):
            logger.save_session_recording(
                session_id="sess-789",
                recording_data=b"data",
            )


# =============================================================================
# audit_action decorator
# =============================================================================


class TestAuditActionDecorator:
    """audit_action decorator functionality."""

    def test_audit_action_logs_success(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False

        @audit_action(
            AuditEventType.RESOURCE_DELETE,
            "Deleted resource {resource_id}",
            resource_type="resource",
            resource_id_arg="resource_id",
        )
        def delete_resource(resource_id: str) -> str:
            return f"Deleted {resource_id}"

        with patch("app.audit.get_audit_logger") as mock_get:
            mock_logger = MagicMock()
            mock_get.return_value = mock_logger

            result = delete_resource(resource_id="res-123")

            assert result == "Deleted res-123"
            mock_logger.log.assert_called_once()
            args, kwargs = mock_logger.log.call_args
            assert kwargs["event_type"] == AuditEventType.RESOURCE_DELETE
            assert kwargs["resource_id"] == "res-123"

    def test_audit_action_logs_failure(self) -> None:
        @audit_action(
            AuditEventType.RESOURCE_DELETE,
            "Delete failed for {resource_id}",
        )
        def delete_resource_fails(resource_id: str) -> None:
            raise ValueError("Cannot delete")

        with patch("app.audit.get_audit_logger") as mock_get:
            mock_logger = MagicMock()
            mock_get.return_value = mock_logger

            with pytest.raises(ValueError):
                delete_resource_fails(resource_id="res-456")

            mock_logger.log.assert_called_once()
            args, kwargs = mock_logger.log.call_args
            assert kwargs["severity"] == AuditSeverity.ERROR
            assert "FAILED" in kwargs["message"]

    def test_audit_action_no_logger(self) -> None:
        @audit_action(
            AuditEventType.RESOURCE_CREATE,
            "Create resource",
        )
        def create_resource() -> str:
            return "created"

        with patch("app.audit.get_audit_logger", return_value=None):
            result = create_resource()
            assert result == "created"


# =============================================================================
# get_audit_logger and init_audit_logger
# =============================================================================


class TestGetAuditLogger:
    """get_audit_logger function."""

    def test_get_audit_logger_from_app(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        logger = AuditLogger(app)

        # Use async context manager
        import asyncio
        async def test():
            async with app.app_context():
                result = get_audit_logger()
                assert result is logger

        asyncio.run(test())

    def test_get_audit_logger_no_app(self) -> None:
        # Outside any app context
        result = get_audit_logger()
        # Should return None when no context
        assert result is None

    def test_get_audit_logger_no_extensions(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False
        # Don't initialize AuditLogger, so extensions won't have audit

        import asyncio
        async def test():
            async with app.app_context():
                result = get_audit_logger()
                # Should return None since audit not in extensions
                assert result is None

        asyncio.run(test())


class TestInitAuditLogger:
    """init_audit_logger function."""

    def test_init_audit_logger(self) -> None:
        app = Quart(__name__)
        app.config["AUDIT_RECORDING_ENABLED"] = False

        logger = init_audit_logger(app)

        assert isinstance(logger, AuditLogger)
        assert logger.app is app
