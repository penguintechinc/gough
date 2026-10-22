/**
 * Machine Detail Page
 *
 * Single machine view with tabs for Info, Hardware, Biomes, and Logs.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../../lib/api';

interface Machine {
  id: string;
  hostname: string;
  state: string;
  zone: string;
  pool: string;
  ip_address: string;
  mac_address?: string;
  created_at: string;
  last_seen?: string;
  egg_name?: string;
  deployed_at?: string;
  metadata?: Record<string, any>;
}

interface ControlPlaneFrontendParams {
  mode: 'kube-vip' | 'external' | 'none';
  endpoint?: string;
  interface?: string;
  network_baseline?: 'mgmt' | 'internal' | 'external';
}

interface HardwareInfo {
  cpu_model: string;
  cpu_cores: number;
  memory_gb: number;
  disk_gb: number;
  network_interfaces: Array<{
    name: string;
    mac: string;
    speed: string;
  }>;
  pci_devices?: Array<{
    id: string;
    vendor: string;
    device: string;
  }>;
}

interface EggDeployment {
  id: number;
  egg_name: string;
  egg_version: string;
  deployed_at: string;
  state: string;
  duration_seconds?: number;
  error_message?: string;
}

interface LogEntry {
  id: number;
  timestamp: string;
  level: string;
  message: string;
  source?: string;
}

type Tab = 'info' | 'hardware' | 'biomes' | 'logs';

export const MachineDetail: React.FC = () => {
  const { machineId } = useParams<{ machineId: string }>();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<Tab>('info');
  const [machine, setMachine] = useState<Machine | null>(null);
  const [hardware, setHardware] = useState<HardwareInfo | null>(null);
  const [biomes, setEggs] = useState<EggDeployment[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Deploy modal state
  const [showDeployModal, setShowDeployModal] = useState(false);
  const [selectedBiome, setSelectedBiome] = useState<string>('');
  const [frontendMode, setFrontendMode] = useState<'kube-vip' | 'external' | 'none'>('kube-vip');
  const [frontendEndpoint, setFrontendEndpoint] = useState('');
  const [frontendInterface, setFrontendInterface] = useState('');
  const [frontendBaseline, setFrontendBaseline] = useState<'mgmt' | 'internal' | 'external'>('external');
  const [isDeploying, setIsDeploying] = useState(false);
  const [deployError, setDeployError] = useState<string | null>(null);

  const fetchMachineData = useCallback(async () => {
    if (!machineId) return;

    setIsLoading(true);
    setError(null);

    try {
      const machineRes = await api.get(`/provisioning/machines/${machineId}`);
      setMachine(machineRes.data);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch machine data';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [machineId]);

  const fetchHardware = useCallback(async () => {
    if (!machineId) return;

    try {
      const response = await api.get(`/provisioning/machines/${machineId}/hardware`);
      setHardware(response.data);
    } catch (err: any) {
      console.error('Failed to fetch hardware:', err);
    }
  }, [machineId]);

  const fetchEggs = useCallback(async () => {
    if (!machineId) return;

    try {
      const response = await api.get(`/provisioning/machines/${machineId}/biomes`);
      setEggs(response.data.biomes || []);
    } catch (err: any) {
      console.error('Failed to fetch biomes:', err);
    }
  }, [machineId]);

  const fetchLogs = useCallback(async () => {
    if (!machineId) return;

    try {
      const response = await api.get(`/provisioning/machines/${machineId}/logs`);
      setLogs(response.data.logs || []);
    } catch (err: any) {
      console.error('Failed to fetch logs:', err);
    }
  }, [machineId]);

  useEffect(() => {
    fetchMachineData();
  }, [fetchMachineData]);

  useEffect(() => {
    if (activeTab === 'hardware' && !hardware) {
      fetchHardware();
    } else if (activeTab === 'biomes' && biomes.length === 0) {
      fetchEggs();
    } else if (activeTab === 'logs' && logs.length === 0) {
      fetchLogs();
    }
  }, [activeTab, hardware, biomes.length, logs.length, fetchHardware, fetchEggs, fetchLogs]);

  const handleMachineAction = async (action: string) => {
    if (!machineId) return;
    if (!confirm(`${action} this machine?`)) return;

    try {
      await api.post(`/provisioning/machines/${machineId}/action`, { action });
      await fetchMachineData();
    } catch (err: any) {
      const message = err.response?.data?.error || `Failed to ${action} machine`;
      setError(message);
    }
  };

  const validateDeployForm = (): boolean => {
    if (!selectedBiome) {
      setDeployError('Please select a biome');
      return false;
    }
    if (frontendMode !== 'none' && !frontendEndpoint) {
      setDeployError('Endpoint is required for kube-vip and external modes');
      return false;
    }
    if (frontendEndpoint && !frontendEndpoint.includes(':')) {
      setDeployError('Endpoint must include port (e.g. 10.2.0.10:6443)');
      return false;
    }
    return true;
  };

  const handleDeploy = async () => {
    if (!machineId || !validateDeployForm()) return;

    setIsDeploying(true);
    setDeployError(null);

    try {
      const params: Record<string, unknown> = {};
      if (selectedBiome === 'k8s-primary') {
        const cpfParams: ControlPlaneFrontendParams = {
          mode: frontendMode,
        };
        if (frontendMode !== 'none') {
          cpfParams.endpoint = frontendEndpoint;
          cpfParams.network_baseline = frontendBaseline;
        }
        if (frontendMode === 'kube-vip' && frontendInterface) {
          cpfParams.interface = frontendInterface;
        }
        params.control_plane_frontend = cpfParams;
      }

      const deployPayload = {
        biome_id: selectedBiome,
        params,
      };

      console.log('[K8sPrimaryDeploy] Submit { mode: "' + frontendMode + '", endpoint: "<masked>", baseline: "' + frontendBaseline + '" }');

      await api.post(`/provisioning/machines/${machineId}/deploy`, deployPayload);

      setShowDeployModal(false);
      setSelectedBiome('');
      setFrontendMode('kube-vip');
      setFrontendEndpoint('');
      setFrontendInterface('');
      setFrontendBaseline('external');

      await fetchEggs();
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to deploy biome';
      setDeployError(message);
    } finally {
      setIsDeploying(false);
    }
  };

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
      case 'success':
        return 'text-green-400 bg-green-900/30 border-green-700';
      case 'retired':
        return 'text-gray-400 bg-gray-900/30 border-gray-700';
      default:
        return 'text-dark-400 bg-dark-800 border-dark-700';
    }
  };

  const getLogLevelColor = (level: string) => {
    switch (level.toLowerCase()) {
      case 'error':
        return 'text-red-400';
      case 'warning':
        return 'text-gold-400';
      case 'info':
        return 'text-blue-400';
      case 'debug':
        return 'text-dark-400';
      default:
        return 'text-white';
    }
  };

  if (isLoading && !machine) {
    return (
      <div className="min-h-screen bg-dark-950 text-white p-6">
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
        </div>
      </div>
    );
  }

  if (!machine) {
    return (
      <div className="min-h-screen bg-dark-950 text-white p-6">
        <div className="text-center py-12">
          <p className="text-red-400">Machine not found</p>
          <button
            onClick={() => navigate('/provisioning/machines')}
            className="mt-4 text-gold-500 hover:text-gold-400"
          >
            Back to Machines
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/provisioning/machines')}
            className="p-2 hover:bg-dark-800 rounded transition-colors"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h1 className="text-2xl font-bold text-gold-500">{machine.hostname}</h1>
            <p className="text-sm text-dark-400 font-mono">{machine.id}</p>
          </div>
          <span className={`inline-flex items-center gap-1 px-3 py-1 text-sm rounded border ${getStateColor(machine.state)}`}>
            {machine.state}
          </span>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          {machine.state === 'ready' && (
            <button
              onClick={() => handleMachineAction('allocate')}
              className="px-3 py-1.5 bg-gold-600 hover:bg-gold-500 text-dark-900 rounded text-sm transition-colors"
            >
              Allocate
            </button>
          )}
          {machine.state === 'failed' && (
            <button
              onClick={() => handleMachineAction('retry')}
              className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded text-sm transition-colors"
            >
              Retry
            </button>
          )}
          <button
            onClick={() => handleMachineAction('reset')}
            className="px-3 py-1.5 bg-dark-700 hover:bg-dark-600 text-white rounded text-sm transition-colors"
          >
            Reset
          </button>
          <button
            onClick={() => handleMachineAction('retire')}
            className="px-3 py-1.5 bg-red-900/30 hover:bg-red-900/50 text-red-400 rounded text-sm transition-colors"
          >
            Retire
          </button>
        </div>
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

      {/* Tabs */}
      <div className="flex border-b border-dark-700 mb-6">
        {[
          { id: 'info', label: 'Information' },
          { id: 'hardware', label: 'Hardware' },
          { id: 'biomes', label: 'Biomes' },
          { id: 'logs', label: 'Logs' },
        ].map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id as Tab)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab.id
                ? 'border-gold-500 text-gold-500'
                : 'border-transparent text-dark-400 hover:text-white'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
        {/* Info Tab */}
        {activeTab === 'info' && (
          <div className="grid gap-6 md:grid-cols-2">
            <div>
              <h3 className="text-lg font-semibold text-gold-500 mb-4">Basic Information</h3>
              <dl className="space-y-3">
                <div>
                  <dt className="text-sm text-dark-400">Machine ID</dt>
                  <dd className="text-white font-mono">{machine.id}</dd>
                </div>
                <div>
                  <dt className="text-sm text-dark-400">Hostname</dt>
                  <dd className="text-white">{machine.hostname}</dd>
                </div>
                <div>
                  <dt className="text-sm text-dark-400">State</dt>
                  <dd><span className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded border ${getStateColor(machine.state)}`}>{machine.state}</span></dd>
                </div>
                <div>
                  <dt className="text-sm text-dark-400">IP Address</dt>
                  <dd className="text-white font-mono">{machine.ip_address}</dd>
                </div>
                {machine.mac_address && (
                  <div>
                    <dt className="text-sm text-dark-400">MAC Address</dt>
                    <dd className="text-white font-mono">{machine.mac_address}</dd>
                  </div>
                )}
              </dl>
            </div>
            <div>
              <h3 className="text-lg font-semibold text-gold-500 mb-4">Location & Pool</h3>
              <dl className="space-y-3">
                <div>
                  <dt className="text-sm text-dark-400">Zone</dt>
                  <dd className="text-white">{machine.zone}</dd>
                </div>
                <div>
                  <dt className="text-sm text-dark-400">Pool</dt>
                  <dd className="text-white">{machine.pool}</dd>
                </div>
                <div>
                  <dt className="text-sm text-dark-400">Created</dt>
                  <dd className="text-white">{new Date(machine.created_at).toLocaleString()}</dd>
                </div>
                {machine.last_seen && (
                  <div>
                    <dt className="text-sm text-dark-400">Last Seen</dt>
                    <dd className="text-white">{new Date(machine.last_seen).toLocaleString()}</dd>
                  </div>
                )}
                {machine.deployed_at && (
                  <div>
                    <dt className="text-sm text-dark-400">Deployed</dt>
                    <dd className="text-white">{new Date(machine.deployed_at).toLocaleString()}</dd>
                  </div>
                )}
              </dl>
            </div>
            {machine.metadata && Object.keys(machine.metadata).length > 0 && (
              <div className="md:col-span-2">
                <h3 className="text-lg font-semibold text-gold-500 mb-4">Metadata</h3>
                <div className="bg-dark-800 rounded p-4">
                  <pre className="text-sm text-dark-300 overflow-x-auto">
                    {JSON.stringify(machine.metadata, null, 2)}
                  </pre>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Hardware Tab */}
        {activeTab === 'hardware' && (
          <div>
            {hardware ? (
              <div className="space-y-6">
                <div>
                  <h3 className="text-lg font-semibold text-gold-500 mb-4">CPU & Memory</h3>
                  <dl className="space-y-3">
                    <div>
                      <dt className="text-sm text-dark-400">CPU Model</dt>
                      <dd className="text-white">{hardware.cpu_model}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-dark-400">CPU Cores</dt>
                      <dd className="text-white">{hardware.cpu_cores}</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-dark-400">Memory</dt>
                      <dd className="text-white">{hardware.memory_gb} GB</dd>
                    </div>
                    <div>
                      <dt className="text-sm text-dark-400">Disk</dt>
                      <dd className="text-white">{hardware.disk_gb} GB</dd>
                    </div>
                  </dl>
                </div>
                <div>
                  <h3 className="text-lg font-semibold text-gold-500 mb-4">Network Interfaces</h3>
                  <div className="space-y-2">
                    {hardware.network_interfaces.map((iface, idx) => (
                      <div key={idx} className="p-3 bg-dark-800 rounded">
                        <div className="flex items-center justify-between">
                          <span className="text-white font-medium">{iface.name}</span>
                          <span className="text-sm text-dark-400">{iface.speed}</span>
                        </div>
                        <p className="text-sm text-dark-300 font-mono mt-1">{iface.mac}</p>
                      </div>
                    ))}
                  </div>
                </div>
                {hardware.pci_devices && hardware.pci_devices.length > 0 && (
                  <div>
                    <h3 className="text-lg font-semibold text-gold-500 mb-4">PCI Devices</h3>
                    <div className="space-y-2">
                      {hardware.pci_devices.map((device, idx) => (
                        <div key={idx} className="p-3 bg-dark-800 rounded">
                          <div className="text-white font-medium">{device.vendor} - {device.device}</div>
                          <p className="text-sm text-dark-300 font-mono mt-1">{device.id}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="text-center py-12 text-dark-400">
                <p>Loading hardware information...</p>
              </div>
            )}
          </div>
        )}

        {/* Biomes Tab */}
        {activeTab === 'biomes' && (
          <div>
            <div className="mb-6">
              <button
                onClick={() => setShowDeployModal(true)}
                className="px-4 py-2 bg-gold-600 hover:bg-gold-500 text-dark-900 rounded font-medium transition-colors"
              >
                Deploy Biome
              </button>
            </div>
            {biomes.length > 0 ? (
              <div className="space-y-3">
                {biomes.map((biome) => (
                  <div key={biome.id} className="p-4 bg-dark-800 rounded-lg">
                    <div className="flex items-center justify-between mb-2">
                      <div>
                        <h4 className="text-white font-medium">{biome.egg_name}</h4>
                        <p className="text-sm text-dark-400">Version {biome.egg_version}</p>
                      </div>
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded border ${getStateColor(biome.state)}`}>
                        {biome.state}
                      </span>
                    </div>
                    <div className="flex items-center gap-4 text-sm text-dark-400">
                      <span>Deployed: {new Date(biome.deployed_at).toLocaleString()}</span>
                      {biome.duration_seconds && <span>Duration: {biome.duration_seconds}s</span>}
                    </div>
                    {biome.error_message && (
                      <div className="mt-2 p-2 bg-red-900/20 border border-red-700 rounded text-sm text-red-300">
                        {biome.error_message}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-12 text-dark-400">
                <p>No biomes deployed on this machine</p>
              </div>
            )}
          </div>
        )}

        {/* Logs Tab */}
        {activeTab === 'logs' && (
          <div>
            {logs.length > 0 ? (
              <div className="space-y-2">
                {logs.map((log) => (
                  <div key={log.id} className="p-3 bg-dark-800 rounded font-mono text-sm">
                    <div className="flex items-center gap-3 mb-1">
                      <span className="text-dark-400">{new Date(log.timestamp).toLocaleString()}</span>
                      <span className={`uppercase font-semibold ${getLogLevelColor(log.level)}`}>{log.level}</span>
                      {log.source && <span className="text-dark-500">[{log.source}]</span>}
                    </div>
                    <p className="text-dark-200">{log.message}</p>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-12 text-dark-400">
                <p>No logs available</p>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Deploy Modal */}
      {showDeployModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-dark-900 border border-dark-700 rounded-lg max-w-md w-full p-6">
            <h2 className="text-xl font-bold text-gold-500 mb-4">Deploy Biome</h2>

            {deployError && (
              <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded text-sm text-red-300">
                {deployError}
              </div>
            )}

            {/* Biome Selection */}
            <div className="mb-4">
              <label className="block text-sm font-medium text-gold-400 mb-2">Biome</label>
              <select
                value={selectedBiome}
                onChange={(e) => setSelectedBiome(e.target.value)}
                className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:outline-none focus:ring-2 focus:ring-gold-500"
              >
                <option value="">Select a biome...</option>
                <option value="k8s-primary">k8s-primary</option>
                <option value="k8s-worker">k8s-worker</option>
                <option value="storage">storage</option>
              </select>
            </div>

            {/* k8s-primary Control Plane Frontend Picker */}
            {selectedBiome === 'k8s-primary' && (
              <div className="mb-4 p-4 bg-dark-800 rounded border border-dark-700">
                <label className="block text-sm font-medium text-gold-400 mb-3">Control Plane Frontend</label>

                <div className="space-y-3">
                  {/* kube-vip Option */}
                  <label className="flex items-start gap-3 cursor-pointer p-3 hover:bg-dark-700/50 rounded transition-colors" data-testid="frontend-mode-kube-vip">
                    <input
                      type="radio"
                      name="frontend-mode"
                      value="kube-vip"
                      checked={frontendMode === 'kube-vip'}
                      onChange={(e) => setFrontendMode(e.target.value as 'kube-vip' | 'external' | 'none')}
                      className="mt-1"
                    />
                    <div>
                      <p className="text-white font-medium">kube-vip (Recommended)</p>
                      <p className="text-xs text-dark-400">Built-in HA for ≤7 control-plane nodes — L2 ARP VIP via static pod</p>
                    </div>
                  </label>

                  {/* kube-vip Fields */}
                  {frontendMode === 'kube-vip' && (
                    <div className="ml-6 space-y-2 p-2 bg-dark-900/50 rounded border border-dark-700/50">
                      <div>
                        <label className="block text-xs font-medium text-dark-400 mb-1">VIP Endpoint*</label>
                        <input
                          type="text"
                          placeholder="10.2.0.10:6443"
                          value={frontendEndpoint}
                          onChange={(e) => setFrontendEndpoint(e.target.value)}
                          data-testid="frontend-endpoint-input"
                          className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-white focus:outline-none focus:ring-1 focus:ring-gold-500"
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-medium text-dark-400 mb-1">Interface (optional)</label>
                        <input
                          type="text"
                          placeholder="ens3"
                          value={frontendInterface}
                          onChange={(e) => setFrontendInterface(e.target.value)}
                          data-testid="frontend-interface-input"
                          className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-white focus:outline-none focus:ring-1 focus:ring-gold-500"
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-medium text-dark-400 mb-1">Network Baseline</label>
                        <select
                          value={frontendBaseline}
                          onChange={(e) => setFrontendBaseline(e.target.value as 'mgmt' | 'internal' | 'external')}
                          data-testid="frontend-baseline-select"
                          className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-white focus:outline-none focus:ring-1 focus:ring-gold-500"
                        >
                          <option value="mgmt">mgmt</option>
                          <option value="internal">internal</option>
                          <option value="external">external</option>
                        </select>
                      </div>
                    </div>
                  )}

                  {/* external Option */}
                  <label className="flex items-start gap-3 cursor-pointer p-3 hover:bg-dark-700/50 rounded transition-colors" data-testid="frontend-mode-external">
                    <input
                      type="radio"
                      name="frontend-mode"
                      value="external"
                      checked={frontendMode === 'external'}
                      onChange={(e) => setFrontendMode(e.target.value as 'kube-vip' | 'external' | 'none')}
                      className="mt-1"
                    />
                    <div>
                      <p className="text-white font-medium">External LB</p>
                      <p className="text-xs text-dark-400">Recommended for larger clusters or existing F5/NLB infrastructure</p>
                    </div>
                  </label>

                  {/* external Fields */}
                  {frontendMode === 'external' && (
                    <div className="ml-6 space-y-2 p-2 bg-dark-900/50 rounded border border-dark-700/50">
                      <div>
                        <label className="block text-xs font-medium text-dark-400 mb-1">LB Endpoint*</label>
                        <input
                          type="text"
                          placeholder="nlb.example.com:6443"
                          value={frontendEndpoint}
                          onChange={(e) => setFrontendEndpoint(e.target.value)}
                          data-testid="frontend-endpoint-input"
                          className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-white focus:outline-none focus:ring-1 focus:ring-gold-500"
                        />
                      </div>
                    </div>
                  )}

                  {/* none Option */}
                  <label className="flex items-start gap-3 cursor-pointer p-3 hover:bg-dark-700/50 rounded transition-colors" data-testid="frontend-mode-none">
                    <input
                      type="radio"
                      name="frontend-mode"
                      value="none"
                      checked={frontendMode === 'none'}
                      onChange={(e) => setFrontendMode(e.target.value as 'kube-vip' | 'external' | 'none')}
                      className="mt-1"
                    />
                    <div>
                      <p className="text-white font-medium">Single-node demo</p>
                      <p className="text-xs text-dark-400">No HA — cluster cannot be horizontally scaled later</p>
                    </div>
                  </label>
                </div>
              </div>
            )}

            {/* Modal Actions */}
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setShowDeployModal(false)}
                disabled={isDeploying}
                className="px-4 py-2 bg-dark-700 hover:bg-dark-600 text-white rounded transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleDeploy}
                disabled={isDeploying || !selectedBiome}
                data-testid="deploy-submit"
                className="px-4 py-2 bg-gold-600 hover:bg-gold-500 text-dark-900 rounded font-medium transition-colors disabled:opacity-50"
              >
                {isDeploying ? 'Deploying...' : 'Deploy'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default MachineDetail;
