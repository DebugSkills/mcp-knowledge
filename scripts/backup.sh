#!/usr/bin/env bash
# backup.sh — бэкап Qdrant (snapshot) + Markdown SSOT (git push / tar)
#            + documents blob-store (Фаза 0, code-2026-10-02-bibliography)
# Использование: ./backup.sh [--no-ssot] [--no-qdrant] [--no-documents] [--test-restore]
#   --test-restore  Проверить полный цикл: snapshot → restore → verify → cleanup
# Cron: ежедневно в 3:00
#
# Контракт exit-кодов (Н10, code-2026-10-05-deploy-host-mechanism):
#   0 — все шаги ок (или осознанные skip: нет .env / нет данных / не воскресенье);
#   1 — один или несколько шагов провалились: прогон доведён до конца (провал
#       шага НЕ прерывает остальные), провалившиеся шаги перечислены в финальной
#       строке «Backup completed WITH ERRORS (…)». Потребитель: preflight-бэкап
#       ansible/playbooks/update.yml — rc=1 => STOP до мутаций.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# DATA_ROOT (code-2026-09-22-002): корень данных ВНЕ git-клона. Env-override:
# прод передаёт DATA_ROOT через crontab-env (deploy.yml, cron-секция), cron-скрипты
# не умеют .env; дефолт = прежнее dev-поведение (data внутри клона).
DATA_ROOT="${DATA_ROOT:-$PROJECT_DIR/data}"
BACKUP_DIR="$DATA_ROOT/backups"
SNAPSHOT_DIR="$DATA_ROOT/qdrant/snapshots"
KNOWLEDGE_DIR="$PROJECT_DIR/../knowledge"
DOCUMENTS_DIR="$DATA_ROOT/documents"   # blob-store оригиналов (Фаза 0, code-2026-10-02-bibliography)
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RETENTION_DAYS=7

QDDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

# --- Общие утилиты ---

# get_collection_points <collection_name> → число
get_collection_points() {
    local collection="$1"
    curl -s "${QDDRANT_URL}/collections/${collection}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null
}

# --- Qdrant snapshot ---
# 2026-08-09: снапшот ВСЕХ коллекций, КРОМЕ ws-*.
# Решение владельца (docs/qdrant-ws-collections-message.md в feature/Svyazi):
# коллекции ws-* НЕ принадлежат Svyazi (подтверждено, 2026-08-09) — они чужие,
# в бэкапы mcp-knowledge не включаются. Снапшотим только свои (knowledge и др.).
create_qdrant_snapshot() {
    echo "[$(date -Iseconds)] Creating Qdrant snapshots (excluding ws-*)..."
    local collections
    collections=$(curl -s "${QDDRANT_URL}/collections" \
        | python3 -c "import sys,json; print(' '.join(c['name'] for c in json.load(sys.stdin)['result']['collections'] if not c['name'].startswith('ws-')))" 2>/dev/null)
    if [ -z "$collections" ]; then
        echo "WARN: Qdrant snapshot failed (server not running?). Skipping."
        return 1
    fi
    echo "    Collections: ${collections}"
    # Н10 (code-2026-10-05-deploy-host-mechanism): ПРЯМАЯ rc-семантика —
    # rc=0 = полный успех (раньше был ok=1 + `return $ok`: успех → rc=1, а провал
    # создания снапшота → rc=0, т.е. инверсия). Ранний return 1 (сервер не
    # запущен / нет коллекций) выше — сохранён намеренно.
    local c rc=0
    for c in $collections; do
        local resp actual
        resp=$(curl -s -X POST "${QDDRANT_URL}/collections/${c}/snapshots" \
            -H "Content-Type: application/json" \
            -d '{"name": "backup-'"${TIMESTAMP}"'"}' 2>&1)
        # Qdrant игнорирует переданный name и генерирует фактическое имя файла
        # ({collection}-{id}-{timestamp}.snapshot) — берём его из ответа API.
        actual=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null)
        if [ -n "$actual" ]; then
            echo "    ✔ ${c}: ${actual}"
            validate_snapshot "${c}" "${actual}" || rc=1
        else
            echo "    ✖ ${c}: ${resp}"
            rc=1
        fi
    done
    return "$rc"
}

# --- Snapshot validation (G1.1) ---
# validate_snapshot <collection> <snapshot_name>
validate_snapshot() {
    local collection="$1"
    local snapshot_name="$2"
    local snapshot_file="${SNAPSHOT_DIR}/${collection}/${snapshot_name}"

    echo "[$(date -Iseconds)] Validating snapshot: ${snapshot_name}..."

    if [ ! -f "${snapshot_file}" ]; then
        echo "ERROR: Snapshot file not found: ${snapshot_file}"
        echo "       Check that Qdrant snapshots directory is mounted correctly."
        exit 1
    fi

    local size
    size=$(stat -c%s "${snapshot_file}" 2>/dev/null || echo 0)
    if [ "${size}" -eq 0 ]; then
        echo "ERROR: Snapshot file is empty: ${snapshot_file}"
        exit 1
    fi

    local size_human
    if command -v numfmt &>/dev/null; then
        size_human=$(numfmt --to=iec "${size}")
    else
        size_human="${size} bytes"
    fi

    echo "OK: Snapshot validated — ${snapshot_name} (size: ${size_human})"
    return 0
}

