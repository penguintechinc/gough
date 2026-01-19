import { useState, useCallback } from 'react';
import api from '../lib/api';
import type { PaginatedResponse } from '../types';

// ============================================================================
// Types
// ============================================================================

export interface Machine {
  id: number;
  system_id: string;
  hostname: string;
  mac_address: string;
  ip_address: string | null;
  status: 'unknown' | 'discovered' | 'commissioning' | 'ready' | 'deploying' | 'deployed' | 'failed';
  zone: string | null;
  pool: string | null;
  architecture: string;
  cpu_count: number;
  ram_mb: number;
  disk_gb: number;
  power_type: string | null;
  power_address: string | null;
  provisioning_status: string | null;
  error_message: string | null;
  last_heartbeat: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface Egg {
  id: number;
  name: string;
  display_name: string;
  description: string;
  egg_type: 'snap' | 'cloud_init' | 'lxd_container' | 'lxd_vm';
  version: string;
  category: string;
  snap_name: string | null;
  snap_channel: string | null;
  snap_classic: boolean;
  cloud_init_content: string | null;
  lxd_image_alias: string | null;
  lxd_image_url: string | null;
  lxd_profiles: string[];
  is_hypervisor_config: boolean;
  dependencies: number[];
  min_ram_mb: number;
  min_disk_gb: number;
  required_architecture: string;
  is_active: boolean;
  is_default: boolean;
  checksum: string | null;
  size_bytes: number | null;
  created_at: string;
  updated_at: string | null;
}

export interface EggGroup {
  id: number;
  name: string;
  display_name: string;
  description: string;
  eggs: number[];
  is_default: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface Image {
  id: number;
  name: string;
  display_name: string;
  description: string;
  image_type: 'ubuntu' | 'debian' | 'custom';
  version: string;
  architecture: string;
  kernel_name: string;
  initrd_name: string;
  squashfs_name: string;
  size_mb: number;
  checksum: string;
  is_active: boolean;
  is_default: boolean;
  release_date: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface BootConfig {
  id: number;
  name: string;
  display_name: string;
  description: string;
  boot_type: 'uefi' | 'bios' | 'both';
  kernel_params: string;
  ipxe_script: string;
  is_active: boolean;
  is_default: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface Deployment {
  id: string;
  machine_id: number;
  image_id: number;
  boot_config_id: number | null;
  eggs_to_deploy: number[];
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'cancelled';
  progress_percent: number;
  created_by: number;
  started_at: string;
  completed_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface Storage {
  id: number;
  name: string;
  display_name: string;
  storage_type: 'minio' | 's3' | 'local' | 'ceph';
  endpoint: string;
  bucket: string;
  access_key: string;
  secret_key: string;
  ssl_verify: boolean;
  total_size_gb: number;
  used_size_gb: number;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface IPxeConfig {
  id: number;
  name: string;
  dhcp_mode: 'full' | 'proxy' | 'disabled';
  dhcp_interface: string | null;
  dhcp_subnet: string | null;
  dhcp_range_start: string | null;
  dhcp_range_end: string | null;
  dhcp_gateway: string | null;
  dns_servers: string[];
  tftp_enabled: boolean;
  http_boot_url: string;
  minio_bucket: string;
  default_boot_script: string;
  chain_url: string;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface CreateMachineData {
  hostname: string;
  mac_address: string;
  architecture: string;
  cpu_count: number;
  ram_mb: number;
  disk_gb: number;
  zone?: string;
  pool?: string;
  power_type?: string;
  power_address?: string;
}

export interface UpdateMachineData {
  hostname?: string;
  zone?: string;
  pool?: string;
  power_type?: string;
  power_address?: string;
}

export interface CreateEggData {
  name: string;
  display_name: string;
  description?: string;
  egg_type: 'snap' | 'cloud_init' | 'lxd_container' | 'lxd_vm';
  version?: string;
  category?: string;
  snap_name?: string;
  snap_channel?: string;
  snap_classic?: boolean;
  cloud_init_content?: string;
  lxd_image_alias?: string;
  lxd_image_url?: string;
  lxd_profiles?: string[];
  is_hypervisor_config?: boolean;
  dependencies?: number[];
  min_ram_mb?: number;
  min_disk_gb?: number;
  required_architecture?: string;
  is_active?: boolean;
  is_default?: boolean;
}

export interface UpdateEggData {
  display_name?: string;
  description?: string;
  version?: string;
  category?: string;
  cloud_init_content?: string;
  lxd_profiles?: string[];
  dependencies?: number[];
  min_ram_mb?: number;
  min_disk_gb?: number;
  is_active?: boolean;
  is_default?: boolean;
}

export interface CreateEggGroupData {
  name: string;
  display_name: string;
  description?: string;
  eggs: number[];
  is_default?: boolean;
}

export interface UpdateEggGroupData {
  display_name?: string;
  description?: string;
  eggs?: number[];
  is_default?: boolean;
}

export interface CreateImageData {
  name: string;
  display_name: string;
  description?: string;
  image_type: 'ubuntu' | 'debian' | 'custom';
  version: string;
  architecture: string;
  kernel_name: string;
  initrd_name: string;
  squashfs_name: string;
  size_mb: number;
  checksum: string;
  is_active?: boolean;
  is_default?: boolean;
  release_date?: string;
}

export interface UpdateImageData {
  display_name?: string;
  description?: string;
  is_active?: boolean;
  is_default?: boolean;
}

export interface CreateBootConfigData {
  name: string;
  display_name: string;
  description?: string;
  boot_type: 'uefi' | 'bios' | 'both';
  kernel_params?: string;
  ipxe_script: string;
  is_active?: boolean;
  is_default?: boolean;
}

export interface UpdateBootConfigData {
  display_name?: string;
  description?: string;
  kernel_params?: string;
  ipxe_script?: string;
  is_active?: boolean;
  is_default?: boolean;
}

export interface UpdateIPxeConfigData {
  name?: string;
  dhcp_mode?: 'full' | 'proxy' | 'disabled';
  dhcp_interface?: string;
  dhcp_subnet?: string;
  dhcp_range_start?: string;
  dhcp_range_end?: string;
  dhcp_gateway?: string;
  dns_servers?: string[];
  tftp_enabled?: boolean;
  http_boot_url?: string;
  minio_bucket?: string;
  default_boot_script?: string;
  chain_url?: string;
  is_active?: boolean;
}

export interface DeploymentData {
  image_id: number;
  boot_config_id?: number;
  eggs_to_deploy?: number[];
}

export interface PowerActionData {
  action: 'on' | 'off' | 'reboot' | 'soft_reboot';
  force?: boolean;
}

// ============================================================================
// Machines API
// ============================================================================

export const machinesApi = {
  list: async (filters?: {
    status?: string;
    zone?: string;
    pool?: string;
  }): Promise<{ machines: Machine[]; total: number }> => {
    const params = new URLSearchParams();
    if (filters?.status) params.append('status', filters.status);
    if (filters?.zone) params.append('zone', filters.zone);
    if (filters?.pool) params.append('pool', filters.pool);

    const response = await api.get('/ipxe/machines', {
      params: Object.fromEntries(params),
    });
    return response.data;
  },

  get: async (id: number | string): Promise<Machine> => {
    const response = await api.get(`/ipxe/machines/${id}`);
    return response.data;
  },

  create: async (data: CreateMachineData): Promise<Machine> => {
    const response = await api.post('/ipxe/machines', data);
    return response.data;
  },

  update: async (id: number | string, data: UpdateMachineData): Promise<Machine> => {
    const response = await api.put(`/ipxe/machines/${id}`, data);
    return response.data;
  },

  commission: async (id: number | string): Promise<{ job_id: string }> => {
    const response = await api.post(`/ipxe/machines/${id}/commission`);
    return response.data;
  },

  deploy: async (id: number | string, data: DeploymentData): Promise<Deployment> => {
    const response = await api.post(`/ipxe/machines/${id}/deploy`, data);
    return response.data;
  },

  release: async (id: number | string): Promise<Machine> => {
    const response = await api.post(`/ipxe/machines/${id}/release`);
    return response.data;
  },

  power: async (id: number | string, data: PowerActionData): Promise<{ status: string }> => {
    const response = await api.post(`/ipxe/machines/${id}/power`, data);
    return response.data;
  },

  assignEggs: async (id: number | string, eggIds: number[]): Promise<Machine> => {
    const response = await api.post(`/ipxe/machines/${id}/eggs`, {
      egg_ids: eggIds,
    });
    return response.data;
  },

  delete: async (id: number | string): Promise<void> => {
    await api.delete(`/ipxe/machines/${id}`);
  },
};

// ============================================================================
// Eggs API
// ============================================================================

export const eggsApi = {
  list: async (filters?: {
    type?: string;
    category?: string;
    is_active?: boolean;
    is_default?: boolean;
  }): Promise<{ eggs: Egg[]; total: number }> => {
    const params = new URLSearchParams();
    if (filters?.type) params.append('type', filters.type);
    if (filters?.category) params.append('category', filters.category);
    if (filters?.is_active !== undefined) params.append('is_active', String(filters.is_active));
    if (filters?.is_default !== undefined) params.append('is_default', String(filters.is_default));

    const response = await api.get('/eggs', {
      params: Object.fromEntries(params),
    });
    return response.data;
  },

  get: async (id: number): Promise<Egg> => {
    const response = await api.get(`/eggs/${id}`);
    return response.data;
  },

  create: async (data: CreateEggData): Promise<Egg> => {
    const response = await api.post('/eggs', data);
    return response.data;
  },

  update: async (id: number, data: UpdateEggData): Promise<Egg> => {
    const response = await api.put(`/eggs/${id}`, data);
    return response.data;
  },

  delete: async (id: number): Promise<void> => {
    await api.delete(`/eggs/${id}`);
  },

  render: async (eggIds: number[]): Promise<{ cloud_init: string }> => {
    const response = await api.post('/eggs/render', {
      egg_ids: eggIds,
    });
    return response.data;
  },
};

// ============================================================================
// Egg Groups API
// ============================================================================

export const eggGroupsApi = {
  list: async (): Promise<{ egg_groups: EggGroup[]; total: number }> => {
    const response = await api.get('/eggs/groups');
    return response.data;
  },

  get: async (id: number): Promise<EggGroup> => {
    const response = await api.get(`/eggs/groups/${id}`);
    return response.data;
  },

  create: async (data: CreateEggGroupData): Promise<EggGroup> => {
    const response = await api.post('/eggs/groups', data);
    return response.data;
  },

  update: async (id: number, data: UpdateEggGroupData): Promise<EggGroup> => {
    const response = await api.put(`/eggs/groups/${id}`, data);
    return response.data;
  },

  delete: async (id: number): Promise<void> => {
    await api.delete(`/eggs/groups/${id}`);
  },
};

// ============================================================================
// Images API
// ============================================================================

export const imagesApi = {
  list: async (filters?: {
    architecture?: string;
    is_active?: boolean;
    is_default?: boolean;
  }): Promise<{ images: Image[]; total: number }> => {
    const params = new URLSearchParams();
    if (filters?.architecture) params.append('architecture', filters.architecture);
    if (filters?.is_active !== undefined) params.append('is_active', String(filters.is_active));
    if (filters?.is_default !== undefined) params.append('is_default', String(filters.is_default));

    const response = await api.get('/ipxe/images', {
      params: Object.fromEntries(params),
    });
    return response.data;
  },

  get: async (id: number): Promise<Image> => {
    const response = await api.get(`/ipxe/images/${id}`);
    return response.data;
  },

  create: async (data: CreateImageData): Promise<Image> => {
    const response = await api.post('/ipxe/images', data);
    return response.data;
  },

  update: async (id: number, data: UpdateImageData): Promise<Image> => {
    const response = await api.put(`/ipxe/images/${id}`, data);
    return response.data;
  },

  delete: async (id: number): Promise<void> => {
    await api.delete(`/ipxe/images/${id}`);
  },
};

// ============================================================================
// Boot Configs API
// ============================================================================

export const bootConfigsApi = {
  list: async (filters?: {
    boot_type?: string;
    is_active?: boolean;
    is_default?: boolean;
  }): Promise<{ boot_configs: BootConfig[]; total: number }> => {
    const params = new URLSearchParams();
    if (filters?.boot_type) params.append('boot_type', filters.boot_type);
    if (filters?.is_active !== undefined) params.append('is_active', String(filters.is_active));
    if (filters?.is_default !== undefined) params.append('is_default', String(filters.is_default));

    const response = await api.get('/ipxe/boot-configs', {
      params: Object.fromEntries(params),
    });
    return response.data;
  },

  get: async (id: number): Promise<BootConfig> => {
    const response = await api.get(`/ipxe/boot-configs/${id}`);
    return response.data;
  },

  create: async (data: CreateBootConfigData): Promise<BootConfig> => {
    const response = await api.post('/ipxe/boot-configs', data);
    return response.data;
  },

  update: async (id: number, data: UpdateBootConfigData): Promise<BootConfig> => {
    const response = await api.put(`/ipxe/boot-configs/${id}`, data);
    return response.data;
  },

  delete: async (id: number): Promise<void> => {
    await api.delete(`/ipxe/boot-configs/${id}`);
  },
};

// ============================================================================
// Deployments API
// ============================================================================

export const deploymentsApi = {
  list: async (filters?: {
    status?: string;
    machine_id?: number;
  }): Promise<{ deployments: Deployment[]; total: number }> => {
    const params = new URLSearchParams();
    if (filters?.status) params.append('status', filters.status);
    if (filters?.machine_id) params.append('machine_id', String(filters.machine_id));

    const response = await api.get('/ipxe/deployments', {
      params: Object.fromEntries(params),
    });
    return response.data;
  },

  get: async (jobId: string): Promise<Deployment> => {
    const response = await api.get(`/ipxe/deployments/${jobId}`);
    return response.data;
  },

  cancel: async (jobId: string): Promise<Deployment> => {
    const response = await api.post(`/ipxe/deployments/${jobId}/cancel`);
    return response.data;
  },

  retry: async (jobId: string): Promise<Deployment> => {
    const response = await api.post(`/ipxe/deployments/${jobId}/retry`);
    return response.data;
  },
};

// ============================================================================
// Storage API
// ============================================================================

export const storageApi = {
  list: async (): Promise<{ storage: Storage[]; total: number }> => {
    const response = await api.get('/storage');
    return response.data;
  },

  get: async (id: number): Promise<Storage> => {
    const response = await api.get(`/storage/${id}`);
    return response.data;
  },

  create: async (data: Omit<Storage, 'id' | 'used_size_gb' | 'created_at' | 'updated_at'>): Promise<Storage> => {
    const response = await api.post('/storage', data);
    return response.data;
  },

  update: async (id: number, data: Partial<Storage>): Promise<Storage> => {
    const response = await api.put(`/storage/${id}`, data);
    return response.data;
  },

  delete: async (id: number): Promise<void> => {
    await api.delete(`/storage/${id}`);
  },

  testConnection: async (id: number): Promise<{ status: 'connected' | 'failed'; message: string }> => {
    const response = await api.post(`/storage/${id}/test`);
    return response.data;
  },
};

// ============================================================================
// iPXE Config API
// ============================================================================

export const ipxeConfigApi = {
  get: async (): Promise<IPxeConfig> => {
    const response = await api.get('/ipxe/config');
    return response.data;
  },

  update: async (data: UpdateIPxeConfigData): Promise<IPxeConfig> => {
    const response = await api.put('/ipxe/config', data);
    return response.data;
  },

  getBootScript: async (): Promise<{ script: string }> => {
    const response = await api.get('/ipxe/config/boot-script');
    return response.data;
  },

  updateBootScript: async (script: string): Promise<{ script: string }> => {
    const response = await api.put('/ipxe/config/boot-script', { script });
    return response.data;
  },
};

// ============================================================================
// Generic Provisioning Hook
// ============================================================================

export function useProvisioningApi<T>() {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const execute = useCallback(async (apiCall: () => Promise<T>) => {
    setIsLoading(true);
    setError(null);
    try {
      const result = await apiCall();
      setData(result);
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : 'An error occurred';
      setError(message);
      throw err;
    } finally {
      setIsLoading(false);
    }
  }, []);

  return { data, error, isLoading, execute, setData };
}
