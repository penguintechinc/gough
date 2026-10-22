/**
 * Disk Plan Editor Component
 *
 * Partition editor with dark-drive toggle and drag-handle reordering.
 */

import React, { useState } from 'react';
import { Partition } from '../../types/provisioning';

interface DiskPlanEditorProps {
  machineId: string;
  partitions: Partition[];
  onSave: (partitions: Partition[]) => Promise<void>;
  isLoading?: boolean;
}

export const DiskPlanEditor: React.FC<DiskPlanEditorProps> = ({
  machineId,
  partitions: initialPartitions,
  onSave,
  isLoading = false,
}) => {
  const [partitions, setPartitions] = useState<Partition[]>(initialPartitions);
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  const handleDragStart = (index: number) => {
    setDraggedIndex(index);
  };

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault();
    if (draggedIndex !== null && draggedIndex !== index) {
      const newPartitions = [...partitions];
      [newPartitions[draggedIndex], newPartitions[index]] = [newPartitions[index], newPartitions[draggedIndex]];
      setPartitions(newPartitions);
      setDraggedIndex(index);
    }
  };

  const handleDragEnd = () => {
    setDraggedIndex(null);
  };

  const toggleDarkDrive = (index: number) => {
    const newPartitions = [...partitions];
    newPartitions[index].is_dark_drive = !newPartitions[index].is_dark_drive;
    setPartitions(newPartitions);
  };

  const handlePartitionChange = (index: number, field: keyof Partition, value: any) => {
    const newPartitions = [...partitions];
    newPartitions[index] = { ...newPartitions[index], [field]: value };
    setPartitions(newPartitions);
  };

  const handleSave = async () => {
    setIsSaving(true);
    try {
      await onSave(partitions);
    } finally {
      setIsSaving(false);
    }
  };

  const totalSize = partitions.reduce((sum, p) => sum + p.size_gb, 0);
  const darkDriveSize = partitions.filter((p) => p.is_dark_drive).reduce((sum, p) => sum + p.size_gb, 0);

  return (
    <div className="bg-dark-900 border border-dark-700 rounded-lg p-6">
      <div className="mb-6">
        <h3 className="text-lg font-semibold text-white mb-4">Disk Plan</h3>

        <div className="grid grid-cols-3 gap-4 mb-6">
          <div className="bg-dark-800 rounded p-3">
            <p className="text-sm text-dark-400">Total Size</p>
            <p className="text-xl font-semibold text-white">{totalSize} GB</p>
          </div>
          <div className="bg-dark-800 rounded p-3">
            <p className="text-sm text-dark-400">Dark Drives</p>
            <p className="text-xl font-semibold text-amber-400">{darkDriveSize} GB</p>
          </div>
          <div className="bg-dark-800 rounded p-3">
            <p className="text-sm text-dark-400">OS/System</p>
            <p className="text-xl font-semibold text-green-400">{totalSize - darkDriveSize} GB</p>
          </div>
        </div>
      </div>

      <div className="space-y-3">
        {partitions.map((partition, index) => (
          <div
            key={partition.device}
            draggable
            onDragStart={() => handleDragStart(index)}
            onDragOver={(e) => handleDragOver(e, index)}
            onDragEnd={handleDragEnd}
            className={`p-4 bg-dark-800 border border-dark-700 rounded cursor-grab active:cursor-grabbing transition-all ${
              draggedIndex === index ? 'opacity-50' : ''
            }`}
          >
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 items-end">
              <div>
                <label className="block text-xs text-dark-400 mb-1">Device</label>
                <input
                  type="text"
                  value={partition.device}
                  readOnly
                  className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-dark-300"
                />
              </div>

              <div>
                <label className="block text-xs text-dark-400 mb-1">Size (GB)</label>
                <input
                  type="number"
                  value={partition.size_gb}
                  onChange={(e) => handlePartitionChange(index, 'size_gb', parseInt(e.target.value))}
                  className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-white"
                />
              </div>

              <div>
                <label className="block text-xs text-dark-400 mb-1">FS Type</label>
                <select
                  value={partition.fstype}
                  onChange={(e) => handlePartitionChange(index, 'fstype', e.target.value)}
                  className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-white"
                >
                  <option>ext4</option>
                  <option>xfs</option>
                  <option>btrfs</option>
                </select>
              </div>

              <div>
                <label className="block text-xs text-dark-400 mb-1">Mount</label>
                <input
                  type="text"
                  value={partition.mount_point}
                  onChange={(e) => handlePartitionChange(index, 'mount_point', e.target.value)}
                  className="w-full px-2 py-1 bg-dark-700 border border-dark-600 rounded text-sm text-white"
                  placeholder="/mnt/data"
                />
              </div>

              <div className="flex items-end gap-2">
                <button
                  onClick={() => toggleDarkDrive(index)}
                  className={`flex-1 px-3 py-1 rounded text-sm font-medium transition-colors ${
                    partition.is_dark_drive
                      ? 'bg-amber-900/30 border border-amber-700 text-amber-300'
                      : 'bg-dark-700 border border-dark-600 text-dark-300 hover:bg-dark-600'
                  }`}
                >
                  {partition.is_dark_drive ? 'Dark' : 'Usable'}
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-6 flex gap-3">
        <button
          onClick={handleSave}
          disabled={isSaving || isLoading}
          className="px-4 py-2 bg-gold-600 hover:bg-gold-500 disabled:bg-dark-700 text-white font-medium rounded transition-colors"
        >
          {isSaving ? 'Saving...' : 'Save Plan'}
        </button>
        <button
          onClick={() => setPartitions(initialPartitions)}
          disabled={isSaving || isLoading}
          className="px-4 py-2 bg-dark-700 hover:bg-dark-600 text-white rounded transition-colors"
        >
          Reset
        </button>
      </div>
    </div>
  );
};

export default DiskPlanEditor;
