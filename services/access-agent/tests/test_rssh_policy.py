"""Security-policy tests for the rssh relay: host scoping, account policy, cert type,
critical options and shell environment hygiene."""

from __future__ import annotations

import os
import pwd
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import paramiko
import pytest

from agent import rssh_server as rssh
from agent.cert_validator import CertificateInfo, CertificateValidator
from agent.config import AgentConfig
from tests.test_rssh_server import (  # noqa: F401  (ssh_env is a fixture)
    CURRENT_USER,
    HOST_PRINCIPAL,
    SSHEnv,
    _FakeChannel,
    _session,
    cert_key,
    connect,
    read_until,
    ssh_env,
)


def _auth_fails(env: SSHEnv, username: str, pkey: paramiko.PKey) -> None:
    """Assert authentication is refused and no session was created."""
    with pytest.raises(paramiko.AuthenticationException):
        connect(env, username, pkey)
    assert env.started == []


def test_cert_without_host_principal_is_rejected(ssh_env: SSHEnv) -> None:
    """A valid CA-signed user cert that names no host is not honoured here (fail closed)."""
    _auth_fails(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER], host_scope=False))


def test_cert_for_another_host_is_rejected(ssh_env: SSHEnv) -> None:
    """A cert scoped to a different agent id cannot open a shell on this host."""
    pkey = cert_key(ssh_env, [CURRENT_USER, "host:other-agent"], host_scope=False)
    _auth_fails(ssh_env, CURRENT_USER, pkey)


def test_host_certificate_is_rejected(ssh_env: SSHEnv) -> None:
    """A host-type (-h) certificate never authenticates a login."""
    _auth_fails(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER], extra_args=["-h"]))


def test_unknown_unix_account_is_rejected(ssh_env: SSHEnv) -> None:
    """A principal that is not a local account is refused (no shell, no root fallback)."""
    _auth_fails(ssh_env, "no-such-acct-xyz", cert_key(ssh_env, ["no-such-acct-xyz"]))


def test_source_address_mismatch_is_rejected(ssh_env: SSHEnv) -> None:
    """source-address that excludes the peer refuses the login."""
    pkey = cert_key(ssh_env, [CURRENT_USER], extra_args=["-O", "source-address=10.9.9.9/32"])
    _auth_fails(ssh_env, CURRENT_USER, pkey)


def test_source_address_match_is_accepted(ssh_env: SSHEnv) -> None:
    """source-address that includes the peer allows the login."""
    pkey = cert_key(ssh_env, [CURRENT_USER], extra_args=["-O", "source-address=127.0.0.0/8"])
    client = connect(ssh_env, CURRENT_USER, pkey)
    transport = client.get_transport()
    assert transport is not None and transport.is_active()


def test_unsupported_critical_option_is_rejected(ssh_env: SSHEnv) -> None:
    """Any critical option other than force-command/source-address is refused."""
    pkey = cert_key(ssh_env, [CURRENT_USER], extra_args=["-O", "critical:unknown-opt=1"])
    _auth_fails(ssh_env, CURRENT_USER, pkey)


def test_force_command_runs_instead_of_login_shell(ssh_env: SSHEnv) -> None:
    """force-command from the cert replaces the interactive login shell."""
    pkey = cert_key(ssh_env, [CURRENT_USER], extra_args=["-O", "force-command=echo FORCED_OK"])
    chan = connect(ssh_env, CURRENT_USER, pkey).invoke_shell()
    assert "FORCED_OK" in read_until(chan, "FORCED_OK")
    shell = pwd.getpwuid(os.getuid()).pw_shell
    shell = shell if os.access(shell, os.X_OK) else "/bin/sh"
    assert ssh_env.popen_calls == [[shell, "-c", "echo FORCED_OK"]]


def test_agent_secrets_are_not_inherited_by_shell(ssh_env: SSHEnv) -> None:
    """The child gets a minimal env: the agent's own env (tokens) is not visible."""
    chan = connect(ssh_env, CURRENT_USER, cert_key(ssh_env, [CURRENT_USER])).invoke_shell()
    time.sleep(0.5)
    chan.send("echo SEC=[${GOUGH_AGENT_SECRET:-unset}] HOME_IS=$HOME; echo DONE\n")
    out = read_until(chan, "DONE")
    assert "SEC=[unset]" in out
    assert f"HOME_IS={pwd.getpwuid(os.getuid()).pw_dir}" in out


