/**
 * Provisioning Dashboard
 *
 * Overview of provisioning system with machine counts, recent deployments, and quick actions.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../../lib/api';

interface MachineStats {
  total: number;
  by_state: {
    ready: number;
    allocated: number;
    deploying: number;
    failed: number;
    retired: number;
  };
  by_zone: Array<{
    zone: string;
    count: number;
  }>;
  by_pool: Array<{
    pool: string;
    count: number;
  }>;
}

interface RecentDeployment {
  id: number;
  machine_id: string;
  hostname: string;
  egg_name: string;
  state: string;
  deployed_at: string;
  duration_seconds?: number;
}

export const ProvisioningDashboard: React.FC = () => {
  const navigate = useNavigate();
  const [stats, setStats] = useState<MachineStats | null>(null);
  const [recentDeployments, setRecentDeployments] = useState<RecentDeployment[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const [statsRes, deploymentsRes] = await Promise.all([
        api.get('/provisioning/stats'),
        api.get('/provisioning/deployments/recent'),
      ]);

      setStats(statsRes.data);
      setRecentDeployments(deploymentsRes.data.deployments || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch dashboard data';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const getStateColor = (state: string) => {
    switch (state.toLowerCase()) {
      case 'ready':
        return 'text-green-400 bg-green-900/30 border-green-700';
      case 'allocated':
        return 'text-blue-400 bg-blue-900/30 border-blue-700';
      case 'deploying':
        return 'text-gold-400 bg-gold-900/30 border-gold-700';
      case 'failed':
        return 'text-red-400 bg-red-900/30 border-red-700';
      case 'retired':
        return 'text-gray-400 bg-gray-900/30 border-gray-700';
      default:
        return 'text-dark-400 bg-dark-800 border-dark-700';
    }
  };

  const getStateIcon = (state: string) => {
    switch (state.toLowerCase()) {
      case 'ready':
        return (
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        );
      case 'allocated':
        return (
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
          </svg>
        );
      case 'deploying':
        return (
          <svg className="h-5 w-5 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        );
      case 'failed':
        return (
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        );
      default:
        return (
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2" />
          </svg>
        );
    }
  };

  const formatDuration = (seconds?: number) => {
    if (!seconds) return 'N/A';
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${minutes}m ${secs}s`;
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-dark-950 text-white p-6">
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gold-500">Provisioning Dashboard</h1>
        <button
          onClick={() => navigate('/provisioning/machines')}
          className="inline-flex items-center gap-2 px-4 py-2
                     bg-gold-600 hover:bg-gold-500 text-dark-900 font-medium
                     rounded transition-colors"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
          </svg>
          View All Machines
        </button>
      </div>

      {/* Error Message */}
      {error && (
        <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-3">
          <svg className="h-5 w-5 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span className="text-red-300">{error}</span>
        </div>
      )}

      {/* Machine State Overview */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5 mb-6">
        {stats?.by_state && Object.entries(stats.by_state).map(([state, count]) => (
          <div
            key={state}
            className="bg-dark-900 border border-dark-700 rounded-lg p-4 hover:border-gold-600/50 transition-colors cursor-pointer"
            onClick={() => navigate(`/provisioning/machines?state=${state}`)}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm text-dark-400 capitalize">{state}</span>
              <div className={getStateColor(state)}>
                {getStateIcon(state)}
              </div>
            </div>
            <p className="text-2xl font-bold text-white">{count}</p>
          </div>
        ))}
      </div>

      {/* Stats Grid */}
      <div className="grid gap-6 md:grid-cols-2 mb-6">
        {/* By Zone */}
        <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
          <h2 className="text-lg font-semibold text-gold-500 mb-4 flex items-center gap-2">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Machines by Zone
          </h2>
          <div className="space-y-2">
            {stats?.by_zone && stats.by_zone.length > 0 ? (
              stats.by_zone.map((item) => (
                <div key={item.zone} className="flex items-center justify-between p-2 bg-dark-800 rounded">
                  <span className="text-sm text-dark-300">{item.zone}</span>
                  <span className="text-sm font-semibold text-white">{item.count}</span>
                </div>
              ))
            ) : (
              <p className="text-dark-400 text-sm">No zones configured</p>
            )}
          </div>
        </div>

        {/* By Pool */}
        <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
          <h2 className="text-lg font-semibold text-gold-500 mb-4 flex items-center gap-2">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
            Machines by Pool
          </h2>
          <div className="space-y-2">
            {stats?.by_pool && stats.by_pool.length > 0 ? (
              stats.by_pool.map((item) => (
                <div key={item.pool} className="flex items-center justify-between p-2 bg-dark-800 rounded">
                  <span className="text-sm text-dark-300">{item.pool}</span>
                  <span className="text-sm font-semibold text-white">{item.count}</span>
                </div>
              ))
            ) : (
              <p className="text-dark-400 text-sm">No pools configured</p>
            )}
          </div>
        </div>
      </div>

      {/* Recent Deployments */}
      <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
        <h2 className="text-lg font-semibold text-gold-500 mb-4 flex items-center gap-2">
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          Recent Deployments
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-dark-700">
                <th className="text-left py-2 px-3 text-sm font-medium text-dark-400">Machine</th>
                <th className="text-left py-2 px-3 text-sm font-medium text-dark-400">Hostname</th>
                <th className="text-left py-2 px-3 text-sm font-medium text-dark-400">Egg</th>
                <th className="text-left py-2 px-3 text-sm font-medium text-dark-400">State</th>
                <th className="text-left py-2 px-3 text-sm font-medium text-dark-400">Duration</th>
                <th className="text-left py-2 px-3 text-sm font-medium text-dark-400">Deployed</th>
              </tr>
            </thead>
            <tbody>
              {recentDeployments.length > 0 ? (
                recentDeployments.map((deployment) => (
                  <tr
                    key={deployment.id}
                    className="border-b border-dark-800 hover:bg-dark-800/50 cursor-pointer transition-colors"
                    onClick={() => navigate(`/provisioning/machines/${deployment.machine_id}`)}
                  >
                    <td className="py-2 px-3 text-sm font-mono text-dark-300">{deployment.machine_id}</td>
                    <td className="py-2 px-3 text-sm text-white">{deployment.hostname}</td>
                    <td className="py-2 px-3 text-sm text-dark-300">{deployment.egg_name}</td>
                    <td className="py-2 px-3">
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded border ${getStateColor(deployment.state)}`}>
                        {deployment.state}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-sm text-dark-300">{formatDuration(deployment.duration_seconds)}</td>
                    <td className="py-2 px-3 text-sm text-dark-400">
                      {new Date(deployment.deployed_at).toLocaleString()}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-dark-400">
                    No recent deployments
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Quick Actions */}
      <div className="mt-6 grid gap-4 md:grid-cols-3">
        <button
          onClick={() => navigate('/provisioning/machines?state=ready')}
          className="p-4 bg-dark-900 border border-dark-700 rounded-lg hover:border-gold-600/50 transition-colors text-left"
        >
          <div className="flex items-center gap-3 mb-2">
            <svg className="h-6 w-6 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
            <span className="font-semibold text-white">Ready Machines</span>
          </div>
          <p className="text-sm text-dark-400">View all machines ready for deployment</p>
        </button>

        <button
          onClick={() => navigate('/provisioning/machines?state=deploying')}
          className="p-4 bg-dark-900 border border-dark-700 rounded-lg hover:border-gold-600/50 transition-colors text-left"
        >
          <div className="flex items-center gap-3 mb-2">
            <svg className="h-6 w-6 text-gold-400 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            <span className="font-semibold text-white">Active Deployments</span>
          </div>
          <p className="text-sm text-dark-400">Monitor machines currently deploying</p>
        </button>

        <button
          onClick={() => navigate('/provisioning/machines?state=failed')}
          className="p-4 bg-dark-900 border border-dark-700 rounded-lg hover:border-gold-600/50 transition-colors text-left"
        >
          <div className="flex items-center gap-3 mb-2">
            <svg className="h-6 w-6 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span className="font-semibold text-white">Failed Machines</span>
          </div>
          <p className="text-sm text-dark-400">Review and retry failed deployments</p>
        </button>
      </div>
    </div>
  );
};

export default ProvisioningDashboard;
