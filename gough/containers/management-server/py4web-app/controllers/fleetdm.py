"""
FleetDM Management Controller
Provides web interface for FleetDM and OSQuery management
"""

from py4web import action, request, abort, redirect, URL, response
from py4web.utils.form import Form, FormStyleBootstrap4
from ..models import db
from ..modules.fleet_client import FleetClient
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

@action("fleetdm/dashboard")
@action.uses("fleetdm/dashboard.html", db)
def fleetdm_dashboard():
    """FleetDM dashboard page"""
    
    # Get FleetDM configuration
    fleet_config = db(db.fleetdm_config.is_active == True).select().first()
    
    if not fleet_config:
        return {
            'configured': False,
            'config_url': URL('fleetdm/config')
        }
    
    try:
        # Create FleetDM client
        client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
        
        if not client.test_connection():
            return {
                'configured': True,
                'connected': False,
                'error': 'Cannot connect to FleetDM server'
            }
        
        # Get dashboard statistics
        stats = client.get_system_stats()
        host_summary = client.get_host_status_summary()
        
        # Get recent hosts
        recent_hosts = db(db.fleet_hosts).select(
            orderby=~db.fleet_hosts.last_seen,
            limitby=(0, 10)
        )
        
        # Get recent query results
        recent_results = db(db.osquery_results).select(
            orderby=~db.osquery_results.created_on,
            limitby=(0, 20)
        )
        
        # Get scheduled queries
        scheduled_queries = db(db.osquery_scheduled_queries.is_active == True).select(
            orderby=db.osquery_scheduled_queries.name
        )
        
        # Get alerts (recent issues)
        alerts = db(
            (db.system_logs.component == 'fleet') &
            (db.system_logs.level.belongs(['WARNING', 'ERROR'])) &
            (db.system_logs.created_on > datetime.utcnow() - timedelta(hours=24))
        ).select(
            orderby=~db.system_logs.created_on,
            limitby=(0, 10)
        )
        
        return {
            'configured': True,
            'connected': True,
            'stats': stats,
            'host_summary': host_summary,
            'recent_hosts': recent_hosts,
            'recent_results': recent_results,
            'scheduled_queries': scheduled_queries,
            'alerts': alerts
        }
        
    except Exception as e:
        logger.error(f"FleetDM dashboard error: {e}")
        return {
            'configured': True,
            'connected': False,
            'error': str(e)
        }

@action("fleetdm/hosts")
@action.uses("fleetdm/hosts.html", db)
def fleetdm_hosts():
    """FleetDM hosts management page"""
    
    # Get filter parameters
    status_filter = request.query.get('status', '')
    platform_filter = request.query.get('platform', '')
    search_query = request.query.get('search', '')
    
    # Build query
    query = db.fleet_hosts
    
    if status_filter:
        if status_filter == 'online':
            query = query(db.fleet_hosts.online_status == True)
        elif status_filter == 'offline':
            query = query(db.fleet_hosts.online_status == False)
    
    if platform_filter:
        query = query(db.fleet_hosts.platform == platform_filter)
    
    if search_query:
        query = query(
            (db.fleet_hosts.hostname.contains(search_query)) |
            (db.fleet_hosts.fleet_host_id.contains(search_query))
        )
    
    # Get hosts with pagination
    page = int(request.query.get('page', 1))
    per_page = 20
    
    hosts = query.select(
        orderby=db.fleet_hosts.hostname,
        limitby=((page-1)*per_page, page*per_page)
    )
    
    total_hosts = query.count()
    total_pages = (total_hosts + per_page - 1) // per_page
    
    # Get unique values for filters
    platforms = db().select(
        db.fleet_hosts.platform,
        distinct=True,
        orderby=db.fleet_hosts.platform
    )
    
    return {
        'hosts': hosts,
        'pagination': {
            'current_page': page,
            'total_pages': total_pages,
            'total_hosts': total_hosts,
            'per_page': per_page,
            'has_prev': page > 1,
            'has_next': page < total_pages
        },
        'filters': {
            'platforms': [row.platform for row in platforms if row.platform],
            'current': {
                'status': status_filter,
                'platform': platform_filter,
                'search': search_query
            }
        }
    }

