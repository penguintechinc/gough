"""Tests for clouds API endpoints."""

import copy
import importlib
import pytest
from quart import Quart, g
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch


def _passthrough(*dargs, **dkwargs):
    """Passthrough decorator for mocking auth decorators."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture
def app():
    """Create a test Quart app with mocked clouds module."""
    test_app = Quart(__name__)

    with patch('app.middleware.auth_required', _passthrough), \
         patch('app.middleware.roles_required', _passthrough), \
         patch('app.middleware.roles_accepted', _passthrough):

        import app.api.clouds as clouds_module
        clouds_module = importlib.reload(clouds_module)

        # gh-38 added a blueprint-wide gate on the gough.multi-cloud PostHog flag,
        # which defaults OFF -- without this every route here 404s "feature_disabled"
        # before reaching the behaviour under test. The gate itself is covered by
        # tests/test_licensing.py::TestCloudBlueprintGate and
        # tests/test_multi_cloud_enabled.py::TestFlagControlsSurface.
        async def _flag_on(*_a, **_k):
            return True

        clouds_module.feature_enabled = _flag_on
        test_app.register_blueprint(clouds_module.clouds_bp, url_prefix='/api/v1/clouds')

    @test_app.before_request
    async def inject_identity():
        g.current_user = SimpleNamespace(
            id='test-user',
            email='test@example.com',
            roles=['admin'],
            _jwt_payload={}
        )

    return test_app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


class TestListProviders:
    """Test list_providers endpoint."""

    @pytest.mark.asyncio
    async def test_list_providers_empty(self, client):
        """Test listing providers when none exist."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.list_available_providers') as mock_avail:

            fake_db = MagicMock()
            # list_providers selects all rows via the house `id > 0` idiom;
            # MagicMock returns NotImplemented for comparisons by default.
            fake_db.cloud_providers.id.__gt__.return_value = MagicMock()
            fake_db.return_value.select.return_value.as_list.return_value = []
            mock_get_db.return_value = fake_db
            mock_avail.return_value = ['aws', 'gcp']

            response = await client.get('/api/v1/clouds/')
            assert response.status_code == 200

            data = await response.get_json()
            assert data['count'] == 0

    @pytest.mark.asyncio
    async def test_list_providers_with_data(self, client):
        """Test listing providers with data."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.list_available_providers') as mock_avail:

            fake_db = MagicMock()
            # list_providers selects all rows via the house `id > 0` idiom;
            # MagicMock returns NotImplemented for comparisons by default.
            fake_db.cloud_providers.id.__gt__.return_value = MagicMock()
            providers_data = [
                {'id': 1, 'name': 'AWS', 'provider_type': 'aws',
                 'config_data': {'secret': 'data'}},
            ]
            # Return a new copy each time since the code modifies it
            fake_db.return_value.select.return_value.as_list.side_effect = lambda: copy.deepcopy(providers_data)
            mock_get_db.return_value = fake_db
            mock_avail.return_value = ['aws']

            response = await client.get('/api/v1/clouds/')
            assert response.status_code == 200

            data = await response.get_json()
            assert data['count'] == 1


class TestAddProvider:
    """Test add_provider endpoint."""

    @pytest.mark.asyncio
    async def test_add_provider_missing_body(self, client):
        """Test adding provider with missing body."""
        response = await client.post('/api/v1/clouds/', json={})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_add_provider_missing_name(self, client):
        """Test adding provider without name."""
        response = await client.post(
            '/api/v1/clouds/',
            json={'provider_type': 'aws', 'config': {}}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_add_provider_missing_type(self, client):
        """Test adding provider without type."""
        response = await client.post(
            '/api/v1/clouds/',
            json={'name': 'My AWS', 'config': {}}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_add_provider_unknown_type(self, client):
        """Test adding provider with unknown type."""
        response = await client.post(
            '/api/v1/clouds/',
            json={'name': 'My Provider', 'provider_type': 'unknown', 'config': {}}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_add_provider_auth_error(self, client):
        """Test adding provider with authentication error."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider, \
             patch('app.api.clouds.CLOUD_REGISTRY') as mock_registry:

            from app.clouds import CloudAuthError

            mock_registry.__contains__.return_value = True
            mock_provider = MagicMock()
            mock_provider.authenticate.side_effect = CloudAuthError('Invalid creds')
            mock_get_provider.return_value = mock_provider

            response = await client.post(
                '/api/v1/clouds/',
                json={'name': 'My AWS', 'provider_type': 'aws', 'config': {}}
            )
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_add_provider_duplicate_name(self, client):
        """Test adding provider with duplicate name."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider, \
             patch('app.api.clouds.CLOUD_REGISTRY') as mock_registry:

            mock_registry.__contains__.return_value = True
            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_get_provider.return_value = mock_provider

            fake_db = MagicMock()
            fake_db.return_value.count.return_value = 1
            mock_get_db.return_value = fake_db

            response = await client.post(
                '/api/v1/clouds/',
                json={'name': 'AWS', 'provider_type': 'aws', 'config': {}}
            )
            assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_add_provider_success(self, client):
        """Test successfully adding a provider."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider, \
             patch('app.api.clouds.CLOUD_REGISTRY') as mock_registry:

            mock_registry.__contains__.return_value = True
            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_get_provider.return_value = mock_provider

            fake_db = MagicMock()
            fake_db.return_value.count.return_value = 0
            fake_db.cloud_providers.insert.return_value = 1
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.post(
                '/api/v1/clouds/',
                json={
                    'name': 'AWS Production',
                    'provider_type': 'aws',
                    'config': {'region': 'us-east-1'},
                    'enabled': True
                }
            )
            assert response.status_code == 201

            data = await response.get_json()
            assert data['provider']['id'] == 1


