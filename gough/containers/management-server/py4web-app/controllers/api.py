#!/usr/bin/env python3
"""
Gough Management Server - REST API Controllers
Comprehensive REST API endpoints for hypervisor automation system
Phase 8.1 - Core Development
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from py4web import action, request, response, abort, HTTP, Field
from py4web.utils.param import Param
from pydal import DAL

from ..lib.maas_api import MaasAPIClient, DeploymentConfig, MachineStatus, PowerState
from ..lib.auth import auth_required, auth_manager, UserRole
from ..lib.redis_client import get_redis_client
from ..models import get_db
from settings import get_config

# Configure logging
logger = logging.getLogger(__name__)
config = get_config()

# Initialize services
redis_client = get_redis_client()


# Utility functions

def json_response(data: Any, status: int = 200) -> str:
    """Return JSON response with proper headers"""
    response.headers['Content-Type'] = 'application/json'
    response.status = status
    return json.dumps(data, default=str, indent=2 if config.DEBUG else None)


def error_response(message: str, code: str = 'error', status: int = 400, details: Dict = None) -> str:
    """Return standardized error response"""
    error_data = {
        'error': {
            'message': message,
            'code': code,
            'timestamp': datetime.utcnow().isoformat(),
            'request_id': getattr(request, 'request_id', str(uuid.uuid4()))
        }
    }
    
    if details:
        error_data['error']['details'] = details
        
    return json_response(error_data, status)


def success_response(data: Any = None, message: str = 'Success') -> str:
    """Return standardized success response"""
    response_data = {
        'success': True,
        'message': message,
        'timestamp': datetime.utcnow().isoformat()
    }
    
    if data is not None:
        response_data['data'] = data
        
    return json_response(response_data)


def get_maas_client() -> MaasAPIClient:
    """Get MaaS API client instance"""
    if not config.MAAS_API_KEY:
        raise ValueError("MaaS API key not configured")
        
    return MaasAPIClient(
        maas_url=config.MAAS_URL,
        api_key=config.MAAS_API_KEY,
        timeout=30,
        max_retries=3
    )


# Authentication Endpoints

@action('api/auth/login', method='POST')
def login():
    """
    User authentication endpoint
    
    POST /api/auth/login
    {
        "username": "admin",
        "password": "password"
    }
    """
    try:
        data = request.json
        if not data or 'username' not in data or 'password' not in data:
            return error_response('Username and password required', status=400)
            
        username = data['username']
        password = data['password']
        
        # Authenticate user
        user_info = auth_manager.authenticate_user(username, password)
        if not user_info:
            return error_response('Invalid credentials', code='authentication_failed', status=401)
            
        # Generate tokens
        tokens = auth_manager.generate_tokens(user_info)
        
        # Add user info to response
        response_data = {
            'user': {
                'id': user_info['id'],
                'username': user_info['username'],
                'email': user_info['email'],
                'role': user_info['role'],
                'permissions': user_info['permissions']
            },
            'tokens': tokens
        }
        
        return success_response(response_data, 'Authentication successful')
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return error_response('Login failed', status=500)


@action('api/auth/refresh', method='POST')
def refresh_token():
    """
    Refresh access token
    
    POST /api/auth/refresh
    {
        "refresh_token": "..."
    }
    """
    try:
        data = request.json
        if not data or 'refresh_token' not in data:
            return error_response('Refresh token required', status=400)
            
        refresh_token = data['refresh_token']
        
        # Refresh tokens
        tokens = auth_manager.refresh_access_token(refresh_token)
        if not tokens:
            return error_response('Invalid refresh token', status=401)
            
        return success_response(tokens, 'Token refreshed successfully')
        
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        return error_response('Token refresh failed', status=500)


@action('api/auth/logout', method='POST')
@auth_required()
def logout():
    """
    User logout endpoint
    
    POST /api/auth/logout
    Authorization: Bearer <token>
    """
    try:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            auth_manager.logout_user(token)
            
        return success_response(message='Logout successful')
        
    except Exception as e:
        logger.error(f"Logout error: {e}")
        return error_response('Logout failed', status=500)


@action('api/auth/profile', method='GET')
@auth_required()
def get_profile():
    """
    Get user profile
    
    GET /api/auth/profile
    Authorization: Bearer <token>
    """
    try:
        user = request.context.user
        
        # Get additional user details from database
        db_user = auth_manager.db(
            auth_manager.db.auth_users.id == user['id']
        ).select().first()
        
        if not db_user:
            return error_response('User not found', status=404)
            
        profile_data = {
            'id': db_user.id,
            'username': db_user.username,
            'email': db_user.email,
            'first_name': db_user.first_name,
            'last_name': db_user.last_name,
            'role': db_user.role,
            'permissions': user.get('permissions', []),
            'is_active': db_user.is_active,
            'email_verified': db_user.email_verified,
            'last_login': db_user.last_login.isoformat() if db_user.last_login else None,
            'login_count': db_user.login_count,
            'created_at': db_user.created_at.isoformat() if db_user.created_at else None
        }
        
        return success_response(profile_data)
        
    except Exception as e:
        logger.error(f"Get profile error: {e}")
        return error_response('Failed to get profile', status=500)


# Machine Management Endpoints

@action('api/machines', method='GET')
@auth_required(['machines:read'])
def get_machines():
    """
    Get list of machines from MaaS
    
    GET /api/machines?status=ready&zone=default&pool=default
    Authorization: Bearer <token>
    """
    try:
        # Parse query parameters
        filters = {}
        if 'status' in request.query:
            filters['status'] = request.query['status']
        if 'zone' in request.query:
            filters['zone'] = request.query['zone']
        if 'pool' in request.query:
            filters['pool'] = request.query['pool']
        if 'tags' in request.query:
            filters['tags'] = request.query['tags']
            
        # Get machines from MaaS
        async def fetch_machines():
            async with get_maas_client() as client:
                machines = await client.get_machines(filters)
                return [
                    {
                        'system_id': m.system_id,
                        'hostname': m.hostname,
                        'fqdn': m.fqdn,
                        'status': m.status.value,
                        'status_name': m.status_name,
                        'power_state': m.power_state.value,
                        'architecture': m.architecture,
                        'memory': m.memory,
                        'cpu_count': m.cpu_count,
                        'storage': m.storage,
                        'ip_addresses': m.ip_addresses,
                        'zone': m.zone,
                        'pool': m.pool,
                        'tags': m.tags,
                        'created': m.created.isoformat(),
                        'updated': m.updated.isoformat(),
                        'power_type': m.power_type,
                        'distro_series': m.distro_series,
                        'osystem': m.osystem,
                        'owner': m.owner
                    }
                    for m in machines
                ]
                
        # Run async operation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        machines_data = loop.run_until_complete(fetch_machines())
        loop.close()
        
        return success_response(machines_data)
        
    except Exception as e:
        logger.error(f"Get machines error: {e}")
        return error_response('Failed to get machines', status=500)


@action('api/machines/<system_id>', method='GET')
@auth_required(['machines:read'])
def get_machine(system_id):
    """
    Get specific machine details
    
    GET /api/machines/{system_id}
    Authorization: Bearer <token>
    """
    try:
        async def fetch_machine():
            async with get_maas_client() as client:
                machine = await client.get_machine(system_id)
                if not machine:
                    return None
                    
                return {
                    'system_id': machine.system_id,
                    'hostname': machine.hostname,
                    'fqdn': machine.fqdn,
                    'status': machine.status.value,
                    'status_name': machine.status_name,
                    'power_state': machine.power_state.value,
                    'architecture': machine.architecture,
                    'memory': machine.memory,
                    'cpu_count': machine.cpu_count,
                    'storage': machine.storage,
                    'ip_addresses': machine.ip_addresses,
                    'zone': machine.zone,
                    'pool': machine.pool,
                    'tags': machine.tags,
                    'created': machine.created.isoformat(),
                    'updated': machine.updated.isoformat(),
                    'power_type': machine.power_type,
                    'distro_series': machine.distro_series,
                    'osystem': machine.osystem,
                    'owner': machine.owner
                }
                
        # Run async operation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        machine_data = loop.run_until_complete(fetch_machine())
        loop.close()
        
        if not machine_data:
            return error_response('Machine not found', status=404)
            
        return success_response(machine_data)
        
    except Exception as e:
        logger.error(f"Get machine error: {e}")
        return error_response('Failed to get machine', status=500)


@action('api/machines/<system_id>/commission', method='POST')
@auth_required(['machines:commission'])
def commission_machine(system_id):
    """
    Commission a machine
    
    POST /api/machines/{system_id}/commission
    {
        "enable_ssh": true,
        "skip_networking": false,
        "skip_storage": false
    }
    """
    try:
        data = request.json or {}
        enable_ssh = data.get('enable_ssh', True)
        
        async def commission():
            async with get_maas_client() as client:
                return await client.commission_machine(system_id, enable_ssh)
                
        # Run async operation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(commission())
        loop.close()
        
        if success:
            # Log the operation
            auth_manager._log_audit_event(
                user_id=request.context.user['id'],
                username=request.context.user['username'],
                action='machine_commission',
                resource='machine',
                resource_id=system_id,
                success=True
            )
            
            return success_response(message=f'Machine {system_id} commissioning started')
        else:
            return error_response('Failed to commission machine', status=500)
            
    except Exception as e:
        logger.error(f"Commission machine error: {e}")
        return error_response('Failed to commission machine', status=500)


@action('api/machines/<system_id>/deploy', method='POST')
@auth_required(['machines:deploy'])
def deploy_machine(system_id):
    """
    Deploy operating system to machine
    
    POST /api/machines/{system_id}/deploy
    {
        "osystem": "ubuntu",
        "distro_series": "jammy",
        "user_data": "...",
        "storage_configuration": {...},
        "network_configuration": {...},
        "install_kvm": false
    }
    """
    try:
        data = request.json
        if not data:
            return error_response('Deployment configuration required', status=400)
            
        # Validate required fields
        if 'osystem' not in data or 'distro_series' not in data:
            return error_response('osystem and distro_series are required', status=400)
            
        # Create deployment config
        config_data = DeploymentConfig(
            osystem=data['osystem'],
            distro_series=data['distro_series'],
            user_data=data.get('user_data'),
            storage_configuration=data.get('storage_configuration'),
            network_configuration=data.get('network_configuration'),
            enable_ssh=data.get('enable_ssh', True),
            install_kvm=data.get('install_kvm', False)
        )
        
        async def deploy():
            async with get_maas_client() as client:
                return await client.deploy_machine(system_id, config_data)
                
        # Run async operation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(deploy())
        loop.close()
        
        if success:
            # Log the operation
            auth_manager._log_audit_event(
                user_id=request.context.user['id'],
                username=request.context.user['username'],
                action='machine_deploy',
                resource='machine',
                resource_id=system_id,
                success=True,
                details={
                    'osystem': data['osystem'],
                    'distro_series': data['distro_series']
                }
            )
            
            return success_response(message=f'Machine {system_id} deployment started')
        else:
            return error_response('Failed to deploy machine', status=500)
            
    except Exception as e:
        logger.error(f"Deploy machine error: {e}")
        return error_response('Failed to deploy machine', status=500)


@action('api/machines/<system_id>/power', method='POST')
@auth_required(['machines:power'])
def power_control(system_id):
    """
    Control machine power
    
    POST /api/machines/{system_id}/power
    {
        "action": "on|off|cycle"
    }
    """
    try:
        data = request.json
        if not data or 'action' not in data:
            return error_response('Power action required', status=400)
            
        action_type = data['action']
        valid_actions = ['on', 'off', 'cycle']
        
        if action_type not in valid_actions:
            return error_response(
                f'Invalid action. Must be one of: {", ".join(valid_actions)}',
                status=400
            )
            
        async def power_action():
            async with get_maas_client() as client:
                return await client.power_control(system_id, action_type)
                
        # Run async operation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(power_action())
        loop.close()
        
        if success:
            # Log the operation
            auth_manager._log_audit_event(
                user_id=request.context.user['id'],
                username=request.context.user['username'],
                action='machine_power',
                resource='machine',
                resource_id=system_id,
                success=True,
                details={'action': action_type}
            )
            
            return success_response(message=f'Power {action_type} command sent to {system_id}')
        else:
            return error_response('Failed to control power', status=500)
            
    except Exception as e:
        logger.error(f"Power control error: {e}")
        return error_response('Failed to control power', status=500)


@action('api/machines/<system_id>/release', method='POST')
@auth_required(['machines:deploy'])
def release_machine(system_id):
    """
    Release machine back to pool
    
    POST /api/machines/{system_id}/release
    {
        "erase_disk": true,
        "secure_erase": false,
        "quick_erase": false
    }
    """
    try:
        data = request.json or {}
        erase_disk = data.get('erase_disk', True)
        
        async def release():
            async with get_maas_client() as client:
                return await client.release_machine(system_id, erase_disk)
                
        # Run async operation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(release())
        loop.close()
        
        if success:
            # Log the operation
            auth_manager._log_audit_event(
                user_id=request.context.user['id'],
                username=request.context.user['username'],
                action='machine_release',
                resource='machine',
                resource_id=system_id,
                success=True,
                details={'erase_disk': erase_disk}
            )
            
            return success_response(message=f'Machine {system_id} release started')
        else:
            return error_response('Failed to release machine', status=500)
            
    except Exception as e:
        logger.error(f"Release machine error: {e}")
        return error_response('Failed to release machine', status=500)


# Deployment Management Endpoints

@action('api/deployments', method='GET')
@auth_required(['deployments:read'])
def get_deployments():
    """
    Get deployment history and status
    
    GET /api/deployments?limit=50&offset=0&status=in_progress
    Authorization: Bearer <token>
    """
    try:
        db = get_db()
        
        # Parse query parameters
        limit = min(int(request.query.get('limit', 50)), 100)
        offset = int(request.query.get('offset', 0))
        status_filter = request.query.get('status')
        
        # Build query
        query = db.deployments
        if status_filter:
            query = query(db.deployments.status == status_filter)
            
        # Get deployments
        deployments = query.select(
            limitby=(offset, offset + limit),
            orderby=~db.deployments.created_at
        )
        
        deployments_data = []
        for deployment in deployments:
            deployments_data.append({
                'id': deployment.id,
                'machine_id': deployment.machine_id,
                'hostname': deployment.hostname,
                'osystem': deployment.osystem,
                'distro_series': deployment.distro_series,
                'status': deployment.status,
                'progress': deployment.progress,
                'started_at': deployment.started_at.isoformat() if deployment.started_at else None,
                'completed_at': deployment.completed_at.isoformat() if deployment.completed_at else None,
                'error_message': deployment.error_message,
                'created_by': deployment.created_by,
                'created_at': deployment.created_at.isoformat()
            })
            
        # Get total count
        total = db(query).count()
        
        response_data = {
            'deployments': deployments_data,
            'pagination': {
                'limit': limit,
                'offset': offset,
                'total': total,
                'has_next': offset + limit < total
            }
        }
        
        return success_response(response_data)
        
    except Exception as e:
        logger.error(f"Get deployments error: {e}")
        return error_response('Failed to get deployments', status=500)


@action('api/deployments/<deployment_id:int>', method='GET')
@auth_required(['deployments:read'])
def get_deployment(deployment_id):
    """
    Get specific deployment details
    
    GET /api/deployments/{deployment_id}
    Authorization: Bearer <token>
    """
    try:
        db = get_db()
        
        deployment = db(db.deployments.id == deployment_id).select().first()
        if not deployment:
            return error_response('Deployment not found', status=404)
            
        deployment_data = {
            'id': deployment.id,
            'machine_id': deployment.machine_id,
            'hostname': deployment.hostname,
            'osystem': deployment.osystem,
            'distro_series': deployment.distro_series,
            'status': deployment.status,
            'progress': deployment.progress,
            'started_at': deployment.started_at.isoformat() if deployment.started_at else None,
            'completed_at': deployment.completed_at.isoformat() if deployment.completed_at else None,
            'error_message': deployment.error_message,
            'user_data': deployment.user_data,
            'storage_config': deployment.storage_config,
            'network_config': deployment.network_config,
            'created_by': deployment.created_by,
            'created_at': deployment.created_at.isoformat()
        }
        
        return success_response(deployment_data)
        
    except Exception as e:
        logger.error(f"Get deployment error: {e}")
        return error_response('Failed to get deployment', status=500)


# Health and Status Endpoints

@action('api/health', method='GET')
def health_check():
    """
    System health check endpoint
    
    GET /api/health
    """
    try:
        health_data = {
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'version': '1.0.0',
            'services': {}
        }
        
        # Check database
        try:
            db = get_db()
            db.executesql('SELECT 1')
            health_data['services']['database'] = {'status': 'healthy'}
        except Exception as e:
            health_data['services']['database'] = {'status': 'unhealthy', 'error': str(e)}
            health_data['status'] = 'degraded'
            
        # Check Redis
        try:
            redis_client.ping()
            health_data['services']['redis'] = {'status': 'healthy'}
        except Exception as e:
            health_data['services']['redis'] = {'status': 'unhealthy', 'error': str(e)}
            health_data['status'] = 'degraded'
            
        # Check MaaS API
        try:
            async def check_maas():
                async with get_maas_client() as client:
                    return await client.health_check()
                    
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            maas_healthy = loop.run_until_complete(check_maas())
            loop.close()
            
            health_data['services']['maas'] = {
                'status': 'healthy' if maas_healthy else 'unhealthy'
            }
            
            if not maas_healthy:
                health_data['status'] = 'degraded'
                
        except Exception as e:
            health_data['services']['maas'] = {'status': 'unhealthy', 'error': str(e)}
            health_data['status'] = 'degraded'
            
        return json_response(health_data)
        
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return json_response({
            'status': 'unhealthy',
            'timestamp': datetime.utcnow().isoformat(),
            'error': str(e)
        }, status=500)


@action('api/metrics', method='GET')
@auth_required(['metrics:read'])
def get_metrics():
    """
    Get system metrics
    
    GET /api/metrics
    Authorization: Bearer <token>
    """
    try:
        db = get_db()
        
        # Get deployment metrics
        deployment_stats = db.executesql("""
            SELECT 
                status,
                COUNT(*) as count
            FROM deployments 
            WHERE created_at >= datetime('now', '-24 hours')
            GROUP BY status
        """)
        
        # Get machine status counts
        async def get_machine_stats():
            async with get_maas_client() as client:
                machines = await client.get_machines()
                stats = {}
                for machine in machines:
                    status = machine.status.name
                    stats[status] = stats.get(status, 0) + 1
                return stats
                
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        machine_stats = loop.run_until_complete(get_machine_stats())
        loop.close()
        
        metrics_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'deployments': {
                'recent_24h': dict(deployment_stats)
            },
            'machines': {
                'by_status': machine_stats
            },
            'system': {
                'uptime': 'TBD',  # Would need system uptime tracking
                'version': '1.0.0'
            }
        }
        
        return success_response(metrics_data)
        
    except Exception as e:
        logger.error(f"Get metrics error: {e}")
        return error_response('Failed to get metrics', status=500)


# Error handling

@action.catch(HTTP)
def http_error_handler(exception):
    """Handle HTTP exceptions"""
    return error_response(
        message=getattr(exception, 'message', 'HTTP Error'),
        code=getattr(exception, 'code', 'http_error'),
        status=exception.status
    )