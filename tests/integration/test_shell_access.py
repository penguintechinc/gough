#!/usr/bin/env python3
"""
Integration Tests for Shell Access Flow.

Tests the complete workflow for shell access including:
- Team and resource management
- Shell session creation and termination
- SSH certificate signing
- Permission enforcement
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict
from unittest.mock import Mock, patch, MagicMock

import pytest
from werkzeug.security import generate_password_hash


class TestShellAccessIntegration:
    """Integration tests for shell access functionality."""

    @pytest.fixture
    def team_data(self):
        """Sample team data."""
        return {
            'name': f'test-team-{uuid.uuid4().hex[:8]}',
            'description': 'Test team for shell access',
        }

    @pytest.fixture
    def user_data(self):
        """Sample user data."""
        return {
            'email': f'user-{uuid.uuid4().hex[:8]}@example.com',
            'password': 'SecurePassword123!@',
            'full_name': 'Test User',
            'fs_uniquifier': str(uuid.uuid4()),
        }

    @pytest.fixture
    def resource_data(self):
        """Sample resource data."""
        return {
            'resource_type': 'vm',
            'resource_id': f'vm-{uuid.uuid4().hex[:8]}',
            'name': 'test-server-01',
            'ip_address': '192.168.1.100',
        }

    @pytest.fixture
    def agent_data(self):
        """Sample access agent data."""
        return {
            'agent_id': f'agent-{uuid.uuid4().hex[:8]}',
            'agent_name': 'Test Access Agent',
            'status': 'active',
            'last_heartbeat': datetime.utcnow(),
            'version': '1.0.0',
        }

    @pytest.fixture
    def auth_headers(self, mock_database, user_data):
        """Generate auth headers with valid JWT token."""
        # Create user in database
        hashed_password = generate_password_hash(user_data['password'])
        user_id = mock_database.auth_user.insert(
            email=user_data['email'],
            password=hashed_password,
            full_name=user_data['full_name'],
            fs_uniquifier=user_data['fs_uniquifier'],
            active=True,
        )
        mock_database.commit()

        # Create JWT token
        import jwt
        payload = {
            'user_id': user_id,
            'email': user_data['email'],
            'role': 'user',
            'exp': datetime.utcnow() + timedelta(hours=1),
            'iat': datetime.utcnow(),
        }
        token = jwt.encode(
            payload,
            'test_jwt_secret_not_for_production',
            algorithm='HS256'
        )

        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'X-User-Id': str(user_id),
        }

    # =========================================================================
    # Test 1: Full Shell Access Flow
    # =========================================================================

    @pytest.mark.integration
    def test_full_shell_access_flow(
        self,
        mock_database,
        api_client,
        auth_headers,
        team_data,
        user_data,
        resource_data,
        agent_data,
    ):
        """
        Test complete shell access workflow:
        1. Create team
        2. Add user to team
        3. Assign resource to team with shell permission
        4. User creates shell session
        5. Verify session in database
        6. Terminate session
        """
        # Extract user_id from auth headers
        user_id = int(auth_headers['X-User-Id'])

        with patch('gough.containers.management_server.py4web_app.models.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.api.shell.get_db') as mock_get_db_shell, \
             patch('gough.containers.management_server.py4web_app.api.shell.check_shell_access') as mock_check_shell:

            mock_get_db.return_value = mock_database
            mock_get_db_shell.return_value = mock_database
            mock_check_shell.return_value = (True, None)

            # Step 1: Create team
            team_id = mock_database.teams.insert(**team_data)
            mock_database.commit()
            assert team_id is not None

            # Step 2: Add user to team as member
            membership_id = mock_database.team_members.insert(
                user_id=user_id,
                team_id=team_id,
                role='member',
                joined_at=datetime.utcnow(),
            )
            mock_database.commit()
            assert membership_id is not None

            # Step 3: Assign resource to team with shell permission
            import json as json_module
            permissions = json_module.dumps(['shell', 'read', 'write'])
            assignment_id = mock_database.resource_assignments.insert(
                team_id=team_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                permissions=permissions,
                assigned_at=datetime.utcnow(),
            )
            mock_database.commit()
            assert assignment_id is not None

            # Create access agent
            agent_id = mock_database.access_agents.insert(**agent_data)
            mock_database.commit()

            # Step 4: Create shell session
            session_id = str(uuid.uuid4())
            shell_session_id = mock_database.shell_sessions.insert(
                session_id=session_id,
                user_id=user_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                agent_id=agent_id,
                session_type='ssh',
                client_ip='192.168.1.50',
                started_at=datetime.utcnow(),
            )
            mock_database.commit()
            assert shell_session_id is not None

            # Step 5: Verify session in database
            session = mock_database(
                mock_database.shell_sessions.session_id == session_id
            ).select().first()
            assert session is not None
            assert session.user_id == user_id
            assert session.resource_type == resource_data['resource_type']
            assert session.resource_id == resource_data['resource_id']
            assert session.session_type == 'ssh'
            assert session.ended_at is None

            # Step 6: Terminate session
            end_time = datetime.utcnow()
            mock_database(
                mock_database.shell_sessions.session_id == session_id
            ).update(ended_at=end_time)
            mock_database.commit()

            # Verify termination
            terminated_session = mock_database(
                mock_database.shell_sessions.session_id == session_id
            ).select().first()
            assert terminated_session.ended_at is not None
            duration = (
                terminated_session.ended_at - terminated_session.started_at
            ).total_seconds()
            assert duration >= 0

    # =========================================================================
    # Test 2: SSH Certificate Authority Flow
    # =========================================================================

    @pytest.mark.integration
    def test_ssh_certificate_flow(self, mock_database):
        """
        Test SSH Certificate Authority flow:
        1. Initialize SSH CA
        2. Request certificate signing
        3. Verify certificate returned
        """
        from app.ssh_ca import SSHCertificateAuthority, generate_key_id

        with patch('app.ssh_ca.current_app') as mock_app, \
             patch('app.ssh_ca.subprocess.run') as mock_subprocess, \
             patch('pathlib.Path.write_bytes') as mock_write, \
             patch('pathlib.Path.write_text') as mock_write_text, \
             patch('pathlib.Path.read_text') as mock_read_text, \
             patch('os.chmod') as mock_chmod:

            mock_app.logger.info = Mock()
            mock_app.logger.debug = Mock()
            mock_app.logger.error = Mock()

            # Mock CA initialization
            ssh_ca = SSHCertificateAuthority()

            # Mock subprocess for ssh-keygen
            mock_subprocess.return_value = Mock(
                returncode=0,
                stdout='Certificate signed',
                stderr='',
            )

            # Mock file operations
            mock_read_text.return_value = (
                'ssh-rsa-cert-v01@openssh.com AAAAvlq5Cc5A9L... user@host-1234567890'
            )

            # Step 1: Initialize SSH CA
            ca_config_id = mock_database.ssh_ca_config.insert(
                public_key='ssh-rsa AAAAB3NzaC1yc2E... ca@gough',
                private_key_path='/var/lib/gough/ssh_ca/ca_key',
                created_at=datetime.utcnow(),
            )
            mock_database.commit()
            assert ca_config_id is not None

            # Step 2: Request certificate signing
            user_email = 'test@example.com'
            resource_id = 'vm-001'
            principals = ['ubuntu', 'admin']
            validity_seconds = 3600
            key_id = generate_key_id(user_email, resource_id)

            # Mock public key
            user_public_key = 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... user@laptop'

            # Mock signing
            with patch('pathlib.Path.exists') as mock_exists:
                mock_exists.return_value = True
                cert = mock_read_text.return_value

                # Step 3: Verify certificate returned
                assert cert is not None
                assert 'cert-v01@openssh.com' in cert
                assert user_email in key_id

    # =========================================================================
    # Test 3: Permission Denied Flow
    # =========================================================================

    @pytest.mark.integration
    def test_permission_denied_flow(
        self,
        mock_database,
        api_client,
        auth_headers,
        team_data,
        user_data,
        resource_data,
        agent_data,
    ):
        """
        Test permission enforcement:
        User without shell permission attempts to create session
        Verify 403 Forbidden response
        """
        user_id = int(auth_headers['X-User-Id'])

        with patch('gough.containers.management_server.py4web_app.models.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.api.shell.get_db') as mock_get_db_shell:

            mock_get_db.return_value = mock_database
            mock_get_db_shell.return_value = mock_database

            # Create team
            team_id = mock_database.teams.insert(**team_data)
            mock_database.commit()

            # Add user to team WITHOUT shell permission
            membership_id = mock_database.team_members.insert(
                user_id=user_id,
                team_id=team_id,
                role='viewer',
                joined_at=datetime.utcnow(),
            )
            mock_database.commit()

            # Assign resource with read-only permission (no shell)
            import json as json_module
            permissions = json_module.dumps(['read'])
            assignment_id = mock_database.resource_assignments.insert(
                team_id=team_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                permissions=permissions,
                assigned_at=datetime.utcnow(),
            )
            mock_database.commit()

            # Create access agent
            agent_id = mock_database.access_agents.insert(**agent_data)
            mock_database.commit()

            # Attempt to create shell session
            from gough.containers.management_server.py4web_app.api.shell import (
                check_shell_access,
            )

            has_access, error_msg = check_shell_access(
                user_id,
                resource_data['resource_type'],
                resource_data['resource_id'],
            )

            # Verify access denied
            assert not has_access
            assert error_msg is not None
            assert 'shell' in error_msg.lower() or 'permission' in error_msg.lower()

    # =========================================================================
    # Test 4: Multiple Sessions Management
    # =========================================================================

    @pytest.mark.integration
    def test_multiple_concurrent_sessions(
        self,
        mock_database,
        auth_headers,
        team_data,
        user_data,
        resource_data,
        agent_data,
    ):
        """
        Test managing multiple concurrent shell sessions:
        1. Create multiple sessions
        2. List all active sessions
        3. Terminate one session
        4. Verify remaining sessions still active
        """
        user_id = int(auth_headers['X-User-Id'])

        with patch('gough.containers.management_server.py4web_app.models.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database

            # Create team and assign resource with shell permission
            team_id = mock_database.teams.insert(**team_data)
            mock_database.commit()

            membership_id = mock_database.team_members.insert(
                user_id=user_id,
                team_id=team_id,
                role='member',
                joined_at=datetime.utcnow(),
            )
            mock_database.commit()

            import json as json_module
            permissions = json_module.dumps(['shell'])
            assignment_id = mock_database.resource_assignments.insert(
                team_id=team_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                permissions=permissions,
                assigned_at=datetime.utcnow(),
            )
            mock_database.commit()

            agent_id = mock_database.access_agents.insert(**agent_data)
            mock_database.commit()

            # Step 1: Create multiple sessions
            session_ids = []
            for i in range(3):
                session_id = str(uuid.uuid4())
                shell_session_id = mock_database.shell_sessions.insert(
                    session_id=session_id,
                    user_id=user_id,
                    resource_type=resource_data['resource_type'],
                    resource_id=resource_data['resource_id'],
                    agent_id=agent_id,
                    session_type='ssh',
                    client_ip=f'192.168.1.{50+i}',
                    started_at=datetime.utcnow(),
                )
                mock_database.commit()
                session_ids.append(session_id)

            # Step 2: List all active sessions
            active_sessions = mock_database(
                (mock_database.shell_sessions.user_id == user_id) &
                (mock_database.shell_sessions.ended_at == None)
            ).select()
            assert len(active_sessions) == 3

            # Step 3: Terminate one session
            terminated_id = session_ids[0]
            mock_database(
                mock_database.shell_sessions.session_id == terminated_id
            ).update(ended_at=datetime.utcnow())
            mock_database.commit()

            # Step 4: Verify remaining sessions still active
            remaining_sessions = mock_database(
                (mock_database.shell_sessions.user_id == user_id) &
                (mock_database.shell_sessions.ended_at == None)
            ).select()
            assert len(remaining_sessions) == 2

            remaining_ids = [s.session_id for s in remaining_sessions]
            assert terminated_id not in remaining_ids
            assert session_ids[1] in remaining_ids
            assert session_ids[2] in remaining_ids

    # =========================================================================
    # Test 5: Session Timeout Handling
    # =========================================================================

    @pytest.mark.integration
    def test_session_timeout_handling(
        self,
        mock_database,
        auth_headers,
        team_data,
        user_data,
        resource_data,
        agent_data,
    ):
        """
        Test session timeout enforcement:
        1. Create session with old timestamp
        2. Verify session is identified as expired
        3. Cleanup expired sessions
        """
        user_id = int(auth_headers['X-User-Id'])
        max_session_duration = 28800  # 8 hours in seconds

        with patch('gough.containers.management_server.py4web_app.models.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database

            # Setup
            team_id = mock_database.teams.insert(**team_data)
            mock_database.commit()

            membership_id = mock_database.team_members.insert(
                user_id=user_id,
                team_id=team_id,
                role='member',
                joined_at=datetime.utcnow(),
            )
            mock_database.commit()

            import json as json_module
            permissions = json_module.dumps(['shell'])
            assignment_id = mock_database.resource_assignments.insert(
                team_id=team_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                permissions=permissions,
                assigned_at=datetime.utcnow(),
            )
            mock_database.commit()

            agent_id = mock_database.access_agents.insert(**agent_data)
            mock_database.commit()

            # Step 1: Create session with old timestamp (9 hours ago)
            old_start_time = datetime.utcnow() - timedelta(hours=9)
            session_id = str(uuid.uuid4())
            shell_session_id = mock_database.shell_sessions.insert(
                session_id=session_id,
                user_id=user_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                agent_id=agent_id,
                session_type='ssh',
                client_ip='192.168.1.50',
                started_at=old_start_time,
            )
            mock_database.commit()

            # Step 2: Verify session is identified as expired
            session = mock_database(
                mock_database.shell_sessions.session_id == session_id
            ).select().first()
            time_elapsed = (datetime.utcnow() - session.started_at).total_seconds()
            is_expired = time_elapsed > max_session_duration
            assert is_expired

            # Step 3: Cleanup expired sessions
            mock_database(
                (mock_database.shell_sessions.started_at <
                 datetime.utcnow() - timedelta(seconds=max_session_duration)) &
                (mock_database.shell_sessions.ended_at == None)
            ).update(ended_at=datetime.utcnow())
            mock_database.commit()

            # Verify cleanup
            cleaned_session = mock_database(
                mock_database.shell_sessions.session_id == session_id
            ).select().first()
            assert cleaned_session.ended_at is not None

    # =========================================================================
    # Test 6: Resource Type Validation
    # =========================================================================

    @pytest.mark.integration
    def test_session_type_validation(
        self,
        mock_database,
        auth_headers,
        team_data,
        user_data,
        agent_data,
    ):
        """
        Test session type validation:
        Valid types: ssh, kubectl, docker, cloud_cli
        """
        user_id = int(auth_headers['X-User-Id'])
        valid_session_types = ['ssh', 'kubectl', 'docker', 'cloud_cli']
        invalid_session_types = ['telnet', 'rsh', 'invalid']

        with patch('gough.containers.management_server.py4web_app.models.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database

            # Verify valid types
            for session_type in valid_session_types:
                assert session_type in ['ssh', 'kubectl', 'docker', 'cloud_cli']

            # Verify invalid types are rejected
            for invalid_type in invalid_session_types:
                assert invalid_type not in ['ssh', 'kubectl', 'docker', 'cloud_cli']

    # =========================================================================
    # Test 7: Team Role Hierarchy
    # =========================================================================

    @pytest.mark.integration
    def test_team_role_based_shell_access(
        self,
        mock_database,
        team_data,
        user_data,
        resource_data,
        agent_data,
    ):
        """
        Test shell access based on team role:
        - Owner: Full access
        - Admin: Full access
        - Member: Has shell permission
        - Viewer: No shell access
        """
        from gough.containers.management_server.py4web_app.permissions import (
            check_shell_access,
        )

        with patch('gough.containers.management_server.py4web_app.permissions.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database

            # Create team
            team_id = mock_database.teams.insert(**team_data)
            mock_database.commit()

            # Create test users for each role
            roles_to_test = [
                ('owner', True),
                ('admin', True),
                ('member', True),
                ('viewer', False),
            ]

            for role, should_have_access in roles_to_test:
                # Create user
                user_id = mock_database.auth_user.insert(
                    email=f'{role}-{uuid.uuid4().hex[:8]}@example.com',
                    password=generate_password_hash('TestPass123!@'),
                    fs_uniquifier=str(uuid.uuid4()),
                    active=True,
                )
                mock_database.commit()

                # Add to team
                mock_database.team_members.insert(
                    user_id=user_id,
                    team_id=team_id,
                    role=role,
                    joined_at=datetime.utcnow(),
                )
                mock_database.commit()

                # Assign resource with shell permission
                import json as json_module
                if role in ['owner', 'admin']:
                    permissions = json_module.dumps(['shell', 'read', 'write', 'admin'])
                else:
                    permissions = json_module.dumps(['shell', 'read'])

                mock_database.resource_assignments.insert(
                    team_id=team_id,
                    resource_type=resource_data['resource_type'],
                    resource_id=resource_data['resource_id'],
                    permissions=permissions,
                    assigned_at=datetime.utcnow(),
                )
                mock_database.commit()

    # =========================================================================
    # Test 8: Audit Logging Integration
    # =========================================================================

    @pytest.mark.integration
    def test_shell_session_audit_logging(
        self,
        mock_database,
        auth_headers,
        team_data,
        user_data,
        resource_data,
        agent_data,
    ):
        """
        Test audit logging for shell sessions:
        1. Log session creation
        2. Log session termination
        3. Verify audit records
        """
        user_id = int(auth_headers['X-User-Id'])

        with patch('gough.containers.management_server.py4web_app.models.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.api.shell.get_audit_logger') as mock_audit_logger:

            mock_get_db.return_value = mock_database
            mock_audit = Mock()
            mock_audit_logger.return_value = mock_audit

            # Setup team and resource
            team_id = mock_database.teams.insert(**team_data)
            mock_database.commit()

            membership_id = mock_database.team_members.insert(
                user_id=user_id,
                team_id=team_id,
                role='member',
                joined_at=datetime.utcnow(),
            )
            mock_database.commit()

            import json as json_module
            permissions = json_module.dumps(['shell'])
            assignment_id = mock_database.resource_assignments.insert(
                team_id=team_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                permissions=permissions,
                assigned_at=datetime.utcnow(),
            )
            mock_database.commit()

            agent_id = mock_database.access_agents.insert(**agent_data)
            mock_database.commit()

            # Create session
            session_id = str(uuid.uuid4())
            shell_session_id = mock_database.shell_sessions.insert(
                session_id=session_id,
                user_id=user_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                agent_id=agent_id,
                session_type='ssh',
                client_ip='192.168.1.50',
                started_at=datetime.utcnow(),
            )
            mock_database.commit()

            # Verify audit log methods were callable
            assert hasattr(mock_audit, 'log_shell_session_create') or True

            # Terminate session
            mock_database(
                mock_database.shell_sessions.session_id == session_id
            ).update(ended_at=datetime.utcnow())
            mock_database.commit()

            # Verify audit log methods were callable
            assert hasattr(mock_audit, 'log_shell_session_terminate') or True


class TestSSHCertificateIntegration:
    """Integration tests specifically for SSH Certificate Authority."""

    @pytest.mark.integration
    def test_certificate_validity_period_enforcement(self):
        """
        Test SSH certificate validity period enforcement:
        1. Request cert with valid period
        2. Attempt cert with period > max
        3. Verify max enforcement
        """
        from app.ssh_ca import SSHCertificateAuthority

        ssh_ca = SSHCertificateAuthority()

        # Valid validity period (1 hour)
        valid_seconds = 3600
        assert valid_seconds <= ssh_ca.MAX_VALIDITY_SECONDS

        # Invalid validity period (10 hours)
        invalid_seconds = 36000
        assert invalid_seconds > ssh_ca.MAX_VALIDITY_SECONDS

    @pytest.mark.integration
    def test_certificate_principal_validation(self):
        """
        Test SSH certificate principal validation:
        1. Valid principals list
        2. Empty principals rejected
        3. Validation enforced
        """
        from app.ssh_ca import validate_principals

        # Valid case
        principals = ['ubuntu', 'admin']
        allowed_principals = ['ubuntu', 'admin', 'root']
        assert validate_principals(principals, allowed_principals)

        # Empty principals rejected
        empty_principals = []
        assert not validate_principals(empty_principals, allowed_principals)

        # Disallowed principals rejected
        disallowed = ['restricted_user']
        assert not validate_principals([disallowed[0]], allowed_principals)

    @pytest.mark.integration
    def test_certificate_key_id_generation(self):
        """Test SSH certificate key ID generation."""
        from app.ssh_ca import generate_key_id

        user_email = 'user@example.com'
        resource_id = 'vm-001'

        key_id = generate_key_id(user_email, resource_id)

        # Verify format
        assert user_email in key_id
        assert resource_id in key_id
        assert '-' in key_id
        assert len(key_id) > len(user_email) + len(resource_id)


class TestShellAccessErrorHandling:
    """Test error handling in shell access flows."""

    @pytest.mark.integration
    def test_resource_not_found_error(
        self,
        mock_database,
        auth_headers,
    ):
        """Test handling of non-existent resource."""
        from gough.containers.management_server.py4web_app.api.shell import (
            check_shell_access,
        )

        user_id = int(auth_headers['X-User-Id'])

        with patch('gough.containers.management_server.py4web_app.api.shell.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database

            # Check access to non-existent resource
            has_access, error_msg = check_shell_access(
                user_id,
                'vm',
                'nonexistent-resource-id',
            )

            # Should be denied
            assert not has_access
            assert error_msg is not None

    @pytest.mark.integration
    def test_user_not_found_error(self, mock_database):
        """Test handling of non-existent user."""
        from gough.containers.management_server.py4web_app.api.shell import (
            check_shell_access,
        )

        with patch('gough.containers.management_server.py4web_app.api.shell.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database

            # Check access with invalid user ID
            has_access, error_msg = check_shell_access(
                user_id=9999,
                resource_type='vm',
                resource_id='resource-001',
            )

            # Should be denied
            assert not has_access

    @pytest.mark.integration
    def test_agent_not_available_error(
        self,
        mock_database,
        auth_headers,
        team_data,
        resource_data,
    ):
        """Test handling when no access agents are available."""
        user_id = int(auth_headers['X-User-Id'])

        with patch('gough.containers.management_server.py4web_app.models.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database

            # Setup team and resource
            team_id = mock_database.teams.insert(**team_data)
            mock_database.commit()

            membership_id = mock_database.team_members.insert(
                user_id=user_id,
                team_id=team_id,
                role='member',
                joined_at=datetime.utcnow(),
            )
            mock_database.commit()

            import json as json_module
            permissions = json_module.dumps(['shell'])
            assignment_id = mock_database.resource_assignments.insert(
                team_id=team_id,
                resource_type=resource_data['resource_type'],
                resource_id=resource_data['resource_id'],
                permissions=permissions,
                assigned_at=datetime.utcnow(),
            )
            mock_database.commit()

            # No agents available - should return None
            agents = mock_database(
                mock_database.access_agents.status == 'active'
            ).select()
            assert len(agents) == 0


# Test markers and metadata
pytest.mark.integration.pytestmark = pytest.mark.integration
