# Runbook: SMART Warning (Disk Health)

**Alert Name:** `gough.hardware.smart_warning`  
**Severity:** WARNING  
**Component:** Disk health monitoring (SMART)  

**Symptoms:**
- Prometheus alert: `gough.hardware.smart_warning{node_id="...",device="..."}`
- SMART attributes show degradation
- Disk may be failing

**Detection:**
```bash
# Check SMART status
gough node hardware-smart --node-id=<id> --json | jq '.[] | {device, status, attributes}'

# Check for predictive failures
gough node hardware-smart --node-id=<id> | grep -i "predictive\|failing"
```

**Resolution:**
```bash
# Monitor closely; schedule disk replacement
# If critical: evacuate data from disk immediately

gough node drain --node-id=<id> --reason="Disk SMART warning"

# After replacement:
gough node undrain --node-id=<id>
```

---
