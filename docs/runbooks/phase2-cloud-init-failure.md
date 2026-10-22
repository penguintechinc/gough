# Runbook: Phase 2 Cloud-Init Failure

**Alert Name:** `gough.provisioning.phase2_cloud_init_failure`  
**Severity:** WARNING  
**Component:** Cloud-init Phase-2 execution  

**Symptoms:**
- Prometheus alert: `gough.provisioning.phase2_cloud_init_failure{node_id="..."}`
- Node logs show cloud-init Phase-2 errors
- Node stuck in `provisioning` state

**Detection:**
```bash
gough node list --filter="state=provisioning" --json | jq '.[] | {id,phase,error}'
kubectl -n gough logs -l app=cloud-init | grep -i "error\|failed"
```

**Resolution:**
1. Check cloud-init logs: `journalctl -u cloud-init -n 100`
2. Retry provisioning: `gough node provision-phase-2 --node-id=<id>`
3. If persistent, escalate to SRE

---
