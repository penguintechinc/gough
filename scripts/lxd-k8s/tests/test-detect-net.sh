#!/usr/bin/env bash
# test-detect-net.sh - tests NIC role detection (detect_network and its helpers in hypervisor-prep.sh) against fixture
# sysfs and /proc/net/bonding trees. Functions are extracted by function boundary and every case runs in its own bash
# process, so no script runs whole. HYPERVISOR_SRC overrides the script under test (used for mutation checks).
# Prints "N passed, M failed"; exits non-zero on any failure or when no case ran.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYPERVISOR="${HYPERVISOR_SRC:-${SCRIPT_DIR}/../hypervisor-prep.sh}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-detect-net.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
EXTRACT="${WORK}/functions.sh"
BODY="${WORK}/body.sh"
PASSED=0
FAILED=0
ROOT=""
SYS=""
VIRT=""
PROC=""
OVR=()

# extract_functions <source> <name-alternation> appends the named top-level functions from a script to EXTRACT.
# Only the named functions are copied, so the tests exercise the real code without running the script's main path.
extract_functions() {
  awk -v re="^(${2})[(][)] [{]" '
    $0 ~ re { print; if ($0 ~ /[}][ \t]*$/) next; on = 1; next }
    on { print }
    on && /^[}]/ { on = 0 }
  ' "$1" >> "${EXTRACT}"
}

# write_body writes the per-case driver: it sources the extracted functions, runs detect_network and prints the resolved roles.
# The driver runs detect_network in its own process, so a die() in one case cannot affect another.
write_body() {
  cat > "${BODY}" <<'BODYEOF'
#!/usr/bin/env bash
set -euo pipefail
source "$1"
BRIDGE_NAME="${BRIDGE_NAME:-br0}"
detect_network
printf 'RESULT MGMT_IFACE=%s\n' "${MGMT_IFACE:-}"
printf 'RESULT UPLINK_MODE=%s\n' "${UPLINK_MODE:-}"
printf 'RESULT UPLINK_IFACE=%s\n' "${UPLINK_IFACE:-}"
printf 'RESULT BOND_MEMBER_1=%s\n' "${BOND_MEMBER_1:-}"
printf 'RESULT BOND_MEMBER_2=%s\n' "${BOND_MEMBER_2:-}"
BODYEOF
}

# new_root starts a fresh fixture tree with a sysfs net dir, a devices/virtual net dir and a bonding proc dir.
# Every case gets a fresh tree, so fixtures never leak between cases.
new_root() {
  ROOT="$(mktemp -d "${WORK}/fx.XXXXXX")"
  SYS="${ROOT}/sys/class/net"
  VIRT="${ROOT}/devices/virtual/net"
  PROC="${ROOT}/proc/bonding"
  mkdir -p "${SYS}" "${VIRT}" "${PROC}"
}

# mk_phys <name> <speed-or-empty> <operstate> <carrier> builds a physical Ethernet NIC (device link, type 1); empty speed leaves no speed file.
# A device link and type 1 make a real Ethernet candidate unless the state or speed says otherwise.
mk_phys() {
  local d="${SYS}/$1"
  mkdir -p "${d}/device"
  printf '1\n' > "${d}/type"
  printf '%s\n' "$3" > "${d}/operstate"
  printf '%s\n' "$4" > "${d}/carrier"
  if [[ -n "$2" ]]; then
    printf '%s\n' "$2" > "${d}/speed"
  fi
}

# mk_virt <name> <speed> builds a virtual interface: a symlink into devices/virtual with no device link, state up.
# Mirrors real sysfs: a symlink into devices/virtual with no device link.
mk_virt() {
  mkdir -p "${VIRT}/$1"
  printf '1\n' > "${VIRT}/$1/type"
  printf 'up\n' > "${VIRT}/$1/operstate"
  printf '1\n' > "${VIRT}/$1/carrier"
  printf '%s\n' "$2" > "${VIRT}/$1/speed"
  ln -s "${VIRT}/$1" "${SYS}/$1"
}

# mk_bond <name> <mode> <speed> [slave...] builds a bond master (virtual target, state up) and its /proc/net/bonding entry.
# Mirrors real sysfs: a virtual master plus a /proc/net/bonding entry carrying mode and slaves.
mk_bond() {
  local name="$1" mode="$2" speed="$3" s
  shift 3
  mk_virt "${name}" "${speed}"
  {
    printf 'Ethernet Channel Bonding Driver: v5.15\n\nBonding Mode: %s\n' "${mode}"
    for s in "$@"; do
      printf 'Slave Interface: %s\nMII Status: up\n' "${s}"
    done
  } > "${PROC}/${name}"
}

# mk_slave <name> <speed> <master> builds a physical NIC enslaved to <master> through a master link, as sysfs does.
# A physical NIC whose master link points at the bond, as the kernel publishes it.
mk_slave() {
  mk_phys "$1" "$2" up 1
  ln -s "../$3" "${SYS}/$1/master"
}

# mk_wifi <name> <speed> builds a physical NIC identified as wireless by its wireless directory (name is neutral).
# Wireless by directory only; the neutral name means only the directory rule can skip it.
mk_wifi() {
  mk_phys "$1" "$2" up 1
  mkdir -p "${SYS}/$1/wireless"
}

