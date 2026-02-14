import { useState, useEffect } from 'react';
import Card from '../../components/Card';
import Button from '../../components/Button';
import TabNavigation from '../../components/TabNavigation';

interface BootImage {
  id: number;
  name: string;
  description: string;
  type: 'iso' | 'vmdk' | 'qcow2' | 'raw';
  size: number;
  storage_path: string;
  checksum: string;
  status: 'pending' | 'uploading' | 'ready' | 'failed';
  upload_progress?: number;
  created_at: string;
  updated_at: string | null;
}

export default function ImagesPage() {
  const [images, setImages] = useState<BootImage[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [activeTab, setActiveTab] = useState('all');

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadData, setUploadData] = useState({
    name: '',
    description: '',
    type: 'iso' as 'iso' | 'vmdk' | 'qcow2' | 'raw',
  });
  const [uploadProgress, setUploadProgress] = useState(0);
  const [isUploading, setIsUploading] = useState(false);

  const tabs = [
    { id: 'all', label: 'All Images' },
    { id: 'iso', label: 'ISO' },
    { id: 'vmdk', label: 'VMDK' },
    { id: 'qcow2', label: 'QCOW2' },
    { id: 'raw', label: 'Raw' },
  ];

  useEffect(() => {
    fetchImages();
    const interval = setInterval(fetchImages, 5000);
    return () => clearInterval(interval);
  }, []);

  const fetchImages = async () => {
    setIsLoading(true);
    try {
      const response = await fetch('/api/v1/provisioning/images', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to fetch images');
      const data = await response.json();
      setImages(data.items || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load images');
      setImages([]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      setUploadFile(file);
      if (!uploadData.name) {
        setUploadData(prev => ({ ...prev, name: file.name }));
      }
    }
  };

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadFile) {
      setError('Please select a file to upload');
      return;
    }

    setIsUploading(true);
    setUploadProgress(0);

    try {
      const formData = new FormData();
      formData.append('file', uploadFile);
      formData.append('name', uploadData.name);
      formData.append('description', uploadData.description);
      formData.append('type', uploadData.type);

      const xhr = new XMLHttpRequest();

      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
          const progress = Math.round((e.loaded / e.total) * 100);
          setUploadProgress(progress);
        }
      });

      xhr.addEventListener('load', () => {
        if (xhr.status === 200 || xhr.status === 201) {
          setShowUploadModal(false);
          setUploadFile(null);
          setUploadData({ name: '', description: '', type: 'iso' });
          setUploadProgress(0);
          fetchImages();
        } else {
          setError('Upload failed: ' + xhr.statusText);
        }
        setIsUploading(false);
      });

      xhr.addEventListener('error', () => {
        setError('Upload failed: Network error');
        setIsUploading(false);
      });

      xhr.open('POST', '/api/v1/provisioning/images/upload');
      xhr.setRequestHeader('Authorization', `Bearer ${localStorage.getItem('access_token')}`);
      xhr.send(formData);

    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload image');
      setIsUploading(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this boot image?')) return;
    try {
      const response = await fetch(`/api/v1/provisioning/images/${id}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to delete image');
      fetchImages();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete image');
    }
  };

  const formatBytes = (bytes: number): string => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
  };

  const getStatusBadge = (status: string) => {
    const colors = {
      pending: 'bg-yellow-900/50 text-yellow-400',
      uploading: 'bg-blue-900/50 text-blue-400',
      ready: 'bg-green-900/50 text-green-400',
      failed: 'bg-red-900/50 text-red-400',
    };
    return colors[status as keyof typeof colors] || 'bg-gray-900/50 text-gray-400';
  };

  const getTypeBadge = (type: string) => {
    const colors = {
      iso: 'bg-purple-900/50 text-purple-400',
      vmdk: 'bg-blue-900/50 text-blue-400',
      qcow2: 'bg-cyan-900/50 text-cyan-400',
      raw: 'bg-orange-900/50 text-orange-400',
    };
    return colors[type as keyof typeof colors] || 'bg-gray-900/50 text-gray-400';
  };

  const filteredImages = activeTab === 'all'
    ? images
    : images.filter(image => image.type === activeTab);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gold-400">Boot Image Management</h1>
          <p className="text-dark-400 mt-1">Upload and manage OS images for provisioning</p>
        </div>
        <Button onClick={() => setShowUploadModal(true)}>+ Upload Image</Button>
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
                <div key={i} className="h-24 bg-dark-700 rounded"></div>
              ))}
            </div>
          ) : filteredImages.length === 0 ? (
            <div className="text-center py-8 text-dark-400">
              <p>No boot images found</p>
              <Button onClick={() => setShowUploadModal(true)} className="mt-4">
                Upload your first image
              </Button>
            </div>
          ) : (
            <div className="space-y-4">
              {filteredImages.map((image) => (
                <div key={image.id} className="border border-dark-700 rounded-lg p-4 hover:border-gold-500/30 transition-colors">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3">
                        <h3 className="text-lg font-semibold text-gold-400">{image.name}</h3>
                        <span className={`badge ${getTypeBadge(image.type)}`}>{image.type.toUpperCase()}</span>
                        <span className={`badge ${getStatusBadge(image.status)}`}>{image.status}</span>
                      </div>
                      <p className="text-dark-300 mt-1">{image.description}</p>
                      <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                        <div>
                          <span className="text-dark-500">Size:</span>
                          <p className="text-gold-400">{formatBytes(image.size)}</p>
                        </div>
                        <div>
                          <span className="text-dark-500">Storage Path:</span>
                          <p className="text-dark-400 font-mono text-xs truncate">{image.storage_path}</p>
                        </div>
                        <div>
                          <span className="text-dark-500">Checksum:</span>
                          <p className="text-dark-400 font-mono text-xs truncate">{image.checksum || 'N/A'}</p>
                        </div>
                        <div>
                          <span className="text-dark-500">Uploaded:</span>
                          <p className="text-dark-400 text-xs">{new Date(image.created_at).toLocaleString()}</p>
                        </div>
                      </div>
                      {image.status === 'uploading' && image.upload_progress !== undefined && (
                        <div className="mt-3">
                          <div className="flex items-center justify-between text-xs text-dark-400 mb-1">
                            <span>Upload Progress</span>
                            <span>{image.upload_progress}%</span>
                          </div>
                          <div className="w-full bg-dark-900 rounded-full h-2">
                            <div
                              className="bg-gold-500 h-2 rounded-full transition-all duration-300"
                              style={{ width: `${image.upload_progress}%` }}
                            ></div>
                          </div>
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-2 ml-4">
                      {image.status === 'ready' && (
                        <button
                          onClick={() => handleDelete(image.id)}
                          className="text-red-400 hover:text-red-300"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {showUploadModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="card w-full max-w-2xl">
            <h2 className="text-xl font-bold text-gold-400 mb-4">Upload Boot Image</h2>
            <form onSubmit={handleUpload} className="space-y-4">
              <div>
                <label className="block text-sm text-dark-400 mb-1">Image Name</label>
                <input
                  type="text"
                  value={uploadData.name}
                  onChange={(e) => setUploadData({ ...uploadData, name: e.target.value })}
                  className="input"
                  required
                  placeholder="ubuntu-22.04-server"
                />
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-1">Description</label>
                <input
                  type="text"
                  value={uploadData.description}
                  onChange={(e) => setUploadData({ ...uploadData, description: e.target.value })}
                  className="input"
                  required
                  placeholder="Ubuntu 22.04 LTS Server Installation"
                />
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-1">Image Type</label>
                <select
                  value={uploadData.type}
                  onChange={(e) => setUploadData({ ...uploadData, type: e.target.value as 'iso' | 'vmdk' | 'qcow2' | 'raw' })}
                  className="input"
                >
                  <option value="iso">ISO Image</option>
                  <option value="vmdk">VMDK (VMware)</option>
                  <option value="qcow2">QCOW2 (QEMU)</option>
                  <option value="raw">Raw Disk Image</option>
                </select>
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-1">Select File</label>
                <input
                  type="file"
                  onChange={handleFileChange}
                  className="input"
                  required
                  accept=".iso,.vmdk,.qcow2,.img,.raw"
                />
                {uploadFile && (
                  <p className="text-xs text-dark-400 mt-2">
                    Selected: {uploadFile.name} ({formatBytes(uploadFile.size)})
                  </p>
                )}
              </div>
              {isUploading && (
                <div>
                  <div className="flex items-center justify-between text-xs text-dark-400 mb-1">
                    <span>Upload Progress</span>
                    <span>{uploadProgress}%</span>
                  </div>
                  <div className="w-full bg-dark-900 rounded-full h-2">
                    <div
                      className="bg-gold-500 h-2 rounded-full transition-all duration-300"
                      style={{ width: `${uploadProgress}%` }}
                    ></div>
                  </div>
                </div>
              )}
              <div className="flex justify-end gap-3 mt-6">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    setShowUploadModal(false);
                    setUploadFile(null);
                    setUploadData({ name: '', description: '', type: 'iso' });
                  }}
                  disabled={isUploading}
                >
                  Cancel
                </Button>
                <Button type="submit" isLoading={isUploading}>
                  Upload Image
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
