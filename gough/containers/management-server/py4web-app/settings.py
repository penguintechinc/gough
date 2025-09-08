#!/usr/bin/env python3
"""
Gough Management Server - Settings Configuration
Py4web application settings for the hypervisor automation system
Phase 8.1 - Core Development
"""

import os
import secrets
from datetime import timedelta


class Config:
    """Base configuration class"""
    
    # Application settings
    APP_NAME = "Gough Hypervisor Management"
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'
    TESTING = False
    
    # Database settings
    DATABASE_URL = os.environ.get('DATABASE_URL') or 'postgresql://postgres:postgres@localhost:5432/gough'
    DATABASE_POOL_SIZE = int(os.environ.get('DATABASE_POOL_SIZE', '20'))
    DATABASE_MAX_OVERFLOW = int(os.environ.get('DATABASE_MAX_OVERFLOW', '0'))
    
    # Redis settings
    REDIS_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    REDIS_SESSIONS = os.environ.get('REDIS_SESSIONS') or 'redis://localhost:6379/1'
    REDIS_CACHE = os.environ.get('REDIS_CACHE') or 'redis://localhost:6379/2'
    
    # MaaS API settings
    MAAS_URL = os.environ.get('MAAS_URL') or 'http://localhost:5240/MAAS'
    MAAS_API_KEY = os.environ.get('MAAS_API_KEY')
    MAAS_WEBHOOK_SECRET = os.environ.get('MAAS_WEBHOOK_SECRET') or secrets.token_hex(32)
    
    # FleetDM settings
    FLEET_URL = os.environ.get('FLEET_URL') or 'https://localhost:8443'
    FLEET_API_TOKEN = os.environ.get('FLEET_API_TOKEN')
    
    # Celery settings
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL') or REDIS_URL
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND') or REDIS_URL
    CELERY_TASK_SERIALIZER = 'json'
    CELERY_ACCEPT_CONTENT = ['json']
    CELERY_RESULT_SERIALIZER = 'json'
    CELERY_TIMEZONE = 'UTC'
    
    # Security settings
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or SECRET_KEY
    JWT_EXPIRATION_HOURS = int(os.environ.get('JWT_EXPIRATION_HOURS', '24'))
    JWT_REFRESH_EXPIRATION_DAYS = int(os.environ.get('JWT_REFRESH_EXPIRATION_DAYS', '30'))
    
    # Session settings
    SESSION_TIMEOUT_HOURS = int(os.environ.get('SESSION_TIMEOUT_HOURS', '8'))
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # File upload settings
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', '16')) * 1024 * 1024  # 16MB
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER') or '/tmp/gough/uploads'
    
    # Logging settings
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    LOG_FILE = os.environ.get('LOG_FILE') or '/var/log/py4web/gough.log'
    
    # Performance settings
    PAGINATION_PER_PAGE = int(os.environ.get('PAGINATION_PER_PAGE', '25'))
    CACHE_TIMEOUT_SECONDS = int(os.environ.get('CACHE_TIMEOUT_SECONDS', '300'))
    
    # Network settings
    MANAGEMENT_NETWORK = os.environ.get('MANAGEMENT_NETWORK', '172.20.0.0/16')
    PXE_NETWORK = os.environ.get('PXE_NETWORK', '192.168.100.0/24')
    
    # Feature flags
    ENABLE_WEBHOOKS = os.environ.get('ENABLE_WEBHOOKS', 'true').lower() == 'true'
    ENABLE_METRICS = os.environ.get('ENABLE_METRICS', 'true').lower() == 'true'
    ENABLE_AUDIT_LOG = os.environ.get('ENABLE_AUDIT_LOG', 'true').lower() == 'true'
    ENABLE_RATE_LIMITING = os.environ.get('ENABLE_RATE_LIMITING', 'true').lower() == 'true'
    
    # Rate limiting
    RATE_LIMIT_DEFAULT = os.environ.get('RATE_LIMIT_DEFAULT', '100 per hour')
    RATE_LIMIT_API = os.environ.get('RATE_LIMIT_API', '1000 per hour')
    RATE_LIMIT_WEBHOOK = os.environ.get('RATE_LIMIT_WEBHOOK', '10000 per hour')


class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    TESTING = False
    LOG_LEVEL = 'DEBUG'
    SESSION_COOKIE_SECURE = False


class TestingConfig(Config):
    """Testing configuration"""
    DEBUG = False
    TESTING = True
    DATABASE_URL = os.environ.get('TEST_DATABASE_URL') or 'postgresql://postgres:postgres@localhost:5432/gough_test'
    REDIS_URL = os.environ.get('TEST_REDIS_URL') or 'redis://localhost:6379/10'
    LOG_LEVEL = 'WARNING'


class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True
    LOG_LEVEL = 'WARNING'
    
    # Enhanced security for production
    JWT_EXPIRATION_HOURS = 8
    SESSION_TIMEOUT_HOURS = 4
    RATE_LIMIT_DEFAULT = '50 per hour'


# Configuration mapping
config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}


def get_config():
    """Get configuration based on environment"""
    env = os.environ.get('FLASK_ENV', 'default')
    return config.get(env, DevelopmentConfig)