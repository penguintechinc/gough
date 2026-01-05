"""WebSocket Support for Real-Time Shell Sessions.

Provides Flask-SocketIO integration for interactive shell sessions
with PTY support. Handles WebSocket connections, PTY process
management, and terminal I/O forwarding.
"""

from __future__ import annotations

import logging
import os
import pty
import select
import struct
import subprocess
import termios
from datetime import datetime
from threading import Thread
from typing import Any, Optional

from flask import Flask, request
from flask_socketio import Namespace, SocketIO, disconnect, emit

from .audit import get_audit_logger
from .models import get_db

log = logging.getLogger(__name__)

# Global SocketIO instance
socketio: Optional[SocketIO] = None


def init_websocket(app: Flask) -> SocketIO:
    """Initialize Flask-SocketIO for WebSocket support.

    Args:
        app: Flask application instance

    Returns:
        SocketIO instance
    """
    global socketio

    # Initialize SocketIO with async mode
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",  # Configure appropriately for production
        async_mode="threading",
        logger=False,
        engineio_logger=False,
        ping_timeout=60,
        ping_interval=25,
    )

    # Register shell namespace
    socketio.on_namespace(ShellNamespace("/ws/shell"))

    log.info("Flask-SocketIO initialized for shell sessions")

    return socketio


class ShellSessionManager:
    """Manages PTY sessions for shell access.

    Handles process spawning, I/O forwarding, and cleanup.
    """

    def __init__(self, session_id: str):
        """Initialize shell session manager.

        Args:
            session_id: Unique session identifier
        """
        self.session_id = session_id
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.process: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[Thread] = None
        self.running = False

    def start(
        self,
        command: str = "/bin/bash",
        rows: int = 24,
        cols: int = 80,
    ) -> None:
        """Start PTY process.

        Args:
            command: Shell command to execute
            rows: Terminal rows
            cols: Terminal columns
        """
        # Create PTY
        self.master_fd, self.slave_fd = pty.openpty()

        # Set terminal size
        self.resize(rows, cols)

        # Start shell process
        self.process = subprocess.Popen(
            [command],
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            shell=False,
            preexec_fn=os.setsid,
        )

        self.running = True

        # Start reader thread
        self.reader_thread = Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()

        log.info(
            f"Shell session {self.session_id} started with PID "
            f"{self.process.pid}"
        )

    def _read_output(self) -> None:
        """Read output from PTY and emit via WebSocket.

        Runs in separate thread to continuously read PTY output.
        """
        global socketio

        if not socketio:
            log.error("SocketIO not initialized")
            return

        while self.running and self.master_fd:
            try:
                # Use select to check if data is available
                readable, _, _ = select.select([self.master_fd], [], [], 0.1)

                if self.master_fd in readable:
                    # Read output from PTY
                    try:
                        output = os.read(self.master_fd, 1024)

                        if not output:
                            # EOF - process terminated
                            self.running = False
                            break

                        # Emit output via WebSocket
                        socketio.emit(
                            "output",
                            {
                                "data": output.decode(
                                    "utf-8", errors="replace"
                                )
                            },
                            namespace="/ws/shell",
                            room=self.session_id,
                        )

                    except OSError as e:
                        log.warning(f"Error reading PTY output: {e}")
                        self.running = False
                        break

            except Exception as e:
                log.exception(f"Error in output reader: {e}")
                self.running = False
                break

        # Notify disconnect
        socketio.emit(
            "disconnect_message",
            {"reason": "Process terminated"},
            namespace="/ws/shell",
            room=self.session_id,
        )  # pragma: no cover

        log.info(f"Shell session {self.session_id} output reader stopped")

    def write_input(self, data: str) -> None:
        """Write input to PTY.

        Args:
            data: Input data to write
        """
        if self.master_fd and self.running:
            try:
                os.write(self.master_fd, data.encode("utf-8"))
            except OSError as e:
                log.error(f"Error writing to PTY: {e}")
                self.running = False

    def resize(self, rows: int, cols: int) -> None:
        """Resize PTY terminal.

        Args:
            rows: Number of rows
            cols: Number of columns
        """
        if self.master_fd:
            try:
                # Set window size using TIOCSWINSZ ioctl
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                import fcntl
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except Exception as e:
                log.error(f"Error resizing PTY: {e}")

    def cleanup(self) -> None:
        """Clean up PTY and process resources."""
        self.running = False

        # Terminate process
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            except Exception as e:
                log.error(f"Error terminating process: {e}")

        # Close file descriptors
        if self.master_fd:
            try:
                os.close(self.master_fd)
            except OSError:
                pass

        if self.slave_fd:
            try:
                os.close(self.slave_fd)
            except OSError:
                pass

        # Wait for reader thread
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=2)

        log.info(f"Shell session {self.session_id} cleaned up")


