"""
MaaS API Client for interfacing with MaaS REST API
"""

import requests
import json
import logging
from urllib.parse import urljoin, urlparse
import base64

logger = logging.getLogger(__name__)

class MaaSClient:
    """Client for MaaS REST API"""
    
    def __init__(self, maas_url, api_key):
        """Initialize MaaS client
        
        Args:
            maas_url: Base URL of MaaS server (e.g., http://maas.local:5240/MAAS/)
            api_key: API key for authentication
        """
        self.maas_url = maas_url.rstrip('/')
        self.api_key = api_key
        self.api_base = urljoin(self.maas_url, '/api/2.0/')
        self.session = requests.Session()
        
        # Set up authentication
        self._setup_auth()
    
    def _setup_auth(self):
        """Setup OAuth authentication for MaaS API"""
        try:
            # MaaS uses OAuth 1.0 with the API key as credentials
            # API key format: consumer_key:token_key:token_secret
            parts = self.api_key.split(':')
            if len(parts) != 3:
                raise ValueError("Invalid API key format. Expected: consumer_key:token_key:token_secret")
            
            consumer_key, token_key, token_secret = parts
            
            # For simplicity, we'll use basic auth with the token
            # In production, implement full OAuth 1.0 signing
            auth_string = f"{consumer_key}:{token_key}:{token_secret}"
            encoded_auth = base64.b64encode(auth_string.encode()).decode()
            
            self.session.headers.update({
                'Authorization': f'OAuth oauth_version="1.0", oauth_signature_method="PLAINTEXT", oauth_consumer_key="{consumer_key}", oauth_token="{token_key}", oauth_signature="{consumer_key}&{token_secret}"',
                'Content-Type': 'application/json'
            })
            
        except Exception as e:
            logger.error(f"Error setting up MaaS authentication: {str(e)}")
            raise
    
    def test_connection(self):
        """Test connection to MaaS API
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            response = self.session.get(urljoin(self.api_base, 'version/'))
            return response.status_code == 200
        except Exception as e:
            logger.error(f"MaaS connection test failed: {str(e)}")
            return False
    
    def get_machines(self):
        """Get all machines from MaaS
        
        Returns:
            list: List of machine dictionaries
        """
        try:
            response = self.session.get(urljoin(self.api_base, 'machines/'))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting machines: {str(e)}")
            raise
    
    def get_machine(self, system_id):
        """Get details for a specific machine
        
        Args:
            system_id: MaaS system ID of the machine
            
        Returns:
            dict: Machine details
        """
        try:
            response = self.session.get(urljoin(self.api_base, f'machines/{system_id}/'))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting machine {system_id}: {str(e)}")
            raise
    
    def deploy_machine(self, system_id, distro_series='jammy', user_data='', kernel='', enable_ssh=True):
        """Deploy a machine
        
        Args:
            system_id: MaaS system ID of the machine
            distro_series: OS series to deploy (default: jammy for Ubuntu 22.04)
            user_data: Cloud-init user data
            kernel: Specific kernel to use
            enable_ssh: Whether to enable SSH access
            
        Returns:
            dict: Deployment result
        """
        try:
            data = {
                'distro_series': distro_series,
                'user_data': base64.b64encode(user_data.encode()).decode() if user_data else '',
                'install_kvm': False
            }
            
            if kernel:
                data['kernel'] = kernel
            
            response = self.session.post(
                urljoin(self.api_base, f'machines/{system_id}/'),
                params={'op': 'deploy'},
                data=data
            )
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error deploying machine {system_id}: {str(e)}")
            raise
    
    def commission_machine(self, system_id, enable_ssh=True, skip_networking=False):
        """Commission a machine
        
        Args:
            system_id: MaaS system ID of the machine
            enable_ssh: Whether to enable SSH during commissioning
            skip_networking: Whether to skip network configuration
            
        Returns:
            dict: Commission result
        """
        try:
            data = {
                'enable_ssh': enable_ssh,
                'skip_networking': skip_networking
            }
            
            response = self.session.post(
                urljoin(self.api_base, f'machines/{system_id}/'),
                params={'op': 'commission'},
                data=data
            )
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error commissioning machine {system_id}: {str(e)}")
            raise
    
    def release_machine(self, system_id, secure_erase=False):
        """Release a machine
        
        Args:
            system_id: MaaS system ID of the machine
            secure_erase: Whether to securely erase disks
            
        Returns:
            dict: Release result
        """
        try:
            data = {
                'secure_erase': secure_erase
            }
            
            response = self.session.post(
                urljoin(self.api_base, f'machines/{system_id}/'),
                params={'op': 'release'},
                data=data
            )
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error releasing machine {system_id}: {str(e)}")
            raise
    
    def power_on_machine(self, system_id):
        """Power on a machine
        
        Args:
            system_id: MaaS system ID of the machine
            
        Returns:
            dict: Power operation result
        """
        try:
            response = self.session.post(
                urljoin(self.api_base, f'machines/{system_id}/'),
                params={'op': 'power_on'}
            )
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error powering on machine {system_id}: {str(e)}")
            raise
    
    def power_off_machine(self, system_id):
        """Power off a machine
        
        Args:
            system_id: MaaS system ID of the machine
            
        Returns:
            dict: Power operation result
        """
        try:
            response = self.session.post(
                urljoin(self.api_base, f'machines/{system_id}/'),
                params={'op': 'power_off'}
            )
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error powering off machine {system_id}: {str(e)}")
            raise
    
    def get_subnets(self):
        """Get all subnets
        
        Returns:
            list: List of subnet dictionaries
        """
        try:
            response = self.session.get(urljoin(self.api_base, 'subnets/'))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting subnets: {str(e)}")
            raise
    
    def get_vlans(self):
        """Get all VLANs
        
        Returns:
            list: List of VLAN dictionaries
        """
        try:
            response = self.session.get(urljoin(self.api_base, 'vlans/'))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting VLANs: {str(e)}")
            raise
    
    def get_zones(self):
        """Get all zones
        
        Returns:
            list: List of zone dictionaries
        """
        try:
            response = self.session.get(urljoin(self.api_base, 'zones/'))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting zones: {str(e)}")
            raise
    
    def create_machine(self, mac_address, architecture='amd64/generic', power_type='manual'):
        """Create/enlist a new machine
        
        Args:
            mac_address: MAC address of the machine's boot interface
            architecture: Machine architecture
            power_type: Power management type
            
        Returns:
            dict: Created machine details
        """
        try:
            data = {
                'mac_addresses': mac_address,
                'architecture': architecture,
                'power_type': power_type
            }
            
            response = self.session.post(
                urljoin(self.api_base, 'machines/'),
                params={'op': 'new'},
                data=data
            )
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error creating machine: {str(e)}")
            raise