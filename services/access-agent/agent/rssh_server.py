"""Reverse SSH Server for Gough Access Agent.

Provides SSH server that accepts connections with certificates
signed by the Gough SSH CA. Spawns PTY sessions for shell access.
"""

import logging
import os
import pty
import select
import socket
import struct
import subprocess
import termios
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import paramiko
from paramiko import RSAKey, ServerInterface, Transport

from .cert_validator import CertificateInfo, CertificateValidator
from .config import AgentConfig

log = logging.getLogger(__name__)


@dataclass
class ShellSession:
    """Active shell session."""

    session_id: str
    channel: paramiko.Channel
    transport: Transport
    principals: List[str]
    master_fd: Optional[int] = None
    slave_fd: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    reader_thread: Optional[threading.Thread] = None
    running: bool = False


class SSHServerInterface(ServerInterface):
    """Paramiko SSH server interface with certificate authentication."""

    def __init__(
        self,
        cert_validator: CertificateValidator,
        allowed_principals: Optional[List[str]] = None,
    ):
        """Initialize SSH server interface.

        Args:
            cert_validator: Certificate validator
            allowed_principals: Optional list of allowed principals
        """
        self.cert_validator = cert_validator
        self.allowed_principals = allowed_principals
        self.authenticated_user: Optional[str] = None
        self.cert_info: Optional[CertificateInfo] = None

        super().__init__()

    def check_auth_publickey(self, username: str, key) -> int:
        """Check public key authentication.

        Validates SSH certificate signed by CA.
        """
        log.debug(f"Auth attempt for user {username}")

        # Check if this is a certificate
        if not hasattr(key, "public_blob"):
            log.warning("Key is not a certificate, rejecting")
            return paramiko.AUTH_FAILED

        try:
            # Get certificate from key
            cert_str = key.get_base64()

            # Validate certificate
            self.cert_info = self.cert_validator.validate_certificate(
                f"ssh-rsa-cert-v01@openssh.com {cert_str}",
                expected_principals=self.allowed_principals,
            )

            # Check username matches a principal
            if username not in self.cert_info.principals:
                log.warning(
                    f"Username {username} not in principals "
                    f"{self.cert_info.principals}"
                )
                return paramiko.AUTH_FAILED

            self.authenticated_user = username
            log.info(
                f"Certificate authentication successful for {username} "
                f"(key_id: {self.cert_info.key_id})"
            )

            return paramiko.AUTH_SUCCESSFUL

        except Exception as e:
            log.warning(f"Certificate validation failed: {e}")
            return paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        """Check channel request."""
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_shell_request(self, channel) -> bool:
        """Allow shell requests."""
        return True

    def check_channel_pty_request(
        self,
        channel,
        term: str,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
        modes: bytes,
    ) -> bool:
        """Allow PTY requests."""
        channel.term = term
        channel.width = width
        channel.height = height
        return True

    def check_channel_window_change_request(
        self,
        channel,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
    ) -> bool:
        """Allow window size changes."""
        channel.width = width
        channel.height = height
        return True

    def get_allowed_auths(self, username: str) -> str:
        """Return allowed authentication methods."""
        return "publickey"


