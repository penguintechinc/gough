"""Audit Logging Module for Gough Hypervisor Orchestration Platform.

Provides comprehensive audit logging for:
- Shell session create/terminate events
- Certificate signing requests
- Agent enrollment/heartbeat events
- Session recordings storage
- All security-relevant operations
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Callable, Optional

from quart import current_app, g, request


class AuditEventType(Enum):
    """Enumeration of audit event types."""

    # Authentication events
    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_PASSWORD_CHANGE = "auth.password_change"
    AUTH_MFA_ENABLED = "auth.mfa_enabled"
    AUTH_MFA_DISABLED = "auth.mfa_disabled"

    # Shell session events
    SHELL_SESSION_CREATE = "shell.session_create"
    SHELL_SESSION_TERMINATE = "shell.session_terminate"
    SHELL_COMMAND_EXECUTE = "shell.command_execute"

    # Certificate events
    CERT_CSR_SUBMIT = "cert.csr_submit"
    CERT_CSR_APPROVE = "cert.csr_approve"
    CERT_CSR_REJECT = "cert.csr_reject"
    CERT_ISSUED = "cert.issued"
    CERT_REVOKED = "cert.revoked"

    # Agent events
    AGENT_ENROLL = "agent.enroll"
    AGENT_HEARTBEAT = "agent.heartbeat"
    AGENT_DISCONNECT = "agent.disconnect"
    AGENT_UPDATE = "agent.update"

    # User management events
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"
    USER_ROLE_CHANGE = "user.role_change"

    # Resource events
    RESOURCE_CREATE = "resource.create"
    RESOURCE_UPDATE = "resource.update"
    RESOURCE_DELETE = "resource.delete"
    RESOURCE_ACCESS = "resource.access"

    # Secrets management events
    SECRET_ACCESS = "secret.access"
    SECRET_CREATE = "secret.create"
    SECRET_UPDATE = "secret.update"
    SECRET_DELETE = "secret.delete"

    # Cloud provider events
    CLOUD_PROVIDER_ADD = "cloud.provider_add"
    CLOUD_PROVIDER_UPDATE = "cloud.provider_update"
    CLOUD_PROVIDER_DELETE = "cloud.provider_delete"
    CLOUD_MACHINE_PROVISION = "cloud.machine_provision"
    CLOUD_MACHINE_TERMINATE = "cloud.machine_terminate"

    # Deployment events
    DEPLOYMENT_START = "deployment.start"
    DEPLOYMENT_COMPLETE = "deployment.complete"
    DEPLOYMENT_FAILED = "deployment.failed"

    # System events
    SYSTEM_CONFIG_CHANGE = "system.config_change"
    SYSTEM_ERROR = "system.error"


class AuditSeverity(Enum):
    """Severity levels for audit events."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(slots=True)
class AuditEvent:
    """Represents an audit event."""

    event_type: AuditEventType
    severity: AuditSeverity
    message: str
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert audit event to dictionary."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }


