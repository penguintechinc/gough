"""Flask Backend Configuration."""

import os
from datetime import timedelta


class Config:
    """Base configuration."""

    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    # Flask-Security-Too Configuration (mandatory per CLAUDE.md)
    SECURITY_PASSWORD_SALT = os.getenv(
        "SECURITY_PASSWORD_SALT", "dev-salt-change-in-production"
    )
    SECURITY_PASSWORD_HASH = "bcrypt"
    SECURITY_REGISTERABLE = True
    SECURITY_SEND_REGISTER_EMAIL = False
    SECURITY_CONFIRMABLE = False
    SECURITY_RECOVERABLE = True
    SECURITY_TRACKABLE = True
    SECURITY_CHANGEABLE = True
    SECURITY_TOKEN_AUTHENTICATION_HEADER = "Authorization"
    SECURITY_TOKEN_AUTHENTICATION_KEY = "auth_token"
    SECURITY_TOKEN_MAX_AGE = int(os.getenv("SECURITY_TOKEN_MAX_AGE", "3600"))
    SECURITY_FRESHNESS = timedelta(minutes=30)
    SECURITY_FRESHNESS_GRACE_PERIOD = timedelta(minutes=5)

    # Disable default Flask-Security-Too views (we use API-only)
    SECURITY_LOGIN_URL = None
    SECURITY_LOGOUT_URL = None
    SECURITY_REGISTER_URL = None
    SECURITY_RESET_URL = None
    SECURITY_CHANGE_URL = None
    SECURITY_CONFIRM_URL = None

    # JWT (kept for API token compatibility)
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", SECRET_KEY)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(
        minutes=int(os.getenv("JWT_ACCESS_TOKEN_MINUTES", "30"))
    )
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(
        days=int(os.getenv("JWT_REFRESH_TOKEN_DAYS", "7"))
    )

    # Database - PyDAL compatible with multi-DB support
    DB_TYPE = os.getenv("DB_TYPE", "postgres")
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME", "gough_db")
    DB_USER = os.getenv("DB_USER", "gough_user")
    DB_PASS = os.getenv("DB_PASS", "gough_pass")
    DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))

    # Secrets Management
    SECRETS_BACKEND = os.getenv("SECRETS_BACKEND", "encrypted_db")
    # Supported: encrypted_db, vault, infisical, aws, gcp, azure

    # HashiCorp Vault
    VAULT_ADDR = os.getenv("VAULT_ADDR", "http://vault:8200")
    VAULT_TOKEN = os.getenv("VAULT_TOKEN", "")
    VAULT_ROLE_ID = os.getenv("VAULT_ROLE_ID", "")
    VAULT_SECRET_ID = os.getenv("VAULT_SECRET_ID", "")
    VAULT_MOUNT_POINT = os.getenv("VAULT_MOUNT_POINT", "secret")

    # Infisical
    INFISICAL_CLIENT_ID = os.getenv("INFISICAL_CLIENT_ID", "")
    INFISICAL_CLIENT_SECRET = os.getenv("INFISICAL_CLIENT_SECRET", "")
    INFISICAL_PROJECT_ID = os.getenv("INFISICAL_PROJECT_ID", "")
    INFISICAL_ENVIRONMENT = os.getenv("INFISICAL_ENVIRONMENT", "dev")

    # AWS Secrets Manager
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")

    # GCP Secret Manager
    GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
    GCP_CREDENTIALS_FILE = os.getenv("GCP_CREDENTIALS_FILE", "")

    # Azure Key Vault
    AZURE_VAULT_URL = os.getenv("AZURE_VAULT_URL", "")
    AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
    AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
    AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")

    # Encryption key for DB-based secrets (Fernet)
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

    # CORS
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

    # Redis (for caching and sessions)
    REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # Rate Limiting
    RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    RATE_LIMIT_REDIS_URL = os.getenv("RATE_LIMIT_REDIS_URL", REDIS_URL)
    RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "100/minute;1000/hour")

    # Audit Logging
    AUDIT_ENABLED = os.getenv("AUDIT_ENABLED", "true").lower() == "true"
    AUDIT_RECORDING_ENABLED = os.getenv(
        "AUDIT_RECORDING_ENABLED", "true"
    ).lower() == "true"
    AUDIT_RECORDING_PATH = os.getenv("AUDIT_RECORDING_PATH", "/var/gough/recordings")

    @classmethod
    def get_db_uri(cls) -> str:
        """Build PyDAL-compatible database URI."""
        db_type = cls.DB_TYPE

        # Map common aliases to PyDAL format
        type_map = {
            "postgresql": "postgres",
            "postgres": "postgres",
            "mysql": "mysql",
            "mariadb": "mysql",
            "sqlite": "sqlite",
            "mssql": "mssql",
        }
        db_type = type_map.get(db_type.lower(), db_type)

        if db_type == "sqlite":
            if cls.DB_NAME == ":memory:":
                return "sqlite:memory"
            return f"sqlite://{cls.DB_NAME}.db"

        # For MySQL, use pymysql driver
        if db_type == "mysql":
            return (
                f"mysql://{cls.DB_USER}:{cls.DB_PASS}@"
                f"{cls.DB_HOST}:{cls.DB_PORT}/{cls.DB_NAME}"
            )

        return (
            f"{db_type}://{cls.DB_USER}:{cls.DB_PASS}@"
            f"{cls.DB_HOST}:{cls.DB_PORT}/{cls.DB_NAME}"
        )


class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG = True


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG = False


class TestingConfig(Config):
    """Testing configuration."""

    TESTING = True
    DB_TYPE = "sqlite"
    DB_NAME = ":memory:"
    WTF_CSRF_ENABLED = False
