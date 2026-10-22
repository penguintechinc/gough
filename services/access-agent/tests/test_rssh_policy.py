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


def test_account_locked(tmp_path: Any) -> None:
    """'!' password field is locked; hashes/'*' are not; unreadable shadow is skipped."""
    sh = tmp_path / "shadow"
    sh.write_text("a:!$6$x:1::\nb:$6$hash:1::\nc:*:1::\nd:!:1::\nbad\n")
    f = str(sh)
    assert rssh.account_locked("a", f) and rssh.account_locked("d", f)
    assert not rssh.account_locked("b", f) and not rssh.account_locked("c", f)
    assert not rssh.account_locked("zzz", f)
    assert not rssh.account_locked("a", str(tmp_path / "absent"))


@pytest.mark.parametrize(
    ("patch", "value"),
    [("shell_problem", "locking shell"), ("account_locked", True)],
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
