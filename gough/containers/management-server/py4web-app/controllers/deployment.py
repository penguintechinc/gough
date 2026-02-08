"""
Deployment Management Controller
Comprehensive deployment tracking, monitoring, and management
"""

from py4web import action, request, abort, redirect, URL, response
from py4web.utils.form import Form, FormStyleBootstrap4
from ..models import db
from ..lib.tasks.deployment import start_deployment
from ..modules.maas_client import MaaSClient
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

@action("deployment/dashboard")
@action.uses("deployment/dashboard.html", db)
def deployment_dashboard():
    """Deployment management dashboard"""
    
    # Get deployment statistics
    total_deployments = db(db.deployment_jobs).count()
    active_deployments = db(db.deployment_jobs.status.belongs(['Pending', 'Running'])).count()
    completed_deployments = db(db.deployment_jobs.status == 'Completed').count()
    failed_deployments = db(db.deployment_jobs.status == 'Failed').count()
    
    # Get recent deployments
    recent_deployments = db(db.deployment_jobs).select(
        orderby=~db.deployment_jobs.created_on,
        limitby=(0, 10)
    )
    
    # Get deployment progress data
    running_deployments = db(db.deployment_jobs.status == 'Running').select()
    
    # Get deployment statistics by template
    template_stats = db().select(
        db.cloud_init_templates.name,
        db.deployment_jobs.id.count().with_alias('count'),
        db.deployment_jobs.status,
        left=db.cloud_init_templates.on(
            db.cloud_init_templates.id == db.deployment_jobs.cloud_init_template_id
        ),
        groupby=[db.cloud_init_templates.name, db.deployment_jobs.status]
    )
    
    # Get server deployment distribution
    server_deployment_counts = db().select(
        db.servers.hostname,
        db.deployment_jobs.id.count().with_alias('deployment_count'),
        left=db.servers.on(db.servers.id == db.deployment_jobs.server_id),
        groupby=db.servers.hostname,
        having=db.deployment_jobs.id.count() > 0,
        orderby=~db.deployment_jobs.id.count(),
        limitby=(0, 10)
    )
    
    # Get deployment timeline data (last 30 days)
    timeline_data = db(
        db.deployment_jobs.created_on > datetime.utcnow() - timedelta(days=30)
    ).select(
        db.deployment_jobs.created_on,
        db.deployment_jobs.status,
        orderby=db.deployment_jobs.created_on
    )
    
    return {
        'stats': {
            'total': total_deployments,
            'active': active_deployments,
            'completed': completed_deployments,
            'failed': failed_deployments
        },
        'recent_deployments': recent_deployments,
        'running_deployments': running_deployments,
        'template_stats': template_stats,
        'server_deployment_counts': server_deployment_counts,
        'timeline_data': timeline_data
    }

