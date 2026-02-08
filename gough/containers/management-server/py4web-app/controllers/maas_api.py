"""
MaaS API integration controller
"""

from py4web import action, request, abort, redirect, URL
from py4web.utils.form import Form, FormStyleBootstrap4
from ..models import db
from ..modules.maas_client import MaaSClient
import json
import logging

logger = logging.getLogger(__name__)

@action("maas/config")
@action.uses("maas_config.html", db)
def maas_config():
    """MaaS configuration page"""
    
    form = Form(
        [
            Field('name', requires=IS_NOT_EMPTY()),
            Field('maas_url', requires=[IS_NOT_EMPTY(), IS_URL()]),
            Field('api_key', 'password', requires=IS_NOT_EMPTY()),
            Field('username', requires=IS_NOT_EMPTY()),
            Field('is_active', 'boolean')
        ],
        formstyle=FormStyleBootstrap4
    )
    
    if form.accepted:
        # Deactivate other configs if this one is set as active
        if form.vars.is_active:
            db(db.maas_config.is_active == True).update(is_active=False)
        
        config_id = db.maas_config.insert(**form.vars)
        
        # Test the connection
        try:
            client = MaaSClient(form.vars.maas_url, form.vars.api_key)
            if client.test_connection():
                db.system_logs.insert(
                    level='INFO',
                    component='maas',
                    message=f'MaaS configuration "{form.vars.name}" added and tested successfully'
                )
                redirect(URL('maas/dashboard'))
            else:
                db.system_logs.insert(
                    level='WARNING',
                    component='maas',
                    message=f'MaaS configuration "{form.vars.name}" added but connection test failed'
                )
        except Exception as e:
            logger.error(f"Error testing MaaS connection: {str(e)}")
            db.system_logs.insert(
                level='ERROR',
                component='maas',
                message=f'Error testing MaaS connection: {str(e)}'
            )
    
    # Get existing configurations
    configs = db(db.maas_config).select(orderby=~db.maas_config.created_on)
    
    return {
        'form': form,
        'configs': configs
    }

@action("maas/dashboard")
@action.uses("maas_dashboard.html", db)
def maas_dashboard():
    """MaaS dashboard showing machines and status"""
    
    config = db(db.maas_config.is_active == True).select().first()
    if not config:
        redirect(URL('maas/config'))
    
    try:
        client = MaaSClient(config.maas_url, config.api_key)
        
        # Get machines from MaaS
        machines = client.get_machines()
        
        # Sync with local database
        for machine in machines:
            existing = db(db.servers.maas_system_id == machine['system_id']).select().first()
            
            machine_data = {
                'hostname': machine.get('hostname', ''),
                'maas_system_id': machine['system_id'],
                'mac_address': machine.get('boot_interface', {}).get('mac_address', ''),
                'ip_address': machine.get('ip_addresses', [''])[0] if machine.get('ip_addresses') else '',
                'status': machine.get('status_name', 'Unknown'),
                'architecture': machine.get('architecture', 'amd64'),
                'memory': machine.get('memory', 0),
                'cpu_count': machine.get('cpu_count', 0),
                'zone': machine.get('zone', {}).get('name', 'default'),
                'pool': machine.get('pool', {}).get('name', 'default'),
                'tags': json.dumps(machine.get('tags', []))
            }
            
            if existing:
                db(db.servers.id == existing.id).update(**machine_data)
            else:
                db.servers.insert(**machine_data)
        
        db.commit()
        
        # Get updated server list
        servers = db(db.servers).select(orderby=db.servers.hostname)
        
        # Get statistics
        stats = {
            'total': len(machines),
            'ready': len([m for m in machines if m.get('status_name') == 'Ready']),
            'deployed': len([m for m in machines if m.get('status_name') == 'Deployed']),
            'failed': len([m for m in machines if 'Failed' in m.get('status_name', '')])
        }
        
        return {
            'servers': servers,
            'stats': stats,
            'config': config
        }
        
    except Exception as e:
        logger.error(f"Error connecting to MaaS: {str(e)}")
        db.system_logs.insert(
            level='ERROR',
            component='maas',
            message=f'Error connecting to MaaS: {str(e)}'
        )
        return {
            'error': str(e),
            'config': config
        }

