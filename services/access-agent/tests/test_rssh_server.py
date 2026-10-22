"""Behaviour tests for the reverse-SSH relay: auth, channel lifecycle and cleanup.

End-to-end cases run a real RSSHServer on 127.0.0.1 (ephemeral port) against a
real paramiko client. The shell spawn is redirected to /bin/sh in a temp cwd
because production hard-codes HOME=/home/<user>, which need not exist in CI.
"""

from __future__ import annotations

import os
import pwd
import select
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import paramiko
import pytest

from agent import rssh_server as rssh
from agent.cert_validator import CertificateValidator
from agent.config import AgentConfig
from tests.ssh_certs import SSHKeyPair, generate_keypair, sign_user_certificate

CURRENT_USER = pwd.getpwuid(os.getuid()).pw_name
AGENT_ID = "agent-1"
HOST_PRINCIPAL = f"host:{AGENT_ID}"


@dataclass(slots=True)
class SSHEnv:
    """Running rssh server plus the keys and recorders needed by a test."""

    server: rssh.RSSHServer
    port: int
    ca: SSHKeyPair
    user: SSHKeyPair
    tmp: Path
    started: list[str] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    popen_calls: list[list[str]] = field(default_factory=list)
    clients: list[paramiko.SSHClient] = field(default_factory=list)


@pytest.fixture
def ssh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SSHEnv]:
    """Start a real RSSHServer on an ephemeral port and tear everything down afterwards."""
    ca = generate_keypair(tmp_path, "gough_ca")
    user = generate_keypair(tmp_path, "operator")
    real_popen = subprocess.Popen
    recorded: list[list[str]] = []

    def redirected_popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Record the production argv and spawn an interactive sh in a temp cwd."""
        recorded.append(list(cmd))
        kwargs["cwd"] = str(tmp_path)
        argv = ["/bin/sh", "-c", cmd[2]] if len(cmd) > 2 and cmd[1] == "-c" else ["/bin/sh", "-i"]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(
        rssh,
        "subprocess",
        SimpleNamespace(Popen=redirected_popen, TimeoutExpired=subprocess.TimeoutExpired),
    )

    config = AgentConfig(
        rssh_listen_host="127.0.0.1",
        rssh_listen_port=0,
        host_key_file=str(tmp_path / "host_key"),
        agent_id=AGENT_ID,
    )
    monkeypatch.setenv("GOUGH_AGENT_SECRET", "s3cret-agent-token")
    env = SSHEnv(server=None, port=0, ca=ca, user=user, tmp=tmp_path)  # type: ignore[arg-type]
    server = rssh.RSSHServer(
        config=config,
        cert_validator=CertificateValidator(ca.public_line),
        on_session_start=env.started.append,
        on_session_end=env.ended.append,
    )
    server.start()
    env.server = server
    env.port = server._server_socket.getsockname()[1]
    env.popen_calls = recorded
    try:
        yield env
    finally:
        for client in env.clients:
            client.close()
        server.stop()
        time.sleep(0.05)


def cert_key(
    env: SSHEnv,
    principals: list[str],
    validity: str = "-1h:+1h",
    key: SSHKeyPair | None = None,
    host_scope: bool = True,
    extra_args: list[str] | None = None,
) -> paramiko.PKey:
    """Load the operator key and attach a CA-signed certificate to it for client auth.

    Args:
        env: Running test environment.
        principals: Principals to certify.
        validity: ssh-keygen validity window.
        key: Optional key pair to certify instead of the default operator key.
        host_scope: Add this host's ``host:<agent_id>`` principal (default True).
        extra_args: Extra ``ssh-keygen`` arguments (critical options, ``-h``).

    Returns:
        Paramiko private key carrying the certificate.
    """
    pair = key or env.user
    all_principals = [*principals, HOST_PRINCIPAL] if host_scope else principals
    sign_user_certificate(env.ca, pair, all_principals, validity=validity, extra_args=extra_args)
    loaded = paramiko.Ed25519Key.from_private_key_file(str(pair.private_path))
    loaded.load_certificate(str(pair.private_path.with_name(f"{pair.name}-cert.pub")))
    return loaded


def connect(env: SSHEnv, username: str, pkey: paramiko.PKey) -> paramiko.SSHClient:
    """Open an authenticated paramiko client to the test server.

    Args:
        env: Running test environment.
        username: Login name to present.
        pkey: Key (optionally certificate-bearing) to authenticate with.

    Returns:
        Connected client (registered for teardown).
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    env.clients.append(client)
    client.connect(
        "127.0.0.1",
        port=env.port,
        username=username,
        pkey=pkey,
        look_for_keys=False,
        allow_agent=False,
        timeout=10,
        auth_timeout=10,
        banner_timeout=10,
    )
    return client


