# Runbook: Helper Image SBOM Verification Failed

**Alert Name:** `gough.images.helper_sbom_verification_failed`  
**Severity:** CRITICAL  
**Component:** Helper image SBOM validation  

**Symptoms:**
- Prometheus alert: `gough.images.helper_sbom_verification_failed`
- Helper image SBOM invalid or missing
- New nodes cannot PXE boot

**Detection:**
```bash
# Check SBOM
gough image helper sbom-verify --json | jq '.status, .errors'
```

**Resolution:**
1. Rebuild helper image with SBOM
2. Verify cosign signature
3. Republish image

---
