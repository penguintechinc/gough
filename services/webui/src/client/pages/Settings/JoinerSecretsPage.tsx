/**
 * Joiner Secrets Page
 *
 * List metadata (NEVER ciphertext), filters, rotate/revoke/ascertain buttons.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../../lib/api';
import { JoinerSecret } from '../../types/provisioning';
import { useAuth } from '../../hooks/useAuth';

export const JoinerSecretsPage: React.FC = () => {
  const navigate = useNavigate();
  const { user } = useAuth();
  const [secrets, setSecrets] = useState<JoinerSecret[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedSecretId, setSelectedSecretId] = useState<string | null>(null);
  const [actionInProgress, setActionInProgress] = useState<string | null>(null);

  const [filters, setFilters] = useState({
    rotation_class: '',
    expired: 'all' as 'all' | 'active' | 'expired',
  });

  const fetchSecrets = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const response = await api.get('/settings/joiner-secrets');
      setSecrets(response.data.secrets || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch joiner secrets';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSecrets();
  }, [fetchSecrets]);

  const filteredSecrets = secrets.filter((secret) => {
    if (filters.rotation_class && secret.rotation_class !== filters.rotation_class) return false;

    if (filters.expired === 'expired') {
      return secret.expires_at && new Date(secret.expires_at) < new Date();
    } else if (filters.expired === 'active') {
      return !secret.expires_at || new Date(secret.expires_at) >= new Date();
    }

    return true;
  });

  const rotationClasses = [...new Set(secrets.map((s) => s.rotation_class))];

  const handleRotate = async (secretId: string) => {
    if (!confirm('Rotate this joiner secret? Old token will be invalidated.')) return;

    setActionInProgress(secretId);
    try {
      await api.post(`/settings/joiner-secrets/${secretId}/rotate`);
      await fetchSecrets();
      setSelectedSecretId(null);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to rotate secret';
      setError(message);
    } finally {
      setActionInProgress(null);
    }
  };

  const handleRevoke = async (secretId: string) => {
    if (!confirm('Revoke this joiner secret? This cannot be undone.')) return;

    setActionInProgress(secretId);
    try {
      await api.post(`/settings/joiner-secrets/${secretId}/revoke`);
      await fetchSecrets();
      setSelectedSecretId(null);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to revoke secret';
      setError(message);
    } finally {
      setActionInProgress(null);
    }
  };

  const handleAscertain = async (secretId: string) => {
    setActionInProgress(secretId);
    try {
      const response = await api.post(`/settings/joiner-secrets/${secretId}/ascertain`);
      // Show ascertainment result
      alert(`Ascertainment result:\n${response.data.message || 'Ascertainment complete'}`);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to ascertain secret';
      setError(message);
    } finally {
      setActionInProgress(null);
    }
  };

  const isSuperAdmin = user?.role === 'Admin' && user?.scope?.includes('admin');

  const isExpired = (secret: JoinerSecret) => {
    return secret.expires_at && new Date(secret.expires_at) < new Date();
  };

  const daysUntilExpiration = (secret: JoinerSecret) => {
    if (!secret.expires_at) return null;

    const now = new Date();
    const expiration = new Date(secret.expires_at);
    const days = Math.ceil((expiration.getTime() - now.getTime()) / (1000 * 60 * 60 * 24));
    return days;
  };

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/settings')}
            className="p-2 hover:bg-dark-800 rounded transition-colors"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
          <h1 className="text-2xl font-bold text-gold-500">Joiner Secrets</h1>
          <span className="px-3 py-1 bg-dark-800 text-dark-300 rounded text-sm">
            {filteredSecrets.length} active
          </span>
        </div>
      </div>

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

      <div className="grid grid-cols-3 gap-6">
        <div className="col-span-2">
          <div className="bg-dark-900 border border-dark-700 rounded-lg p-4 mb-6">
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <label className="block text-sm text-dark-400 mb-1">Rotation Class</label>
                <select
                  value={filters.rotation_class}
                  onChange={(e) => setFilters({ ...filters, rotation_class: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
                >
                  <option value="">All Classes</option>
                  {rotationClasses.map((rc) => (
                    <option key={rc} value={rc}>
                      {rc}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm text-dark-400 mb-1">Status</label>
                <select
                  value={filters.expired}
                  onChange={(e) => setFilters({ ...filters, expired: e.target.value as any })}
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
                >
                  <option value="all">All</option>
                  <option value="active">Active</option>
                  <option value="expired">Expired</option>
                </select>
              </div>
            </div>
          </div>

          {isLoading ? (
            <div className="flex items-center justify-center h-64">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
            </div>
          ) : filteredSecrets.length === 0 ? (
            <div className="text-center py-12 text-dark-400 bg-dark-900 border border-dark-700 rounded-lg">
              <p className="text-lg">No joiner secrets found</p>
            </div>
          ) : (
            <div className="space-y-3">
              {filteredSecrets.map((secret) => {
                const expired = isExpired(secret);
                const daysLeft = daysUntilExpiration(secret);

                return (
                  <div
                    key={secret.id}
                    onClick={() => setSelectedSecretId(secret.id)}
                    className={`p-4 rounded-lg border cursor-pointer transition-all ${
                      selectedSecretId === secret.id
                        ? 'bg-dark-800 border-gold-600'
                        : 'bg-dark-900 border-dark-700 hover:border-dark-600'
                    }`}
                  >
                    <div className="flex items-start justify-between mb-2">
                      <h4 className="font-semibold text-white">{secret.name}</h4>
                      <span className={`px-2 py-0.5 text-xs rounded ${
                        expired ? 'bg-red-900/30 text-red-300' :
                        daysLeft !== null && daysLeft < 7 ? 'bg-yellow-900/30 text-yellow-300' :
                        'bg-green-900/30 text-green-300'
                      }`}>
                        {expired ? 'Expired' : daysLeft !== null ? `${daysLeft}d left` : 'No expiry'}
                      </span>
                    </div>

                    <p className="text-sm text-dark-400">
                      Class: <span className="text-dark-300">{secret.rotation_class}</span>
                    </p>
                    <p className="text-xs text-dark-500 mt-1">
                      Issued: {new Date(secret.issued_at).toLocaleString()}
                    </p>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div>
          {selectedSecretId ? (
            <div className="bg-dark-900 border border-dark-700 rounded-lg overflow-hidden sticky top-6">
              <div className="bg-dark-800 px-4 py-3 border-b border-dark-700">
                <h3 className="font-semibold text-white">Actions</h3>
              </div>

              <div className="p-4 space-y-3">
                <button
                  onClick={() => handleRotate(selectedSecretId)}
                  disabled={actionInProgress === selectedSecretId}
                  className="w-full px-4 py-2 bg-amber-600 hover:bg-amber-500 disabled:bg-dark-700 text-white font-medium rounded transition-colors text-sm"
                >
                  {actionInProgress === selectedSecretId ? 'Rotating...' : '🔄 Rotate'}
                </button>

                <button
                  onClick={() => handleRevoke(selectedSecretId)}
                  disabled={actionInProgress === selectedSecretId}
                  className="w-full px-4 py-2 bg-red-900/30 hover:bg-red-900/50 disabled:bg-dark-700 text-red-300 font-medium rounded transition-colors text-sm border border-red-700"
                >
                  {actionInProgress === selectedSecretId ? 'Revoking...' : '🗑️ Revoke'}
                </button>

                {isSuperAdmin && (
                  <button
                    onClick={() => handleAscertain(selectedSecretId)}
                    disabled={actionInProgress === selectedSecretId}
                    className="w-full px-4 py-2 bg-blue-900/30 hover:bg-blue-900/50 disabled:bg-dark-700 text-blue-300 font-medium rounded transition-colors text-sm border border-blue-700"
                  >
                    {actionInProgress === selectedSecretId ? 'Ascertaining...' : '✓ Ascertain'}
                  </button>
                )}
              </div>
            </div>
          ) : (
            <div className="bg-dark-900 border border-dark-700 rounded-lg p-6 text-center text-dark-400 sticky top-6">
              <svg className="h-12 w-12 mx-auto mb-3 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
              </svg>
              <p className="text-sm">Select a secret to manage</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default JoinerSecretsPage;