def _iface(info: CertificateInfo, allowed: list[str] | None = None, addr: Any = ("127.0.0.1", 1)):
    """Build an interface whose validator returns ``info`` plus a stand-in cert key."""
    validator = MagicMock(spec=CertificateValidator)
    validator.validate_certificate.return_value = info
    iface = rssh.SSHServerInterface(
        cert_validator=validator,
        allowed_principals=[HOST_PRINCIPAL] if allowed is None else allowed,
        client_addr=addr,
    )
    key = SimpleNamespace(
        public_blob=SimpleNamespace(key_blob=b"x", key_type="ssh-ed25519-cert-v01@openssh.com")
    )
    return iface, key


def _info(**kw: Any) -> CertificateInfo:
    """Build a CertificateInfo for direct policy tests."""
    base: dict[str, Any] = {
        "key_id": "k",
        "serial": 1,
        "principals": [CURRENT_USER, HOST_PRINCIPAL],
        "valid_after": 0,
        "valid_before": 2**40,
        "cert_type": "user",
        "critical_options": {},
    }
    base.update(kw)
    return CertificateInfo(**base)


def test_empty_allowed_principals_fails_closed() -> None:
    """With no host principal (unenrolled agent) every auth is refused."""
    iface, key = _iface(_info(), allowed=[])
    assert iface.check_auth_publickey(CURRENT_USER, key) == paramiko.AUTH_FAILED
    assert rssh.host_principals(None) == []
    assert rssh.host_principals("a") == ["host:a"]


def test_root_requires_allow_root_principal() -> None:
    """uid 0 is refused unless the cert carries allow-root."""
    iface, key = _iface(_info(principals=["root", HOST_PRINCIPAL]))
    assert iface.check_auth_publickey("root", key) == paramiko.AUTH_FAILED
    iface, key = _iface(_info(principals=["root", HOST_PRINCIPAL, "allow-root"]))
    assert iface.check_auth_publickey("root", key) == paramiko.AUTH_SUCCESSFUL


@pytest.mark.parametrize("name", ["host:agent-1", "allow-root"])
def test_reserved_principal_names_cannot_be_login_names(name: str) -> None:
    """host:* / allow-root are policy markers, never accounts."""
    iface, key = _iface(_info(principals=[name, HOST_PRINCIPAL]))
    assert iface.check_auth_publickey(name, key) == paramiko.AUTH_FAILED


def test_source_address_without_peer_fails_closed() -> None:
    """source-address with an unknown peer is refused."""
    iface, key = _iface(_info(critical_options={"source-address": "127.0.0.1/32"}), addr=None)
    assert iface.check_auth_publickey(CURRENT_USER, key) == paramiko.AUTH_FAILED


@pytest.mark.parametrize(
    ("spec", "peer", "ok"),
    [
        ("10.0.0.0/8", "10.1.2.3", True),
        ("10.0.0.0/8,192.168.0.1", "192.168.0.1", True),
        ("10.0.0.0/8", "11.0.0.1", False),
        ("garbage,10.0.0.0/8", "10.0.0.9", True),
        ("garbage", "10.0.0.9", False),
        ("10.0.0.0/8", "not-an-ip", False),
        ("10.0.0.0/8", None, False),
        ("fe80::/10", "fe80::1%eth0", True),
    ],
)
def test_source_address_allowed(spec: str, peer: str | None, ok: bool) -> None:
    """Peer matching follows OpenSSH list semantics and fails closed on junk."""
    assert rssh.source_address_allowed(spec, peer) is ok


def test_start_shell_unknown_user_never_defaults_to_root() -> None:
    """_start_shell for a vanished account raises before spawning anything."""
    srv = rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))
    session = _session(_FakeChannel(payloads=[]), master_fd=None)
    with pytest.raises(KeyError):
        srv._start_shell(session, "no-such-acct-xyz")
    assert session.process is None


