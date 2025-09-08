"""
Server Management Controller
"""

from py4web import action, request, abort, redirect, URL, response
from py4web.utils.form import Form, FormStyleBootstrap4
from ..models import db
from ..modules.maas_client import MaaSClient
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

@action("servers/list")
@action.uses("servers/list.html", db)
def servers_list():
    """Server inventory page"""
    
    # Get filter parameters
    status_filter = request.query.get('status', '')
    server_type_filter = request.query.get('server_type', '')
    search_query = request.query.get('search', '')
    
    # Build query
    query = db.servers
    
    if status_filter:
        query = query(db.servers.status == status_filter)
    
    if server_type_filter:
        query = query(db.servers.server_type == server_type_filter)
    
    if search_query:
        query = query(
            (db.servers.hostname.contains(search_query)) |
            (db.servers.ip_address.contains(search_query)) |
            (db.servers.mac_address.contains(search_query))
        )
    
    # Get servers with pagination
    page = int(request.query.get('page', 1))
    per_page = int(request.query.get('per_page', 20))
    
    servers = query.select(
        orderby=db.servers.hostname,
        limitby=((page-1)*per_page, page*per_page)
    )
    
    total_servers = query.count()
    total_pages = (total_servers + per_page - 1) // per_page
    
    # Get unique values for filters
    statuses = db().select(
        db.servers.status,
        distinct=True,
        orderby=db.servers.status
    )
    
    server_types = db().select(
        db.servers.server_type,
        distinct=True,
        orderby=db.servers.server_type
    )
    
    return {
        'servers': servers,
        'pagination': {
            'current_page': page,
            'total_pages': total_pages,
            'total_servers': total_servers,
            'per_page': per_page,
            'has_prev': page > 1,
            'has_next': page < total_pages
        },
        'filters': {
            'statuses': [row.status for row in statuses if row.status],
            'server_types': [row.server_type for row in server_types if row.server_type],
            'current': {
                'status': status_filter,
                'server_type': server_type_filter,
                'search': search_query
            }
        }
    }

@action("servers/detail/<server_id:int>")
@action.uses("servers/detail.html", db)
def server_detail(server_id):
    """Server detail page"""
    
    server = db(db.servers.id == server_id).select().first()
    if not server:
        abort(404)
    
    # Get deployment history
    deployments = db(db.deployment_jobs.server_id == server_id).select(
        orderby=~db.deployment_jobs.created_on,
        limitby=(0, 10)
    )
    
    # Get recent OSQuery results for this server
    osquery_results = db(db.osquery_results.host_id == server_id).select(
        orderby=~db.osquery_results.created_on,
        limitby=(0, 20)
    )
    
    # Get FleetDM host information if available
    fleet_host = db(db.fleet_hosts.server_id == server_id).select().first()
    
    # Get system logs for this server
    logs = db(
        (db.system_logs.component == 'server') &
        (db.system_logs.metadata.contains(f'"server_id": {server_id}'))
    ).select(
        orderby=~db.system_logs.created_on,
        limitby=(0, 50)
    )
    
    return {
        'server': server,
        'deployments': deployments,
        'osquery_results': osquery_results,
        'fleet_host': fleet_host,
        'logs': logs
    }