# mk_phy80211 <name> <speed> builds a physical NIC identified as wireless by its phy80211 link (name is neutral).
# Wireless by phy80211 link only; the neutral name means only the link rule can skip it.
mk_phy80211() {
  mk_phys "$1" "$2" up 1
  mkdir -p "${ROOT}/phy0"
  ln -s "${ROOT}/phy0" "${SYS}/$1/phy80211"
}

# mk_lo builds the loopback entry: no device link, operstate unknown.
# Loopback as the kernel reports it: no device link and operstate unknown.
mk_lo() {
  mkdir -p "${SYS}/lo"
  printf 'unknown\n' > "${SYS}/lo/operstate"
  printf '1\n' > "${SYS}/lo/carrier"
}

# run_case <name> <pass|fail> <needle...> runs detect_network on the current fixture (OVR entries become env) and checks
# exit status and output. A needle starting with "!" must be absent. A case passes only when every needle holds.
run_case() {
  local name="$1" want="$2" rc=0 out reason="" n
  shift 2
  out="$(env -u MGMT_IFACE -u UPLINK_IFACE -u UPLINK_MODE -u BOND_MEMBER_1 -u BOND_MEMBER_2 -u BRIDGE_NAME \
    SYSFS_NET="${SYS}" PROC_BONDING="${PROC}" ${OVR[@]+"${OVR[@]}"} bash "${BODY}" "${EXTRACT}" 2>&1)" || rc=$?
  OVR=()
  if [[ "${want}" == "pass" ]]; then
    [[ "${rc}" -eq 0 ]] || reason="expected success, exit ${rc}"
  else
    [[ "${rc}" -ne 0 ]] || reason="expected failure, got success"
  fi
  for n in "$@"; do
    if [[ "${n}" == !* ]]; then
      if grep -qF -- "${n#!}" <<< "${out}"; then
        reason="${reason:-unexpected output: ${n#!}}"
      fi
    elif ! grep -qF -- "${n}" <<< "${out}"; then
      reason="${reason:-output lacks: ${n}}"
    fi
  done
  if [[ -z "${reason}" ]]; then
    PASSED=$((PASSED + 1))
    printf 'PASS %s\n' "${name}"
  else
    FAILED=$((FAILED + 1))
    printf 'FAIL %s: %s\n' "${name}" "${reason}"
    printf '%s\n' "${out}" | sed 's/^/    | /'
  fi
}

BOND_8023AD="IEEE 802.3ad Dynamic link aggregation"

: > "${EXTRACT}"
extract_functions "${HYPERVISOR}" 'log|die|warn|read_attr|bond_mode|bond_slaves|name_is_virtual|classify_iface|scan_interfaces|list_candidates|check_override_kind|select_mgmt|select_uplink|print_decision_table|detect_network|validate_iface_name|valid_ipv4|valid_cidr4|validate_net_values'
for fn in log die warn read_attr bond_mode bond_slaves name_is_virtual classify_iface scan_interfaces list_candidates check_override_kind select_mgmt select_uplink print_decision_table detect_network; do
  grep -q "^${fn}() {" "${EXTRACT}" || { printf 'extraction failed: %s not found\n' "${fn}" >&2; exit 1; }
done
write_body

# (a) one slow and one fast live NIC.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
run_case "a slow+fast NICs: mgmt slow, uplink single fast" pass \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_MODE=single" "RESULT UPLINK_IFACE=eth1" "examined 2, candidates 2, skipped 0"

# (b) slow NIC plus a live 802.3ad bond whose two slaves are not management candidates.
new_root
mk_phys eth0 1000 up 1
mk_bond bond0 "${BOND_8023AD}" 20000 eth1 eth2
mk_slave eth1 10000 bond0
mk_slave eth2 10000 bond0
run_case "b slow NIC + 802.3ad bond: mgmt slow, uplink bond, slaves never mgmt" pass \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_MODE=bond" "RESULT BOND_MEMBER_1=eth1" "RESULT BOND_MEMBER_2=eth2" \
  "!RESULT MGMT_IFACE=eth1" "skip: slave of bond0"

# (c) tie for slowest: refused, both names listed.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 1000 up 1
mk_phys eth2 10000 up 1
run_case "c tie for slowest refused and names listed" fail \
  "ambiguous" "eth0 eth1 all run at 1000" "Set MGMT_IFACE"

# (d) wireless by directory, by phy80211 link and by name, even when slowest or fastest.
new_root
mk_wifi wifi0 100
mk_phy80211 ath0 50
mk_phys wlan0 40000 up 1
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
run_case "d wireless skipped by dir, link and name (slowest and fastest)" pass \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_IFACE=eth1" "skip: wireless device" "skip: wireless (name pattern)" \
  "!RESULT MGMT_IFACE=wifi0" "!RESULT MGMT_IFACE=ath0" "!RESULT UPLINK_IFACE=wlan0"

