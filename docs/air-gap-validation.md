# 🔒 Air-gap Deployment Validation — MCP Knowledge Server

> **trace_id:** `code-2026-07-31-003` | **Phase:** 3 G4 | **Last updated:** 2026-08-03
> **Dependencies:** `scripts/offline-deploy.sh`, `docker-compose.yml`, `.env.example`

---

## Table of Contents

1. [Pre-requisites](#1-pre-requisites)
2. [Phase 1: Prepare (Internet-connected Machine)](#2-phase-1-prepare-internet-connected-machine)
3. [Phase 2: Deploy (Isolated Host)](#3-phase-2-deploy-isolated-host)
4. [Phase 3: Verify (Isolated Host)](#4-phase-3-verify-isolated-host)
5. [Troubleshooting](#5-troubleshooting)
6. [Air-gap Architecture Notes](#6-air-gap-architecture-notes)

---

## 1. Pre-requisites

### 1.1 Two Machines

| Machine | Network | Role | Minimum Specs |
|---------|---------|------|---------------|
| **Build host** | Internet access | Download artifacts, build bundle | Docker, Python 3.11, pip, 10 GB free disk |
| **Target host** | **Isolated** (no internet) | Run MCP Knowledge Server | Docker, Python 3.11, 8 GB RAM, 15 GB free disk |

### 1.2 Software Requirements (both machines)

| Software | Version | Check Command |
|----------|---------|---------------|
| Docker | ≥ 24.0 | `docker --version` |
| Docker Compose | ≥ 2.20 | `docker compose version` |
| Python | ≥ 3.11 | `python3 --version` |
| pip | ≥ 23.0 | `pip --version` |
| curl | any | `curl --version` |
| sha256sum | any | `sha256sum --version` |

### 1.3 Transfer Method (choose one)

| Method | Requirement | Max Speed |
|--------|-------------|:---------:|
| **USB drive** | ≥ 8 GB FAT32/exFAT/NTFS | ~100 MB/s |
| **External SSD** | ≥ 8 GB | ~500 MB/s |
| **Direct cable** | Ethernet between machines | ~1 Gbps |
| **Intermediate NAS** | Both machines can reach it temporarily | Varies |

> **Note:** The bundle size is approximately **2–4 GB** for core artifacts (images ~1.5 GB, wheels ~200 MB, model ~1.2 GB, code ~10 MB). Plan for transfer time accordingly.

---

## 2. Phase 1: Prepare (Internet-connected Machine)

### 2.1 Checklist

Run `./scripts/offline-deploy.sh prepare` on the build host, then verify:

- [ ] **Docker images saved** — `qdrant/qdrant:v1.13.4` + `python:3.11-slim`
- [ ] **Python wheels downloaded** — all dependencies from `requirements.txt`
- [ ] **BGE-M3 model pre-downloaded** — all artifacts present (config, tokenizer, weights)
- [ ] **Checksums generated** — `CHECKSUMS.sha256` for all files
- [ ] **Bundle packed** — `mcp-kb-airgap-bundle.tar.gz`
- [ ] **Size verified** — bundle < 8 GB

### 2.2 Detailed Verification Commands

```bash
# 1. Run prepare
./scripts/offline-deploy.sh prepare

# 2. Verify Docker images in bundle
tar -tzf mcp-kb-airgap-bundle.tar.gz | grep "images/images.tar"
# Expected: artifacts/images/images.tar

# 3. Verify wheel count
tar -tzf mcp-kb-airgap-bundle.tar.gz | grep "wheelhouse/" | wc -l
# Expected: 30-50 wheel files

# 4. Verify model artifacts
tar -tzf mcp-kb-airgap-bundle.tar.gz | grep "models/" | head -20
# Expected: models/bge-m3/ (or models/models--BAAI--bge-m3/) with:
#   - config.json
#   - tokenizer.json / tokenizer_config.json
#   - vocab.txt / special_tokens_map.json
#   - model.safetensors or pytorch_model.bin (> 100 MB)

# 5. Verify checksums
tar -xzf mcp-kb-airgap-bundle.tar.gz -C /tmp/verify-artifacts
cd /tmp/verify-artifacts/artifacts
sha256sum -c CHECKSUMS.sha256
# Expected: all files "OK"

# 6. Check bundle size
ls -lh mcp-kb-airgap-bundle.tar.gz
# Expected: < 8 GB (typically 2–4 GB)
```

### 2.3 Prepare Success Criteria

- [ ] `./offline-deploy.sh prepare` exits with code 0
- [ ] `sha256sum -c CHECKSUMS.sha256` — all files OK
- [ ] `verify_model_cache` passes (called automatically by prepare)
- [ ] `mcp-kb-airgap-bundle.tar.gz` size < 8 GB
- [ ] At least 3 wheelhouse files present
- [ ] Docker images.tar > 300 MB (confirms images saved, not just metadata)

### 2.4 Transfer Bundle to Isolated Host

```bash
# Option A: USB drive
cp mcp-kb-airgap-bundle.tar.gz /media/usb/
# ... move USB to isolated host ...
cp /media/usb/mcp-kb-airgap-bundle.tar.gz /opt/mcp-knowledge/

# Option B: Direct SCP (if temporary network available)
scp mcp-kb-airgap-bundle.tar.gz user@isolated-host:/opt/mcp-knowledge/

# Option C: Intermediate server
scp mcp-kb-airgap-bundle.tar.gz user@nas-server:/tmp/
# ... from isolated host ...
scp user@nas-server:/tmp/mcp-kb-airgap-bundle.tar.gz /opt/mcp-knowledge/
```

---

## 3. Phase 2: Deploy (Isolated Host)

### 3.1 Checklist

Run `./scripts/offline-deploy.sh deploy` on the isolated host, then verify:

- [ ] **Bundle extracted** — all directories present
- [ ] **Checksums verified** — `sha256sum -c CHECKSUMS.sha256` all OK
- [ ] **Docker images loaded** — `docker images` shows `qdrant/qdrant:v1.13.4`, `python:3.11-slim`
- [ ] **Python deps installed** — `pip list` shows fastapi, qdrant-client, sentence-transformers
- [ ] **BGE-M3 model placed** — `/opt/mcp-knowledge/models_cache/` populated
- [ ] **`.env` configured** — `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `SENTENCE_TRANSFORMERS_HOME` set
- [ ] **Model cache verified** — `verify_model_cache` passes (called automatically by deploy)
- [ ] **Services started** — `docker compose ps` shows both containers running

### 3.2 Detailed Verification Commands

```bash
# 1. Run deploy
./scripts/offline-deploy.sh deploy

# 2. Verify Docker images
docker images | grep -E "qdrant/qdrant|python"
# Expected:
#   qdrant/qdrant    v1.13.4    abc123...    ~200 MB
#   python            3.11-slim  def456...    ~150 MB

# 3. Verify Python packages (installed from wheelhouse, NOT internet)
pip show fastapi qdrant-client sentence-transformers 2>&1 | grep -E "^(Name|Version|Location):"
# Location should be local, not fetched from internet

# 4. Verify model cache contents
find /opt/mcp-knowledge/models_cache -type f | sort
# Expected artifacts:
#   .../config.json
#   .../tokenizer.json
#   .../tokenizer_config.json
#   .../vocab.txt
#   .../special_tokens_map.json
#   .../model.safetensors (or pytorch_model.bin) — > 100 MB

# 5. Check model weights size
find /opt/mcp-knowledge/models_cache -name "*.safetensors" -o -name "pytorch_model.bin" \
  | xargs ls -lh
# Expected: > 100 MB (BGE-M3 is ~560 MB)

# 6. Verify offline env vars
grep -E "HF_HUB_OFFLINE|TRANSFORMERS_OFFLINE|SENTENCE_TRANSFORMERS_HOME" .env
# Expected:
#   HF_HUB_OFFLINE=1
#   TRANSFORMERS_OFFLINE=1
#   SENTENCE_TRANSFORMERS_HOME=/app/models_cache

# 7. Check container status
docker compose ps
# Expected: both mcp-server and qdrant "Up" (healthy)
```

### 3.3 Deploy Success Criteria

- [ ] `./offline-deploy.sh deploy` exits with code 0
- [ ] Both Docker images visible in `docker images`
- [ ] `pip list` shows all required packages
- [ ] Model cache contains all 6+ required artifacts
- [ ] Model weights file > 100 MB
- [ ] `.env` has `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`
- [ ] `docker compose ps` — both services running

---

## 4. Phase 3: Verify (Isolated Host)

### 4.1 Checklist

- [ ] **Health endpoint** — `/health` returns healthy
- [ ] **Liveness endpoint** — `/health/live` returns alive
- [ ] **MCP tools/list** — returns tool list
- [ ] **search_knowledge** — returns results
- [ ] **No outbound network** — `tcpdump` confirms zero external connections
- [ ] **Cold start time** — < 90 seconds

### 4.2 Health Checks

```bash
# 1. Liveness probe (should always return 200)
curl -sf http://localhost:8000/health/live && echo "✅ liveness OK" || echo "❌ liveness FAIL"

# 2. Readiness probe (deep checks — qdrant + embed + pipeline + dlq)
curl -s http://localhost:8000/health | python3 -m json.tool
# Expected:
# {
#   "status": "healthy",
#   "version": "...",
#   "checks": {
#     "qdrant": {"ok": true, "connected": true, "points": ...},
#     "embedding": {"ok": true, "loaded": true, "backend": "cpu"},
#     "pipeline": {"ok": true, "worker_alive": true, "queue_size": 0},
#     "dlq": {"ok": true, "size": 0}
#   }
# }

# 3. Wait for all checks green (may take 30-60s for model to load)
for i in $(seq 1 10); do
  STATUS=$(curl -s http://localhost:8000/health | python3 -c "import sys, json; print(json.load(sys.stdin)['status'])")
  echo "Attempt $i: $STATUS"
  [ "$STATUS" = "healthy" ] && break
  sleep 10
done
```

### 4.3 MCP Tools Verification

```bash
# Read key from .env
MCP_READ_KEY=$(python3 -c "
import json, os
from dotenv import load_dotenv
load_dotenv()
keys = json.loads(os.getenv('MCP_READ_KEYS', '[\"test\"]'))
print(keys[0])
")

# 4. Initialize MCP session
curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: $MCP_READ_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"init-1","method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"air-gap-verify"},"capabilities":{}}}' \
  | python3 -m json.tool

# Expected: serverInfo with version, capabilities.tools=true

# 5. List tools
curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: $MCP_READ_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"list-1","method":"tools/list"}' \
  | python3 -c "import sys, json; tools=json.load(sys.stdin)['result']['tools']; print(f'{len(tools)} tools'); [print(f'  - {t[\"name\"]}') for t in tools]"

# Expected: 11 tools listed (search_knowledge, search_by_tags, get_entry, get_knowledge_map,
#                           list_domains, list_subjects, list_projects,
#                           write_knowledge, update_entry, delete_entry, reindex)

# 6. Search (requires some data in knowledge/ dir)
curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: $MCP_READ_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"search-1","method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"deployment","top_k":3}}}' \
  | python3 -m json.tool

# Expected: result with content array (may be empty if no knowledge loaded)
```

### 4.4 Network Isolation Verification

**Verify ZERO outbound connections:**

```bash
# Method 1: tcpdump (requires root/sudo)
sudo timeout 30 tcpdump -i any not host localhost and not net 127.0.0.0/8 \
  -c 1 2>&1

# Expected (after 30 sec timeout): "0 packets captured"
# If ANY packet captured → there's a leak!

# Method 2: Monitor for 60 seconds
sudo tcpdump -i any not host localhost -w /tmp/airgap-check.pcap &
TCPDUMP_PID=$!
sleep 60
sudo kill $TCPDUMP_PID
PACKET_COUNT=$(sudo tcpdump -r /tmp/airgap-check.pcap 2>&1 | wc -l)
echo "Outbound packets in 60s: $PACKET_COUNT"
# Expected: 0

# Method 3: Check for DNS queries (common leak vector)
sudo tcpdump -i any port 53 -c 1 &
sleep 30
# If no DNS packet captured after 30s → DNS is not leaking
```

**Verify HuggingFace offline mode is enforced:**

```bash
# Simulate what happens if code tries to reach huggingface.co
docker compose exec mcp-server python3 -c "
import os
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
try:
    from huggingface_hub import hf_hub_download
    hf_hub_download('BAAI/bge-m3', 'config.json')
    print('ERROR: Should have raised offline error')
except Exception as e:
    print(f'OK: Offline enforcement works — {type(e).__name__}')
"
# Expected: "OK: Offline enforcement works — ..."
```

### 4.5 Cold Start Timing

```bash
# Measure full cold start
time (docker compose down && docker compose up -d --wait)
# Expected: < 90 sec total

# OR measure more precisely:
START_TIME=$(date +%s)
docker compose down > /dev/null 2>&1
docker compose up -d --wait > /dev/null 2>&1
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
echo "Cold start: ${DURATION}s"
[ $DURATION -lt 90 ] && echo "✅ Within 90s threshold" || echo "❌ Exceeds 90s threshold"
```

### 4.6 Verify Success Criteria

- [ ] `/health/live` → HTTP 200
- [ ] `/health` → HTTP 200, `"status":"healthy"`
- [ ] `/health` checks: qdrant connected, embed loaded, pipeline alive, DLQ empty
- [ ] `tools/list` → returns 11 tools
- [ ] `search_knowledge` → returns results (or empty array if no data)
- [ ] `get_knowledge_map` → returns valid structure
- [ ] `tcpdump` → **0 outbound packets**
- [ ] Cold start < 90 seconds
- [ ] `docker compose ps` → both containers "healthy"

---

## 5. Troubleshooting

### 5.1 "Model fails to load with HuggingFace Hub error"

**Symptoms:** Server logs show `HFValidationError`, `OfflineModeIsEnabled`, or `ConnectionError` during model loading.

**Cause:** `HF_HUB_OFFLINE=1` is set, but model cache is incomplete or in wrong path.

**Fix:**
```bash
# 1. Check env vars are passed to container
docker compose exec mcp-server env | grep -E "HF_HUB|TRANSFORMERS|SENTENCE"

# 2. Verify model files exist INSIDE the container
docker compose exec mcp-server find /app/models_cache -type f | sort

# 3. If missing, check docker-compose.yml volume mount:
#    volumes:
#      - /opt/mcp-knowledge/models_cache:/app/models_cache
docker compose config | grep -A2 models_cache

# 4. Re-run model placement from artifacts
docker compose down
rm -rf /opt/mcp-knowledge/models_cache
mkdir -p /opt/mcp-knowledge/models_cache/bge-m3
cp -r artifacts/models/bge-m3/* /opt/mcp-knowledge/models_cache/bge-m3/
docker compose up -d
```

### 5.2 "pip install fails with 'No matching distribution'"

**Symptoms:** `pip install --no-index --find-links wheelhouse/` fails.

**Cause:** Missing wheel for the platform or Python version.

**Fix:**
```bash
# 1. Check Python version matches build host
python3 --version  # must be 3.11.x on both machines

# 2. Check platform tag
python3 -c "import pip._internal.utils.compatibility_tags; print(list(pip._internal.utils.compatibility_tags.get_supported()))" 2>/dev/null \
  || python3 -c "import sysconfig; print(sysconfig.get_platform())"

# 3. Re-download wheels for correct platform on build host
pip download --platform manylinux2014_x86_64 --python-version 311 \
  --only-binary=:all: -r requirements.txt -d wheelhouse/

# 4. Include source distributions as fallback
pip download -r requirements.txt -d wheelhouse/
```

### 5.3 "Docker images don't match platform"

**Symptoms:** `docker load` succeeds but `docker compose up` fails with "exec format error".

**Cause:** Images built for different CPU architecture (arm64 vs amd64).

**Fix:**
```bash
# 1. Check host architecture
uname -m
# Expected: x86_64 (amd64) or aarch64 (arm64)

# 2. Check loaded image architecture
docker inspect qdrant/qdrant:v1.13.4 | jq '.[0].Architecture'

# 3. On build host, pull correct platform:
docker pull --platform linux/amd64 qdrant/qdrant:v1.13.4
docker pull --platform linux/amd64 python:3.11-slim
docker save qdrant/qdrant:v1.13.4 python:3.11-slim -o images/images.tar

# 4. Re-pack and transfer bundle
```

### 5.4 "Qdrant container can't start — port conflict"

**Symptoms:** `docker compose up` fails: "port is already allocated".

**Fix:**
```bash
# Check if something is already on Qdrant ports
ss -tlnp | grep -E "633[3-4]"
lsof -i :6333
lsof -i :6334

# If ports are in use, stop the conflicting process or change ports in docker-compose.yml
```

### 5.5 "Server starts but search returns empty"

**Symptoms:** `search_knowledge` returns `[]` but knowledge files exist.

**Cause:** Empty Qdrant collection — data not indexed yet.

**Fix:**
```bash
# Trigger reindex from SSOT
MCP_WRITE_KEY=$(python3 -c "import json, os; from dotenv import load_dotenv; load_dotenv(); print(json.loads(os.getenv('MCP_WRITE_KEYS','[\"\"]'))[0])")

curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: $MCP_WRITE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"reindex-1","method":"tools/call","params":{"name":"reindex","arguments":{}}}'
```

### 5.6 "Outbound network detected"

**Symptoms:** `tcpdump` captures outbound packets.

**Common leak vectors:**

| Component | Check | Fix |
|-----------|-------|-----|
| Docker DNS | `docker compose exec mcp-server nslookup google.com` | Set `dns: 127.0.0.1` in docker-compose or configure iptables |
| HuggingFace Hub | `docker compose logs mcp-server \| grep -i "huggingface\|hf.co"` | Verify `HF_HUB_OFFLINE=1` in `.env` |
| Python package check | `docker compose exec mcp-server pip list --outdated` | Remove `--outdated` from any startup scripts |
| Qdrant telemetry | Check Qdrant config | Set `telemetry_disabled: true` in qdrant config |

---

## 6. Air-gap Architecture Notes

### 6.1 What Works Without Internet

| Component | Offline Capability |
|-----------|-------------------|
| **Qdrant** (vector DB) | ✅ Fully offline — in-memory + disk storage |
| **BGE-M3 embedding** | ✅ Offline with `HF_HUB_OFFLINE=1` + pre-downloaded model |
| **Markdown store** (git) | ✅ Fully offline — local git repo |
| **MCP JSON-RPC API** | ✅ Fully offline — FastAPI + uvicorn |
| **Knowledge indexing** | ✅ Fully offline — in-process pipeline |
| **Health checks** | ✅ Fully offline — local services only |

### 6.2 Layered Defense for Network Isolation

```
Layer 1: Environment variables
  HF_HUB_OFFLINE=1           # HuggingFace Hub
  TRANSFORMERS_OFFLINE=1     # Transformers library
  SENTENCE_TRANSFORMERS_HOME=/app/models_cache  # Model path

Layer 2: Docker network isolation
  docker-compose.yml:
    networks:
      internal:
        driver: bridge
        internal: true       # No external access

Layer 3: Host firewall (optional, for defense-in-depth)
  iptables -A OUTPUT -d 0.0.0.0/0 -j DROP  # Block all outbound
  iptables -I OUTPUT -d 127.0.0.0/8 -j ACCEPT  # Allow localhost
```

### 6.3 Regular Air-gap Validation

Run this checklist every **30 days** or before each production deployment:

```bash
# Automated validation script (save as scripts/validate-airgap.sh)
#!/usr/bin/env bash
set -euo pipefail

echo "=== Air-gap Validation $(date) ==="

# 1. Health
echo -n "Health: "
curl -sf http://localhost:8000/health/live && echo "PASS" || echo "FAIL"

# 2. Network
echo -n "Network isolation: "
PACKETS=$(sudo timeout 10 tcpdump -i any not host localhost -c 1 2>&1 | grep -c "captured" || true)
[ "$PACKETS" = "0" ] && echo "PASS (no outbound)" || echo "FAIL ($PACKETS packets)"

# 3. Tools
echo -n "MCP tools: "
TOOLS=$(curl -sf -X POST http://localhost:8000/mcp \
  -H "X-API-Key: ${MCP_READ_KEY:-test}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python3 -c "import sys,json; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null || echo "0")
[ "$TOOLS" -ge 9 ] && echo "PASS ($TOOLS tools)" || echo "FAIL ($TOOLS tools)"

echo "=== Done ==="
```

---

## Appendix A: Bundle Contents Reference

```
mcp-kb-airgap-bundle.tar.gz
├── artifacts/
│   ├── images/
│   │   └── images.tar              # qdrant/qdrant:v1.13.4 + python:3.11-slim
│   ├── wheelhouse/
│   │   ├── fastapi-*.whl           # + dependencies
│   │   ├── qdrant_client-*.whl
│   │   ├── sentence_transformers-*.whl
│   │   └── ...                     # ~30-50 wheels
│   ├── models/
│   │   └── bge-m3/                 # (or models--BAAI--bge-m3/)
│   │       ├── config.json
│   │       ├── tokenizer.json
│   │       ├── tokenizer_config.json
│   │       ├── vocab.txt
│   │       ├── special_tokens_map.json
│   │       ├── model.safetensors   # ~560 MB
│   │       └── ...                 # sentence_bert_config.json, modules.json, etc.
│   └── CHECKSUMS.sha256
├── docker-compose.yml
├── .env.example
├── Makefile
└── scripts/
    └── offline-deploy.sh
```