@action("deployment/list")
@action.uses("deployment/list.html", db)
def deployment_list():
    """List all deployments with filtering and pagination"""
    
    # Get filter parameters
    status_filter = request.query.get('status', '')
    server_filter = request.query.get('server', '')
    template_filter = request.query.get('template', '')
    date_filter = request.query.get('date_range', '')
    
    # Build query
    query = db.deployment_jobs
    
    if status_filter:
        query = query(db.deployment_jobs.status == status_filter)
    
    if server_filter:
        try:
            server_id = int(server_filter)
            query = query(db.deployment_jobs.server_id == server_id)
        except ValueError:
            pass
    
    if template_filter:
        try:
            template_id = int(template_filter)
            query = query(db.deployment_jobs.cloud_init_template_id == template_id)
        except ValueError:
            pass
    
    if date_filter:
        if date_filter == 'today':
            cutoff = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            query = query(db.deployment_jobs.created_on >= cutoff)
        elif date_filter == 'week':
            cutoff = datetime.utcnow() - timedelta(days=7)
            query = query(db.deployment_jobs.created_on >= cutoff)
        elif date_filter == 'month':
            cutoff = datetime.utcnow() - timedelta(days=30)
            query = query(db.deployment_jobs.created_on >= cutoff)
    
    # Get deployments with pagination
    page = int(request.query.get('page', 1))
    per_page = 25
    
    # Join with servers and templates for display
    deployments = db(query._where).select(
        db.deployment_jobs.ALL,
        db.servers.hostname,
        db.cloud_init_templates.name.with_alias('template_name'),
        left=[
            db.servers.on(db.servers.id == db.deployment_jobs.server_id),
            db.cloud_init_templates.on(db.cloud_init_templates.id == db.deployment_jobs.cloud_init_template_id)
        ],
        orderby=~db.deployment_jobs.created_on,
        limitby=((page-1)*per_page, page*per_page)
    )
    
    total_deployments = query.count()
    total_pages = (total_deployments + per_page - 1) // per_page
    
    # Get filter options
    servers = db(db.servers).select(db.servers.id, db.servers.hostname, orderby=db.servers.hostname)
    templates = db(db.cloud_init_templates).select(
        db.cloud_init_templates.id, 
        db.cloud_init_templates.name, 
        orderby=db.cloud_init_templates.name
    )
    
    return {
        'deployments': deployments,
        'pagination': {
            'current_page': page,
            'total_pages': total_pages,
            'total_deployments': total_deployments,
            'per_page': per_page,
            'has_prev': page > 1,
            'has_next': page < total_pages
        },
        'servers': servers,
        'templates': templates,
        'current_filters': {
            'status': status_filter,
            'server': server_filter,
            'template': template_filter,
            'date_range': date_filter
        }
    }

@action("deployment/detail/<job_id:int>")
@action.uses("deployment/detail.html", db)
def deployment_detail(job_id):
    """Detailed view of a deployment job"""
    
    # Get deployment job with related data
    job = db().select(
        db.deployment_jobs.ALL,
        db.servers.hostname,
        db.servers.ip_address,
        db.cloud_init_templates.name.with_alias('template_name'),
        db.cloud_init_templates.description.with_alias('template_description'),
        left=[
            db.servers.on(db.servers.id == db.deployment_jobs.server_id),
            db.cloud_init_templates.on(db.cloud_init_templates.id == db.deployment_jobs.cloud_init_template_id)
        ],
        where=(db.deployment_jobs.id == job_id)
    ).first()
    
    if not job:
        abort(404)
    
    # Get deployment logs
    logs = db(
        (db.system_logs.component.belongs(['deployment', 'ansible'])) &
        (db.system_logs.metadata.contains(f'"job_id": {job_id}'))
    ).select(
        orderby=db.system_logs.created_on,
        limitby=(0, 500)
    )
    
    # Get deployment steps/tasks
    deployment_steps = []
    ansible_logs = [log for log in logs if 'ansible' in log.component.lower()]
    
    for log in ansible_logs:
        try:
            metadata = json.loads(log.metadata) if log.metadata else {}
            if 'task_name' in metadata:
                deployment_steps.append({
                    'timestamp': log.created_on,
                    'task': metadata['task_name'],
                    'status': 'completed' if log.level == 'INFO' else 'failed',
                    'message': log.message,
                    'details': metadata
                })
        except json.JSONDecodeError:
            continue
    
    # Get package configurations if any
    package_configs = []
    if job.deployment_jobs.package_config_ids:
        try:
            config_ids = json.loads(job.deployment_jobs.package_config_ids)
            package_configs = db(db.package_configs.id.belongs(config_ids)).select()
        except (json.JSONDecodeError, AttributeError):
            pass
    
    # Get rendered cloud-init configuration if available
    rendered_config = None
    if job.deployment_jobs.rendered_config:
        rendered_config = job.deployment_jobs.rendered_config
    
    return {
        'job': job,
        'logs': logs,
        'deployment_steps': deployment_steps,
        'package_configs': package_configs,
        'rendered_config': rendered_config
    }

