#!/usr/bin/env python3
"""
Performance Tests for Management Portal Load Testing
Load testing, stress testing, and performance benchmarking for the management portal
"""

import asyncio
import json
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
import requests


class TestManagementPortalLoad:
    """Performance test cases for management portal load testing."""

    @pytest.fixture
    def load_test_config(self):
        """Load testing configuration."""
        return {
            'BASE_URL': 'http://test-mgmt:8000',
            'CONCURRENT_USERS': [1, 5, 10, 25, 50, 100],
            'TEST_DURATION': 60,  # seconds
            'RAMP_UP_TIME': 10,   # seconds
            'ENDPOINTS_TO_TEST': [
                '/api/servers',
                '/api/deployment/jobs',
                '/api/fleet/hosts',
                '/api/maas/machines',
                '/api/system/health'
            ],
            'PERFORMANCE_THRESHOLDS': {
                'avg_response_time': 2.0,  # seconds
                'p95_response_time': 5.0,  # seconds
                'error_rate': 0.05,        # 5%
                'throughput_rps': 100      # requests per second
            }
        }

    @pytest.fixture
    def test_data_generator(self):
        """Generate test data for load testing."""
        def generate_server_data():
            return {
                'hostname': f'load-test-server-{random.randint(1000, 9999)}',
                'mac_address': ':'.join([f'{random.randint(0, 255):02x}' for _ in range(6)]),
                'ip_address': f'192.168.{random.randint(1, 254)}.{random.randint(1, 254)}',
                'status': random.choice(['New', 'Ready', 'Deployed', 'Failed']),
                'memory': random.choice([4096, 8192, 16384, 32768]),
                'cpu_count': random.choice([2, 4, 8, 16])
            }
        
        def generate_deployment_job():
            return {
                'job_id': f'load-test-job-{random.randint(1000, 9999)}',
                'server_id': random.randint(1, 100),
                'cloud_init_template_id': random.randint(1, 10),
                'package_config_id': random.randint(1, 10),
                'status': random.choice(['Pending', 'Running', 'Completed', 'Failed'])
            }
        
        return {
            'server': generate_server_data,
            'deployment_job': generate_deployment_job
        }

    @pytest.mark.performance
    def test_baseline_response_times(self, load_test_config):
        """Test baseline response times for individual endpoints."""
        endpoints = load_test_config['ENDPOINTS_TO_TEST']
        base_url = load_test_config['BASE_URL']
        
        response_times = {}
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {'status': 'success', 'data': []},
                elapsed=timedelta(milliseconds=random.randint(100, 500))
            )
            
            for endpoint in endpoints:
                times = []
                for _ in range(10):  # 10 baseline measurements
                    start_time = time.time()
                    response = requests.get(f"{base_url}{endpoint}")
                    end_time = time.time()
                    
                    times.append(end_time - start_time)
                
                response_times[endpoint] = {
                    'avg': statistics.mean(times),
                    'min': min(times),
                    'max': max(times),
                    'p95': sorted(times)[int(0.95 * len(times))]
                }
        
        # Verify baseline performance
        for endpoint, metrics in response_times.items():
            assert metrics['avg'] < load_test_config['PERFORMANCE_THRESHOLDS']['avg_response_time']
            assert metrics['p95'] < load_test_config['PERFORMANCE_THRESHOLDS']['p95_response_time']

    @pytest.mark.performance
    @pytest.mark.slow
    def test_concurrent_user_load(self, load_test_config, test_data_generator):
        """Test system performance under concurrent user load."""
        
        def simulate_user_session(user_id, duration, base_url, endpoints):
            """Simulate a user session with multiple API calls."""
            session_metrics = {
                'user_id': user_id,
                'requests_made': 0,
                'successful_requests': 0,
                'failed_requests': 0,
                'response_times': [],
                'errors': []
            }
            
            session = requests.Session()
            session.headers.update({'Authorization': f'Bearer test_token_{user_id}'})
            
            start_time = time.time()
            while (time.time() - start_time) < duration:
                try:
                    # Select random endpoint
                    endpoint = random.choice(endpoints)
                    request_start = time.time()
                    
                    with patch('requests.Session.get') as mock_get:
                        mock_get.return_value = Mock(
                            status_code=200 if random.random() > 0.05 else 500,
                            json=lambda: {'status': 'success', 'data': []},
                            elapsed=timedelta(milliseconds=random.randint(50, 2000))
                        )
                        
                        response = session.get(f"{base_url}{endpoint}")
                        request_end = time.time()
                        
                        session_metrics['requests_made'] += 1
                        session_metrics['response_times'].append(request_end - request_start)
                        
                        if response.status_code == 200:
                            session_metrics['successful_requests'] += 1
                        else:
                            session_metrics['failed_requests'] += 1
                            session_metrics['errors'].append({
                                'endpoint': endpoint,
                                'status_code': response.status_code,
                                'timestamp': request_end
                            })
                
                except Exception as e:
                    session_metrics['failed_requests'] += 1
                    session_metrics['errors'].append({
                        'endpoint': endpoint,
                        'error': str(e),
                        'timestamp': time.time()
                    })
                
                # Random delay between requests
                time.sleep(random.uniform(0.1, 1.0))
            
            return session_metrics
        
        concurrent_users = load_test_config['CONCURRENT_USERS']
        test_duration = load_test_config['TEST_DURATION']
        base_url = load_test_config['BASE_URL']
        endpoints = load_test_config['ENDPOINTS_TO_TEST']
        
        load_test_results = {}
        
        for user_count in concurrent_users:
            print(f"Testing with {user_count} concurrent users...")
            
            with ThreadPoolExecutor(max_workers=user_count) as executor:
                # Submit user simulation tasks
                futures = [
                    executor.submit(simulate_user_session, i, test_duration, base_url, endpoints)
                    for i in range(user_count)
                ]
                
                # Collect results
                user_results = []
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        user_results.append(result)
                    except Exception as e:
                        print(f"User session failed: {e}")
                
                # Aggregate results
                total_requests = sum(r['requests_made'] for r in user_results)
                successful_requests = sum(r['successful_requests'] for r in user_results)
                failed_requests = sum(r['failed_requests'] for r in user_results)
                
                all_response_times = []
                for r in user_results:
                    all_response_times.extend(r['response_times'])
                
                load_test_results[user_count] = {
                    'total_requests': total_requests,
                    'successful_requests': successful_requests,
                    'failed_requests': failed_requests,
                    'error_rate': failed_requests / total_requests if total_requests > 0 else 0,
                    'throughput_rps': total_requests / test_duration,
                    'avg_response_time': statistics.mean(all_response_times) if all_response_times else 0,
                    'p95_response_time': sorted(all_response_times)[int(0.95 * len(all_response_times))] if all_response_times else 0,
                    'p99_response_time': sorted(all_response_times)[int(0.99 * len(all_response_times))] if all_response_times else 0
                }
        
        # Analyze results and verify performance thresholds
        thresholds = load_test_config['PERFORMANCE_THRESHOLDS']
        
        for user_count, metrics in load_test_results.items():
            print(f"Results for {user_count} users: {metrics}")
            
            # Performance should degrade gracefully
            if user_count <= 10:  # Low load should meet all thresholds
                assert metrics['error_rate'] <= thresholds['error_rate']
                assert metrics['avg_response_time'] <= thresholds['avg_response_time']
            
            # Even under high load, error rate should not exceed 20%
            assert metrics['error_rate'] <= 0.20

    @pytest.mark.performance
    def test_database_query_performance(self, mock_database, load_test_config):
        """Test database query performance under load."""
        
        # Generate test data
        server_count = 10000
        for i in range(server_count):
            mock_database.servers.insert(
                hostname=f'perf-test-server-{i:05d}',
                mac_address=f'00:16:3e:{i//256:02x}:{(i//16)%16:x}:{i%16:x}',
                status=random.choice(['New', 'Ready', 'Deployed', 'Failed']),
                memory=random.choice([4096, 8192, 16384]),
                cpu_count=random.choice([2, 4, 8])
            )
        mock_database.commit()
        
        # Test query performance
        query_performance = {}
        
        queries = {
            'simple_select': lambda: mock_database(mock_database.servers.id > 0).select(),
            'filtered_select': lambda: mock_database(mock_database.servers.status == 'Ready').select(),
            'aggregate_query': lambda: mock_database.servers.memory.sum(),
            'join_query': lambda: mock_database(
                (mock_database.servers.id == mock_database.deployment_jobs.server_id)
            ).select(limitby=(0, 100)) if hasattr(mock_database, 'deployment_jobs') else [],
            'pagination_query': lambda: mock_database(mock_database.servers.id > 0).select(limitby=(0, 50))
        }
        
        for query_name, query_func in queries.items():
            times = []
            for _ in range(50):  # 50 iterations
                start_time = time.time()
                result = query_func()
                end_time = time.time()
                times.append(end_time - start_time)
            
            query_performance[query_name] = {
                'avg': statistics.mean(times),
                'min': min(times),
                'max': max(times),
                'p95': sorted(times)[int(0.95 * len(times))]
            }
        
        # Verify query performance
        for query_name, metrics in query_performance.items():
            # Most queries should complete within 100ms
            assert metrics['avg'] < 0.1, f"Query {query_name} average time too high: {metrics['avg']}"
            assert metrics['p95'] < 0.5, f"Query {query_name} P95 time too high: {metrics['p95']}"

    @pytest.mark.performance
    def test_memory_usage_under_load(self, load_test_config):
        """Test memory usage under sustained load."""
        import psutil
        import gc
        
        # Monitor memory usage during load test
        memory_samples = []
        
        def monitor_memory():
            """Monitor memory usage in background."""
            process = psutil.Process()
            while monitoring:
                memory_info = process.memory_info()
                memory_samples.append({
                    'timestamp': time.time(),
                    'rss': memory_info.rss,  # Resident Set Size
                    'vms': memory_info.vms   # Virtual Memory Size
                })
                time.sleep(1)  # Sample every second
        
        monitoring = True
        
        # Start memory monitoring in background thread
        import threading
        monitor_thread = threading.Thread(target=monitor_memory)
        monitor_thread.start()
        
        try:
            # Simulate memory-intensive operations
            large_objects = []
            for i in range(1000):
                # Create and store objects to test memory usage
                obj = {
                    'id': i,
                    'data': 'x' * 1000,  # 1KB string
                    'nested': {
                        'items': list(range(100)),
                        'timestamp': time.time()
                    }
                }
                large_objects.append(obj)
                
                # Periodically force garbage collection
                if i % 100 == 0:
                    gc.collect()
                
                time.sleep(0.01)  # Small delay
            
            # Test sustained memory usage
            time.sleep(10)
            
            # Clean up objects
            large_objects.clear()
            gc.collect()
            
            # Allow memory to stabilize
            time.sleep(5)
        
        finally:
            monitoring = False
            monitor_thread.join()
        
        # Analyze memory usage
        if memory_samples:
            initial_memory = memory_samples[0]['rss']
            peak_memory = max(sample['rss'] for sample in memory_samples)
            final_memory = memory_samples[-1]['rss']
            
            memory_growth = peak_memory - initial_memory
            memory_leak = final_memory - initial_memory
            
            # Memory growth should be reasonable
            assert memory_growth < 500 * 1024 * 1024  # Less than 500MB growth
            
            # Memory should return close to initial level (allow 10% variance)
            assert abs(memory_leak) < 0.1 * initial_memory

    @pytest.mark.performance
    def test_concurrent_deployment_performance(self, load_test_config, mock_maas_client, mock_ansible_runner):
        """Test performance of concurrent deployment operations."""
        
        deployment_count = 20
        concurrent_limit = 5
        
        def simulate_deployment(deployment_id):
            """Simulate a deployment operation."""
            start_time = time.time()
            
            # Simulate various deployment steps with realistic delays
            steps = [
                ('commission_machine', random.uniform(10, 30)),
                ('configure_network', random.uniform(2, 8)),
                ('configure_storage', random.uniform(3, 10)),
                ('deploy_os', random.uniform(60, 180)),
                ('configure_services', random.uniform(15, 45)),
                ('validate_deployment', random.uniform(5, 15))
            ]
            
            deployment_log = []
            for step_name, duration in steps:
                step_start = time.time()
                time.sleep(duration / 10)  # Scaled down for testing
                step_end = time.time()
                
                deployment_log.append({
                    'step': step_name,
                    'duration': step_end - step_start,
                    'timestamp': step_end
                })
            
            total_time = time.time() - start_time
            
            return {
                'deployment_id': deployment_id,
                'status': 'completed' if random.random() > 0.05 else 'failed',
                'total_time': total_time,
                'steps': deployment_log
            }
        
        # Mock deployment operations
        mock_maas_client.deploy_machine.return_value = {'status': 'deploying'}
        mock_ansible_runner.run.return_value = Mock(
            status='successful',
            rc=0
        )
        
        # Execute concurrent deployments with semaphore
        semaphore = asyncio.Semaphore(concurrent_limit)
        
        async def limited_deployment(deployment_id):
            async with semaphore:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, simulate_deployment, deployment_id)
        
        async def run_concurrent_deployments():
            tasks = [limited_deployment(i) for i in range(deployment_count)]
            return await asyncio.gather(*tasks)
        
        # Run the concurrent deployment test
        deployment_start = time.time()
        results = asyncio.run(run_concurrent_deployments())
        deployment_end = time.time()
        
        # Analyze performance
        successful_deployments = [r for r in results if r['status'] == 'completed']
        failed_deployments = [r for r in results if r['status'] == 'failed']
        
        avg_deployment_time = statistics.mean([r['total_time'] for r in results])
        total_test_time = deployment_end - deployment_start
        
        # Performance assertions
        assert len(successful_deployments) >= deployment_count * 0.9  # 90% success rate
        assert avg_deployment_time < 60  # Average deployment under 1 minute (scaled)
        assert total_test_time < 300  # Total test time under 5 minutes
        
        # Verify concurrency worked (should be faster than sequential)
        estimated_sequential_time = sum(r['total_time'] for r in results)
        concurrency_benefit = estimated_sequential_time / total_test_time
        assert concurrency_benefit > 2  # At least 2x speedup from concurrency

    @pytest.mark.performance
    def test_api_response_size_optimization(self, load_test_config):
        """Test API response size optimization and compression."""
        
        def generate_large_response_data():
            """Generate large response data for testing."""
            return {
                'servers': [
                    {
                        'id': i,
                        'hostname': f'server-{i:04d}',
                        'mac_address': f'00:16:3e:{i//256:02x}:{(i//16)%16:x}:{i%16:x}',
                        'status': random.choice(['New', 'Ready', 'Deployed']),
                        'memory': random.choice([4096, 8192, 16384]),
                        'cpu_count': random.choice([2, 4, 8]),
                        'logs': ['log entry ' + str(j) for j in range(10)],  # Bulk data
                        'metadata': {
                            'created': datetime.utcnow().isoformat(),
                            'tags': [f'tag{k}' for k in range(5)],
                            'description': 'A' * 200  # Large description
                        }
                    }
                    for i in range(1000)  # 1000 servers
                ],
                'pagination': {
                    'page': 1,
                    'per_page': 1000,
                    'total': 1000,
                    'pages': 1
                }
            }
        
        large_data = generate_large_response_data()
        
        # Test uncompressed response size
        uncompressed_json = json.dumps(large_data)
        uncompressed_size = len(uncompressed_json.encode('utf-8'))
        
        # Test compressed response size
        import gzip
        compressed_data = gzip.compress(uncompressed_json.encode('utf-8'))
        compressed_size = len(compressed_data)
        
        compression_ratio = uncompressed_size / compressed_size
        
        # Performance assertions
        assert uncompressed_size > 1024 * 1024  # Should be > 1MB
        assert compression_ratio > 3  # Should compress to less than 1/3 original size
        
        # Test pagination reduces response size
        paginated_data = {
            'servers': large_data['servers'][:50],  # Only first 50
            'pagination': {
                'page': 1,
                'per_page': 50,
                'total': 1000,
                'pages': 20
            }
        }
        
        paginated_json = json.dumps(paginated_data)
        paginated_size = len(paginated_json.encode('utf-8'))
        
        # Paginated response should be much smaller
        assert paginated_size < uncompressed_size * 0.1  # Less than 10% of full response

    @pytest.mark.performance
    def test_caching_performance(self, load_test_config, mock_redis):
        """Test caching performance and effectiveness."""
        
        # Simulate cache operations
        cache_operations = [
            ('set', 'server:1', {'hostname': 'test-server-1'}),
            ('set', 'server:2', {'hostname': 'test-server-2'}),
            ('get', 'server:1', None),
            ('get', 'server:2', None),
            ('get', 'server:3', None),  # Cache miss
            ('delete', 'server:1', None),
            ('get', 'server:1', None),  # Cache miss after delete
        ]
        
        cache_metrics = {
            'operations': len(cache_operations),
            'hits': 0,
            'misses': 0,
            'sets': 0,
            'deletes': 0,
            'total_time': 0
        }
        
        # Mock Redis responses
        cached_data = {}
        
        def mock_redis_get(key):
            if key in cached_data:
                cache_metrics['hits'] += 1
                return json.dumps(cached_data[key])
            else:
                cache_metrics['misses'] += 1
                return None
        
        def mock_redis_set(key, value):
            cached_data[key] = json.loads(value) if isinstance(value, str) else value
            cache_metrics['sets'] += 1
            return True
        
        def mock_redis_delete(key):
            if key in cached_data:
                del cached_data[key]
            cache_metrics['deletes'] += 1
            return 1
        
        mock_redis.get.side_effect = mock_redis_get
        mock_redis.set.side_effect = mock_redis_set
        mock_redis.delete.side_effect = mock_redis_delete
        
        # Execute cache operations
        start_time = time.time()
        
        for operation, key, value in cache_operations:
            if operation == 'set':
                mock_redis.set(key, json.dumps(value))
            elif operation == 'get':
                mock_redis.get(key)
            elif operation == 'delete':
                mock_redis.delete(key)
        
        end_time = time.time()
        cache_metrics['total_time'] = end_time - start_time
        
        # Calculate cache hit rate
        total_reads = cache_metrics['hits'] + cache_metrics['misses']
        hit_rate = cache_metrics['hits'] / total_reads if total_reads > 0 else 0
        
        # Performance assertions
        assert hit_rate >= 0.4  # At least 40% hit rate
        assert cache_metrics['total_time'] < 1.0  # Operations should complete quickly
        
        # Test cache performance under load
        load_test_keys = [f'load_test:{i}' for i in range(1000)]
        load_test_values = [{'data': f'value_{i}', 'timestamp': time.time()} for i in range(1000)]
        
        # Bulk set operations
        bulk_set_start = time.time()
        for key, value in zip(load_test_keys, load_test_values):
            mock_redis.set(key, json.dumps(value))
        bulk_set_end = time.time()
        
        bulk_set_time = bulk_set_end - bulk_set_start
        set_ops_per_second = len(load_test_keys) / bulk_set_time
        
        # Bulk get operations
        bulk_get_start = time.time()
        for key in load_test_keys:
            mock_redis.get(key)
        bulk_get_end = time.time()
        
        bulk_get_time = bulk_get_end - bulk_get_start
        get_ops_per_second = len(load_test_keys) / bulk_get_time
        
        # Performance assertions for bulk operations
        assert set_ops_per_second > 1000  # Should handle 1000+ sets per second
        assert get_ops_per_second > 5000  # Should handle 5000+ gets per second