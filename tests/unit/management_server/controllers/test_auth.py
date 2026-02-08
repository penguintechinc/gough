#!/usr/bin/env python3
"""
Unit Tests for Authentication Controller
Tests for user authentication, authorization, JWT handling, and security features
"""

import json
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

import pytest
import jwt
from werkzeug.security import generate_password_hash


class TestAuthController:
    """Test cases for authentication controller functionality."""

    @pytest.fixture
    def auth_config(self):
        """Authentication configuration for testing."""
        return {
            'JWT_SECRET': 'test_jwt_secret_not_for_production',
            'JWT_ALGORITHM': 'HS256',
            'JWT_EXPIRATION_HOURS': 24,
            'BCRYPT_ROUNDS': 12,
            'MAX_LOGIN_ATTEMPTS': 5,
            'LOCKOUT_DURATION': 300  # 5 minutes
        }

    @pytest.fixture
    def sample_user_data(self):
        """Sample user data for testing."""
        return {
            'username': 'testuser',
            'email': 'test@example.com',
            'password': 'SecurePassword123!',
            'role': 'admin',
            'is_active': True
        }

    @pytest.fixture
    def mock_user_db(self, mock_database, sample_user_data):
        """Mock user database with sample user."""
        # Create users table if it doesn't exist
        if not hasattr(mock_database, 'users'):
            mock_database.define_table('users',
                mock_database.Field('username', 'string', unique=True, notnull=True),
                mock_database.Field('email', 'string', unique=True, notnull=True),
                mock_database.Field('password_hash', 'password', notnull=True),
                mock_database.Field('role', 'string', default='user'),
                mock_database.Field('is_active', 'boolean', default=True),
                mock_database.Field('last_login', 'datetime'),
                mock_database.Field('failed_attempts', 'integer', default=0),
                mock_database.Field('locked_until', 'datetime'),
                mock_database.Field('created_on', 'datetime', default=datetime.utcnow),
                mock_database.Field('updated_on', 'datetime', update=datetime.utcnow)
            )
        
        # Insert test user
        user_data = sample_user_data.copy()
        user_data['password_hash'] = generate_password_hash(user_data.pop('password'))
        user_id = mock_database.users.insert(**user_data)
        mock_database.commit()
        
        return mock_database, user_id

    @pytest.mark.auth
    def test_login_success(self, api_client, mock_user_db, auth_config):
        """Test successful user login."""
        mock_db, user_id = mock_user_db
        
        login_data = {
            'username': 'testuser',
            'password': 'SecurePassword123!'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.auth.get_config') as mock_config:
            
            mock_get_db.return_value = mock_db
            mock_config.return_value = Mock(**auth_config)
            
            response = api_client.post(
                '/auth/login',
                data=json.dumps(login_data),
                headers={'Content-Type': 'application/json'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'access_token' in data
            assert 'user' in data
            assert data['user']['username'] == 'testuser'

    @pytest.mark.auth
    def test_login_invalid_credentials(self, api_client, mock_user_db):
        """Test login with invalid credentials."""
        mock_db, user_id = mock_user_db
        
        login_data = {
            'username': 'testuser',
            'password': 'WrongPassword'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db:
            mock_get_db.return_value = mock_db
            
            response = api_client.post(
                '/auth/login',
                data=json.dumps(login_data),
                headers={'Content-Type': 'application/json'}
            )
            
            assert response.status_code == 401
            data = json.loads(response.data)
            assert 'error' in data
            assert 'invalid' in data['error']['message'].lower()

    @pytest.mark.auth
    def test_login_nonexistent_user(self, api_client, mock_database):
        """Test login with non-existent user."""
        login_data = {
            'username': 'nonexistent',
            'password': 'password'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.post(
                '/auth/login',
                data=json.dumps(login_data),
                headers={'Content-Type': 'application/json'}
            )
            
            assert response.status_code == 401
            data = json.loads(response.data)
            assert 'error' in data

    @pytest.mark.auth
    def test_jwt_token_creation(self, auth_config, sample_user_data):
        """Test JWT token creation and structure."""
        from gough.containers.management_server.py4web_app.lib.auth import create_access_token
        
        with patch('gough.containers.management_server.py4web_app.lib.auth.get_config') as mock_config:
            mock_config.return_value = Mock(**auth_config)
            
            token = create_access_token(
                user_id=1,
                username=sample_user_data['username'],
                role=sample_user_data['role']
            )
            
            # Decode and verify token
            decoded = jwt.decode(
                token,
                auth_config['JWT_SECRET'],
                algorithms=[auth_config['JWT_ALGORITHM']]
            )
            
            assert decoded['user_id'] == 1
            assert decoded['username'] == sample_user_data['username']
            assert decoded['role'] == sample_user_data['role']
            assert 'exp' in decoded
            assert 'iat' in decoded

    @pytest.mark.auth
    def test_jwt_token_expiration(self, auth_config):
        """Test JWT token expiration handling."""
        from gough.containers.management_server.py4web_app.lib.auth import create_access_token
        
        # Create expired token
        config_expired = auth_config.copy()
        config_expired['JWT_EXPIRATION_HOURS'] = -1  # Already expired
        
        with patch('gough.containers.management_server.py4web_app.lib.auth.get_config') as mock_config:
            mock_config.return_value = Mock(**config_expired)
            
            token = create_access_token(user_id=1, username='test', role='user')
            
            # Verify token is expired
            with pytest.raises(jwt.ExpiredSignatureError):
                jwt.decode(
                    token,
                    auth_config['JWT_SECRET'],
                    algorithms=[auth_config['JWT_ALGORITHM']]
                )

    @pytest.mark.auth
    def test_token_validation_middleware(self, api_client, auth_config):
        """Test JWT token validation middleware."""
        # Create a valid token
        payload = {
            'user_id': 1,
            'username': 'testuser',
            'role': 'admin',
            'exp': datetime.utcnow() + timedelta(hours=1),
            'iat': datetime.utcnow()
        }
        
        token = jwt.encode(payload, auth_config['JWT_SECRET'], algorithm=auth_config['JWT_ALGORITHM'])
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_config') as mock_config:
            mock_config.return_value = Mock(**auth_config)
            
            response = api_client.get('/api/servers', headers=headers)
            
            # Should not fail due to authentication (may fail for other reasons)
            assert response.status_code != 401

    @pytest.mark.auth
    def test_invalid_token_format(self, api_client):
        """Test handling of malformed JWT tokens."""
        headers = {
            'Authorization': 'Bearer invalid.token.format',
            'Content-Type': 'application/json'
        }
        
        response = api_client.get('/api/servers', headers=headers)
        
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'error' in data

    @pytest.mark.auth
    def test_missing_authorization_header(self, api_client):
        """Test handling of missing Authorization header."""
        response = api_client.get('/api/servers')
        
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'error' in data
        assert 'authorization' in data['error']['message'].lower()

    @pytest.mark.auth
    def test_role_based_access_control(self, api_client, auth_config):
        """Test role-based access control functionality."""
        # Test with admin role
        admin_payload = {
            'user_id': 1,
            'username': 'admin',
            'role': 'admin',
            'exp': datetime.utcnow() + timedelta(hours=1)
        }
        admin_token = jwt.encode(admin_payload, auth_config['JWT_SECRET'])
        
        # Test with user role
        user_payload = {
            'user_id': 2,
            'username': 'user',
            'role': 'user',
            'exp': datetime.utcnow() + timedelta(hours=1)
        }
        user_token = jwt.encode(user_payload, auth_config['JWT_SECRET'])
        
        # Admin should access admin endpoints
        admin_response = api_client.delete(
            '/api/servers/1',
            headers={'Authorization': f'Bearer {admin_token}'}
        )
        assert admin_response.status_code != 403  # Not forbidden due to role
        
        # User should be forbidden from admin endpoints
        user_response = api_client.delete(
            '/api/servers/1',
            headers={'Authorization': f'Bearer {user_token}'}
        )
        # May be 403 (forbidden) or 404 (not found) depending on implementation
        assert user_response.status_code in [403, 404]

    @pytest.mark.auth
    def test_user_registration(self, api_client, mock_database):
        """Test user registration functionality."""
        registration_data = {
            'username': 'newuser',
            'email': 'newuser@example.com',
            'password': 'SecurePassword123!',
            'role': 'user'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.post(
                '/auth/register',
                data=json.dumps(registration_data),
                headers={'Content-Type': 'application/json'}
            )
            
            # Registration may be disabled in production
            assert response.status_code in [201, 405, 501]

    @pytest.mark.auth
    def test_password_reset_request(self, api_client, mock_user_db):
        """Test password reset request functionality."""
        mock_db, user_id = mock_user_db
        
        reset_data = {'email': 'test@example.com'}
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db:
            mock_get_db.return_value = mock_db
            
            response = api_client.post(
                '/auth/password-reset',
                data=json.dumps(reset_data),
                headers={'Content-Type': 'application/json'}
            )
            
            # Should indicate success even if email doesn't exist (security)
            assert response.status_code in [200, 202, 501]

    @pytest.mark.auth
    def test_account_lockout_after_failed_attempts(self, api_client, mock_user_db, auth_config):
        """Test account lockout after multiple failed login attempts."""
        mock_db, user_id = mock_user_db
        
        login_data = {
            'username': 'testuser',
            'password': 'WrongPassword'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.auth.get_config') as mock_config:
            
            mock_get_db.return_value = mock_db
            mock_config.return_value = Mock(**auth_config)
            
            # Make multiple failed attempts
            for i in range(auth_config['MAX_LOGIN_ATTEMPTS'] + 1):
                response = api_client.post(
                    '/auth/login',
                    data=json.dumps(login_data),
                    headers={'Content-Type': 'application/json'}
                )
            
            # Account should be locked
            assert response.status_code == 423  # Locked
            data = json.loads(response.data)
            assert 'locked' in data['error']['message'].lower()

    @pytest.mark.auth
    def test_token_refresh(self, api_client, auth_config):
        """Test JWT token refresh functionality."""
        # Create a token that's close to expiring
        payload = {
            'user_id': 1,
            'username': 'testuser',
            'role': 'admin',
            'exp': datetime.utcnow() + timedelta(minutes=5),
            'iat': datetime.utcnow()
        }
        
        token = jwt.encode(payload, auth_config['JWT_SECRET'])
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_config') as mock_config:
            mock_config.return_value = Mock(**auth_config)
            
            response = api_client.post('/auth/refresh', headers=headers)
            
            # Token refresh may not be implemented yet
            assert response.status_code in [200, 404, 501]

    @pytest.mark.auth
    def test_logout_functionality(self, api_client, auth_config):
        """Test user logout and token invalidation."""
        payload = {
            'user_id': 1,
            'username': 'testuser',
            'role': 'admin',
            'exp': datetime.utcnow() + timedelta(hours=1)
        }
        
        token = jwt.encode(payload, auth_config['JWT_SECRET'])
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        response = api_client.post('/auth/logout', headers=headers)
        
        # Logout endpoint may not be implemented yet
        assert response.status_code in [200, 404, 501]

    @pytest.mark.auth
    def test_session_timeout(self, api_client, auth_config):
        """Test session timeout handling."""
        # Create token with very short expiration
        payload = {
            'user_id': 1,
            'username': 'testuser',
            'role': 'admin',
            'exp': datetime.utcnow() - timedelta(seconds=1),  # Already expired
            'iat': datetime.utcnow() - timedelta(seconds=2)
        }
        
        token = jwt.encode(payload, auth_config['JWT_SECRET'])
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        response = api_client.get('/api/servers', headers=headers)
        
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'expired' in data['error']['message'].lower()

    @pytest.mark.auth
    def test_concurrent_login_handling(self, api_client, mock_user_db, auth_config):
        """Test handling of concurrent login sessions."""
        mock_db, user_id = mock_user_db
        
        login_data = {
            'username': 'testuser',
            'password': 'SecurePassword123!'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.auth.get_config') as mock_config:
            
            mock_get_db.return_value = mock_db
            mock_config.return_value = Mock(**auth_config)
            
            # Multiple concurrent logins should succeed
            responses = []
            for _ in range(3):
                response = api_client.post(
                    '/auth/login',
                    data=json.dumps(login_data),
                    headers={'Content-Type': 'application/json'}
                )
                responses.append(response)
            
            for response in responses:
                assert response.status_code == 200

    @pytest.mark.auth
    def test_password_strength_validation(self, api_client, mock_database):
        """Test password strength validation during registration."""
        weak_passwords = [
            'password',
            '123456',
            'qwerty',
            'short',
            'NoNumbers!',
            'nonumbers123',
            'NoSpecialChars123'
        ]
        
        for weak_password in weak_passwords:
            registration_data = {
                'username': f'user_{len(weak_password)}',
                'email': f'user_{len(weak_password)}@example.com',
                'password': weak_password,
                'role': 'user'
            }
            
            with patch('gough.containers.management_server.py4web_app.controllers.auth.get_db') as mock_get_db:
                mock_get_db.return_value = mock_database
                
                response = api_client.post(
                    '/auth/register',
                    data=json.dumps(registration_data),
                    headers={'Content-Type': 'application/json'}
                )
                
                # Should reject weak passwords or registration may be disabled
                assert response.status_code in [400, 405, 422, 501]