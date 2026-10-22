"""Tests for EncryptedDBSecretsManager."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cryptography.fernet import Fernet

from app.secrets.encrypted_db import EncryptedDBSecretsManager
from app.secrets.base import SecretNotFoundError, SecretsManagerError


@pytest.fixture
def manager(mock_current_app):
    return EncryptedDBSecretsManager()


def test_generate_key():
    """Test key generation."""
    key = EncryptedDBSecretsManager.generate_key()
    assert isinstance(key, str)
    assert Fernet(key.encode()) is not None


def test_fernet_property_with_key(mock_current_app):
    """Test fernet property with configured key."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()
    fernet = manager.fernet
    assert fernet is not None
    assert isinstance(fernet, Fernet)


def test_fernet_property_generates_key_when_missing(mock_current_app, caplog):
    """Test fernet generates key when not configured."""
    mock_current_app.config.pop("ENCRYPTION_KEY", None)
    manager = EncryptedDBSecretsManager()
    fernet = manager.fernet
    assert fernet is not None
    assert "No ENCRYPTION_KEY set" in caplog.text


def test_fernet_property_derives_key_from_password(mock_current_app, caplog):
    """Test fernet derives key from invalid format."""
    mock_current_app.config["ENCRYPTION_KEY"] = "not-a-fernet-key"
    manager = EncryptedDBSecretsManager()
    fernet = manager.fernet
    assert fernet is not None
    assert "Invalid Fernet key format" in caplog.text


def test_derive_key(manager):
    """Test key derivation."""
    derived = manager._derive_key("password")
    assert isinstance(derived, bytes)
    assert Fernet(derived) is not None


def test_encrypt_decrypt(mock_current_app, manager):
    """Test encryption/decryption roundtrip."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    data = {"user": "admin", "pass": "secret"}
    encrypted = manager._encrypt(data)
    decrypted = manager._decrypt(encrypted)
    assert decrypted == data


def test_decrypt_invalid_token(mock_current_app, manager):
    """Test decrypt with invalid data."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    with pytest.raises(SecretsManagerError) as exc:
        manager._decrypt("invalid")
    assert "Failed to decrypt" in str(exc.value)


def test_decrypt_invalid_json(mock_current_app, manager):
    """Test decrypt with non-JSON data."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    fernet = manager.fernet
    encrypted = fernet.encrypt(b"not-json").decode()

    with pytest.raises(SecretsManagerError) as exc:
        manager._decrypt(encrypted)
    assert "Failed to parse" in str(exc.value)


@pytest.mark.asyncio
async def test_get_secret(mock_current_app, manager):
    """Test get_secret."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    data = {"key": "value"}
    encrypted_data = manager._encrypt(data)
    assert isinstance(encrypted_data, str), "encrypted_data should be string"

    # Create the complete mock chain: db(filter).select().first()
    mock_row = MagicMock()
    mock_row.encrypted_data = encrypted_data

    # db().select().first() returns the row
    select_mock = MagicMock()
    select_mock.first.return_value = mock_row

    # db(filter) returns something that has .select()
    db_call_result = MagicMock()
    db_call_result.select.return_value = select_mock

    # db() as callable returns the filtered result
    mock_db = MagicMock()
    mock_db.return_value = db_call_result
    # Also add encrypted_secrets attribute for the filter expression
    mock_db.encrypted_secrets = MagicMock()

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_db):
        result = await manager.get_secret("test/path")
        assert result == data


@pytest.mark.asyncio
async def test_get_secret_not_found(mock_current_app, manager):
    """Test get_secret raises when not found."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    select_mock = MagicMock()
    select_mock.first.return_value = None
    db_call_result = MagicMock()
    db_call_result.select.return_value = select_mock
    mock_db = MagicMock()
    mock_db.return_value = db_call_result
    mock_db.encrypted_secrets = MagicMock()

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_db):
        with pytest.raises(SecretNotFoundError):
            await manager.get_secret("missing")


@pytest.mark.asyncio
async def test_set_secret_create(mock_current_app, manager):
    """Test set_secret creates new secret."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    select_mock = MagicMock()
    select_mock.first.return_value = None
    db_call_result = MagicMock()
    db_call_result.select.return_value = select_mock
    mock_db = MagicMock()
    mock_db.return_value = db_call_result
    mock_db.encrypted_secrets = MagicMock()

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_db):
        result = await manager.set_secret("new", {"a": "b"})
        assert result is True
        # Should call insert on encrypted_secrets table
        assert mock_db.encrypted_secrets.insert.called


@pytest.mark.asyncio
async def test_set_secret_update(mock_current_app, manager):
    """Test set_secret updates existing secret."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    mock_row = MagicMock()
    select_mock = MagicMock()
    select_mock.first.return_value = mock_row
    db_call_result = MagicMock()
    db_call_result.select.return_value = select_mock
    mock_db = MagicMock()
    mock_db.return_value = db_call_result
    mock_db.encrypted_secrets = MagicMock()

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_db):
        result = await manager.set_secret("existing", {"c": "d"})
        assert result is True
        # update() should be called
        assert db_call_result.update.called or mock_db.update.called


@pytest.mark.asyncio
async def test_delete_secret(mock_current_app, manager):
    """Test delete_secret."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    mock_query = MagicMock()
    mock_query.delete.return_value = 1

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_query):
        result = await manager.delete_secret("path")
        assert result is True


@pytest.mark.asyncio
async def test_delete_secret_not_found(mock_current_app, manager):
    """Test delete_secret returns False when not found."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    # db(filter).delete() returns 0 (no rows deleted)
    db_call_result = MagicMock()
    db_call_result.delete.return_value = 0
    mock_db = MagicMock()
    mock_db.return_value = db_call_result
    mock_db.encrypted_secrets = MagicMock()

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_db):
        result = await manager.delete_secret("missing")
        assert result is False


@pytest.mark.asyncio
async def test_list_secrets(mock_current_app, manager):
    """Test list_secrets."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    rows = [MagicMock(path="s1"), MagicMock(path="s2")]
    # db(db.encrypted_secrets).select() returns rows
    db_call_result = MagicMock()
    db_call_result.select.return_value = rows
    mock_db = MagicMock()
    mock_db.return_value = db_call_result
    mock_db.encrypted_secrets = MagicMock()

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_db):
        result = await manager.list_secrets()
        assert result == ["s1", "s2"]


@pytest.mark.asyncio
async def test_list_secrets_with_prefix(mock_current_app, manager):
    """Test list_secrets with prefix."""
    key = EncryptedDBSecretsManager.generate_key()
    mock_current_app.config["ENCRYPTION_KEY"] = key
    manager = EncryptedDBSecretsManager()

    rows = [MagicMock(path="cloud/aws/key")]
    # db(db.encrypted_secrets.path.startswith(path)).select() returns rows
    db_call_result = MagicMock()
    db_call_result.select.return_value = rows
    mock_db = MagicMock()
    mock_db.return_value = db_call_result
    # Need to mock the path attribute
    mock_path = MagicMock()
    mock_path.startswith.return_value = True
    mock_encrypted_secrets = MagicMock()
    mock_encrypted_secrets.path = mock_path
    mock_db.encrypted_secrets = mock_encrypted_secrets

    with patch("app.secrets.encrypted_db.get_db", return_value=mock_db):
        result = await manager.list_secrets("cloud")
        assert result == ["cloud/aws/key"]
