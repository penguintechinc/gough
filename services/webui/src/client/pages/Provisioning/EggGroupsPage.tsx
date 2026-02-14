import { useState, useEffect } from 'react';
import Card from '../../components/Card';
import Button from '../../components/Button';

interface Egg {
  id: number;
  name: string;
  type: string;
}

interface EggGroup {
  id: number;
  name: string;
  description: string;
  eggs: number[];
  egg_details?: Egg[];
  created_at: string;
  updated_at: string | null;
}

export default function EggGroupsPage() {
  const [groups, setGroups] = useState<EggGroup[]>([]);
  const [availableEggs, setAvailableEggs] = useState<Egg[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<EggGroup | null>(null);

  const [formData, setFormData] = useState({
    name: '',
    description: '',
    eggs: [] as number[],
  });
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    fetchGroups();
    fetchAvailableEggs();
  }, []);

  const fetchGroups = async () => {
    setIsLoading(true);
    try {
      const response = await fetch('/api/v1/provisioning/egg-groups', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to fetch egg groups');
      const data = await response.json();
      setGroups(data.items || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load egg groups');
      setGroups([]);
    } finally {
      setIsLoading(false);
    }
  };

  const fetchAvailableEggs = async () => {
    try {
      const response = await fetch('/api/v1/provisioning/eggs', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to fetch eggs');
      const data = await response.json();
      setAvailableEggs(data.items || []);
    } catch (err) {
      console.error('Failed to load eggs:', err);
      setAvailableEggs([]);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      const response = await fetch('/api/v1/provisioning/egg-groups', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
        body: JSON.stringify(formData),
      });
      if (!response.ok) throw new Error('Failed to create egg group');
      setShowCreateModal(false);
      setFormData({ name: '', description: '', eggs: [] });
      fetchGroups();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create egg group');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedGroup) return;
    setIsSubmitting(true);
    try {
      const response = await fetch(`/api/v1/provisioning/egg-groups/${selectedGroup.id}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
        body: JSON.stringify(formData),
      });
      if (!response.ok) throw new Error('Failed to update egg group');
      setShowEditModal(false);
      setSelectedGroup(null);
      setFormData({ name: '', description: '', eggs: [] });
      fetchGroups();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update egg group');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this egg group?')) return;
    try {
      const response = await fetch(`/api/v1/provisioning/egg-groups/${id}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to delete egg group');
      fetchGroups();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete egg group');
    }
  };

  const openEditModal = (group: EggGroup) => {
    setSelectedGroup(group);
    setFormData({
      name: group.name,
      description: group.description,
      eggs: group.eggs,
    });
    setShowEditModal(true);
  };

  const toggleEggSelection = (eggId: number) => {
    setFormData(prev => ({
      ...prev,
      eggs: prev.eggs.includes(eggId)
        ? prev.eggs.filter(id => id !== eggId)
        : [...prev.eggs, eggId]
    }));
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gold-400">Egg Group Management</h1>
          <p className="text-dark-400 mt-1">Organize eggs into deployment groups</p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>+ Create Group</Button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg text-red-400">
          {error}
        </div>
      )}

      <Card>
        {isLoading ? (
          <div className="animate-pulse space-y-4">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-24 bg-dark-700 rounded"></div>
            ))}
          </div>
        ) : groups.length === 0 ? (
          <div className="text-center py-8 text-dark-400">
            <p>No egg groups found</p>
            <Button onClick={() => setShowCreateModal(true)} className="mt-4">
              Create your first group
            </Button>
          </div>
        ) : (
          <div className="space-y-4">
            {groups.map((group) => (
              <div key={group.id} className="border border-dark-700 rounded-lg p-4 hover:border-gold-500/30 transition-colors">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <h3 className="text-lg font-semibold text-gold-400">{group.name}</h3>
                    <p className="text-dark-300 mt-1">{group.description}</p>
                    <div className="mt-3">
                      <p className="text-sm text-dark-400 mb-2">
                        Eggs in this group ({group.eggs.length}):
                      </p>
                      {group.eggs.length === 0 ? (
                        <p className="text-sm text-dark-500 italic">No eggs assigned</p>
                      ) : (
                        <div className="flex flex-wrap gap-2">
                          {group.egg_details?.map((egg) => (
                            <span
                              key={egg.id}
                              className="px-2 py-1 bg-dark-800 border border-dark-600 rounded text-xs text-gold-400"
                            >
                              {egg.name} ({egg.type})
                            </span>
                          )) || group.eggs.map((eggId) => (
                            <span
                              key={eggId}
                              className="px-2 py-1 bg-dark-800 border border-dark-600 rounded text-xs text-dark-400"
                            >
                              Egg #{eggId}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <p className="text-xs text-dark-500 mt-3">
                      Created: {new Date(group.created_at).toLocaleString()}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 ml-4">
                    <button
                      onClick={() => openEditModal(group)}
                      className="text-gold-400 hover:text-gold-300"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => handleDelete(group.id)}
                      className="text-red-400 hover:text-red-300"
                    >
                      Delete
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {(showCreateModal || showEditModal) && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <h2 className="text-xl font-bold text-gold-400 mb-4">
              {showCreateModal ? 'Create New Egg Group' : 'Edit Egg Group'}
            </h2>
            <form onSubmit={showCreateModal ? handleCreate : handleEdit} className="space-y-4">
              <div>
                <label className="block text-sm text-dark-400 mb-1">Group Name</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="input"
                  required
                  placeholder="Production Stack"
                />
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-1">Description</label>
                <input
                  type="text"
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="input"
                  required
                  placeholder="Production deployment configuration"
                />
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-2">Select Eggs</label>
                {availableEggs.length === 0 ? (
                  <p className="text-dark-500 text-sm italic">No eggs available. Create eggs first.</p>
                ) : (
                  <div className="space-y-2 max-h-60 overflow-y-auto border border-dark-700 rounded-lg p-3">
                    {availableEggs.map((egg) => (
                      <label
                        key={egg.id}
                        className="flex items-center gap-3 p-2 hover:bg-dark-800 rounded cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={formData.eggs.includes(egg.id)}
                          onChange={() => toggleEggSelection(egg.id)}
                          className="w-4 h-4 accent-gold-500"
                        />
                        <div className="flex-1">
                          <span className="text-gold-400">{egg.name}</span>
                          <span className="text-dark-500 text-xs ml-2">({egg.type})</span>
                        </div>
                      </label>
                    ))}
                  </div>
                )}
                <p className="text-xs text-dark-500 mt-2">
                  Selected: {formData.eggs.length} egg(s)
                </p>
              </div>
              <div className="flex justify-end gap-3 mt-6">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    setShowCreateModal(false);
                    setShowEditModal(false);
                    setSelectedGroup(null);
                    setFormData({ name: '', description: '', eggs: [] });
                  }}
                >
                  Cancel
                </Button>
                <Button type="submit" isLoading={isSubmitting}>
                  {showCreateModal ? 'Create Group' : 'Save Changes'}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
