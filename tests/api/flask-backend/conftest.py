"""Pytest fixtures for Flask backend API tests."""

import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from flask import Flask
from flask_security import Security

from gough.services.flask_backend.app import create_app
from gough.services.flask_backend.app.config import Config
from gough.services.flask_backend.app.models import get_db
from gough.services.flask_backend.app.security_datastore import PyDALUserDatastore


class TestConfig(Config):
    """Test configuration for Flask app."""

    TESTING = True
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret-key-do-not-use-in-production"
    JWT_SECRET = "test-jwt-secret-do-not-use-in-production"
    SECURITY_PASSWORD_SALT = "test-salt-do-not-use-in-production"


@pytest.fixture(scope="function")
def app():
    """Create and configure a test Flask application."""
    app = create_app(TestConfig)

    with app.app_context():
        yield app


@pytest.fixture(scope="function")
def client(app):
    """Create a test client."""
    return app.test_client()


@pytest.fixture(scope="function")
def auth_headers(app, client):
    """Create authentication headers with a test user."""
    with app.app_context():
        db = get_db()
        user_datastore = app.user_datastore

        # Create default admin role if it doesn't exist
        if not db(db.auth_role.name == "admin").select().first():
            db.auth_role.insert(
                name="admin",
                description="Administrator",
                permissions=json.dumps(["all"])
            )
            db.commit()

        # Create test user
        test_email = "testuser@example.com"
        test_password = "SecurePassword123!"

        # Clean up any existing test user
        db(db.auth_user.email == test_email).delete()
        db.commit()

        # Create user
        user = user_datastore.create_user(
            email=test_email,
            password=test_password,
            active=True,
            confirmed_at=datetime.utcnow()
        )
        db.commit()

        # Assign admin role
        admin_role = db(db.auth_role.name == "admin").select().first()
        if admin_role:
            db.auth_user_roles.insert(
                user_id=user.id,
                role_id=admin_role.id
            )
            db.commit()

    # Login and get token
    response = client.post(
        "/api/v1/auth/login",
        json={"email": test_email, "password": test_password},
        content_type="application/json"
    )

    if response.status_code == 200:
        data = response.get_json()
        token = data.get("token") or data.get("access_token")
        return {"Authorization": f"Bearer {token}"}

    # Fallback if login fails
    return {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="function")
def mock_agent(app):
    """Create a mock access agent."""
    with app.app_context():
        db = get_db()

        agent = db.access_agents.insert(
            agent_id="test-agent-001",
            hostname="test-agent.local",
            ip_address="192.168.1.100",
            status="active",
            capabilities=json.dumps(["ssh", "kubectl", "docker", "cloud_cli"]),
            last_heartbeat=datetime.utcnow()
        )
        db.commit()

        return agent


@pytest.fixture(scope="function")
def mock_resource(app):
    """Create a mock resource for testing."""
    with app.app_context():
        db = get_db()

        # Create a team
        team = db.resource_teams.insert(
            name="test-team",
            description="Test team for shell sessions",
            created_by=1
        )

        # Create resource assignment with shell permission
        assignment = db.resource_assignments.insert(
            team_id=team,
            resource_type="vm",
            resource_id="test-vm-001",
            permissions=json.dumps(["shell", "read", "write"]),
            assigned_by=1
        )

        db.commit()

        return {
            "team_id": team,
            "resource_type": "vm",
            "resource_id": "test-vm-001"
        }


@pytest.fixture(scope="function")
def admin_user(app):
    """Create an admin user."""
    with app.app_context():
        db = get_db()
        user_datastore = app.user_datastore

        # Create admin role if needed
        admin_role = db(db.auth_role.name == "admin").select().first()
        if not admin_role:
            admin_role = db.auth_role.insert(
                name="admin",
                description="Administrator",
                permissions=json.dumps(["all"])
            )
            db.commit()

        admin_email = "admin@example.com"

        # Clean up any existing admin user
        db(db.auth_user.email == admin_email).delete()
        db.commit()

        # Create admin user
        admin = user_datastore.create_user(
            email=admin_email,
            password="AdminPassword123!",
            active=True,
            confirmed_at=datetime.utcnow()
        )

        # Assign admin role
        db.auth_user_roles.insert(
            user_id=admin.id,
            role_id=admin_role.id
        )

        db.commit()

        return admin


@pytest.fixture(scope="function")
def regular_user(app):
    """Create a regular (non-admin) user."""
    with app.app_context():
        db = get_db()
        user_datastore = app.user_datastore

        # Create viewer role if needed
        viewer_role = db(db.auth_role.name == "viewer").select().first()
        if not viewer_role:
            viewer_role = db.auth_role.insert(
                name="viewer",
                description="Viewer",
                permissions=json.dumps(["read"])
            )
            db.commit()

        user_email = "regularuser@example.com"

        # Clean up any existing user
        db(db.auth_user.email == user_email).delete()
        db.commit()

        # Create user
        user = user_datastore.create_user(
            email=user_email,
            password="UserPassword123!",
            active=True,
            confirmed_at=datetime.utcnow()
        )

        # Assign viewer role
        db.auth_user_roles.insert(
            user_id=user.id,
            role_id=viewer_role.id
        )

        db.commit()

        return user


@pytest.fixture(scope="function")
def team_with_shell_access(app, admin_user):
    """Create a team with shell access to a resource."""
    with app.app_context():
        db = get_db()

        # Create team
        team = db.resource_teams.insert(
            name="shell-access-team",
            description="Team with shell access",
            created_by=admin_user.id
        )

        # Add admin user to team
        db.team_members.insert(
            team_id=team,
            user_id=admin_user.id,
            role="owner",
            added_by=admin_user.id
        )

        # Create resource assignment with shell permission
        db.resource_assignments.insert(
            team_id=team,
            resource_type="vm",
            resource_id="shell-test-vm",
            permissions=json.dumps(["shell", "read"]),
            assigned_by=admin_user.id
        )

        db.commit()

        return {
            "team_id": team,
            "resource_type": "vm",
            "resource_id": "shell-test-vm"
        }
