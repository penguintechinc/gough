/**
 * Biome Detail Page
 *
 * Tabs: Overview, Versions, Dependencies (D3 DAG), Requirements, Joiner Secrets, Tests, Audit.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../../lib/api';
import { Biome } from '../../types/provisioning';
import EligibilityCheck from '../../components/Provisioning/EligibilityCheck';

interface BiomeDetail extends Biome {
  versions: string[];
  dependencies: Array<{ name: string; version: string; required: boolean }>;
  requirements: Array<{ name: string; description: string }>;
  joiner_secrets: Array<{ name: string; rotation_class: string }>;
  tests: Array<{ name: string; description: string; status: 'pass' | 'fail' | 'pending' }>;
  audit_events: Array<{ timestamp: string; action: string; actor_id: string }>;
}

type Tab = 'overview' | 'versions' | 'dependencies' | 'requirements' | 'secrets' | 'tests' | 'audit';

export const BiomeDetail: React.FC = () => {
  const { biomeId } = useParams<{ biomeId: string }>();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  const [biome, setEgg] = useState<BiomeDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchBiomeDetail = useCallback(async () => {
    if (!biomeId) return;

    setIsLoading(true);
    setError(null);

    try {
      const response = await api.get(`/provisioning/biomes/${biomeId}`);
      setEgg(response.data);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch biome details';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [biomeId]);

  useEffect(() => {
    fetchBiomeDetail();
  }, [fetchBiomeDetail]);

  const getKindColor = (kind: string) => {
    switch (kind) {
      case 'compute':
        return 'bg-blue-900/30 border-blue-700 text-blue-300';
      case 'storage':
        return 'bg-purple-900/30 border-purple-700 text-purple-300';
      case 'infrastructure':
        return 'bg-orange-900/30 border-orange-700 text-orange-300';
      default:
        return 'bg-dark-700 border-dark-600 text-dark-300';
    }
  };

  const tabs: { id: Tab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'versions', label: 'Versions' },
    { id: 'dependencies', label: 'Dependencies' },
    { id: 'requirements', label: 'Requirements' },
    { id: 'secrets', label: 'Joiner Secrets' },
    { id: 'tests', label: 'Tests' },
    { id: 'audit', label: 'Audit' },
  ];

  if (isLoading) {
    return (
      <div className="min-h-screen bg-dark-950 text-white p-6 flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
      </div>
    );
  }

  if (error || !biome) {
    return (
      <div className="min-h-screen bg-dark-950 text-white p-6">
        <button
          onClick={() => navigate('/provisioning/biomes')}
          className="mb-4 p-2 hover:bg-dark-800 rounded transition-colors inline-flex items-center gap-2 text-gold-500"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Biomes
        </button>
        <div className="mt-6 p-4 bg-red-900/30 border border-red-700 rounded-lg text-red-300">
          {error || 'Biome not found'}
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      <button
        onClick={() => navigate('/provisioning/biomes')}
        className="mb-4 p-2 hover:bg-dark-800 rounded transition-colors inline-flex items-center gap-2 text-gold-500"
      >
        <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
        </svg>
        Back to Biomes
      </button>

      <div className="mt-6 bg-dark-900 border border-dark-700 rounded-lg overflow-hidden">
        <div className="bg-dark-800 p-6 border-b border-dark-700">
          <h1 className="text-3xl font-bold text-white mb-2">{biome.name}</h1>
          <p className="text-dark-400">{biome.version}</p>

          {biome.description && (
            <p className="mt-4 text-dark-200">{biome.description}</p>
          )}

          <div className="flex flex-wrap gap-2 mt-4">
            <span className={`px-3 py-1 text-sm rounded border ${getKindColor(biome.kind)}`}>
              {biome.kind}
            </span>
            <span className="px-3 py-1 text-sm rounded border bg-dark-700 border-dark-600 text-dark-300">
              {biome.phase}
            </span>
            <span className="px-3 py-1 text-sm rounded border bg-dark-700 border-dark-600 text-dark-300">
              {biome.workload_type}
            </span>
            {biome.lock_to_host && (
              <span className="px-3 py-1 text-sm rounded border bg-red-900/30 border-red-700 text-red-300">
                🔒 Locked to host
              </span>
            )}
            {biome.emits_joiner_secrets && (
              <span className="px-3 py-1 text-sm rounded border bg-green-900/30 border-green-700 text-green-300">
                🔑 Emits secrets
              </span>
            )}
          </div>
        </div>

        <div className="border-b border-dark-700">
          <div className="flex flex-wrap">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`px-4 py-3 font-medium text-sm transition-colors border-b-2 ${
                  activeTab === tab.id
                    ? 'border-b-gold-500 text-gold-400'
                    : 'border-b-transparent text-dark-400 hover:text-dark-200'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        <div className="p-6">
          {activeTab === 'overview' && (
            <div className="space-y-4">
              <div>
                <h3 className="text-lg font-semibold text-white mb-2">Info</h3>
                <dl className="grid grid-cols-2 gap-4">
                  <div>
                    <dt className="text-sm text-dark-400">ID</dt>
                    <dd className="text-white font-mono">{biome.id}</dd>
                  </div>
                  <div>
                    <dt className="text-sm text-dark-400">Created</dt>
                    <dd className="text-white">{new Date(biome.created_at).toLocaleString()}</dd>
                  </div>
                </dl>
              </div>

              {biome.requires_hardware_tags.length > 0 && (
                <div>
                  <h3 className="text-lg font-semibold text-white mb-2">Required Hardware Tags</h3>
                  <div className="flex flex-wrap gap-2">
                    {biome.requires_hardware_tags.map((tag) => (
                      <span key={tag} className="px-2 py-1 bg-dark-800 border border-dark-700 text-dark-300 rounded text-sm">
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {activeTab === 'versions' && (
            <div>
              <h3 className="text-lg font-semibold text-white mb-4">Available Versions</h3>
              <div className="space-y-2">
                {biome.versions.map((v) => (
                  <div key={v} className="px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white font-mono text-sm">
                    {v}
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === 'dependencies' && (
            <div>
              <h3 className="text-lg font-semibold text-white mb-4">Dependencies</h3>
              <div className="space-y-2">
                {biome.dependencies.map((dep) => (
                  <div key={`${dep.name}-${dep.version}`} className="px-3 py-2 bg-dark-800 border border-dark-700 rounded flex items-center justify-between">
                    <div>
                      <p className="text-white font-medium">{dep.name}</p>
                      <p className="text-dark-400 text-sm font-mono">{dep.version}</p>
                    </div>
                    {!dep.required && (
                      <span className="text-xs text-yellow-400 bg-yellow-900/30 border border-yellow-700 px-2 py-1 rounded">
                        Optional
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === 'requirements' && (
            <EligibilityCheck
              requirements={biome.requirements.map((r) => ({
                name: r.name,
                satisfied: true,
                reason: r.description,
              }))}
              title="Requirements"
            />
          )}

          {activeTab === 'secrets' && (
            <div>
              <h3 className="text-lg font-semibold text-white mb-4">Joiner Secrets</h3>
              {biome.joiner_secrets.length === 0 ? (
                <p className="text-dark-400">No joiner secrets</p>
              ) : (
                <div className="space-y-2">
                  {biome.joiner_secrets.map((secret) => (
                    <div key={secret.name} className="px-3 py-2 bg-dark-800 border border-dark-700 rounded">
                      <p className="text-white font-medium">{secret.name}</p>
                      <p className="text-dark-400 text-sm">Rotation: {secret.rotation_class}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {activeTab === 'tests' && (
            <div>
              <h3 className="text-lg font-semibold text-white mb-4">Tests</h3>
              <div className="space-y-2">
                {biome.tests.map((test) => (
                  <div key={test.name} className="px-3 py-2 bg-dark-800 border border-dark-700 rounded flex items-center justify-between">
                    <div className="flex-1">
                      <p className="text-white font-medium">{test.name}</p>
                      <p className="text-dark-400 text-sm">{test.description}</p>
                    </div>
                    <div className={`px-2 py-1 rounded text-xs font-medium ${
                      test.status === 'pass' ? 'bg-green-900/30 text-green-300' :
                      test.status === 'fail' ? 'bg-red-900/30 text-red-300' :
                      'bg-yellow-900/30 text-yellow-300'
                    }`}>
                      {test.status}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === 'audit' && (
            <div>
              <h3 className="text-lg font-semibold text-white mb-4">Audit Trail</h3>
              <div className="space-y-2">
                {biome.audit_events.map((event, idx) => (
                  <div key={idx} className="px-3 py-2 bg-dark-800 border border-dark-700 rounded text-sm">
                    <p className="text-dark-400">{new Date(event.timestamp).toLocaleString()}</p>
                    <p className="text-white font-medium">{event.action}</p>
                    <p className="text-dark-400 text-xs">{event.actor_id}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default BiomeDetail;
