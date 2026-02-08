"""WebSocket Support for Real-Time Shell Sessions.

Provides Quart native WebSocket integration for interactive shell sessions
with PTY support. Handles WebSocket connections, PTY process
management, and terminal I/O forwarding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import select
import struct
import subprocess
import termios
from datetime import datetime
from typing import Any, Optional

from quart import Quart, websocket

from .audit import get_audit_logger
from .models import get_db

log = logging.getLogger(__name__)


def init_websocket(app: Quart) -> None:
    """Initialize Quart native WebSocket routes for shell sessions.

    Args:
        app: Quart application instance
    """
    @app.websocket('/ws/shell')
    async def shell_websocket():
        """Handle shell WebSocket connections."""
        session_id = websocket.args.get("session_id")

        if not session_id:
            log.warning("Connection rejected: No session_id provided")
            await websocket.close(1008, "No session_id provided")
            return

        # Validate session exists in database
        db = get_db()
        session = db(
            db.shell_sessions.session_id == session_id
        ).select().first()

        if not session:
            log.warning(
                f"Connection rejected: Invalid session_id {session_id}"
            )
            await websocket.close(1008, "Invalid session_id")
            return

        # Check if session already ended
        if session.ended_at:
            log.warning(
                f"Connection rejected: Session {session_id} already ended"
            )
            await websocket.send(json.dumps({
                "type": "error",
                "message": "Session already terminated"
            }))
            await websocket.close(1000)
            return

        log.info(f"Client connected to shell session {session_id}")

        # Start PTY session
        try:
            rows = int(websocket.args.get("rows", 24))
            cols = int(websocket.args.get("cols", 80))

            manager = ShellSessionManager(session_id)
            command = _get_shell_command(session.session_type)
            await manager.start(command=command, rows=rows, cols=cols)

            # Send connected message
            await websocket.send(json.dumps({
                "type": "connected",
                "session_id": session_id,
                "message": "Shell ready"
            }))

            # Handle messages
            try:
                async for message in websocket:
                    data = json.loads(message)
                    msg_type = data.get("type")

                    if msg_type == "input":
                        await manager.write_input(data.get("input", ""))
                    elif msg_type == "resize":
                        await manager.resize(data.get("rows", 24), data.get("cols", 80))

            except Exception as e:
                log.exception(f"Error handling websocket message: {e}")
            finally:
                # Cleanup
                await manager.cleanup()

                # Update session in database
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

        except Exception as e:
            log.exception(f"Error starting shell session: {e}")
            await websocket.send(json.dumps({
                "type": "error",
                "message": f"Failed to start shell: {e}"
            }))
            await websocket.close(1011)

    log.info("Quart WebSocket initialized for shell sessions")


def _get_shell_command(session_type: str) -> str:
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
        self.reader_task: Optional[asyncio.Task] = None
        self.running = False

    async def start(
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
        self.master_fd, self.slave_fd = await asyncio.to_thread(pty.openpty)

        # Set terminal size
        await self.resize(rows, cols)

        # Start shell process
        self.process = await asyncio.to_thread(
            subprocess.Popen,
            [command],
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            shell=False,
            preexec_fn=os.setsid,
        )

        self.running = True

        # Start reader task
        self.reader_task = asyncio.create_task(self._read_output())

        log.info(
            f"Shell session {self.session_id} started with PID "
            f"{self.process.pid}"
        )

    async def _read_output(self) -> None:
        """Read output from PTY and send via WebSocket.

        Runs as async task to continuously read PTY output.
        """
        while self.running and self.master_fd:
            try:
                # Use select to check if data is available
                readable, _, _ = await asyncio.to_thread(
                    select.select, [self.master_fd], [], [], 0.1
                )

                if self.master_fd in readable:
                    # Read output from PTY
                    try:
                        output = await asyncio.to_thread(os.read, self.master_fd, 1024)

                        if not output:
                            # EOF - process terminated
                            self.running = False
                            break

                        # Send output via WebSocket
                        await websocket.send(json.dumps({
                            "type": "output",
                            "data": output.decode("utf-8", errors="replace")
                        }))

                    except OSError as e:
                        log.warning(f"Error reading PTY output: {e}")
                        self.running = False
                        break

            except Exception as e:
                log.exception(f"Error in output reader: {e}")
                self.running = False
                break

        # Notify disconnect
        try:
            await websocket.send(json.dumps({
                "type": "disconnect_message",
                "reason": "Process terminated"
            }))
        except Exception:
            pass  # Connection may already be closed

        log.info(f"Shell session {self.session_id} output reader stopped")

    async def write_input(self, data: str) -> None:
        """Write input to PTY.

        Args:
            data: Input data to write
        """
        if self.master_fd and self.running:
            try:
                await asyncio.to_thread(os.write, self.master_fd, data.encode("utf-8"))
            except OSError as e:
                log.error(f"Error writing to PTY: {e}")
                self.running = False

    async def resize(self, rows: int, cols: int) -> None:
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
                await asyncio.to_thread(
                    fcntl.ioctl, self.master_fd, termios.TIOCSWINSZ, winsize
                )
            except Exception as e:
                log.error(f"Error resizing PTY: {e}")

    async def cleanup(self) -> None:
        """Clean up PTY and process resources."""
        self.running = False

        # Cancel reader task
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass

        # Terminate process
        if self.process:
            try:
                await asyncio.to_thread(self.process.terminate)
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                await asyncio.to_thread(self.process.kill)
            except Exception as e:
                log.error(f"Error terminating process: {e}")

        # Close file descriptors
        if self.master_fd:
            try:
                await asyncio.to_thread(os.close, self.master_fd)
            except OSError:
                pass

        if self.slave_fd:
            try:
                await asyncio.to_thread(os.close, self.slave_fd)
            except OSError:
                pass

        log.info(f"Shell session {self.session_id} cleaned up")
