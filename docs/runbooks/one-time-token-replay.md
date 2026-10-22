# Runbook: One-Time Token Replay Attack Detected

**Alert Name:** `gough.security.otp_replay_detected`  
**Severity:** WARNING  
**Component:** One-time token validation  
**SLO Impact:** Minor; replay detected and blocked  

---

## Symptoms

- Prometheus alert: `gough.security.otp_replay_detected{token_id="..."}`
- `gough security otp-status` shows replay attempts
- Logs: "OTP token reuse detected; token invalidated"

---

## Detection

**Metrics:**
```bash
gough.security.otp_replay_detected{...} == 1
gough.security.otp_replay_attempts_total > 0
```

**CLI verification:**
```bash
# Check for replay attempts
gough security otp-status --format=json | jq '.replay_attempts | .[] | {token_id, attempt_count, last_attempt}'

# Audit log
gough audit log --filter="action=otp_replay_detected" --limit=10
```

---

## Diagnosis & Resolution

**Cause 1: User Accidentally Reused OTP (90%)**
```bash
# Invalidate token
gough security otp-invalidate --token-id=<id>

# Notify user (new token can be issued)
# Replay is expected if user retried after network timeout
```

**Cause 2: Attacker Attempting Replay (10%)**
```bash
# Escalate to security team
gough alert escalate gough.security.otp_replay_detected \
  --team=security-team \
  --reason="Potential OTP replay attack" \
  --ticket=<jira-ticket-id>
```

---

## Prevention

1. Implement rate limiting on token validation.
2. Log all replay attempts for forensics.

---
