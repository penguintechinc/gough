#!/usr/bin/env python3
"""
MaaS Dynamic Inventory Script for Ansible
Generates Ansible inventory from MaaS server data with host grouping and variable management

Usage:
    ./maas-inventory.py [--list] [--host <hostname>] [--config <config_file>]

Configuration:
    Set environment variables or create config file:
    - MAAS_URL: MaaS server URL
    - MAAS_API_KEY: MaaS API key/OAuth token
    - MAAS_API_VERSION: API version (default: 2.0)
"""

import argparse
import json
import os
import sys
import yaml
from urllib.request import Request, urlopen
from urllib.parse import urljoin
from urllib.error import HTTPError, URLError
import base64
import hmac
import hashlib
import time
import uuid
from collections import defaultdict


class MaaSInventory:
    """MaaS Dynamic Inventory for Ansible"""
    
    def __init__(self, config_file=None):
        """Initialize inventory with configuration"""
        self.config_file = config_file
        self.config = self._load_config()
        self.inventory = {
            '_meta': {
                'hostvars': {}
            }
        }
        
        # Validate required configuration
        required_config = ['maas_url', 'maas_api_key']
        for key in required_config:
            if not self.config.get(key):
                raise ValueError(f"Missing required configuration: {key}")
    
    def _load_config(self):
        """Load configuration from file or environment variables"""
        config = {}
        
        # Load from config file if provided
        if self.config_file and os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                if self.config_file.endswith('.yaml') or self.config_file.endswith('.yml'):
                    config.update(yaml.safe_load(f))
                else:
                    config.update(json.load(f))
        
        # Override with environment variables
        config.update({
            'maas_url': os.environ.get('MAAS_URL', config.get('maas_url', '')),
            'maas_api_key': os.environ.get('MAAS_API_KEY', config.get('maas_api_key', '')),
            'maas_api_version': os.environ.get('MAAS_API_VERSION', config.get('maas_api_version', '2.0')),
            'ssh_user': os.environ.get('MAAS_SSH_USER', config.get('ssh_user', 'ubuntu')),
            'ssh_key_file': os.environ.get('MAAS_SSH_KEY', config.get('ssh_key_file', '~/.ssh/id_rsa')),
            'group_by_tags': config.get('group_by_tags', True),
            'group_by_status': config.get('group_by_status', True),
            'group_by_zone': config.get('group_by_zone', True),
            'group_by_architecture': config.get('group_by_architecture', True),
            'include_all_machines': config.get('include_all_machines', False),
            'cache_ttl': config.get('cache_ttl', 300),  # 5 minutes
        })
        
        return config
    
    def _make_request(self, endpoint, method='GET', data=None):
        """Make authenticated request to MaaS API"""
        url = urljoin(self.config['maas_url'], f"/api/{self.config['maas_api_version']}/{endpoint}")
        
        # Create OAuth 1.0 signature
        oauth_token = self.config['maas_api_key'].split(':')[1] if ':' in self.config['maas_api_key'] else ''
        oauth_consumer_key = self.config['maas_api_key'].split(':')[0] if ':' in self.config['maas_api_key'] else self.config['maas_api_key']
        oauth_consumer_secret = self.config['maas_api_key'].split(':')[2] if ':' in self.config['maas_api_key'] and len(self.config['maas_api_key'].split(':')) > 2 else ''
        
        # Simple OAuth header (MaaS specific)
        auth_header = f"OAuth oauth_version=1.0, oauth_signature_method=PLAINTEXT, oauth_consumer_key={oauth_consumer_key}, oauth_token={oauth_token}, oauth_signature={oauth_consumer_secret}&"
        
        headers = {
            'Authorization': auth_header,
            'Accept': 'application/json',
            'Content-Type': 'application/json' if data else 'application/x-www-form-urlencoded'
        }
        
        try:
            request = Request(url, data=data, headers=headers, method=method)
            response = urlopen(request, timeout=30)
            return json.loads(response.read().decode('utf-8'))
        except HTTPError as e:
            error_msg = f"HTTP Error {e.code}: {e.reason} when accessing {url}"
            if e.code == 401:
                error_msg += "\nCheck your MaaS API key and URL"
            elif e.code == 404:
                error_msg += f"\nEndpoint not found. Check API version ({self.config['maas_api_version']})"
            raise Exception(error_msg)
        except URLError as e:
            raise Exception(f"Connection error: {e.reason}. Check MaaS URL: {self.config['maas_url']}")
    
    def _get_machines(self):
        """Get all machines from MaaS"""
        try:
            machines = self._make_request('machines/')
            return machines
        except Exception as e:
            print(f"Error fetching machines: {e}", file=sys.stderr)
            return []
    
    def _get_zones(self):
        """Get all zones from MaaS"""
        try:
            zones = self._make_request('zones/')
            return {zone['id']: zone for zone in zones}
        except Exception as e:
            print(f"Error fetching zones: {e}", file=sys.stderr)
            return {}
    
    def _get_subnets(self):
        """Get all subnets from MaaS"""
        try:
            subnets = self._make_request('subnets/')
            return {subnet['id']: subnet for subnet in subnets}
        except Exception as e:
            print(f"Error fetching subnets: {e}", file=sys.stderr)
            return {}
    
    def _should_include_machine(self, machine):
        """Determine if machine should be included in inventory"""
        if self.config['include_all_machines']:
            return True
        
        # Only include deployed machines by default
        return machine.get('status_name', '').lower() == 'deployed'
    
    def _get_machine_ip(self, machine):
        """Get the primary IP address for a machine"""
        # Try to get IP from ip_addresses field first
        if machine.get('ip_addresses'):
            return machine['ip_addresses'][0]
        
        # Fallback to interface details
        for interface in machine.get('interface_set', []):
            for link in interface.get('links', []):
                if link.get('ip_address'):
                    return link['ip_address']
        
        # If no IP found, use hostname
        return machine.get('hostname', machine.get('system_id', 'unknown'))
    
    def _get_host_vars(self, machine, zones, subnets):
        """Generate host variables for a machine"""
        zone_info = zones.get(machine.get('zone', {}).get('id', 0), {})
        
        # Basic host variables
        host_vars = {
            'ansible_host': self._get_machine_ip(machine),
            'ansible_user': self.config['ssh_user'],
            'ansible_ssh_private_key_file': os.path.expanduser(self.config['ssh_key_file']),
            'ansible_ssh_common_args': '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
            
            # MaaS specific variables
            'maas_system_id': machine.get('system_id'),
            'maas_hostname': machine.get('hostname'),
            'maas_fqdn': machine.get('fqdn'),
            'maas_status': machine.get('status_name'),
            'maas_status_code': machine.get('status'),
            'maas_power_state': machine.get('power_state'),
            'maas_power_type': machine.get('power_type'),
            'maas_architecture': machine.get('architecture'),
            'maas_cpu_count': machine.get('cpu_count', 0),
            'maas_memory': machine.get('memory', 0),
            'maas_storage': machine.get('storage', 0),
            'maas_zone': zone_info.get('name', 'default'),
            'maas_zone_description': zone_info.get('description', ''),
            'maas_pool': machine.get('pool', {}).get('name', 'default'),
            'maas_tags': machine.get('tag_names', []),
            'maas_owner': machine.get('owner'),
            'maas_created': machine.get('created'),
            'maas_updated': machine.get('updated'),
        }
        
        # Network information
        if machine.get('ip_addresses'):
            host_vars['maas_ip_addresses'] = machine['ip_addresses']
            host_vars['maas_primary_ip'] = machine['ip_addresses'][0]
        
        # Interface information
        interfaces = []
        for interface in machine.get('interface_set', []):
            interface_info = {
                'name': interface.get('name'),
                'mac_address': interface.get('mac_address'),
                'type': interface.get('type'),
                'enabled': interface.get('enabled', False),
                'links': []
            }
            
            for link in interface.get('links', []):
                link_info = {
                    'mode': link.get('mode'),
                    'ip_address': link.get('ip_address'),
                    'subnet': subnets.get(link.get('subnet', {}).get('id', 0), {}).get('cidr')
                }
                interface_info['links'].append(link_info)
            
            interfaces.append(interface_info)
        
        host_vars['maas_interfaces'] = interfaces
        
        # Storage information
        storage_devices = []
        for device in machine.get('physicalblockdevice_set', []):
            device_info = {
                'name': device.get('name'),
                'model': device.get('model'),
                'serial': device.get('serial'),
                'size': device.get('size', 0),
                'type': device.get('type'),
                'path': device.get('path')
            }
            storage_devices.append(device_info)
        
        host_vars['maas_storage_devices'] = storage_devices
        
        # Tag-based variables (for role identification)
        for tag in machine.get('tag_names', []):
            if tag.startswith('role-'):
                host_vars['server_role'] = tag[5:]  # Remove 'role-' prefix
            elif tag.startswith('env-'):
                host_vars['server_environment'] = tag[4:]  # Remove 'env-' prefix
            elif tag.startswith('cluster-'):
                host_vars['cluster_name'] = tag[8:]  # Remove 'cluster-' prefix
        
        return host_vars
    
    def _create_groups(self, machines, zones):
        """Create inventory groups based on machine attributes"""
        groups = defaultdict(list)
        
        for machine in machines:
            hostname = machine.get('hostname', machine.get('system_id', 'unknown'))
            
            # Add to main groups
            groups['all'].append(hostname)
            
            if machine.get('status_name', '').lower() == 'deployed':
                groups['deployed'].append(hostname)
            
            # Group by status
            if self.config['group_by_status']:
                status_group = f"status_{machine.get('status_name', 'unknown').lower()}"
                groups[status_group].append(hostname)
            
            # Group by zone
            if self.config['group_by_zone']:
                zone_name = zones.get(machine.get('zone', {}).get('id', 0), {}).get('name', 'default')
                zone_group = f"zone_{zone_name}"
                groups[zone_group].append(hostname)
            
            # Group by architecture
            if self.config['group_by_architecture']:
                arch = machine.get('architecture', 'unknown').split('/')[0]  # Remove sub-architecture
                arch_group = f"arch_{arch}"
                groups[arch_group].append(hostname)
            
            # Group by tags
            if self.config['group_by_tags']:
                for tag in machine.get('tag_names', []):
                    # Create clean group names
                    if tag.startswith('role-'):
                        groups[tag[5:] + '_servers'].append(hostname)  # role-docker -> docker_servers
                    elif tag.startswith('env-'):
                        groups[tag].append(hostname)  # Keep env- tags as is
                    else:
                        # Clean tag name for group
                        clean_tag = tag.replace('-', '_').replace(' ', '_').lower()
                        groups[f"tag_{clean_tag}"].append(hostname)
            
            # Group by pool
            pool_name = machine.get('pool', {}).get('name', 'default')
            groups[f"pool_{pool_name}"].append(hostname)
            
            # Group by power type
            power_type = machine.get('power_type', 'unknown')
            if power_type != 'manual':  # Skip manual power type
                groups[f"power_{power_type}"].append(hostname)
        
        return dict(groups)
    
    def get_inventory(self):
        """Generate complete inventory"""
        # Fetch data from MaaS
        machines = self._get_machines()
        zones = self._get_zones()
        subnets = self._get_subnets()
        
        if not machines:
            print("Warning: No machines found or error accessing MaaS API", file=sys.stderr)
            return self.inventory
        
        # Filter machines
        filtered_machines = [m for m in machines if self._should_include_machine(m)]
        
        # Create groups
        groups = self._create_groups(filtered_machines, zones)
        
        # Add groups to inventory
        for group_name, hosts in groups.items():
            if hosts:  # Only add non-empty groups
                self.inventory[group_name] = {
                    'hosts': hosts,
                    'vars': {}
                }
        
        # Add group variables
        self._add_group_vars()
        
        # Add host variables
        for machine in filtered_machines:
            hostname = machine.get('hostname', machine.get('system_id', 'unknown'))
            host_vars = self._get_host_vars(machine, zones, subnets)
            self.inventory['_meta']['hostvars'][hostname] = host_vars
        
        return self.inventory
    
    def _add_group_vars(self):
        """Add variables to groups"""
        # Global variables for all hosts
        if 'all' in self.inventory:
            self.inventory['all']['vars'] = {
                'maas_url': self.config['maas_url'],
                'maas_api_version': self.config['maas_api_version'],
                'ansible_ssh_common_args': '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
                'ansible_python_interpreter': '/usr/bin/python3'
            }
        
        # Environment-specific variables
        for group_name in self.inventory.keys():
            if group_name.startswith('env-'):
                env_name = group_name[4:]  # Remove 'env-' prefix
                self.inventory[group_name]['vars'] = {
                    'environment': env_name,
                    'env_name': env_name
                }
        
        # Role-specific variables
        role_configs = {
            'docker_servers': {
                'server_role': 'docker_host',
                'docker_enabled': True,
                'container_runtime': 'docker'
            },
            'kubernetes_servers': {
                'server_role': 'kubernetes_node',
                'k8s_enabled': True,
                'container_runtime': 'containerd'
            },
            'lxd_servers': {
                'server_role': 'lxd_host',
                'lxd_enabled': True,
                'container_runtime': 'lxd'
            }
        }
        
        for group_name, vars_dict in role_configs.items():
            if group_name in self.inventory:
                self.inventory[group_name]['vars'].update(vars_dict)
    
    def get_host(self, hostname):
        """Get variables for a specific host"""
        inventory = self.get_inventory()
        return inventory['_meta']['hostvars'].get(hostname, {})
    
    def list_inventory(self):
        """Return inventory in JSON format"""
        return json.dumps(self.get_inventory(), indent=2, sort_keys=True)
    
    def get_host_info(self, hostname):
        """Return host info in JSON format"""
        return json.dumps(self.get_host(hostname), indent=2, sort_keys=True)


def main():
    """Main function"""
    parser = argparse.ArgumentParser(description='MaaS Dynamic Inventory for Ansible')
    parser.add_argument('--list', action='store_true', help='List all hosts')
    parser.add_argument('--host', help='Get variables for specific host')
    parser.add_argument('--config', help='Configuration file path')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    
    args = parser.parse_args()
    
    try:
        inventory = MaaSInventory(config_file=args.config)
        
        if args.list:
            print(inventory.list_inventory())
        elif args.host:
            print(inventory.get_host_info(args.host))
        else:
            parser.print_help()
            sys.exit(1)
            
    except Exception as e:
        if args.debug:
            import traceback
            traceback.print_exc()
        else:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()