def read_until(chan: paramiko.Channel, needle: str, timeout_s: float = 10.0) -> str:
    """Read channel output until ``needle`` appears or the deadline passes.

    Args:
        chan: Client-side channel.
        needle: Text to wait for.
        timeout_s: Overall deadline.

    Returns:
        Everything read so far, decoded leniently.
    """
    buf = b""
    deadline = time.monotonic() + timeout_s
    while needle.encode() not in buf and time.monotonic() < deadline:
        if chan.recv_ready():
            buf += chan.recv(4096)
        else:
            time.sleep(0.02)
    return buf.decode(errors="replace")


def wait_for(predicate: Any, timeout_s: float = 10.0) -> bool:
    """Poll ``predicate`` until it returns truthy or the deadline passes.

    Args:
        predicate: Zero-argument callable.
        timeout_s: Overall deadline.

    Returns:
        Whether the predicate became truthy in time.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_certified_operator_gets_interactive_shell(ssh_env: SSHEnv) -> None:
    """A CA-signed certificate authenticates, opens a PTY shell and relays I/O both ways."""
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    chan = client.invoke_shell(term="xterm-256color", width=100, height=40)
    chan.send("echo MARK$((6*7))\n")
    assert "MARK42" in read_until(chan, "MARK42")
    assert wait_for(lambda: ssh_env.started), "session-start callback never fired"
    assert ssh_env.server.get_active_session_count() == 1


def test_shell_environment_and_pty_geometry_are_applied(ssh_env: SSHEnv) -> None:
    """TERM and requested rows/cols reach the spawned shell; the production argv is /bin/bash -l."""
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    chan = client.invoke_shell(term="xterm-256color", width=100, height=40)
    time.sleep(0.5)  # let sh finish terminal setup before typing
    chan.send("echo TERM=$TERM USER_IS=$USER; stty size; echo DONE\n")
    out = read_until(chan, "DONE")
    assert "TERM=xterm-256color" in out
    assert f"USER_IS={CURRENT_USER}" in out
    assert "40 100" in out
    shell = pwd.getpwuid(os.getuid()).pw_shell
    assert ssh_env.popen_calls == [[shell if os.access(shell, os.X_OK) else "/bin/sh", "-l"]]


def test_window_change_request_resizes_pty(ssh_env: SSHEnv) -> None:
    """A window-change request after login is applied to the live terminal."""
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    chan = client.invoke_shell(term="xterm", width=80, height=24)
    time.sleep(0.5)
    chan.resize_pty(width=132, height=50)
    time.sleep(0.6)
    chan.send("stty size; echo DONE\n")
    assert "50 132" in read_until(chan, "DONE")


def test_shell_exit_ends_session_and_closes_channel(ssh_env: SSHEnv) -> None:
    """Typing exit ends the shell, closes the channel and fires the end callback."""
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    chan = client.invoke_shell()
    chan.send("echo ALIVE\n")
    read_until(chan, "ALIVE")
    chan.send("exit\n")
    assert wait_for(lambda: ssh_env.ended, timeout_s=10), "session-end callback never fired"
    assert ssh_env.server.get_active_session_count() == 0
    assert wait_for(lambda: chan.closed or chan.exit_status_ready(), timeout_s=10)


def test_certificate_from_untrusted_ca_is_rejected(ssh_env: SSHEnv, tmp_path: Path) -> None:
    """A certificate minted by a different CA cannot authenticate."""
    rogue = generate_keypair(tmp_path, "rogue_ca")
    sign_user_certificate(rogue, ssh_env.user, [CURRENT_USER, HOST_PRINCIPAL])
    pkey = paramiko.Ed25519Key.from_private_key_file(str(ssh_env.user.private_path))
    pkey.load_certificate(str(ssh_env.user.private_path.with_name("operator-cert.pub")))
    with pytest.raises(paramiko.AuthenticationException):
        connect(ssh_env, CURRENT_USER, pkey)
    assert ssh_env.started == []


def test_expired_certificate_is_rejected(ssh_env: SSHEnv) -> None:
    """A trusted but expired certificate is refused."""
    with pytest.raises(paramiko.AuthenticationException):
        connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER], validity="-2h:-1h"))


def test_login_name_outside_certificate_principals_is_rejected(ssh_env: SSHEnv) -> None:
    """Logging in as a user the certificate does not name is refused even with a valid cert."""
    with pytest.raises(paramiko.AuthenticationException):
        connect(ssh_env, "not-a-principal", cert_key(ssh_env, [CURRENT_USER]))


def test_plain_key_without_certificate_is_rejected(ssh_env: SSHEnv) -> None:
    """A bare public key with no CA-issued certificate is refused."""
    bare = paramiko.Ed25519Key.from_private_key_file(str(ssh_env.user.private_path))
    with pytest.raises(paramiko.AuthenticationException):
        connect(ssh_env, CURRENT_USER, bare)


def test_non_session_channel_types_are_refused(ssh_env: SSHEnv) -> None:
    """Authenticated clients still cannot open direct-tcpip (port-forward) channels."""
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    transport = client.get_transport()
    assert transport is not None
    with pytest.raises(paramiko.ChannelException):
        transport.open_channel("direct-tcpip", ("127.0.0.1", 9), ("127.0.0.1", 0))


def test_client_disconnect_before_channel_releases_handler(ssh_env: SSHEnv) -> None:
    """Authenticating and then dropping without opening a channel creates no session."""
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    client.close()
    assert wait_for(lambda: threading.active_count() < 50)
    assert ssh_env.server.get_active_session_count() == 0
    assert ssh_env.started == []


def test_non_ssh_bytes_do_not_create_a_session(ssh_env: SSHEnv) -> None:
    """A plain HTTP request to the SSH port is dropped without creating a session."""
    with socket.create_connection(("127.0.0.1", ssh_env.port), timeout=5) as raw:
        raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        raw.settimeout(5)
        try:
            raw.recv(64)
        except socket.timeout:
            pass
    assert ssh_env.server.get_active_session_count() == 0
    assert ssh_env.started == []


def test_accept_loop_survives_idle_timeouts(ssh_env: SSHEnv) -> None:
    """The accept loop keeps serving after its 1s accept timeout fires while idle."""
    time.sleep(1.2)
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    assert client.get_transport() is not None and client.get_transport().is_active()


def test_start_twice_is_noop_and_busy_port_raises(ssh_env: SSHEnv, tmp_path: Path) -> None:
    """Starting a running server does nothing; binding an occupied port fails loudly."""
    ssh_env.server.start()  # already running: no-op
    config = AgentConfig(
        rssh_listen_host="127.0.0.1",
        rssh_listen_port=ssh_env.port,
        host_key_file=str(tmp_path / "host_key"),
    )
    clash = rssh.RSSHServer(config, CertificateValidator(ssh_env.ca.public_line))
    with pytest.raises(RuntimeError, match="Failed to start rssh server"):
        clash.start()


def test_stop_closes_sessions_and_socket(ssh_env: SSHEnv) -> None:
    """stop tears down live sessions and releases the listening socket."""
    client = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER]))
    chan = client.invoke_shell()
    chan.send("echo UP\n")
    read_until(chan, "UP")
    ssh_env.server.stop()
    assert ssh_env.server.get_active_session_count() == 0
    assert ssh_env.server._server_socket is None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", ssh_env.port), timeout=1)


def test_host_key_is_generated_once_then_reloaded(tmp_path: Path) -> None:
    """A missing host key is generated with 0600 perms and an existing one is reused."""
    key_file = tmp_path / "keys" / "host_key"
    config = AgentConfig(host_key_file=str(key_file))
    first = rssh.RSSHServer(config, MagicMock(spec=CertificateValidator))._ensure_host_key()
    assert key_file.exists()
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"

    second_srv = rssh.RSSHServer(config, MagicMock(spec=CertificateValidator))
    second = second_srv._ensure_host_key()
    assert second.get_fingerprint() == first.get_fingerprint()
    assert second_srv._ensure_host_key() is second  # cached on the instance


def test_server_interface_auth_method_and_channel_policy() -> None:
    """Only publickey auth is offered; only session channels open; PTY and shell requests are allowed."""
    iface = rssh.SSHServerInterface(cert_validator=MagicMock(spec=CertificateValidator))
    assert iface.get_allowed_auths("anyone") == "publickey"
    assert iface.check_channel_request("session", 1) == paramiko.OPEN_SUCCEEDED
    assert iface.check_channel_request("direct-tcpip", 1) == paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    assert iface.check_channel_shell_request(object()) is True


def test_server_interface_records_pty_and_window_changes() -> None:
    """PTY and window-change requests store terminal geometry on the channel."""
    iface = rssh.SSHServerInterface(cert_validator=MagicMock(spec=CertificateValidator))
    chan = SimpleNamespace()
    assert iface.check_channel_pty_request(chan, "vt220", 90, 30, 0, 0, b"") is True
    assert (chan.term, chan.width, chan.height) == ("vt220", 90, 30)
    assert iface.check_channel_window_change_request(chan, 120, 45, 0, 0) is True
    assert (chan.width, chan.height) == (120, 45)


def test_publickey_without_blob_attribute_fails_closed() -> None:
    """A key object that is not a paramiko key is rejected before validation runs."""
    validator = MagicMock(spec=CertificateValidator)
    iface = rssh.SSHServerInterface(cert_validator=validator)
    assert iface.check_auth_publickey("alice", object()) == paramiko.AUTH_FAILED
    validator.validate_certificate.assert_not_called()


def test_read_input_forwards_channel_bytes_to_pty_and_stops_on_eof() -> None:
    """Bytes from the channel are written to the PTY master; an empty read ends the loop."""
    rfd, wfd = os.pipe()
    chan = _FakeChannel(payloads=[b"ls\n", b""])
    session = _session(chan, master_fd=wfd)
    try:
        srv = rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))
        srv._read_input(session)
        assert os.read(rfd, 16) == b"ls\n"
        assert session.running is False
    finally:
        for fd in (rfd, wfd):
            os.close(fd)


def test_read_input_idle_channel_does_not_busy_poll() -> None:
    """An idle channel is waited on via select, not polled every 10ms.

    Regression guard for the ops-audit busy-poll flagged at rssh_server.py read loop:
    the old 10ms sleep produced ~50 recv_ready() calls per 0.5s of idle time.
    """
    pipe_r, pipe_w = os.pipe()
    chan = _FakeChannel(payloads=[])
    session = _session(chan, master_fd=os.open(os.devnull, os.O_WRONLY))
    srv = rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))
    stopper = threading.Timer(0.5, lambda: setattr(session, "running", False))
    stopper.start()
    try:
        srv._read_input(session)
    finally:
        stopper.join()
        for fd in (pipe_r, pipe_w, session.master_fd):
            os.close(fd)
    assert chan.recv_ready_calls <= 15, f"busy-poll: {chan.recv_ready_calls} checks in 0.5s"


def test_read_input_recv_error_ends_loop() -> None:
    """An exception while reading the channel is logged and terminates the relay."""
    chan = _FakeChannel(payloads=[], raise_on_recv=True)
    session = _session(chan, master_fd=os.open(os.devnull, os.O_WRONLY))
    try:
        rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))._read_input(session)
    finally:
        os.close(session.master_fd)
    assert session.running is False


def test_read_output_relays_pty_data_then_stops_on_eof() -> None:
    """PTY output is sent to the channel until the master side reaches EOF."""
    rfd, wfd = os.pipe()
    sent: list[bytes] = []
    chan = _FakeChannel(payloads=[])
    chan.send = sent.append  # type: ignore[method-assign]
    session = _session(chan, master_fd=rfd)
    session.running = True
    os.write(wfd, b"hello from pty")
    os.close(wfd)
    rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))._read_output(session)
    assert b"".join(sent) == b"hello from pty"
    assert session.running is False
    os.close(rfd)


def test_read_output_select_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected select() failure ends the relay instead of propagating."""
    rfd, wfd = os.pipe()
    session = _session(_FakeChannel(payloads=[]), master_fd=rfd)
    session.running = True

    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("bad fd")

    monkeypatch.setattr(rssh.select, "select", boom)
    rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))._read_output(session)
    assert session.running is False
    os.close(rfd)
    os.close(wfd)


