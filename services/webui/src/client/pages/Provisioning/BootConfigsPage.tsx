import { useState, useEffect } from 'react';
import Card from '../../components/Card';
import Button from '../../components/Button';
import TabNavigation from '../../components/TabNavigation';

interface BootConfig {
  id: number;
  name: string;
  description: string;
  boot_type: 'ipxe' | 'grub' | 'uefi';
  script: string;
  kernel_params?: string;
  created_at: string;
  updated_at: string | null;
}

export default function BootConfigsPage() {
  const [configs, setConfigs] = useState<BootConfig[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showPreviewModal, setShowPreviewModal] = useState(false);
  const [selectedConfig, setSelectedConfig] = useState<BootConfig | null>(null);
  const [activeTab, setActiveTab] = useState('all');

  const [formData, setFormData] = useState({
    name: '',
    description: '',
    boot_type: 'ipxe' as 'ipxe' | 'grub' | 'uefi',
    script: '',
    kernel_params: '',
  });
  const [isSubmitting, setIsSubmitting] = useState(false);

  const tabs = [
    { id: 'all', label: 'All Configs' },
    { id: 'ipxe', label: 'iPXE' },
    { id: 'grub', label: 'GRUB' },
    { id: 'uefi', label: 'UEFI' },
  ];

  useEffect(() => {
    fetchConfigs();
  }, []);

  const fetchConfigs = async () => {
    setIsLoading(true);
    try {
      const response = await fetch('/api/v1/provisioning/boot-configs', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to fetch boot configs');
      const data = await response.json();
      setConfigs(data.items || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load boot configs');
      setConfigs([]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      const response = await fetch('/api/v1/provisioning/boot-configs', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
        body: JSON.stringify(formData),
      });
      if (!response.ok) throw new Error('Failed to create boot config');
      setShowCreateModal(false);
      setFormData({ name: '', description: '', boot_type: 'ipxe', script: '', kernel_params: '' });
      fetchConfigs();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create boot config');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedConfig) return;
    setIsSubmitting(true);
    try {
      const response = await fetch(`/api/v1/provisioning/boot-configs/${selectedConfig.id}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
        body: JSON.stringify(formData),
      });
      if (!response.ok) throw new Error('Failed to update boot config');
      setShowEditModal(false);
      setSelectedConfig(null);
      setFormData({ name: '', description: '', boot_type: 'ipxe', script: '', kernel_params: '' });
      fetchConfigs();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update boot config');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this boot config?')) return;
    try {
      const response = await fetch(`/api/v1/provisioning/boot-configs/${id}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to delete boot config');
      fetchConfigs();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete boot config');
    }
  };

  const openEditModal = (config: BootConfig) => {
    setSelectedConfig(config);
    setFormData({
      name: config.name,
      description: config.description,
      boot_type: config.boot_type,
      script: config.script,
      kernel_params: config.kernel_params || '',
    });
    setShowEditModal(true);
  };

  const openPreviewModal = (config: BootConfig) => {
    setSelectedConfig(config);
    setShowPreviewModal(true);
  };

  const getScriptTemplate = (type: 'ipxe' | 'grub' | 'uefi'): string => {
    switch (type) {
      case 'ipxe':
        return `#!ipxe

# iPXE boot script
dhcp
echo Loading kernel...
kernel http://boot.server.io/images/vmlinuz
echo Loading initrd...
initrd http://boot.server.io/images/initrd.img
echo Booting...
boot

# Or chain to another bootloader
# chain http://boot.server.io/boot.ipxe`;
      case 'grub':
        return `# GRUB boot configuration
set timeout=5
set default=0

menuentry "Ubuntu Server" {
    linux /vmlinuz root=/dev/sda1 ro quiet splash
    initrd /initrd.img
}

menuentry "Ubuntu Server (Recovery)" {
    linux /vmlinuz root=/dev/sda1 ro recovery nomodeset
    initrd /initrd.img
}`;
      case 'uefi':
        return `# UEFI boot configuration
\\EFI\\BOOT\\BOOTX64.EFI

# Or custom UEFI shell script
fs0:
cd \\EFI\\ubuntu
grubx64.efi`;
    }
  };

  const getTypeBadge = (type: string) => {
    const colors = {
      ipxe: 'bg-purple-900/50 text-purple-400',
      grub: 'bg-blue-900/50 text-blue-400',
      uefi: 'bg-cyan-900/50 text-cyan-400',
    };
    return colors[type as keyof typeof colors] || 'bg-gray-900/50 text-gray-400';
  };

  const filteredConfigs = activeTab === 'all'
    ? configs
    : configs.filter(config => config.boot_type === activeTab);

  const renderScriptPreview = (script: string, type: string) => {
    const lines = script.split('\n');
    return (
      <div className="bg-dark-900 rounded-lg p-4 font-mono text-sm overflow-x-auto">
        {lines.map((line, idx) => (
          <div key={idx} className="flex">
            <span className="text-dark-600 select-none mr-4 text-right" style={{ minWidth: '2rem' }}>
              {idx + 1}
            </span>
            <span className={
              line.trim().startsWith('#') ? 'text-green-500' :
              line.trim().startsWith('menuentry') || line.trim().startsWith('set') ? 'text-blue-400' :
              line.includes('kernel') || line.includes('initrd') || line.includes('boot') ? 'text-gold-400' :
              'text-dark-300'
            }>
              {line || ' '}
            </span>
          </div>
        ))}
      </div>
    );
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gold-400">Boot Configuration Management</h1>
          <p className="text-dark-400 mt-1">Create and manage iPXE, GRUB, and UEFI boot configurations</p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>+ Create Config</Button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg text-red-400">
          {error}
          <button
            onClick={() => setError(null)}
            className="ml-4 text-red-300 hover:text-red-200"
          >
            ✕
          </button>
        </div>
      )}

      <TabNavigation tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />

      <div className="mt-6">
        <Card>
          {isLoading ? (
            <div className="animate-pulse space-y-4">
              {[1, 2, 3].map((i) => (
                <div key={i} className="h-32 bg-dark-700 rounded"></div>
              ))}
            </div>
          ) : filteredConfigs.length === 0 ? (
            <div className="text-center py-8 text-dark-400">
              <p>No boot configurations found</p>
              <Button onClick={() => setShowCreateModal(true)} className="mt-4">
                Create your first config
              </Button>
            </div>
          ) : (
            <div className="space-y-4">
              {filteredConfigs.map((config) => (
                <div key={config.id} className="border border-dark-700 rounded-lg p-4 hover:border-gold-500/30 transition-colors">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3">
                        <h3 className="text-lg font-semibold text-gold-400">{config.name}</h3>
                        <span className={`badge ${getTypeBadge(config.boot_type)}`}>
                          {config.boot_type.toUpperCase()}
                        </span>
                      </div>
                      <p className="text-dark-300 mt-1">{config.description}</p>
                      {config.kernel_params && (
                        <div className="mt-2">
                          <span className="text-xs text-dark-500">Kernel Parameters:</span>
                          <p className="text-xs text-dark-400 font-mono mt-1">{config.kernel_params}</p>
                        </div>
                      )}
                      <div className="mt-3 p-3 bg-dark-900 rounded-lg">
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-xs text-dark-500">Boot Script Preview:</span>
                          <button
                            onClick={() => openPreviewModal(config)}
                            className="text-xs text-gold-400 hover:text-gold-300"
                          >
                            View Full Script
                          </button>
                        </div>
                        <pre className="text-xs text-dark-400 font-mono overflow-x-auto whitespace-pre-wrap">
                          {config.script.substring(0, 300)}
                          {config.script.length > 300 && '...'}
                        </pre>
                      </div>
                      <p className="text-xs text-dark-500 mt-2">
                        Created: {new Date(config.created_at).toLocaleString()}
                      </p>
                    </div>
                    <div className="flex items-center gap-2 ml-4">
                      <button
                        onClick={() => openEditModal(config)}
                        className="text-gold-400 hover:text-gold-300"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDelete(config.id)}
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
          <div className="card w-full max-w-4xl max-h-[90vh] overflow-y-auto">
            <h2 className="text-xl font-bold text-gold-400 mb-4">
              {showCreateModal ? 'Create Boot Configuration' : 'Edit Boot Configuration'}
            </h2>
            <form onSubmit={showCreateModal ? handleCreate : handleEdit} className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm text-dark-400 mb-1">Configuration Name</label>
                  <input
                    type="text"
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    className="input"
                    required
                    placeholder="ubuntu-pxe-boot"
                  />
                </div>
                <div>
                  <label className="block text-sm text-dark-400 mb-1">Boot Type</label>
                  <select
                    value={formData.boot_type}
                    onChange={(e) => {
                      const newType = e.target.value as 'ipxe' | 'grub' | 'uefi';
                      setFormData({
                        ...formData,
                        boot_type: newType,
                        script: formData.script || getScriptTemplate(newType)
                      });
                    }}
                    className="input"
                  >
                    <option value="ipxe">iPXE</option>
                    <option value="grub">GRUB</option>
                    <option value="uefi">UEFI</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-1">Description</label>
                <input
                  type="text"
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="input"
                  required
                  placeholder="Ubuntu 22.04 PXE boot configuration"
                />
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-1">Kernel Parameters (Optional)</label>
                <input
                  type="text"
                  value={formData.kernel_params}
                  onChange={(e) => setFormData({ ...formData, kernel_params: e.target.value })}
                  className="input"
                  placeholder="quiet splash acpi=off"
                />
              </div>
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="block text-sm text-dark-400">Boot Script</label>
                  <button
                    type="button"
                    onClick={() => setFormData({ ...formData, script: getScriptTemplate(formData.boot_type) })}
                    className="text-xs text-gold-400 hover:text-gold-300"
                  >
                    Load Template
                  </button>
                </div>
                <textarea
                  value={formData.script}
                  onChange={(e) => setFormData({ ...formData, script: e.target.value })}
                  className="input font-mono text-sm"
                  rows={18}
                  required
                  placeholder="Enter boot script..."
                />
              </div>
              <div className="flex justify-end gap-3 mt-6">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    setShowCreateModal(false);
                    setShowEditModal(false);
                    setSelectedConfig(null);
                    setFormData({ name: '', description: '', boot_type: 'ipxe', script: '', kernel_params: '' });
                  }}
                >
                  Cancel
                </Button>
                <Button type="submit" isLoading={isSubmitting}>
                  {showCreateModal ? 'Create Configuration' : 'Save Changes'}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}

      {showPreviewModal && selectedConfig && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card w-full max-w-4xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-gold-400">
                {selectedConfig.name} - Script Preview
              </h2>
              <button
                onClick={() => {
                  setShowPreviewModal(false);
                  setSelectedConfig(null);
                }}
                className="text-dark-400 hover:text-gold-400 text-2xl"
              >
                ✕
              </button>
            </div>
            <div className="mb-4">
              <span className={`badge ${getTypeBadge(selectedConfig.boot_type)}`}>
                {selectedConfig.boot_type.toUpperCase()}
              </span>
              <p className="text-dark-300 mt-2">{selectedConfig.description}</p>
              {selectedConfig.kernel_params && (
                <div className="mt-2">
                  <span className="text-xs text-dark-500">Kernel Parameters:</span>
                  <p className="text-sm text-dark-400 font-mono mt-1">{selectedConfig.kernel_params}</p>
                </div>
              )}
            </div>
            {renderScriptPreview(selectedConfig.script, selectedConfig.boot_type)}
            <div className="mt-4 flex justify-end">
              <Button
                variant="secondary"
                onClick={() => {
                  setShowPreviewModal(false);
                  setSelectedConfig(null);
                }}
              >
                Close
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
