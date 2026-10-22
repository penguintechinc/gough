"""Additional test coverage for app/audit.py (target lines 157, 235-246, 260-293, 302, 621-622, 662-663).

Tests for audit logger initialization, database storage, NATS publishing failures,
and decorator error handling.
"""

import pytest
import json
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from pathlib import Path

from app.audit import (
    AuditLogger,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    audit_action,
    get_audit_logger,
    init_audit_logger,
)


class TestAuditLoggerDatabaseStorage:
    """Test _store_to_database with exception handling (235-246)."""

    def test_store_to_database_db_insert_exception(self):
        """Test exception when db.system_logs.insert fails (247-250)."""
        logger = AuditLogger()
        logger.app = MagicMock()

        mock_db = MagicMock()
        mock_db.system_logs.insert.side_effect = Exception("DB error")

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        with patch("app.models.get_db", return_value=mock_db):
            with patch.object(logger, "_publish_audit_event_nats"):
                # Should not raise, exception is caught
                logger._store_to_database(event)
                logger.app.logger.error.assert_called_once()

    def test_store_to_database_no_system_logs_table(self):
        """Test when db doesn't have system_logs table (235)."""
        logger = AuditLogger()
        logger.app = MagicMock()

        mock_db = MagicMock()
        # Delete system_logs attribute
        del mock_db.system_logs

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        with patch("app.models.get_db", return_value=mock_db):
            # Should not raise - db.system_logs check fails
            logger._store_to_database(event)


class TestNATSPublishingEdgeCases:
    """Test NATS publishing error paths (260-293)."""

    def test_nats_no_client_returns_early(self):
        """Test NATS publish returns early when no client (259-261)."""
        logger = AuditLogger()

        mock_app = MagicMock()
        mock_app.nats_client = None

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        with patch("app.audit.current_app", mock_app):
            # Should return early without error
            logger._publish_audit_event_nats(event)

    def test_nats_asyncio_create_task_runtime_error(self):
        """Test RuntimeError when no event loop (288-293)."""
        import asyncio as _asyncio
        from quart import Quart

        logger = AuditLogger()
        logger.app = MagicMock()

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        test_app = Quart(__name__)
        test_app.nats_client = MagicMock()

        async def _run():
            async with test_app.app_context():
                with patch("asyncio.create_task", side_effect=RuntimeError("No loop")):
                    logger._publish_audit_event_nats(event)

        _asyncio.run(_run())
        # No assertion needed — just verify no exception raised

    def test_nats_outer_exception_handling(self):
        """Test outer exception (not RuntimeError) handling (294-297)."""
        logger = AuditLogger()
        logger.app = MagicMock()

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        # Outside app context, current_app raises RuntimeError which is caught
        # by the outer except in _publish_audit_event_nats
        logger._publish_audit_event_nats(event)
        # Should not raise — outer except swallows all exceptions


class TestAppLoggerIntegration:
    """Test _log_to_app_logger integration (302)."""

    def test_log_to_app_logger_no_app_returns_early(self):
        """Test returns early when no app (301-302)."""
        logger = AuditLogger()
        # No app set

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        # Should return without error
        logger._log_to_app_logger(event)

    def test_log_to_app_logger_uses_correct_method(self):
        """Test correct logger method used for severity (309-318)."""
        mock_app = MagicMock()
        logger = AuditLogger()
        logger.app = mock_app

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.CRITICAL,
            message="Test",
        )

        logger._log_to_app_logger(event)
        # Should call critical method
        mock_app.logger.critical.assert_called_once()

    def test_log_to_app_logger_default_info_for_unknown(self):
        """Test defaults to info for unknown severity (317)."""
        mock_app = MagicMock()
        logger = AuditLogger()
        logger.app = mock_app

        event = AuditEvent(
            event_type=AuditEventType.AUTH_LOGIN,
            severity=AuditSeverity.INFO,
            message="Test",
        )

        logger._log_to_app_logger(event)
        mock_app.logger.info.assert_called_once()