def test_window_size_failure_is_logged_not_raised() -> None:
    """A bad fd for TIOCSWINSZ is logged and does not abort shell start-up."""
    srv = rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))
    srv._set_window_size(-1, 24, 80)  # must not raise


def test_cleanup_kills_process_that_ignores_terminate() -> None:
    """A shell that does not exit within 5s after SIGTERM is SIGKILLed and still reaped."""
    process = MagicMock()
    process.pid = 2**22 + 12345  # no such pgid -> falls back to send_signal
    process.wait.side_effect = [subprocess.TimeoutExpired(cmd="bash", timeout=5), 0]
    session = _session(MagicMock(), master_fd=None)
    session.process = process
    rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))._cleanup_session(session)
    sent = [c.args[0] for c in process.send_signal.call_args_list]
    assert signal.SIGKILL in sent and process.wait.call_count == 2
    assert session.running is False


def test_cleanup_tolerates_already_closed_descriptors_and_failed_signal() -> None:
    """Closed fds and signal errors are logged and cleanup still completes."""
    process = MagicMock()
    process.pid = 2**22 + 12345
    process.send_signal.side_effect = RuntimeError("no such process")
    channel = MagicMock()
    channel.close.side_effect = RuntimeError("already closed")
    session = _session(channel, master_fd=999_999)
    session.slave_fd = 999_998
    session.process = process
    rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))._cleanup_session(session)
    channel.close.assert_called_once()


