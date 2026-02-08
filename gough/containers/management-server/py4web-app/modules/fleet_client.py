"""
FleetDM API Client for Gough Hypervisor Management Portal
Provides integration with FleetDM server for OSQuery management
"""

import requests
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import urllib3

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class FleetAPIException(Exception):
    """Custom exception for Fleet API errors"""
    pass


class FleetClient:
    """Client for FleetDM API interactions"""
    
    def __init__(self, fleet_url: str, api_token: str, verify_ssl: bool = False):
        """
        Initialize Fleet client
        
        Args:
            fleet_url: Base URL of FleetDM server
            api_token: API token for authentication
            verify_ssl: Whether to verify SSL certificates
        """
        self.fleet_url = fleet_url.rstrip('/')
        self.api_token = api_token
        self.verify_ssl = verify_ssl
        
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        })
        self.session.verify = verify_ssl
        
        # Set timeout for all requests
        self.session.timeout = 30
        
    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[Any, Any]:
        """
        Make HTTP request to FleetDM API
        
        Args:
            method: HTTP method
            endpoint: API endpoint
            **kwargs: Additional request parameters
            
        Returns:
            JSON response data
            
        Raises:
            FleetAPIException: If request fails
        """
        url = f"{self.fleet_url}/api/v1/fleet{endpoint}"
        
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            
            # Handle empty responses
            if response.content:
                return response.json()
            else:
                return {}
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Fleet API request failed: {e}")
            raise FleetAPIException(f"API request failed: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Fleet API: {e}")
            raise FleetAPIException(f"Invalid JSON response: {str(e)}")
    
    def test_connection(self) -> bool:
        """
        Test connection to FleetDM server
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            response = self._make_request('GET', '/version')
            return 'version' in response
        except Exception as e:
            logger.error(f"Fleet connection test failed: {e}")
            return False
    
    def get_hosts(self, query: Optional[str] = None, team_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get list of enrolled hosts
        
        Args:
            query: Search query to filter hosts
            team_id: Team ID to filter hosts
            
        Returns:
            List of host information
        """
        params = {}
        if query:
            params['query'] = query
        if team_id:
            params['team_id'] = team_id
            
        try:
            response = self._make_request('GET', '/hosts', params=params)
            return response.get('hosts', [])
        except FleetAPIException:
            logger.error("Failed to retrieve hosts from Fleet")
            return []
    
    def get_host_details(self, host_id: int) -> Optional[Dict[str, Any]]:
        """
        Get detailed information about a specific host
        
        Args:
            host_id: ID of the host
            
        Returns:
            Host details or None if not found
        """
        try:
            response = self._make_request('GET', f'/hosts/{host_id}')
            return response.get('host')
        except FleetAPIException:
            logger.error(f"Failed to retrieve host {host_id} details from Fleet")
            return None
    
    def get_host_by_uuid(self, uuid: str) -> Optional[Dict[str, Any]]:
        """
        Get host information by UUID
        
        Args:
            uuid: Host UUID
            
        Returns:
            Host information or None if not found
        """
        hosts = self.get_hosts(query=uuid)
        for host in hosts:
            if host.get('uuid') == uuid:
                return host
        return None
    
    def run_live_query(self, query: str, host_ids: List[int]) -> Dict[str, Any]:
        """
        Run a live query on specified hosts
        
        Args:
            query: SQL query to execute
            host_ids: List of host IDs to target
            
        Returns:
            Query campaign information
        """
        data = {
            'query': query,
            'selected': {
                'hosts': host_ids
            }
        }
        
        try:
            response = self._make_request('POST', '/queries/run', json=data)
            return response
        except FleetAPIException:
            logger.error("Failed to run live query")
            return {}
    
    def get_query_results(self, campaign_id: int) -> Dict[str, Any]:
        """
        Get results from a query campaign
        
        Args:
            campaign_id: ID of the query campaign
            
        Returns:
            Query results
        """
        try:
            response = self._make_request('GET', f'/queries/{campaign_id}/results')
            return response
        except FleetAPIException:
            logger.error(f"Failed to get query results for campaign {campaign_id}")
            return {}
    
    def get_saved_queries(self) -> List[Dict[str, Any]]:
        """
        Get list of saved queries
        
        Returns:
            List of saved queries
        """
        try:
            response = self._make_request('GET', '/queries')
            return response.get('queries', [])
        except FleetAPIException:
            logger.error("Failed to retrieve saved queries")
            return []
    
    def create_query(self, name: str, query: str, description: str = "") -> Optional[Dict[str, Any]]:
        """
        Create a new saved query
        
        Args:
            name: Query name
            query: SQL query
            description: Query description
            
        Returns:
            Created query information or None if failed
        """
        data = {
            'name': name,
            'query': query,
            'description': description
        }
        
        try:
            response = self._make_request('POST', '/queries', json=data)
            return response.get('query')
        except FleetAPIException:
            logger.error(f"Failed to create query: {name}")
            return None
    
    def get_query_packs(self) -> List[Dict[str, Any]]:
        """
        Get list of query packs
        
        Returns:
            List of query packs
        """
        try:
            response = self._make_request('GET', '/packs')
            return response.get('packs', [])
        except FleetAPIException:
            logger.error("Failed to retrieve query packs")
            return []
    
    def create_query_pack(self, name: str, description: str = "", queries: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """
        Create a new query pack
        
        Args:
            name: Pack name
            description: Pack description
            queries: List of queries to include in pack
            
        Returns:
            Created pack information or None if failed
        """
        data = {
            'name': name,
            'description': description
        }
        
        try:
            response = self._make_request('POST', '/packs', json=data)
            pack = response.get('pack')
            
            # Add queries to the pack if provided
            if pack and queries:
                pack_id = pack['id']
                for query_data in queries:
                    self.add_query_to_pack(pack_id, query_data)
                    
            return pack
        except FleetAPIException:
            logger.error(f"Failed to create query pack: {name}")
            return None
    
    def add_query_to_pack(self, pack_id: int, query_data: Dict[str, Any]) -> bool:
        """
        Add a query to a query pack
        
        Args:
            pack_id: ID of the pack
            query_data: Query configuration data
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self._make_request('POST', f'/packs/{pack_id}/queries', json=query_data)
            return True
        except FleetAPIException:
            logger.error(f"Failed to add query to pack {pack_id}")
            return False
    
    def get_activities(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get recent activities/events
        
        Args:
            limit: Maximum number of activities to return
            
        Returns:
            List of activities
        """
        params = {'per_page': limit}
        
        try:
            response = self._make_request('GET', '/activities', params=params)
            return response.get('activities', [])
        except FleetAPIException:
            logger.error("Failed to retrieve activities")
            return []
    
    def get_host_software(self, host_id: int) -> List[Dict[str, Any]]:
        """
        Get software inventory for a host
        
        Args:
            host_id: ID of the host
            
        Returns:
            List of installed software
        """
        try:
            response = self._make_request('GET', f'/hosts/{host_id}/software')
            return response.get('software', [])
        except FleetAPIException:
            logger.error(f"Failed to get software for host {host_id}")
            return []
    
    def get_vulnerabilities(self, host_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get vulnerability information
        
        Args:
            host_id: Optional host ID to filter vulnerabilities
            
        Returns:
            List of vulnerabilities
        """
        endpoint = '/vulnerabilities'
        params = {}
        
        if host_id:
            params['host_id'] = host_id
            
        try:
            response = self._make_request('GET', endpoint, params=params)
            return response.get('vulnerabilities', [])
        except FleetAPIException:
            logger.error("Failed to retrieve vulnerabilities")
            return []
    
    def get_enrollment_secrets(self) -> List[Dict[str, Any]]:
        """
        Get enrollment secrets
        
        Returns:
            List of enrollment secrets
        """
        try:
            response = self._make_request('GET', '/enroll_secrets')
            return response.get('secrets', [])
        except FleetAPIException:
            logger.error("Failed to retrieve enrollment secrets")
            return []
    
    def create_enrollment_secret(self, name: str = "", team_id: Optional[int] = None) -> Optional[str]:
        """
        Create a new enrollment secret
        
        Args:
            name: Name for the secret
            team_id: Optional team ID
            
        Returns:
            New enrollment secret or None if failed
        """
        data = {'name': name}
        if team_id:
            data['team_id'] = team_id
            
        try:
            response = self._make_request('POST', '/enroll_secrets', json=data)
            return response.get('secret', {}).get('secret')
        except FleetAPIException:
            logger.error("Failed to create enrollment secret")
            return None
    
    def get_system_stats(self) -> Dict[str, Any]:
        """
        Get system statistics and health information
        
        Returns:
            System statistics
        """
        stats = {
            'total_hosts': 0,
            'online_hosts': 0,
            'offline_hosts': 0,
            'new_hosts': 0,
            'total_queries': 0,
            'total_packs': 0,
            'recent_activities': 0
        }
        
        try:
            # Get host counts
            hosts = self.get_hosts()
            stats['total_hosts'] = len(hosts)
            
            # Calculate online/offline hosts (hosts seen in last 30 minutes are considered online)
            now = datetime.utcnow()
            online_threshold = now - timedelta(minutes=30)
            
            for host in hosts:
                last_seen = host.get('seen_time')
                if last_seen:
                    # Parse the timestamp (FleetDM uses RFC3339 format)
                    try:
                        last_seen_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                        if last_seen_dt > online_threshold:
                            stats['online_hosts'] += 1
                        else:
                            stats['offline_hosts'] += 1
                    except (ValueError, TypeError):
                        stats['offline_hosts'] += 1
                else:
                    stats['offline_hosts'] += 1
            
            # Count new hosts (enrolled in last 24 hours)
            new_threshold = now - timedelta(hours=24)
            for host in hosts:
                created_at = host.get('created_at')
                if created_at:
                    try:
                        created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        if created_dt > new_threshold:
                            stats['new_hosts'] += 1
                    except (ValueError, TypeError):
                        pass
            
            # Get query and pack counts
            queries = self.get_saved_queries()
            stats['total_queries'] = len(queries)
            
            packs = self.get_query_packs()
            stats['total_packs'] = len(packs)
            
            # Get recent activities count
            activities = self.get_activities(limit=10)
            stats['recent_activities'] = len(activities)
            
        except Exception as e:
            logger.error(f"Failed to get system stats: {e}")
        
        return stats
    
    def get_host_status_summary(self) -> Dict[str, int]:
        """
        Get summary of host statuses
        
        Returns:
            Dictionary with host status counts
        """
        summary = {
            'online': 0,
            'offline': 0,
            'new': 0,
            'missing_in_action': 0
        }
        
        try:
            hosts = self.get_hosts()
            now = datetime.utcnow()
            online_threshold = now - timedelta(minutes=30)
            new_threshold = now - timedelta(hours=24)
            mia_threshold = now - timedelta(days=7)
            
            for host in hosts:
                last_seen = host.get('seen_time')
                created_at = host.get('created_at')
                
                # Parse timestamps
                last_seen_dt = None
                created_dt = None
                
                if last_seen:
                    try:
                        last_seen_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        pass
                        
                if created_at:
                    try:
                        created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        pass
                
                # Categorize host status
                if created_dt and created_dt > new_threshold:
                    summary['new'] += 1
                elif last_seen_dt:
                    if last_seen_dt > online_threshold:
                        summary['online'] += 1
                    elif last_seen_dt < mia_threshold:
                        summary['missing_in_action'] += 1
                    else:
                        summary['offline'] += 1
                else:
                    summary['missing_in_action'] += 1
                    
        except Exception as e:
            logger.error(f"Failed to get host status summary: {e}")
        
        return summary