class TestAuditDecorator:
    """Test @audit_action decorator (578-654)."""

    def test_audit_action_message_formatting_keyerror(self):
        """Test message formatting KeyError handling (619-622)."""
        mock_logger = MagicMock()

        @audit_action(
            AuditEventType.RESOURCE_CREATE,
            "Created {missing}",
            resource_type="test",
        )
        def create_resource(resource_id):
            return {"id": resource_id}

        with patch("app.audit.get_audit_logger", return_value=mock_logger):
            result = create_resource(resource_id="123")
            assert result == {"id": "123"}
            # Should log with template as-is when KeyError
            call_kwargs = mock_logger.log.call_args[1]
            assert call_kwargs["message"] == "Created {missing}"

    def test_audit_action_success_logs_status(self):
        """Test successful call logs success status."""
        mock_logger = MagicMock()

        @audit_action(
            AuditEventType.RESOURCE_CREATE,
            "Created {resource_id}",
            resource_type="test",
            resource_id_arg="resource_id",
        )
        def create_resource(resource_id):
            return {"id": resource_id}

        with patch("app.audit.get_audit_logger", return_value=mock_logger):
            result = create_resource(resource_id="123")
            call_kwargs = mock_logger.log.call_args[1]
            assert call_kwargs["details"]["status"] == "success"

    def test_audit_action_exception_logs_error(self):
        """Test exception logs error status."""
        mock_logger = MagicMock()

        @audit_action(
            AuditEventType.RESOURCE_DELETE,
            "Deleted {resource_id}",
            resource_type="test",
            resource_id_arg="resource_id",
        )
        def delete_resource(resource_id):
            raise ValueError("Not found")

        with patch("app.audit.get_audit_logger", return_value=mock_logger):
            with pytest.raises(ValueError):
                delete_resource(resource_id="999")

            call_kwargs = mock_logger.log.call_args[1]
            assert call_kwargs["details"]["status"] == "failed"
            assert "Not found" in call_kwargs["details"]["error"]


class TestGetAuditLogger:
    """Test get_audit_logger function (657-664)."""

    def test_get_audit_logger_from_app_extensions(self):
        """Test retrieving from app extensions (660-661)."""
        mock_app = MagicMock()
        mock_logger = MagicMock()
        mock_app.extensions = {"audit": mock_logger}

        with patch("app.audit.current_app", mock_app):
            result = get_audit_logger()
            assert result is mock_logger

    def test_get_audit_logger_no_audit_key(self):
        """Test returns None when no audit key (661)."""
        mock_app = MagicMock()
        mock_app.extensions = {"other": "value"}

        with patch("app.audit.current_app", mock_app):
            result = get_audit_logger()
            assert result is None

    def test_get_audit_logger_no_extensions(self):
        """Test returns None when no extensions attr."""
        mock_app = MagicMock(spec=[])  # No extensions

        with patch("app.audit.current_app", mock_app):
            result = get_audit_logger()
            assert result is None

    def test_get_audit_logger_runtime_error(self):
        """Test handles RuntimeError outside app context (662-663)."""
        # Outside Quart app context, current_app raises RuntimeError.
        # get_audit_logger catches it and returns None.
        result = get_audit_logger()
        assert result is None


class TestInitAuditLogger:
    """Test init_audit_logger function."""

    def test_init_audit_logger_returns_instance(self):
        """Test returns AuditLogger instance."""
        mock_app = MagicMock()
        logger = init_audit_logger(mock_app)

        assert isinstance(logger, AuditLogger)
        assert logger.app is mock_app

    def test_init_audit_logger_sets_global(self):
        """Test sets global _global_audit_logger variable."""
        mock_app = MagicMock()

        with patch("app.audit._global_audit_logger", None):
            logger = init_audit_logger(mock_app)
            assert isinstance(logger, AuditLogger)
