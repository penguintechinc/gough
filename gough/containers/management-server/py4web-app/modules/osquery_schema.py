"""
OSQuery Schema Information for Query Builder
Provides table definitions and helper functions for building OSQuery queries
"""

# OSQuery tables commonly used in monitoring and security
OSQUERY_TABLES = {
    'system_info': {
        'description': 'System hardware and OS information',
        'columns': {
            'hostname': 'System hostname',
            'uuid': 'Unique system identifier',
            'cpu_brand': 'CPU brand string',
            'cpu_physical_cores': 'Number of physical CPU cores',
            'cpu_logical_cores': 'Number of logical CPU cores',
            'cpu_microcode': 'CPU microcode version',
            'physical_memory': 'Total physical memory in bytes',
            'hardware_vendor': 'System hardware vendor',
            'hardware_model': 'System hardware model',
            'hardware_version': 'System hardware version',
            'hardware_serial': 'System serial number',
            'computer_name': 'Friendly computer name',
            'local_hostname': 'Local hostname'
        },
        'examples': [
            'SELECT hostname, cpu_brand, physical_memory FROM system_info;',
            'SELECT hardware_vendor, hardware_model FROM system_info;'
        ]
    },
    
    'processes': {
        'description': 'All running processes on the system',
        'columns': {
            'pid': 'Process ID',
            'name': 'Process name',
            'path': 'Path to process binary',
            'cmdline': 'Complete command line',
            'state': 'Process state',
            'cwd': 'Process current working directory',
            'root': 'Process virtual root directory',
            'uid': 'Unsigned user ID',
            'gid': 'Unsigned group ID',
            'euid': 'Unsigned effective user ID',
            'egid': 'Unsigned effective group ID',
            'suid': 'Unsigned saved user ID',
            'sgid': 'Unsigned saved group ID',
            'wired_size': 'Bytes of unpagable memory used',
            'resident_size': 'Bytes of private memory used',
            'total_size': 'Total virtual memory size',
            'user_time': 'CPU time in user mode',
            'system_time': 'CPU time in kernel mode',
            'disk_bytes_read': 'Bytes read from disk',
            'disk_bytes_written': 'Bytes written to disk',
            'start_time': 'Process start time',
            'parent': 'Process parent PID',
            'pgroup': 'Process group ID',
            'threads': 'Number of threads',
            'nice': 'Process nice level'
        },
        'examples': [
            'SELECT name, pid, cmdline FROM processes WHERE name = "sshd";',
            'SELECT * FROM processes ORDER BY resident_size DESC LIMIT 10;',
            'SELECT name, COUNT(*) FROM processes GROUP BY name ORDER BY COUNT(*) DESC;'
        ]
    },
    
    'users': {
        'description': 'Local system users',
        'columns': {
            'uid': 'User ID',
            'gid': 'Group ID (primary)',
            'uid_signed': 'User ID as int64 signed',
            'gid_signed': 'Default group ID as int64 signed',
            'username': 'Username',
            'description': 'Optional user description',
            'directory': 'User home directory',
            'shell': 'User configured shell',
            'uuid': 'User UUID (macOS) or SID (Windows)'
        },
        'examples': [
            'SELECT username, uid, shell FROM users;',
            'SELECT username FROM users WHERE uid < 1000;'
        ]
    },
    
    'listening_ports': {
        'description': 'Processes with listening (bound) network sockets',
        'columns': {
            'pid': 'Process (or thread) ID',
            'port': 'Transport protocol port number',
            'protocol': 'Transport protocol (TCP/UDP)',
            'family': 'Network protocol (IPv4, IPv6)',
            'address': 'Specific address for bind',
            'fd': 'Socket file descriptor number',
            'socket': 'Socket handle or inode number',
            'path': 'For UNIX sockets, the domain path'
        },
        'examples': [
            'SELECT pid, port, protocol, address FROM listening_ports;',
            'SELECT * FROM listening_ports WHERE port = 22;'
        ]
    },
    
    'process_open_sockets': {
        'description': 'Processes with open network sockets',
        'columns': {
            'pid': 'Process (or thread) ID',
            'fd': 'Socket file descriptor number',
            'socket': 'Socket handle or inode number',
            'family': 'Network protocol (IPv4, IPv6)',
            'protocol': 'Transport protocol (TCP/UDP)',
            'local_address': 'Socket local address',
            'remote_address': 'Socket remote address',
            'local_port': 'Socket local port',
            'remote_port': 'Socket remote port',
            'path': 'For UNIX sockets, the domain path',
            'state': 'TCP socket state'
        },
        'examples': [
            'SELECT pid, local_port, remote_address, remote_port FROM process_open_sockets;',
            'SELECT * FROM process_open_sockets WHERE remote_address != "127.0.0.1";'
        ]
    },
    
    'file_events': {
        'description': 'File system events (requires file integrity monitoring)',
        'columns': {
            'target_path': 'The path associated with the event',
            'category': 'The category of the file defined in the config',
            'action': 'The action that was performed (CREATED, UPDATED, DELETED)',
            'transaction_id': 'ID used during bulk update',
            'inode': 'Filesystem inode number',
            'uid': 'Owning user ID',
            'gid': 'Owning group ID',
            'mode': 'Permission bits',
            'size': 'Size of file in bytes',
            'atime': 'Last access time',
            'mtime': 'Last modification time',
            'ctime': 'Last status change time',
            'md5': 'The MD5 of the file after change',
            'sha1': 'The SHA1 of the file after change',
            'sha256': 'The SHA256 of the file after change',
            'hashed': 'Whether the file was hashed',
            'time': 'Time of file event',
            'eid': 'Event ID'
        },
        'examples': [
            'SELECT target_path, action, time FROM file_events WHERE target_path LIKE "/etc/%";',
            'SELECT * FROM file_events WHERE action = "CREATED" AND time > strftime("%s", "now", "-1 hour");'
        ]
    },
    
    'last': {
        'description': 'System logins and logouts',
        'columns': {
            'username': 'Entry username',
            'tty': 'Entry terminal',
            'pid': 'Process (or thread) ID',
            'type': 'Entry type, according to ut_type types',
            'type_name': 'Entry type name',
            'time': 'Entry timestamp',
            'host': 'Entry hostname'
        },
        'examples': [
            'SELECT username, tty, host, datetime(time, "unixepoch") FROM last;',
            'SELECT * FROM last WHERE username != "" AND time > strftime("%s", "now", "-1 day");'
        ]
    },
    
    'syslog_events': {
        'description': 'Syslog entries (Linux/Unix)',
        'columns': {
            'time': 'Current unix epoch time',
            'datetime': 'Time known to syslog',
            'host': 'Hostname configured for syslog',
            'severity': 'Syslog severity',
            'facility': 'Syslog facility',
            'tag': 'The syslog tag',
            'message': 'The syslog message',
            'eid': 'Event ID'
        },
        'examples': [
            'SELECT datetime, severity, tag, message FROM syslog_events;',
            'SELECT * FROM syslog_events WHERE severity <= 3;'
        ]
    },
    
    'docker_containers': {
        'description': 'Docker containers information',
        'columns': {
            'id': 'Container ID',
            'name': 'Container name',
            'image': 'Docker image name',
            'image_id': 'Docker image ID',
            'command': 'Command with arguments',
            'created': 'Time of creation as UNIX time',
            'state': 'Container state (running, stopped, etc)',
            'status': 'Container status information',
            'pid': 'Identifier of the initial process',
            'path': 'Container path',
            'config_entrypoint': 'Container entrypoint(s)',
            'started_at': 'Container start time as string',
            'finished_at': 'Container finish time as string',
            'privileged': 'Is the container privileged',
            'security_options': 'List of container security options',
            'env_variables': 'Container environment variables',
            'readonly_rootfs': 'Is the root filesystem mounted as read only',
            'cgroup_namespace': 'cgroup namespace',
            'ipc_namespace': 'IPC namespace',
            'mnt_namespace': 'Mount namespace',
            'net_namespace': 'Network namespace',
            'pid_namespace': 'PID namespace',
            'user_namespace': 'User namespace',
            'uts_namespace': 'UTS namespace'
        },
        'examples': [
            'SELECT name, image, state, status FROM docker_containers;',
            'SELECT * FROM docker_containers WHERE state = "running";'
        ]
    },
    
    'memory_info': {
        'description': 'Main memory information',
        'columns': {
            'memory_total': 'Total amount of physical RAM, in bytes',
            'memory_free': 'The amount of physical RAM, in bytes, left unused',
            'memory_available': 'The amount of memory available',
            'buffers': 'The amount of physical RAM, in bytes, used for file buffers',
            'cached': 'The amount of physical RAM, in bytes, used as cache memory',
            'swap_cached': 'The amount of swap, in bytes, used as cache memory',
            'active': 'The amount of memory that has been used more recently',
            'inactive': 'The amount of memory that has been used less recently',
            'swap_total': 'The total amount of swap available, in bytes',
            'swap_free': 'The amount of swap space that is currently unused'
        },
        'examples': [
            'SELECT memory_total, memory_free, memory_available FROM memory_info;',
            'SELECT * FROM memory_info;'
        ]
    },
    
    'cpu_time': {
        'description': 'Displays the current cpu times',
        'columns': {
            'cpu': 'Name of the cpu (core)',
            'user': 'Time spent in user mode',
            'nice': 'Time spent in user mode with low priority (nice)',
            'system': 'Time spent in system mode',
            'idle': 'Time spent in the idle task',
            'iowait': 'Time spent waiting for I/O to complete',
            'irq': 'Time spent servicing hardware interrupts',
            'softirq': 'Time spent servicing software interrupts',
            'steal': 'Time spent in other operating systems',
            'guest': 'Time spent running a virtual CPU for a guest OS',
            'guest_nice': 'Time spent running a niced guest'
        },
        'examples': [
            'SELECT * FROM cpu_time WHERE cpu = "cpu";',
            'SELECT cpu, user, system, idle FROM cpu_time;'
        ]
    },
    
    'disk_stats': {
        'description': 'Disk I/O statistics',
        'columns': {
            'name': 'Device name',
            'reads': 'Number of reads completed',
            'read_merges': 'Number of read merges',
            'read_sectors': 'Number of sectors read',
            'read_time': 'Time spent reading (ms)',
            'writes': 'Number of writes completed',
            'write_merges': 'Number of write merges',
            'write_sectors': 'Number of sectors written',
            'write_time': 'Time spent writing (ms)',
            'io_time': 'Time spent doing I/Os (ms)',
            'weighted_io_time': 'Weighted time spent doing I/Os (ms)'
        },
        'examples': [
            'SELECT name, reads, writes, read_time, write_time FROM disk_stats;',
            'SELECT * FROM disk_stats WHERE name NOT LIKE "loop%";'
        ]
    }
}

