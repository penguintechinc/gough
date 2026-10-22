#!/bin/bash
# launch-sim.sh — QEMU VM launcher for Gough node-sim tests
# Supports --firmware, --ipv6, --secure-boot, --dmi-uuid, --smart-pattern, --with-bmc

set -e

FIRMWARE="bios"
IPV6_MODE="disabled"
SECURE_BOOT="false"
DMI_UUID=""
SMART_PATTERN="healthy"
WITH_BMC="false"
VM_NAME="gough-sim-$(date +%s)"
QEMU_PID_FILE="/tmp/${VM_NAME}.pid"

usage() {
    cat <<EOF
Usage: launch-sim.sh [OPTIONS]

Options:
  --firmware {bios|uefi}       BIOS or UEFI firmware (default: bios)
  --ipv6 {disabled|slaac-only|dhcpv6|dual-stack}  IPv6 mode (default: disabled)
  --secure-boot                Enable UEFI Secure Boot
  --dmi-uuid <uuid>            Set DMI system UUID
  --smart-pattern {healthy|warning|failing}  SMART disk pattern (default: healthy)
  --with-bmc                   Enable mock BMC (Redfish)
  --help                       Show this help

Output:
  PID file: ${QEMU_PID_FILE}
  VM name: ${VM_NAME}
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --firmware) FIRMWARE="$2"; shift 2 ;;
        --ipv6) IPV6_MODE="$2"; shift 2 ;;
        --secure-boot) SECURE_BOOT="true"; shift ;;
        --dmi-uuid) DMI_UUID="$2"; shift 2 ;;
        --smart-pattern) SMART_PATTERN="$2"; shift 2 ;;
        --with-bmc) WITH_BMC="true"; shift ;;
        --help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# Generate DMI UUID if not provided
if [[ -z "$DMI_UUID" ]]; then
    DMI_UUID=$(uuidgen)
fi

echo "[launch-sim] Starting QEMU VM: ${VM_NAME}"
echo "[launch-sim] Firmware: ${FIRMWARE}, IPv6: ${IPV6_MODE}, DMI UUID: ${DMI_UUID}"

# Disk image setup
QEMU_DATA_DIR="/tmp/gough-qemu"
mkdir -p "${QEMU_DATA_DIR}"

SYSTEM_DISK="${QEMU_DATA_DIR}/${VM_NAME}-system.qcow2"
DARK_DISK_1="${QEMU_DATA_DIR}/${VM_NAME}-dark-1.qcow2"
DARK_DISK_2="${QEMU_DATA_DIR}/${VM_NAME}-dark-2.qcow2"

qemu-img create -f qcow2 "${SYSTEM_DISK}" 20G > /dev/null 2>&1 || true
qemu-img create -f qcow2 "${DARK_DISK_1}" 10G > /dev/null 2>&1 || true
qemu-img create -f qcow2 "${DARK_DISK_2}" 10G > /dev/null 2>&1 || true

# Generate SMART fixtures
SMART_FIXTURE_DIR="${QEMU_DATA_DIR}/${VM_NAME}-smart"
mkdir -p "${SMART_FIXTURE_DIR}"
python3 "$(dirname "$0")/seed-smart.py" \
    --pattern "${SMART_PATTERN}" \
    --output "${SMART_FIXTURE_DIR}/smartctl.json"

# Build QEMU command line
QEMU_CMD="qemu-system-x86_64"
QEMU_OPTS="-name ${VM_NAME}"
QEMU_OPTS+=" -m 2048"  # 2GB RAM
QEMU_OPTS+=" -smp 2"    # 2 CPUs
QEMU_OPTS+=" -enable-kvm"

# Firmware
if [[ "$FIRMWARE" == "uefi" ]]; then
    QEMU_OPTS+=" -bios /usr/share/OVMF/OVMF_CODE.fd"
    if [[ "$SECURE_BOOT" == "true" ]]; then
        QEMU_OPTS+=" -global driver=cfi.pflash01,property=secure,value=on"
    fi
fi

# DMI
QEMU_OPTS+=" -smbios type=1,uuid=${DMI_UUID}"

# Storage
QEMU_OPTS+=" -drive file=${SYSTEM_DISK},format=qcow2,index=0,media=disk"
QEMU_OPTS+=" -drive file=${DARK_DISK_1},format=qcow2,index=1,media=disk"
QEMU_OPTS+=" -drive file=${DARK_DISK_2},format=qcow2,index=2,media=disk"

# Network (virtio-net, two NICs for mgmt + internal baseline)
QEMU_OPTS+=" -netdev bridge,br=gough-sim-bridge,id=net0"
QEMU_OPTS+=" -device virtio-net-pci,netdev=net0,mac=52:55:00:00:00:01"
QEMU_OPTS+=" -netdev bridge,br=gough-sim-bridge,id=net1"
QEMU_OPTS+=" -device virtio-net-pci,netdev=net1,mac=52:55:00:00:00:02"

# IPv6 configuration (passed via kernel args or DHCP)
# This is handled by cloud-init in the guest; just label here for reference
if [[ "$IPV6_MODE" != "disabled" ]]; then
    QEMU_OPTS+=" -fw_cfg name=opt/gough/ipv6_mode,string=${IPV6_MODE}"
fi

# Serial console
QEMU_OPTS+=" -serial stdio"

# VNC (for debugging)
QEMU_OPTS+=" -vnc 127.0.0.1:0"

# Start QEMU in background
if [[ "$WITH_BMC" == "true" ]]; then
    # Start mock Redfish server alongside QEMU
    python3 "$(dirname "$0")/mock-redfish.py" \
        --uuid "${DMI_UUID}" \
        --output "${SMART_FIXTURE_DIR}" \
        --port 8000 &
    REDFISH_PID=$!
    echo "${REDFISH_PID}" > "${QEMU_DATA_DIR}/${VM_NAME}-redfish.pid"
    QEMU_OPTS+=" -fw_cfg name=opt/gough/bmc_endpoint,string=127.0.0.1:8000"
fi

# Launch QEMU
${QEMU_CMD} ${QEMU_OPTS} &
QEMU_PID=$!
echo "$QEMU_PID" > "${QEMU_PID_FILE}"

echo "[launch-sim] QEMU PID: $QEMU_PID"
echo "[launch-sim] PID file: ${QEMU_PID_FILE}"
echo "[launch-sim] System disk: ${SYSTEM_DISK}"
echo "[launch-sim] SMART fixture: ${SMART_FIXTURE_DIR}/smartctl.json"

wait $QEMU_PID