@action("servers/deploy")
@action("servers/deploy/<server_id:int>")
@action.uses("servers/deploy.html", db)
def server_deploy(server_id=None):
    """Server deployment page"""
    
    if server_id:
        server = db(db.servers.id == server_id).select().first()
        if not server:
            abort(404)
    else:
        server = None
    
    # Get available cloud-init templates
    templates = db(db.cloud_init_templates.is_active == True).select(
        orderby=db.cloud_init_templates.name
    )
    
    # Get available package configurations
    packages = db(db.package_configs.is_active == True).select(
        orderby=db.package_configs.name
    )
    
    # Get available servers for deployment (if not specific server)
    available_servers = None
    if not server:
        available_servers = db(
            (db.servers.status.belongs(['Available', 'Ready'])) &
            (db.servers.maas_node_id != None)
        ).select(orderby=db.servers.hostname)
    
    # Create deployment form
    form_fields = []
    
    if not server:
        form_fields.append(Field('server_id', 'reference servers',
                                requires=IS_IN_DB(db(db.servers.status.belongs(['Available', 'Ready'])),
                                                 'servers.id', '%(hostname)s (%(ip_address)s)',
                                                 zero=T('Select a server...'))))
    
    form_fields.extend([
        Field('cloud_init_template_id', 'reference cloud_init_templates',
              requires=IS_IN_DB(db(db.cloud_init_templates.is_active == True),
                               'cloud_init_templates.id', '%(name)s',
                               zero=T('Select template...'))),
        Field('package_config_ids', 'list:reference package_configs',
              requires=IS_IN_DB(db(db.package_configs.is_active == True),
                               'package_configs.id', '%(name)s', multiple=True)),
        Field('deployment_name', 'string', length=255,
              requires=[IS_NOT_EMPTY(), IS_LENGTH(255)],
              label='Deployment Name'),
        Field('description', 'text',
              label='Description (optional)'),
        Field('schedule_time', 'datetime',
              label='Schedule Time (leave empty for immediate deployment)',
              requires=IS_EMPTY_OR(IS_DATETIME())),
        Field('variables', 'json',
              label='Template Variables (JSON)',
              default='{}',
              requires=IS_JSON())
    ])
    
    # Dynamic form creation based on available fields
    form = Form(form_fields, formstyle=FormStyleBootstrap4)
    
    if form.accepted:
        try:
            # Create deployment job
            deployment_data = {
                'server_id': server.id if server else form.vars.server_id,
                'cloud_init_template_id': form.vars.cloud_init_template_id,
                'package_config_ids': form.vars.package_config_ids or [],
                'deployment_name': form.vars.deployment_name,
                'description': form.vars.description or '',
                'variables': form.vars.variables or {},
                'status': 'Pending',
                'created_by': 'admin',  # TODO: Get from session
                'scheduled_time': form.vars.schedule_time
            }
            
            job_id = db.deployment_jobs.insert(**deployment_data)
            
            # If not scheduled, start deployment immediately
            if not form.vars.schedule_time:
                from ..lib.tasks.deployment import start_deployment
                start_deployment.delay(job_id)
            
            # Log the deployment
            db.system_logs.insert(
                level='INFO',
                component='deployment',
                message=f'Deployment job {job_id} created for server {deployment_data["server_id"]}',
                metadata=json.dumps({
                    'job_id': job_id,
                    'server_id': deployment_data['server_id'],
                    'deployment_name': deployment_data['deployment_name']
                })
            )
            
            db.commit()
            redirect(URL('deployment/status', job_id))
            
        except Exception as e:
            logger.error(f"Failed to create deployment: {e}")
            form.errors['general'] = f'Failed to create deployment: {str(e)}'
    
    return {
        'server': server,
        'form': form,
        'templates': templates,
        'packages': packages,
        'available_servers': available_servers
    }