def test_shell_problem_cases(tmp_path: Any) -> None:
    """Locking, missing, non-executable and unlisted shells are refused; empty is allowed."""
    shells = tmp_path / "shells"
    ok_shell = tmp_path / "okshell"
    ok_shell.write_text("#!/bin/sh\n")
    ok_shell.chmod(0o755)
    noexec = tmp_path / "noexec"
    noexec.write_text("x")
    noexec.chmod(0o644)
    shells.write_text(f"# c\n\n{ok_shell}\n{noexec}\n")
    f = str(shells)
    assert rssh.shell_problem("", f) is None
    assert rssh.shell_problem(str(ok_shell), f) is None
    assert rssh.shell_problem("/usr/sbin/nologin", f) == "locking shell"
    assert rssh.shell_problem("/bin/false", f) == "locking shell"
    assert "missing" in (rssh.shell_problem(str(tmp_path / "nope"), f) or "")
    assert "not executable" in (rssh.shell_problem(str(noexec), f) or "")
    shells.write_text("/other\n")
    assert "not in /etc/shells" in (rssh.shell_problem(str(ok_shell), f) or "")
    assert "cannot read" in (rssh.shell_problem(str(ok_shell), str(tmp_path / "absent")) or "")


class _FakePam:
    """Stand-in for the libpam CDLL; ``acct_rc`` is what pam_acct_mgmt returns."""

    def __init__(self, acct_rc: int = 0, start_rc: int = 0) -> None:
        self.acct_rc, self.start_rc, self.ended = acct_rc, start_rc, 0
        self.service: bytes | None = None
        self.user: bytes | None = None
        # Plain functions (not bound methods) so ctypes-style argtypes/restype can be set.
        self.pam_start = lambda *a: self._start(*a)
        self.pam_acct_mgmt = lambda *a: self._acct(*a)
        self.pam_end = lambda *a: self._end(*a)
        self.pam_set_item = lambda *a: self._set(*a)
        self.items: dict[int, bytes] = {}
        self.set_rc = 0

    def _set(self, handle: Any, item: int, value: bytes) -> int:
        self.items[item] = value
        return self.set_rc

    def _start(self, svc: bytes, user: bytes, conv: Any, handle: Any) -> int:
        self.service, self.user = svc, user
        return self.start_rc

    def _acct(self, handle: Any, flags: int) -> int:
        return self.acct_rc

    def _end(self, handle: Any, rc: int) -> int:
        self.ended += 1
        return 0


@pytest.mark.parametrize(
    ("rc", "ok"),
    [(0, True), (13, False), (12, False), (7, False), (10, False)],  # ok/expired/newtok/denied/acct
)
def test_account_permitted_maps_pam_codes(monkeypatch: pytest.MonkeyPatch, rc: int, ok: bool) -> None:
    """Only PAM_SUCCESS permits; expired / authtok-required / denied reject; handle is ended."""
    fake = _FakePam(acct_rc=rc)
    monkeypatch.setattr(rssh, "_load_libpam", lambda: fake)
    assert rssh.account_permitted("alice", "10.1.2.3") is ok
    assert fake.user == b"alice" and fake.ended == 1


