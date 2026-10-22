"""Reverse SSH Server for Gough Access Agent.

Provides SSH server that accepts connections with certificates
signed by the Gough SSH CA. Spawns PTY sessions for shell access.
"""

import base64
import ctypes
import ipaddress
import os
from penguintechinc_utils import get_logger
import pty
import pwd
import re
import select
import signal
import socket
import struct
import subprocess
import termios
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import paramiko
from paramiko import RSAKey, ServerInterface, Transport

from .cert_validator import CertificateInfo, CertificateValidator
from .config import AgentConfig

log = get_logger(__name__)


def _become_session_leader() -> None:
    """Start a new session and make the PTY slave (fd 0) its controlling terminal.

    Without TIOCSCTTY the shell has no controlling tty, so job control and Ctrl-C
    do not work in relayed sessions.
    """
    import fcntl

    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


# Principal convention: a certificate is only honoured on a host when it carries
# ``host:<agent_id>`` for that host's hub-assigned agent id, in addition to the unix
# username principal. Root logins additionally require the ``allow-root`` principal.
HOST_PRINCIPAL_PREFIX = "host:"
ALLOW_ROOT_PRINCIPAL = "allow-root"
SUPPORTED_CRITICAL_OPTIONS = frozenset({"force-command", "source-address"})
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_TERM_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")


LOCKING_SHELLS = frozenset({"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"})
SHELLS_FILE = "/etc/shells"


def shell_problem(shell: str, shells_file: str = SHELLS_FILE) -> Optional[str]:
    """Return why ``shell`` may not be used for a login, or None if it is acceptable.

    Args:
        shell: The account's pw_shell (empty means "use /bin/sh").
        shells_file: Path of the permitted-shells list.

    Returns:
        A short reason string, or None when the shell is allowed.
    """
    if not shell:
        return None
    if shell in LOCKING_SHELLS:
        return "locking shell"
    if not os.path.isfile(shell) or not os.access(shell, os.X_OK):
        return "shell missing or not executable"
    try:
        with open(shells_file) as f:
            listed = {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except OSError:
        return "cannot read shells list"
    return None if shell in listed else "shell not in /etc/shells"


PAM_SERVICE = "gough-agent"
PAM_FALLBACK_SERVICE = "sshd"
PAM_SUCCESS = 0
PAM_CONV_ERR = 19


class _PamMessage(ctypes.Structure):
    """C ``struct pam_message``."""

    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class _PamResponse(ctypes.Structure):
    """C ``struct pam_response``."""

    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]


_CONV_FUNC = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(_PamMessage)),
    ctypes.POINTER(ctypes.POINTER(_PamResponse)),
    ctypes.c_void_p,
)


