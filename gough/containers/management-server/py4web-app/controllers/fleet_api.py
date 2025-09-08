"""
FleetDM API Controller for Gough Management Portal
Handles FleetDM integration endpoints and fleet management
"""

from py4web import action, request, abort, redirect, URL
from py4web.utils.form import Form, FormStyleBootstrap4
from ..models import db
from ..modules.fleet_client import FleetClient, FleetAPIException
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def get_fleet_client() -> Optional[FleetClient]:
    """Get configured FleetDM client"""
    try:
        fleet_config = db(db.fleetdm_config.is_active == True).select().first()
        if not fleet_config:
            return None
        
        return FleetClient(
            fleet_url=fleet_config.fleet_url,
            api_token=fleet_config.api_token,
            verify_ssl=False  # Use False for development/self-signed certs
        )
    except Exception as e:
        logger.error(f"Failed to create Fleet client: {e}")
        return None


@action("fleet/dashboard")
@action.uses("fleet_dashboard.html", db)
def fleet_dashboard():
    """FleetDM dashboard with host overview and statistics"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {
            'error': 'FleetDM not configured. Please configure FleetDM settings first.',
            'stats': {},
            'hosts': [],
            'recent_activities': []
        }
    
    try:
        # Test connection
        if not fleet_client.test_connection():
            return {
                'error': 'Unable to connect to FleetDM server.',
                'stats': {},
                'hosts': [],
                'recent_activities': []
            }
        
        # Get system statistics
        stats = fleet_client.get_system_stats()
        
        # Get host list with basic information
        hosts = fleet_client.get_hosts()
        
        # Get recent activities
        recent_activities = fleet_client.get_activities(limit=10)
        
        # Get host status summary
        host_status_summary = fleet_client.get_host_status_summary()
        
        return {
            'stats': stats,
            'hosts': hosts[:20],  # Limit to first 20 hosts for dashboard
            'recent_activities': recent_activities,
            'host_status_summary': host_status_summary,
            'total_hosts_count': len(hosts)
        }
        
    except Exception as e:
        logger.error(f"Fleet dashboard error: {e}")
        return {
            'error': f'Error retrieving FleetDM data: {str(e)}',
            'stats': {},
            'hosts': [],
            'recent_activities': []
        }


@action("fleet/hosts")
@action.uses("fleet_hosts.html", db)
def fleet_hosts():
    """FleetDM hosts management page"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {
            'error': 'FleetDM not configured. Please configure FleetDM settings first.',
            'hosts': []
        }
    
    # Get search query from request
    search_query = request.query.get('query', '')
    
    try:
        # Get hosts with optional search
        hosts = fleet_client.get_hosts(query=search_query if search_query else None)
        
        # Enhance hosts with additional information
        for host in hosts:
            # Parse last seen time
            if host.get('seen_time'):
                try:
                    last_seen = datetime.fromisoformat(host['seen_time'].replace('Z', '+00:00'))
                    now = datetime.utcnow().replace(tzinfo=last_seen.tzinfo)
                    time_diff = now - last_seen
                    
                    if time_diff.total_seconds() < 1800:  # 30 minutes
                        host['status_display'] = 'Online'
                        host['status_class'] = 'success'
                    elif time_diff.days < 7:
                        host['status_display'] = 'Offline'
                        host['status_class'] = 'warning'
                    else:
                        host['status_display'] = 'MIA'
                        host['status_class'] = 'danger'
                except (ValueError, TypeError):
                    host['status_display'] = 'Unknown'
                    host['status_class'] = 'secondary'
            else:
                host['status_display'] = 'Never seen'
                host['status_class'] = 'secondary'
        
        return {
            'hosts': hosts,
            'search_query': search_query,
            'total_count': len(hosts)
        }
        
    except Exception as e:
        logger.error(f"Fleet hosts error: {e}")
        return {
            'error': f'Error retrieving hosts: {str(e)}',
            'hosts': []
        }


