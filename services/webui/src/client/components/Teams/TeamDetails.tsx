/**
 * TeamDetails - View and manage team members and resources.
 */

import React, { useState, useEffect, useCallback } from 'react';
import api from '../../lib/api';

interface TeamMember {
  id: number;
  user_id: number;
  user_email: string;
  user_name: string;
  role: 'admin' | 'member' | 'viewer';
  added_at: string;
}

interface ResourceAssignment {
  id: number;
  resource_type: string;
  resource_id: string;
  resource_name: string;
  assigned_at: string;
}

interface TeamDetailsProps {
  teamId: number;
  onBack: () => void;
  onEdit: () => void;
  onDelete: () => void;
}

type Tab = 'members' | 'resources';

/**
 * Team details component with members and resources management.
 */
export const TeamDetails: React.FC<TeamDetailsProps> = ({
  teamId,
  onBack,
  onEdit,
  onDelete,
}) => {
  const [activeTab, setActiveTab] = useState<Tab>('members');
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [resources, setResources] = useState<ResourceAssignment[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetch team data
  const fetchData = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const [membersRes, resourcesRes] = await Promise.all([
        api.get(`/teams/${teamId}/members`),
        api.get(`/teams/${teamId}/resources`),
      ]);
      setMembers(membersRes.data.members || []);
      setResources(resourcesRes.data.resources || []);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch team data';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [teamId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Remove member
  const handleRemoveMember = useCallback(async (memberId: number) => {
    if (!confirm('Remove this member from the team?')) return;

    try {
      await api.delete(`/teams/${teamId}/members/${memberId}`);
      setMembers((prev) => prev.filter((m) => m.id !== memberId));
    } catch (err: any) {
      setError(err.response?.data?.error || 'Failed to remove member');
    }
  }, [teamId]);

  // Remove resource
  const handleRemoveResource = useCallback(async (assignmentId: number) => {
    if (!confirm('Remove this resource from the team?')) return;

    try {
      await api.delete(`/teams/${teamId}/resources/${assignmentId}`);
      setResources((prev) => prev.filter((r) => r.id !== assignmentId));
    } catch (err: any) {
      setError(err.response?.data?.error || 'Failed to remove resource');
    }
  }, [teamId]);

  // Role badge color
  const getRoleBadgeColor = (role: string) => {
    switch (role) {
      case 'admin':
        return 'bg-gold-600/20 text-gold-400 border-gold-600/30';
      case 'member':
        return 'bg-blue-600/20 text-blue-400 border-blue-600/30';
      default:
        return 'bg-dark-600/20 text-dark-400 border-dark-600/30';
    }
  };

  // Resource type icon
  const getResourceIcon = (type: string) => {
    switch (type) {
      case 'vm':
        return (
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
          </svg>
        );
      case 'cluster':
        return (
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
          </svg>
        );
      case 'container':
        return (
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
          </svg>
        );
      default:
        return (
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2" />
          </svg>
        );
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
      </div>
    );
  }

  return (
    <div>
      {/* Actions */}
      <div className="flex items-center gap-2 mb-6">
        <button
          onClick={onEdit}
          className="px-3 py-1.5 bg-dark-700 hover:bg-dark-600 text-white
                     rounded transition-colors text-sm inline-flex items-center gap-2"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
          </svg>
          Edit
        </button>
        <button
          onClick={onDelete}
          className="px-3 py-1.5 bg-red-900/30 hover:bg-red-900/50 text-red-400
                     rounded transition-colors text-sm inline-flex items-center gap-2"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
          Delete
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b border-dark-700 mb-4">
        <button
          onClick={() => setActiveTab('members')}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            activeTab === 'members'
              ? 'border-gold-500 text-gold-500'
              : 'border-transparent text-dark-400 hover:text-white'
          }`}
        >
          Members ({members.length})
        </button>
        <button
          onClick={() => setActiveTab('resources')}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            activeTab === 'resources'
              ? 'border-gold-500 text-gold-500'
              : 'border-transparent text-dark-400 hover:text-white'
          }`}
        >
          Resources ({resources.length})
        </button>
      </div>

      {/* Members Tab */}
      {activeTab === 'members' && (
        <div className="space-y-2">
          {members.length === 0 ? (
            <p className="text-dark-400 text-center py-8">No members in this team</p>
          ) : (
            members.map((member) => (
              <div
                key={member.id}
                className="flex items-center justify-between p-3 bg-dark-800 rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 bg-dark-600 rounded-full flex items-center justify-center text-sm font-medium">
                    {member.user_name?.charAt(0) || member.user_email?.charAt(0) || '?'}
                  </div>
                  <div>
                    <p className="text-sm font-medium">{member.user_name || member.user_email}</p>
                    <p className="text-xs text-dark-400">{member.user_email}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`px-2 py-0.5 text-xs rounded border ${getRoleBadgeColor(member.role)}`}>
                    {member.role}
                  </span>
                  <button
                    onClick={() => handleRemoveMember(member.id)}
                    className="p-1 hover:bg-red-900/30 rounded transition-colors"
                    title="Remove member"
                  >
                    <svg className="h-4 w-4 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      )}

      {/* Resources Tab */}
      {activeTab === 'resources' && (
        <div className="space-y-2">
          {resources.length === 0 ? (
            <p className="text-dark-400 text-center py-8">No resources assigned to this team</p>
          ) : (
            resources.map((resource) => (
              <div
                key={resource.id}
                className="flex items-center justify-between p-3 bg-dark-800 rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <div className="text-dark-400">
                    {getResourceIcon(resource.resource_type)}
                  </div>
                  <div>
                    <p className="text-sm font-medium">{resource.resource_name || resource.resource_id}</p>
                    <p className="text-xs text-dark-400">{resource.resource_type} / {resource.resource_id}</p>
                  </div>
                </div>
                <button
                  onClick={() => handleRemoveResource(resource.id)}
                  className="p-1 hover:bg-red-900/30 rounded transition-colors"
                  title="Remove resource"
                >
                  <svg className="h-4 w-4 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
};

export default TeamDetails;
