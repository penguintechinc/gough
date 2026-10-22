/**
 * Capacity Page
 *
 * Forecast charts (1d/7d/30d), risk heatmap, migration events table,
 * migration-policy editor; license-gated banner if WaddleAI returns 402.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../../lib/api';
import { CapacityForecast, MigrationPolicy } from '../../types/provisioning';
import LicenseGatedFeature from '../../components/Provisioning/LicenseGatedFeature';

interface MigrationEvent {
  id: string;
  source_node: string;
  target_node: string;
  egg_name: string;
  started_at: string;
  completed_at?: string;
  status: 'in-progress' | 'success' | 'failed';
  error?: string;
}

type Horizon = '1d' | '7d' | '30d';

export const CapacityPage: React.FC = () => {
  const navigate = useNavigate();
  const [forecast, setForecast] = useState<CapacityForecast | null>(null);
  const [policy, setPolicy] = useState<MigrationPolicy | null>(null);
  const [migrations, setMigrations] = useState<MigrationEvent[]>([]);
  const [horizon, setHorizon] = useState<Horizon>('7d');
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [waddleAILicensed, setWaddleAILicensed] = useState(true);
  const [licenseError, setLicenseError] = useState<string | null>(null);
  const [licenseHttpStatus, setLicenseHttpStatus] = useState<number | undefined>();

  const fetchCapacityData = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const forecastRes = await api.get(`/provisioning/capacity/forecast?horizon=${horizon.replace('d', '')}d`);
      setForecast(forecastRes.data);

      const policyRes = await api.get('/provisioning/capacity/migration-policy');
      setPolicy(policyRes.data);

      const migrationsRes = await api.get('/provisioning/capacity/migrations');
      setMigrations(migrationsRes.data.migrations || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch capacity data';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [horizon]);

  const checkWaddleAILicense = useCallback(async () => {
    try {
      await api.get('/license/check?feature=waddleai');
      setWaddleAILicensed(true);
      setLicenseError(null);
    } catch (err: any) {
      const status = err.response?.status;
      setLicenseHttpStatus(status);
      if (status === 402) {
        setWaddleAILicensed(false);
        setLicenseError(err.response?.data?.message || 'WaddleAI license not available');
      } else {
        setWaddleAILicensed(false);
        setLicenseError('Unable to verify license');
      }
    }
  }, []);

  useEffect(() => {
    checkWaddleAILicense();
    fetchCapacityData();
  }, [checkWaddleAILicense, fetchCapacityData]);

  const handlePolicyChange = async (field: keyof MigrationPolicy, value: number) => {
    if (!policy) return;

    const newPolicy = { ...policy, [field]: value };

    try {
      await api.put('/provisioning/capacity/migration-policy', newPolicy);
      setPolicy(newPolicy);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to update policy';
      setError(message);
    }
  };

  const getHorizonColor = (percent: number) => {
    if (percent >= 95) return 'bg-red-600';
    if (percent >= 80) return 'bg-yellow-600';
    if (percent >= 60) return 'bg-amber-600';
    return 'bg-green-600';
  };

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/provisioning')}
            className="p-2 hover:bg-dark-800 rounded transition-colors"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
          <h1 className="text-2xl font-bold text-gold-500">Capacity Planning</h1>
        </div>
      </div>

      {error && (
        <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg text-red-300">
          {error}
        </div>
      )}

      <LicenseGatedFeature
        featureName="WaddleAI Capacity Forecasting"
        licensed={waddleAILicensed}
        licenseError={licenseError}
        httpStatus={licenseHttpStatus}
      >
        <div className="space-y-6">
          <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-semibold text-white">Forecast</h2>
              <div className="flex gap-2">
                {(['1d', '7d', '30d'] as const).map((h) => (
                  <button
                    key={h}
                    onClick={() => setHorizon(h)}
                    className={`px-4 py-2 rounded font-medium transition-colors ${
                      horizon === h
                        ? 'bg-gold-600 text-white'
                        : 'bg-dark-800 text-dark-300 hover:bg-dark-700'
                    }`}
                  >
                    {h === '1d' ? '24h' : h === '7d' ? '7d' : '30d'}
                  </button>
                ))}
              </div>
            </div>

            {isLoading ? (
              <div className="flex items-center justify-center h-40">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
              </div>
            ) : forecast ? (
              <div className="space-y-4">
                {forecast.nodes.map((node) => (
                  <div key={node.node_id} className="bg-dark-800 rounded p-4">
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="font-medium text-white">{node.hostname}</h4>
                      <span className={`px-2 py-1 text-xs rounded font-medium ${
                        node.memory_percent_used >= 95 ? 'bg-red-900/30 text-red-300' :
                        node.memory_percent_used >= 80 ? 'bg-yellow-900/30 text-yellow-300' :
                        'bg-green-900/30 text-green-300'
                      }`}>
                        {Math.round(node.memory_percent_used)}% used
                      </span>
                    </div>

                    <div className="grid grid-cols-3 gap-4">
                      <div>
                        <p className="text-xs text-dark-400 mb-1">Current</p>
                        <div className="h-2 bg-dark-700 rounded overflow-hidden">
                          <div
                            className="h-full bg-gold-600"
                            style={{ width: `${Math.min(node.memory_percent_used, 100)}%` }}
                          />
                        </div>
                      </div>
                      <div>
                        <p className="text-xs text-dark-400 mb-1">Forecast {horizon}</p>
                        <div className="h-2 bg-dark-700 rounded overflow-hidden">
                          <div
                            className={`h-full ${getHorizonColor(node.memory_forecast_7d)}`}
                            style={{ width: `${Math.min(node.memory_forecast_7d, 100)}%` }}
                          />
                        </div>
                      </div>
                      <div>
                        <p className="text-xs text-dark-400 mb-1">CPU</p>
                        <div className="h-2 bg-dark-700 rounded overflow-hidden">
                          <div
                            className={`h-full ${getHorizonColor(node.cpu_percent_used)}`}
                            style={{ width: `${Math.min(node.cpu_percent_used, 100)}%` }}
                          />
                        </div>
                      </div>
                    </div>

                    {node.risk_indicators.length > 0 && (
                      <div className="mt-2 text-xs text-yellow-400">
                        ⚠️ {node.risk_indicators.join(', ')}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-dark-400">No forecast data available</p>
            )}
          </div>

          <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
            <h2 className="text-xl font-semibold text-white mb-4">Recent Migrations</h2>

            {migrations.length === 0 ? (
              <p className="text-dark-400">No migrations</p>
            ) : (
              <div className="space-y-2">
                {migrations.map((mig) => (
                  <div key={mig.id} className="px-3 py-2 bg-dark-800 border border-dark-700 rounded flex items-center justify-between text-sm">
                    <div className="flex-1">
                      <p className="text-white font-medium">{mig.egg_name}</p>
                      <p className="text-dark-400 text-xs">
                        {mig.source_node} → {mig.target_node}
                      </p>
                    </div>
                    <div className={`px-2 py-1 rounded text-xs font-medium ${
                      mig.status === 'success' ? 'bg-green-900/30 text-green-300' :
                      mig.status === 'failed' ? 'bg-red-900/30 text-red-300' :
                      'bg-yellow-900/30 text-yellow-300'
                    }`}>
                      {mig.status}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
            <h2 className="text-xl font-semibold text-white mb-4">Migration Policy</h2>

            {policy ? (
              <div className="space-y-4">
                <div>
                  <label className="block text-sm text-dark-400 mb-1">Minimum Healthy Nodes</label>
                  <input
                    type="number"
                    value={policy.min_healthy_nodes}
                    onChange={(e) => handlePolicyChange('min_healthy_nodes', parseInt(e.target.value))}
                    className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
                  />
                  <p className="text-xs text-dark-400 mt-1">Minimum nodes that must remain healthy</p>
                </div>

                <div>
                  <label className="block text-sm text-dark-400 mb-1">Max Concurrent Migrations</label>
                  <input
                    type="number"
                    value={policy.max_concurrent_migrations}
                    onChange={(e) => handlePolicyChange('max_concurrent_migrations', parseInt(e.target.value))}
                    className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
                  />
                  <p className="text-xs text-dark-400 mt-1">Maximum simultaneous migrations allowed</p>
                </div>

                <div>
                  <label className="block text-sm text-dark-400 mb-1">Target Capacity Headroom (Memory %)</label>
                  <input
                    type="number"
                    value={policy.require_target_capacity_headroom_mem_pct}
                    onChange={(e) => handlePolicyChange('require_target_capacity_headroom_mem_pct', parseInt(e.target.value))}
                    className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
                  />
                  <p className="text-xs text-dark-400 mt-1">Minimum free memory on target node after migration</p>
                </div>

                <div>
                  <label className="block text-sm text-dark-400 mb-1">Target Capacity Headroom (CPU %)</label>
                  <input
                    type="number"
                    value={policy.require_target_capacity_headroom_cpu_pct}
                    onChange={(e) => handlePolicyChange('require_target_capacity_headroom_cpu_pct', parseInt(e.target.value))}
                    className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
                  />
                  <p className="text-xs text-dark-400 mt-1">Minimum free CPU on target node after migration</p>
                </div>
              </div>
            ) : (
              <p className="text-dark-400">No policy configured</p>
            )}
          </div>
        </div>
      </LicenseGatedFeature>
    </div>
  );
};

export default CapacityPage;