@action("fleetdm/host/<host_id:int>")
@action.uses("fleetdm/host_detail.html", db)
def fleetdm_host_detail(host_id):
    """FleetDM host detail page"""
    
    host = db(db.fleet_hosts.id == host_id).select().first()
    if not host:
        abort(404)
    
    # Get associated server
    server = db(db.servers.id == host.server_id).select().first() if host.server_id else None
    
    # Get recent OSQuery results for this host
    recent_results = db(db.osquery_results.host_id == host_id).select(
        orderby=~db.osquery_results.created_on,
        limitby=(0, 50)
    )
    
    # Get host system information from FleetDM
    fleet_config = db(db.fleetdm_config.is_active == True).select().first()
    host_info = None
    host_policies = []
    
    if fleet_config:
        try:
            client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
            host_info = client.get_host_details(host.fleet_host_id)
            host_policies = client.get_host_policies(host.fleet_host_id)
        except Exception as e:
            logger.error(f"Failed to get host details from FleetDM: {e}")
    
    return {
        'host': host,
        'server': server,
        'recent_results': recent_results,
        'host_info': host_info,
        'host_policies': host_policies
    }

@action("fleetdm/queries")
@action.uses("fleetdm/queries.html", db)
def fleetdm_queries():
    """FleetDM queries management page"""
    
    # Get scheduled queries
    scheduled_queries = db(db.osquery_scheduled_queries).select(
        orderby=[db.osquery_scheduled_queries.is_active.with_alias('active_first'), 
                db.osquery_scheduled_queries.name]
    )
    
    # Get recent query executions
    recent_executions = db(db.osquery_results).select(
        db.osquery_results.query_name,
        db.osquery_results.created_on.max().with_alias('last_run'),
        db.osquery_results.id.count().with_alias('execution_count'),
        groupby=db.osquery_results.query_name,
        orderby=~db.osquery_results.created_on.max(),
        limitby=(0, 20)
    )
    
    return {
        'scheduled_queries': scheduled_queries,
        'recent_executions': recent_executions
    }

@action("fleetdm/query/create")
@action("fleetdm/query/edit/<query_id:int>")
@action.uses("fleetdm/query_form.html", db)
def fleetdm_query_form(query_id=None):
    """Create or edit OSQuery scheduled query"""
    
    query = None
    if query_id:
        query = db(db.osquery_scheduled_queries.id == query_id).select().first()
        if not query:
            abort(404)
    
    # Create form
    form = Form([
        Field('name', 'string', length=255,
              requires=[IS_NOT_EMPTY(), IS_LENGTH(255)],
              default=query.name if query else ''),
        Field('description', 'text',
              default=query.description if query else ''),
        Field('sql_query', 'text',
              requires=IS_NOT_EMPTY(),
              default=query.sql_query if query else ''),
        Field('interval_seconds', 'integer',
              requires=IS_INT_IN_RANGE(30, 86400),
              default=query.interval_seconds if query else 3600,
              label='Interval (seconds)'),
        Field('target_hosts', 'text',
              default=query.target_hosts if query else 'all',
              label='Target Hosts (all, or specific host IDs)'),
        Field('is_active', 'boolean',
              default=query.is_active if query else True)
    ], formstyle=FormStyleBootstrap4)
    
    if form.accepted:
        try:
            data = {
                'name': form.vars.name,
                'description': form.vars.description,
                'sql_query': form.vars.sql_query,
                'interval_seconds': form.vars.interval_seconds,
                'target_hosts': form.vars.target_hosts,
                'is_active': form.vars.is_active,
                'updated_on': datetime.utcnow()
            }
            
            if query:
                # Update existing query
                db(db.osquery_scheduled_queries.id == query_id).update(**data)
                logger.info(f"Updated OSQuery scheduled query: {form.vars.name}")
            else:
                # Create new query
                data['created_on'] = datetime.utcnow()
                query_id = db.osquery_scheduled_queries.insert(**data)
                logger.info(f"Created OSQuery scheduled query: {form.vars.name}")
            
            # Sync with FleetDM if configured
            try:
                sync_query_with_fleetdm(query_id)
            except Exception as e:
                logger.warning(f"Failed to sync query with FleetDM: {e}")
            
            db.commit()
            redirect(URL('fleetdm/queries'))
            
        except Exception as e:
            logger.error(f"Failed to save query: {e}")
            form.errors['general'] = f'Failed to save query: {str(e)}'
    
    return {
        'form': form,
        'query': query,
        'is_edit': query is not None
    }

