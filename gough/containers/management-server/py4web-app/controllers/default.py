"""
Default controller for MaaS Management Portal
"""

from py4web import action, request, abort, redirect, URL
from py4web.utils.form import Form, FormStyleBootstrap4
from ..models import db
import json
import logging

logger = logging.getLogger(__name__)

@action("index")
@action.uses("index.html", db)
def index():
    """Dashboard home page"""
    
    # Get system statistics
    total_servers = db(db.servers).count()
    active_jobs = db(db.deployment_jobs.status.belongs(['Pending', 'Running'])).count()
    completed_jobs = db(db.deployment_jobs.status == 'Completed').count()
    failed_jobs = db(db.deployment_jobs.status == 'Failed').count()
    
    # Get FleetDM/OSQuery statistics
    fleet_stats = get_fleet_dashboard_data()
    
    # Get recent logs
    recent_logs = db(db.system_logs).select(
        orderby=~db.system_logs.created_on,
        limitby=(0, 10)
    )
    
    # Get server status distribution
    server_statuses = db().select(
        db.servers.status,
        db.servers.status.count(),
        groupby=db.servers.status
    )
    
    status_data = []
    status_labels = []
    for row in server_statuses:
        status_labels.append(row.servers.status or 'Unknown')
        status_data.append(row._extra[db.servers.status.count()])
    
    # Get recent OSQuery results for key metrics
    recent_osquery_results = get_recent_osquery_results()
    
    return {
        'stats': {
            'total_servers': total_servers,
            'active_jobs': active_jobs,
            'completed_jobs': completed_jobs,
            'failed_jobs': failed_jobs
        },
        'fleet_stats': fleet_stats,
        'recent_logs': recent_logs,
        'recent_osquery_results': recent_osquery_results,
        'status_chart_data': {
            'labels': status_labels,
            'data': status_data
        }
    }


def get_fleet_dashboard_data():
    """Get FleetDM statistics for dashboard display"""
    try:
        from ..modules.fleet_client import FleetClient
        
        # Get FleetDM configuration
        fleet_config = db(db.fleetdm_config.is_active == True).select().first()
        if not fleet_config:
            return {
                'configured': False,
                'total_hosts': 0,
                'online_hosts': 0,
                'offline_hosts': 0
            }
        
        # Create FleetDM client
        client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
        if not client.test_connection():
            return {
                'configured': True,
                'connected': False,
                'total_hosts': 0,
                'online_hosts': 0,
                'offline_hosts': 0
            }
        
        # Get system statistics
        stats = client.get_system_stats()
        host_status = client.get_host_status_summary()
        
        return {
            'configured': True,
            'connected': True,
            'total_hosts': stats.get('total_hosts', 0),
            'online_hosts': host_status.get('online', 0),
            'offline_hosts': host_status.get('offline', 0),
            'new_hosts': host_status.get('new', 0),
            'mia_hosts': host_status.get('missing_in_action', 0),
            'total_queries': stats.get('total_queries', 0),
            'total_packs': stats.get('total_packs', 0)
        }
        
    except Exception as e:
        logger.error(f"Error getting Fleet dashboard data: {e}")
        return {
            'configured': True,
            'connected': False,
            'error': str(e),
            'total_hosts': 0,
            'online_hosts': 0,
            'offline_hosts': 0
        }


def get_recent_osquery_results():
    """Get recent OSQuery results for dashboard display"""
    try:
        # Get recent cached results
        recent_results = db(db.osquery_results).select(
            db.osquery_results.host_id,
            db.osquery_results.query_name,
            db.osquery_results.result_data,
            db.osquery_results.execution_time,
            orderby=~db.osquery_results.created_on,
            limitby=(0, 20)
        )
        
        results_summary = []
        for result in recent_results:
            try:
                # Parse JSON result data
                import json
                result_data = json.loads(result.result_data) if result.result_data else []
                
                # Get host information
                host = db(db.fleet_hosts.id == result.host_id).select().first()
                hostname = host.hostname if host else f"Host-{result.host_id}"
                
                results_summary.append({
                    'hostname': hostname,
                    'query_name': result.query_name,
                    'result_count': len(result_data) if isinstance(result_data, list) else 1,
                    'execution_time': result.execution_time,
                    'sample_data': result_data[:3] if isinstance(result_data, list) else result_data
                })
            except (json.JSONDecodeError, AttributeError):
                continue
        
        return results_summary
        
    except Exception as e:
        logger.error(f"Error getting recent OSQuery results: {e}")
        return []


@action("about")
@action.uses("about.html")
def about():
    """About page"""
    return dict()

@action("logs")
@action.uses("logs.html", db)
def logs():
    """System logs page"""
    
    # Filter parameters
    level_filter = request.query.get('level', '')
    component_filter = request.query.get('component', '')
    
    query = db.system_logs
    if level_filter:
        query = query(db.system_logs.level == level_filter)
    if component_filter:
        query = query(db.system_logs.component == component_filter)
    
    logs = query.select(
        orderby=~db.system_logs.created_on,
        limitby=(0, 100)
    )
    
    # Get unique levels and components for filters
    levels = db().select(
        db.system_logs.level,
        distinct=True,
        orderby=db.system_logs.level
    )
    
    components = db().select(
        db.system_logs.component,
        distinct=True,
        orderby=db.system_logs.component
    )
    
    return {
        'logs': logs,
        'levels': [row.level for row in levels],
        'components': [row.component for row in components],
        'current_filters': {
            'level': level_filter,
            'component': component_filter
        }
    }

@action("settings")
@action.uses("settings.html", db)
def settings():
    """System settings page"""
    
    # Get current MaaS configuration
    maas_config = db(db.maas_config.is_active == True).select().first()
    
    # Get current FleetDM configuration
    fleetdm_config = db(db.fleetdm_config.is_active == True).select().first()
    
    return {
        'maas_config': maas_config,
        'fleetdm_config': fleetdm_config
    }

@action("api/status")
def api_status():
    """API endpoint for system status"""
    
    try:
        # Check database connectivity
        db_status = "connected"
        try:
            db().select(db.servers.id, limitby=(0, 1))
        except Exception as e:
            db_status = f"error: {str(e)}"
        
        # Check MaaS connectivity
        maas_status = "not_configured"
        maas_config = db(db.maas_config.is_active == True).select().first()
        if maas_config:
            try:
                from ..modules.maas_client import MaaSClient
                client = MaaSClient(maas_config.maas_url, maas_config.api_key)
                if client.test_connection():
                    maas_status = "connected"
                else:
                    maas_status = "connection_failed"
            except Exception as e:
                maas_status = f"error: {str(e)}"
        
        # Check FleetDM connectivity
        fleet_status = "not_configured"
        fleet_config = db(db.fleetdm_config.is_active == True).select().first()
        if fleet_config:
            try:
                from ..modules.fleet_client import FleetClient
                client = FleetClient(fleet_config.fleet_url, fleet_config.api_token)
                if client.test_connection():
                    fleet_status = "connected"
                else:
                    fleet_status = "connection_failed"
            except Exception as e:
                fleet_status = f"error: {str(e)}"
        
        return {
            'status': 'ok',
            'components': {
                'database': db_status,
                'maas': maas_status,
                'fleetdm': fleet_status
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting system status: {str(e)}")
        return {
            'status': 'error',
            'message': str(e)
        }