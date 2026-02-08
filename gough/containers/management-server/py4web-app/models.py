from pydal import Database, Field
from pydal.validators import *
import datetime

# Define database tables
def define_tables(db):
    """Define all database tables for the MaaS management portal."""
    
    # MaaS Configuration table
    db.define_table('maas_config',
        Field('name', 'string', unique=True, notnull=True),
        Field('maas_url', 'string', notnull=True),
        Field('api_key', 'password', notnull=True),
        Field('username', 'string', notnull=True),
        Field('is_active', 'boolean', default=True),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(name)s'
    )
    
    # Server inventory table
    db.define_table('servers',
        Field('hostname', 'string', notnull=True, unique=True),
        Field('maas_system_id', 'string', unique=True),
        Field('mac_address', 'string', notnull=True),
        Field('ip_address', 'string'),
        Field('status', 'string', default='New'),
        Field('architecture', 'string', default='amd64'),
        Field('memory', 'integer'),  # in MB
        Field('cpu_count', 'integer'),
        Field('storage', 'integer'),  # in GB
        Field('power_type', 'string'),
        Field('power_parameters', 'text'),
        Field('zone', 'string', default='default'),
        Field('pool', 'string', default='default'),
        Field('tags', 'text'),  # JSON array of tags
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(hostname)s'
    )
    
    # Cloud-init templates table
    db.define_table('cloud_init_templates',
        Field('name', 'string', notnull=True, unique=True),
        Field('description', 'text'),
        Field('template_content', 'text', notnull=True),
        Field('template_type', 'string', default='user-data'),  # user-data, meta-data, network-config
        Field('is_default', 'boolean', default=False),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(name)s'
    )
    
    # Package configurations table
    db.define_table('package_configs',
        Field('name', 'string', notnull=True, unique=True),
        Field('description', 'text'),
        Field('packages', 'text', notnull=True),  # JSON array of packages
        Field('repositories', 'text'),  # JSON array of additional repositories
        Field('pre_install_scripts', 'text'),  # Bash scripts to run before package installation
        Field('post_install_scripts', 'text'),  # Bash scripts to run after package installation
        Field('is_default', 'boolean', default=False),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(name)s'
    )
    
    # Deployment jobs table
    db.define_table('deployment_jobs',
        Field('job_id', 'string', notnull=True, unique=True),
        Field('server_id', 'reference servers'),
        Field('cloud_init_template_id', 'reference cloud_init_templates'),
        Field('package_config_id', 'reference package_configs'),
        Field('status', 'string', default='Pending'),  # Pending, Running, Completed, Failed
        Field('ansible_playbook', 'string'),
        Field('log_output', 'text'),
        Field('error_message', 'text'),
        Field('started_on', 'datetime'),
        Field('completed_on', 'datetime'),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        format='Job %(job_id)s'
    )
    
    # FleetDM configuration table
    db.define_table('fleetdm_config',
        Field('name', 'string', unique=True, notnull=True),
        Field('fleet_url', 'string', notnull=True),
        Field('api_token', 'password', notnull=True),
        Field('is_active', 'boolean', default=True),
        Field('osquery_version', 'string', default='5.10.2'),
        Field('enroll_secret', 'password'),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(name)s'
    )
    
    # System logs table
    db.define_table('system_logs',
        Field('level', 'string', notnull=True),  # INFO, WARNING, ERROR, DEBUG
        Field('component', 'string', notnull=True),  # maas, ansible, fleetdm, etc.
        Field('message', 'text', notnull=True),
        Field('details', 'text'),  # JSON formatted details
        Field('server_id', 'reference servers'),
        Field('job_id', 'reference deployment_jobs'),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        format='%(level)s - %(component)s'
    )
    
    # FleetDM hosts tracking table
    db.define_table('fleet_hosts',
        Field('fleet_host_id', 'integer', notnull=True, unique=True),
        Field('hostname', 'string', notnull=True),
        Field('uuid', 'string', unique=True),
        Field('server_id', 'reference servers'),
        Field('last_seen', 'datetime'),
        Field('status', 'string', default='offline'),  # online, offline, mia, new
        Field('platform', 'string'),
        Field('osquery_version', 'string'),
        Field('enrollment_date', 'datetime'),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(hostname)s'
    )
    
    # Saved FleetDM queries table
    db.define_table('fleet_queries',
        Field('name', 'string', notnull=True, unique=True),
        Field('description', 'text'),
        Field('query', 'text', notnull=True),
        Field('fleet_query_id', 'integer'),
        Field('category', 'string', default='custom'),
        Field('is_scheduled', 'boolean', default=False),
        Field('interval_seconds', 'integer', default=3600),
        Field('created_by', 'string'),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(name)s'
    )
    
    # Query execution history table
    db.define_table('query_executions',
        Field('query_id', 'reference fleet_queries'),
        Field('campaign_id', 'integer'),
        Field('executed_by', 'string'),
        Field('target_hosts', 'text'),  # JSON array of host IDs
        Field('status', 'string', default='pending'),  # pending, running, completed, failed
        Field('results_count', 'integer', default=0),
        Field('execution_time', 'integer'),  # in seconds
        Field('error_message', 'text'),
        Field('executed_on', 'datetime', default=datetime.datetime.now),
        Field('completed_on', 'datetime'),
        format='Query %(id)s'
    )
    
    # FleetDM alerts configuration table
    db.define_table('fleet_alerts',
        Field('name', 'string', notnull=True, unique=True),
        Field('description', 'text'),
        Field('query_id', 'reference fleet_queries'),
        Field('alert_condition', 'string', notnull=True),  # query_results, host_offline, etc.
        Field('condition_parameters', 'text'),  # JSON parameters for condition
        Field('notification_channels', 'text'),  # JSON array of notification methods
        Field('is_active', 'boolean', default=True),
        Field('severity', 'string', default='medium'),  # low, medium, high, critical
        Field('cooldown_minutes', 'integer', default=60),
        Field('last_triggered', 'datetime'),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        Field('updated_on', 'datetime', update=datetime.datetime.now),
        format='%(name)s'
    )
    
    # Alert execution history table
    db.define_table('alert_history',
        Field('alert_id', 'reference fleet_alerts'),
        Field('triggered_by', 'string'),  # query_result, system_event, etc.
        Field('trigger_data', 'text'),  # JSON data that triggered the alert
        Field('notification_sent', 'boolean', default=False),
        Field('notification_channels_used', 'text'),  # JSON array
        Field('resolved', 'boolean', default=False),
        Field('resolved_by', 'string'),
        Field('resolution_notes', 'text'),
        Field('triggered_on', 'datetime', default=datetime.datetime.now),
        Field('resolved_on', 'datetime'),
        format='Alert %(alert_id)s - %(triggered_on)s'
    )
    
    # OSQuery result cache table (for dashboard display)
    db.define_table('osquery_results',
        Field('host_id', 'reference fleet_hosts'),
        Field('query_name', 'string', notnull=True),
        Field('result_data', 'text'),  # JSON formatted query results
        Field('result_hash', 'string'),  # Hash to detect changes
        Field('execution_time', 'datetime'),
        Field('created_on', 'datetime', default=datetime.datetime.now),
        format='%(host_id)s - %(query_name)s'
    )
    
    return db