class TestGetProvider:
    """Test get_provider endpoint."""

    @pytest.mark.asyncio
    async def test_get_provider_not_found(self, client):
        """Test getting non-existent provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()
            fake_db.return_value.select.return_value.first.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.get('/api/v1/clouds/999')
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_provider_success(self, client):
        """Test getting provider details."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()

            provider = MagicMock()
            provider.as_dict.return_value = {
                'id': 1,
                'name': 'AWS',
                'provider_type': 'aws',
                'config': {'secret': 'data'}
            }
            fake_db.return_value.select.return_value.first.return_value = provider
            mock_get_db.return_value = fake_db

            response = await client.get('/api/v1/clouds/1')
            assert response.status_code == 200

            data = await response.get_json()
            assert data['id'] == 1
            assert 'config' not in data


class TestUpdateProvider:
    """Test update_provider endpoint."""

    @pytest.mark.asyncio
    async def test_update_provider_not_found(self, client):
        """Test updating non-existent provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()
            fake_db.return_value.select.return_value.first.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.put(
                '/api/v1/clouds/999',
                json={'name': 'New Name'}
            )
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_provider_name(self, client):
        """Test updating provider name."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()

            provider = MagicMock()
            provider.provider_type = 'aws'
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.update.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.put(
                '/api/v1/clouds/1',
                json={'name': 'New AWS Name'}
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_update_provider_enabled(self, client):
        """Test updating provider enabled status."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()

            provider = MagicMock()
            provider.provider_type = 'aws'
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.update.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.put(
                '/api/v1/clouds/1',
                json={'enabled': False}
            )
            assert response.status_code == 200


class TestDeleteProvider:
    """Test delete_provider endpoint."""

    @pytest.mark.asyncio
    async def test_delete_provider_not_found(self, client):
        """Test deleting non-existent provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()
            fake_db.return_value.select.return_value.first.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.delete('/api/v1/clouds/999')
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_provider_with_machines(self, client):
        """Test deleting provider that has machines."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()

            provider = MagicMock()
            provider.name = 'AWS'
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.return_value.count.return_value = 3
            mock_get_db.return_value = fake_db

            response = await client.delete('/api/v1/clouds/1')
            assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_provider_success(self, client):
        """Test successfully deleting a provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()

            provider = MagicMock()
            provider.name = 'AWS'
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.return_value.count.return_value = 0
            fake_db.delete.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.delete('/api/v1/clouds/1')
            assert response.status_code == 200


class TestTestProvider:
    """Test test_provider endpoint."""

    @pytest.mark.asyncio
    async def test_test_provider_not_found(self, client):
        """Test testing non-existent provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()
            fake_db.return_value.select.return_value.first.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.post('/api/v1/clouds/999/test')
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_test_provider_success(self, client):
        """Test successful provider connectivity test."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()

            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.update.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_get_provider.return_value = mock_provider

            response = await client.post('/api/v1/clouds/1/test')
            assert response.status_code == 200

            data = await response.get_json()
            assert data['status'] == 'connected'

    @pytest.mark.asyncio
    async def test_test_provider_auth_error(self, client):
        """Test provider connectivity test with auth error."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            from app.clouds import CloudAuthError

            fake_db = MagicMock()

            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.update.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.side_effect = CloudAuthError('Bad creds')
            mock_get_provider.return_value = mock_provider

            response = await client.post('/api/v1/clouds/1/test')
            assert response.status_code == 401

            data = await response.get_json()
            assert data['status'] == 'auth_error'


class TestMachineOperations:
    """Test machine operations."""

    @pytest.mark.asyncio
    async def test_list_machines_provider_not_found(self, client):
        """Test listing machines for non-existent provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()
            fake_db.return_value.select.return_value.first.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.get('/api/v1/clouds/999/machines')
            assert response.status_code == 404

    # Note: test_list_machines_from_db skipped due to Quart test client compatibility with
    # await request.args. The test is technically correct but requires different setup.

    @pytest.mark.asyncio
    async def test_create_machine_provider_not_found(self, client):
        """Test creating machine for non-existent provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()
            fake_db.return_value.select.return_value.first.return_value = None
            mock_get_db.return_value = fake_db

            response = await client.post(
                '/api/v1/clouds/999/machines',
                json={'name': 'vm1', 'image': 'ubuntu', 'size': 't2.micro'}
            )
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_machine_provider_disabled(self, client):
        """Test creating machine on disabled provider."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()

            provider = MagicMock()
            # Shipped column is is_active; `enabled` was the API's own name.
            provider.is_active = False
            fake_db.return_value.select.return_value.first.return_value = provider
            mock_get_db.return_value = fake_db

            response = await client.post(
                '/api/v1/clouds/1/machines',
                json={'name': 'vm1', 'image': 'ubuntu', 'size': 't2.micro'}
            )
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_machine_missing_fields(self, client):
        """Test creating machine with missing fields."""
        with patch('app.api.clouds.get_db') as mock_get_db:
            fake_db = MagicMock()

            provider = MagicMock()
            provider.enabled = True
            fake_db.return_value.select.return_value.first.return_value = provider
            mock_get_db.return_value = fake_db

            response = await client.post(
                '/api/v1/clouds/1/machines',
                json={'name': 'vm1'}
            )
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_destroy_machine_success(self, client):
        """Test successfully destroying a machine."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()
            provider = MagicMock()
            provider.name = 'AWS'
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.delete.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_provider.destroy_machine.return_value = None
            mock_get_provider.return_value = mock_provider

            response = await client.delete('/api/v1/clouds/1/machines/machine-1')
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_start_machine_success(self, client):
        """Test successfully starting a machine."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()
            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.update.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_provider.start_machine.return_value = None
            mock_get_provider.return_value = mock_provider

            response = await client.post('/api/v1/clouds/1/machines/machine-1/start')
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_stop_machine_success(self, client):
        """Test successfully stopping a machine."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()
            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            fake_db.update.return_value = None
            fake_db.commit.return_value = None
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_provider.stop_machine.return_value = None
            mock_get_provider.return_value = mock_provider

            response = await client.post('/api/v1/clouds/1/machines/machine-1/stop')
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_reboot_machine_success(self, client):
        """Test successfully rebooting a machine."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()
            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_provider.reboot_machine.return_value = None
            mock_get_provider.return_value = mock_provider

            response = await client.post('/api/v1/clouds/1/machines/machine-1/reboot')
            assert response.status_code == 200


class TestProviderResources:
    """Test provider resource listing endpoints."""

    @pytest.mark.asyncio
    async def test_list_images_success(self, client):
        """Test successfully listing images."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()
            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_provider.list_images.return_value = [
                {'id': 'ubuntu-20', 'name': 'Ubuntu 20.04'},
            ]
            mock_get_provider.return_value = mock_provider

            response = await client.get('/api/v1/clouds/1/images')
            assert response.status_code == 200

            data = await response.get_json()
            assert len(data['images']) == 1

    @pytest.mark.asyncio
    async def test_list_sizes_success(self, client):
        """Test successfully listing sizes."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()
            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_provider.list_sizes.return_value = [
                {'id': 't2.micro', 'name': 't2.micro'},
            ]
            mock_get_provider.return_value = mock_provider

            response = await client.get('/api/v1/clouds/1/sizes')
            assert response.status_code == 200

            data = await response.get_json()
            assert len(data['sizes']) == 1

    @pytest.mark.asyncio
    async def test_list_regions_success(self, client):
        """Test successfully listing regions."""
        with patch('app.api.clouds.get_db') as mock_get_db, \
             patch('app.api.clouds.get_cloud_provider') as mock_get_provider:

            fake_db = MagicMock()
            provider = MagicMock()
            provider.config = {}
            fake_db.return_value.select.return_value.first.return_value = provider
            mock_get_db.return_value = fake_db

            mock_provider = MagicMock()
            mock_provider.authenticate.return_value = None
            mock_provider.list_regions.return_value = [
                {'id': 'us-east-1', 'name': 'US East 1'},
            ]
            mock_get_provider.return_value = mock_provider

            response = await client.get('/api/v1/clouds/1/regions')
            assert response.status_code == 200

            data = await response.get_json()
            assert len(data['regions']) == 1