@action("deployment/create")
@action.uses("deployment/create.html", db)
def deployment_create():
    """Create new deployment wizard"""
    
    # Get available servers
    available_servers = db(
        (db.servers.status.belongs(['Available', 'Ready', 'Deployed'])) &
        (db.servers.maas_node_id != None)
    ).select(orderby=db.servers.hostname)
    
    # Get available templates
    templates = db(db.cloud_init_templates.is_active == True).select(
        orderby=db.cloud_init_templates.name
    )
    
    # Get available package configurations
    packages = db(db.package_configs.is_active == True).select(
        orderby=db.package_configs.name
    )
    
    return {
        'available_servers': available_servers,
        'templates': templates,
        'packages': packages
    }

@action("deployment/bulk_create")
@action.uses("deployment/bulk_create.html", db)
def deployment_bulk_create():
    """Bulk deployment creation"""
    
    if request.method == 'POST':
        try:
            data = request.json or {}
            
            selected_servers = data.get('servers', [])
            template_id = data.get('template_id')
            package_ids = data.get('package_ids', [])
            deployment_name = data.get('deployment_name', 'Bulk Deployment')
            variables = data.get('variables', {})
            
            if not selected_servers or not template_id:
                return {'success': False, 'error': 'Missing required fields'}
            
            # Create deployment jobs for each server
            job_ids = []
            
            for server_id in selected_servers:
                server = db(db.servers.id == server_id).select().first()
                if not server:
                    continue
                
                job_data = {
                    'server_id': server_id,
                    'cloud_init_template_id': template_id,
                    'package_config_ids': json.dumps(package_ids),
                    'deployment_name': f"{deployment_name} - {server.hostname}",
                    'description': f'Bulk deployment to {server.hostname}',
                    'variables': json.dumps(variables),
                    'status': 'Pending',
                    'created_by': 'admin',  # TODO: Get from session
                    'created_on': datetime.utcnow()
                }
                
                job_id = db.deployment_jobs.insert(**job_data)
                job_ids.append(job_id)
                
                # Start deployment asynchronously
                start_deployment.delay(job_id)
            
            db.commit()
            
            # Log bulk deployment
            db.system_logs.insert(
                level='INFO',
                component='deployment',
                message=f'Bulk deployment created: {len(job_ids)} servers',
                metadata=json.dumps({
                    'job_ids': job_ids,
                    'server_count': len(job_ids),
                    'template_id': template_id
                })
            )
            
            return {
                'success': True,
                'job_ids': job_ids,
                'message': f'Created {len(job_ids)} deployment jobs'
            }
            
        except Exception as e:
            logger.error(f"Bulk deployment creation failed: {e}")
            return {'success': False, 'error': str(e)}
    
    # GET request - show form
    available_servers = db(
        (db.servers.status.belongs(['Available', 'Ready', 'Deployed'])) &
        (db.servers.maas_node_id != None)
    ).select(orderby=db.servers.hostname)
    
    templates = db(db.cloud_init_templates.is_active == True).select(
        orderby=db.cloud_init_templates.name
    )
    
    packages = db(db.package_configs.is_active == True).select(
        orderby=db.package_configs.name
    )
    
    return {
        'available_servers': available_servers,
        'templates': templates,
        'packages': packages
    }

