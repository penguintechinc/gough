import { useState, useEffect } from 'react';
import Card from '../../components/Card';
import Button from '../../components/Button';
import TabNavigation from '../../components/TabNavigation';

interface Egg {
  id: number;
  name: string;
  description: string;
  type: 'snap' | 'cloud-init' | 'lxd';
  config: string;
  created_at: string;
  updated_at: string | null;
}

export default function EggsPage() {
  const [eggs, setEggs] = useState<Egg[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [selectedEgg, setSelectedEgg] = useState<Egg | null>(null);
  const [activeTab, setActiveTab] = useState('all');

  const [formData, setFormData] = useState({
    name: '',
    description: '',
    type: 'snap' as 'snap' | 'cloud-init' | 'lxd',
    config: '',
  });
  const [isSubmitting, setIsSubmitting] = useState(false);

  const tabs = [
    { id: 'all', label: 'All Eggs' },
    { id: 'snap', label: 'Snap' },
    { id: 'cloud-init', label: 'Cloud-Init' },
    { id: 'lxd', label: 'LXD' },
  ];

  useEffect(() => {
    fetchEggs();
  }, []);

  const fetchEggs = async () => {
    setIsLoading(true);
    try {
      const response = await fetch('/api/v1/provisioning/eggs', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to fetch eggs');
      const data = await response.json();
      setEggs(data.items || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load eggs');
      setEggs([]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      const response = await fetch('/api/v1/provisioning/eggs', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
        body: JSON.stringify(formData),
      });
      if (!response.ok) throw new Error('Failed to create egg');
      setShowCreateModal(false);
      setFormData({ name: '', description: '', type: 'snap', config: '' });
      fetchEggs();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create egg');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedEgg) return;
    setIsSubmitting(true);
    try {
      const response = await fetch(`/api/v1/provisioning/eggs/${selectedEgg.id}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
        body: JSON.stringify(formData),
      });
      if (!response.ok) throw new Error('Failed to update egg');
      setShowEditModal(false);
      setSelectedEgg(null);
      setFormData({ name: '', description: '', type: 'snap', config: '' });
      fetchEggs();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update egg');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this egg?')) return;
    try {
      const response = await fetch(`/api/v1/provisioning/eggs/${id}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to delete egg');
      fetchEggs();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete egg');
    }
  };

  const openEditModal = (egg: Egg) => {
    setSelectedEgg(egg);
    setFormData({
      name: egg.name,
      description: egg.description,
      type: egg.type,
      config: egg.config,
    });
    setShowEditModal(true);
  };

  const getConfigTemplate = (type: 'snap' | 'cloud-init' | 'lxd'): string => {
    switch (type) {
      case 'snap':
        return `name: my-snap
version: '1.0'
summary: My application snap
description: |
  This is my application snap package
confinement: strict
base: core22

apps:
  my-app:
    command: bin/my-app
    plugs: [network]

parts:
  my-part:
    plugin: nil`;
      case 'cloud-init':
        return `#cloud-config
hostname: myhost
fqdn: myhost.example.com

users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ssh-rsa AAAAB3...

packages:
  - curl
  - git
  - vim

runcmd:
  - echo "Cloud-init completed"`;
      case 'lxd':
        return `config:
  limits.cpu: "2"
  limits.memory: 2GB
  security.nesting: "true"

devices:
  root:
    path: /
    pool: default
    type: disk
  eth0:
    name: eth0
    network: lxdbr0
    type: nic`;
    }
  };

  const filteredEggs = activeTab === 'all'
    ? eggs
    : eggs.filter(egg => egg.type === activeTab);

  const getTypeBadge = (type: string) => {
    const colors = {
      snap: 'bg-purple-900/50 text-purple-400',
      'cloud-init': 'bg-blue-900/50 text-blue-400',
      lxd: 'bg-cyan-900/50 text-cyan-400',
    };
    return colors[type as keyof typeof colors] || 'bg-gray-900/50 text-gray-400';
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gold-400">Egg Management</h1>
          <p className="text-dark-400 mt-1">Manage snap packages, cloud-init configs, and LXD templates</p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>+ Create Egg</Button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg text-red-400">
          {error}
        </div>
      )}

      <TabNavigation tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />

      <div className="mt-6">
        <Card>
          {isLoading ? (
            <div className="animate-pulse space-y-4">
              {[1, 2, 3].map((i) => (
                <div key={i} className="h-20 bg-dark-700 rounded"></div>
              ))}
            </div>
          ) : filteredEggs.length === 0 ? (
            <div className="text-center py-8 text-dark-400">
              <p>No eggs found</p>
              <Button onClick={() => setShowCreateModal(true)} className="mt-4">
                Create your first egg
              </Button>
            </div>
          ) : (
            <div className="space-y-4">
              {filteredEggs.map((egg) => (
                <div key={egg.id} className="border border-dark-700 rounded-lg p-4 hover:border-gold-500/30 transition-colors">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3">
                        <h3 className="text-lg font-semibold text-gold-400">{egg.name}</h3>
                        <span className={`badge ${getTypeBadge(egg.type)}`}>{egg.type}</span>
                      </div>
                      <p className="text-dark-300 mt-1">{egg.description}</p>
                      <div className="mt-3 p-3 bg-dark-900 rounded-lg">
                        <pre className="text-xs text-dark-400 font-mono overflow-x-auto whitespace-pre-wrap">
                          {egg.config.substring(0, 200)}
                          {egg.config.length > 200 && '...'}
                        </pre>
                      </div>
                      <p className="text-xs text-dark-500 mt-2">
                        Created: {new Date(egg.created_at).toLocaleString()}
                      </p>
                    </div>
                    <div className="flex items-center gap-2 ml-4">
                      <button
                        onClick={() => openEditModal(egg)}
                        className="text-gold-400 hover:text-gold-300"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDelete(egg.id)}
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
      </div>

      {(showCreateModal || showEditModal) && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card w-full max-w-3xl max-h-[90vh] overflow-y-auto">
            <h2 className="text-xl font-bold text-gold-400 mb-4">
              {showCreateModal ? 'Create New Egg' : 'Edit Egg'}
            </h2>
            <form onSubmit={showCreateModal ? handleCreate : handleEdit} className="space-y-4">
              <div>
                <label className="block text-sm text-dark-400 mb-1">Name</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="input"
                  required
                  placeholder="my-application"
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
                  placeholder="Brief description of this egg"
                />
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-1">Type</label>
                <select
                  value={formData.type}
                  onChange={(e) => {
                    const newType = e.target.value as 'snap' | 'cloud-init' | 'lxd';
                    setFormData({
                      ...formData,
                      type: newType,
                      config: formData.config || getConfigTemplate(newType)
                    });
                  }}
                  className="input"
                >
                  <option value="snap">Snap Package</option>
                  <option value="cloud-init">Cloud-Init</option>
                  <option value="lxd">LXD Container</option>
                </select>
              </div>
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="block text-sm text-dark-400">Configuration</label>
                  <button
                    type="button"
                    onClick={() => setFormData({ ...formData, config: getConfigTemplate(formData.type) })}
                    className="text-xs text-gold-400 hover:text-gold-300"
                  >
                    Load Template
                  </button>
                </div>
                <textarea
                  value={formData.config}
                  onChange={(e) => setFormData({ ...formData, config: e.target.value })}
                  className="input font-mono text-sm"
                  rows={15}
                  required
                  placeholder="YAML configuration..."
                />
              </div>
              <div className="flex justify-end gap-3 mt-6">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    setShowCreateModal(false);
                    setShowEditModal(false);
                    setSelectedEgg(null);
                    setFormData({ name: '', description: '', type: 'snap', config: '' });
                  }}
                >
                  Cancel
                </Button>
                <Button type="submit" isLoading={isSubmitting}>
                  {showCreateModal ? 'Create Egg' : 'Save Changes'}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
