"""Tests for gough init cluster bootstrap script."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import uuid

import pytest

# Note: In actual test run, import would be: from services.api_manager.scripts.gough_init import ...
# For now, we mock the imports


@pytest.fixture
def temp_etc_gough(tmp_path):
    """Provide temporary /etc/gough directory."""
    gough_dir = tmp_path / "etc" / "gough"
    gough_dir.mkdir(parents=True, exist_ok=True)
    with patch("pathlib.Path.__new__") as mock_path:
        # Simplified: in real tests, patch the actual Path usage
        yield gough_dir


@pytest.fixture
def mock_vault_client():
    """Mock VaultClient."""
    client = MagicMock()
    client.health.return_value = {"sealed": False, "version": "1.15.0"}
    client._hvac_client.secrets.transit.create_key = MagicMock()
    client.kv_write = MagicMock()
    return client


@pytest.fixture
def mock_db_session():
    """Mock SQLAlchemy session."""
    return MagicMock()


def test_already_initialized_exits_9(tmp_path, capsys):
    """Cluster already initialized without --reinit exits 9."""
    from pathlib import Path
    from unittest.mock import patch
    import sys

    cluster_json = tmp_path / "cluster.json"
    cluster_json.write_text(json.dumps({"cluster_id": "existing"}))

    with patch("pathlib.Path") as mock_path_class:
        mock_path_obj = MagicMock()
        mock_path_obj.exists.return_value = True
        mock_path_class.return_value = mock_path_obj

        # Simulate check_preexisting_state
        from scripts.gough_init import check_preexisting_state

        with pytest.raises(SystemExit) as exc_info:
            check_preexisting_state(reinit=False)
        assert exc_info.value.code == 9


def test_vault_sealed_exits_2(mock_vault_client):
    """Vault sealed triggers exit 2."""
    from scripts.gough_init import bootstrap_vault
    from app.clients.vault import VaultSealedError

    mock_vault_client.health.side_effect = VaultSealedError("Vault sealed")

    with pytest.raises(SystemExit) as exc_info:
        bootstrap_vault(mock_vault_client, "test-cluster", "password")
    assert exc_info.value.code == 2


def test_lxd_init_failure_exits_3():
    """LXD init failure exits 3."""
    from unittest.mock import patch
    from scripts.gough_init import init_lxd_cluster
    import subprocess

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "lxc", stderr=b"Init failed"
        )

        with pytest.raises(SystemExit) as exc_info:
            init_lxd_cluster(ha=False)
        assert exc_info.value.code == 3


def test_spire_bootstrap_failure_exits_4():
    """SPIRE bootstrap failure exits 4."""
    from unittest.mock import patch
    from scripts.gough_init import bootstrap_spire
    import subprocess

    with patch("subprocess.run") as mock_run:
        # First call (healthcheck) succeeds; subsequent calls (registration) fail
        mock_run.side_effect = [
            None,  # healthcheck passes
            subprocess.CalledProcessError(1, "spire-server", stderr=b"Register failed"),
        ]

        # bootstrap_spire logs warning but doesn't exit on registration fail
        # To test exit 4, we need healthcheck to fail:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "spire-server", stderr=b"Health failed"
        )

        with pytest.raises(SystemExit) as exc_info:
            bootstrap_spire("test-cluster")
        assert exc_info.value.code == 4


def test_alembic_failure_exits_5():
    """Alembic migration failure exits 5."""
    from unittest.mock import patch
    from scripts.gough_init import run_alembic_migrations
    import subprocess

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "alembic", stderr=b"Migration failed"
        )

        with pytest.raises(SystemExit) as exc_info:
            run_alembic_migrations()
        assert exc_info.value.code == 5


def test_genesis_row_failure_exits_6(mock_db_session):
    """Genesis audit row insert failure exits 6.

    Patches ``scripts.gough_init.insert_genesis_row`` (where it's called),
    not ``app.security.audit_chain.insert_genesis_row`` (where it's
    defined) -- ``scripts/gough_init.py`` does ``from
    app.security.audit_chain import insert_genesis_row``, a name binding
    local to ``scripts.gough_init``'s own module namespace that patching
    the source module's attribute never touches (standard "patch where it's
    used" mocking pitfall). The old patch target incidentally still made
    this test pass, but only because it fell through to the REAL
    ``insert_genesis_row`` running against a bare ``MagicMock`` session,
    which raised a Pydantic validation error for an unrelated reason (a
    ``MagicMock`` isn't valid ``bytes`` for ``AuditEvent.hash``) -- not
    because of the ``side_effect`` this test sets up. Task 8a's
    ``_as_bytes32`` normalization no longer raises there (it now
    defensively falls back to ``ZERO_HASH`` for a non-bytes hash column,
    correctly matching what real Postgres always returns), which is what
    surfaced this pre-existing wrong-target patch.
    """
    from unittest.mock import patch
    from scripts.gough_init import insert_genesis_audit_row

    with patch("scripts.gough_init.insert_genesis_row") as mock_insert:
        mock_insert.side_effect = Exception("DB error")

        with pytest.raises(SystemExit) as exc_info:
            insert_genesis_audit_row(mock_db_session, "test-cluster")
        assert exc_info.value.code == 6
        mock_insert.assert_called_once_with(mock_db_session, "test-cluster")


def test_vault_transit_failure_exits_7(mock_vault_client):
    """Vault transit key creation failure exits 7."""
    from scripts.gough_init import bootstrap_vault

    mock_vault_client.health.return_value = {"sealed": False}
    mock_vault_client._hvac_client.secrets.transit.create_key.side_effect = Exception(
        "Transit error"
    )

    with pytest.raises(SystemExit) as exc_info:
        bootstrap_vault(mock_vault_client, "test-cluster", "password")
    assert exc_info.value.code == 7


def test_idempotent_transit_key_creation(mock_vault_client):
    """Transit key idempotency: already-exists is OK."""
    from scripts.gough_init import bootstrap_vault

    mock_vault_client.health.return_value = {"sealed": False}
    # First key fails with "already exists"; second succeeds
    mock_vault_client._hvac_client.secrets.transit.create_key.side_effect = [
        Exception("already exists"),
        None,
    ]
    mock_vault_client.kv_write = MagicMock()

    keys_created = bootstrap_vault(mock_vault_client, "test-cluster", "password")
    # Both keys logged as existing or created; no error
    assert keys_created >= 0


def test_lxd_password_is_128_alphanumeric_chars():
    """LXD password is exactly 128 alphanumeric characters."""
    from scripts.gough_init import generate_lxd_password

    password = generate_lxd_password()
    assert len(password) == 128
    assert password.isalnum()


def test_lxd_password_masked_in_summary_output(capsys):
    """Summary output shows masked password, never full."""
    from scripts.gough_init import print_summary

    full_password = "a" * 128
    print_summary("test-cluster", full_password, 2, 5)

    captured = capsys.readouterr()
    assert full_password not in captured.out
    assert "aaaa...aaaa" in captured.out


def test_cluster_id_generated_when_not_provided():
    """Cluster ID is generated as UUIDv4 when not provided."""
    from scripts.gough_init import detect_or_generate_cluster_id

    cid = detect_or_generate_cluster_id(None)
    # Verify it's a valid UUID
    uuid.UUID(cid)


def test_cluster_id_used_when_provided():
    """Explicit cluster ID is used if provided."""
    from scripts.gough_init import detect_or_generate_cluster_id

    explicit_id = str(uuid.uuid4())
    cid = detect_or_generate_cluster_id(explicit_id)
    assert cid == explicit_id


def test_cluster_json_atomic_write(tmp_path):
    """cluster.json is written atomically via temp file."""
    from unittest.mock import patch
    from scripts.gough_init import persist_cluster_json

    target_path = tmp_path / "cluster.json"
    with patch("pathlib.Path") as mock_path_class:
        mock_path_obj = MagicMock()
        mock_path_obj.parent.mkdir = MagicMock()
        mock_path_class.return_value = mock_path_obj

        # Mock the actual write (test that temp file is used)
        with patch("tempfile.NamedTemporaryFile") as mock_temp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/cluster.tmp"
            mock_temp.return_value = mock_file

            with patch("os.replace") as mock_replace:
                persist_cluster_json("test-cluster", "5.21/stable", "bare-metal")
                # Verify atomic rename was called
                mock_replace.assert_called_once()


def test_summary_includes_required_fields(capsys):
    """Summary output includes cluster-id, password, keys, registrations."""
    from scripts.gough_init import print_summary

    print_summary("abc-123", "p" * 128, 2, 5)

    captured = capsys.readouterr()
    assert "abc-123" in captured.out
    assert "Vault Transit Keys" in captured.out
    assert "SPIRE Workload Regs" in captured.out
    assert "2" in captured.out
    assert "5" in captured.out


def test_no_secret_logging(caplog):
    """Secrets (full passwords/tokens) are never logged."""
    from scripts.gough_init import set_lxd_password
    from unittest.mock import patch
    import logging

    caplog.set_level(logging.DEBUG)
    full_password = "secret_password_value_12345"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = None
        set_lxd_password(full_password)

    # Check that full password never appears in logs
    assert full_password not in caplog.text


def test_detect_host_context_bare_metal(tmp_path):
    """Host detection: bare-metal when no DMI vendor or unknown."""
    from unittest.mock import patch
    from scripts.gough_init import detect_host_context

    with patch("pathlib.Path.exists", return_value=False):
        tier = detect_host_context()
        assert tier == "bare-metal"


def test_detect_host_context_cloud_vm(tmp_path):
    """Host detection: cloud-vm when EC2/GCP vendor found."""
    from unittest.mock import patch, MagicMock
    from scripts.gough_init import detect_host_context
    from pathlib import Path

    mock_path = MagicMock(spec=Path)
    mock_path.exists.return_value = True
    mock_path.read_text.return_value = "Amazon EC2"

    with patch("scripts.gough_init.Path", return_value=mock_path):
        tier = detect_host_context()
        assert tier == "cloud-vm"


def test_mask_password():
    """Password mask: first 4 and last 4 chars with ellipsis."""
    from scripts.gough_init import mask_password

    password = "abcdefghijklmnopqrstuvwxyz"
    masked = mask_password(password)
    assert masked == "abcd...wxyz"
    assert password not in masked


def test_reinit_requires_reason():
    """--reinit without --reason fails argument parsing."""
    from scripts.gough_init import main
    from unittest.mock import patch
    import sys

    with patch("sys.argv", ["gough_init.py", "--reinit"]):
        with pytest.raises(SystemExit):
            main()


def test_install_lxd_snap_already_present():
    """LXD snap install is skipped if already installed."""
    from unittest.mock import patch
    from scripts.gough_init import install_lxd_snap

    with patch("subprocess.run") as mock_run:
        # which lxd succeeds
        mock_run.return_value.returncode = 0

        install_lxd_snap("5.21/stable")

        # snap install should not be called
        calls = [c for c in mock_run.call_args_list if "snap" in str(c)]
        assert len(calls) == 0


def test_main_returns_zero_on_success(tmp_path):
    """Main function returns 0 on successful bootstrap."""
    from unittest.mock import patch, MagicMock
    from scripts.gough_init import main

    with patch("scripts.gough_init.detect_host_context", return_value="bare-metal"):
        with patch("scripts.gough_init.check_preexisting_state", return_value=False):
            with patch(
                "scripts.gough_init.detect_or_generate_cluster_id", return_value="test"
            ):
                with patch("scripts.gough_init.install_lxd_snap"):
                    with patch("scripts.gough_init.init_lxd_cluster"):
                        with patch(
                            "scripts.gough_init.generate_lxd_password",
                            return_value="p" * 128,
                        ):
                            with patch("scripts.gough_init.set_lxd_password"):
                                with patch(
                                    "scripts.gough_init.VaultClient", MagicMock()
                                ):
                                    with patch(
                                        "scripts.gough_init.bootstrap_vault",
                                        return_value=2,
                                    ):
                                        with patch(
                                            "scripts.gough_init.bootstrap_spire",
                                            return_value=5,
                                        ):
                                            with patch(
                                                "scripts.gough_init.persist_cluster_json"
                                            ):
                                                with patch(
                                                    "scripts.gough_init.print_summary"
                                                ):
                                                    with patch(
                                                        "sys.argv", ["gough_init.py"]
                                                    ):
                                                        result = main()
                                                        assert result == 0
