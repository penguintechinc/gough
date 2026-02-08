/**
 * TeamsList - Display list of resource teams.
 */

import React from 'react';

interface Team {
  id: number;
  name: string;
  description: string;
  created_at: string;
  member_count?: number;
  resource_count?: number;
}

interface TeamsListProps {
  teams: Team[];
  onSelect: (team: Team) => void;
  onEdit: (team: Team) => void;
  onDelete: (teamId: number) => void;
}

/**
 * Teams list component with selection and actions.
 */
export const TeamsList: React.FC<TeamsListProps> = ({
  teams,
  onSelect,
  onEdit,
  onDelete,
}) => {
  if (teams.length === 0) {
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
            d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"
          />
        </svg>
        <p className="text-lg">No teams yet</p>
        <p className="text-sm mt-1">Create a team to group resources and manage access</p>
      </div>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      {teams.map((team) => (
        <div
          key={team.id}
          className="bg-dark-800 border border-dark-700 rounded-lg p-4
                     hover:border-gold-600/50 transition-colors cursor-pointer"
          onClick={() => onSelect(team)}
        >
          {/* Team Header */}
          <div className="flex items-start justify-between mb-3">
            <div>
              <h3 className="font-semibold text-white">{team.name}</h3>
              <p className="text-sm text-dark-400 line-clamp-2 mt-1">
                {team.description || 'No description'}
              </p>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onEdit(team);
                }}
                className="p-1.5 hover:bg-dark-700 rounded transition-colors"
                title="Edit team"
              >
                <svg className="h-4 w-4 text-dark-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                </svg>
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(team.id);
                }}
                className="p-1.5 hover:bg-red-900/30 rounded transition-colors"
                title="Delete team"
              >
                <svg className="h-4 w-4 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </button>
            </div>
          </div>

          {/* Team Stats */}
          <div className="flex items-center gap-4 text-sm text-dark-400">
            <div className="flex items-center gap-1">
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197m13.5-9a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0z" />
              </svg>
              <span>{team.member_count || 0} members</span>
            </div>
            <div className="flex items-center gap-1">
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2" />
              </svg>
              <span>{team.resource_count || 0} resources</span>
            </div>
          </div>

          {/* Created Date */}
          <div className="mt-3 pt-3 border-t border-dark-700 text-xs text-dark-500">
            Created {new Date(team.created_at).toLocaleDateString()}
          </div>
        </div>
      ))}
    </div>
  );
};

export default TeamsList;