class _PamConv(ctypes.Structure):
    """C ``struct pam_conv``."""

    _fields_ = [("conv", _CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]


def _no_prompt_conv(n: int, msg: Any, resp: Any, data: Any) -> int:
    """PAM conversation callback that never answers prompts (account mgmt needs none)."""
    return PAM_CONV_ERR


def _load_libpam() -> Any:
    """Load libpam via ctypes; raises OSError when unavailable."""
    return ctypes.CDLL("libpam.so.0")


def pam_service_name() -> str:
    """Pick the PAM service: ``gough-agent`` if its file is installed, else ``sshd``."""
    for d in ("/etc/pam.d", "/usr/lib/pam.d"):
        if os.path.exists(os.path.join(d, PAM_SERVICE)):
            return PAM_SERVICE
    return PAM_FALLBACK_SERVICE


def account_permitted(username: str) -> bool:
    """Run PAM account management (``pam_acct_mgmt``) for ``username``.

    Covers locked, expired, time/host-restricted and NSS/LDAP/SSSD accounts. Anything
    other than PAM_SUCCESS (expired, new-authtok-required, denied, ...) rejects, and an
    unloadable libpam FAILS CLOSED. Install /etc/pam.d/gough-agent (installer follow-up);
    until then the ``sshd`` service stack is used.

    Args:
        username: Resolved local account name.

    Returns:
        True only when PAM returns PAM_SUCCESS.
    """
    try:
        libpam = _load_libpam()
        start = libpam.pam_start
        start.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(_PamConv),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        start.restype = ctypes.c_int
        acct = libpam.pam_acct_mgmt
        acct.argtypes = [ctypes.c_void_p, ctypes.c_int]
        acct.restype = ctypes.c_int
        end = libpam.pam_end
        end.argtypes = [ctypes.c_void_p, ctypes.c_int]
        end.restype = ctypes.c_int
    except (OSError, AttributeError) as e:
        log.error(f"libpam unavailable, failing closed: {e}")
        return False

    conv = _PamConv(_CONV_FUNC(_no_prompt_conv), None)
    handle = ctypes.c_void_p()
    rc = start(pam_service_name().encode(), username.encode(), ctypes.byref(conv),
               ctypes.byref(handle))
    if rc != PAM_SUCCESS:
        log.error(f"pam_start failed rc={rc}, failing closed")
        return False
    rc = PAM_CONV_ERR
    try:
        rc = acct(handle, 0)
    finally:
        end(handle, rc)
    if rc != PAM_SUCCESS:
        log.warning(f"PAM account check denied {username!r} (rc={rc})")
        return False
    return True


def host_principals(agent_id: Optional[str]) -> List[str]:
    """Return the principals that scope a certificate to this host.

    Args:
        agent_id: Hub-assigned agent id for this host.

    Returns:
        ``["host:<agent_id>"]``, or an empty list (fail closed) when unenrolled.
    """
    return [f"{HOST_PRINCIPAL_PREFIX}{agent_id}"] if agent_id else []


def source_address_allowed(spec: str, peer_ip: Optional[str]) -> bool:
    """Check a peer IP against an OpenSSH ``source-address`` critical option.

    Args:
        spec: Comma-separated list of addresses/CIDRs from the certificate.
        peer_ip: Connecting peer address; None fails closed.

    Returns:
        True only if the peer matches one entry; malformed entries never match.
    """
    if not peer_ip:
        return False
    try:
        peer = ipaddress.ip_address(peer_ip.split("%")[0])
    except ValueError:
        return False
    for entry in spec.split(","):
        try:
            if peer in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


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
    force_command: Optional[str] = None


class SSHServerInterface(ServerInterface):
    """Paramiko SSH server interface with certificate authentication."""

    def __init__(
        self,
        cert_validator: CertificateValidator,
        allowed_principals: Optional[List[str]] = None,
        on_window_change: Optional[Callable[[Any, int, int], None]] = None,
        client_addr: Optional[Tuple[Any, ...]] = None,
    ):
        """Initialize SSH server interface.

        Args:
            cert_validator: Certificate validator
            allowed_principals: Optional list of allowed principals
            on_window_change: Called with (channel, rows, cols) on terminal resize
            client_addr: Peer address tuple, used for ``source-address`` enforcement
        """
        self.client_addr = client_addr
        self.force_command: Optional[str] = None
        self.cert_validator = cert_validator
        self.allowed_principals = allowed_principals
        self.on_window_change = on_window_change
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
            # The certificate itself lives in public_blob; get_base64() returns the bare key.
            public_blob = key.public_blob
            cert_str = base64.b64encode(public_blob.key_blob).decode()

            # Validate certificate
            self.cert_info = self.cert_validator.validate_certificate(
                f"{public_blob.key_type} {cert_str}",
                expected_principals=self.allowed_principals,
            )

            if not self._authorize(username):
                return paramiko.AUTH_FAILED

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

    def _authorize(self, username: str) -> bool:
        """Apply host scoping, cert-type, critical-option and account policy.

        Args:
            username: Requested unix account.

        Returns:
            True only if every check passes; any doubt fails closed.
        """
        info = self.cert_info
        if info is None:
            return False
        if not self.allowed_principals:
            log.warning("No host-scoped principal configured, rejecting")
            return False
        if info.cert_type != "user":
            log.warning(f"Rejecting non-user certificate (type={info.cert_type})")
            return False
        if username.startswith(HOST_PRINCIPAL_PREFIX) or username == ALLOW_ROOT_PRINCIPAL:
            return False

        options = info.critical_options or {}
        unknown = set(options) - SUPPORTED_CRITICAL_OPTIONS
        if unknown:
            log.warning(f"Rejecting certificate with unsupported critical options {sorted(unknown)}")
            return False
        if "source-address" in options:
            peer_ip = self.client_addr[0] if self.client_addr else None
            if not source_address_allowed(options["source-address"], peer_ip):
                log.warning("Peer address not permitted by certificate source-address")
                return False

        try:
            pw = pwd.getpwnam(username)
        except KeyError:
            log.warning(f"Unknown account {username!r}")
            return False
        problem = shell_problem(pw.pw_shell)
        if problem:
            log.warning(f"Rejecting login for {username!r}: {problem}")
            return False
        if not account_permitted(username):
            return False
        if pw.pw_uid == 0 and ALLOW_ROOT_PRINCIPAL not in info.principals:
            log.warning("Root login requires the allow-root principal")
            return False

        self.force_command = options.get("force-command") or None
        return True

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
        """Allow window size changes and propagate them to the PTY."""
        channel.width = width
        channel.height = height
        if self.on_window_change is not None:
            self.on_window_change(channel, height, width)
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

            # Create server interface
            server = SSHServerInterface(
                cert_validator=self.cert_validator,
                allowed_principals=host_principals(self.config.agent_id),
                on_window_change=self._apply_window_change,
                client_addr=client_addr,
            )

            # Start server
            transport.start_server(server=server)

            # Wait for authentication
            channel = transport.accept(timeout=30)
            if channel is None:
                log.warning("No channel opened within timeout")
                return

            if not server.authenticated_user:
                log.warning("Channel opened without an authenticated user, refusing")
                channel.close()
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
                force_command=server.force_command,
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
            self._start_shell(session, server.authenticated_user)

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

    def _apply_window_change(self, channel: Any, rows: int, cols: int) -> None:
        """Resize the PTY of the session that owns ``channel``.

        Args:
            channel: Channel that reported the window-change request.
            rows: New terminal height.
            cols: New terminal width.
        """
        with self._lock:
            owned = [s for s in self._sessions.values() if s.channel is channel]
        for session in owned:
            if session.master_fd is not None:
                self._set_window_size(session.master_fd, rows, cols)

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

        pw = pwd.getpwnam(username)  # KeyError -> session aborted, never a root fallback

        if os.geteuid() == 0:
            # Hand the slave to the target user so the shell owns its terminal.
            os.fchown(slave_fd, pw.pw_uid, -1)

        # Set terminal size
        term = getattr(session.channel, "term", "xterm")
        if isinstance(term, bytes):
            term = term.decode("ascii", "replace")
        if not isinstance(term, str) or not _TERM_RE.match(term):
            term = "xterm"
        width = getattr(session.channel, "width", 80)
        height = getattr(session.channel, "height", 24)
        self._set_window_size(master_fd, height, width)

        # Minimal environment: nothing from the agent's own env (tokens, keys) is inherited.
        shell = pw.pw_shell or "/bin/sh"  # validated in _authorize; /bin/sh only if unset
        env = {
            "TERM": term,
            "USER": username,
            "LOGNAME": username,
            "HOME": pw.pw_dir,
            "SHELL": shell,
            "PATH": _SAFE_PATH,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        argv = [shell, "-c", session.force_command] if session.force_command else [shell, "-l"]

        # Drop privileges (setgroups -> setgid -> setuid) to the target account.
        priv: Dict[str, Any] = {}
        if pw.pw_uid != os.geteuid():
            priv = {
                "user": pw.pw_uid,
                "group": pw.pw_gid,
                "extra_groups": os.getgrouplist(username, pw.pw_gid),
            }

        session.process = subprocess.Popen(
            argv,
            **priv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_become_session_leader,
            env=env,
            cwd=pw.pw_dir,
        )

        # The child holds its own copy of the slave; drop ours so the master
        # reaches EOF/EIO when the shell exits and the session can end.
        os.close(slave_fd)
        session.slave_fd = None

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
                # Block on the channel's readiness fd instead of a 10ms busy-poll.
                # The timeout bounds how long a stop request can go unnoticed.
                readable, _, _ = select.select([channel], [], [], 0.1)
                if readable:
                    # EOF also makes the channel readable; recv() then returns b"" (no spin).
                    data = channel.recv(1024)
                    if not data:
                        break
                    os.write(session.master_fd, data)

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

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        """Signal the shell's whole process group (it is a session leader)."""
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(sig)
            except Exception:  # noqa: BLE001 - already gone
                pass

    def _reap_process(self, process: subprocess.Popen) -> None:
        """Terminate a session shell and always wait() on it so no zombie remains.

        Args:
            process: The shell process (session leader of its own group).
        """
        try:
            self._signal_group(process, signal.SIGHUP)
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                process.wait(timeout=5)
        except Exception as e:
            log.error(f"Error terminating process: {e}")

    def _cleanup_session(self, session: ShellSession) -> None:
        """Clean up session resources.

        Args:
            session: Shell session to clean up
        """
        session.running = False

        # Terminate process
        if session.process:
            self._reap_process(session.process)

        # Let the reader thread exit before its fd is closed (avoids fd-reuse races).
        reader = session.reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)

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