class AuditLogger:
    """Audit logging service for Gough platform."""

    def __init__(self, app=None):
        """Initialize audit logger."""
        self.app = app
        self._recording_path = None
        if app is not None:
            self.init_app(app)

    def init_app(self, app) -> None:
        """Initialize audit logger with Quart app."""
        self.app = app

        # Configure recording storage path
        self._recording_path = Path(
            app.config.get("AUDIT_RECORDING_PATH", "/var/gough/recordings")
        )

        # Create recording directory if it doesn't exist
        if app.config.get("AUDIT_RECORDING_ENABLED", True):
            self._recording_path.mkdir(parents=True, exist_ok=True)

        # Store instance in app extensions
        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions["audit"] = self

    def _get_request_context(self) -> dict:
        """Extract request context information."""
        context = {
            "ip_address": None,
            "user_agent": None,
            "user_id": None,
            "user_email": None,
        }

        try:
            if request:
                # Get real IP address (handle proxies)
                x_forwarded = (
                    request.headers.get("X-Forwarded-For", "")
                    .split(",")[0]
                    .strip()
                )
                context["ip_address"] = (
                    x_forwarded
                    or request.headers.get("X-Real-IP")
                    or request.remote_addr
                )
                context["user_agent"] = request.headers.get("User-Agent")

            # Get current user if authenticated
            if hasattr(g, "current_user") and g.current_user:
                context["user_id"] = g.current_user["id"]
                context["user_email"] = g.current_user["email"]
        except RuntimeError:
            # Outside of request context
            pass

        return context

    def log(
        self,
        event_type: AuditEventType,
        message: str,
        severity: AuditSeverity = AuditSeverity.INFO,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[dict] = None,
        user_id: Optional[int] = None,
        user_email: Optional[str] = None,
    ) -> AuditEvent:
        """Log an audit event."""
        context = self._get_request_context()

        event = AuditEvent(
            event_type=event_type,
            severity=severity,
            message=message,
            user_id=user_id or context["user_id"],
            user_email=user_email or context["user_email"],
            ip_address=context["ip_address"],
            user_agent=context["user_agent"],
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )

        # Store to database
        self._store_to_database(event)

        # Log to application logger
        self._log_to_app_logger(event)

        return event

    def _store_to_database(self, event: AuditEvent) -> None:
        """Store audit event to database."""
        try:
            from .models import get_db

            db = get_db()
            if db and hasattr(db, "system_logs"):
                db.system_logs.insert(
                    level=event.severity.value.upper(),
                    component="audit",
                    message=f"[{event.event_type.value}] {event.message}",
                    details=json.dumps(event.to_dict()),
                    user_id=event.user_id,
                )
                db.commit()
        except Exception as e:
            # Don't let audit failures break the application
            if self.app:
                self.app.logger.error(f"Failed to store audit event: {e}")

    def _log_to_app_logger(self, event: AuditEvent) -> None:
        """Log audit event to application logger."""
        if not self.app:
            return

        log_message = (
            f"AUDIT [{event.event_type.value}] {event.message} "
            f"user={event.user_email} ip={event.ip_address}"
        )

        severity_map = {
            AuditSeverity.DEBUG: self.app.logger.debug,
            AuditSeverity.INFO: self.app.logger.info,
            AuditSeverity.WARNING: self.app.logger.warning,
            AuditSeverity.ERROR: self.app.logger.error,
            AuditSeverity.CRITICAL: self.app.logger.critical,
        }

        log_func = severity_map.get(event.severity, self.app.logger.info)
        log_func(log_message)

    # Shell session audit methods
    def log_shell_session_create(
        self,
        session_id: str,
        target_host: str,
        target_user: str,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log shell session creation."""
        return self.log(
            event_type=AuditEventType.SHELL_SESSION_CREATE,
            message=f"Shell session created to {target_user}@{target_host}",
            severity=AuditSeverity.INFO,
            resource_type="shell_session",
            resource_id=session_id,
            details={
                "session_id": session_id,
                "target_host": target_host,
                "target_user": target_user,
                **(details or {}),
            },
        )

    def log_shell_session_terminate(
        self,
        session_id: str,
        reason: str = "normal",
        duration_seconds: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log shell session termination."""
        return self.log(
            event_type=AuditEventType.SHELL_SESSION_TERMINATE,
            message=f"Shell session {session_id} terminated: {reason}",
            severity=AuditSeverity.INFO,
            resource_type="shell_session",
            resource_id=session_id,
            details={
                "session_id": session_id,
                "reason": reason,
                "duration_seconds": duration_seconds,
                **(details or {}),
            },
        )

    # Certificate audit methods
    def log_csr_submit(
        self,
        csr_id: str,
        common_name: str,
        requester: str,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log certificate signing request submission."""
        return self.log(
            event_type=AuditEventType.CERT_CSR_SUBMIT,
            message=f"CSR submitted for {common_name} by {requester}",
            severity=AuditSeverity.INFO,
            resource_type="certificate_request",
            resource_id=csr_id,
            details={
                "csr_id": csr_id,
                "common_name": common_name,
                "requester": requester,
                **(details or {}),
            },
        )

    def log_csr_approve(
        self,
        csr_id: str,
        approver: str,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log CSR approval."""
        return self.log(
            event_type=AuditEventType.CERT_CSR_APPROVE,
            message=f"CSR {csr_id} approved by {approver}",
            severity=AuditSeverity.INFO,
            resource_type="certificate_request",
            resource_id=csr_id,
            details={
                "csr_id": csr_id,
                "approver": approver,
                **(details or {}),
            },
        )

    def log_cert_issued(
        self,
        cert_id: str,
        common_name: str,
        serial_number: str,
        expires_at: datetime,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log certificate issuance."""
        msg = f"Certificate issued for {common_name} (serial: {serial_number})"
        return self.log(
            event_type=AuditEventType.CERT_ISSUED,
            message=msg,
            severity=AuditSeverity.INFO,
            resource_type="certificate",
            resource_id=cert_id,
            details={
                "cert_id": cert_id,
                "common_name": common_name,
                "serial_number": serial_number,
                "expires_at": expires_at.isoformat(),
                **(details or {}),
            },
        )

    def log_cert_revoked(
        self,
        cert_id: str,
        serial_number: str,
        reason: str,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log certificate revocation."""
        return self.log(
            event_type=AuditEventType.CERT_REVOKED,
            message=f"Certificate {serial_number} revoked: {reason}",
            severity=AuditSeverity.WARNING,
            resource_type="certificate",
            resource_id=cert_id,
            details={
                "cert_id": cert_id,
                "serial_number": serial_number,
                "reason": reason,
                **(details or {}),
            },
        )

    # Agent audit methods
    def log_agent_enroll(
        self,
        agent_id: str,
        hostname: str,
        agent_version: str,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log agent enrollment."""
        return self.log(
            event_type=AuditEventType.AGENT_ENROLL,
            message=f"Agent enrolled: {hostname} (v{agent_version})",
            severity=AuditSeverity.INFO,
            resource_type="agent",
            resource_id=agent_id,
            details={
                "agent_id": agent_id,
                "hostname": hostname,
                "agent_version": agent_version,
                **(details or {}),
            },
        )

    def log_agent_heartbeat(
        self,
        agent_id: str,
        hostname: str,
        status: str,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log agent heartbeat."""
        return self.log(
            event_type=AuditEventType.AGENT_HEARTBEAT,
            message=f"Agent heartbeat: {hostname} status={status}",
            severity=AuditSeverity.DEBUG,
            resource_type="agent",
            resource_id=agent_id,
            details={
                "agent_id": agent_id,
                "hostname": hostname,
                "status": status,
                **(details or {}),
            },
        )

    def log_agent_disconnect(
        self,
        agent_id: str,
        hostname: str,
        reason: str = "unknown",
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log agent disconnection."""
        return self.log(
            event_type=AuditEventType.AGENT_DISCONNECT,
            message=f"Agent disconnected: {hostname} reason={reason}",
            severity=AuditSeverity.WARNING,
            resource_type="agent",
            resource_id=agent_id,
            details={
                "agent_id": agent_id,
                "hostname": hostname,
                "reason": reason,
                **(details or {}),
            },
        )

    # Session recording methods
    def save_session_recording(
        self,
        session_id: str,
        recording_data: bytes,
        metadata: Optional[dict] = None,
    ) -> str:
        """Save session recording to storage.

        Args:
            session_id: Unique session identifier
            recording_data: Binary recording data
            metadata: Optional metadata about the recording

        Returns:
            Path to the saved recording file
        """
        if not self._recording_path:
            raise RuntimeError("Recording storage not configured")

        # Create date-based subdirectory
        date_str = datetime.utcnow().strftime("%Y/%m/%d")
        date_dir = self._recording_path / date_str
        date_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename
        timestamp = datetime.utcnow().strftime("%H%M%S")
        filename = f"{session_id}_{timestamp}.cast"
        filepath = date_dir / filename

        # Write recording data
        filepath.write_bytes(recording_data)

        # Write metadata sidecar if provided
        if metadata:
            metadata_path = filepath.with_suffix(".json")
            metadata_path.write_text(json.dumps(metadata, indent=2))

        # Log the recording save
        self.log(
            event_type=AuditEventType.SHELL_SESSION_TERMINATE,
            message=f"Session recording saved: {filepath}",
            severity=AuditSeverity.INFO,
            resource_type="session_recording",
            resource_id=session_id,
            details={
                "filepath": str(filepath),
                "size_bytes": len(recording_data),
                "metadata": metadata,
            },
        )

        return str(filepath)


# Decorator for auditing function calls
def audit_action(
    event_type: AuditEventType,
    message_template: str,
    severity: AuditSeverity = AuditSeverity.INFO,
    resource_type: Optional[str] = None,
    resource_id_arg: Optional[str] = None,
) -> Callable:
    """Decorator to automatically audit function calls.

    Args:
        event_type: Type of audit event
        message_template: Message template (can use {arg_name} placeholders)
        severity: Event severity level
        resource_type: Type of resource being accessed
        resource_id_arg: Name of argument containing resource ID

    Example:
        @audit_action(
            AuditEventType.RESOURCE_DELETE,
            "Deleted machine {machine_id}",
            resource_type="machine",
            resource_id_arg="machine_id"
        )
        def delete_machine(machine_id: str):
            ...
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get audit logger
            audit_logger = get_audit_logger()
            if not audit_logger:
                return func(*args, **kwargs)

            # Extract resource ID if specified
            resource_id = None
            if resource_id_arg:
                resource_id = kwargs.get(resource_id_arg)

            # Format message with function arguments
            try:
                message = message_template.format(**kwargs)
            except KeyError:
                message = message_template

            # Execute function
            try:
                result = func(*args, **kwargs)

                # Log successful action
                audit_logger.log(
                    event_type=event_type,
                    message=message,
                    severity=severity,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details={"status": "success"},
                )

                return result

            except Exception as e:
                # Log failed action
                audit_logger.log(
                    event_type=event_type,
                    message=f"{message} - FAILED",
                    severity=AuditSeverity.ERROR,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details={"status": "failed", "error": str(e)},
                )
                raise

        return wrapper

    return decorator


def get_audit_logger() -> Optional[AuditLogger]:
    """Get the audit logger instance from current app."""
    try:
        if current_app and hasattr(current_app, "extensions"):
            return current_app.extensions.get("audit")
    except RuntimeError:
        pass
    return None


# Global audit logger instance for non-Flask contexts
_global_audit_logger: Optional[AuditLogger] = None


def init_audit_logger(app) -> AuditLogger:
    """Initialize and return audit logger for app."""
    global _global_audit_logger
    audit_logger = AuditLogger(app)
    _global_audit_logger = audit_logger
    return audit_logger