@action("fleet/host/<host_id:int>")
@action.uses("fleet_host_details.html", db)
def fleet_host_details(host_id):
    """Detailed view of a specific FleetDM host"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {'error': 'FleetDM not configured.', 'host': None}
    
    try:
        # Get host details
        host = fleet_client.get_host_details(host_id)
        if not host:
            abort(404, "Host not found")
        
        # Get host software
        software = fleet_client.get_host_software(host_id)
        
        # Get vulnerabilities for this host
        vulnerabilities = fleet_client.get_vulnerabilities(host_id=host_id)
        
        return {
            'host': host,
            'software': software[:50],  # Limit to first 50 software items
            'software_count': len(software),
            'vulnerabilities': vulnerabilities,
            'vulnerability_count': len(vulnerabilities)
        }
        
    except Exception as e:
        logger.error(f"Fleet host details error: {e}")
        return {
            'error': f'Error retrieving host details: {str(e)}',
            'host': None
        }


@action("fleet/queries")
@action.uses("fleet_queries.html", db)
def fleet_queries():
    """FleetDM queries management page"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {
            'error': 'FleetDM not configured.',
            'queries': [],
            'packs': []
        }
    
    try:
        # Get saved queries
        queries = fleet_client.get_saved_queries()
        
        # Get query packs
        packs = fleet_client.get_query_packs()
        
        return {
            'queries': queries,
            'packs': packs,
            'queries_count': len(queries),
            'packs_count': len(packs)
        }
        
    except Exception as e:
        logger.error(f"Fleet queries error: {e}")
        return {
            'error': f'Error retrieving queries: {str(e)}',
            'queries': [],
            'packs': []
        }


@action("fleet/query_builder")
@action.uses("fleet_query_builder.html", db)
def fleet_query_builder():
    """Query builder interface for creating and testing OSQuery queries"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {'error': 'FleetDM not configured.'}
    
    # Get hosts for query targeting
    try:
        from ..modules.osquery_schema import get_all_tables, get_query_patterns, OSQUERY_TABLES
        
        hosts = fleet_client.get_hosts()
        
        return {
            'hosts': hosts,
            'osquery_tables': OSQUERY_TABLES,
            'table_list': get_all_tables(),
            'query_patterns': get_query_patterns(),
            'predefined_queries': get_predefined_queries(),
            'saved_queries': db(db.fleet_queries).select(orderby=db.fleet_queries.name)
        }
    except Exception as e:
        logger.error(f"Query builder error: {e}")
        return {
            'error': f'Error loading query builder: {str(e)}',
            'hosts': []
        }


@action("fleet/run_query", methods=["POST"])
def run_live_query():
    """API endpoint to run live queries"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {'success': False, 'error': 'FleetDM not configured'}
    
    try:
        data = request.json
        query = data.get('query', '').strip()
        host_ids = data.get('host_ids', [])
        
        if not query:
            return {'success': False, 'error': 'Query is required'}
        
        if not host_ids:
            return {'success': False, 'error': 'At least one host must be selected'}
        
        # Run the query
        result = fleet_client.run_live_query(query, host_ids)
        
        if result:
            return {
                'success': True, 
                'campaign_id': result.get('campaign', {}).get('id'),
                'message': 'Query executed successfully'
            }
        else:
            return {'success': False, 'error': 'Failed to execute query'}
            
    except Exception as e:
        logger.error(f"Run live query error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/query_results/<campaign_id:int>")
def get_query_results(campaign_id):
    """API endpoint to get query results"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {'success': False, 'error': 'FleetDM not configured'}
    
    try:
        results = fleet_client.get_query_results(campaign_id)
        return {'success': True, 'results': results}
        
    except Exception as e:
        logger.error(f"Get query results error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/create_query", methods=["POST"])
def create_saved_query():
    """API endpoint to create a saved query"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {'success': False, 'error': 'FleetDM not configured'}
    
    try:
        data = request.json
        name = data.get('name', '').strip()
        query = data.get('query', '').strip()
        description = data.get('description', '').strip()
        
        if not name or not query:
            return {'success': False, 'error': 'Name and query are required'}
        
        result = fleet_client.create_query(name, query, description)
        
        if result:
            return {
                'success': True, 
                'query_id': result.get('id'),
                'message': 'Query saved successfully'
            }
        else:
            return {'success': False, 'error': 'Failed to save query'}
            
    except Exception as e:
        logger.error(f"Create query error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/enrollment")