def test_reap_process_leaves_no_zombie_and_kills_whole_group() -> None:
    """A real session-leader shell and its child are both gone and wait()ed afterwards."""
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "trap '' TERM HUP; sleep 300 & wait"], start_new_session=True
    )
    time.sleep(0.3)
    srv = rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))
    start = time.monotonic()
    srv._reap_process(proc)
    assert proc.returncode is not None  # wait() collected it: no zombie
    assert time.monotonic() - start < 15

    def group_gone() -> bool:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return True
        return False

    assert wait_for(group_gone, timeout_s=5), "group member survived"


def test_reap_process_logs_unexpected_wait_failure() -> None:
    """A wait() failure is contained."""
    process = MagicMock()
    process.pid = 2**22 + 12345
    process.wait.side_effect = RuntimeError("boom")
    rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))._reap_process(process)


def test_active_session_count_reflects_registry() -> None:
    """get_active_session_count reads the live registry under the lock."""
    srv = rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))
    assert srv.get_active_session_count() == 0
    srv._sessions["a"] = _session(MagicMock(), master_fd=None)
    assert srv.get_active_session_count() == 1


class _FakeChannel:
    """Channel double whose readiness is signalled through a real pipe for select().

    Each scripted payload (or the single pending error) is one byte in the pipe, so
    select() reports readable exactly while there is something to recv().
    """

    def __init__(self, payloads: list[bytes], raise_on_recv: bool = False) -> None:
        self._rfd, self._wfd = os.pipe()
        self._payloads = list(payloads)
        self._raise = raise_on_recv
        self.recv_ready_calls = 0
        self.active = True
        self.closed = False
        pending = 1 if raise_on_recv else len(self._payloads)
        if pending:
            os.write(self._wfd, b"x" * pending)

    def fileno(self) -> int:
        """Return the fd that select() should watch."""
        return self._rfd

    def recv_ready(self) -> bool:
        """Count readiness checks and report data while payloads remain."""
        self.recv_ready_calls += 1
        return bool(self._payloads) or self._raise

    def recv(self, _n: int) -> bytes:
        """Consume one pipe byte, then return the next payload or raise."""
        os.read(self._rfd, 1)
        if self._raise:
            raise OSError("channel reset")
        return self._payloads.pop(0)

    def send(self, data: bytes) -> int:
        """Accept outbound data."""
        return len(data)

    def close(self) -> None:
        """Mark the channel closed."""
        self.closed = True


def _session(channel: Any, master_fd: int | None) -> rssh.ShellSession:
    """Build a ShellSession around a channel double."""
    return rssh.ShellSession(
        session_id="s-test",
        channel=channel,
        transport=MagicMock(),
        principals=[],
        master_fd=master_fd,
        running=True,
    )
