/**
 * Provisioning Types
 *
 * Type definitions for machines, biomes, clusters, and related entities.
 */

export interface Machine {
  id: string;
  hostname: string;
  state: 'new' | 'probed' | 'ready' | 'allocated' | 'deploying' | 'failed' | 'retired';
  phase?: 'phase-0' | 'phase-1' | 'phase-2';
  zone: string;
  pool: string;
  ip_address: string;
  mac_address?: string;
  created_at: string;
  last_seen?: string;
  tags?: string[];
  metadata?: Record<string, any>;
}

export interface HardwareInfo {
  cpu_model: string;
  cpu_vendor: string;
  cpu_cores: number;
  cpu_threads: number;
  memory_mb: number;
  numa_nodes?: number;
  disk_gb: number;
  network_interfaces: NetworkInterface[];
  pci_devices?: PCIDevice[];
  bmc_info?: BMCInfo;
  firmware?: FirmwareInfo;
}

export interface NetworkInterface {
  name: string;
  mac: string;
  speed?: string;
  mtu?: number;
  bondable?: boolean;
  status?: 'up' | 'down';
}

export interface PCIDevice {
  id: string;
  vendor: string;
  device: string;
  bus_type?: string;
}

export interface BMCInfo {
  vendor?: string;
  model?: string;
  firmware_version?: string;
  ip_address?: string;
}

export interface FirmwareInfo {
  bios_version?: string;
  bios_date?: string;
  secure_boot?: boolean;
}

export interface DiskPlan {
  id: string;
  machine_id: string;
  partitions: Partition[];
  created_at: string;
}

export interface Partition {
  device: string;
  size_gb: number;
  fstype: string;
  mount_point: string;
  is_dark_drive?: boolean;
  raid_level?: string;
}

export interface Biome {
  id: string;
  name: string;
  version: string;
  kind: 'compute' | 'storage' | 'infrastructure';
  phase: 'phase-0' | 'phase-1' | 'phase-2';
  workload_type: 'lxc' | 'qemu' | 'k8s';
  lock_to_host: boolean;
  requires_hardware_tags: string[];
  emits_joiner_secrets: boolean;
  description?: string;
  created_at: string;
  metadata?: Record<string, any>;
}

export interface EggAssignment {
  id: string;
  machine_id: string;
  biomeId: string;
  egg_name: string;
  egg_version: string;
  deployed_at?: string;
  state?: 'pending' | 'deploying' | 'ready' | 'failed';
}

export interface Cluster {
  id: string;
  name: string;
  state: 'bootstrap' | 'ready' | 'degraded' | 'down';
  nodes_total: number;
  nodes_healthy: number;
  storage_backend: 'ceph' | 'local' | 'iscsi';
  created_at: string;
}

export interface CapacityForecast {
  timestamp: string;
  horizon_days: number;
  nodes: NodeCapacity[];
  risk_level: 'low' | 'medium' | 'high';
}

export interface NodeCapacity {
  node_id: string;
  hostname: string;
  memory_percent_used: number;
  memory_forecast_7d: number;
  cpu_percent_used: number;
  disk_percent_used: number;
  risk_indicators: string[];
}

export interface MigrationPolicy {
  min_healthy_nodes: number;
  max_concurrent_migrations: number;
  require_target_capacity_headroom_mem_pct: number;
  require_target_capacity_headroom_cpu_pct: number;
}

export interface AuditEvent {
  id: string;
  timestamp: string;
  actor_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  changes?: Record<string, any>;
  chain_hash?: string;
  prev_chain_hash?: string;
}

export interface JoinerSecret {
  id: string;
  name: string;
  rotation_class: string;
  issued_at: string;
  expires_at?: string;
  metadata?: Record<string, any>;
}

export interface WebhookConfig {
  id: string;
  url: string;
  signing_mode: 'hmac' | 'jwks';
  events: string[];
  tenant_id: string;
  created_at: string;
}

export interface License {
  key: string;
  valid: boolean;
  features: string[];
  expires_at?: string;
  tier: 'free' | 'pro' | 'enterprise';
}