# Common query patterns for the query builder
QUERY_PATTERNS = {
    'security_monitoring': {
        'name': 'Security Monitoring',
        'queries': [
            {
                'name': 'Failed Login Attempts',
                'query': 'SELECT username, tty, host, datetime(time, "unixepoch") as login_time FROM last WHERE username != "" AND tty NOT LIKE "tty%" AND time > strftime("%s", "now", "-24 hours") ORDER BY time DESC;',
                'description': 'Show failed login attempts in the last 24 hours'
            },
            {
                'name': 'Processes with Network Connections',
                'query': 'SELECT p.name, p.pid, p.cmdline, ps.local_port, ps.remote_address, ps.remote_port FROM processes p JOIN process_open_sockets ps ON p.pid = ps.pid WHERE ps.remote_address != "127.0.0.1" AND ps.remote_address != "::1";',
                'description': 'Find processes with external network connections'
            },
            {
                'name': 'Listening Network Services',
                'query': 'SELECT p.name, p.pid, lp.port, lp.protocol, lp.address FROM processes p JOIN listening_ports lp ON p.pid = lp.pid ORDER BY lp.port;',
                'description': 'Show all processes listening on network ports'
            },
            {
                'name': 'File System Changes',
                'query': 'SELECT target_path, action, datetime(time, "unixepoch") as event_time FROM file_events WHERE target_path IN ("/etc/passwd", "/etc/shadow", "/etc/hosts", "/etc/sudoers") ORDER BY time DESC LIMIT 50;',
                'description': 'Monitor changes to critical system files'
            }
        ]
    },
    
    'system_monitoring': {
        'name': 'System Monitoring',
        'queries': [
            {
                'name': 'System Overview',
                'query': 'SELECT hostname, cpu_brand, cpu_physical_cores, ROUND(physical_memory/1024/1024/1024, 2) as memory_gb, hardware_vendor, hardware_model FROM system_info;',
                'description': 'Get basic system hardware information'
            },
            {
                'name': 'Top CPU Processes',
                'query': 'SELECT name, pid, ROUND(((user_time + system_time) / 1000000.0), 2) as cpu_time_seconds, ROUND(resident_size/1024/1024, 2) as memory_mb FROM processes ORDER BY (user_time + system_time) DESC LIMIT 20;',
                'description': 'Find processes using the most CPU time'
            },
            {
                'name': 'Top Memory Processes',
                'query': 'SELECT name, pid, cmdline, ROUND(resident_size/1024/1024, 2) as memory_mb FROM processes WHERE resident_size > 0 ORDER BY resident_size DESC LIMIT 20;',
                'description': 'Find processes using the most memory'
            },
            {
                'name': 'Disk Usage',
                'query': 'SELECT name, reads, writes, ROUND(read_time/1000.0, 2) as read_time_sec, ROUND(write_time/1000.0, 2) as write_time_sec FROM disk_stats WHERE name NOT LIKE "loop%" AND name NOT LIKE "ram%";',
                'description': 'Show disk I/O statistics'
            }
        ]
    },
    
    'container_monitoring': {
        'name': 'Container Monitoring',
        'queries': [
            {
                'name': 'Docker Container Status',
                'query': 'SELECT name, image, state, status, datetime(created, "unixepoch") as created_time FROM docker_containers ORDER BY created DESC;',
                'description': 'Show all Docker containers and their status'
            },
            {
                'name': 'Running Containers',
                'query': 'SELECT name, image, pid, started_at FROM docker_containers WHERE state = "running";',
                'description': 'Show only running Docker containers'
            },
            {
                'name': 'Container Resource Usage',
                'query': 'SELECT dc.name, dc.image, p.resident_size/1024/1024 as memory_mb, p.user_time + p.system_time as cpu_time FROM docker_containers dc JOIN processes p ON dc.pid = p.pid WHERE dc.state = "running";',
                'description': 'Show resource usage for running containers'
            }
        ]
    },
    
    'user_activity': {
        'name': 'User Activity',
        'queries': [
            {
                'name': 'Current User Sessions',
                'query': 'SELECT username, tty, host, datetime(time, "unixepoch") as login_time FROM last WHERE tty LIKE "pts%" OR tty LIKE "console" ORDER BY time DESC LIMIT 20;',
                'description': 'Show recent user login sessions'
            },
            {
                'name': 'System Users',
                'query': 'SELECT username, uid, gid, directory, shell FROM users ORDER BY uid;',
                'description': 'List all system users'
            },
            {
                'name': 'Processes by User',
                'query': 'SELECT u.username, COUNT(p.pid) as process_count, SUM(p.resident_size)/1024/1024 as total_memory_mb FROM processes p LEFT JOIN users u ON p.uid = u.uid GROUP BY u.username ORDER BY process_count DESC;',
                'description': 'Show process counts and memory usage by user'
            }
        ]
    }
}

