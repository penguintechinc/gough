# biomes

Gough biomes — LXD cloud-init workload definitions (`k8s-controller`, `k8s-worker`, …) — and `gough-install.py`, the bare-metal installer that brings a fresh Ubuntu 26.04 host into a gough LXD cluster.

- Installer: `installer/gough-install.py` (Python 3 stdlib only; wizard or `--non-interactive` flags for cloud-init/MaaS/AWS)
- Biomes: `biomes/<name>/` (versioned, mirror-aware, airgap-capable)
- The gough hub-api caches pinned releases of this repo and serves them to joining hosts.
