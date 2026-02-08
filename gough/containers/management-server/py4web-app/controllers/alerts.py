"""
FleetDM Alerts Controller for Gough Management Portal
Handles alert configuration, management, and notification setup
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
            verify_ssl=False
        )
    except Exception as e:
        logger.error(f"Failed to create Fleet client: {e}")
        return None


@action("alerts")
@action.uses("alerts_dashboard.html", db)
def alerts_dashboard():
    """Alerts dashboard with overview and recent alerts"""
    
    # Get alert statistics
    total_alerts = db(db.fleet_alerts).count()
    active_alerts = db(db.fleet_alerts.is_active == True).count()
    recent_triggers = db(
        db.alert_history.triggered_on > datetime.now() - timedelta(hours=24)
    ).count()
    
    # Get recent alert history
    recent_history = db().select(
        db.alert_history.ALL,
        db.fleet_alerts.name,
        db.fleet_alerts.severity,
        left=db.fleet_alerts.on(db.alert_history.alert_id == db.fleet_alerts.id),
        orderby=~db.alert_history.triggered_on,
        limitby=(0, 20)
    )
    
    # Get alerts by severity
    severity_stats = db().select(
        db.fleet_alerts.severity,
        db.fleet_alerts.severity.count(),
        groupby=db.fleet_alerts.severity
    )
    
    severity_data = {}
    for row in severity_stats:
        severity_data[row.fleet_alerts.severity] = row._extra[db.fleet_alerts.severity.count()]
    
    return {
        'stats': {
            'total_alerts': total_alerts,
            'active_alerts': active_alerts,
            'recent_triggers': recent_triggers,
            'resolved_today': db(
                (db.alert_history.resolved == True) &
                (db.alert_history.resolved_on > datetime.now() - timedelta(hours=24))
            ).count()
        },
        'recent_history': recent_history,
        'severity_stats': severity_data
    }


@action("alerts/configure")
@action.uses("alerts_configure.html", db)
def alerts_configure():
    """Alert configuration page"""
    
    # Get existing alerts
    alerts = db(db.fleet_alerts).select(orderby=db.fleet_alerts.name)
    
    # Get available queries for alerts
    queries = db(db.fleet_queries).select(orderby=db.fleet_queries.name)
    
    return {
        'alerts': alerts,
        'queries': queries,
        'alert_conditions': get_alert_conditions(),
        'notification_channels': get_notification_channels(),
        'severity_levels': ['low', 'medium', 'high', 'critical']
    }


@action("alerts/create", methods=["GET", "POST"])
@action.uses("alerts_create.html", db)
def create_alert():
    """Create new alert configuration"""
    
    # Get available queries
    queries = db(db.fleet_queries).select(orderby=db.fleet_queries.name)
    
    # Create form
    form = Form(
        [
            Field('name', 'string', required=True, label='Alert Name'),
            Field('description', 'text', label='Description'),
            Field('query_id', 'reference fleet_queries', required=True, label='Query'),
            Field('alert_condition', 'string', required=True, label='Condition Type'),
            Field('condition_parameters', 'text', label='Condition Parameters (JSON)'),
            Field('notification_channels', 'text', label='Notification Channels (JSON)'),
            Field('severity', 'string', required=True, default='medium', label='Severity'),
            Field('cooldown_minutes', 'integer', default=60, label='Cooldown (minutes)'),
            Field('is_active', 'boolean', default=True, label='Active')
        ],
        formstyle=FormStyleBootstrap4
    )
    
    if form.accepted:
        try:
            # Insert new alert
            alert_id = db.fleet_alerts.insert(
                name=form.vars['name'],
                description=form.vars['description'],
                query_id=form.vars['query_id'],
                alert_condition=form.vars['alert_condition'],
                condition_parameters=form.vars['condition_parameters'],
                notification_channels=form.vars['notification_channels'],
                severity=form.vars['severity'],
                cooldown_minutes=form.vars['cooldown_minutes'],
                is_active=form.vars['is_active']
            )
            
            # Log the creation
            db.system_logs.insert(
                level='INFO',
                component='alerts',
                message=f'Alert created: {form.vars["name"]}',
                details=json.dumps({'alert_id': alert_id})
            )
            
            redirect(URL('alerts/configure'))
            
        except Exception as e:
            logger.error(f"Error creating alert: {e}")
            form.errors['name'] = str(e)
    
    return {
        'form': form,
        'queries': queries,
        'alert_conditions': get_alert_conditions(),
        'notification_channels': get_notification_channels(),
        'severity_levels': ['low', 'medium', 'high', 'critical']
    }


@action("alerts/edit/<alert_id:int>", methods=["GET", "POST"])
@action.uses("alerts_edit.html", db)
def edit_alert(alert_id):
    """Edit existing alert configuration"""
    
    alert = db.fleet_alerts[alert_id]
    if not alert:
        abort(404, "Alert not found")
    
    # Get available queries
    queries = db(db.fleet_queries).select(orderby=db.fleet_queries.name)
    
    # Create form with existing data
    form = Form(
        db.fleet_alerts,
        record=alert,
        formstyle=FormStyleBootstrap4
    )
    
    if form.accepted:
        try:
            # Log the update
            db.system_logs.insert(
                level='INFO',
                component='alerts',
                message=f'Alert updated: {alert.name}',
                details=json.dumps({'alert_id': alert_id})
            )
            
            redirect(URL('alerts/configure'))
            
        except Exception as e:
            logger.error(f"Error updating alert: {e}")
            form.errors['name'] = str(e)
    
    return {
        'form': form,
        'alert': alert,
        'queries': queries,
        'alert_conditions': get_alert_conditions(),
        'notification_channels': get_notification_channels(),
        'severity_levels': ['low', 'medium', 'high', 'critical']
    }


@action("alerts/test/<alert_id:int>", methods=["POST"])
def test_alert(alert_id):
    """Test an alert configuration"""
    
    alert = db.fleet_alerts[alert_id]
    if not alert:
        return {'success': False, 'error': 'Alert not found'}
    
    try:
        # Get the associated query
        query = db.fleet_queries[alert.query_id]
        if not query:
            return {'success': False, 'error': 'Associated query not found'}
        
        # Test the alert condition
        result = test_alert_condition(alert, query)
        
        # Log the test
        db.system_logs.insert(
            level='INFO',
            component='alerts',
            message=f'Alert test executed: {alert.name}',
            details=json.dumps({
                'alert_id': alert_id,
                'test_result': result
            })
        )
        
        return {'success': True, 'result': result}
        
    except Exception as e:
        logger.error(f"Error testing alert {alert_id}: {e}")
        return {'success': False, 'error': str(e)}


@action("alerts/history")
@action.uses("alerts_history.html", db)
def alerts_history():
    """Alert execution history page"""
    
    # Get filter parameters
    alert_filter = request.query.get('alert', '')
    severity_filter = request.query.get('severity', '')
    days_filter = int(request.query.get('days', 7))
    
    # Build query
    query = db.alert_history.triggered_on > (datetime.now() - timedelta(days=days_filter))
    
    if alert_filter:
        query &= db.alert_history.alert_id == alert_filter
    
    # Get history with alert details
    history = db().select(
        db.alert_history.ALL,
        db.fleet_alerts.name,
        db.fleet_alerts.severity,
        db.fleet_alerts.description,
        left=db.fleet_alerts.on(db.alert_history.alert_id == db.fleet_alerts.id),
        where=query,
        orderby=~db.alert_history.triggered_on,
        limitby=(0, 100)
    )
    
    # Apply severity filter if specified
    if severity_filter:
        history = [h for h in history if h.fleet_alerts.severity == severity_filter]
    
    # Get alerts for filter dropdown
    alerts = db(db.fleet_alerts).select(orderby=db.fleet_alerts.name)
    
    return {
        'history': history,
        'alerts': alerts,
        'filters': {
            'alert': alert_filter,
            'severity': severity_filter,
            'days': days_filter
        },
        'severity_levels': ['low', 'medium', 'high', 'critical']
    }


@action("alerts/resolve/<history_id:int>", methods=["POST"])
def resolve_alert(history_id):
    """Mark an alert as resolved"""
    
    try:
        data = request.json if request.json else {}
        resolution_notes = data.get('notes', '')
        resolved_by = data.get('resolved_by', 'system')
        
        # Update alert history
        db(db.alert_history.id == history_id).update(
            resolved=True,
            resolved_by=resolved_by,
            resolution_notes=resolution_notes,
            resolved_on=datetime.now()
        )
        
        # Log the resolution
        db.system_logs.insert(
            level='INFO',
            component='alerts',
            message=f'Alert resolved by {resolved_by}',
            details=json.dumps({
                'history_id': history_id,
                'resolution_notes': resolution_notes
            })
        )
        
        return {'success': True, 'message': 'Alert marked as resolved'}
        
    except Exception as e:
        logger.error(f"Error resolving alert {history_id}: {e}")
        return {'success': False, 'error': str(e)}


@action("api/alerts/trigger_check")
def trigger_check():
    """API endpoint to manually trigger alert checking"""
    
    try:
        # Get active alerts
        active_alerts = db(db.fleet_alerts.is_active == True).select()
        
        results = []
        for alert in active_alerts:
            try:
                query = db.fleet_queries[alert.query_id]
                if query:
                    result = check_alert_condition(alert, query)
                    results.append({
                        'alert_id': alert.id,
                        'alert_name': alert.name,
                        'triggered': result['triggered'],
                        'message': result.get('message', '')
                    })
            except Exception as e:
                logger.error(f"Error checking alert {alert.id}: {e}")
                results.append({
                    'alert_id': alert.id,
                    'alert_name': alert.name,
                    'error': str(e)
                })
        
        return {
            'success': True,
            'checked_alerts': len(results),
            'results': results
        }
        
    except Exception as e:
        logger.error(f"Error in trigger check: {e}")
        return {'success': False, 'error': str(e)}


def get_alert_conditions():
    """Get available alert condition types"""
    return [
        {
            'value': 'query_results_count',
            'label': 'Query Results Count',
            'description': 'Trigger when query returns more/less than specified number of results',
            'parameters': {
                'operator': ['>', '<', '=', '!='],
                'threshold': 'integer'
            }
        },
        {
            'value': 'query_results_contains',
            'label': 'Query Results Contains',
            'description': 'Trigger when query results contain specific values',
            'parameters': {
                'field': 'string',
                'value': 'string',
                'match_type': ['exact', 'contains', 'regex']
            }
        },
        {
            'value': 'host_offline',
            'label': 'Host Offline',
            'description': 'Trigger when a host has not been seen for specified time',
            'parameters': {
                'minutes': 'integer'
            }
        },
        {
            'value': 'new_host',
            'label': 'New Host Enrolled',
            'description': 'Trigger when a new host enrolls',
            'parameters': {}
        },
        {
            'value': 'scheduled_query',
            'label': 'Scheduled Query Execution',
            'description': 'Run query on schedule and check results',
            'parameters': {
                'interval_minutes': 'integer',
                'condition': 'nested'
            }
        }
    ]


def get_notification_channels():
    """Get available notification channel types"""
    return [
        {
            'value': 'email',
            'label': 'Email',
            'description': 'Send email notifications',
            'parameters': {
                'recipients': 'array',
                'subject_template': 'string'
            }
        },
        {
            'value': 'webhook',
            'label': 'Webhook',
            'description': 'HTTP webhook notification',
            'parameters': {
                'url': 'string',
                'method': ['POST', 'PUT'],
                'headers': 'object'
            }
        },
        {
            'value': 'slack',
            'label': 'Slack',
            'description': 'Slack channel notification',
            'parameters': {
                'webhook_url': 'string',
                'channel': 'string'
            }
        },
        {
            'value': 'syslog',
            'label': 'Syslog',
            'description': 'System log entry',
            'parameters': {
                'facility': 'string',
                'severity': 'string'
            }
        }
    ]


def test_alert_condition(alert, query):
    """Test an alert condition without triggering notifications"""
    try:
        condition_type = alert.alert_condition
        
        if condition_type == 'query_results_count':
            # Test by running the query and counting results
            fleet_client = get_fleet_client()
            if not fleet_client:
                return {'success': False, 'error': 'FleetDM not configured'}
            
            hosts = fleet_client.get_hosts()
            if not hosts:
                return {'success': False, 'error': 'No hosts available for testing'}
            
            # Use first host for testing
            host_ids = [hosts[0]['id']]
            result = fleet_client.run_live_query(query.query, host_ids)
            
            return {
                'success': True,
                'condition_type': condition_type,
                'test_query_executed': bool(result),
                'message': 'Test executed successfully - check FleetDM for results'
            }
        
        elif condition_type == 'host_offline':
            # Test by checking current host statuses
            fleet_client = get_fleet_client()
            if not fleet_client:
                return {'success': False, 'error': 'FleetDM not configured'}
            
            host_status = fleet_client.get_host_status_summary()
            offline_count = host_status.get('offline', 0) + host_status.get('missing_in_action', 0)
            
            return {
                'success': True,
                'condition_type': condition_type,
                'offline_hosts_found': offline_count,
                'message': f'Found {offline_count} offline hosts'
            }
        
        else:
            return {
                'success': True,
                'condition_type': condition_type,
                'message': 'Test condition not implemented for this type'
            }
            
    except Exception as e:
        logger.error(f"Error testing alert condition: {e}")
        return {'success': False, 'error': str(e)}


def check_alert_condition(alert, query):
    """Check if an alert condition is met (for actual alert processing)"""
    try:
        # Check cooldown period
        if alert.last_triggered:
            cooldown_end = alert.last_triggered + timedelta(minutes=alert.cooldown_minutes)
            if datetime.now() < cooldown_end:
                return {'triggered': False, 'message': 'In cooldown period'}
        
        condition_type = alert.alert_condition
        condition_params = json.loads(alert.condition_parameters or '{}')
        
        if condition_type == 'query_results_count':
            # Implementation for query results count checking
            return check_query_results_count(alert, query, condition_params)
        
        elif condition_type == 'host_offline':
            # Implementation for host offline checking
            return check_host_offline(alert, condition_params)
        
        elif condition_type == 'new_host':
            # Implementation for new host checking
            return check_new_host(alert, condition_params)
        
        else:
            return {'triggered': False, 'message': f'Unknown condition type: {condition_type}'}
            
    except Exception as e:
        logger.error(f"Error checking alert condition: {e}")
        return {'triggered': False, 'error': str(e)}


def check_query_results_count(alert, query, params):
    """Check query results count condition"""
    # This would be implemented to actually run the query and check results
    # For now, return a placeholder
    return {'triggered': False, 'message': 'Query results count check not fully implemented'}


def check_host_offline(alert, params):
    """Check host offline condition"""
    # This would be implemented to check host statuses
    # For now, return a placeholder
    return {'triggered': False, 'message': 'Host offline check not fully implemented'}


def check_new_host(alert, params):
    """Check new host enrollment condition"""
    # This would be implemented to check for new hosts
    # For now, return a placeholder
    return {'triggered': False, 'message': 'New host check not fully implemented'}