class RSSHServer:
    """Reverse SSH server for Gough Access Agent."""

    def __init__(
        self,
        config: AgentConfig,
        cert_validator: CertificateValidator,
        on_session_start: Optional[Callable[[str], None]] = None,
        on_session_end: Optional[Callable[[str], None]] = None,
    ):
        """Initialize rssh server.

        Args:
            config: Agent configuration
            cert_validator: Certificate validator
            on_session_start: Callback when session starts
            on_session_end: Callback when session ends
        """
        self.config = config
        self.cert_validator = cert_validator
        self.on_session_start = on_session_start
        self.on_session_end = on_session_end

        self._running = False
        self._server_socket: Optional[socket.socket] = None
        self._host_key: Optional[RSAKey] = None
        self._sessions: Dict[str, ShellSession] = {}
        self._accept_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_host_key(self) -> RSAKey:
        """Load or generate host key."""
        if self._host_key:
            return self._host_key

        key_path = Path(self.config.host_key_file)

        if key_path.exists():
            log.info(f"Loading host key from {key_path}")
            self._host_key = RSAKey.from_private_key_file(str(key_path))
        else:
            log.info("Generating new host key")
            self._host_key = RSAKey.generate(2048)

            # Save host key
            key_path.parent.mkdir(parents=True, exist_ok=True)
            self._host_key.write_private_key_file(str(key_path))
            key_path.chmod(0o600)

        return self._host_key

    def start(self) -> None:
        """Start rssh server."""
        if self._running:
            return

        self._ensure_host_key()

        # Create server socket
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self._server_socket.bind(
                (self.config.rssh_listen_host, self.config.rssh_listen_port)
            )
            self._server_socket.listen(10)
            self._server_socket.settimeout(1.0)

            self._running = True

            # Start accept thread
            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                daemon=True,
            )
            self._accept_thread.start()

            log.info(
                f"rssh server listening on "
                f"{self.config.rssh_listen_host}:{self.config.rssh_listen_port}"
            )

        except Exception as e:
            self._server_socket.close()
            raise RuntimeError(f"Failed to start rssh server: {e}")

    def stop(self) -> None:
        """Stop rssh server."""
        self._running = False

        # Close all sessions
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                self._cleanup_session(session)
            self._sessions.clear()

        # Close server socket
        if self._server_socket:
            self._server_socket.close()
            self._server_socket = None

        # Wait for accept thread
        if self._accept_thread:
            self._accept_thread.join(timeout=2)

        log.info("rssh server stopped")

    def get_active_session_count(self) -> int:
        """Get number of active sessions."""
        with self._lock:
            return len(self._sessions)

    def _accept_loop(self) -> None:
        """Accept incoming connections."""
        while self._running:
            try:
                client_socket, client_addr = self._server_socket.accept()
                log.info(f"Connection from {client_addr}")

                # Handle connection in new thread
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket, client_addr),
                    daemon=True,
                )
                thread.start()

            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log.error(f"Error accepting connection: {e}")

    def _handle_client(
        self,
        client_socket: socket.socket,
        client_addr: tuple,
    ) -> None:
        """Handle incoming client connection.

        Args:
            client_socket: Client socket
            client_addr: Client address tuple
        """
        transport = None
        session = None

        try:
            # Create transport
            transport = Transport(client_socket)
            transport.add_server_key(self._host_key)
            transport.set_gss_host(socket.getfqdn())

            # Create server interface
            server = SSHServerInterface(
                cert_validator=self.cert_validator,
            )

            # Start server
            transport.start_server(server=server)

            # Wait for authentication
            channel = transport.accept(timeout=30)
            if channel is None:
                log.warning("No channel opened within timeout")
                return

            # Wait for shell request
            if not channel.recv_ready():
                # Give time for shell request
                import time
                time.sleep(0.5)

            # Generate session ID
            import uuid
            session_id = str(uuid.uuid4())

            # Create session
            session = ShellSession(
                session_id=session_id,
                channel=channel,
                transport=transport,
                principals=server.cert_info.principals if server.cert_info else [],
            )

            with self._lock:
                self._sessions[session_id] = session

            # Notify session start
            if self.on_session_start:
                self.on_session_start(session_id)

            log.info(
                f"Shell session {session_id} started for "
                f"user {server.authenticated_user}"
            )

            # Start shell
            self._start_shell(session, server.authenticated_user or "root")

        except Exception as e:
            log.error(f"Error handling client: {e}")
        finally:
            if session:
                self._cleanup_session(session)
                with self._lock:
                    self._sessions.pop(session.session_id, None)

                # Notify session end
                if self.on_session_end:
                    self.on_session_end(session.session_id)

            if transport:
                transport.close()

    def _start_shell(self, session: ShellSession, username: str) -> None:
        """Start PTY shell for session.

        Args:
            session: Shell session
            username: Unix username
        """
        # Create PTY
        master_fd, slave_fd = pty.openpty()
        session.master_fd = master_fd
        session.slave_fd = slave_fd

        # Set terminal size
        term = getattr(session.channel, "term", "xterm")
        width = getattr(session.channel, "width", 80)
        height = getattr(session.channel, "height", 24)
        self._set_window_size(master_fd, height, width)

        # Start shell process
        env = os.environ.copy()
        env["TERM"] = term
        env["USER"] = username
        env["LOGNAME"] = username
        env["HOME"] = f"/home/{username}"

        session.process = subprocess.Popen(
            ["/bin/bash", "-l"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=env,
            cwd=env["HOME"],
        )

        session.running = True

        # Start reader thread
        session.reader_thread = threading.Thread(
            target=self._read_output,
            args=(session,),
            daemon=True,
        )
        session.reader_thread.start()

        # Read input from channel
        self._read_input(session)

    def _read_input(self, session: ShellSession) -> None:
        """Read input from SSH channel and write to PTY.

        Args:
            session: Shell session
        """
        channel = session.channel

        while session.running and channel.active:
            try:
                if channel.recv_ready():
                    data = channel.recv(1024)
                    if data:
                        os.write(session.master_fd, data)
                    else:
                        break

                # Check for window size changes
                # This is handled by the channel callback

                import time
                time.sleep(0.01)

            except Exception as e:
                log.error(f"Error reading from channel: {e}")
                break

        session.running = False

    def _read_output(self, session: ShellSession) -> None:
        """Read output from PTY and write to SSH channel.

        Args:
            session: Shell session
        """
        channel = session.channel

        while session.running:
            try:
                readable, _, _ = select.select([session.master_fd], [], [], 0.1)

                if session.master_fd in readable:
                    try:
                        data = os.read(session.master_fd, 1024)
                        if data:
                            channel.send(data)
                        else:
                            break
                    except OSError:
                        break

            except Exception as e:
                log.error(f"Error reading from PTY: {e}")
                break

        session.running = False

    def _set_window_size(self, fd: int, rows: int, cols: int) -> None:
        """Set PTY window size.

        Args:
            fd: PTY file descriptor
            rows: Number of rows
            cols: Number of columns
        """
        try:
            import fcntl
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except Exception as e:
            log.error(f"Error setting window size: {e}")

    def _cleanup_session(self, session: ShellSession) -> None:
        """Clean up session resources.

        Args:
            session: Shell session to clean up
        """
        session.running = False

        # Terminate process
        if session.process:
            try:
                session.process.terminate()
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.process.kill()
            except Exception as e:
                log.error(f"Error terminating process: {e}")

        # Close file descriptors
        if session.master_fd:
            try:
                os.close(session.master_fd)
            except OSError:
                pass

        if session.slave_fd:
            try:
                os.close(session.slave_fd)
            except OSError:
                pass

        # Close channel
        if session.channel:
            try:
                session.channel.close()
            except Exception:
                pass

        log.info(f"Session {session.session_id} cleaned up")
