#!/bin/sh
# initramfs-tools hook for Gough discovery-agent
#
# This hook is called by update-initramfs during kernel/initramfs builds
# It includes the discovery-agent binary and required hardware probe modules into the initrd
#
# Location: /usr/share/initramfs-tools/hooks/gough-discovery-agent
# Permissions: 0755

PREREQ=""

case $1 in
    prereqs) echo "$PREREQ"; exit 0 ;;
esac

. /usr/share/initramfs-tools/hook-functions

# Copy discovery-agent binary into the initramfs
if [ -f /usr/local/bin/discovery-agent ]; then
    copy_exec /usr/local/bin/discovery-agent /usr/local/bin
else
    echo "WARNING: discovery-agent binary not found; helper image may be incomplete"
fi

# Pre-load kernel modules required for hardware probing
# Network drivers (NICs)
manual_add_modules e1000e
manual_add_modules igb
manual_add_modules ixgbe
manual_add_modules mlx5_core
manual_add_modules virtio_net
manual_add_modules bnx2
manual_add_modules bnx2x

# Storage controllers (disk/NVME detection)
manual_add_modules nvme
manual_add_modules ahci
manual_add_modules sd_mod
manual_add_modules ata_generic
manual_add_modules ata_piix
manual_add_modules megaraid_sas
manual_add_modules mpt3sas
manual_add_modules lpfc
manual_add_modules qla2xxx

# Hardware monitoring
manual_add_modules ipmi_si
manual_add_modules ipmi_devintf
manual_add_modules dmi

# RAID
manual_add_modules raid0
manual_add_modules raid1
manual_add_modules raid10
manual_add_modules raid456

# Encryption (for LUKS probing)
manual_add_modules dm_crypt
manual_add_modules dm_mod

# TPM (for attestation and sealing)
manual_add_modules tpm
manual_add_modules tpm2

# Include cloud-init and required runtime dependencies
copy_exec /usr/bin/cloud-init /usr/bin
copy_exec /usr/lib/cloud-init/cloud-init-generator /usr/lib/cloud-init

# Copy OpenSSL dependencies for HTTPS/TLS communication
copy_exec /usr/bin/openssl /usr/bin

# Include curl for HTTP operations if needed
copy_exec /usr/bin/curl /usr/bin

# Include jq for JSON parsing (discovery-agent may output JSON)
copy_exec /usr/bin/jq /usr/bin

# Copy lshw binary for hardware inventory
copy_exec /usr/bin/lshw /usr/bin

# Copy smartctl for SMART probe
copy_exec /usr/sbin/smartctl /usr/sbin

# Include necessary libraries for the above binaries
copy_exec /lib/x86_64-linux-gnu/libc.so.6 /lib/x86_64-linux-gnu
copy_exec /lib/x86_64-linux-gnu/libm.so.6 /lib/x86_64-linux-gnu
copy_exec /lib/x86_64-linux-gnu/libdl.so.2 /lib/x86_64-linux-gnu
copy_exec /lib/x86_64-linux-gnu/libpthread.so.0 /lib/x86_64-linux-gnu

# Copy CA certificates for TLS verification
copy_dir /etc/ssl/certs /etc/ssl/certs

# Create mount points and directories expected by cloud-init
mkdir -p "${DESTDIR}/var/lib/cloud"
mkdir -p "${DESTDIR}/var/log/gough"
mkdir -p "${DESTDIR}/run/gough"

# Ensure discovery-agent is executable in the initrd
chmod 755 "${DESTDIR}/usr/local/bin/discovery-agent" 2>/dev/null || true

exit 0