@action("deployment/cancel/<job_id:int>")
@action.uses(db)
def deployment_cancel(job_id):
    """Cancel a deployment job"""
    
    job = db(db.deployment_jobs.id == job_id).select().first()
    if not job:
        response.status = 404
        return {'success': False, 'error': 'Deployment job not found'}
    
    if job.status not in ['Pending', 'Running']:
        response.status = 400
        return {'success': False, 'error': 'Cannot cancel completed deployment'}
    
    try:
        # Update job status
        db(db.deployment_jobs.id == job_id).update(
            status='Cancelled',
            finished_on=datetime.utcnow(),
            error_message='Cancelled by user'
        )
        
        # Log cancellation
        db.system_logs.insert(
            level='WARNING',
            component='deployment',
            message=f'Deployment job {job_id} cancelled',
            metadata=json.dumps({
                'job_id': job_id,
                'deployment_name': job.deployment_name
            })
        )
        
        db.commit()
        
        # TODO: Stop running Ansible processes if possible
        
        return {'success': True, 'message': 'Deployment cancelled'}
        
    except Exception as e:
        logger.error(f"Failed to cancel deployment {job_id}: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("deployment/retry/<job_id:int>")
@action.uses(db)
def deployment_retry(job_id):
    """Retry a failed deployment"""
    
    job = db(db.deployment_jobs.id == job_id).select().first()
    if not job:
        response.status = 404
        return {'success': False, 'error': 'Deployment job not found'}
    
    if job.status not in ['Failed', 'Cancelled']:
        response.status = 400
        return {'success': False, 'error': 'Can only retry failed or cancelled deployments'}
    
    try:
        # Reset job status
        db(db.deployment_jobs.id == job_id).update(
            status='Pending',
            progress=0,
            started_on=None,
            finished_on=None,
            error_message=None,
            retry_count=(job.retry_count or 0) + 1
        )
        
        # Log retry
        db.system_logs.insert(
            level='INFO',
            component='deployment',
            message=f'Deployment job {job_id} retried (attempt {(job.retry_count or 0) + 1})',
            metadata=json.dumps({
                'job_id': job_id,
                'deployment_name': job.deployment_name,
                'retry_count': (job.retry_count or 0) + 1
            })
        )
        
        db.commit()
        
        # Start deployment
        start_deployment.delay(job_id)
        
        return {'success': True, 'message': 'Deployment restarted'}
        
    except Exception as e:
        logger.error(f"Failed to retry deployment {job_id}: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("deployment/clone/<job_id:int>")
@action.uses(db)
def deployment_clone(job_id):
    """Clone a deployment job"""
    
    job = db(db.deployment_jobs.id == job_id).select().first()
    if not job:
        response.status = 404
        return {'success': False, 'error': 'Deployment job not found'}
    
    try:
        # Create cloned job
        cloned_data = {
            'server_id': job.server_id,
            'cloud_init_template_id': job.cloud_init_template_id,
            'package_config_ids': job.package_config_ids,
            'deployment_name': f"{job.deployment_name} (Clone)",
            'description': job.description,
            'variables': job.variables,
            'status': 'Pending',
            'created_by': 'admin',  # TODO: Get from session
            'created_on': datetime.utcnow()
        }
        
        new_job_id = db.deployment_jobs.insert(**cloned_data)
        
        # Log cloning
        db.system_logs.insert(
            level='INFO',
            component='deployment',
            message=f'Deployment job {job_id} cloned as {new_job_id}',
            metadata=json.dumps({
                'original_job_id': job_id,
                'cloned_job_id': new_job_id,
                'deployment_name': cloned_data['deployment_name']
            })
        )
        
        db.commit()
        
        return {
            'success': True,
            'job_id': new_job_id,
            'message': f'Deployment cloned as job #{new_job_id}'
        }
        
    except Exception as e:
        logger.error(f"Failed to clone deployment {job_id}: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("api/deployment/status/<job_id:int>")
@action.uses(db)
def api_deployment_status(job_id):
    """API endpoint for deployment status"""
    
    job = db(db.deployment_jobs.id == job_id).select().first()
    if not job:
        response.status = 404
        return {'error': 'Deployment job not found'}
    
    # Get recent logs for this deployment
    recent_logs = db(
        (db.system_logs.component.belongs(['deployment', 'ansible'])) &
        (db.system_logs.metadata.contains(f'"job_id": {job_id}'))
    ).select(
        orderby=~db.system_logs.created_on,
        limitby=(0, 5)
    )
    
    # Format recent log messages
    log_messages = []
    for log in recent_logs:
        log_messages.append({
            'timestamp': log.created_on.isoformat(),
            'level': log.level,
            'message': log.message
        })
    
    return {
        'job_id': job_id,
        'status': job.status,
        'progress': job.progress or 0,
        'deployment_name': job.deployment_name,
        'started_on': job.started_on.isoformat() if job.started_on else None,
        'finished_on': job.finished_on.isoformat() if job.finished_on else None,
        'error_message': job.error_message,
        'recent_logs': log_messages
    }

@action("api/deployment/logs/<job_id:int>")
@action.uses(db)
def api_deployment_logs(job_id):
    """API endpoint for deployment logs"""
    
    # Get logs for this deployment
    logs = db(
        (db.system_logs.component.belongs(['deployment', 'ansible'])) &
        (db.system_logs.metadata.contains(f'"job_id": {job_id}'))
    ).select(
        orderby=db.system_logs.created_on,
        limitby=(0, 1000)  # Limit to prevent excessive data
    )
    
    log_entries = []
    for log in logs:
        log_entries.append({
            'timestamp': log.created_on.isoformat(),
            'level': log.level,
            'component': log.component,
            'message': log.message
        })
    
    return {
        'job_id': job_id,
        'logs': log_entries,
        'total_count': len(log_entries)
    }

@action("deployment/templates")
@action.uses("deployment/templates.html", db)
def deployment_templates():
    """Deployment templates and presets"""
    
    # Get template usage statistics
    template_stats = db().select(
        db.cloud_init_templates.id,
        db.cloud_init_templates.name,
        db.cloud_init_templates.category,
        db.cloud_init_templates.description,
        db.deployment_jobs.id.count().with_alias('usage_count'),
        db.deployment_jobs.status,
        left=db.deployment_jobs.on(
            db.deployment_jobs.cloud_init_template_id == db.cloud_init_templates.id
        ),
        groupby=[
            db.cloud_init_templates.id,
            db.cloud_init_templates.name,
            db.cloud_init_templates.category,
            db.cloud_init_templates.description,
            db.deployment_jobs.status
        ]
    )
    
    # Organize statistics by template
    template_usage = {}
    for stat in template_stats:
        template_id = stat.cloud_init_templates.id
        if template_id not in template_usage:
            template_usage[template_id] = {
                'template': stat.cloud_init_templates,
                'total_deployments': 0,
                'successful_deployments': 0,
                'failed_deployments': 0
            }
        
        count = stat._extra['COUNT(deployment_jobs.id)'] or 0
        template_usage[template_id]['total_deployments'] += count
        
        if stat.deployment_jobs.status == 'Completed':
            template_usage[template_id]['successful_deployments'] += count
        elif stat.deployment_jobs.status == 'Failed':
            template_usage[template_id]['failed_deployments'] += count
    
    return {
        'template_usage': list(template_usage.values())
    }

@action("deployment/schedule")
@action.uses("deployment/schedule.html", db)
def deployment_schedule():
    """Scheduled deployments management"""
    
    # Get scheduled deployments (future scheduled_time)
    scheduled_deployments = db(
        (db.deployment_jobs.scheduled_time > datetime.utcnow()) &
        (db.deployment_jobs.status == 'Pending')
    ).select(
        db.deployment_jobs.ALL,
        db.servers.hostname,
        db.cloud_init_templates.name.with_alias('template_name'),
        left=[
            db.servers.on(db.servers.id == db.deployment_jobs.server_id),
            db.cloud_init_templates.on(db.cloud_init_templates.id == db.deployment_jobs.cloud_init_template_id)
        ],
        orderby=db.deployment_jobs.scheduled_time
    )
    
    return {
        'scheduled_deployments': scheduled_deployments
    }