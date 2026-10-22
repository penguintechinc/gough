/**
 * Audit Log Page
 *
 * Chain-integrity badge + verify-now button, virtualized list for large datasets,
 * side panel detail with JSON viewer.
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../../lib/api';
import { AuditEvent } from '../../types/provisioning';

interface VerificationStatus {
  valid: boolean;
  message: string;
  last_verified_at?: string;
}

interface AuditEventDetail extends AuditEvent {
  raw_event_json?: string;
}

const ITEMS_PER_PAGE = 50;

export const AuditLogPage: React.FC = () => {
  const navigate = useNavigate();
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [selectedEvent, setSelectedEvent] = useState<AuditEventDetail | null>(null);
  const [verificationStatus, setVerificationStatus] = useState<VerificationStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isVerifying, setIsVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);

  const [filters, setFilters] = useState({
    action: '',
    resource_type: '',
    actor_id: '',
  });

  const fetchEvents = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const params: any = {};
      if (filters.action) params.action = filters.action;
      if (filters.resource_type) params.resource_type = filters.resource_type;
      if (filters.actor_id) params.actor_id = filters.actor_id;

      const response = await api.get('/audit/events', { params });
      setEvents(response.data.events || []);
      setPage(0);
      setSelectedEvent(null);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch audit events';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [filters]);

  const verifyChain = useCallback(async () => {
    setIsVerifying(true);

    try {
      const response = await api.post('/audit/verify');
      setVerificationStatus({
        valid: response.data.valid,
        message: response.data.message || 'Chain integrity verified',
        last_verified_at: new Date().toISOString(),
      });
    } catch (err: any) {
      const message = err.response?.data?.error || 'Verification failed';
      setVerificationStatus({
        valid: false,
        message,
      });
    } finally {
      setIsVerifying(false);
    }
  }, []);

  const fetchEventDetail = useCallback(async (eventId: string) => {
    try {
      const response = await api.get(`/audit/events/${eventId}`);
      setSelectedEvent({
        ...response.data,
        raw_event_json: JSON.stringify(response.data, null, 2),
      });
    } catch (err: any) {
      console.error('Failed to fetch event detail:', err);
    }
  }, []);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  useEffect(() => {
    verifyChain();
  }, [verifyChain]);

  const filteredEvents = useMemo(() => {
    return events;
  }, [events]);

  const paginatedEvents = useMemo(() => {
    return filteredEvents.slice(page * ITEMS_PER_PAGE, (page + 1) * ITEMS_PER_PAGE);
  }, [filteredEvents, page]);

  const totalPages = Math.ceil(filteredEvents.length / ITEMS_PER_PAGE);

  const handleFilterChange = (key: string, value: string) => {
    setFilters({ ...filters, [key]: value });
  };

  const clearFilters = () => {
    setFilters({ action: '', resource_type: '', actor_id: '' });
  };

  const hasActiveFilters = filters.action || filters.resource_type || filters.actor_id;

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/')}
            className="p-2 hover:bg-dark-800 rounded transition-colors"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
          <h1 className="text-2xl font-bold text-gold-500">Audit Log</h1>
        </div>

        <div className="flex items-center gap-3">
          {verificationStatus && (
            <div className={`px-3 py-1 rounded text-sm font-medium inline-flex items-center gap-1 ${
              verificationStatus.valid
                ? 'bg-green-900/30 border border-green-700 text-green-300'
                : 'bg-red-900/30 border border-red-700 text-red-300'
            }`}>
              {verificationStatus.valid ? '✓' : '✗'} Chain Verified
            </div>
          )}

          <button
            onClick={verifyChain}
            disabled={isVerifying}
            className="px-4 py-2 bg-gold-600 hover:bg-gold-500 disabled:bg-dark-700 text-white font-medium rounded transition-colors"
          >
            {isVerifying ? 'Verifying...' : 'Verify Now'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg text-red-300">
          {error}
        </div>
      )}

      <div className="grid grid-cols-3 gap-6">
        <div className="col-span-2 space-y-4">
          <div className="bg-dark-900 border border-dark-700 rounded-lg p-4">
            <div className="grid gap-3 md:grid-cols-3">
              <div>
                <label className="block text-sm text-dark-400 mb-1">Action</label>
                <input
                  type="text"
                  value={filters.action}
                  onChange={(e) => handleFilterChange('action', e.target.value)}
                  placeholder="Filter by action..."
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white placeholder-dark-500 focus:border-gold-600 focus:outline-none"
                />
              </div>

              <div>
                <label className="block text-sm text-dark-400 mb-1">Resource Type</label>
                <input
                  type="text"
                  value={filters.resource_type}
                  onChange={(e) => handleFilterChange('resource_type', e.target.value)}
                  placeholder="machine, biome, cluster..."
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white placeholder-dark-500 focus:border-gold-600 focus:outline-none"
                />
              </div>

              <div>
                <label className="block text-sm text-dark-400 mb-1">Actor</label>
                <input
                  type="text"
                  value={filters.actor_id}
                  onChange={(e) => handleFilterChange('actor_id', e.target.value)}
                  placeholder="User ID..."
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white placeholder-dark-500 focus:border-gold-600 focus:outline-none"
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

          <div className="bg-dark-900 border border-dark-700 rounded-lg overflow-hidden">
            {isLoading ? (
              <div className="flex items-center justify-center h-64">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
              </div>
            ) : paginatedEvents.length === 0 ? (
              <div className="text-center py-12 text-dark-400">
                <p className="text-lg">No audit events found</p>
              </div>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-dark-700 bg-dark-800">
                        <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Timestamp</th>
                        <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Action</th>
                        <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Resource</th>
                        <th className="text-left py-3 px-3 text-sm font-medium text-dark-400">Actor</th>
                      </tr>
                    </thead>
                    <tbody>
                      {paginatedEvents.map((event) => (
                        <tr
                          key={event.id}
                          onClick={() => fetchEventDetail(event.id)}
                          className="border-b border-dark-800 hover:bg-dark-800/50 cursor-pointer transition-colors"
                        >
                          <td className="py-3 px-3 text-sm text-dark-300">
                            {new Date(event.timestamp).toLocaleString()}
                          </td>
                          <td className="py-3 px-3 text-sm text-white font-medium">{event.action}</td>
                          <td className="py-3 px-3 text-sm text-dark-300">
                            {event.resource_type} <span className="font-mono text-xs">{event.resource_id}</span>
                          </td>
                          <td className="py-3 px-3 text-sm font-mono text-dark-400">{event.actor_id}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {totalPages > 1 && (
                  <div className="border-t border-dark-700 px-3 py-3 flex items-center justify-between">
                    <span className="text-sm text-dark-400">
                      Page {page + 1} of {totalPages}
                    </span>
                    <div className="flex gap-2">
                      <button
                        onClick={() => setPage(Math.max(0, page - 1))}
                        disabled={page === 0}
                        className="px-3 py-1 bg-dark-800 hover:bg-dark-700 disabled:bg-dark-900 text-white rounded text-sm transition-colors"
                      >
                        Previous
                      </button>
                      <button
                        onClick={() => setPage(Math.min(totalPages - 1, page + 1))}
                        disabled={page === totalPages - 1}
                        className="px-3 py-1 bg-dark-800 hover:bg-dark-700 disabled:bg-dark-900 text-white rounded text-sm transition-colors"
                      >
                        Next
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        <div className="col-span-1">
          {selectedEvent ? (
            <div className="bg-dark-900 border border-dark-700 rounded-lg overflow-hidden sticky top-6">
              <div className="bg-dark-800 px-4 py-3 border-b border-dark-700 flex items-center justify-between">
                <h3 className="font-semibold text-white">Event Details</h3>
                <button
                  onClick={() => setSelectedEvent(null)}
                  className="text-dark-400 hover:text-white"
                >
                  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>

              <div className="p-4 space-y-4 max-h-[calc(100vh-200px)] overflow-y-auto">
                <div>
                  <p className="text-xs text-dark-400 mb-1">ID</p>
                  <p className="text-white font-mono text-sm break-all">{selectedEvent.id}</p>
                </div>

                <div>
                  <p className="text-xs text-dark-400 mb-1">Timestamp</p>
                  <p className="text-white text-sm">{new Date(selectedEvent.timestamp).toLocaleString()}</p>
                </div>

                <div>
                  <p className="text-xs text-dark-400 mb-1">Action</p>
                  <p className="text-white text-sm">{selectedEvent.action}</p>
                </div>

                <div>
                  <p className="text-xs text-dark-400 mb-1">Resource</p>
                  <p className="text-white text-sm">{selectedEvent.resource_type}</p>
                  <p className="text-dark-400 font-mono text-xs">{selectedEvent.resource_id}</p>
                </div>

                <div>
                  <p className="text-xs text-dark-400 mb-1">Actor</p>
                  <p className="text-white font-mono text-sm">{selectedEvent.actor_id}</p>
                </div>

                {selectedEvent.changes && (
                  <div>
                    <p className="text-xs text-dark-400 mb-1">Changes</p>
                    <pre className="bg-dark-800 border border-dark-700 rounded p-2 text-xs text-dark-200 overflow-x-auto">
                      {JSON.stringify(selectedEvent.changes, null, 2)}
                    </pre>
                  </div>
                )}

                {selectedEvent.chain_hash && (
                  <div>
                    <p className="text-xs text-dark-400 mb-1">Chain Hash</p>
                    <p className="text-dark-300 font-mono text-xs break-all">{selectedEvent.chain_hash}</p>
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="bg-dark-900 border border-dark-700 rounded-lg p-6 text-center text-dark-400 sticky top-6">
              <svg className="h-12 w-12 mx-auto mb-3 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              <p className="text-sm">Select an event to view details</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default AuditLogPage;
