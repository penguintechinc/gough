#!/bin/bash
# cleanup.sh — Tear down QEMU VMs + storage for node-sim tests

set -e

QEMU_DATA_DIR="${1:-/tmp/gough-qemu}"

if [[ ! -d "$QEMU_DATA_DIR" ]]; then
    echo "[cleanup] Data directory not found: $QEMU_DATA_DIR"
    exit 0
fi

echo "[cleanup] Shutting down QEMU VMs..."
for pid_file in "${QEMU_DATA_DIR}"/*.pid; do
    if [[ -f "$pid_file" ]]; then
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "[cleanup] Killing QEMU PID $pid"
            kill -9 "$pid" || true
        fi
        rm -f "$pid_file"
    fi
done

for pid_file in "${QEMU_DATA_DIR}"/*-redfish.pid; do
    if [[ -f "$pid_file" ]]; then
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "[cleanup] Killing Redfish PID $pid"
            kill -9 "$pid" || true
        fi
        rm -f "$pid_file"
    fi
done

echo "[cleanup] Removing disk images..."
rm -f "${QEMU_DATA_DIR}"/*-system.qcow2
rm -f "${QEMU_DATA_DIR}"/*-dark-*.qcow2
rm -rf "${QEMU_DATA_DIR}"/*-smart

echo "[cleanup] Done."
