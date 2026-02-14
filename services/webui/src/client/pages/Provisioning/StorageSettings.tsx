/**
 * StorageSettings - S3 Storage Backend Configuration
 *
 * Provides interface for managing S3-compatible storage backends for boot images.
 */

import React, { useState, useEffect, useCallback } from 'react';
import api from '../../lib/api';
import Card from '../../components/Card';

interface StorageBackend {
  id: number;
  name: string;
  endpoint: string;
  access_key: string;
  bucket: string;
  region: string;
  use_ssl: boolean;
  is_default: boolean;
  status: 'active' | 'inactive' | 'error';
  created_at: string;
  last_tested?: string;
}

type ViewMode = 'list' | 'create' | 'edit';

/**
 * Storage settings page for S3 backend configuration.
 */
export const StorageSettings: React.FC = () => {
  const [backends, setBackends] = useState<StorageBackend[]>([]);
  const [selectedBackend, setSelectedBackend] = useState<StorageBackend | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('list');
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [formData, setFormData] = useState({
    name: '',
    endpoint: '',
    access_key: '',
    secret_key: '',
    bucket: '',
    region: 'us-east-1',
    use_ssl: true,
  });

  // Fetch storage backends
  const fetchBackends = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const response = await api.get('/provisioning/storage');
      setBackends(response.data.backends || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch storage backends';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchBackends();
  }, [fetchBackends]);

  // Handle create backend
  const handleCreateClick = useCallback(() => {
    setFormData({
      name: '',
      endpoint: '',
      access_key: '',
      secret_key: '',
      bucket: '',
      region: 'us-east-1',
      use_ssl: true,
    });
    setSelectedBackend(null);
    setViewMode('create');
  }, []);

  // Handle edit backend
  const handleEditClick = useCallback((backend: StorageBackend) => {
    setFormData({
      name: backend.name,
      endpoint: backend.endpoint,
      access_key: backend.access_key,
      secret_key: '',
      bucket: backend.bucket,
      region: backend.region,
      use_ssl: backend.use_ssl,
    });
    setSelectedBackend(backend);
    setViewMode('edit');
  }, []);

  // Handle form submit
  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);
    setError(null);
    setSuccess(null);

    try {
      if (viewMode === 'create') {
        await api.post('/provisioning/storage', formData);
        setSuccess('Storage backend created successfully');
      } else if (viewMode === 'edit' && selectedBackend) {
        const updateData: any = { ...formData };
        if (!updateData.secret_key) {
          delete updateData.secret_key;
        }
        await api.put(`/provisioning/storage/${selectedBackend.id}`, updateData);
        setSuccess('Storage backend updated successfully');
      }
      await fetchBackends();
      setViewMode('list');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to save storage backend';
      setError(message);
    } finally {
      setIsSaving(false);
    }
  }, [viewMode, selectedBackend, formData, fetchBackends]);

  // Handle test connection
  const handleTestConnection = useCallback(async (backendId?: number) => {
    setIsTesting(true);
    setError(null);
    setSuccess(null);

    try {
      const data = backendId ? null : formData;
      const url = backendId
        ? `/provisioning/storage/${backendId}/test`
        : '/provisioning/storage/test';

      const response = await api.post(url, data);
      setSuccess(response.data.message || 'Connection test successful');
      setTimeout(() => setSuccess(null), 3000);

      if (backendId) {
        await fetchBackends();
      }
    } catch (err: any) {
      const message = err.response?.data?.error || 'Connection test failed';
      setError(message);
    } finally {
      setIsTesting(false);
    }
  }, [formData, fetchBackends]);

  // Handle set default
  const handleSetDefault = useCallback(async (backendId: number) => {
    try {
      await api.post(`/provisioning/storage/${backendId}/default`);
      await fetchBackends();
      setSuccess('Default storage backend updated');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to set default backend';
      setError(message);
    }
  }, [fetchBackends]);

  // Handle delete backend
  const handleDelete = useCallback(async (backendId: number) => {
    if (!confirm('Are you sure you want to delete this storage backend?')) return;

    try {
      await api.delete(`/provisioning/storage/${backendId}`);
      await fetchBackends();
      setSuccess('Storage backend deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to delete storage backend';
      setError(message);
    }
  }, [fetchBackends]);

  // Handle back to list
  const handleBackToList = useCallback(() => {
    setViewMode('list');
    setSelectedBackend(null);
    setError(null);
  }, []);

  // Get status badge class
  const getStatusBadge = (status: string) => {
    const statusColors: Record<string, string> = {
      active: 'bg-green-500/20 text-green-400 border-green-500/50',
      inactive: 'bg-gray-500/20 text-gray-400 border-gray-500/50',
      error: 'bg-red-500/20 text-red-400 border-red-500/50',
    };
    return statusColors[status] || statusColors.inactive;
  };

  // Render backend list
  const renderList = () => {
    if (isLoading) {
      return (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
        </div>
      );
    }

    if (backends.length === 0) {
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
              d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4"
            />
          </svg>
          <p className="text-lg">No storage backends configured</p>
          <p className="text-sm mt-1">Add an S3-compatible storage backend to store boot images</p>
        </div>
      );
    }

    return (
      <div className="space-y-4">
        {backends.map((backend) => (
          <div
            key={backend.id}
            className="bg-dark-800 border border-dark-700 rounded-lg p-4"
          >
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-2">
                  <h3 className="text-white font-medium">{backend.name}</h3>
                  <span className={`px-2 py-0.5 text-xs rounded border ${getStatusBadge(backend.status)}`}>
                    {backend.status}
                  </span>
                  {backend.is_default && (
                    <span className="px-2 py-0.5 text-xs rounded bg-gold-500/20 text-gold-400 border border-gold-500/50">
                      default
                    </span>
                  )}
                </div>
                <div className="text-sm text-dark-400 space-y-1">
                  <div>Endpoint: {backend.endpoint}</div>
                  <div>Bucket: {backend.bucket}</div>
                  <div>Region: {backend.region}</div>
                  <div>SSL: {backend.use_ssl ? 'Enabled' : 'Disabled'}</div>
                  {backend.last_tested && (
                    <div>Last Tested: {new Date(backend.last_tested).toLocaleString()}</div>
                  )}
                </div>
              </div>
              <div className="flex flex-col gap-2 ml-4">
                <button
                  onClick={() => handleTestConnection(backend.id)}
                  disabled={isTesting}
                  className="px-3 py-1.5 text-sm bg-blue-600/20 hover:bg-blue-600/30
                             text-blue-400 rounded transition-colors disabled:opacity-50"
                >
                  Test
                </button>
                {!backend.is_default && (
                  <button
                    onClick={() => handleSetDefault(backend.id)}
                    className="px-3 py-1.5 text-sm bg-gold-600/20 hover:bg-gold-600/30
                               text-gold-400 rounded transition-colors"
                  >
                    Set Default
                  </button>
                )}
                <button
                  onClick={() => handleEditClick(backend)}
                  className="px-3 py-1.5 text-sm bg-dark-700 hover:bg-dark-600
                             text-dark-300 rounded transition-colors"
                >
                  Edit
                </button>
                {!backend.is_default && (
                  <button
                    onClick={() => handleDelete(backend.id)}
                    className="px-3 py-1.5 text-sm bg-red-600/20 hover:bg-red-600/30
                               text-red-400 rounded transition-colors"
                  >
                    Delete
                  </button>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    );
  };

  // Render form
  const renderForm = () => (
    <Card title={viewMode === 'create' ? 'Add Storage Backend' : 'Edit Storage Backend'}>
      <form onSubmit={handleSubmit} className="space-y-6">
        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">Name *</span>
            <input
              type="text"
              className="input"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              placeholder="Production S3"
              required
            />
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">Endpoint *</span>
            <input
              type="text"
              className="input"
              value={formData.endpoint}
              onChange={(e) => setFormData({ ...formData, endpoint: e.target.value })}
              placeholder="s3.amazonaws.com or minio.example.com:9000"
              required
            />
            <span className="text-sm text-dark-400 mt-1 block">
              S3-compatible endpoint (AWS S3, MinIO, etc.)
            </span>
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">Access Key *</span>
            <input
              type="text"
              className="input"
              value={formData.access_key}
              onChange={(e) => setFormData({ ...formData, access_key: e.target.value })}
              placeholder="AKIAIOSFODNN7EXAMPLE"
              required
            />
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">
              Secret Key {viewMode === 'edit' && '(leave blank to keep current)'}
            </span>
            <input
              type="password"
              className="input"
              value={formData.secret_key}
              onChange={(e) => setFormData({ ...formData, secret_key: e.target.value })}
              placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
              required={viewMode === 'create'}
            />
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">Bucket Name *</span>
            <input
              type="text"
              className="input"
              value={formData.bucket}
              onChange={(e) => setFormData({ ...formData, bucket: e.target.value })}
              placeholder="boot-images"
              required
            />
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">Region</span>
            <input
              type="text"
              className="input"
              value={formData.region}
              onChange={(e) => setFormData({ ...formData, region: e.target.value })}
              placeholder="us-east-1"
            />
          </label>
        </div>

        <div>
          <label className="flex items-center justify-between">
            <div>
              <span className="text-gold-400 block">Use SSL/TLS</span>
              <span className="text-sm text-dark-400">Enable HTTPS for secure connections</span>
            </div>
            <input
              type="checkbox"
              className="w-5 h-5"
              checked={formData.use_ssl}
              onChange={(e) => setFormData({ ...formData, use_ssl: e.target.checked })}
            />
          </label>
        </div>

        <div className="pt-4 border-t border-dark-700 flex gap-3">
          <button
            type="submit"
            disabled={isSaving}
            className="px-4 py-2 bg-gold-600 hover:bg-gold-500 text-dark-900
                       font-medium rounded transition-colors disabled:opacity-50"
          >
            {isSaving ? 'Saving...' : viewMode === 'create' ? 'Add Backend' : 'Update Backend'}
          </button>
          <button
            type="button"
            onClick={() => handleTestConnection()}
            disabled={isTesting}
            className="px-4 py-2 bg-blue-600/20 hover:bg-blue-600/30 text-blue-400
                       border border-blue-700/50 rounded transition-colors disabled:opacity-50"
          >
            {isTesting ? 'Testing...' : 'Test Connection'}
          </button>
          <button
            type="button"
            onClick={handleBackToList}
            className="px-4 py-2 bg-dark-800 hover:bg-dark-700 text-dark-300
                       border border-dark-700 rounded transition-colors"
          >
            Cancel
          </button>
        </div>
      </form>
    </Card>
  );

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          {viewMode !== 'list' && (
            <button
              onClick={handleBackToList}
              className="p-2 hover:bg-dark-800 rounded transition-colors"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
              </svg>
            </button>
          )}
          <div>
            <h1 className="text-2xl font-bold text-gold-500">Storage Backends</h1>
            <p className="text-dark-400 mt-1">Manage S3-compatible storage for boot images</p>
          </div>
        </div>

        {viewMode === 'list' && (
          <button
            onClick={handleCreateClick}
            className="inline-flex items-center gap-2 px-4 py-2
                       bg-gold-600 hover:bg-gold-500 text-dark-900 font-medium
                       rounded transition-colors"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            Add Backend
          </button>
        )}
      </div>

      {/* Success Message */}
      {success && (
        <div className="mb-6 p-4 bg-green-900/30 border border-green-700 rounded-lg flex items-center gap-3">
          <svg className="h-5 w-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span className="text-green-300">{success}</span>
        </div>
      )}

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

      {/* Content */}
      <div className="bg-dark-900 rounded-lg border border-dark-700 p-6">
        {viewMode === 'list' ? renderList() : renderForm()}
      </div>
    </div>
  );
};

export default StorageSettings;