class ShellNamespace(Namespace):
    """WebSocket namespace for shell sessions."""

    def __init__(self, namespace: str):
        """Initialize namespace.

        Args:
            namespace: Namespace path
        """
        super().__init__(namespace)
        self.sessions: dict[str, ShellSessionManager] = {}

    def on_connect(self):
        """Handle client connection."""
        # Extract session ID from query parameters
        session_id = request.args.get("session_id")

        if not session_id:
            log.warning("Connection rejected: No session_id provided")
            disconnect()
            return False

        # Validate session exists in database
        db = get_db()
        session = db(
            db.shell_sessions.session_id == session_id
        ).select().first()

        if not session:
            log.warning(
                f"Connection rejected: Invalid session_id {session_id}"
            )
            disconnect()
            return False

        # Check if session already ended
        if session.ended_at:
            log.warning(
                f"Connection rejected: Session {session_id} already ended"
            )
            emit("error", {"message": "Session already terminated"})
            disconnect()
            return False

        # Join session room
        from flask_socketio import join_room
        join_room(session_id)

        log.info(f"Client connected to shell session {session_id}")

        # Start PTY session if not already started
        if session_id not in self.sessions:
            try:
                manager = ShellSessionManager(session_id)

                # Get terminal size from client
                rows = int(request.args.get("rows", 24))
                cols = int(request.args.get("cols", 80))

                # Start shell based on session type
                command = self._get_shell_command(session.session_type)
                manager.start(command=command, rows=rows, cols=cols)

                self.sessions[session_id] = manager

                emit(
                    "connected",
                    {
                        "session_id": session_id,
                        "message": "Shell ready"
                    }
                )

            except Exception as e:
                log.exception(f"Error starting shell session: {e}")
                emit("error", {"message": f"Failed to start shell: {e}"})
                disconnect()
                return False

        return True

    def _get_shell_command(self, session_type: str) -> str:
        """Get shell command based on session type.

        Args:
            session_type: Type of session (ssh, kubectl, docker, cloud_cli)

        Returns:
            Shell command to execute
        """
        # Map session types to commands
        # In production, this would be more sophisticated
        commands = {
            "ssh": "/bin/bash",
            "kubectl": "/bin/bash",  # Would wrap kubectl
            "docker": "/bin/bash",  # Would wrap docker exec
            "cloud_cli": "/bin/bash",  # Would wrap cloud CLI tools
        }

        return commands.get(session_type, "/bin/bash")

    def on_input(self, data: dict[str, Any]):
        """Handle input from client.

        Args:
            data: Input data containing 'input' key
        """
        session_id = request.args.get("session_id")

        if not session_id or session_id not in self.sessions:
            return

        user_input = data.get("input", "")
        manager = self.sessions[session_id]
        manager.write_input(user_input)

    def on_resize(self, data: dict[str, Any]):
        """Handle terminal resize from client.

        Args:
            data: Resize data containing 'rows' and 'cols'
        """
        session_id = request.args.get("session_id")

        if not session_id or session_id not in self.sessions:
            return

        rows = data.get("rows", 24)
        cols = data.get("cols", 80)

        manager = self.sessions[session_id]
        manager.resize(rows, cols)

        log.debug(f"Resized session {session_id} to {rows}x{cols}")

    def on_disconnect(self):
        """Handle client disconnection."""
        session_id = request.args.get("session_id")

        if not session_id:
            return

        log.info(f"Client disconnected from shell session {session_id}")

        # Clean up session
        if session_id in self.sessions:
            manager = self.sessions[session_id]
            manager.cleanup()
            del self.sessions[session_id]

        # Update session in database
        db = get_db()
        session = (
            db(db.shell_sessions.session_id == session_id)
            .select()
            .first()
        )

        if session and not session.ended_at:
            # Calculate duration
            duration_seconds = None
            if session.started_at:
                duration_seconds = int(
                    (datetime.utcnow() - session.started_at).total_seconds()
                )

            # Update session
            db(db.shell_sessions.session_id == session_id).update(
                ended_at=datetime.utcnow()
            )
            db.commit()

            # Audit log
            audit_logger = get_audit_logger()
            if audit_logger:
                audit_logger.log_shell_session_terminate(
                    session_id=session_id,
                    reason="client_disconnect",
                    duration_seconds=duration_seconds,
                )