# --- Test Restore (G1.2) ---
# Полный цикл: snapshot → restore to temp collection → verify point count → cleanup
test_restore() {
    echo "=== Test Restore: Snapshot → Temp Collection → Verify → Cleanup ==="
    echo ""

    # Step 1: Create a fresh snapshot for testing
    echo "[$(date -Iseconds)] Step 1/5: Creating test snapshot 'test-restore-${TIMESTAMP}'..."
    local snapshot_resp test_snapshot
    snapshot_resp=$(curl -s -X POST "${QDDRANT_URL}/collections/knowledge/snapshots" \
        -H "Content-Type: application/json" \
        -d '{"name": "test-restore-'"${TIMESTAMP}"'"}' 2>&1) || {
        echo "ERROR: Failed to create snapshot for test restore. Is Qdrant running?"
        return 1
    }
    # Qdrant генерирует фактическое имя файла — берём из ответа API
    test_snapshot=$(echo "$snapshot_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null)
    echo "    Actual snapshot: ${test_snapshot}"
    echo "    Response: ${snapshot_resp}"

    # Step 1b: Validate the snapshot
    validate_snapshot "knowledge" "${test_snapshot}" || return 1
    echo ""

    # Step 2: Get point count of the original collection
    echo "[$(date -Iseconds)] Step 2/5: Getting point count of 'knowledge'..."
    local orig_count
    orig_count=$(get_collection_points "knowledge") || {
        echo "ERROR: Failed to get point count of 'knowledge' collection"
        return 1
    }
    echo "    Original collection 'knowledge' has ${orig_count} points"
    echo ""

    # Step 3: Restore snapshot to a temporary collection
    local test_collection="knowledge_restore_test"
    echo "[$(date -Iseconds)] Step 3/5: Restoring snapshot to '${test_collection}'..."
    local restore_resp
    restore_resp=$(curl -s -X PUT "${QDDRANT_URL}/collections/${test_collection}/snapshots/recover" \
        -H "Content-Type: application/json" \
        -d '{"location": "file:///qdrant/storage/snapshots/knowledge/'"${test_snapshot}"'"}' 2>&1) || {
        echo "ERROR: Failed to restore snapshot to '${test_collection}'"
        return 1
    }
    echo "    Response: ${restore_resp}"

    # Qdrant processes snapshot recovery asynchronously — wait for it
    echo "    Waiting for restore to complete..."
    sleep 3
    echo ""

    # Step 4: Verify point count matches
    echo "[$(date -Iseconds)] Step 4/5: Verifying point count of '${test_collection}'..."
    local restored_count
    restored_count=$(get_collection_points "${test_collection}") || {
        echo "ERROR: Failed to get point count of '${test_collection}'"
        # Attempt cleanup even on failure
        curl -s -X DELETE "${QDDRANT_URL}/collections/${test_collection}" > /dev/null 2>&1 || true
        return 1
    }
    echo "    Restored collection '${test_collection}' has ${restored_count} points"

    if [ "${orig_count}" -eq "${restored_count}" ]; then
        echo ""
        echo "✅ RESTORE TEST PASSED: Point counts match (${orig_count} == ${restored_count})"
    else
        echo ""
        echo "❌ RESTORE TEST FAILED: Point count mismatch"
        echo "   Original:  ${orig_count}"
        echo "   Restored:  ${restored_count}"
        # Cleanup before exiting with error
        curl -s -X DELETE "${QDDRANT_URL}/collections/${test_collection}" > /dev/null 2>&1 || true
        return 1
    fi
    echo ""

    # Step 5: Cleanup — delete temporary collection
    echo "[$(date -Iseconds)] Step 5/5: Cleaning up temporary collection '${test_collection}'..."
    local delete_resp
    delete_resp=$(curl -s -X DELETE "${QDDRANT_URL}/collections/${test_collection}" 2>&1) || {
        echo "WARN: Failed to delete temporary collection '${test_collection}' (manual cleanup may be required)"
    }
    echo "    Deleted: ${delete_resp}"

    echo ""
    echo "=== Test Restore completed successfully ==="
    return 0
}

# --- SSOT backup (git push to bare remote) ---
# P0-1 (code-2026-09-22-002): git -C вместо cd — cd без возврата ломал
# последующие функции с относительными путями (backup_console_state тихо скипала).
backup_ssot_git() {
    echo "[$(date -Iseconds)] Backing up SSOT (git push to bare)..."
    if [ -d "$KNOWLEDGE_DIR/.git" ]; then
        git -C "$KNOWLEDGE_DIR" push backup main 2>&1 || {
            echo "WARN: git push backup failed. Falling back to tar."
            backup_ssot_tar
        }
    else
        echo "WARN: $KNOWLEDGE_DIR is not a git repo. Falling back to tar."
        backup_ssot_tar
    fi
}

# --- SSOT backup (tar fallback) ---
backup_ssot_tar() {
    echo "[$(date -Iseconds)] Backing up SSOT (tar)..."
    mkdir -p "$BACKUP_DIR"
    tar -czf "$BACKUP_DIR/knowledge-${TIMESTAMP}.tar.gz" \
        -C "$(dirname "$KNOWLEDGE_DIR")" "$(basename "$KNOWLEDGE_DIR")" 2>&1
    echo "[$(date -Iseconds)] SSOT tar: $BACKUP_DIR/knowledge-${TIMESTAMP}.tar.gz"
}

# --- Console state backup (036 §2.2: staging-протокол ПОЛНОГО стейта) ---
# Состав staging (зеркалит layout DATA_ROOT → в таре console/, tokens/):
#   users.jsonl (pbkdf2 невосстановимы) + users_audit.jsonl + storage_secret
#   + access_requests.db (снапшот VACUUM INTO под КАНОНИЧЕСКИМ именем)
#   + tokens/ целиком (если есть).
# Tar собирает STAGING, живой console/ НЕ тарим: живая БД и её -journal/-wal/-shm
# в архив не попадают по построению.
# Guard «каталог только с .db» (бывш. дыра :227): jsonl ИЛИ .db ИЛИ secret.
# Guard пустой БД: файла нет → INFO-скип; 0 байт / 0 таблиц → ERROR + rc=1.
backup_console_state() {
    echo "[$(date -Iseconds)] Backing up console state (staging-протокол, 036 §2.2)..."
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    local console_dir="$DATA_ROOT/console" tokens_dir="$DATA_ROOT/tokens"
    local rc=0

    local has_state=false dir
    for dir in "$console_dir" "$tokens_dir"; do
        [ -d "$dir" ] || continue
        if ls "$dir"/*.jsonl >/dev/null 2>&1 || ls "$dir"/*.db >/dev/null 2>&1 \
           || [ -f "$dir/storage_secret" ]; then
            has_state=true
        fi
    done
    if [ "$has_state" = false ]; then
        echo "[$(date -Iseconds)] Console state: файлов стейта нет — пропуск (не ошибка)."
        return 0
    fi

    # Свежий staging на каждый прогон: VACUUM INTO не падает на существующий
    # target («already exists») — staging всегда чистый.
    local staging
    staging="$(mktemp -d)"
    chmod 700 "$staging"
    mkdir -p "$staging/console"

    local f
    for f in users.jsonl users_audit.jsonl storage_secret; do
        if [ -f "$console_dir/$f" ]; then
            cp -p "$console_dir/$f" "$staging/console/$f"
            echo "    + console/$f"
        fi
    done

    local db="$console_dir/access_requests.db"
    if [ ! -f "$db" ]; then
        echo "    · access_requests.db нет — скип (свежая установка, INFO)"
    elif [ ! -s "$db" ]; then
        echo "    ✖ ERROR: access_requests.db существует, но 0 байт (битая?) — снапшота нет"
        rc=1
    elif ! python3 -c "import sqlite3,sys; conn=sqlite3.connect(sys.argv[1]); sys.exit(0 if conn.execute('SELECT count(*) FROM sqlite_master').fetchone()[0] > 0 else 3)" "$db"; then
        echo "    ✖ ERROR: access_requests.db без таблиц/битая — снапшот НЕ сделан"
        rc=1
    elif python3 - "$db" "$staging/console/access_requests.db" <<'PY'
import sqlite3, sys

src, dst = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(src, timeout=5.0)
conn.execute("PRAGMA busy_timeout=5000")  # параллельная запись не роняет бэкап
conn.execute("VACUUM INTO ?", (dst,))
conn.close()
PY
    then
        echo "    + console/access_requests.db (VACUUM INTO, каноническое имя)"
    else
        echo "    ✖ ERROR: VACUUM INTO не удался — снапшота нет"
        rc=1
    fi

    if [ -d "$tokens_dir" ]; then
        mkdir -p "$staging/tokens"
        cp -Rp "$tokens_dir/." "$staging/tokens/"
        # страховка: sqlite-спутники в архив не едут (в токенах их быть не должно)
        find "$staging/tokens" \( -name '*.db-journal' -o -name '*.db-wal' \
            -o -name '*.db-shm' \) -delete 2>/dev/null || true
        echo "    + tokens/"
    fi

    local tar_items=()
    [ -n "$(ls -A "$staging/console" 2>/dev/null)" ] && tar_items+=(console)
    [ -d "$staging/tokens" ] && tar_items+=(tokens)
    if [ ${#tar_items[@]} -eq 0 ]; then
        echo "[$(date -Iseconds)] Console state: staging пуст — тар не создан (rc=$rc)."
        rm -rf "$staging"
        return "$rc"
    fi
    # -C staging: относительные имена (console/, tokens/) — как прежде;
    # распаковка restore-скриптом в любой каталог без разворока путей.
    tar -czf "$BACKUP_DIR/console-state-${TIMESTAMP}.tar.gz" \
        -C "$staging" "${tar_items[@]}" 2>&1
    rm -rf "$staging"
    echo "[$(date -Iseconds)] Console state tar: $BACKUP_DIR/console-state-${TIMESTAMP}.tar.gz"
    return "$rc"
}

# --- Secrets backup (.env; P1-4/P2-9, code-2026-09-22-002) ---
# .env = ключи всех уровней + пароль консоли — единственный незеркалируемый
# конфиг. vault.yml в тар НЕ включаем (сам зашифрован ansible-vault, едет на
# offsite отдельно). Guard P2-9: dev .env может отсутствовать (gitignored,
# необязателен) — skip с сообщением, НЕ ошибка (set -euo pipefail не роняет nightly).
backup_secrets() {
    echo "[$(date -Iseconds)] Backing up secrets (.env)..."
    if [ -f "$PROJECT_DIR/.env" ]; then
        mkdir -p "$BACKUP_DIR"
        chmod 700 "$BACKUP_DIR"
        tar -czf "$BACKUP_DIR/secrets-${TIMESTAMP}.tar.gz" -C "$PROJECT_DIR" .env 2>&1
        chmod 600 "$BACKUP_DIR/secrets-${TIMESTAMP}.tar.gz"
        echo "[$(date -Iseconds)] Secrets tar: $BACKUP_DIR/secrets-${TIMESTAMP}.tar.gz (mode 600)"
    else
        echo "[$(date -Iseconds)] secrets: .env отсутствует — пропуск (не ошибка)."
    fi
}

# --- Errors state backup (Error→Rule Ф4, code-2026-09-22-003) ---
# Sink-состояние цикла Error→Rule: config.json + alert_state.json +
# aggregates/signatures.json. Без raw-событий (восстанавливаемы ретро-сканом
# логов) и БЕЗ notify.json — TG-токен в тары НЕ попадает (P2-10; notify.json
# рендерится заново ansible errors.yml setup из vault).
backup_errors_state() {
    # 006: audit обращений errors_query ([ERRORS_QUERY]-маркеры) в tar НЕ входит
    # BY DESIGN: аудит живёт в docker logs (json-file 10m×3) и raw sink (TTL 90d,
    # collector routine-P3) — см. спеку 006 §5/P2-8, prune не трогает audit/.
    echo "[$(date -Iseconds)] Backing up errors state (config/aggregates/alert_state)..."
    local sink="$DATA_ROOT/logs/errors"
    if [ ! -d "$sink" ]; then
        echo "[$(date -Iseconds)] errors state: sink отсутствует — пропуск (не ошибка)."
        return 0
    fi
    local items=()
    local f
    for f in config.json alert_state.json; do
        [ -f "$sink/$f" ] && items+=("$f")
    done
    [ -f "$sink/aggregates/signatures.json" ] && items+=("aggregates/signatures.json")
    if [ ${#items[@]} -eq 0 ]; then
        echo "[$(date -Iseconds)] errors state: нет файлов состояния — пропуск."
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    # -C "$sink": относительные имена (config.json, aggregates/) — как console-state
    tar -czf "$BACKUP_DIR/errors-state-${TIMESTAMP}.tar.gz" -C "$sink" "${items[@]}" 2>&1
    echo "[$(date -Iseconds)] errors state tar: $BACKUP_DIR/errors-state-${TIMESTAMP}.tar.gz"
}

# --- Documents blob-store backup (Фаза 0, code-2026-10-02-bibliography) ---
# Каталог data/documents (blob-store оригиналов) — в бэкап-сет + sha256-манифест.
# Манифест (детерминированный, отсортированный): `<sha256>  <относительный-путь>`
# на каждый blob — restore/verify сверяет файлы по хешу (гейт A4). Пустой каталог
# или отсутствие каталога → no-op с сообщением (гейт A6 — бэкап не падает).
backup_documents() {
    local docs_dir="$DOCUMENTS_DIR"
    echo "[$(date -Iseconds)] Backing up documents (blob-store)..."
    if [ ! -d "$docs_dir" ]; then
        echo "[$(date -Iseconds)] documents: каталог $docs_dir отсутствует — пропуск (не ошибка)."
        return 0
    fi
    # Есть ли хоть один файл? Пусто → no-op (гейт A6).
    if [ -z "$(find "$docs_dir" -type f -print -quit 2>/dev/null)" ]; then
        echo "[$(date -Iseconds)] documents: каталог пуст — бэкап пропущен (no-op)."
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    local tar_file="$BACKUP_DIR/documents-${TIMESTAMP}.tar.gz"
    local manifest_file="${tar_file}.sha256"
    tar -czf "$tar_file" -C "$docs_dir" . 2>&1
    # Манифест: sha256 каждого файла (относительный путь от корня docs), сортировка
    # по пути — детерминизм (одинаковый манифест при неизменном содержимом).
    # Пути blob'ов — hex-имена (sha256), без пробелов/newline → формат sha256sum безопасен.
    (cd "$docs_dir" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) > "$manifest_file"
    chmod 600 "$manifest_file"
    echo "[$(date -Iseconds)] documents tar: $tar_file ($(stat -c%s "$tar_file" 2>/dev/null || echo 0) bytes)"
    echo "[$(date -Iseconds)] documents manifest: $manifest_file ($(wc -l < "$manifest_file") файлов)"
}

# --- Ротация старых бэкапов ---
rotate_backups() {
    echo "[$(date -Iseconds)] Rotating backups older than ${RETENTION_DAYS} days..."
    find "$BACKUP_DIR" -name "knowledge-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    find "$BACKUP_DIR" -name "console-state-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    find "$BACKUP_DIR" -name "secrets-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # Error→Rule state (Ф4): тот же retention, что остальные тары
    find "$BACKUP_DIR" -name "errors-state-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # Documents blob-store (Фаза 0): тар + манифест — тот же retention (О-5: общий)
    find "$BACKUP_DIR" -name "documents-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    find "$BACKUP_DIR" -name "documents-*.tar.gz.sha256" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # P2-5/P2-b: pre-restore-копии (создаются restore-плейбуком) — паттерн `pre-restore-*`
    # ловит и легитимные тары, и мусорные имена вида `pre-restore-console-$(date`
    # (следствие старого command+$(date …) до фикса P1-A)
    find "$BACKUP_DIR" -name "pre-restore-*" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # 2026-08-09: ротация Qdrant-снапшотов (раньше копились бесконечно).
    # Файлы снапшотов теперь в bind-mount (data/qdrant/snapshots) — удаляем по mtime.
    find "$SNAPSHOT_DIR" -name "backup-*.snapshot" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # остаточные .checksum файлы
    find "$SNAPSHOT_DIR" -name "backup-*.snapshot.checksum" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true
    # Ф3 P2-7: weekly-каталоги старше ~4 недель (по mtime каталога, имена не парсятся)
    find "$SNAPSHOT_DIR/weekly" -mindepth 1 -maxdepth 1 -mtime +28 -exec rm -rf {} + 2>/dev/null || true
}

# --- Weekly-4 снапшоты (P2-7, code-2026-09-22-002 Ф3) ---
# Имена Qdrant-снапшотов произвольные ({collection}-{uuid}-{ts}.snapshot) —
# find по суффсуксу даты невозможен. Вместо этого: по воскресеньям (date +%u = 7)
# каталог weekly/%G-W%V, куда КОПИРУЕТСЯ последний по mtime снапшот каждой
# коллекции; ротация по mtime каталогов (-mtime +28 ≈ 4 недели, имена не парсятся).
# Offsite (§11) забирает только weekly/-поддерево.
backup_weekly_snapshots() {
    if [ "$(date +%u)" != "7" ]; then
        return 0
    fi
    local week_dir="$SNAPSHOT_DIR/weekly/$(date +%G-W%V)"
    echo "[$(date -Iseconds)] Weekly-4: копируем последние снапшоты в ${week_dir}..."
    mkdir -p "$week_dir"
    local coll_dir latest
    for coll_dir in "$SNAPSHOT_DIR"/*/; do
        [ -d "$coll_dir" ] || continue
        local cname
        cname="$(basename "$coll_dir")"
        [ "$cname" = "weekly" ] && continue
        latest="$(ls -t "$coll_dir"/*.snapshot 2>/dev/null | head -1 || true)"
        if [ -n "$latest" ]; then
            cp -p "$latest" "$week_dir/"
            echo "    ✔ ${cname}: $(basename "$latest")"
        else
            echo "    — ${cname}: снапшотов нет, пропуск"
        fi
    done
    echo "[$(date -Iseconds)] Weekly-4: готово ($(ls "$week_dir" | wc -l) файлов)."
}

# --- Weekly documents-копия (Фаза 0, code-2026-10-02-bibliography) ---
# Отдельная от qdrant-weekly: documents НЕ зависят от Qdrant (P2-c) — при
# --no-qdrant в воскресенье documents-копия всё равно делается. Offsite забирает
# weekly целиком (О-1); манифест копируем рядом для restore-verify (гейт A4).
backup_weekly_documents() {
    if [ "$(date +%u)" != "7" ]; then
        return 0
    fi
    local week_dir="$SNAPSHOT_DIR/weekly/$(date +%G-W%V)"
    echo "[$(date -Iseconds)] Weekly: documents-копия в ${week_dir}..."
    mkdir -p "$week_dir"
    local docs_tar
    docs_tar="$(ls -t "$BACKUP_DIR"/documents-*.tar.gz 2>/dev/null | head -1 || true)"
    if [ -n "$docs_tar" ]; then
        cp -p "$docs_tar" "$week_dir/"
        cp -p "${docs_tar}.sha256" "$week_dir/" 2>/dev/null || true
        echo "    ✔ documents: $(basename "$docs_tar")"
    else
        echo "    — documents: таров нет, пропуск"
    fi
}

# --- Console-state untar-drill (036 §2.2: presence + канонический путь + integrity) ---
# Последний console-state-тар: tar -tzf (целостность) → untar во временный каталог →
#   1) ПУСТОЙ архив (нет console/ и tokens/) → FAIL — закрыт ложный rc=0 «ok»
#      от пустого find|while;
#   2) presence-ассерты ДО цикла: файл, существующий на источнике, обязан быть
#      в архиве и непустым — иначе FAIL ПОИМЁННО;
#   3) БД заявок: канонический путь console/access_requests.db (имя снапшота =
#      каноническое → «рядом лежащий неиспользованный» исключён по построению);
#      PRAGMA integrity_check + count; живая БД без снапшота в архиве → FAIL;
#   4) каждая строка *.jsonl парсится json.loads.
# Возвращает 0/1; отсутствие тара — skip (return 0 с сообщением).
verify_console_drill() {
    echo "[$(date -Iseconds)] Console untar-drill (036 §2.2)..."
    local tar_file
    tar_file="$(ls -t "$BACKUP_DIR"/console-state-*.tar.gz 2>/dev/null | head -1 || true)"
    if [ -z "$tar_file" ]; then
        echo "    — console-state-таров нет — drill пропущен (не ошибка)."
        return 0
    fi
    echo "    Tar: $tar_file"
    tar -tzf "$tar_file" > /dev/null || {
        echo "    ✖ tar -tzf FAILED (архив битый): $tar_file"
        return 1
    }
    local tmp
    tmp="$(mktemp -d)"
    tar -xzf "$tar_file" -C "$tmp" || { rm -rf "$tmp"; return 1; }
    local rc=0

    # Пустой архив ≠ «ok»
    if [ ! -d "$tmp/console" ] && [ ! -d "$tmp/tokens" ]; then
        echo "    ✖ FAIL: архив пуст — нет ни console/, ни tokens/"
        rm -rf "$tmp"
        return 1
    fi

    # Presence-ассерты обязательных файлов (ожидание = наличие на источнике)
    local f
    for f in users.jsonl users_audit.jsonl storage_secret; do
        if [ -s "$DATA_ROOT/console/$f" ]; then
            if [ -s "$tmp/console/$f" ]; then
                echo "    ✔ console/$f: presence ok"
            else
                echo "    ✖ FAIL: console/$f есть на источнике, но ОТСУТСТВУЕТ/пуст в архиве"
                rc=1
            fi
        fi
    done
    if [ -d "$DATA_ROOT/tokens" ] && [ -n "$(ls -A "$DATA_ROOT/tokens" 2>/dev/null)" ]; then
        if [ -d "$tmp/tokens" ] && [ -n "$(ls -A "$tmp/tokens" 2>/dev/null)" ]; then
            echo "    ✔ tokens/: presence ok"
        else
            echo "    ✖ FAIL: tokens/ есть на источнике, но ОТСУТСТВУЕТ/пуст в архиве"
            rc=1
        fi
    fi

    # БД заявок: канонический путь + integrity + count
    if [ -f "$tmp/console/access_requests.db" ]; then
        local dbcheck
        dbcheck="$(python3 - "$tmp/console/access_requests.db" <<'PY'
import sqlite3, sys

conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
try:
    count = conn.execute("SELECT count(*) FROM access_requests").fetchone()[0]
except sqlite3.Error:
    count = -1
print(f"{integrity} {count}")
PY
)" || dbcheck="error -1"
        local integrity="${dbcheck% *}" dbcount="${dbcheck#* }"
        if [ "$integrity" = "ok" ] && [ "$dbcount" -ge 0 ] 2>/dev/null; then
            echo "    ✔ console/access_requests.db: integrity=ok, заявок: $dbcount"
            [ "$dbcount" -eq 0 ] && echo "      (warning: заявок в снапшоте не было)"
        else
            echo "    ✖ FAIL: access_requests.db integrity=$integrity count=$dbcount"
            rc=1
        fi
    elif [ -s "$DATA_ROOT/console/access_requests.db" ]; then
        echo "    ✖ FAIL: живая БД заявок есть, снапшота в архиве НЕТ"
        rc=1
    else
        echo "    · access_requests.db в архиве нет (легальный скип свежей установки)"
    fi

    # JSONL-валидация (каждая строка — валидный JSON)
    local jsonl jrc
    while IFS= read -r jsonl; do
        local n
        n="$(python3 -c "
import sys, json
ok = 0
with open(sys.argv[1], encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
            ok += 1
        except json.JSONDecodeError as e:
            print(f'BAD line {i}: {e}', file=sys.stderr)
            sys.exit(1)
print(ok)
" "$jsonl")" || jrc=1
        if [ "${jrc:-0}" -eq 0 ]; then
            echo "    ✔ $(basename "$jsonl"): ${n} строк, все валидный JSON"
        else
            echo "    ✖ $(basename "$jsonl"): битые строки JSON"
            rc=1
            break
        fi
    done < <(find "$tmp" -name '*.jsonl' -type f | sort)
    rm -rf "$tmp"
    return "$rc"
}

# --- Documents untar-drill (Фаза 0): tar-целостность + выборочная sha256-сверка ---
# Последний documents-тар: tar -tzf (целостность) → untar во временный каталог →
# сверка первых 3 файлов манифеста по sha256 (гейт A3 — быстрый hash-check;
# полная сверка всех файлов — в restore scope=documents, гейт A4). Возвращает 0/1;
# отсутствие тара — skip (return 0 с сообщением).
verify_documents_drill() {
    echo "[$(date -Iseconds)] Documents drill (Фаза 0)..."
    local tar_file manifest_file
    tar_file="$(ls -t "$BACKUP_DIR"/documents-*.tar.gz 2>/dev/null | head -1 || true)"
    manifest_file="${tar_file}.sha256"
    if [ -z "$tar_file" ]; then
        echo "    — documents-таров нет — drill пропущен (не ошибка)."
        return 0
    fi
    echo "    Tar: $tar_file"
    tar -tzf "$tar_file" > /dev/null || {
        echo "    ✖ tar -tzf FAILED (архив битый): $tar_file"
        return 1
    }
    if [ ! -f "$manifest_file" ]; then
        echo "    ✖ FAIL: манифест отсутствует: $manifest_file"
        return 1
    fi
    local tmp
    tmp="$(mktemp -d)"
    tar -xzf "$tar_file" -C "$tmp" || { rm -rf "$tmp"; return 1; }
    local rc=0 checked=0 total line expected rel actual
    total="$(wc -l < "$manifest_file")"
    # Детерминированная выборочная сверка: первые 3 файла отсортированного манифеста
    # (файлов <3 → сверяем все). Пустой манифест не бывает (tar создаётся только при
    # непустом каталоге) — но это признак порчи, FAIL.
    if [ "$total" -eq 0 ]; then
        echo "    ✖ FAIL: манифест пуст (tar создан, но манифест без файлов)"
        rm -rf "$tmp"
        return 1
    fi
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        read -r expected rel <<< "$line"
        actual="$(sha256sum "$tmp/$rel" 2>/dev/null | awk '{print $1}')"
        if [ "$actual" = "$expected" ]; then
            checked=$((checked + 1))
            echo "    ✔ $rel"
        else
            echo "    ✖ FAIL: $rel (sha256 mismatch: ожидался $expected, получен $actual)"
            rc=1
        fi
    done < <(sed -n '1,3p' "$manifest_file")
    rm -rf "$tmp"
    if [ "$rc" -eq 0 ]; then
        echo "    ✔ documents drill: $checked/$total (выборка) файлов сверено"
    fi
    return "$rc"
}

# --- Verify-режим (--verify, code-2026-09-22-002 Ф3; В2(б) «не молчи когда всё ок») ---
# НЕ создаёт новые бэкапы — проверяет существующие:
#   1) qdrant test-restore (ПОЛНЫЙ цикл recover — ТРЕБУЕТ живого Qdrant;
#      с --no-qdrant пропускается с сообщением — это задокументированное поведение)
#   2) sha256-контрольная сумма последних таров каждого типа (отсутствие = skip)
#   3) console untar-drill (P2-6)
# Итог: «RESTORE TEST PASSED/FAILED», non-zero exit при провале.
verify_mode() {
    echo "=== VERIFY MODE: восстановимость бэкапов ==="
    local fails=0 checks=0 rc

    # 1. Qdrant test-restore
    if [ "$NO_QDRANT" = true ]; then
        echo "[SKIP] Qdrant test-restore: --no-qdrant (требует живого Qdrant :6333)."
    else
        checks=$((checks + 1))
        test_restore && rc=0 || rc=1
        [ "$rc" -ne 0 ] && fails=$((fails + 1))
    fi

    # 2. sha256 последних таров каждого типа
    local kind f
    for kind in knowledge console-state secrets; do
        f="$(ls -t "$BACKUP_DIR"/${kind}-*.tar.gz 2>/dev/null | head -1 || true)"
        if [ -z "$f" ]; then
            echo "[SKIP] sha256 ${kind}: таров нет."
            continue
        fi
        checks=$((checks + 1))
        if sha256sum "$f"; then
            echo "    ✔ sha256 OK: $(basename "$f")"
        else
            echo "    ✖ sha256 FAILED: $f"
            fails=$((fails + 1))
        fi
    done

    # 3. Console untar-drill
    checks=$((checks + 1))
    verify_console_drill && rc=0 || rc=1
    [ "$rc" -ne 0 ] && fails=$((fails + 1))

    # 4. Documents drill (Фаза 0, code-2026-10-02-bibliography)
    if [ "$NO_DOCUMENTS" = true ]; then
        echo "[SKIP] Documents drill: --no-documents."
    else
        checks=$((checks + 1))
        verify_documents_drill && rc=0 || rc=1
        [ "$rc" -ne 0 ] && fails=$((fails + 1))
    fi

    echo ""
    if [ "$fails" -eq 0 ] && [ "$checks" -gt 0 ]; then
        echo "✅ RESTORE TEST PASSED (${checks} проверок, 0 провалов)"
        return 0
    elif [ "$checks" -eq 0 ]; then
        echo "❌ RESTORE TEST FAILED: нечего проверять (0 проверок) — бэкапов нет?"
        return 1
    else
        echo "❌ RESTORE TEST FAILED: ${fails}/${checks} проверок провалено"
        return 1
    fi
}

# --- Main ---
echo "=== MCP Knowledge Backup: ${TIMESTAMP} ==="

NO_QDRANT=false
NO_SSOT=false
NO_DOCUMENTS=false
TEST_RESTORE=false
VERIFY=false
for arg in "$@"; do
    case "$arg" in
        --no-qdrant)    NO_QDRANT=true ;;
        --no-ssot)      NO_SSOT=true ;;
        --no-documents) NO_DOCUMENTS=true ;;
        --test-restore) TEST_RESTORE=true ;;
        --verify)       VERIFY=true ;;
        --help|-h)
            echo "Usage: $0 [--no-ssot] [--no-qdrant] [--no-documents] [--test-restore] [--verify]"
            echo ""
            echo "Options:"
            echo "  --no-ssot       Skip SSOT (Markdown) backup"
            echo "  --no-qdrant     Skip Qdrant snapshot"
            echo "  --no-documents  Skip documents blob-store backup"
            echo "  --test-restore  Run full restore test cycle and exit"
            echo "  --verify        Verify existing backups (test-restore + sha256 + console-drill + documents-drill) and exit"
            echo "  --help, -h      Show this help"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $arg"
            echo "Usage: $0 [--no-ssot] [--no-qdrant] [--no-documents] [--test-restore] [--verify]"
            exit 1
            ;;
    esac