@action.uses("fleet_enrollment.html", db)
def fleet_enrollment():
    """FleetDM enrollment management page"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {
            'error': 'FleetDM not configured.',
            'secrets': []
        }
    
    try:
        # Get enrollment secrets
        secrets = fleet_client.get_enrollment_secrets()
        
        return {
            'secrets': secrets,
            'fleet_url': get_fleet_client().fleet_url if get_fleet_client() else ''
        }
        
    except Exception as e:
        logger.error(f"Fleet enrollment error: {e}")
        return {
            'error': f'Error retrieving enrollment info: {str(e)}',
            'secrets': []
        }


@action("fleet/create_enrollment_secret", methods=["POST"])
def create_enrollment_secret():
    """API endpoint to create enrollment secret"""
    
    fleet_client = get_fleet_client()
    if not fleet_client:
        return {'success': False, 'error': 'FleetDM not configured'}
    
    try:
        data = request.json if request.json else {}
        name = data.get('name', f'Gough-Secret-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
        
        secret = fleet_client.create_enrollment_secret(name)
        
        if secret:
            return {
                'success': True,
                'secret': secret,
                'name': name,
                'message': 'Enrollment secret created successfully'
            }
        else:
            return {'success': False, 'error': 'Failed to create enrollment secret'}
            
    except Exception as e:
        logger.error(f"Create enrollment secret error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/validate_query", methods=["POST"])
def validate_query():
    """API endpoint to validate OSQuery syntax"""
    
    try:
        from ..modules.osquery_schema import validate_query_syntax
        
        data = request.json
        query = data.get('query', '').strip()
        
        if not query:
            return {'valid': False, 'error': 'Query is required'}
        
        is_valid, message = validate_query_syntax(query)
        
        return {
            'valid': is_valid,
            'message': message
        }
        
    except Exception as e:
        logger.error(f"Query validation error: {e}")
        return {'valid': False, 'error': str(e)}


@action("fleet/query_suggestions", methods=["POST"])
def get_query_suggestions():
    """API endpoint to get query completion suggestions"""
    
    try:
        from ..modules.osquery_schema import get_query_suggestions
        
        data = request.json
        partial_query = data.get('partial_query', '')
        
        suggestions = get_query_suggestions(partial_query)
        
        return {
            'success': True,
            'suggestions': suggestions
        }
        
    except Exception as e:
        logger.error(f"Query suggestions error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/table_info/<table_name>")
def get_table_info(table_name):
    """API endpoint to get detailed information about an OSQuery table"""
    
    try:
        from ..modules.osquery_schema import get_table_info
        
        table_info = get_table_info(table_name)
        
        if not table_info:
            return {'success': False, 'error': 'Table not found'}
        
        return {
            'success': True,
            'table_info': table_info
        }
        
    except Exception as e:
        logger.error(f"Table info error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/export_query/<query_id:int>")
def export_query(query_id):
    """Export a saved query in various formats"""
    
    try:
        query = db.fleet_queries[query_id]
        if not query:
            abort(404, "Query not found")
        
        format_type = request.query.get('format', 'json')
        
        if format_type == 'json':
            query_data = {
                'name': query.name,
                'description': query.description,
                'query': query.query,
                'category': query.category,
                'interval_seconds': query.interval_seconds,
                'created_on': query.created_on.isoformat() if query.created_on else None
            }
            
            return {
                'success': True,
                'format': 'json',
                'data': query_data
            }
        
        elif format_type == 'sql':
            return {
                'success': True,
                'format': 'sql',
                'data': f"-- {query.name}\n-- {query.description}\n{query.query}"
            }
        
        else:
            return {'success': False, 'error': 'Unsupported format'}
            
    except Exception as e:
        logger.error(f"Export query error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/import_query", methods=["POST"])
def import_query():
    """Import a query from JSON or SQL format"""
    
    try:
        data = request.json
        import_format = data.get('format', 'json')
        import_data = data.get('data', '')
        
        if not import_data:
            return {'success': False, 'error': 'Import data is required'}
        
        if import_format == 'json':
            try:
                import json
                query_data = json.loads(import_data)
                
                # Validate required fields
                if 'name' not in query_data or 'query' not in query_data:
                    return {'success': False, 'error': 'Name and query are required'}
                
                # Check if query with same name exists
                existing = db(db.fleet_queries.name == query_data['name']).select().first()
                if existing:
                    return {'success': False, 'error': 'Query with this name already exists'}
                
                # Insert the query
                query_id = db.fleet_queries.insert(
                    name=query_data['name'],
                    description=query_data.get('description', ''),
                    query=query_data['query'],
                    category=query_data.get('category', 'imported'),
                    interval_seconds=query_data.get('interval_seconds', 3600),
                    created_by='imported'
                )
                
                return {
                    'success': True,
                    'query_id': query_id,
                    'message': f'Query "{query_data["name"]}" imported successfully'
                }
                
            except json.JSONDecodeError:
                return {'success': False, 'error': 'Invalid JSON format'}
        
        elif import_format == 'sql':
            # Extract query name from SQL comments
            lines = import_data.split('\n')
            name = 'Imported Query'
            description = ''
            query = ''
            
            for line in lines:
                line = line.strip()
                if line.startswith('-- ') and not query:
                    if not description:
                        name = line[3:]
                    else:
                        description = line[3:]
                elif not line.startswith('--') and line:
                    query += line + '\n'
            
            if not query.strip():
                return {'success': False, 'error': 'No valid SQL query found'}
            
            # Check if query with same name exists
            existing = db(db.fleet_queries.name == name).select().first()
            if existing:
                name = f"{name} (imported)"
            
            # Insert the query
            query_id = db.fleet_queries.insert(
                name=name,
                description=description,
                query=query.strip(),
                category='imported',
                created_by='imported'
            )
            
            return {
                'success': True,
                'query_id': query_id,
                'message': f'Query "{name}" imported successfully'
            }
        
        else:
            return {'success': False, 'error': 'Unsupported import format'}
            
    except Exception as e:
        logger.error(f"Import query error: {e}")
        return {'success': False, 'error': str(e)}


@action("fleet/query_history")
@action.uses("fleet_query_history.html", db)
def query_history():
    """Query execution history page"""
    
    try:
        # Get query execution history with query details
        history = db().select(
            db.query_executions.ALL,
            db.fleet_queries.name,
            db.fleet_queries.description,
            left=db.fleet_queries.on(db.query_executions.query_id == db.fleet_queries.id),
            orderby=~db.query_executions.executed_on,
            limitby=(0, 50)
        )
        
        return {
            'history': history,
            'status_counts': get_execution_status_counts()
        }
        
    except Exception as e:
        logger.error(f"Query history error: {e}")
        return {
            'error': f'Error loading query history: {str(e)}',
            'history': []
        }


def get_execution_status_counts():
    """Get counts of query executions by status"""
    try:
        status_counts = db().select(
            db.query_executions.status,
            db.query_executions.status.count(),
            groupby=db.query_executions.status
        )
        
        counts = {}
        for row in status_counts:
            counts[row.query_executions.status] = row._extra[db.query_executions.status.count()]
        
        return counts
    except:
        return {}


@action("api/fleet/status")
def api_fleet_status():
    """API endpoint for FleetDM status"""
    
    try:
        fleet_client = get_fleet_client()
        if not fleet_client:
            return {
                'status': 'not_configured',
                'message': 'FleetDM not configured'
            }
        
        if fleet_client.test_connection():
            stats = fleet_client.get_system_stats()
            return {
                'status': 'connected',
                'stats': stats,
                'message': 'FleetDM connected successfully'
            }
        else:
            return {
                'status': 'connection_failed',
                'message': 'Unable to connect to FleetDM server'
            }
            
    except Exception as e:
        logger.error(f"Fleet status check error: {e}")
        return {
            'status': 'error',
            'message': str(e)
        }


def get_predefined_queries() -> List[Dict[str, str]]:
    """Get list of predefined useful queries"""
    return [
        {
            'name': 'System Information',
            'query': 'SELECT hostname, cpu_brand, cpu_physical_cores, cpu_logical_cores, physical_memory, hardware_vendor, hardware_model FROM system_info;',
            'description': 'Basic system hardware information'
        },
        {
            'name': 'Running Processes',
            'query': 'SELECT name, pid, cmdline, cpu_time, resident_size FROM processes ORDER BY cpu_time DESC LIMIT 20;',
            'description': 'Top 20 processes by CPU time'
        },
        {
            'name': 'Network Connections',
            'query': 'SELECT p.name, p.pid, p.cmdline, ps.local_port, ps.remote_address, ps.remote_port FROM processes p JOIN process_open_sockets ps ON p.pid = ps.pid WHERE ps.remote_address != "127.0.0.1" AND ps.remote_address != "::1" LIMIT 50;',
            'description': 'Active network connections'
        },
        {
            'name': 'Installed Software',
            'query': 'SELECT name, version, source FROM deb_packages WHERE name LIKE "%docker%" OR name LIKE "%kubernetes%" OR name LIKE "%ansible%" LIMIT 20;',
            'description': 'Key infrastructure software packages'
        },
        {
            'name': 'System Uptime',
            'query': 'SELECT days, hours, minutes, total_seconds FROM uptime;',
            'description': 'System uptime information'
        },
        {
            'name': 'Disk Usage',
            'query': 'SELECT device, path, blocks_size, blocks, blocks_free, blocks_available FROM mounts WHERE path IN ("/", "/home", "/var", "/tmp") AND device NOT LIKE "tmpfs%";',
            'description': 'Disk space usage for critical mount points'
        },
        {
            'name': 'SSH Login Attempts',
            'query': 'SELECT datetime(time,"unixepoch") AS datetime, username, address FROM last WHERE username != "" AND address != "console" AND time > (SELECT max(time) FROM last) - 86400;',
            'description': 'SSH login attempts in the last 24 hours'
        },
        {
            'name': 'Docker Containers',
            'query': 'SELECT name, image, status, created_at, ports FROM docker_containers;',
            'description': 'Docker container status and information'
        }
    ]