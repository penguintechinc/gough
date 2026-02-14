/**
 * Machine Detail Page
 *
 * Single machine view with tabs for Info, Hardware, Eggs, and Logs.
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

type Tab = 'info' | 'hardware' | 'eggs' | 'logs';

export const MachineDetail: React.FC = () => {
  const { machineId } = useParams<{ machineId: string }>();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<Tab>('info');
  const [machine, setMachine] = useState<Machine | null>(null);
  const [hardware, setHardware] = useState<HardwareInfo | null>(null);
  const [eggs, setEggs] = useState<EggDeployment[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
      const response = await api.get(`/provisioning/machines/${machineId}/eggs`);
      setEggs(response.data.eggs || []);
    } catch (err: any) {
      console.error('Failed to fetch eggs:', err);
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
    } else if (activeTab === 'eggs' && eggs.length === 0) {
      fetchEggs();
    } else if (activeTab === 'logs' && logs.length === 0) {
      fetchLogs();
    }
  }, [activeTab, hardware, eggs.length, logs.length, fetchHardware, fetchEggs, fetchLogs]);

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
          { id: 'eggs', label: 'Eggs' },
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

        {/* Eggs Tab */}
        {activeTab === 'eggs' && (
          <div>
            {eggs.length > 0 ? (
              <div className="space-y-3">
                {eggs.map((egg) => (
                  <div key={egg.id} className="p-4 bg-dark-800 rounded-lg">
                    <div className="flex items-center justify-between mb-2">
                      <div>
                        <h4 className="text-white font-medium">{egg.egg_name}</h4>
                        <p className="text-sm text-dark-400">Version {egg.egg_version}</p>
                      </div>
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded border ${getStateColor(egg.state)}`}>
                        {egg.state}
                      </span>
                    </div>
                    <div className="flex items-center gap-4 text-sm text-dark-400">
                      <span>Deployed: {new Date(egg.deployed_at).toLocaleString()}</span>
                      {egg.duration_seconds && <span>Duration: {egg.duration_seconds}s</span>}
                    </div>
                    {egg.error_message && (
                      <div className="mt-2 p-2 bg-red-900/20 border border-red-700 rounded text-sm text-red-300">
                        {egg.error_message}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-12 text-dark-400">
                <p>No eggs deployed on this machine</p>
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
    </div>
  );
};

export default MachineDetail;
