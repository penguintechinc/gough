/**
 * TeamsPage - Resource Teams Management
 *
 * Provides interface for managing resource teams, members, and assignments.
 */

import React, { useState, useEffect, useCallback } from 'react';
import api from '../../lib/api';
import { TeamsList } from '../../components/Teams/TeamsList';
import { TeamForm } from '../../components/Teams/TeamForm';
import { TeamDetails } from '../../components/Teams/TeamDetails';

interface Team {
  id: number;
  name: string;
  description: string;
  created_at: string;
  member_count?: number;
  resource_count?: number;
}

type ViewMode = 'list' | 'create' | 'edit' | 'details';

/**
 * Main teams management page.
 */
export const TeamsPage: React.FC = () => {
  const [teams, setTeams] = useState<Team[]>([]);
  const [selectedTeam, setSelectedTeam] = useState<Team | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('list');
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetch teams
  const fetchTeams = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const response = await api.get('/teams');
      setTeams(response.data.teams || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch teams';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTeams();
  }, [fetchTeams]);

  // Handle team selection
  const handleSelectTeam = useCallback((team: Team) => {
    setSelectedTeam(team);
    setViewMode('details');
  }, []);

  // Handle create new team
  const handleCreateClick = useCallback(() => {
    setSelectedTeam(null);
    setViewMode('create');
  }, []);

  // Handle edit team
  const handleEditClick = useCallback((team: Team) => {
    setSelectedTeam(team);
    setViewMode('edit');
  }, []);

  // Handle form submit
  const handleFormSubmit = useCallback(async (data: Partial<Team>) => {
    try {
      if (viewMode === 'create') {
        await api.post('/teams', data);
      } else if (viewMode === 'edit' && selectedTeam) {
        await api.put(`/teams/${selectedTeam.id}`, data);
      }
      await fetchTeams();
      setViewMode('list');
      setSelectedTeam(null);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to save team';
      throw new Error(message);
    }
  }, [viewMode, selectedTeam, fetchTeams]);

  // Handle delete team
  const handleDeleteTeam = useCallback(async (teamId: number) => {
    if (!confirm('Are you sure you want to delete this team?')) return;

    try {
      await api.delete(`/teams/${teamId}`);
      await fetchTeams();
      if (selectedTeam?.id === teamId) {
        setSelectedTeam(null);
        setViewMode('list');
      }
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to delete team';
      setError(message);
    }
  }, [selectedTeam, fetchTeams]);

  // Handle back to list
  const handleBackToList = useCallback(() => {
    setViewMode('list');
    setSelectedTeam(null);
  }, []);

  // Render content based on view mode
  const renderContent = () => {
    if (isLoading && viewMode === 'list') {
      return (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
        </div>
      );
    }

    switch (viewMode) {
      case 'create':
      case 'edit':
        return (
          <TeamForm
            team={selectedTeam}
            onSubmit={handleFormSubmit}
            onCancel={handleBackToList}
          />
        );

      case 'details':
        return selectedTeam ? (
          <TeamDetails
            teamId={selectedTeam.id}
            onBack={handleBackToList}
            onEdit={() => handleEditClick(selectedTeam)}
            onDelete={() => handleDeleteTeam(selectedTeam.id)}
          />
        ) : null;

      default:
        return (
          <TeamsList
            teams={teams}
            onSelect={handleSelectTeam}
            onEdit={handleEditClick}
            onDelete={handleDeleteTeam}
          />
        );
    }
  };

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
          <h1 className="text-2xl font-bold text-gold-500">
            {viewMode === 'create' && 'Create Team'}
            {viewMode === 'edit' && 'Edit Team'}
            {viewMode === 'details' && selectedTeam?.name}
            {viewMode === 'list' && 'Resource Teams'}
          </h1>
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
            Create Team
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

      {/* Content */}
      <div className="bg-dark-900 rounded-lg border border-dark-700 p-6">
        {renderContent()}
      </div>
    </div>
  );
};

export default TeamsPage;
