/**
 * Biomes Page (Extended)
 *
 * Extends the existing BiomesPage with kind/phase/workload/tag filter chips
 * and per-node "requirements met?" indicator.
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../../lib/api';
import { Biome, Machine } from '../../types/provisioning';

interface EggWithEligibility extends Biome {
  eligible_nodes: number;
  total_nodes: number;
  eligibility_percent: number;
}

export const BiomesPageExtended: React.FC = () => {
  const navigate = useNavigate();
  const [biomes, setEggs] = useState<EggWithEligibility[]>([]);
  const [machines, setMachines] = useState<Machine[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [filters, setFilters] = useState({
    kind: '' as 'compute' | 'storage' | 'infrastructure' | '',
    phase: '' as 'phase-0' | 'phase-1' | 'phase-2' | '',
    workload: '' as 'lxc' | 'qemu' | 'k8s' | '',
    tags: '' as string,
  });

  const fetchEggs = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const response = await api.get('/provisioning/biomes');
      const eggsList = response.data.biomes || [];

      // Fetch machines for eligibility check
      const machinesRes = await api.get('/provisioning/machines');
      const machinesList = machinesRes.data.machines || [];
      setMachines(machinesList);

      // Calculate eligibility for each biome
      const eggsWithEligibility = eggsList.map((biome: Biome) => {
        const eligibleNodes = machinesList.filter((m: Machine) =>
          biome.requires_hardware_tags.every((tag) => m.tags?.includes(tag))
        ).length;

        return {
          ...biome,
          eligible_nodes: eligibleNodes,
          total_nodes: machinesList.length,
          eligibility_percent: machinesList.length > 0 ? Math.round((eligibleNodes / machinesList.length) * 100) : 0,
        };
      });

      setEggs(eggsWithEligibility);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch biomes';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchEggs();
  }, [fetchEggs]);

  const filteredEggs = useMemo(() => {
    return biomes.filter((biome) => {
      if (filters.kind && biome.kind !== filters.kind) return false;
      if (filters.phase && biome.phase !== filters.phase) return false;
      if (filters.workload && biome.workload_type !== filters.workload) return false;
      return true;
    });
  }, [biomes, filters]);

  const handleFilterChange = (key: string, value: string) => {
    setFilters({
      ...filters,
      [key]: value,
    });
  };

  const clearFilters = () => {
    setFilters({ kind: '', phase: '', workload: '', tags: '' });
  };

  const hasActiveFilters = filters.kind || filters.phase || filters.workload;

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
          <h1 className="text-2xl font-bold text-gold-500">Biomes</h1>
          <span className="px-3 py-1 bg-dark-800 text-dark-300 rounded text-sm">
            {filteredEggs.length} available
          </span>
        </div>
      </div>

      {error && (
        <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-3">
          <svg className="h-5 w-5 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span className="text-red-300">{error}</span>
        </div>
      )}

      <div className="bg-dark-900 border border-dark-700 rounded-lg p-4 mb-6">
        <div className="grid gap-4 md:grid-cols-4">
          <div>
            <label className="block text-sm text-dark-400 mb-1">Kind</label>
            <select
              value={filters.kind}
              onChange={(e) => handleFilterChange('kind', e.target.value)}
              className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
            >
              <option value="">All Kinds</option>
              <option value="compute">Compute</option>
              <option value="storage">Storage</option>
              <option value="infrastructure">Infrastructure</option>
            </select>
          </div>

          <div>
            <label className="block text-sm text-dark-400 mb-1">Phase</label>
            <select
              value={filters.phase}
              onChange={(e) => handleFilterChange('phase', e.target.value)}
              className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
            >
              <option value="">All Phases</option>
              <option value="phase-0">Phase 0</option>
              <option value="phase-1">Phase 1</option>
              <option value="phase-2">Phase 2</option>
            </select>
          </div>

          <div>
            <label className="block text-sm text-dark-400 mb-1">Workload</label>
            <select
              value={filters.workload}
              onChange={(e) => handleFilterChange('workload', e.target.value)}
              className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white focus:border-gold-600 focus:outline-none"
            >
              <option value="">All Workloads</option>
              <option value="lxc">LXC</option>
              <option value="qemu">QEMU</option>
              <option value="k8s">Kubernetes</option>
            </select>
          </div>

          {hasActiveFilters && (
            <div className="flex items-end">
              <button
                onClick={clearFilters}
                className="w-full px-3 py-2 text-sm text-gold-500 hover:text-gold-400 border border-gold-600 rounded transition-colors"
              >
                Clear Filters
              </button>
            </div>
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
        </div>
      ) : filteredEggs.length === 0 ? (
        <div className="text-center py-12 text-dark-400 bg-dark-900 border border-dark-700 rounded-lg">
          <p className="text-lg">No biomes found</p>
          {hasActiveFilters && (
            <button
              onClick={clearFilters}
              className="mt-2 text-sm text-gold-500 hover:text-gold-400"
            >
              Clear filters
            </button>
          )}
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filteredEggs.map((biome) => (
            <div
              key={`${biome.name}-${biome.version}`}
              onClick={() => navigate(`/provisioning/biomes/${biome.id}`)}
              className="bg-dark-900 border border-dark-700 rounded-lg p-4 hover:border-gold-600 cursor-pointer transition-all hover:shadow-lg hover:shadow-gold-500/20"
            >
              <div className="mb-3">
                <h3 className="text-lg font-semibold text-white">{biome.name}</h3>
                <p className="text-sm text-dark-400">{biome.version}</p>
              </div>

              {biome.description && (
                <p className="text-sm text-dark-300 mb-3 line-clamp-2">{biome.description}</p>
              )}

              <div className="flex flex-wrap gap-1 mb-3">
                <span className={`px-2 py-0.5 text-xs rounded border ${getKindColor(biome.kind)}`}>
                  {biome.kind}
                </span>
                <span className="px-2 py-0.5 text-xs rounded border bg-dark-700 border-dark-600 text-dark-300">
                  {biome.phase}
                </span>
                <span className="px-2 py-0.5 text-xs rounded border bg-dark-700 border-dark-600 text-dark-300">
                  {biome.workload_type}
                </span>
              </div>

              <div className="bg-dark-800 rounded p-2 mb-3">
                <p className="text-xs text-dark-400 mb-1">Node Eligibility</p>
                <div className="flex items-center justify-between">
                  <div className="flex-1 h-2 bg-dark-700 rounded mr-2 overflow-hidden">
                    <div
                      className="h-full bg-gradient-to-r from-gold-600 to-gold-500"
                      style={{ width: `${biome.eligibility_percent}%` }}
                    />
                  </div>
                  <span className={`text-xs font-medium whitespace-nowrap ${biome.eligibility_percent === 100 ? 'text-green-400' : biome.eligibility_percent >= 50 ? 'text-yellow-400' : 'text-red-400'}`}>
                    {biome.eligible_nodes}/{biome.total_nodes}
                  </span>
                </div>
              </div>

              <div className="text-xs text-dark-400 space-y-1">
                {biome.lock_to_host && (
                  <div>🔒 Locked to host</div>
                )}
                {biome.emits_joiner_secrets && (
                  <div>🔑 Emits secrets</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default BiomesPageExtended;
