/**
 * Machines Page
 *
 * List view of all machines with filtering by status, zone, and pool.
 * Supports bulk actions on selected machines.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import api from '../../lib/api';

interface Machine {
  id: string;
  hostname: string;
  state: string;
  zone: string;
  pool: string;
  ip_address: string;
  created_at: string;
  last_seen?: string;
  egg_name?: string;
  deployed_at?: string;
}

export const MachinesPage: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [machines, setMachines] = useState<Machine[]>([]);
  const [selectedMachines, setSelectedMachines] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [filters, setFilters] = useState({
    state: searchParams.get('state') || '',
    zone: searchParams.get('zone') || '',
    pool: searchParams.get('pool') || '',
    search: searchParams.get('search') || '',
  });

  const fetchMachines = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const params: any = {};
      if (filters.state) params.state = filters.state;
      if (filters.zone) params.zone = filters.zone;
      if (filters.pool) params.pool = filters.pool;
      if (filters.search) params.search = filters.search;

      const response = await api.get('/provisioning/machines', { params });
      setMachines(response.data.machines || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch machines';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    fetchMachines();
  }, [fetchMachines]);

  const handleFilterChange = (key: string, value: string) => {
    const newFilters = { ...filters, [key]: value };
    setFilters(newFilters);

    const params: any = {};
    if (newFilters.state) params.state = newFilters.state;
    if (newFilters.zone) params.zone = newFilters.zone;
    if (newFilters.pool) params.pool = newFilters.pool;
    if (newFilters.search) params.search = newFilters.search;
    setSearchParams(params);
  };

  const handleSelectAll = () => {
    if (selectedMachines.size === machines.length) {
      setSelectedMachines(new Set());
    } else {
      setSelectedMachines(new Set(machines.map((m) => m.id)));
    }
  };

  const handleSelectMachine = (machineId: string) => {
    const newSelected = new Set(selectedMachines);
    if (newSelected.has(machineId)) {
      newSelected.delete(machineId);
    } else {
      newSelected.add(machineId);
    }
    setSelectedMachines(newSelected);
  };

  const handleBulkAction = async (action: string) => {
    if (selectedMachines.size === 0) {
      alert('No machines selected');
      return;
    }

    if (!confirm(`${action} ${selectedMachines.size} machine(s)?`)) return;

    try {
      await api.post('/provisioning/machines/bulk', {
        action,
        machine_ids: Array.from(selectedMachines),
      });
      setSelectedMachines(new Set());
      await fetchMachines();
    } catch (err: any) {
      const message = err.response?.data?.error || `Failed to ${action} machines`;
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
      case 'retired':
        return 'text-gray-400 bg-gray-900/30 border-gray-700';
      default:
        return 'text-dark-400 bg-dark-800 border-dark-700';
    }
  };

  const clearFilters = () => {
    setFilters({ state: '', zone: '', pool: '', search: '' });
    setSearchParams({});
  };

  const hasActiveFilters = filters.state || filters.zone || filters.pool || filters.search;

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      {/* Header */}
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
          <h1 className="text-2xl font-bold text-gold-500">Machines</h1>
          <span className="px-3 py-1 bg-dark-800 text-dark-300 rounded text-sm">
            {machines.length} total
          </span>
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

      {/* Filters */}
      <div className="bg-dark-900 border border-dark-700 rounded-lg p-4 mb-6">
        <div className="grid gap-4 md:grid-cols-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Search</label>
            <input
              type="text"
              value={filters.search}
              onChange={(e) => handleFilterChange('search', e.target.value)}
              placeholder="Hostname or IP..."
              className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded
                         text-white placeholder-dark-500 focus:border-gold-600 focus:outline-none"
            />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">State</label>
            <select
              value={filters.state}
              onChange={(e) => handleFilterChange('state', e.target.value)}
              className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded
                         text-white focus:border-gold-600 focus:outline-none"
            >
              <option value="">All States</option>
              <option value="ready">Ready</option>
              <option value="allocated">Allocated</option>
              <option value="deploying">Deploying</option>
              <option value="failed">Failed</option>
              <option value="retired">Retired</option>
            </select>
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Zone</label>
            <input
              type="text"
              value={filters.zone}
              onChange={(e) => handleFilterChange('zone', e.target.value)}
              placeholder="Zone name..."
              className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded
                         text-white placeholder-dark-500 focus:border-gold-600 focus:outline-none"
            />
          </div>
          <div>
            <label className="block text-sm text-dark-400 mb-1">Pool</label>
            <input
              type="text"
              value={filters.pool}
              onChange={(e) => handleFilterChange('pool', e.target.value)}
              placeholder="Pool name..."
              className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded
                         text-white placeholder-dark-500 focus:border-gold-600 focus:outline-none"
            />
          </div>
        </div>
        {hasActiveFilters && (
          <div className="mt-3">
            <button
              onClick={clearFilters}
              className="text-sm text-gold-500 hover:text-gold-400 inline-flex items-center gap-1"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
              Clear Filters
            </button>
          </div>
        )}
      </div>

      {/* Bulk Actions */}
      {selectedMachines.size > 0 && (
        <div className="bg-dark-900 border border-dark-700 rounded-lg p-4 mb-6 flex items-center justify-between">
          <span className="text-sm text-dark-300">
            {selectedMachines.size} machine(s) selected
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => handleBulkAction('retire')}
              className="px-3 py-1.5 bg-dark-700 hover:bg-dark-600 text-white rounded text-sm transition-colors"
            >
              Retire
            </button>
            <button
              onClick={() => handleBulkAction('reset')}
              className="px-3 py-1.5 bg-dark-700 hover:bg-dark-600 text-white rounded text-sm transition-colors"
            >
              Reset
            </button>
            <button
              onClick={() => setSelectedMachines(new Set())}
              className="px-3 py-1.5 bg-red-900/30 hover:bg-red-900/50 text-red-400 rounded text-sm transition-colors"
            >
              Clear Selection
            </button>
          </div>
        </div>
      )}

      {/* Machines Table */}
      <div className="bg-dark-900 border border-dark-700 rounded-lg overflow-hidden">
        {isLoading ? (
          <div className="flex items-center justify-center h-64">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
          </div>
        ) : machines.length === 0 ? (
          <div className="text-center py-12 text-dark-400">
            <svg
              className="mx-auto h-12 w-12 mb-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2" />
            </svg>
            <p className="text-lg">No machines found</p>
            {hasActiveFilters && (
              <button
                onClick={clearFilters}
                className="mt-2 text-sm text-gold-500 hover:text-gold-400"
              >
                Clear filters to see all machines
              </button>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-dark-700 bg-dark-800">
                  <th className="py-3 px-3">
                    <input
                      type="checkbox"
                      checked={selectedMachines.size === machines.length && machines.length > 0}
                      onChange={handleSelectAll}
                      className="rounded border-dark-600 bg-dark-700 text-gold-600 focus:ring-gold-500"
                    />
                  </th>
                  <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">ID</th>
                  <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Hostname</th>
                  <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">State</th>
                  <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Zone</th>
                  <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Pool</th>
                  <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">IP Address</th>
                  <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Last Seen</th>
                </tr>
              </thead>
              <tbody>
                {machines.map((machine) => (
                  <tr
                    key={machine.id}
                    className="border-b border-dark-800 hover:bg-dark-800/50 cursor-pointer transition-colors"
                    onClick={() => navigate(`/provisioning/machines/${machine.id}`)}
                  >
                    <td className="py-3 px-3" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={selectedMachines.has(machine.id)}
                        onChange={() => handleSelectMachine(machine.id)}
                        className="rounded border-dark-600 bg-dark-700 text-gold-600 focus:ring-gold-500"
                      />
                    </td>
                    <td className="py-3 px-3 text-sm font-mono text-dark-300">{machine.id}</td>
                    <td className="py-3 px-3 text-sm text-white font-medium">{machine.hostname}</td>
                    <td className="py-3 px-3">
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded border ${getStateColor(machine.state)}`}>
                        {machine.state}
                      </span>
                    </td>
                    <td className="py-3 px-3 text-sm text-dark-300">{machine.zone}</td>
                    <td className="py-3 px-3 text-sm text-dark-300">{machine.pool}</td>
                    <td className="py-3 px-3 text-sm font-mono text-dark-300">{machine.ip_address}</td>
                    <td className="py-3 px-3 text-sm text-dark-400">
                      {machine.last_seen ? new Date(machine.last_seen).toLocaleString() : 'Never'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

export default MachinesPage;