@action("maas/machine/<machine_id>")
@action.uses("machine_detail.html", db)
def machine_detail(machine_id):
    """Detailed view of a specific machine"""
    
    server = db(db.servers.maas_system_id == machine_id).select().first()
    if not server:
        abort(404)
    
    config = db(db.maas_config.is_active == True).select().first()
    if not config:
        redirect(URL('maas/config'))
    
    try:
        client = MaaSClient(config.maas_url, config.api_key)
        machine_detail = client.get_machine(machine_id)
        
        # Get deployment history
        jobs = db(db.deployment_jobs.server_id == server.id).select(
            orderby=~db.deployment_jobs.created_on
        )
        
        return {
            'server': server,
            'machine_detail': machine_detail,
            'jobs': jobs
        }
        
    except Exception as e:
        logger.error(f"Error getting machine details: {str(e)}")
        return {
            'error': str(e),
            'server': server
        }

@action("maas/deploy/<machine_id>", methods=['POST'])
def deploy_machine(machine_id):
    """Deploy a machine with specified configuration"""
    
    server = db(db.servers.maas_system_id == machine_id).select().first()
    if not server:
        abort(404)
    
    config = db(db.maas_config.is_active == True).select().first()
    if not config:
        abort(400, "No active MaaS configuration")
    
    try:
        # Get form data
        cloud_init_template_id = request.json.get('cloud_init_template_id')
        package_config_id = request.json.get('package_config_id')
        distro_series = request.json.get('distro_series', 'jammy')
        
        client = MaaSClient(config.maas_url, config.api_key)
        
        # Get cloud-init data
        user_data = ""
        if cloud_init_template_id:
            template = db.cloud_init_templates[cloud_init_template_id]
            if template:
                user_data = template.template_content
        
        # Deploy machine
        result = client.deploy_machine(
            machine_id,
            distro_series=distro_series,
            user_data=user_data
        )
        
        # Create deployment job record
        job_id = f"deploy_{machine_id}_{db().select(db.deployment_jobs.id.count()).first()._extra[db.deployment_jobs.id.count()] + 1}"
        
        db.deployment_jobs.insert(
            job_id=job_id,
            server_id=server.id,
            cloud_init_template_id=cloud_init_template_id,
            package_config_id=package_config_id,
            status='Running',
            started_on=request.now
        )
        
        db.system_logs.insert(
            level='INFO',
            component='maas',
            message=f'Deployment started for machine {machine_id}',
            server_id=server.id
        )
        
        return {'status': 'success', 'job_id': job_id, 'result': result}
        
    except Exception as e:
        logger.error(f"Error deploying machine: {str(e)}")
        db.system_logs.insert(
            level='ERROR',
            component='maas',
            message=f'Error deploying machine {machine_id}: {str(e)}',
            server_id=server.id
        )
        return {'status': 'error', 'message': str(e)}

@action("api/maas/refresh")
def refresh_maas_data():
    """API endpoint to refresh MaaS machine data"""
    
    config = db(db.maas_config.is_active == True).select().first()
    if not config:
        return {'status': 'error', 'message': 'No active MaaS configuration'}
    
    try:
        client = MaaSClient(config.maas_url, config.api_key)
        machines = client.get_machines()
        
        # Update database with fresh data
        for machine in machines:
            existing = db(db.servers.maas_system_id == machine['system_id']).select().first()
            
            machine_data = {
                'hostname': machine.get('hostname', ''),
                'status': machine.get('status_name', 'Unknown'),
                'ip_address': machine.get('ip_addresses', [''])[0] if machine.get('ip_addresses') else '',
            }
            
            if existing:
                db(db.servers.id == existing.id).update(**machine_data)
        
        db.commit()
        
        return {'status': 'success', 'count': len(machines)}
        
    except Exception as e:
        logger.error(f"Error refreshing MaaS data: {str(e)}")
        return {'status': 'error', 'message': str(e)}