@action("fleetdm/query/execute/<query_id:int>")
@action.uses("fleetdm/query_execute.html", db)
def fleetdm_query_execute(query_id):
    """Execute an OSQuery on selected hosts"""
    
    query = db(db.osquery_scheduled_queries.id == query_id).select().first()
    if not query:
        abort(404)
    
    # Get available hosts
    hosts = db(db.fleet_hosts.online_status == True).select(
        orderby=db.fleet_hosts.hostname
    )
    
    if request.method == 'POST':
        try:
            # Get selected hosts
            selected_hosts = request.forms.getlist('selected_hosts')
            
            if not selected_hosts:
                return {
                    'query': query,
                    'hosts': hosts,
                    'error': 'Please select at least one host'
                }
            
            # Execute query via FleetDM
            fleet_config = db(db.fleetdm_config.is_active == True).select().first()
            if not fleet_config:
                return {
                    'query': query,
                    'hosts': hosts,
                    'error': 'FleetDM not configured'
                }
            
            client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
            
            # Execute query on selected hosts
            execution_results = []
            for host_id in selected_hosts:
                host = db(db.fleet_hosts.id == host_id).select().first()
                if host:
                    try:
                        result = client.execute_query(host.fleet_host_id, query.sql_query)
                        execution_results.append({
                            'host': host,
                            'success': True,
                            'result': result
                        })
                        
                        # Store result in database
                        db.osquery_results.insert(
                            host_id=host_id,
                            query_name=query.name,
                            sql_query=query.sql_query,
                            result_data=json.dumps(result.get('results', [])),
                            execution_time=result.get('execution_time', 0),
                            status='completed',
                            created_on=datetime.utcnow()
                        )
                        
                    except Exception as e:
                        execution_results.append({
                            'host': host,
                            'success': False,
                            'error': str(e)
                        })
            
            db.commit()
            
            return {
                'query': query,
                'hosts': hosts,
                'execution_results': execution_results,
                'executed': True
            }
            
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            return {
                'query': query,
                'hosts': hosts,
                'error': str(e)
            }
    
    return {
        'query': query,
        'hosts': hosts
    }

@action("fleetdm/policies")
@action.uses("fleetdm/policies.html", db)
def fleetdm_policies():
    """FleetDM policies management page"""
    
    fleet_config = db(db.fleetdm_config.is_active == True).select().first()
    
    if not fleet_config:
        return {
            'configured': False,
            'config_url': URL('fleetdm/config')
        }
    
    try:
        client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
        
        # Get policies from FleetDM
        policies = client.get_policies()
        
        # Get policy compliance statistics
        policy_stats = []
        for policy in policies:
            try:
                stats = client.get_policy_stats(policy['id'])
                policy_stats.append({
                    'policy': policy,
                    'stats': stats
                })
            except:
                policy_stats.append({
                    'policy': policy,
                    'stats': None
                })
        
        return {
            'configured': True,
            'connected': True,
            'policy_stats': policy_stats
        }
        
    except Exception as e:
        logger.error(f"Failed to get policies: {e}")
        return {
            'configured': True,
            'connected': False,
            'error': str(e)
        }

@action("fleetdm/config")
@action.uses("fleetdm/config.html", db)
def fleetdm_config():
    """FleetDM configuration page"""
    
    # Get current configuration
    config = db(db.fleetdm_config.is_active == True).select().first()
    
    # Create configuration form
    form = Form([
        Field('fleet_url', 'string', length=500,
              requires=[IS_NOT_EMPTY(), IS_URL()],
              default=config.fleet_url if config else '',
              label='Fleet Server URL'),
        Field('api_token', 'string', length=500,
              requires=IS_NOT_EMPTY(),
              default=config.api_token if config else '',
              label='API Token'),
        Field('webhook_secret', 'string', length=255,
              default=config.webhook_secret if config else '',
              label='Webhook Secret (optional)'),
        Field('sync_interval', 'integer',
              requires=IS_INT_IN_RANGE(60, 86400),
              default=config.sync_interval if config else 300,
              label='Sync Interval (seconds)'),
        Field('auto_enroll', 'boolean',
              default=config.auto_enroll if config else True,
              label='Auto-enroll new servers')
    ], formstyle=FormStyleBootstrap4)
    
    if form.accepted:
        try:
            # Test connection
            client = FleetClient(form.vars.fleet_url, form.vars.api_token)
            if not client.test_connection():
                form.errors['api_token'] = 'Cannot connect to FleetDM with provided credentials'
            else:
                # Deactivate current config
                db(db.fleetdm_config.is_active == True).update(is_active=False)
                
                # Insert new config
                db.fleetdm_config.insert(
                    fleet_url=form.vars.fleet_url,
                    api_token=form.vars.api_token,
                    webhook_secret=form.vars.webhook_secret,
                    sync_interval=form.vars.sync_interval,
                    auto_enroll=form.vars.auto_enroll,
                    is_active=True,
                    created_on=datetime.utcnow()
                )
                
                db.commit()
                
                # Log configuration change
                db.system_logs.insert(
                    level='INFO',
                    component='fleetdm_config',
                    message='FleetDM configuration updated',
                    metadata=json.dumps({
                        'fleet_url': form.vars.fleet_url,
                        'sync_interval': form.vars.sync_interval,
                        'auto_enroll': form.vars.auto_enroll
                    })
                )
                
                redirect(URL('fleetdm/dashboard'))
        except Exception as e:
            logger.error(f"FleetDM configuration error: {e}")
            form.errors['general'] = f'Configuration error: {str(e)}'
    
    # Test current connection
    connection_status = None
    if config:
        try:
            client = FleetClient(config.fleet_url, config.api_token)
            connection_status = {
                'connected': client.test_connection(),
                'url': config.fleet_url
            }
        except Exception as e:
            connection_status = {
                'connected': False,
                'error': str(e),
                'url': config.fleet_url
            }
    
    return {
        'form': form,
        'config': config,
        'connection_status': connection_status
    }