def get_table_info(table_name):
    """Get information about a specific OSQuery table"""
    return OSQUERY_TABLES.get(table_name, {})

def get_all_tables():
    """Get list of all available OSQuery tables"""
    return list(OSQUERY_TABLES.keys())

def get_table_columns(table_name):
    """Get column information for a specific table"""
    table_info = OSQUERY_TABLES.get(table_name, {})
    return table_info.get('columns', {})

def get_query_patterns():
    """Get all query patterns organized by category"""
    return QUERY_PATTERNS

def validate_query_syntax(query):
    """Basic validation of OSQuery SQL syntax"""
    query = query.strip().upper()
    
    # Check for basic SQL structure
    if not query.startswith('SELECT'):
        return False, "Query must start with SELECT"
    
    # Check for potential dangerous operations
    dangerous_keywords = ['DROP', 'DELETE', 'UPDATE', 'INSERT', 'CREATE', 'ALTER']
    for keyword in dangerous_keywords:
        if keyword in query:
            return False, f"Dangerous keyword '{keyword}' not allowed"
    
    # Check for FROM clause
    if 'FROM' not in query:
        return False, "Query must include FROM clause"
    
    return True, "Query syntax appears valid"

def get_query_suggestions(partial_query):
    """Get suggestions for query completion"""
    suggestions = []
    
    partial_upper = partial_query.upper()
    
    # Suggest table names after FROM
    if 'FROM ' in partial_upper and not partial_query.endswith(' '):
        last_word = partial_query.split()[-1].lower()
        for table in OSQUERY_TABLES:
            if table.startswith(last_word):
                suggestions.append({
                    'type': 'table',
                    'value': table,
                    'description': OSQUERY_TABLES[table]['description']
                })
    
    # Suggest common SQL keywords
    if partial_query.strip() == '' or partial_query.strip().upper().startswith('SEL'):
        suggestions.extend([
            {'type': 'keyword', 'value': 'SELECT', 'description': 'Start a query'},
            {'type': 'keyword', 'value': 'SELECT *', 'description': 'Select all columns'}
        ])
    
    return suggestions[:10]  # Limit to 10 suggestions