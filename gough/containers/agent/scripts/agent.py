#!/usr/bin/env python3
"""
MaaS Infrastructure Agent

This agent runs on provisioned servers and provides:
- System monitoring and reporting
- Remote command execution
- Docker and LXD management
- FleetDM/OSQuery integration
- Health checks and maintenance
"""

import os
import sys
import json
import time
import logging
import threading
import signal
import psutil
import requests
import schedule
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify
import yaml
from dotenv import load_dotenv

# Load environment variables
load_dotenv('/etc/maas-agent/.env')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/opt/maas-agent/logs/agent.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class MaaSAgent:
    """Main MaaS infrastructure agent"""
    
    def __init__(self):
        self.config = self.load_config()
        self.server_id = self.get_server_id()
        self.management_url = os.getenv('MANAGEMENT_SERVER_URL', 'http://management-server:8000')
        self.agent_id = os.getenv('AGENT_ID', self.server_id)
        self.api_key = os.getenv('AGENT_API_KEY', '')
        
        # Flask app for API endpoints
        self.app = Flask(__name__)
        self.setup_api_routes()
        
        # Control flags
        self.running = True
        self.last_heartbeat = None
        
        logger.info(f"MaaS Agent initialized - ID: {self.agent_id}")
    
    def load_config(self):
        """Load agent configuration"""
        config_file = Path('/etc/maas-agent/config.yaml')
        
        default_config = {
            'agent': {
                'name': os.getenv('HOSTNAME', 'unknown'),
                'version': '1.0.0',
                'port': 9090,
                'heartbeat_interval': 60,
                'metrics_interval': 300,
                'log_level': 'INFO'
            },
            'services': {
                'docker': True,
                'lxd': True,
                'osquery': True,
                'monitoring': True
            },
            'management': {
                'url': os.getenv('MANAGEMENT_SERVER_URL', 'http://management-server:8000'),
                'api_key': os.getenv('AGENT_API_KEY', ''),
                'ssl_verify': False
            }
        }
        
        if config_file.exists():
            try:
                with open(config_file, 'r') as f:
                    user_config = yaml.safe_load(f)
                    default_config.update(user_config)
            except Exception as e:
                logger.warning(f"Error loading config file: {e}")
        
        return default_config
    
    def get_server_id(self):
        """Get server identifier"""
        # Try to get from environment first
        server_id = os.getenv('SERVER_ID')
        if server_id:
            return server_id
        
        # Generate from hostname and MAC address
        hostname = os.getenv('HOSTNAME', 'unknown')
        try:
            # Get primary network interface MAC
            import uuid
            mac = ':'.join(['{:02x}'.format((uuid.getnode() >> i) & 0xff) for i in range(0,8*6,8)][::-1])
            return f"{hostname}-{mac.replace(':', '')[:12]}"
        except:
            return hostname
    
    def setup_api_routes(self):
        """Setup Flask API routes"""
        
        @self.app.route('/health', methods=['GET'])
        def health_check():
            """Health check endpoint"""
            try:
                system_info = self.get_system_info()
                return jsonify({
                    'status': 'healthy',
                    'agent_id': self.agent_id,
                    'timestamp': datetime.utcnow().isoformat(),
                    'uptime': system_info['uptime'],
                    'services': self.check_services()
                })
            except Exception as e:
                logger.error(f"Health check error: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500
        
        @self.app.route('/info', methods=['GET'])
        def get_info():
            """Get detailed system information"""
            try:
                return jsonify(self.get_system_info())
            except Exception as e:
                logger.error(f"Info request error: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/execute', methods=['POST'])
        def execute_command():
            """Execute remote command"""
            try:
                data = request.get_json()
                command = data.get('command')
                if not command:
                    return jsonify({'error': 'No command provided'}), 400
                
                result = self.execute_system_command(command)
                return jsonify(result)
            except Exception as e:
                logger.error(f"Command execution error: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/docker/containers', methods=['GET'])
        def list_docker_containers():
            """List Docker containers"""
            try:
                containers = self.get_docker_containers()
                return jsonify(containers)
            except Exception as e:
                logger.error(f"Docker list error: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/lxd/containers', methods=['GET'])
        def list_lxd_containers():
            """List LXD containers"""
            try:
                containers = self.get_lxd_containers()
                return jsonify(containers)
            except Exception as e:
                logger.error(f"LXD list error: {e}")
                return jsonify({'error': str(e)}), 500
    
    def get_system_info(self):
        """Collect comprehensive system information"""
        try:
            # CPU information
            cpu_percent = psutil.cpu_percent(interval=1, percpu=True)
            cpu_count = psutil.cpu_count()
            cpu_freq = psutil.cpu_freq()
            
            # Memory information
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            
            # Disk information
            disk_usage = []
            for partition in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    disk_usage.append({
                        'device': partition.device,
                        'mountpoint': partition.mountpoint,
                        'fstype': partition.fstype,
                        'total': usage.total,
                        'used': usage.used,
                        'free': usage.free,
                        'percent': round((usage.used / usage.total) * 100, 2)
                    })
                except:
                    continue
            
            # Network information
            network_stats = psutil.net_io_counters()
            network_interfaces = []
            for interface, addrs in psutil.net_if_addrs().items():
                interface_info = {'name': interface, 'addresses': []}
                for addr in addrs:
                    if addr.family == 2:  # IPv4
                        interface_info['addresses'].append({
                            'type': 'IPv4',
                            'address': addr.address,
                            'netmask': addr.netmask
                        })
                    elif addr.family == 17:  # MAC
                        interface_info['mac'] = addr.address
                network_interfaces.append(interface_info)
            
            # Boot time and uptime
            boot_time = datetime.fromtimestamp(psutil.boot_time())
            uptime = datetime.now() - boot_time
            
            return {
                'agent_id': self.agent_id,
                'hostname': os.getenv('HOSTNAME', 'unknown'),
                'timestamp': datetime.utcnow().isoformat(),
                'uptime_seconds': int(uptime.total_seconds()),
                'uptime': str(uptime),
                'boot_time': boot_time.isoformat(),
                'cpu': {
                    'count': cpu_count,
                    'usage_percent': cpu_percent,
                    'average_usage': round(sum(cpu_percent) / len(cpu_percent), 2),
                    'frequency': cpu_freq._asdict() if cpu_freq else None
                },
                'memory': {
                    'total': memory.total,
                    'available': memory.available,
                    'used': memory.used,
                    'percent': memory.percent,
                    'swap_total': swap.total,
                    'swap_used': swap.used,
                    'swap_percent': swap.percent
                },
                'disk': disk_usage,
                'network': {
                    'interfaces': network_interfaces,
                    'stats': {
                        'bytes_sent': network_stats.bytes_sent,
                        'bytes_recv': network_stats.bytes_recv,
                        'packets_sent': network_stats.packets_sent,
                        'packets_recv': network_stats.packets_recv
                    }
                },
                'services': self.check_services()
            }
            
        except Exception as e:
            logger.error(f"Error collecting system info: {e}")
            raise
    
    def check_services(self):
        """Check status of monitored services"""
        services = {}
        
        # Check Docker
        try:
            import docker
            client = docker.from_env()
            containers = client.containers.list()
            services['docker'] = {
                'status': 'running',
                'containers': len(containers)
            }
        except Exception as e:
            services['docker'] = {
                'status': 'error',
                'message': str(e)
            }
        
        # Check LXD
        try:
            import subprocess
            result = subprocess.run(['lxc', 'list', '--format', 'json'], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                containers = json.loads(result.stdout)
                services['lxd'] = {
                    'status': 'running',
                    'containers': len(containers)
                }
            else:
                services['lxd'] = {
                    'status': 'error',
                    'message': result.stderr
                }
        except Exception as e:
            services['lxd'] = {
                'status': 'error',
                'message': str(e)
            }
        
        # Check OSQuery
        try:
            result = subprocess.run(['systemctl', 'is-active', 'osqueryd'], 
                                  capture_output=True, text=True, timeout=5)
            services['osquery'] = {
                'status': 'running' if result.returncode == 0 else 'stopped'
            }
        except Exception as e:
            services['osquery'] = {
                'status': 'error',
                'message': str(e)
            }
        
        return services
    
    def execute_system_command(self, command):
        """Execute system command safely"""
        import subprocess
        
        try:
            logger.info(f"Executing command: {command}")
            
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            return {
                'command': command,
                'returncode': result.returncode,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'timestamp': datetime.utcnow().isoformat()
            }
            
        except subprocess.TimeoutExpired:
            return {
                'command': command,
                'returncode': -1,
                'stdout': '',
                'stderr': 'Command timed out after 5 minutes',
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            return {
                'command': command,
                'returncode': -1,
                'stdout': '',
                'stderr': str(e),
                'timestamp': datetime.utcnow().isoformat()
            }
    
    def get_docker_containers(self):
        """Get Docker container information"""
        try:
            import docker
            client = docker.from_env()
            containers = []
            
            for container in client.containers.list(all=True):
                containers.append({
                    'id': container.id[:12],
                    'name': container.name,
                    'image': container.image.tags[0] if container.image.tags else container.image.id[:12],
                    'status': container.status,
                    'created': container.attrs['Created'],
                    'ports': container.ports
                })
            
            return containers
            
        except Exception as e:
            logger.error(f"Error getting Docker containers: {e}")
            return []
    
    def get_lxd_containers(self):
        """Get LXD container information"""
        try:
            import subprocess
            result = subprocess.run(['lxc', 'list', '--format', 'json'], 
                                  capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                containers = json.loads(result.stdout)
                return [{
                    'name': c['name'],
                    'status': c['status'],
                    'type': c['type'],
                    'architecture': c['architecture'],
                    'created_at': c['created_at'],
                    'profiles': c['profiles']
                } for c in containers]
            else:
                logger.error(f"LXC command failed: {result.stderr}")
                return []
                
        except Exception as e:
            logger.error(f"Error getting LXD containers: {e}")
            return []
    
    def send_heartbeat(self):
        """Send heartbeat to management server"""
        try:
            data = {
                'agent_id': self.agent_id,
                'timestamp': datetime.utcnow().isoformat(),
                'status': 'online',
                'quick_stats': {
                    'cpu_percent': psutil.cpu_percent(),
                    'memory_percent': psutil.virtual_memory().percent,
                    'disk_percent': psutil.disk_usage('/').percent
                }
            }
            
            headers = {}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'
            
            response = requests.post(
                f"{self.management_url}/api/agents/heartbeat",
                json=data,
                headers=headers,
                timeout=30,
                verify=self.config['management']['ssl_verify']
            )
            
            if response.status_code == 200:
                self.last_heartbeat = datetime.utcnow()
                logger.debug(f"Heartbeat sent successfully")
            else:
                logger.warning(f"Heartbeat failed: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Error sending heartbeat: {e}")
    
    def send_metrics(self):
        """Send detailed metrics to management server"""
        try:
            system_info = self.get_system_info()
            
            headers = {}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'
            
            response = requests.post(
                f"{self.management_url}/api/agents/metrics",
                json=system_info,
                headers=headers,
                timeout=60,
                verify=self.config['management']['ssl_verify']
            )
            
            if response.status_code == 200:
                logger.debug("Metrics sent successfully")
            else:
                logger.warning(f"Metrics send failed: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Error sending metrics: {e}")
    
    def setup_schedules(self):
        """Setup scheduled tasks"""
        heartbeat_interval = self.config['agent']['heartbeat_interval']
        metrics_interval = self.config['agent']['metrics_interval']
        
        schedule.every(heartbeat_interval).seconds.do(self.send_heartbeat)
        schedule.every(metrics_interval).seconds.do(self.send_metrics)
        
        logger.info(f"Scheduled tasks: heartbeat every {heartbeat_interval}s, metrics every {metrics_interval}s")
    
    def run_scheduler(self):
        """Run scheduled tasks in background thread"""
        while self.running:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(5)
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
    
    def start(self):
        """Start the agent"""
        logger.info("Starting MaaS Agent...")
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        # Setup scheduled tasks
        self.setup_schedules()
        
        # Start scheduler thread
        scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        scheduler_thread.start()
        
        # Send initial heartbeat
        self.send_heartbeat()
        
        # Start Flask API server
        try:
            port = self.config['agent']['port']
            logger.info(f"Starting API server on port {port}")
            self.app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"Error starting API server: {e}")
            sys.exit(1)

def main():
    """Main entry point"""
    agent = MaaSAgent()
    agent.start()

if __name__ == '__main__':
    main()