def test_account_permitted_fails_closed_when_libpam_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unloadable libpam rejects (never skips)."""

    def boom() -> None:
        raise OSError("no libpam")

    monkeypatch.setattr(rssh, "_load_libpam", boom)
    assert rssh.account_permitted("alice", "10.1.2.3") is False


def test_account_permitted_fails_closed_on_pam_start_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """pam_start failure rejects."""
    monkeypatch.setattr(rssh, "_load_libpam", lambda: _FakePam(start_rc=4))
    assert rssh.account_permitted("alice", "10.1.2.3") is False


def test_pam_service_falls_back_to_sshd(monkeypatch: pytest.MonkeyPatch) -> None:
    """gough-agent is used only when its pam.d file exists."""
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert rssh.pam_service_name() == "sshd"
    monkeypatch.setattr(os.path, "exists", lambda p: p.endswith("/gough-agent"))
    assert rssh.pam_service_name() == "gough-agent"


def test_no_prompt_conv_refuses_prompts() -> None:
    """The conversation callback never answers a prompt."""
    assert rssh._no_prompt_conv(1, None, None, None) == rssh.PAM_CONV_ERR


@pytest.mark.skipif(os.geteuid() != 0, reason="real PAM call needs root")
def test_real_pam_call_for_current_user() -> None:
    """A real libpam round trip returns a bool without crashing."""
    assert isinstance(rssh.account_permitted(CURRENT_USER, "127.0.0.1"), bool)


def test_real_libpam_loader_is_ctypes_cdll() -> None:
    """The real loader either returns a CDLL or raises OSError (handled fail-closed)."""
    try:
        lib = rssh._load_libpam()
    except OSError:
        return
    assert hasattr(lib, "pam_acct_mgmt")


@pytest.mark.parametrize(
    ("patch", "value"),
    [("shell_problem", "locking shell"), ("account_permitted", False)],
)
def test_authorize_rejects_bad_shell_or_locked(
    monkeypatch: pytest.MonkeyPatch, patch: str, value: Any
) -> None:
    """_authorize refuses on shell problems and locked accounts."""
    monkeypatch.setattr(rssh, patch, lambda *a, **k: value)
    iface, key = _iface(_info())
    assert iface.check_auth_publickey(CURRENT_USER, key) == paramiko.AUTH_FAILED


def test_empty_pw_shell_falls_back_to_bin_sh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an empty pw_shell yields /bin/sh, and force-command uses the account shell."""
    real = pwd.getpwnam(CURRENT_USER)
    fake = pwd.struct_passwd((real.pw_name, "x", real.pw_uid, real.pw_gid, "", real.pw_dir, ""))
    monkeypatch.setattr(rssh.pwd, "getpwnam", lambda n: fake)
    calls: list[list[str]] = []

    class _P:
        pid = 1

    monkeypatch.setattr(rssh.subprocess, "Popen", lambda argv, **k: calls.append(argv) or _P())
    srv = rssh.RSSHServer(AgentConfig(), MagicMock(spec=CertificateValidator))
    session = _session(_FakeChannel(payloads=[]), master_fd=None)
    session.force_command = "id"
    monkeypatch.setattr(srv, "_read_input", lambda s: None)
    monkeypatch.setattr(rssh.threading, "Thread", lambda **k: MagicMock())
    srv._start_shell(session, CURRENT_USER)
    assert calls == [["/bin/sh", "-c", "id"]]


def test_pam_context_items_are_set_before_acct_mgmt(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAM_RHOST, PAM_TTY (and PAM_RUSER) are set so host-based PAM rules can apply."""
    fake = _FakePam()
    monkeypatch.setattr(rssh, "_load_libpam", lambda: fake)
    assert rssh.account_permitted("alice", "10.1.2.3", "key-1") is True
    assert fake.items == {rssh.PAM_RHOST: b"10.1.2.3", rssh.PAM_TTY: b"ssh", rssh.PAM_RUSER: b"key-1"}
    fake2 = _FakePam()
    monkeypatch.setattr(rssh, "_load_libpam", lambda: fake2)
    rssh.account_permitted("alice", "10.1.2.3")
    assert rssh.PAM_RUSER not in fake2.items


def test_pam_set_item_failure_fails_closed_and_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing pam_set_item rejects and still calls pam_end."""
    fake = _FakePam()
    fake.set_rc = 7
    monkeypatch.setattr(rssh, "_load_libpam", lambda: fake)
    assert rssh.account_permitted("alice", "10.1.2.3") is False
    assert fake.ended == 1


@pytest.mark.parametrize("peer", [None, ""])
def test_pam_unknown_peer_fails_closed(monkeypatch: pytest.MonkeyPatch, peer: str | None) -> None:
    """No peer address means no PAM context: reject without calling PAM."""
    monkeypatch.setattr(rssh, "_load_libpam", lambda: pytest.fail("must not load"))
    assert rssh.account_permitted("alice", peer) is False
