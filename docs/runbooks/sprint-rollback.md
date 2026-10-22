# Runbook: Sprint Rollback (Operational)

**When:** After a sprint release, if critical bugs are discovered and immediate rollback is required.

**Procedure:**
1. Identify affected biomes and versions
2. Create rollback plan (communication, timing)
3. Execute `gough biome rollback --biome-id=<id> --to-version=<previous>`
4. Monitor for side effects
5. Update audit logs with reason and approver

**Timeline:**
- Decision to rollback: minutes (executive decision)
- Planning: 15 min (document reason, impact)
- Execution: 5-15 min per biome (depends on cluster size)
- Monitoring: 30 min post-rollback

---
