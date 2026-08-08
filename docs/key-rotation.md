# 🔐 Key Rotation Runbook — MCP Knowledge Server

> **trace_id:** `code-2026-07-31-003` | **Phase:** 3 G3 | **Last updated:** 2026-08-03
> **Dependencies:** `mcp_server/src/mcp_server/auth.py` (multi-key auth, lines 81-90), `.env.example`

---

## Table of Contents

1. [Pre-requisites](#1-pre-requisites)
2. [Key Anatomy & Naming](#2-key-anatomy--naming)
3. [0-Downtime Rotation Procedure](#3-0-downtime-rotation-procedure)
4. [Emergency Rotation (Key Compromised)](#4-emergency-rotation-key-compromised)
5. [Audit & Verification](#5-audit--verification)
6. [Troubleshooting](#6-troubleshooting)
7. [Best Practices](#7-best-practices)

---

## 1. Pre-requisites

| Requirement | Detail |
|-------------|--------|
| **SSH access** | To the host running `mcp-knowledge-server` container |
| **`.env` file access** | Read/write to the project `.env` file |
| **Docker + Docker Compose** | `docker compose` available on the host |
| **`openssl`** (optional) | For generating cryptographically secure keys |
| **Log access** | `docker compose logs mcp-server` or journald |

**Auth system reference:** The server supports multi-key arrays via `MCP_READ_KEYS` and `MCP_WRITE_KEYS` in `.env`. Authentication uses constant-time comparison (`hmac.compare_digest`) — see [`auth.py:81-90`](../mcp_server/src/mcp_server/auth.py). **Any key in the array is accepted.** This overlap mechanism is the foundation of 0-downtime rotation.

---

## 2. Key Anatomy & Naming

### 2.1 Key Types

| Key Level | Access | `.env` Variable |
|-----------|--------|-----------------|
| **read-key** | `search_knowledge`, `search_by_tags`, `get_entry`, `get_knowledge_map`, `list_*`, `list_collections`, `analyze_content`, resources, prompts | `MCP_READ_KEYS=["..."]` |
| **import-key** | All read tools + `import_content` (без delete/reindex/write) | `MCP_IMPORT_KEYS=["..."]` |
| **write-key** | All read tools + `write_knowledge`, `update_entry`, `delete_entry`, `reindex` | `MCP_WRITE_KEYS=["..."]` |

### 2.2 Key Format

- **Recommended:** 64-character hex string (`openssl rand -hex 32`)
- **Minimum:** 32 characters
- **Format:** Any string accepted; stored as plain text in `.env` (not hashed)
- **Masking:** Logs show only first 4 chars + SHA256[:12] — e.g., `a1b2...e3f4g5h6i7j8`

### 2.3 Naming Convention (for human reference — NOT in `.env`)

```
Purpose         Environment     Example identifier
──────────      ───────────     ──────────────────
agent-read      prod            prod-agent-read-2026-08
agent-write     prod            prod-agent-write-2026-08
ci-read         prod            prod-ci-read-2026-08
admin           prod            prod-admin-2026-08
monitoring      prod            prod-monitor-2026-08
```

> Store key→purpose mapping in a **separate, encrypted** file (1Password, HashiCorp Vault, or GPG-encrypted `keys.asc`). Never commit keys to git.

---

## 3. 0-Downtime Rotation Procedure

### Principle: Overlap-based rotation

Both old and new keys are valid simultaneously during the transition window. The server authenticates against ANY key in the list — the first match wins (write-key check runs first).

```
Timeline:
  ──[old-key only]──[old+new overlap]──[new-key only]──▶
                        ▲
                   migration window
```

### Step 1: Add NEW key to `.env`

**Generate a new key:**
```bash
openssl rand -hex 32
# Example output: a1b2c3d4e5f6... (64 hex chars)
```

**Edit `.env` — add new key to the JSON array:**

```bash
# BEFORE (old key only):
MCP_READ_KEYS=["old-key-abc123..."]
MCP_WRITE_KEYS=["old-write-key-xyz789..."]

# AFTER (both keys active):
MCP_READ_KEYS=["old-key-abc123...","new-key-def456..."]
MCP_WRITE_KEYS=["old-write-key-xyz789...","new-write-key-uvw012..."]
```

**Restart the server:**
```bash
docker compose restart mcp-server
# Wait for health:
curl -sf http://localhost:8000/health/live && echo "✅ alive"
```

> **Expected behavior:** Both old and new keys work. `AuthMiddleware` iterates the key array — both keys will authenticate successfully.

**Verification — test both keys:**
```bash
# Old key should still work
curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: old-key-abc123..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'

# New key should also work
curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: new-key-def456..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
```

Both should return the tool count (20 tools as of Phase 13).

---

### Step 2: Migrate all clients to new key

Update every client/agent configuration that uses the old key:

| Client Type | Where to update |
|-------------|-----------------|
| **Kilo/Claude config** | `kilo.json` → `mcpServers.knowledge.headers.X-API-Key` |
| **OpenCode config** | `opencode.json` → MCP server config |
| **CI/CD pipelines** | `.gitlab-ci.yml` / GitHub Actions secrets |
| **Monitoring scripts** | `scripts/health-check.sh`, cron jobs |
| **Development `.env`** | Local `.env` files on developer machines |

**Monitor logs for migration progress:**
```bash
# Watch for auth successes — verify clients are using the new key
docker compose logs -f mcp-server 2>&1 | grep "Auth SUCCESS"

# Count key_hash usage (new key hash will appear as clients migrate)
docker compose logs mcp-server 2>&1 | grep "Auth SUCCESS" | wc -l
```

> **Pro tip:** The `masked` value in Auth SUCCESS logs (format: `first4chars...SHA256[:12]`) identifies which key a client used without revealing it. Compute the expected mask for your new key:
> ```bash
> python3 -c "
> import hashlib
> key = 'new-key-def456...'
> print(f'{key[:4]}...{hashlib.sha256(key.encode()).hexdigest()[:12]}')
> "
> ```
> Then grep for this mask to confirm migration:
> ```bash
> docker compose logs mcp-server 2>&1 | grep "masked=YOUR_MASK" | tail -20
> ```
> **Note:** `key_hash` (SHA256[:16]) only appears in `Auth FORBIDDEN` logs (permission violations), not in `Auth SUCCESS`. For auditing successful auth events, use the `masked=` field.

**Migration complete when:** Zero auth events use the old `key_hash` for a full monitoring period (recommended: 24h for production).

---

### Step 3: Remove OLD key

**Edit `.env` — remove old key:**

```bash
# BEFORE (both keys):
MCP_READ_KEYS=["old-key-abc123...","new-key-def456..."]

# AFTER (new key only):
MCP_READ_KEYS=["new-key-def456..."]
```

**Restart the server:**
```bash
docker compose restart mcp-server
curl -sf http://localhost:8000/health/live && echo "✅ alive"
```

**Verification — old key MUST fail:**
```bash
# Old key should now return 401
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://localhost:8000/mcp \
  -H "X-API-Key: old-key-abc123..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# Expected: 401
```

**New key must work:**
```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: new-key-def456..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# Expected: 18
```

---

### Step 4: Audit

**1. Verify no clients use the old key:**
```bash
# Search logs for auth failures with the old key (masked prefix + hash)
docker compose logs mcp-server --since 1h 2>&1 | grep "Auth FAILED"

# If any appear — a client still has the old key. Update it.
```

**2. List active key hashes (via admin endpoint):**
```bash
# Write-key authenticated endpoint (proposed in auth.py architecture)
curl -s -X GET http://localhost:8000/admin/keys \
  -H "X-API-Key: new-write-key-uvw012..." | jq '.'
```

> **Note:** The `/admin/keys` endpoint is a proposed feature from the Phase 3 plan (§6 G3). If not yet implemented, use log-based verification (grep for `Auth SUCCESS`).

**3. Check for unexpected auth patterns:**
```bash
# Auth failures by minute (spike = misconfigured client)
docker compose logs mcp-server --since 24h 2>&1 \
  | grep "Auth FAILED" \
  | awk '{print $1, $2}' | uniq -c

# Write operations by read-keys (permission violations)
docker compose logs mcp-server --since 24h 2>&1 \
  | grep "Auth FORBIDDEN: read-key attempted write"
```

**4. Document the rotation:**
```markdown
## Key Rotation Log — 2026-08-03
- **Rotated:** MCP_READ_KEYS, MCP_WRITE_KEYS
- **Old key hash:** abc123... (removed)
- **New key hash:** def456... (active)
- **Overlap window:** 2026-08-03 10:00 – 14:00 UTC (4h)
- **Clients migrated:** 5/5
- **Auth failures post-removal:** 0
- **Verified by:** @admin
```

---

## 4. Emergency Rotation (Key Compromised)

> **Trigger:** A key has been leaked (committed to git, exposed in logs, shared accidentally, or suspected breach).

### 4.1 Immediate Revocation (READ keys)

**Goal:** Remove the compromised key within 60 seconds.

```bash
# 1. SSH to host
ssh mcp-host

# 2. Edit .env — REMOVE compromised key, KEEP other keys
# BEFORE: MCP_READ_KEYS=["good-key","COMPROMISED-KEY"]
# AFTER:  MCP_READ_KEYS=["good-key"]
vim .env

# 3. Restart
docker compose restart mcp-server

# 4. Verify the compromised key is REJECTED
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:8000/mcp \
  -H "X-API-Key: COMPROMISED-KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# MUST return 401
```

### 4.2 Immediate Revocation (WRITE keys)

**Additional steps for write-key compromise:**

```bash
# 1. Revoke the key (same as 4.1)
# 2. AUDIT all write operations during compromise window:
docker compose logs mcp-server --since "2026-08-03T10:00" --until "2026-08-03T11:00" 2>&1 \
  | grep -E "(write_knowledge|update_entry|delete_entry|reindex)"

# 3. Check git log for unauthorized changes:
cd knowledge/
git log --oneline --since="2026-08-03T10:00" --until="2026-08-03T11:00"

# 4. If unauthorized writes detected → restore from backup (see docs/restore-runbook.md)
```

### 4.3 Post-Incident Checklist

- [ ] Key removed from `.env` and server restarted
- [ ] All other keys rotated (attacker may have seen them)
- [ ] Git history audited for unauthorized commits
- [ ] Qdrant data integrity verified (`curl /health` → checks all green)
- [ ] `.env` checked — no other secrets exposed in same context
- [ ] If committed to git: `git filter-branch` or `BFG Repo-Cleaner` to purge, then rotate ALL keys
- [ ] Incident documented (date, compromised key hash, impact assessment, remediation)

---

## 5. Audit & Verification

### 5.1 Log Analysis Commands

```bash
# All auth events (last hour)
docker compose logs mcp-server --since 1h 2>&1 | grep "Auth "

# Auth success by masked key (identify which keys are active)
docker compose logs mcp-server --since 24h 2>&1 \
  | grep "Auth SUCCESS" \
  | grep -oP 'masked=\K[^)]+' \
  | sort | uniq -c | sort -rn

# Auth failures by masked prefix
docker compose logs mcp-server --since 24h 2>&1 \
  | grep "Auth FAILED" \
  | grep -oP 'masked=\K[^)]+' \
  | sort | uniq -c | sort -rn

# Write attempts by read-keys (permission violations)
docker compose logs mcp-server --since 24h 2>&1 \
  | grep "Auth FORBIDDEN"

# Rate limit hits (possible brute-force or misconfigured client)
docker compose logs mcp-server --since 24h 2>&1 \
  | grep "Rate limit exceeded"
```

### 5.2 Automated Audit Script

```bash
#!/usr/bin/env bash
# scripts/audit-keys.sh — daily key usage audit
# Run via cron: 0 8 * * * /path/to/scripts/audit-keys.sh

LOG_LINES=$(docker compose logs mcp-server --since 24h 2>&1)

echo "=== Key Usage Report ($(date +%Y-%m-%d)) ==="
echo ""
echo "Active key masks:"
echo "$LOG_LINES" | grep "Auth SUCCESS" \
  | grep -oP 'masked=\K[^)]+' \
  | sort | uniq -c | sort -rn

echo ""
echo "Auth failures:"
echo "$LOG_LINES" | grep -c "Auth FAILED"

echo ""
echo "Permission violations:"
echo "$LOG_LINES" | grep -c "Auth FORBIDDEN"

echo ""
echo "Rate limit hits:"
echo "$LOG_LINES" | grep -c "Rate limit exceeded"
```

### 5.3 Admin Endpoint (proposed)

When implemented, the `/admin/keys` endpoint provides programmatic key inventory:

```bash
# List active key hashes (write-key required)
curl -s http://localhost:8000/admin/keys \
  -H "X-API-Key: $MCP_WRITE_KEY" | jq '.'

# Expected response:
# {
#   "read_keys": [
#     {"key_hash": "abc123...", "last_used": "2026-08-03T14:30:00Z", "request_count": 1542}
#   ],
#   "write_keys": [
#     {"key_hash": "def456...", "last_used": "2026-08-03T14:30:00Z", "request_count": 87}
#   ]
# }
```

---

## 6. Troubleshooting

### 6.1 "New key doesn't work after adding to .env"

**Symptoms:** HTTP 401 after adding key and restarting.

**Checklist:**
```bash
# 1. Verify .env syntax — JSON array, valid quotes
grep MCP_READ_KEYS .env
grep MCP_WRITE_KEYS .env

# 2. Verify server restarted with new config
docker compose restart mcp-server
docker compose logs mcp-server --tail 5

# 3. Test with verbose curl
curl -v -X POST http://localhost:8000/mcp \
  -H "X-API-Key: YOUR-NEW-KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**Common causes:**
- JSON syntax error: trailing comma, unescaped quotes, missing brackets
- Key copied with extra whitespace/newline — use `echo -n "key"` to verify
- Server didn't pick up `.env` changes — run `docker compose down && docker compose up -d` for full restart
- Key exceeds env variable length limits (> 32 KB) — unlikely but possible with extremely long keys

### 6.2 "Server fails to restart after editing .env"

**Symptoms:** Container exits immediately or stays in restart loop.

**Checklist:**
```bash
# 1. Validate .env JSON syntax
python3 -c "
import json
import os
# Simulate pydantic-settings parsing
keys = os.getenv('MCP_READ_KEYS', '[]')
parsed = json.loads(keys)
print(f'OK: {len(parsed)} key(s) parsed')
"

# 2. Check container logs
docker compose logs mcp-server --tail 50

# 3. Check for syntax errors
docker compose config  # validates docker-compose.yml
```

**Common causes:**
- Unescaped special characters in key string (backslash `\`, double-quote `"`)
- Invalid JSON (single quotes instead of double quotes)
- Missing closing bracket `]`
- Pydantic validation failure — key too short or contains control characters

### 6.3 "Old key still works after removal"

**Symptoms:** Authentication succeeds with the key that was removed from `.env`.

**Checklist:**
```bash
# 1. Confirm .env on the RUNNING container, not just the file
docker compose exec mcp-server cat /app/.env | grep MCP_READ_KEYS

# 2. Force full restart (not just restart)
docker compose down
docker compose up -d

# 3. Check for multiple .env files
find . -name ".env" -o -name ".env.production" -o -name ".env.local"
```

**Common causes:**
- `docker compose restart` didn't reload `.env` — use `down && up -d`
- Multiple `.env` files — server loading a different one
- Cached env in Docker volume — `docker compose down -v && docker compose up -d`

### 6.4 "Auth FAILED spike after rotation"

**Symptoms:** High rate of `Auth FAILED` in logs after removing old key.

**Diagnosis:**
```bash
# Identify which masked key is failing
docker compose logs mcp-server --since 15m 2>&1 \
  | grep "Auth FAILED" \
  | grep -oP 'masked=\K[^)]+' \
  | sort | uniq -c | sort -rn

# Check if the failed key matches the old key's masked prefix
# Old key: first 4 chars = "a1b2"
# If failed masked = "a1b2...xxx" → some client wasn't migrated
```

**Resolution:**
1. Identify the client using the old key (check CI configs, agent configs, dev machines)
2. Update that client to the new key
3. **OR:** Temporarily re-add the old key to `.env` and restart (buy time for migration)

### 6.5 "Can't distinguish which client uses which key"

**Problem:** Multiple clients share the same key, can't tell them apart.

**Solution:** Issue separate keys per client (or per client role):

```bash
# .env
MCP_READ_KEYS=[
  "key-for-agent-1...",
  "key-for-agent-2...",
  "key-for-monitoring..."
]
```

Each key has a unique `key_hash` in logs, enabling per-client audit. When rotating, rotate one client at a time.

---

## 7. Best Practices

### 7.1 Key Generation

```bash
# Recommended: 64-char hex
openssl rand -hex 32

# Alternative: base64 URL-safe (no special chars)
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='

# NEVER: predictable keys, dictionary words, dates, usernames
```

### 7.2 Key Storage

| Storage | Suitable for | NOT suitable for |
|---------|-------------|------------------|
| `.env` file on server | ✅ Runtime config | ❌ Backup/archive |
| 1Password / Vault | ✅ Master copy | — |
| Git repository | ❌ NEVER | ❌ NEVER |
| CI/CD secrets | ✅ Automated pipelines | ❌ Long-term reference |
| Encrypted `.gpg` file | ✅ Cold backup | — |

### 7.3 Rotation Schedule

| Environment | Rotation Frequency | Rationale |
|-------------|-------------------|-----------|
| **Production** | Every 90 days | PCI-DSS / SOC2 best practice |
| **Staging** | Every 180 days | Lower risk, same procedure |
| **Development** | On team member departure | Only rotate when needed |

### 7.4 Pre-Rotation Checklist

- [ ] New key generated and stored in password manager
- [ ] All client configurations documented (who uses which key)
- [ ] Maintenance window communicated (if needed — 0-downtime, but good practice)
- [ ] Backup of current `.env` created
- [ ] Rollback plan ready (re-add old key if migration fails)

### 7.5 Key Hygiene

- **One key per purpose:** Don't share the same key between CI and agent
- **Read vs Write segregation:** Never give write-keys to read-only clients
- **Key length:** ≥ 32 characters (64 hex chars recommended)
- **No key reuse:** Generate fresh keys; never copy keys between environments
- **Audit trail:** Log every rotation with date, reason, and old/new key hashes
- **Expired keys:** Remove immediately after migration — don't keep "just in case"

### 7.6 Monitoring & Alerting

```bash
# Prometheus alert rule (if metrics endpoint tracks auth failures)
# ALERT HighAuthFailures
#   IF rate(mcp_auth_failures_total[5m]) > 10
#   FOR 5m
#   LABELS { severity: "warning" }
#   ANNOTATIONS { summary: "High auth failure rate — possible brute force or misconfiguration" }

# Simple cron-based alert:
# */5 * * * * [ $(docker compose logs mcp-server --since 5m 2>&1 | grep -c "Auth FAILED") -gt 10 ] && echo "ALERT: Auth spike" | mail -s "MCP Auth Alert" admin@example.com
```
