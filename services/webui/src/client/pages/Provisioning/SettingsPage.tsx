/**
 * SettingsPage - iPXE and DHCP Configuration
 *
 * Provides interface for configuring iPXE boot settings and DHCP options.
 */

import React, { useState, useEffect, useCallback } from 'react';
import api from '../../lib/api';
import Card from '../../components/Card';
import TabNavigation from '../../components/TabNavigation';

interface iPXESettings {
  boot_timeout: number;
  default_kernel_args: string;
  enable_secure_boot: boolean;
  tftp_server: string;
  http_server: string;
}

interface DHCPSettings {
  enabled: boolean;
  lease_time: number;
  default_gateway: string;
  dns_servers: string;
  ntp_servers: string;
  domain_name: string;
  next_server: string;
  boot_file_name: string;
}

/**
 * Main provisioning settings page.
 */
export const SettingsPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('ipxe');
  const [ipxeSettings, setIpxeSettings] = useState<iPXESettings>({
    boot_timeout: 10,
    default_kernel_args: 'quiet splash',
    enable_secure_boot: false,
    tftp_server: '',
    http_server: '',
  });
  const [dhcpSettings, setDhcpSettings] = useState<DHCPSettings>({
    enabled: true,
    lease_time: 86400,
    default_gateway: '',
    dns_servers: '',
    ntp_servers: '',
    domain_name: '',
    next_server: '',
    boot_file_name: 'undionly.kpxe',
  });
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const tabs = [
    { id: 'ipxe', label: 'iPXE Settings' },
    { id: 'dhcp', label: 'DHCP Settings' },
    { id: 'advanced', label: 'Advanced' },
  ];

  // Fetch settings
  const fetchSettings = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const [ipxeResp, dhcpResp] = await Promise.all([
        api.get('/provisioning/settings/ipxe'),
        api.get('/provisioning/settings/dhcp'),
      ]);
      setIpxeSettings(ipxeResp.data || ipxeSettings);
      setDhcpSettings(dhcpResp.data || dhcpSettings);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to fetch settings';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSettings();
  }, [fetchSettings]);

  // Save iPXE settings
  const saveIpxeSettings = useCallback(async () => {
    setIsSaving(true);
    setError(null);
    setSuccess(null);

    try {
      await api.put('/provisioning/settings/ipxe', ipxeSettings);
      setSuccess('iPXE settings saved successfully');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to save iPXE settings';
      setError(message);
    } finally {
      setIsSaving(false);
    }
  }, [ipxeSettings]);

  // Save DHCP settings
  const saveDhcpSettings = useCallback(async () => {
    setIsSaving(true);
    setError(null);
    setSuccess(null);

    try {
      await api.put('/provisioning/settings/dhcp', dhcpSettings);
      setSuccess('DHCP settings saved successfully');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to save DHCP settings';
      setError(message);
    } finally {
      setIsSaving(false);
    }
  }, [dhcpSettings]);

  // Test DHCP configuration
  const testDhcpConfig = useCallback(async () => {
    setError(null);
    setSuccess(null);

    try {
      const response = await api.post('/provisioning/settings/dhcp/test');
      setSuccess(response.data.message || 'DHCP configuration is valid');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: any) {
      const message = err.response?.data?.error || 'DHCP configuration test failed';
      setError(message);
    }
  }, []);

  // Render iPXE settings
  const renderIpxeSettings = () => (
    <Card title="iPXE Boot Configuration">
      <div className="space-y-6">
        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">Boot Timeout (seconds)</span>
            <input
              type="number"
              className="input"
              value={ipxeSettings.boot_timeout}
              onChange={(e) => setIpxeSettings({ ...ipxeSettings, boot_timeout: parseInt(e.target.value) || 10 })}
              min="1"
              max="300"
            />
            <span className="text-sm text-dark-400 mt-1 block">
              Time to wait before booting default option
            </span>
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">Default Kernel Arguments</span>
            <input
              type="text"
              className="input"
              value={ipxeSettings.default_kernel_args}
              onChange={(e) => setIpxeSettings({ ...ipxeSettings, default_kernel_args: e.target.value })}
              placeholder="quiet splash"
            />
            <span className="text-sm text-dark-400 mt-1 block">
              Arguments passed to the kernel on boot
            </span>
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">TFTP Server</span>
            <input
              type="text"
              className="input"
              value={ipxeSettings.tftp_server}
              onChange={(e) => setIpxeSettings({ ...ipxeSettings, tftp_server: e.target.value })}
              placeholder="tftp://192.168.1.1"
            />
            <span className="text-sm text-dark-400 mt-1 block">
              TFTP server address for PXE boot files
            </span>
          </label>
        </div>

        <div>
          <label className="block">
            <span className="text-gold-400 block mb-2">HTTP Server</span>
            <input
              type="text"
              className="input"
              value={ipxeSettings.http_server}
              onChange={(e) => setIpxeSettings({ ...ipxeSettings, http_server: e.target.value })}
              placeholder="http://192.168.1.1:8080"
            />
            <span className="text-sm text-dark-400 mt-1 block">
              HTTP server for boot images and scripts
            </span>
          </label>
        </div>

        <div>
          <label className="flex items-center justify-between">
            <div>
              <span className="text-gold-400 block">Enable Secure Boot</span>
              <span className="text-sm text-dark-400">Require signed boot images</span>
            </div>
            <input
              type="checkbox"
              className="w-5 h-5"
              checked={ipxeSettings.enable_secure_boot}
              onChange={(e) => setIpxeSettings({ ...ipxeSettings, enable_secure_boot: e.target.checked })}
            />
          </label>
        </div>

        <div className="pt-4 border-t border-dark-700">
          <button
            onClick={saveIpxeSettings}
            disabled={isSaving}
            className="px-4 py-2 bg-gold-600 hover:bg-gold-500 text-dark-900
                       font-medium rounded transition-colors disabled:opacity-50"
          >
            {isSaving ? 'Saving...' : 'Save iPXE Settings'}
          </button>
        </div>
      </div>
    </Card>
  );

  // Render DHCP settings
  const renderDhcpSettings = () => (
    <div className="space-y-6">
      <Card title="DHCP Server Configuration">
        <div className="space-y-6">
          <div>
            <label className="flex items-center justify-between">
              <div>
                <span className="text-gold-400 block">Enable DHCP Server</span>
                <span className="text-sm text-dark-400">Enable built-in DHCP server</span>
              </div>
              <input
                type="checkbox"
                className="w-5 h-5"
                checked={dhcpSettings.enabled}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, enabled: e.target.checked })}
              />
            </label>
          </div>

          <div>
            <label className="block">
              <span className="text-gold-400 block mb-2">Lease Time (seconds)</span>
              <input
                type="number"
                className="input"
                value={dhcpSettings.lease_time}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, lease_time: parseInt(e.target.value) || 86400 })}
                min="300"
                max="604800"
              />
              <span className="text-sm text-dark-400 mt-1 block">
                Default: 86400 (24 hours)
              </span>
            </label>
          </div>

          <div>
            <label className="block">
              <span className="text-gold-400 block mb-2">Default Gateway</span>
              <input
                type="text"
                className="input"
                value={dhcpSettings.default_gateway}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, default_gateway: e.target.value })}
                placeholder="192.168.1.1"
              />
            </label>
          </div>

          <div>
            <label className="block">
              <span className="text-gold-400 block mb-2">DNS Servers</span>
              <input
                type="text"
                className="input"
                value={dhcpSettings.dns_servers}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, dns_servers: e.target.value })}
                placeholder="8.8.8.8, 8.8.4.4"
              />
              <span className="text-sm text-dark-400 mt-1 block">
                Comma-separated list of DNS servers
              </span>
            </label>
          </div>

          <div>
            <label className="block">
              <span className="text-gold-400 block mb-2">NTP Servers</span>
              <input
                type="text"
                className="input"
                value={dhcpSettings.ntp_servers}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, ntp_servers: e.target.value })}
                placeholder="pool.ntp.org"
              />
              <span className="text-sm text-dark-400 mt-1 block">
                Comma-separated list of NTP servers
              </span>
            </label>
          </div>

          <div>
            <label className="block">
              <span className="text-gold-400 block mb-2">Domain Name</span>
              <input
                type="text"
                className="input"
                value={dhcpSettings.domain_name}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, domain_name: e.target.value })}
                placeholder="example.com"
              />
            </label>
          </div>
        </div>
      </Card>

      <Card title="PXE Boot Options">
        <div className="space-y-6">
          <div>
            <label className="block">
              <span className="text-gold-400 block mb-2">Next Server (TFTP)</span>
              <input
                type="text"
                className="input"
                value={dhcpSettings.next_server}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, next_server: e.target.value })}
                placeholder="192.168.1.1"
              />
              <span className="text-sm text-dark-400 mt-1 block">
                DHCP Option 66: TFTP server address
              </span>
            </label>
          </div>

          <div>
            <label className="block">
              <span className="text-gold-400 block mb-2">Boot File Name</span>
              <input
                type="text"
                className="input"
                value={dhcpSettings.boot_file_name}
                onChange={(e) => setDhcpSettings({ ...dhcpSettings, boot_file_name: e.target.value })}
                placeholder="undionly.kpxe"
              />
              <span className="text-sm text-dark-400 mt-1 block">
                DHCP Option 67: PXE boot file
              </span>
            </label>
          </div>

          <div className="pt-4 border-t border-dark-700 flex gap-3">
            <button
              onClick={saveDhcpSettings}
              disabled={isSaving}
              className="px-4 py-2 bg-gold-600 hover:bg-gold-500 text-dark-900
                         font-medium rounded transition-colors disabled:opacity-50"
            >
              {isSaving ? 'Saving...' : 'Save DHCP Settings'}
            </button>
            <button
              onClick={testDhcpConfig}
              className="px-4 py-2 bg-dark-800 hover:bg-dark-700 text-gold-400
                         border border-dark-700 rounded transition-colors"
            >
              Test Configuration
            </button>
          </div>
        </div>
      </Card>
    </div>
  );

  // Render advanced settings
  const renderAdvancedSettings = () => (
    <Card title="Advanced Settings">
      <div className="space-y-6">
        <div className="p-4 bg-yellow-900/20 border border-yellow-700/50 rounded">
          <div className="flex items-start gap-3">
            <svg className="h-5 w-5 text-yellow-500 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            <div>
              <div className="text-yellow-400 font-medium mb-1">Advanced Configuration</div>
              <div className="text-sm text-yellow-300/80">
                These settings should only be modified by experienced administrators.
                Incorrect configuration may prevent systems from booting.
              </div>
            </div>
          </div>
        </div>

        <div>
          <h3 className="text-gold-400 font-medium mb-3">Boot Chain Configuration</h3>
          <div className="text-sm text-dark-400 space-y-2">
            <p>BIOS Chain: undionly.kpxe → iPXE script → Kernel</p>
            <p>UEFI Chain: snponly.efi → iPXE script → Kernel</p>
          </div>
        </div>

        <div>
          <h3 className="text-gold-400 font-medium mb-3">Network Boot Process</h3>
          <ol className="text-sm text-dark-400 space-y-2 list-decimal list-inside">
            <li>Client broadcasts DHCP discover</li>
            <li>Server responds with IP and boot parameters</li>
            <li>Client downloads boot file via TFTP</li>
            <li>iPXE loads and executes boot script</li>
            <li>Kernel and initrd downloaded via HTTP</li>
            <li>System boots into provisioning environment</li>
          </ol>
        </div>

        <div className="pt-4 border-t border-dark-700">
          <button
            onClick={fetchSettings}
            className="px-4 py-2 bg-dark-800 hover:bg-dark-700 text-gold-400
                       border border-dark-700 rounded transition-colors"
          >
            Reset to Defaults
          </button>
        </div>
      </div>
    </Card>
  );

  if (isLoading) {
    return (
      <div className="min-h-screen bg-dark-950 text-white p-6">
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-500" />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-dark-950 text-white p-6">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gold-500">Provisioning Settings</h1>
        <p className="text-dark-400 mt-1">Configure iPXE and DHCP settings</p>
      </div>

      {/* Success Message */}
      {success && (
        <div className="mb-6 p-4 bg-green-900/30 border border-green-700 rounded-lg flex items-center gap-3">
          <svg className="h-5 w-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span className="text-green-300">{success}</span>
        </div>
      )}

      {/* Error Message */}
      {error && (
        <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-3">
          <svg className="h-5 w-5 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span className="text-red-300">{error}</span>
          <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-300">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      )}

      {/* Tab Navigation */}
      <TabNavigation tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />

      {/* Content */}
      <div className="mt-6">
        {activeTab === 'ipxe' && renderIpxeSettings()}
        {activeTab === 'dhcp' && renderDhcpSettings()}
        {activeTab === 'advanced' && renderAdvancedSettings()}
      </div>
    </div>
  );
};

export default SettingsPage;