@action("fleetdm/sync")
@action.uses(db)
def fleetdm_sync():
    """Sync hosts from FleetDM"""
    
    try:
        fleet_config = db(db.fleetdm_config.is_active == True).select().first()
        if not fleet_config:
            response.status = 400
            return {'success': False, 'error': 'FleetDM not configured'}
        
        client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
        
        # Get hosts from FleetDM
        hosts = client.get_hosts()
        
        synced_count = 0
        updated_count = 0
        
        for host_data in hosts:
            # Check if host exists
            existing = db(db.fleet_hosts.fleet_host_id == host_data['id']).select().first()
            
            # Try to match with servers
            server_id = None
            if host_data.get('hostname'):
                server = db(db.servers.hostname == host_data['hostname']).select().first()
                if server:
                    server_id = server.id
            
            fleet_host_data = {
                'fleet_host_id': host_data['id'],
                'hostname': host_data.get('hostname', ''),
                'display_name': host_data.get('display_name', ''),
                'platform': host_data.get('platform', ''),
                'os_version': host_data.get('os_version', ''),
                'online_status': host_data.get('status') == 'online',
                'last_seen': datetime.fromisoformat(host_data['seen_time'].replace('Z', '+00:00')) if host_data.get('seen_time') else None,
                'server_id': server_id,
                'fleet_data': json.dumps(host_data)
            }
            
            if existing:
                # Update existing host
                db(db.fleet_hosts.id == existing.id).update(**fleet_host_data)
                updated_count += 1
            else:
                # Insert new host
                fleet_host_data['created_on'] = datetime.utcnow()
                db.fleet_hosts.insert(**fleet_host_data)
                synced_count += 1
        
        db.commit()
        
        # Log sync
        db.system_logs.insert(
            level='INFO',
            component='fleetdm_sync',
            message=f'Synced {synced_count} new hosts, updated {updated_count} existing hosts',
            metadata=json.dumps({
                'synced': synced_count,
                'updated': updated_count,
                'total_hosts': len(hosts)
            })
        )
        
        return {
            'success': True,
            'synced': synced_count,
            'updated': updated_count,
            'total': len(hosts)
        }
        
    except Exception as e:
        logger.error(f"FleetDM sync failed: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

def sync_query_with_fleetdm(query_id):
    """Sync scheduled query with FleetDM"""
    
    query = db(db.osquery_scheduled_queries.id == query_id).select().first()
    if not query:
        return
    
    fleet_config = db(db.fleetdm_config.is_active == True).select().first()
    if not fleet_config:
        return
    
    try:
        client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
        
        # Create or update query in FleetDM
        fleet_query_data = {
            'name': query.name,
            'description': query.description or '',
            'query': query.sql_query,
            'interval': query.interval_seconds,
            'observer_can_run': True
        }
        
        # Try to find existing query in FleetDM
        existing_queries = client.get_queries()
        existing_query = next((q for q in existing_queries if q['name'] == query.name), None)
        
        if existing_query:
            # Update existing query
            client.update_query(existing_query['id'], fleet_query_data)
        else:
            # Create new query
            client.create_query(fleet_query_data)
        
        logger.info(f"Synced query '{query.name}' with FleetDM")
        
    except Exception as e:
        logger.error(f"Failed to sync query with FleetDM: {e}")
        raise