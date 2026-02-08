#!/usr/bin/env python3
"""
Ansible Callback Plugin for Status Updates
Sends deployment status updates to the management server
"""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = '''
    name: status_update
    type: notification
    short_description: Send deployment status updates to management server
    description:
        - This callback plugin sends real-time status updates during Ansible playbook execution
        - Updates are sent to the management server via HTTP API
        - Provides progress tracking and detailed logging for deployments
    requirements:
        - requests library
        - Access to management server API
    options:
        management_server_url:
            description: URL of the management server
            env:
                - name: MANAGEMENT_SERVER_URL
            default: 'http://localhost:8000'
        api_token:
            description: API token for authentication
            env:
                - name: MANAGEMENT_API_TOKEN
        job_id:
            description: Deployment job ID
            env:
                - name: DEPLOYMENT_JOB_ID
        update_interval:
            description: Minimum seconds between updates
            default: 5
            type: int
'''

import os
import sys
import json
import time
import traceback
from datetime import datetime
from collections import defaultdict

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from ansible.plugins.callback import CallbackBase
from ansible.parsing.ajson import AnsibleJSONEncoder


class CallbackModule(CallbackBase):
    """
    Ansible callback plugin for deployment status updates
    """
    
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'status_update'
    CALLBACK_NEEDS_WHITELIST = False
    
    def __init__(self):
        super(CallbackModule, self).__init__()
        
        # Configuration
        self.management_server_url = os.getenv('MANAGEMENT_SERVER_URL', 'http://localhost:8000')
        self.api_token = os.getenv('MANAGEMENT_API_TOKEN', '')
        self.job_id = os.getenv('DEPLOYMENT_JOB_ID', '')
        self.update_interval = int(os.getenv('UPDATE_INTERVAL', 5))
        
        # State tracking
        self.start_time = None
        self.last_update = 0
        self.task_count = 0
        self.completed_tasks = 0
        self.failed_tasks = 0
        self.skipped_tasks = 0
        self.host_stats = defaultdict(lambda: {'ok': 0, 'failed': 0, 'skipped': 0, 'unreachable': 0})
        self.current_play = None
        self.current_task = None
        
        # Validation
        if not HAS_REQUESTS:
            self._display.warning("requests library not available, status updates disabled")
            self.disabled = True
        elif not self.job_id:
            self._display.warning("DEPLOYMENT_JOB_ID not set, status updates disabled")
            self.disabled = True
        elif not self.api_token:
            self._display.warning("MANAGEMENT_API_TOKEN not set, status updates disabled")
            self.disabled = True
        else:
            self.disabled = False
            self._display.v(f"Status update callback initialized for job {self.job_id}")
    
    def _send_update(self, status, message, progress=None, force=False):
        """Send status update to management server"""
        
        if self.disabled:
            return
        
        current_time = time.time()
        
        # Rate limiting (unless forced)
        if not force and (current_time - self.last_update) < self.update_interval:
            return
        
        try:
            # Calculate progress if not provided
            if progress is None and self.task_count > 0:
                progress = int((self.completed_tasks / self.task_count) * 100)
            elif progress is None:
                progress = 0
            
            # Prepare update data
            update_data = {
                'job_id': self.job_id,
                'status': status,
                'message': message,
                'progress': min(100, max(0, progress)),
                'details': {
                    'current_play': self.current_play,
                    'current_task': self.current_task,
                    'task_stats': {
                        'total': self.task_count,
                        'completed': self.completed_tasks,
                        'failed': self.failed_tasks,
                        'skipped': self.skipped_tasks
                    },
                    'host_stats': dict(self.host_stats),
                    'timestamp': datetime.utcnow().isoformat()
                }
            }
            
            # Send HTTP request
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_token}'
            }
            
            response = requests.post(
                f'{self.management_server_url}/api/deployment/status',
                headers=headers,
                json=update_data,
                timeout=10
            )
            
            if response.status_code != 200:
                self._display.warning(f"Status update failed: HTTP {response.status_code}")
            else:
                self.last_update = current_time
                self._display.vv(f"Status update sent: {status} - {message}")
        
        except requests.exceptions.RequestException as e:
            self._display.warning(f"Status update request failed: {e}")
        except Exception as e:
            self._display.warning(f"Status update error: {e}")
    
    def v2_playbook_on_start(self, playbook):
        """Playbook started"""
        self.start_time = time.time()
        self._send_update('Running', 'Playbook execution started', 0, force=True)
    
    def v2_playbook_on_play_start(self, play):
        """Play started"""
        self.current_play = play.get_name()
        self._display.v(f"Starting play: {self.current_play}")
        self._send_update('Running', f'Starting play: {self.current_play}')
    
    def v2_playbook_on_task_start(self, task, is_conditional):
        """Task started"""
        self.current_task = task.get_name()
        self.task_count += 1
        self._display.vv(f"Starting task: {self.current_task}")
        
        # Send update every 10 tasks or for important tasks
        if self.task_count % 10 == 0 or self._is_important_task(task):
            self._send_update('Running', f'Executing: {self.current_task}')
    
    def v2_runner_on_ok(self, result):
        """Task completed successfully"""
        self.completed_tasks += 1
        self.host_stats[result._host.get_name()]['ok'] += 1
        
        # Send update for important task completions
        if self._is_important_task(result._task):
            self._send_update('Running', f'Completed: {result._task.get_name()}')
    
    def v2_runner_on_failed(self, result, ignore_errors=False):
        """Task failed"""
        self.failed_tasks += 1
        self.host_stats[result._host.get_name()]['failed'] += 1
        
        # Always send update for failures
        task_name = result._task.get_name()
        error_msg = self._extract_error_message(result)
        
        if ignore_errors:
            self._send_update('Running', f'Task failed (ignored): {task_name} - {error_msg}', force=True)
        else:
            self._send_update('Failed', f'Task failed: {task_name} - {error_msg}', force=True)
    
    def v2_runner_on_skipped(self, result):
        """Task skipped"""
        self.skipped_tasks += 1
        self.host_stats[result._host.get_name()]['skipped'] += 1
    
    def v2_runner_on_unreachable(self, result):
        """Host unreachable"""
        self.failed_tasks += 1
        self.host_stats[result._host.get_name()]['unreachable'] += 1
        
        hostname = result._host.get_name()
        error_msg = self._extract_error_message(result)
        self._send_update('Failed', f'Host unreachable: {hostname} - {error_msg}', force=True)
    
    def v2_playbook_on_stats(self, stats):
        """Playbook completed"""
        
        # Calculate final statistics
        total_hosts = len(stats.processed.keys())
        failed_hosts = len(stats.failures.keys()) + len(stats.dark.keys())
        
        # Determine final status
        if failed_hosts > 0:
            status = 'Failed'
            message = f'Playbook completed with failures: {failed_hosts}/{total_hosts} hosts failed'
        else:
            status = 'Completed'
            message = f'Playbook completed successfully: {total_hosts} hosts processed'
        
        # Calculate total duration
        duration = int(time.time() - self.start_time) if self.start_time else 0
        
        # Send final update with detailed statistics
        final_data = {
            'job_id': self.job_id,
            'status': status,
            'message': message,
            'progress': 100,
            'details': {
                'duration': duration,
                'task_stats': {
                    'total': self.task_count,
                    'completed': self.completed_tasks,
                    'failed': self.failed_tasks,
                    'skipped': self.skipped_tasks
                },
                'host_stats': {
                    'total': total_hosts,
                    'successful': total_hosts - failed_hosts,
                    'failed': failed_hosts,
                    'by_host': dict(self.host_stats)
                },
                'ansible_stats': self._format_ansible_stats(stats),
                'timestamp': datetime.utcnow().isoformat()
            }
        }
        
        try:
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_token}'
            }
            
            requests.post(
                f'{self.management_server_url}/api/deployment/complete',
                headers=headers,
                json=final_data,
                timeout=10
            )
            
            self._display.v(f"Final status sent: {status}")
            
        except Exception as e:
            self._display.warning(f"Failed to send final status: {e}")
    
    def v2_playbook_on_no_hosts_matched(self):
        """No hosts matched"""
        self._send_update('Failed', 'No hosts matched the pattern', force=True)
    
    def v2_playbook_on_no_hosts_remaining(self):
        """No hosts remaining"""
        self._send_update('Failed', 'No hosts remaining to execute tasks', force=True)
    
    def _is_important_task(self, task):
        """Determine if a task is important enough to send immediate updates"""
        task_name = task.get_name().lower()
        
        important_keywords = [
            'install', 'download', 'deploy', 'configure', 'setup', 'start', 'stop',
            'restart', 'reboot', 'update', 'upgrade', 'create', 'delete', 'copy'
        ]
        
        return any(keyword in task_name for keyword in important_keywords)
    
    def _extract_error_message(self, result):
        """Extract meaningful error message from result"""
        try:
            if hasattr(result, '_result') and result._result:
                # Try to get error message from various sources
                error_sources = ['msg', 'stderr', 'exception', 'module_stderr']
                
                for source in error_sources:
                    if source in result._result and result._result[source]:
                        error = result._result[source]
                        # Truncate long error messages
                        if len(error) > 200:
                            error = error[:200] + '...'
                        return error
                
                # If no specific error, return generic info
                return json.dumps(result._result, cls=AnsibleJSONEncoder)[:200]
            
            return "Unknown error"
            
        except Exception:
            return "Error parsing failure message"
    
    def _format_ansible_stats(self, stats):
        """Format Ansible statistics for API"""
        try:
            formatted_stats = {}
            
            for hostname in stats.processed.keys():
                host_summary = stats.summarize(hostname)
                formatted_stats[hostname] = {
                    'ok': host_summary.get('ok', 0),
                    'failures': host_summary.get('failures', 0),
                    'unreachable': host_summary.get('unreachable', 0),
                    'skipped': host_summary.get('skipped', 0),
                    'rescued': host_summary.get('rescued', 0),
                    'ignored': host_summary.get('ignored', 0)
                }
            
            return formatted_stats
            
        except Exception as e:
            self._display.warning(f"Failed to format Ansible stats: {e}")
            return {}


# Compatibility with older Ansible versions
class CallbackModule_v1(CallbackBase):
    """Fallback for Ansible < 2.0"""
    
    CALLBACK_VERSION = 1.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'status_update'
    
    def __init__(self):
        super(CallbackModule_v1, self).__init__()
        self._display.warning("Using legacy callback interface")
    
    def on_any(self, *args, **kwargs):
        pass


# Export the appropriate callback class based on Ansible version
try:
    from ansible import __version__ as ansible_version
    if ansible_version.startswith('1.'):
        CallbackModule = CallbackModule_v1
except ImportError:
    pass