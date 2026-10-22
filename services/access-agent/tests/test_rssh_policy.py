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