done

# G1.2: --test-restore runs a standalone validation cycle
if [ "$TEST_RESTORE" = true ]; then
    test_restore
    exit $?
fi

# Ф3 (code-2026-09-22-002): --verify проверяет существующие бэкапы
# (с --no-qdrant Qdrant-часть пропускается — работает без живого сервера).
if [ "$VERIFY" = true ]; then
    verify_mode
    exit $?
fi

# Regular backup flow
# Н10: провал qdrant-шага НЕ прерывает прогон — rc аккумулируется (паттерн
# CONSOLE_RC ниже), остальные бэкапы выполняются всегда, итоговый exit
# отдаётся ПОСЛЕ полного прогона. { … || QDRANT_RC=1; } гасит set -e внутри
# AND-списка: без braces функция — последняя команда списка, её не-0 статус
# молча убивал скрипт сразу после успешной валидации снапшота.
QDRANT_RC=0
[ "$NO_QDRANT" = false ] && { create_qdrant_snapshot || QDRANT_RC=1; }
[ "$NO_SSOT" = false ] && backup_ssot_git
# P2-1 (code-2026-10-02-bibliography): documents ДО weekly — иначе воскресный
# weekly-тар забирает прошлый прогон (backup_documents шёл после weekly).
[ "$NO_DOCUMENTS" = false ] && backup_documents       # Фаза 0: blob-store оригиналов
# P2-c: documents-weekly НЕ гейтится NO_QDRANT (documents не зависят от Qdrant)
[ "$NO_DOCUMENTS" = false ] && backup_weekly_documents
[ "$NO_QDRANT" = false ] && backup_weekly_snapshots   # P2-7: вс-копии qdrant (no-op в остальные дни)
# 036 §2.2: ошибка console-state (битая БД и т.п.) НЕ прерывает остальные
# бэкапы — rc фиксируется и отдаётся в exit ПОСЛЕ полного прогона.
CONSOLE_RC=0
backup_console_state || CONSOLE_RC=1
backup_secrets         # code-2026-09-22-002 P1-4: .env (guard P2-9 — skip без файла)
backup_errors_state    # Error→Rule Ф4: config/aggregates/alert_state (без notify.json)
rotate_backups

# Н10: итоговый rc — по аккумуляции провалов (qdrant / console); финальная
# строка называет провалившиеся шаги одной строкой.
if [ "$QDRANT_RC" -ne 0 ] || [ "$CONSOLE_RC" -ne 0 ]; then
    failed_steps=""
    if [ "$QDRANT_RC" -ne 0 ]; then failed_steps="qdrant-snapshot"; fi
    if [ "$CONSOLE_RC" -ne 0 ]; then failed_steps="${failed_steps:+${failed_steps}, }console-state"; fi
    echo "=== Backup completed WITH ERRORS (${failed_steps} — см. ✖ выше): ${TIMESTAMP} ==="
    exit 1
fi
echo "=== Backup completed: ${TIMESTAMP} ==="
