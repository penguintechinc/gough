#!/usr/bin/env python3
"""
Gough Management Server - Webhook Controllers  
MaaS webhook handlers for provisioning events and system notifications
Phase 8.2 - Integration Development
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional

from py4web import action, request, response, HTTP
from pydal import DAL

from ..lib.auth import auth_required, auth_manager
from ..lib.redis_client import get_redis_client
from ..lib.tasks.notifications import notify_deployment_complete, notify_deployment_failed
from ..lib.tasks.monitoring import sync_machines
from ..models import get_db
from settings import get_config

# Configure logging
logger = logging.getLogger(__name__)
config = get_config()

# Initialize services
redis_client = get_redis_client()


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify webhook signature using HMAC-SHA256
    
    Args:
        payload: Raw payload bytes
        signature: Provided signature
        secret: Webhook secret
        
    Returns:
        True if signature is valid
    """
    if not secret:
        logger.warning("Webhook secret not configured")
        return True  # Allow webhooks if no secret configured
        
    try:
        # Remove 'sha256=' prefix if present
        if signature.startswith('sha256='):
            signature = signature[7:]
            
        # Calculate expected signature
        expected = hmac.new(
            secret.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(expected, signature)
        
    except Exception as e:
        logger.error(f"Signature verification failed: {e}")
        return False


def json_response(data: Any, status: int = 200) -> str:
    """Return JSON response with proper headers"""
    response.headers['Content-Type'] = 'application/json'
    response.status = status
    return json.dumps(data, default=str, indent=2 if config.DEBUG else None)


def error_response(message: str, status: int = 400) -> str:
    """Return standardized error response"""
    return json_response({
        'error': message,
        'timestamp': datetime.utcnow().isoformat()
    }, status)


@action('webhooks/maas', method='POST')
def maas_webhook_handler():
    """
    Handle MaaS webhook events for machine provisioning
    
    POST /webhooks/maas
    X-MaaS-Event: machine.deployed
    X-MaaS-Signature: sha256=...
    Content-Type: application/json
    
    {
        "event": "machine.deployed", 
        "machine": {...},
        "timestamp": "2024-01-01T00:00:00Z"
    }
    """
    try:
        # Verify content type
        if request.headers.get('Content-Type') != 'application/json':
            return error_response('Content-Type must be application/json', 400)
            
        # Get raw payload for signature verification
        payload = request.body.read()
        if not payload:
            return error_response('Empty payload', 400)
            
        # Verify signature if configured
        signature = request.headers.get('X-MaaS-Signature', '')
        if not verify_webhook_signature(payload, signature, config.MAAS_WEBHOOK_SECRET):
            logger.warning(f"Invalid webhook signature from {request.environ.get('REMOTE_ADDR')}")
            return error_response('Invalid signature', 401)
            
        # Parse JSON payload
        try:
            data = json.loads(payload.decode('utf-8'))
        except json.JSONDecodeError as e:
            return error_response(f'Invalid JSON: {str(e)}', 400)
            
        # Extract event information
        event_type = data.get('event') or request.headers.get('X-MaaS-Event', '')
        machine_data = data.get('machine', {})
        timestamp = data.get('timestamp', datetime.utcnow().isoformat())
        
        if not event_type:
            return error_response('Missing event type', 400)
            
        if not machine_data or 'system_id' not in machine_data:
            return error_response('Missing machine data', 400)
            
        machine_id = machine_data['system_id']
        
        logger.info(f"Received MaaS webhook: {event_type} for machine {machine_id}")
        
        # Process different event types
        result = process_maas_event(event_type, machine_data, timestamp)
        
        # Store webhook event for auditing
        store_webhook_event('maas', event_type, machine_id, data)
        
        # Trigger machine sync in background
        sync_machines.delay()
        
        return json_response({
            'status': 'processed',
            'event': event_type,
            'machine_id': machine_id,
            'timestamp': datetime.utcnow().isoformat(),
            'result': result
        })
        
    except Exception as e:
        logger.error(f"MaaS webhook processing failed: {e}")
        return error_response('Webhook processing failed', 500)


def process_maas_event(event_type: str, machine_data: Dict, timestamp: str) -> Dict:
    """
    Process specific MaaS events and update system state
    
    Args:
        event_type: Type of MaaS event
        machine_data: Machine information from webhook
        timestamp: Event timestamp
        
    Returns:
        Processing result dictionary
    """
    db = get_db()
    machine_id = machine_data['system_id']
    
    try:
        # Update machine information in database
        existing_machine = db(db.machines.system_id == machine_id).select().first()
        
        machine_update_data = {
            'hostname': machine_data.get('hostname', ''),
            'fqdn': machine_data.get('fqdn', ''),
            'status': machine_data.get('status', 0),
            'status_name': machine_data.get('status_name', ''),
            'power_state': machine_data.get('power_state', 'unknown'),
            'architecture': machine_data.get('architecture', ''),
            'memory': machine_data.get('memory', 0),
            'cpu_count': machine_data.get('cpu_count', 0),
            'storage': machine_data.get('storage', 0.0),
            'ip_addresses': json.dumps(machine_data.get('ip_addresses', [])),
            'zone': machine_data.get('zone', {}).get('name', ''),
            'pool': machine_data.get('pool', {}).get('name', ''),
            'tags': json.dumps([tag.get('name', '') for tag in machine_data.get('tags', [])]),
            'power_type': machine_data.get('power_type', ''),
            'distro_series': machine_data.get('distro_series', ''),
            'osystem': machine_data.get('osystem', ''),
            'owner': machine_data.get('owner', ''),
            'last_sync': datetime.utcnow()
        }
        
        if existing_machine:
            db(db.machines.system_id == machine_id).update(**machine_update_data)
        else:
            machine_update_data['system_id'] = machine_id
            machine_update_data['first_seen'] = datetime.utcnow()
            db.machines.insert(**machine_update_data)
            
        db.commit()
        
        # Process specific event types
        result = {'action': 'machine_updated'}
        
        if event_type == 'machine.deployed':
            result.update(handle_machine_deployed(machine_id, machine_data))
            
        elif event_type == 'machine.failed_deployment':
            result.update(handle_machine_deployment_failed(machine_id, machine_data))
            
        elif event_type == 'machine.released':
            result.update(handle_machine_released(machine_id, machine_data))
            
        elif event_type == 'machine.commissioned':
            result.update(handle_machine_commissioned(machine_id, machine_data))
            
        elif event_type == 'machine.failed_commissioning':
            result.update(handle_machine_commissioning_failed(machine_id, machine_data))
            
        elif event_type == 'machine.power_on':
            result.update(handle_machine_power_change(machine_id, 'on'))
            
        elif event_type == 'machine.power_off':
            result.update(handle_machine_power_change(machine_id, 'off'))
            
        else:
            logger.info(f"Unhandled MaaS event type: {event_type}")
            result.update({'action': 'event_logged'})
            
        return result
        
    except Exception as e:
        logger.error(f"Error processing MaaS event {event_type}: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_machine_deployed(machine_id: str, machine_data: Dict) -> Dict:
    """Handle machine deployment completion"""
    db = get_db()
    
    try:
        # Find active deployment for this machine
        deployment = db(
            (db.deployments.machine_id == machine_id) &
            (db.deployments.status.belongs(['deploying', 'initializing']))
        ).select().first()
        
        if deployment:
            # Update deployment status
            db(db.deployments.id == deployment.id).update(
                status='completed',
                progress=100,
                completed_at=datetime.utcnow()
            )
            db.commit()
            
            # Update cache
            redis_client.set_json(f"deployment:{deployment.id}", {
                'id': deployment.id,
                'machine_id': machine_id,
                'status': 'completed',
                'progress': 100,
                'hostname': machine_data.get('hostname', machine_id),
                'completed_at': datetime.utcnow().isoformat()
            }, ex=3600)
            
            # Send notification
            notify_deployment_complete(deployment.id)
            
            logger.info(f"Deployment {deployment.id} completed for machine {machine_id}")
            
            return {
                'action': 'deployment_completed',
                'deployment_id': deployment.id
            }
        else:
            logger.warning(f"No active deployment found for deployed machine {machine_id}")
            return {'action': 'no_deployment_found'}
            
    except Exception as e:
        logger.error(f"Error handling machine deployment: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_machine_deployment_failed(machine_id: str, machine_data: Dict) -> Dict:
    """Handle machine deployment failure"""
    db = get_db()
    
    try:
        # Find active deployment for this machine
        deployment = db(
            (db.deployments.machine_id == machine_id) &
            (db.deployments.status.belongs(['deploying', 'initializing']))
        ).select().first()
        
        if deployment:
            error_message = machine_data.get('status_message', 'Deployment failed')
            
            # Update deployment status
            db(db.deployments.id == deployment.id).update(
                status='failed',
                error_message=error_message,
                completed_at=datetime.utcnow()
            )
            db.commit()
            
            # Update cache
            redis_client.set_json(f"deployment:{deployment.id}", {
                'id': deployment.id,
                'machine_id': machine_id,
                'status': 'failed',
                'hostname': machine_data.get('hostname', machine_id),
                'error': error_message,
                'completed_at': datetime.utcnow().isoformat()
            }, ex=3600)
            
            # Send notification
            notify_deployment_failed(deployment.id)
            
            logger.error(f"Deployment {deployment.id} failed for machine {machine_id}: {error_message}")
            
            return {
                'action': 'deployment_failed',
                'deployment_id': deployment.id,
                'error': error_message
            }
        else:
            logger.warning(f"No active deployment found for failed machine {machine_id}")
            return {'action': 'no_deployment_found'}
            
    except Exception as e:
        logger.error(f"Error handling machine deployment failure: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_machine_released(machine_id: str, machine_data: Dict) -> Dict:
    """Handle machine release"""
    db = get_db()
    
    try:
        # Log machine release
        logger.info(f"Machine {machine_id} has been released")
        
        # Update any active deployments
        active_deployments = db(
            (db.deployments.machine_id == machine_id) &
            (db.deployments.status.belongs(['deploying', 'initializing', 'commissioning']))
        ).select()
        
        for deployment in active_deployments:
            db(db.deployments.id == deployment.id).update(
                status='cancelled',
                error_message='Machine was released',
                completed_at=datetime.utcnow()
            )
            
        if active_deployments:
            db.commit()
            
        return {
            'action': 'machine_released',
            'cancelled_deployments': len(active_deployments)
        }
        
    except Exception as e:
        logger.error(f"Error handling machine release: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_machine_commissioned(machine_id: str, machine_data: Dict) -> Dict:
    """Handle machine commissioning completion"""
    db = get_db()
    
    try:
        logger.info(f"Machine {machine_id} commissioning completed")
        
        # Update any commissioning deployments
        deployment = db(
            (db.deployments.machine_id == machine_id) &
            (db.deployments.status == 'commissioning')
        ).select().first()
        
        if deployment:
            db(db.deployments.id == deployment.id).update(
                status='completed',
                progress=100,
                completed_at=datetime.utcnow()
            )
            db.commit()
            
            return {
                'action': 'commissioning_completed',
                'deployment_id': deployment.id
            }
            
        return {'action': 'commissioning_completed'}
        
    except Exception as e:
        logger.error(f"Error handling machine commissioning: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_machine_commissioning_failed(machine_id: str, machine_data: Dict) -> Dict:
    """Handle machine commissioning failure"""
    db = get_db()
    
    try:
        error_message = machine_data.get('status_message', 'Commissioning failed')
        logger.error(f"Machine {machine_id} commissioning failed: {error_message}")
        
        # Update any commissioning deployments
        deployment = db(
            (db.deployments.machine_id == machine_id) &
            (db.deployments.status == 'commissioning')
        ).select().first()
        
        if deployment:
            db(db.deployments.id == deployment.id).update(
                status='failed',
                error_message=error_message,
                completed_at=datetime.utcnow()
            )
            db.commit()
            
            return {
                'action': 'commissioning_failed',
                'deployment_id': deployment.id,
                'error': error_message
            }
            
        return {'action': 'commissioning_failed', 'error': error_message}
        
    except Exception as e:
        logger.error(f"Error handling machine commissioning failure: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_machine_power_change(machine_id: str, power_state: str) -> Dict:
    """Handle machine power state changes"""
    try:
        logger.info(f"Machine {machine_id} power state changed to: {power_state}")
        
        # Update power state cache
        redis_client.set_json(f"machine:{machine_id}:power", {
            'power_state': power_state,
            'updated_at': datetime.utcnow().isoformat()
        }, ex=3600)
        
        return {
            'action': 'power_state_updated',
            'power_state': power_state
        }
        
    except Exception as e:
        logger.error(f"Error handling power state change: {e}")
        return {'action': 'error', 'error': str(e)}


def store_webhook_event(source: str, event_type: str, resource_id: str, payload: Dict):
    """Store webhook event for auditing and debugging"""
    db = get_db()
    
    try:
        # Store in webhook events table
        db.webhook_events.insert(
            source=source,
            event_type=event_type,
            resource_id=resource_id,
            payload=json.dumps(payload, default=str),
            received_at=datetime.utcnow(),
            processed=True
        )
        db.commit()
        
        # Also store in Redis for quick access
        event_key = f"webhook_event:{source}:{event_type}:{resource_id}:{int(time.time())}"
        redis_client.set_json(event_key, {
            'source': source,
            'event_type': event_type,
            'resource_id': resource_id,
            'payload': payload,
            'received_at': datetime.utcnow().isoformat()
        }, ex=86400)  # Keep for 24 hours
        
    except Exception as e:
        logger.error(f"Failed to store webhook event: {e}")


@action('webhooks/fleet', method='POST')
def fleet_webhook_handler():
    """
    Handle FleetDM webhook events
    
    POST /webhooks/fleet
    Authorization: Bearer <api_key>
    Content-Type: application/json
    """
    try:
        # Simple authentication for FleetDM webhooks
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return error_response('Missing or invalid authorization', 401)
            
        # For FleetDM, we might use a different auth method
        # This is a placeholder for proper FleetDM webhook auth
        
        # Parse JSON payload
        try:
            data = json.loads(request.body.read().decode('utf-8'))
        except json.JSONDecodeError as e:
            return error_response(f'Invalid JSON: {str(e)}', 400)
            
        event_type = data.get('type', '')
        host_data = data.get('data', {})
        timestamp = data.get('timestamp', datetime.utcnow().isoformat())
        
        logger.info(f"Received FleetDM webhook: {event_type}")
        
        # Process FleetDM events
        result = process_fleet_event(event_type, host_data, timestamp)
        
        # Store webhook event
        store_webhook_event('fleet', event_type, host_data.get('id', ''), data)
        
        return json_response({
            'status': 'processed',
            'event': event_type,
            'timestamp': datetime.utcnow().isoformat(),
            'result': result
        })
        
    except Exception as e:
        logger.error(f"FleetDM webhook processing failed: {e}")
        return error_response('Webhook processing failed', 500)


def process_fleet_event(event_type: str, host_data: Dict, timestamp: str) -> Dict:
    """Process FleetDM webhook events"""
    db = get_db()
    
    try:
        # Map FleetDM events to our system
        if event_type == 'host_enrolled':
            return handle_host_enrolled(host_data)
        elif event_type == 'host_status_changed':
            return handle_host_status_changed(host_data)
        elif event_type == 'vulnerability_detected':
            return handle_vulnerability_detected(host_data)
        else:
            logger.info(f"Unhandled FleetDM event: {event_type}")
            return {'action': 'event_logged'}
            
    except Exception as e:
        logger.error(f"Error processing FleetDM event: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_host_enrolled(host_data: Dict) -> Dict:
    """Handle FleetDM host enrollment"""
    db = get_db()
    
    try:
        hostname = host_data.get('hostname', '')
        fleet_id = host_data.get('id', '')
        
        # Find corresponding machine by hostname
        machine = db(db.machines.hostname == hostname).select().first()
        if machine:
            db(db.machines.id == machine.id).update(
                fleet_host_id=fleet_id,
                fleet_status='enrolled',
                last_fleet_sync=datetime.utcnow()
            )
            db.commit()
            
            logger.info(f"Host {hostname} enrolled in FleetDM with ID {fleet_id}")
            return {'action': 'host_enrolled', 'machine_id': machine.system_id}
        else:
            logger.warning(f"No machine found for FleetDM host {hostname}")
            return {'action': 'no_machine_found'}
            
    except Exception as e:
        logger.error(f"Error handling host enrollment: {e}")
        return {'action': 'error', 'error': str(e)}


def handle_host_status_changed(host_data: Dict) -> Dict:
    """Handle FleetDM host status changes"""
    # This would update host status in our database
    # Implementation depends on FleetDM webhook payload structure
    return {'action': 'status_updated'}


def handle_vulnerability_detected(host_data: Dict) -> Dict:
    """Handle vulnerability detection events"""
    # This would process security vulnerabilities and create alerts
    # Implementation depends on requirements
    return {'action': 'vulnerability_logged'}


@action('webhooks/test', method='POST')
@auth_required(['webhooks:test'])
def webhook_test():
    """
    Test webhook endpoint for development and debugging
    
    POST /webhooks/test
    Authorization: Bearer <token>
    """
    try:
        payload = json.loads(request.body.read().decode('utf-8'))
        
        logger.info(f"Test webhook received: {payload}")
        
        # Store test event
        store_webhook_event('test', 'test_event', 'test', payload)
        
        return json_response({
            'status': 'received',
            'payload': payload,
            'headers': dict(request.headers),
            'timestamp': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Test webhook error: {e}")
        return error_response('Test webhook failed', 500)


@action('webhooks/events', method='GET')
@auth_required(['webhooks:read'])
def get_webhook_events():
    """
    Get recent webhook events for debugging
    
    GET /webhooks/events?limit=50&source=maas&event_type=machine.deployed
    Authorization: Bearer <token>
    """
    try:
        db = get_db()
        
        # Parse query parameters
        limit = min(int(request.query.get('limit', 50)), 100)
        source = request.query.get('source')
        event_type = request.query.get('event_type')
        
        # Build query
        query = db.webhook_events
        if source:
            query = query(db.webhook_events.source == source)
        if event_type:
            query = query(db.webhook_events.event_type == event_type)
            
        # Get events
        events = query.select(
            limitby=(0, limit),
            orderby=~db.webhook_events.received_at
        )
        
        events_data = []
        for event in events:
            try:
                payload = json.loads(event.payload) if event.payload else {}
            except json.JSONDecodeError:
                payload = {'raw': event.payload}
                
            events_data.append({
                'id': event.id,
                'source': event.source,
                'event_type': event.event_type,
                'resource_id': event.resource_id,
                'payload': payload,
                'received_at': event.received_at.isoformat(),
                'processed': event.processed
            })
            
        return json_response({
            'events': events_data,
            'count': len(events_data),
            'timestamp': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error getting webhook events: {e}")
        return error_response('Failed to get webhook events', 500)