# (e) loopback, our bridge, container, VM, tunnel and overlay names are skipped even when they look fast.
new_root
mk_lo
mk_virt docker0 10000
mk_virt lxdbr0 10000
mk_virt veth1234 10000
mk_virt br-abc 10000
mk_virt virbr0 10000
mk_virt tap0 10000
mk_virt br0 10000
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
run_case "e lo, docker, lxdbr, veth, br-*, virbr, tap, br0 skipped" pass \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_IFACE=eth1" "examined 10, candidates 2, skipped 8" \
  "skip: virtual name pattern" "skip: loopback" "!RESULT UPLINK_IFACE=docker0"

# (f) carrier 0 and operstate down are not live.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 0
mk_phys eth2 10000 down 0
mk_phys eth3 5000 up 1
run_case "f no-carrier and operstate-down NICs not live" pass \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_IFACE=eth3" "not live: no carrier" "not live: operstate down"

# (g) speed -1 is not live for selection.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 -1 up 1
mk_phys eth2 10000 up 1
run_case "g speed -1 not live for selection" pass \
  "RESULT UPLINK_IFACE=eth2" "not live: no speed" "!RESULT MGMT_IFACE=eth1" "!RESULT UPLINK_IFACE=eth1"

# (h) a single live NIC cannot be both roles.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 down 0
run_case "h single live NIC refused" fail "only 1 live candidate" "single live NIC cannot be both"

# (i1) MGMT_IFACE override wins over detection.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
OVR=(MGMT_IFACE=eth1)
run_case "i1 MGMT_IFACE override wins" pass \
  "RESULT MGMT_IFACE=eth1" "RESULT UPLINK_IFACE=eth0" "MGMT: operator override"

# (i2) UPLINK_IFACE override wins over the auto uplink; an override may name a NIC that is not live.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
mk_phys eth2 10000 down 0
OVR=(UPLINK_IFACE=eth2)
run_case "i2 UPLINK_IFACE override wins" pass \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_MODE=single" "RESULT UPLINK_IFACE=eth2"

# (i3) BOND_MEMBER_1/2 override wins over the auto uplink.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
mk_phys eth2 10000 up 1
OVR=(BOND_MEMBER_1=eth1 BOND_MEMBER_2=eth2)
run_case "i3 BOND_MEMBER_1/2 override wins" pass \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_MODE=bond" "RESULT BOND_MEMBER_1=eth1" "RESULT BOND_MEMBER_2=eth2"

# (i4) UPLINK_IFACE and BOND_MEMBER_1/2 together are refused.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
mk_phys eth2 10000 up 1
OVR=(UPLINK_IFACE=eth1 BOND_MEMBER_1=eth1 BOND_MEMBER_2=eth2)
run_case "i4 UPLINK_IFACE with BOND_MEMBER_1/2 refused" fail "set either UPLINK_IFACE or BOND_MEMBER_1/2, not both"

# (i5) an override naming a missing interface is refused.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
OVR=(MGMT_IFACE=nosuch0)
run_case "i5 MGMT_IFACE naming a missing NIC refused" fail "MGMT_IFACE=nosuch0 not found"

# (i6) a bond can never be the management NIC, even by override.
new_root
mk_phys eth0 1000 up 1
mk_bond bond0 "${BOND_8023AD}" 20000 eth1 eth2
mk_slave eth1 10000 bond0
mk_slave eth2 10000 bond0
OVR=(MGMT_IFACE=bond0)
run_case "i6 bond refused as MGMT_IFACE override" fail "MGMT_IFACE=bond0 is a bond interface"

# (j) two spare NICs and no bond: refuse, never create a bond automatically.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
mk_phys eth2 10000 up 1
run_case "j two spare NICs and no bond: uplink refused, never auto-bond" fail \
  "cannot choose the application uplink" "never creates a bond" "UPLINK_IFACE=<nic>"

# (k) an active-backup bond is not a candidate bond; the spare NIC becomes the single uplink.
new_root
mk_phys eth0 1000 up 1
mk_phys eth1 10000 up 1
mk_bond bond0 "fault-tolerance (active-backup)" 10000 eth2
mk_slave eth2 10000 bond0
run_case "k active-backup bond is not a candidate" pass \
  "skip: bond mode 'fault-tolerance (active-backup)' is not 802.3ad" \
  "RESULT MGMT_IFACE=eth0" "RESULT UPLINK_MODE=single" "RESULT UPLINK_IFACE=eth1" "!RESULT UPLINK_MODE=bond"

# (l) zero interfaces examined is a failure.
new_root
run_case "l zero interfaces examined refused" fail "examined 0 interfaces"

# (m) a bond with one member cannot be the uplink.
new_root
mk_phys eth0 1000 up 1
mk_bond bond0 "${BOND_8023AD}" 10000 eth1
mk_slave eth1 10000 bond0
run_case "m bond with one member refused as uplink" fail "has 1 member(s)"

if [[ $((PASSED + FAILED)) -eq 0 ]]; then
  printf 'no cases ran\n' >&2
  exit 1
fi
printf '%d passed, %d failed\n' "${PASSED}" "${FAILED}"
if [[ "${FAILED}" -ne 0 ]]; then
  exit 1
fi