@action("servers/sync")
@action.uses(db)
def sync_servers():
    """Sync servers from MaaS"""
    
    try:
        # Get MaaS configuration
        maas_config = db(db.maas_config.is_active == True).select().first()
        if not maas_config:
            response.status = 400
            return {'success': False, 'error': 'MaaS not configured'}
        
        # Create MaaS client
        client = MaaSClient(maas_config.maas_url, maas_config.api_key)
        if not client.test_connection():
            response.status = 500
            return {'success': False, 'error': 'Cannot connect to MaaS'}
        
        # Get machines from MaaS
        machines = client.get_machines()
        
        synced_count = 0
        updated_count = 0
        
        for machine in machines:
            # Check if server exists
            existing = db(db.servers.maas_node_id == machine['system_id']).select().first()
            
            server_data = {
                'hostname': machine.get('hostname', ''),
                'ip_address': machine.get('ip_addresses', [''])[0] if machine.get('ip_addresses') else '',
                'mac_address': machine.get('boot_interface', {}).get('mac_address', ''),
                'status': machine.get('status_name', 'Unknown'),
                'server_type': machine.get('node_type_name', 'Machine'),
                'cpu_count': machine.get('cpu_count', 0),
                'memory_mb': machine.get('memory', 0),
                'storage_gb': sum(bd.get('size', 0) for bd in machine.get('blockdevice_set', [])) // (1024**3),
                'maas_node_id': machine['system_id'],
                'maas_data': json.dumps(machine),
                'last_seen': datetime.utcnow()
            }
            
            if existing:
                # Update existing server
                db(db.servers.id == existing.id).update(**server_data)
                updated_count += 1
            else:
                # Insert new server
                db.servers.insert(**server_data)
                synced_count += 1
        
        db.commit()
        
        # Log the sync
        db.system_logs.insert(
            level='INFO',
            component='maas_sync',
            message=f'Synced {synced_count} new servers, updated {updated_count} existing servers',
            metadata=json.dumps({
                'synced': synced_count,
                'updated': updated_count,
                'total_machines': len(machines)
            })
        )
        
        return {
            'success': True,
            'synced': synced_count,
            'updated': updated_count,
            'total': len(machines)
        }
        
    except Exception as e:
        logger.error(f"Server sync failed: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("servers/power/<server_id:int>/<action>")
@action.uses(db)
def server_power(server_id, action):
    """Control server power"""
    
    if action not in ['on', 'off', 'restart']:
        response.status = 400
        return {'success': False, 'error': 'Invalid power action'}
    
    server = db(db.servers.id == server_id).select().first()
    if not server or not server.maas_node_id:
        response.status = 404
        return {'success': False, 'error': 'Server not found or not managed by MaaS'}
    
    try:
        # Get MaaS configuration
        maas_config = db(db.maas_config.is_active == True).select().first()
        if not maas_config:
            response.status = 400
            return {'success': False, 'error': 'MaaS not configured'}
        
        # Create MaaS client
        client = MaaSClient(maas_config.maas_url, maas_config.api_key)
        
        # Execute power action
        result = client.power_control(server.maas_node_id, action)
        
        if result.get('success'):
            # Update server status
            db(db.servers.id == server_id).update(
                status=f'Power {action.title()}',
                last_seen=datetime.utcnow()
            )
            
            # Log the action
            db.system_logs.insert(
                level='INFO',
                component='power_control',
                message=f'Power {action} executed for server {server.hostname}',
                metadata=json.dumps({
                    'server_id': server_id,
                    'hostname': server.hostname,
                    'action': action
                })
            )
            
            db.commit()
            return {'success': True, 'message': f'Power {action} executed successfully'}
        else:
            return {'success': False, 'error': result.get('error', 'Power action failed')}
            
    except Exception as e:
        logger.error(f"Power control failed for server {server_id}: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("api/servers/status/<server_id:int>")
@action.uses(db)
def api_server_status(server_id):
    """API endpoint for server status"""
    
    server = db(db.servers.id == server_id).select().first()
    if not server:
        response.status = 404
        return {'error': 'Server not found'}
    
    # Get latest deployment status
    latest_deployment = db(db.deployment_jobs.server_id == server_id).select(
        orderby=~db.deployment_jobs.created_on,
        limitby=(0, 1)
    ).first()
    
    # Get FleetDM status
    fleet_host = db(db.fleet_hosts.server_id == server_id).select().first()
    
    return {
        'server': {
            'id': server.id,
            'hostname': server.hostname,
            'ip_address': server.ip_address,
            'status': server.status,
            'last_seen': server.last_seen.isoformat() if server.last_seen else None
        },
        'deployment': {
            'status': latest_deployment.status if latest_deployment else None,
            'progress': latest_deployment.progress if latest_deployment else 0,
            'last_update': latest_deployment.updated_on.isoformat() if latest_deployment and latest_deployment.updated_on else None
        },
        'fleet': {
            'online': fleet_host.online_status if fleet_host else False,
            'last_seen': fleet_host.last_seen.isoformat() if fleet_host and fleet_host.last_seen else None
        }
    }