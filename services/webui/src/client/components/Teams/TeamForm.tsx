/**
 * TeamForm - Create/Edit team form.
 */

import React, { useState, useCallback } from 'react';

interface Team {
  id: number;
  name: string;
  description: string;
}

interface TeamFormProps {
  team?: Team | null;
  onSubmit: (data: Partial<Team>) => Promise<void>;
  onCancel: () => void;
}

/**
 * Form for creating or editing a team.
 */
export const TeamForm: React.FC<TeamFormProps> = ({
  team,
  onSubmit,
  onCancel,
}) => {
  const [name, setName] = useState(team?.name || '');
  const [description, setDescription] = useState(team?.description || '');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isEdit = Boolean(team);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();

    if (!name.trim()) {
      setError('Team name is required');
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      await onSubmit({
        name: name.trim(),
        description: description.trim(),
      });
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsSubmitting(false);
    }
  }, [name, description, onSubmit]);

  return (
    <form onSubmit={handleSubmit} className="max-w-xl">
      {/* Error Message */}
      {error && (
        <div className="mb-6 p-3 bg-red-900/30 border border-red-700 rounded text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Team Name */}
      <div className="mb-4">
        <label htmlFor="name" className="block text-sm font-medium text-dark-300 mb-1">
          Team Name <span className="text-red-500">*</span>
        </label>
        <input
          type="text"
          id="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g., Production Servers"
          className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded
                     text-white placeholder-dark-500 focus:outline-none
                     focus:border-gold-500 transition-colors"
          disabled={isSubmitting}
        />
      </div>

      {/* Description */}
      <div className="mb-6">
        <label htmlFor="description" className="block text-sm font-medium text-dark-300 mb-1">
          Description
        </label>
        <textarea
          id="description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Optional description of this team's purpose"
          rows={3}
          className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded
                     text-white placeholder-dark-500 focus:outline-none
                     focus:border-gold-500 transition-colors resize-none"
          disabled={isSubmitting}
        />
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={isSubmitting}
          className="px-4 py-2 bg-gold-600 hover:bg-gold-500 text-dark-900
                     font-medium rounded transition-colors disabled:opacity-50
                     disabled:cursor-not-allowed inline-flex items-center gap-2"
        >
          {isSubmitting ? (
            <>
              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
              <span>Saving...</span>
            </>
          ) : (
            <span>{isEdit ? 'Update Team' : 'Create Team'}</span>
          )}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={isSubmitting}
          className="px-4 py-2 bg-dark-700 hover:bg-dark-600 text-white
                     rounded transition-colors disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </form>
  );
};

export default TeamForm;
