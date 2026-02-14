/**
 * DeploymentsPage - Deployment Job Management
 *
 * Provides interface for managing deployment jobs, viewing status, and logs.
 */

import React, { useState, useEffect, useCallback } from 'react';
import api from '../../lib/api';
import Card from '../../components/Card';
import TabNavigation from '../../components/TabNavigation';

interface Deployment {
  id: number;
  asset_id: number;
  profile_id: number;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  created_at: string;
  asset_name?: string;
  profile_name?: string;
}

type ViewMode = 'list' | 'details';

/**
 * Main deployments management page.
 */
export const DeploymentsPage: React.FC = () => {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [selectedDeployment, setSelectedDeployment] = useState<Deployment | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('list');
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState('all');
  const [logs, setLogs] = useState<string>('');

  const tabs = [
    { id: 'all', label: 'All' },
    { id: 'running', label: 'Running' },
    { id: 'completed', label: 'Completed' },
    { id: 'failed', label: 'Failed' },
  ];

  // Fetch deployments
  const fetchDeployments = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const response = await api.get('/deployments');
      setDeployments(response.data.deployments || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch deployments';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDeployments();
    const interval = setInterval(fetchDeployments, 5000);
    return () => clearInterval(interval);
  }, [fetchDeployments]);

  // Fetch deployment logs
  const fetchLogs = useCallback(async (deploymentId: number) => {
    try {
      const response = await api.get(`/deployments/${deploymentId}/logs`);
      setLogs(response.data.logs || 'No logs available');
    } catch (err: any) {
      setLogs('Failed to fetch logs');
    }
  }, []);

  // Handle deployment selection
  const handleSelectDeployment = useCallback((deployment: Deployment) => {
    setSelectedDeployment(deployment);
    setViewMode('details');
    fetchLogs(deployment.id);
  }, [fetchLogs]);

  // Handle cancel deployment
  const handleCancelDeployment = useCallback(async (deploymentId: number) => {
    if (!confirm('Are you sure you want to cancel this deployment?')) return;

    try {
      await api.post(`/deployments/${deploymentId}/cancel`);
      await fetchDeployments();
      setError(null);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to cancel deployment';
      setError(message);
    }
  }, [fetchDeployments]);

  // Handle retry deployment
  const handleRetryDeployment = useCallback(async (deploymentId: number) => {
    try {
      await api.post(`/deployments/${deploymentId}/retry`);
      await fetchDeployments();
      setError(null);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to retry deployment';
      setError(message);
    }
  }, [fetchDeployments]);

  // Handle back to list
  const handleBackToList = useCallback(() => {
    setViewMode('list');
    setSelectedDeployment(null);
    setLogs('');
  }, []);

  // Filter deployments by status
  const filteredDeployments = deployments.filter((d) => {
    if (activeTab === 'all') return true;
    return d.status === activeTab;
  });

  // Get status badge class
  const getStatusBadge = (status: string) => {
    const statusColors: Record<string, string> = {
      pending: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/50',
      running: 'bg-blue-500/20 text-blue-400 border-blue-500/50',
      completed: 'bg-green-500/20 text-green-400 border-green-500/50',
      failed: 'bg-red-500/20 text-red-400 border-red-500/50',
      cancelled: 'bg-gray-500/20 text-gray-400 border-gray-500/50',
    };
    return statusColors[status] || statusColors.pending;
  };

  // Render deployment list
  const renderList = () => {
    if (isLoading && deployments.length === 0) {
      return (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
        </div>
      );
    }

    if (filteredDeployments.length === 0) {
      return (
        <div className="text-center py-12 text-dark-400">
          <svg
            className="mx-auto h-12 w-12 mb-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1}
              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
            />
          </svg>
          <p className="text-lg">No deployments found</p>
          <p className="text-sm mt-1">
            {activeTab === 'all' ? 'Start a deployment from Assets page' : `No ${activeTab} deployments`}
          </p>
        </div>
      );
    }

    return (
      <div className="space-y-3">
        {filteredDeployments.map((deployment) => (
          <div
            key={deployment.id}
            className="bg-dark-800 border border-dark-700 rounded-lg p-4
                       hover:border-gold-600/50 transition-colors cursor-pointer"
            onClick={() => handleSelectDeployment(deployment)}
          >
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-2">
                  <span className="text-white font-medium">
                    Deployment #{deployment.id}
                  </span>
                  <span className={`px-2 py-0.5 text-xs rounded border ${getStatusBadge(deployment.status)}`}>
                    {deployment.status}
                  </span>
                </div>
                <div className="text-sm text-dark-400 space-y-1">
                  <div>Asset: {deployment.asset_name || `Asset #${deployment.asset_id}`}</div>
                  <div>Profile: {deployment.profile_name || `Profile #${deployment.profile_id}`}</div>
                  <div>Created: {new Date(deployment.created_at).toLocaleString()}</div>
                  {deployment.started_at && (
                    <div>Started: {new Date(deployment.started_at).toLocaleString()}</div>
                  )}
                  {deployment.completed_at && (
                    <div>Completed: {new Date(deployment.completed_at).toLocaleString()}</div>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2 ml-4">
                {deployment.status === 'running' && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleCancelDeployment(deployment.id);
                    }}
                    className="px-3 py-1.5 text-sm bg-red-600/20 hover:bg-red-600/30
                               text-red-400 rounded transition-colors"
                    title="Cancel deployment"
                  >
                    Cancel
                  </button>
                )}
                {deployment.status === 'failed' && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleRetryDeployment(deployment.id);
                    }}
                    className="px-3 py-1.5 text-sm bg-blue-600/20 hover:bg-blue-600/30
                               text-blue-400 rounded transition-colors"
                    title="Retry deployment"
                  >
                    Retry
                  </button>
                )}
              </div>
            </div>
            {deployment.error_message && (
              <div className="mt-3 p-2 bg-red-900/20 border border-red-700/50 rounded text-sm text-red-400">
                {deployment.error_message}
              </div>
            )}
          </div>
        ))}
      </div>
    );
  };

  // Render deployment details
  const renderDetails = () => {
    if (!selectedDeployment) return null;

    return (
      <div className="space-y-6">
        <Card title="Deployment Information">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <div className="text-sm text-dark-400 mb-1">Status</div>
              <span className={`inline-block px-2 py-1 text-sm rounded border ${getStatusBadge(selectedDeployment.status)}`}>
                {selectedDeployment.status}
              </span>
            </div>
            <div>
              <div className="text-sm text-dark-400 mb-1">Deployment ID</div>
              <div className="text-white">#{selectedDeployment.id}</div>
            </div>
            <div>
              <div className="text-sm text-dark-400 mb-1">Asset</div>
              <div className="text-white">
                {selectedDeployment.asset_name || `Asset #${selectedDeployment.asset_id}`}
              </div>
            </div>
            <div>
              <div className="text-sm text-dark-400 mb-1">Profile</div>
              <div className="text-white">
                {selectedDeployment.profile_name || `Profile #${selectedDeployment.profile_id}`}
              </div>
            </div>
            <div>
              <div className="text-sm text-dark-400 mb-1">Created</div>
              <div className="text-white">{new Date(selectedDeployment.created_at).toLocaleString()}</div>
            </div>
            {selectedDeployment.started_at && (
              <div>
                <div className="text-sm text-dark-400 mb-1">Started</div>
                <div className="text-white">{new Date(selectedDeployment.started_at).toLocaleString()}</div>
              </div>
            )}
            {selectedDeployment.completed_at && (
              <div>
                <div className="text-sm text-dark-400 mb-1">Completed</div>
                <div className="text-white">{new Date(selectedDeployment.completed_at).toLocaleString()}</div>
              </div>
            )}
          </div>
          {selectedDeployment.error_message && (
            <div className="mt-4 p-3 bg-red-900/30 border border-red-700 rounded">
              <div className="text-sm text-dark-400 mb-1">Error</div>
              <div className="text-red-400">{selectedDeployment.error_message}</div>
            </div>
          )}
        </Card>

        <Card title="Deployment Logs">
          <div className="bg-dark-950 rounded p-4 overflow-x-auto">
            <pre className="text-sm text-green-400 font-mono whitespace-pre-wrap">
              {logs}
            </pre>
          </div>
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => fetchLogs(selectedDeployment.id)}
              className="px-4 py-2 bg-dark-800 hover:bg-dark-700 text-gold-400
                         border border-dark-700 rounded transition-colors"
            >
              Refresh Logs
            </button>
            {selectedDeployment.status === 'running' && (
              <button
                onClick={() => handleCancelDeployment(selectedDeployment.id)}
                className="px-4 py-2 bg-red-600/20 hover:bg-red-600/30 text-red-400
                           border border-red-700/50 rounded transition-colors"
              >
                Cancel Deployment
              </button>
            )}
            {selectedDeployment.status === 'failed' && (
              <button
                onClick={() => handleRetryDeployment(selectedDeployment.id)}
                className="px-4 py-2 bg-blue-600/20 hover:bg-blue-600/30 text-blue-400
                           border border-blue-700/50 rounded transition-colors"
              >
                Retry Deployment
              </button>
            )}
          </div>
        </Card>
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          {viewMode === 'details' && (
            <button
              onClick={handleBackToList}
              className="p-2 hover:bg-dark-800 rounded transition-colors"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
              </svg>
            </button>
          )}
          <h1 className="text-2xl font-bold text-gold-500">
            {viewMode === 'details' ? `Deployment #${selectedDeployment?.id}` : 'Deployments'}
          </h1>
        </div>
        {viewMode === 'list' && (
          <button
            onClick={fetchDeployments}
            className="p-2 hover:bg-dark-800 rounded transition-colors text-gold-400"
            title="Refresh"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        )}
      </div>

      {/* Error Message */}
      {error && (
        <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-3">
          <svg className="h-5 w-5 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span className="text-red-300">{error}</span>
          <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-300">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      )}

      {/* Tab Navigation (only in list view) */}
      {viewMode === 'list' && (
        <TabNavigation tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />
      )}

      {/* Content */}
      <div className="mt-6">
        <div className="bg-dark-900 rounded-lg border border-dark-700 p-6">
          {viewMode === 'list' ? renderList() : renderDetails()}
        </div>
      </div>
    </div>
  );
};

export default DeploymentsPage;
