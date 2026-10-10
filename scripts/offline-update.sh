#!/usr/bin/env bash
# =============================================================================
# offline-update.sh — Air-gap ОБНОВЛЕНИЕ изолированного контура (038, Ф1)
#
# Поток (полный пакет + идемпотентное применение «только то, что нужно»):
#   1. Интернет-машина:  ./scripts/offline-update.sh pack [--with-models]
#                        → mcp-kb-update-<ISO>.tar.gz
#   2. Носитель:         ./scripts/offline-update.sh verify /media/…/mcp-kb-update-….tar.gz
#   3. Изолированный aikb: ./scripts/offline-update.sh apply-stage <пакет> \
#                          --clone /opt/mcp-knowledge/mcp-knowledge
#   Штатный путь на aikb — ansible: make -C ansible update-local BUNDLE=… (плейбук
#   повторяет те же гейты с честными changed; apply-stage — standalone-путь).
#
# Пакет: repo.git (git bundle --all) + images/<slug>.tar.gz (per-image docker
# save) + models/ (опц.) + manifest.json (target_commit, image .Id + digest
# OCI-манифеста, sha256) +
# CHECKSUMS.sha256. Применение идемпотентно: неизменный коммит → skip merge,
# совпавший .Id образа → skip load, совпавший digest модели → skip копирования.
#
# Идемпотентность — критерий приёмки: повторный прогон без изменений =
# 0 мутаций (docker load no-op / skip), контейнеры НЕ рестартят (up здесь нет).
#
# ═══ ИНВАРИАНТ КОНТУРНОЙ ИЗОЛЯЦИИ (аудит 2026-10-02) ═══
# Скрипт пишет ТОЛЬКО в: (1) клон кода (git fetch/merge --ff-only),
# (2) docker (load образов), (3) models-dir (модели), (4) staging (распаковка пакета).
# НИКОГДА в corpus (репозиторий `…/knowledge`, структура universal/) и в data/qdrant
# (индекс) — документы и индексы у dev и prod СВОИ, между контурами едут только код и
# модели. Runtime-защита путей записи — guard_write_path() в cmd_apply_stage.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TOOL_VERSION="1"
COMPOSE_PROJECT="mcp-knowledge"   # pack обязан фиксированный -p: иначе имя образа
                                  # mcp-server дрейфует по basename каталога (038 §2.4)
UPDATE_MODELS_NEEDED=("mxbai-embed-large" "nomic-embed-text" "qwen2.5:7b")
# qwen2.5:7b — chat-модель compose (OLLAMA_CHAT_MODEL); в 003-списке её не было
# (аудит 038 №13): без неё analyze_content на air-gap без chat-модели.

# 6-й элемент — ollama, composite ref «tag@digest» (arch-2026-10-10-ai-ws-p2-1 R1,
# решение A1; digest = index/RepoDigest, НЕ платформо-специфичный amd64-манифест):
# pack тянет ИМЕННО по digest — гарантия pinned-контента; pack-assert ниже сверяет
# RepoDigests ДО save; save/TSV/manifest — по тег-части ${img%@*} — О-1.
# ВНИМАНИЕ: внутри массива НЕ оставлять комментариев со скобками — ленивый regex
# `_bash_array` в tests/test_airgap_* обрежет массив на первой «)» и 6-й элемент
# станет невидим для стражей.
BASE_IMAGES=(
    "mcp-knowledge-mcp-server:latest"
    "kb-console:prod"
    "mcp-knowledge-kb-converter:latest"
    "qdrant/qdrant:v1.13.4"
    "caddy:2-alpine"
    "ollama/ollama:0.20.2@sha256:0455f166da85b1d07f694c33ba09278ca649603c0611ba8e46272b16eed7fccd"
)
# Образы с тегом :latest, для которых перед load делается ретег :prev (rollback).
ROLLBACK_IMAGES=(
    "mcp-knowledge-mcp-server:latest"
    "kb-console:prod"
    "mcp-knowledge-kb-converter:latest"
)

die() { echo "ОШИБКА: $*" >&2; exit 1; }
# info → stderr: stdout занят данными (verify_pkg возвращает target_commit через
# command substitution — диагностика не должна загрязнять захват, 038 Ф5)
info() { echo "[offline-update] $*" >&2; }

# ─── is_corpus_or_index_path PATH — маркер «внутри корпуса/индекса» (НЕ hardcode) ───
# Корпус знаний — git-репо `…/knowledge` (basename == knowledge) ИЛИ каталог с
# категорийной структурой корпуса (маркер-подкаталог universal/). Индекс — data/qdrant.
# Проверка по маркерам, без абсолютных путей. 0 (true) = путь внутри корпуса/индекса.
is_corpus_or_index_path() {
    local p="$1" abs base
    [ -n "$p" ] || return 1
    abs="$(cd "$p" 2>/dev/null && pwd || true)"
    [ -n "$abs" ] || abs="$p"
    base="$(basename "$abs")"
    [ "$base" = "knowledge" ] && return 0            # корпус-репо …/knowledge
    [ -d "$abs/universal" ] && return 0              # маркер структуры корпуса
    case "$abs" in
        */data/qdrant|*/data/qdrant/*) return 0 ;;   # индекс Qdrant
    esac
    return 1
}

# ─── guard_write_path LABEL PATH — runtime-защита: путь записи НЕ в корпус/индекс ───
guard_write_path() {
    local label="$1" p="$2"
    [ -n "$p" ] || return 0
    # ВАЖНО: явный return 0. Раньше функция заканчивалась на `is_... && die …`, и при
    # «не корпус» последняя команда давала rc=1 → под `set -e` apply-stage падал сразу
    # (ложный STOP БЕЗ вывода) — найдено тестом Н11 2026-10-02.
    if is_corpus_or_index_path "$p"; then
        die "$label '$p' ведёт внутрь корпуса/индекса \
(knowledge / universal/ / data/qdrant) — offline-update переносит ТОЛЬКО код и модели; \
документы и индексы НЕ трогает. STOP."
    fi
    return 0
}

# ─── manifest_field FILE KEY [SUBKEY IDX] — значение из manifest.json ───
manifest_field() {
    local file="$1" key="$2"
    python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
v = m[sys.argv[2]]
if isinstance(v, list):
    for item in v:
        if isinstance(item, dict):
            print("\t".join(str(item.get(k, "")) for k in ("name", "id", "file", "sha256", "bytes", "digest")))
        else:
            print(item)
else:
    print(v)
' "$file" "$key"
}

# ─── image_manifest_digest TAR.GZ → digest OCI-манифеста из index.json ───
# docker save (OCI-layout, docker 25+) кладёт рядом с legacy manifest.json ещё и
# index.json, где manifests[0].digest — digest OCI-манифеста. В containerd image
# store именно он становится IMAGE ID (`docker image inspect -f {{.Id}}`), тогда
# как в overlay2 — config-digest. Пусто, если index.json нет (классический save) —
# не ошибка, пакет просто остаётся сверяемым только по .Id (Н11).
image_manifest_digest() {
    local file="$1"
    python3 - "$file" <<'PY' 2>/dev/null || true
import json, sys, tarfile
try:
    with tarfile.open(sys.argv[1], "r:gz") as t:
        for m in t:
            if m.name == "index.json":
                print(json.load(t.extractfile(m))["manifests"][0]["digest"])
                break
except Exception:
    pass
PY
}

# ─── assert_image_pin IMG — pack-side pin-verify: RepoDigests образа ⊇ digest ───
# Для composite ref (name:tag@sha256:…): локальный образ под тег-частью обязан
# иметь pinned digest в RepoDigests (index). Вызов — pack-этап, ПОСЛЕ pull, ДО
# docker save. Работает ТОЛЬКО на pack-машине (pull-контекст): после docker load
# RepoDigests пуст (§6-проба 1, arch-2026-10-10-ai-ws-p2-1) — на узле пин
# проверяет store-агностичный is_ok(.Id|mdig ↔ manifest.json), НЕ этот ассерт.
assert_image_pin() {
    local img="$1" localref want repodigests
    case "$img" in
        *@sha256:*) ;;
        *) return 0 ;;
    esac
    localref="${img%@*}"     # тег-часть: под ней образ лежит в локальном store
    want="${img##*@}"        # sha256:<hex>
    repodigests="$(docker image inspect -f '{{join .RepoDigests " "}}' "$localref" 2>/dev/null || true)"
    case " $repodigests " in
        *"${want}"*) ;;
        *) die "pack: образ $img не соответствует пину (RepoDigests тег-части '$localref': '${repodigests:-<пусто>}')" ;;
    esac
}

# ─── image_id_acceptable NEW_ID MANIFEST_ID [MANIFEST_DIGEST] ───
# 0 = ID приемлем. Один и тот же образ имеет два «ID» в зависимости от image store:
# config-digest (overlay2: manifest.images[].id) и digest OCI-манифеста (containerd:
# index.json.manifests[0].digest, пишется в manifest как images[].digest). Н11: сверка
# только по config-digest ложно обрывала apply-stage ПОСЛЕ успешного docker load.
# Пустой MANIFEST_DIGEST (пакет старого формата) → поведение прежнее.
image_id_acceptable() {
    local new_id="$1" mid="$2" mdig="${3:-}"
    [ -n "$new_id" ] || return 1
    [ "$new_id" = "$mid" ] && return 0
    [ -n "$mdig" ] && [ "$new_id" = "$mdig" ] && return 0
    return 1
}

# ─── slug IMAGE_NAME → имя файла без / и : ───
slug() { echo "${1//\//_}" | tr ':' '_'; }

# ─── prepare_pkg_dir DIR|TAR.GZ → каталог пакета (расаковка tar во mktemp) ───
PKG_TMP=""
cleanup() { [ -n "$PKG_TMP" ] && [ -d "$PKG_TMP" ] && rm -rf "$PKG_TMP"; return 0; }
trap cleanup EXIT
prepare_pkg_dir() {
    local src="$1"
    if [ -d "$src" ]; then echo "$src"; return 0; fi
    if [ -f "$src" ] && [[ "$src" == *.tar.gz ]]; then
        PKG_TMP="$(mktemp -d /tmp/kilo/offline-update-pkg.XXXXXX)"
        tar -xzf "$src" -C "$PKG_TMP"
        # внутри один каталог mcp-kb-update-*/
        local inner
        inner="$(find "$PKG_TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
        [ -n "$inner" ] || die "в архиве $src нет каталога пакета"
        echo "$inner"
        return 0
    fi
    die "пакет не найден: $src (ожидался каталог или *.tar.gz)"
}

# ─── bundle_head PKG_DIR BRANCH → sha из git bundle list-heads ───
bundle_head() {
    local pkg="$1" branch="$2" sha=""
    while read -r ref_sha ref_name; do
        [ "$ref_name" = "refs/heads/$branch" ] && sha="$ref_sha"
    done < <(git bundle list-heads "$pkg/repo.git" 2>/dev/null)
    echo "$sha"
}

# ─── verify_pkg PKG_DIR [CLONE_DIR] — все проверки ДО любых мутаций ───
# Все git-вызовы: LC_ALL=C + решение ТОЛЬКО по коду возврата (никакого парсинга
# локализуемого stdout). Пути абсолютизируются: git -C меняет CWD, относительный
# путь к bundle резолвился бы от клона (038 Ф5, false-red).
verify_pkg() {
    local pkg="$1" clone="${2:-}" target bundle_sha tmprepo
    pkg="$(cd "$(dirname "$pkg")" && pwd)/$(basename "$pkg")"
    if [ -n "$clone" ]; then clone="$(cd "$clone" && pwd)"; fi
    [ -f "$pkg/manifest.json" ] || die "нет manifest.json в $pkg"
    [ -f "$pkg/CHECKSUMS.sha256" ] || die "нет CHECKSUMS.sha256 в $pkg"
    [ -f "$pkg/repo.git" ] || die "нет repo.git (git bundle) в $pkg"

    info "Проверка sha256 по CHECKSUMS.sha256 …"
    ( cd "$pkg" && sha256sum -c CHECKSUMS.sha256 --quiet ) \
        || die "целостность пакета НАРУШЕНА (sha256). НЕ применять — пересоберите/перекачайте пакет."

    target="$(manifest_field "$pkg/manifest.json" target_commit)"
    [ -n "$target" ] || die "manifest.json без target_commit"

    info "git bundle verify (целостность + prerequisites) …"
    if [ -n "$clone" ]; then
        if ! LC_ALL=C git -C "$clone" bundle verify "$pkg/repo.git" >/dev/null 2>&1; then
            die "git bundle verify FAILED (битый bundle или нет prerequisites в $clone). STOP до мутаций."
        fi
    else
        tmprepo="$(mktemp -d /tmp/kilo/offline-update-verify.XXXXXX)"
        git -C "$tmprepo" init --quiet
        if ! LC_ALL=C git -C "$tmprepo" bundle verify "$pkg/repo.git" >/dev/null 2>&1; then
            die "git bundle verify FAILED. Полный bundle(--all) проходит в пустом репо; \
инкрементальный требует клона — передайте --clone <каталог>. STOP до мутаций."
        fi
    fi

    info "Сверка головы bundle ↔ manifest.target_commit …"
    local branch
    branch="$(manifest_field "$pkg/manifest.json" branch)"
    bundle_sha="$(bundle_head "$pkg" "$branch")"
    [ -n "$bundle_sha" ] || die "в bundle нет refs/heads/$branch"
    [ "$bundle_sha" = "$target" ] \
        || die "голова bundle ($bundle_sha) != manifest.target_commit ($target). STOP до мутаций."
    echo "$target"
}

# ─── ollama_models_dir (паттерн offline-deploy.sh 003) ───
ollama_models_dir() {
    if [ -n "${OLLAMA_MODELS:-}" ]; then echo "$OLLAMA_MODELS"; return 0; fi
    if [ -d "$HOME/.ollama/models" ]; then echo "$HOME/.ollama/models"; return 0; fi
    if [ -d /usr/share/ollama/.ollama/models ]; then echo "/usr/share/ollama/.ollama/models"; return 0; fi
    echo ""
}

# ═══════════════════════════════════════════════════════════════════════════
# pack — сборка пакета на машине с интернетом
# ═══════════════════════════════════════════════════════════════════════════
cmd_pack() {
    local with_models=0 explicit_commit="" out_dir="$GIT_ROOT/artifacts"
    # Легаси-ollama-флаги удалены (arch-2026-10-10-ai-ws-p2-1 R2, Ф-A2):
    # ollama — обязательный 6-й элемент BASE_IMAGES, флаг мёртв.
    while [ $# -gt 0 ]; do
        case "$1" in
            --with-models) with_models=1 ;;
            --commit) shift; explicit_commit="${1:-}" ;;
            --out) shift; out_dir="${1:-}" ;;
            *) die "pack: неизвестный флаг $1" ;;
        esac
        shift
    done

    local -a images=("${BASE_IMAGES[@]}")

    echo "=== [pack] Сборка offline-update пакета (машина с интернетом) ==="

    # dirty-gate: пакуем только чистый клон/явный commit (038 §2.4: образ != коммиту)
    local dirty target head
    dirty="$(git -C "$GIT_ROOT" status --porcelain)"
    head="$(git -C "$GIT_ROOT" rev-parse HEAD)"
    target="$head"
    if [ -n "$dirty" ]; then
        if [ -z "$explicit_commit" ]; then
            die "клон ГРЯЗНЫЙ (git status --porcelain непуст) — образ не будет соответствовать \
manifest.target_commit. Закоммитьте правки ИЛИ передайте --commit <sha> осознанно."
        fi
        echo "WARN: клон грязный, но передан --commit — образы собраны из рабочего дерева \
и могут отличаться от $explicit_commit." >&2
    fi
    if [ -n "$explicit_commit" ]; then
        git -C "$GIT_ROOT" cat-file -e "$explicit_commit^{commit}" 2>/dev/null \
            || die "--commit $explicit_commit не существует в репозитории"
        [ "$explicit_commit" = "$head" ] \
            || die "--commit ($explicit_commit) != HEAD ($head): сделайте checkout — \
образы собираются из рабочего дерева"
        target="$explicit_commit"
    fi
    local branch
    branch="$(git -C "$GIT_ROOT" rev-parse --abbrev-ref HEAD)"

    # build своих образов с фиксированным проектом (P1-2: -p mcp-knowledge)
    info "build mcp-server + kb-console + kb-converter (docker compose -p $COMPOSE_PROJECT) …"
    ( cd "$GIT_ROOT" && docker compose -p "$COMPOSE_PROJECT" -f docker-compose.yml \
        build mcp-server kb-console kb-converter ) || die "docker compose build FAILED"

    # внешние образы: pull при отсутствии; composite ref (tag@digest, R1) — pull
    # ВСЕГДА: резолв именно по digest (гарантия pinned-контента + (пере)назначение
    # тега на него; идемпотентен — «Image is up to date» при совпадении)
    local img
    for img in "${images[@]}"; do
        case "$img" in
            *@sha256:*)
                info "pull $img (pinned by digest) …"
                docker pull "$img" || die "docker pull $img FAILED" ;;
            *)
                if ! docker image inspect "$img" >/dev/null 2>&1; then
                    case "$img" in
                        qdrant/*|caddy/*|ollama/*)
                            info "pull $img (отсутствует локально) …"
                            docker pull "$img" || die "docker pull $img FAILED" ;;
                        *) die "образ $img не найден локально (это свой образ — должен был собраться build)" ;;
                    esac
                fi ;;
        esac
    done

    local iso staging
    iso="$(date -u +%Y%m%dT%H%M%SZ)"
    staging="$out_dir/mcp-kb-update-$iso"
    mkdir -p "$staging/images"

    # per-image docker save | gzip + факты для manifest.
    # О-1 (manifest-name seam): для composite ref (tag@digest) всё локальное — по
    # тег-части localref="${img%@*}": docker save, image inspect и name в TSV/manifest.
    # Composite name в manifest.json убил бы node-side «docker image inspect
    # '{{img.name}}'» (update.yml:418, set -e): после docker load digest-рефы не
    # резолвятся (RepoDigests пуст). Дубль тег-части в BASE_IMAGES (если когда
    # появится) — один tar; pinned-ассерт при этом выполняется для КАЖДОЙ записи
    # с @sha256: (порядок элементов не важен).
    local tsv="$staging/.images.tsv"; : > "$tsv"
    local -a pin_seen=()
    for img in "${images[@]}"; do
        local id file bytes sum mdig localref
        localref="${img%@*}"
        assert_image_pin "$img"
        case " ${pin_seen[*]-} " in
            *" $localref "*) info "  ($img: тег-часть $localref уже сохранена — пропуск дубля)"; continue ;;
        esac
        pin_seen+=("$localref")
        id="$(docker image inspect -f '{{.Id}}' "$localref")"
        file="images/$(slug "$localref").tar.gz"
        info "docker save $localref → $file …"
        docker save "$localref" | gzip -1 > "$staging/$file"
        bytes="$(stat -c%s "$staging/$file")"
        sum="$(sha256sum "$staging/$file" | awk '{print $1}')"
        mdig="$(image_manifest_digest "$staging/$file")"
        [ -n "$mdig" ] || info "  ($localref: index.json без manifests — сверка только по .Id)"
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$localref" "$id" "$file" "$sum" "$bytes" "$mdig" >> "$tsv"
    done

    # git bundle --all (полный: не требует знания prev-HEAD на pack-машине)
    info "git bundle create repo.git --all …"
    git -C "$GIT_ROOT" bundle create "$staging/repo.git" --all

    # модели (опция): выборка по digest, паттерн offline-deploy.sh 003
    local mtsv="$staging/.models.tsv"; : > "$mtsv"
    if [ "$with_models" -eq 1 ]; then
        local msrc mdest
        msrc="$(ollama_models_dir)"
        if [ -z "$msrc" ]; then
            echo "WARN: каталог Ollama-моделей не найден — модели НЕ включены (ollama pull ${UPDATE_MODELS_NEEDED[*]})." >&2
        else
            mdest="$staging/models"
            mkdir -p "$mdest/blobs"
            local m manifest_file mdigest blob_src blob_dst
            for m in "${UPDATE_MODELS_NEEDED[@]}"; do
                manifest_file="$msrc/manifests/registry.ollama.ai/library/$m/latest"
                [ -f "$manifest_file" ] || { echo "WARN: модель '$m' не найдена — пропуск" >&2; continue; }
                info "  модель $m …"
                mkdir -p "$mdest/manifests/registry.ollama.ai/library/$m"
                cp "$manifest_file" "$mdest/manifests/registry.ollama.ai/library/$m/latest"
                mdigest="$(sha256sum "$manifest_file" | awk '{print $1}')"
                while read -r blob_src; do
                    [ -f "$blob_src" ] || { echo "WARN: blob не найден: $blob_src" >&2; continue; }
                    blob_dst="$mdest/blobs/$(basename "$blob_src")"
                    [ -f "$blob_dst" ] || cp "$blob_src" "$blob_dst"
                done < <(python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
for l in m["layers"]:
    print(sys.argv[2] + "/sha256-" + l["digest"].removeprefix("sha256:"))
' "$manifest_file" "$msrc/blobs")
                printf '%s\t%s\n' "$m" "$mdigest" >> "$mtsv"
            done
            info "  модели: $(du -sh "$mdest" | cut -f1)"
        fi
    fi

    # manifest.json (python собирает из TSV — никаких ручных кавычек)
    python3 - "$staging" "$target" "$branch" "$TOOL_VERSION" "$with_models" <<'PY'
import json, sys, datetime
staging, target, branch, tool_version, with_models = sys.argv[1:6]
def tsv(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                rows.append(parts)
    except FileNotFoundError:
        pass
    return rows
images = []
for r in tsv(f"{staging}/.images.tsv"):
    if len(r) < 5:
        continue
    item = {"name": r[0], "id": r[1], "file": r[2], "sha256": r[3], "bytes": int(r[4])}
    if len(r) > 5 and r[5]:
        item["digest"] = r[5]   # digest OCI-манифеста (containerd-store ID, Н11)
    images.append(item)
models = [{"name": r[0], "digest": r[1]} for r in tsv(f"{staging}/.models.tsv")]
manifest = {
    "tool_version": tool_version,
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "target_commit": target,
    "branch": branch,
    "images": images,
    "models": models if with_models == "1" else [],
}
with open(f"{staging}/manifest.json", "w") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)
PY
    rm -f "$tsv" "$mtsv"

    # CHECKSUMS + итоговый tar (tmp ВНЕ staging: иначе find видит файл-вывод — гонка;
    # abs-источник + rel-назначение: внутри subshell CWD = staging)
    local tmpsum
    tmpsum="$(cd "$staging" && pwd).CHECKSUMS.tmp"
    ( cd "$staging" \
      && find . -type f ! -name CHECKSUMS.sha256 -exec sha256sum {} + > "$tmpsum" \
      && mv "$tmpsum" CHECKSUMS.sha256 )
    tar -C "$out_dir" -czf "$out_dir/mcp-kb-update-$iso.tar.gz" "mcp-kb-update-$iso"

    echo ""
    echo "=== Пакет собран ==="
    python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, ensure_ascii=False))' \
        "$staging/manifest.json"
    echo "  TOTAL: $(du -sh "$out_dir/mcp-kb-update-$iso.tar.gz" | cut -f1) → $out_dir/mcp-kb-update-$iso.tar.gz"
    echo ""
    echo "Дальше: verify на носителе → apply-stage/ansible update-local на aikb."
}

# ═══════════════════════════════════════════════════════════════════════════
# inspect [--check] DIR — читаемый отчёт по manifest (опц. с проверками)
# ═══════════════════════════════════════════════════════════════════════════
cmd_inspect() {
    local do_check=0 clone=""
    local args=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --check) do_check=1 ;;
            --clone) shift; clone="${1:-}" ;;
            *) args+=("$1") ;;
        esac
        shift
    done
    [ "${#args[@]}" -ge 1 ] || die "inspect [--check] [--clone DIR] <пакет.tar.gz|каталог>"
    local pkg
    pkg="$(prepare_pkg_dir "${args[0]}")"

    echo "=== [inspect] $pkg ==="
    python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, ensure_ascii=False))' \
        "$pkg/manifest.json" || die "manifest.json не читается"
    echo "--- git bundle list-heads ---"
    git bundle list-heads "$pkg/repo.git"
    echo "--- размеры ---"
    du -sh "$pkg" "$pkg/repo.git" 2>/dev/null || true
    for f in "$pkg"/images/*.tar.gz; do
        [ -f "$f" ] && echo "  $(du -h "$f" | cut -f1)  images/$(basename "$f")"
    done

    if [ "$do_check" -eq 1 ]; then
        local target
        target="$(verify_pkg "$pkg" "$clone")"
        echo "✅ Проверки целостности пройдены (target_commit=$target)."
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# verify DIR [--clone DIR] — полная проверка ДО любых мутаций (STOP при битом)
# ═══════════════════════════════════════════════════════════════════════════
cmd_verify() {
    local clone=""
    local args=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --clone) shift; clone="${1:-}" ;;
            *) args+=("$1") ;;
        esac
        shift
    done
    [ "${#args[@]}" -ge 1 ] || die "verify [--clone DIR] <пакет.tar.gz|каталог>"
    local pkg target
    pkg="$(prepare_pkg_dir "${args[0]}")"
    target="$(verify_pkg "$pkg" "$clone")"
    echo "✅ verify OK: sha256 + bundle + сверка head↔manifest (target_commit=$target)."
}

# ═══════════════════════════════════════════════════════════════════════════
# apply-stage DIR --clone DIR [--stage DIR] [--models-dir DIR]
# Распаковка staging → docker load ТОЛЬКО расходящихся → fetch+merge --ff-only
# (skip при prev == target) → пост-проверка HEAD == target_commit.
# Контейнеры НЕ трогает (up — в ansible/операторе): повторный прогон без
# изменений = 0 мутаций, рестартов нет.
# ═══════════════════════════════════════════════════════════════════════════
cmd_apply_stage() {
    local clone="" stage="" models_dir=""
    local args=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --clone) shift; clone="${1:-}" ;;
            --stage) shift; stage="${1:-}" ;;
            --models-dir) shift; models_dir="${1:-}" ;;
            *) args+=("$1") ;;
        esac
        shift
    done
    [ "${#args[@]}" -ge 1 ] || die "apply-stage <пакет> --clone <каталог-клона> [--stage DIR] [--models-dir DIR]"
    [ -n "$clone" ] || die "apply-stage: обязателен --clone <каталог git-клона>"
    [ -d "$clone/.git" ] || die "--clone ($clone) не git-репозиторий"
    clone="$(cd "$clone" && pwd)"   # abs: далее git -C "$clone" меняет CWD вызова
    guard_write_path "--clone" "$clone"

    local src="${args[0]}" pkg
    if [ -d "$src" ]; then
        pkg="$src"
    else
        [ -n "$stage" ] || stage="$(dirname "$src")/update-staging"
        mkdir -p "$stage"
        stage="$(cd "$stage" && pwd)"   # abs: пути к bundle от клона не сломаются
        guard_write_path "--stage" "$stage"
        pkg="$stage/$(basename "${src%.tar.gz}")"
        if [ ! -f "$pkg/manifest.json" ]; then
            info "Распаковка $src → $stage …"
            tar -xzf "$src" -C "$stage"
        fi
    fi

    # Проверки ДО мутаций (битый пакет = STOP, клон/образы не тронуты)
    local target
    target="$(verify_pkg "$pkg" "$clone")"
    local branch
    branch="$(manifest_field "$pkg/manifest.json" branch)"

    # ── docker load только расходящихся (сравнение .Id) ──
    local loaded=0 skipped=0
    while IFS=$'\t' read -r name id file _sum _bytes mdig; do
        [ -n "$name" ] || continue
        local local_id=""
        local_id="$(docker image inspect -f '{{.Id}}' "$name" 2>/dev/null || true)"
        if image_id_acceptable "$local_id" "$id" "$mdig"; then
            info "SKIP образ $name (ID совпал с manifest)"
            skipped=$((skipped+1))
            continue
        fi
        local rb
        for rb in "${ROLLBACK_IMAGES[@]}"; do
            if [ "$rb" = "$name" ] && [ -n "$local_id" ]; then
                docker tag "$name" "${name%:*}:prev"
                info "retag $name → ${name%:*}:prev (rollback-образ)"
            fi
        done
        info "docker load $name ← $file (локальный: ${local_id:-отсутствует}; пакет: $id${mdig:+, $mdig})"
        docker load -i "$pkg/$file"
        local new_id
        new_id="$(docker image inspect -f '{{.Id}}' "$name" 2>/dev/null || true)"
        image_id_acceptable "$new_id" "$id" "$mdig" \
            || die "после load ID образа $name ($new_id) не совпал ни с manifest.images[].id \
($id), ни с digest OCI-манифеста (${mdig:-нет}) — загруженный образ действительно чужой. STOP."
        loaded=$((loaded+1))
    done < <(manifest_field "$pkg/manifest.json" images)
    info "образы: загружено $loaded, пропущено (идентичны) $skipped"

    # ── git: fetch + merge --ff-only, skip при prev == target ──
    local prev
    prev="$(git -C "$clone" rev-parse HEAD)"
    if [ "$prev" = "$target" ]; then
        info "SKIP git-merge: HEAD уже == target_commit ($target)"
    else
        local cur_branch
        cur_branch="$(git -C "$clone" symbolic-ref --short HEAD 2>/dev/null || true)"
        [ "$cur_branch" = "$branch" ] \
            || die "клон на ветке '${cur_branch:-detached}', пакет для '$branch' — ff-only пишет в текущую ветку. STOP."
        git -C "$clone" fetch "$pkg/repo.git" "$branch"
        git -C "$clone" merge --ff-only FETCH_HEAD
    fi
    # пост-проверка ВСЕГДА (закрывает ложный зелёный host-ahead: merge «Уже
    # актуально» rc=0 при локальных коммитах поверх target)
    local post
    post="$(git -C "$clone" rev-parse HEAD)"
    [ "$post" = "$target" ] \
        || die "POST-CHECK FAILED: HEAD ($post) != manifest.target_commit ($target). \
Клон содержит посторонние коммиты: git -C $clone log $target..HEAD"

    # ── модели (если в пакете): копирование только при расхождении digest ──
    if [ -d "$pkg/models/manifests" ]; then
        [ -n "$models_dir" ] || models_dir="$(ollama_models_dir)"
        guard_write_path "--models-dir" "$models_dir"
        if [ -z "$models_dir" ]; then
            echo "WARN: в пакете есть модели, но каталог Ollama не найден — передайте --models-dir." >&2
        else
            local m mdigest local_manifest
            while IFS=$'\t' read -r m mdigest; do
                [ -n "$m" ] || continue
                local_manifest="$models_dir/manifests/registry.ollama.ai/library/$m/latest"
                if [ -f "$local_manifest" ] \
                   && [ "$(sha256sum "$local_manifest" | awk '{print $1}')" = "$mdigest" ]; then
                    info "SKIP модель $m (digest совпал)"
                    continue
                fi
                info "модель $m: digest разошёлся → копирование манифеста+blobs …"
                mkdir -p "$(dirname "$local_manifest")" "$models_dir/blobs"
                cp "$pkg/models/manifests/registry.ollama.ai/library/$m/latest" "$local_manifest"
                local b
                find "$pkg/models/blobs" -type f -exec cp -n {} "$models_dir/blobs/" \; 2>/dev/null || true
                while read -r b; do
                    [ -f "$models_dir/blobs/$b" ] || cp "$pkg/models/blobs/$b" "$models_dir/blobs/$b"
                done < <(python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
for l in m["layers"]:
    print("sha256-" + l["digest"].removeprefix("sha256:"))
' "$local_manifest")
            done < <(manifest_field "$pkg/manifest.json" models)
        fi
    fi

    echo ""
    echo "✅ apply-stage завершён: HEAD=$target, образы синхронизированы (load=$loaded skip=$skipped)."
    echo "   Дальше (штатно): make -C ansible run-tag PLAYBOOK=playbooks/update.yml ROLE=up HOST=aikb"
}

# ─── usage — справка (одна точка правды для main и -h/--help) ───
usage() {
    echo "Usage: $0 {pack|inspect|verify|apply-stage} …"
    echo ""
    echo "  pack [--with-models] [--commit <sha>] [--out DIR]"
    echo "       собрать полный пакет обновления (интернет-машина)"
    echo "  inspect [--check] [--clone DIR] <пакет.tar.gz|каталог>"
    echo "       отчёт по manifest (--check = sha256+bundle+сверка)"
    echo "  verify [--clone DIR] <пакет.tar.gz|каталог>"
    echo "       полная проверка целостности ДО мутаций (rc≠0 = STOP)"
    echo "  apply-stage <пакет> --clone DIR [--stage DIR] [--models-dir DIR]"
    echo "       идемпотентное применение: staging + load расходящихся + ff-only merge"
    echo ""
    echo "  Флаги pack: --with-models — добавить Ollama-модели; образ ollama входит"
    echo "              всегда (6-й элемент BASE_IMAGES)."
}

# ─── wants_help ARGS… — 0, если среди аргументов есть -h/--help ───
wants_help() {
    local a
    for a in "$@"; do
        case "$a" in -h|--help) return 0 ;; esac
    done
    return 1
}

# ─── Main ─────────────────────────────────────────────────────────────────
# -h/--help — всегда справка и rc=0 (раньше `verify --help` давало
# «пакет не найден: --help»: подкоманды не знали про справку)
if wants_help "$@"; then
    usage
    exit 0
fi
case "${1:-}" in
    pack)        shift; cmd_pack "$@" ;;
    inspect)     shift; cmd_inspect "$@" ;;
    verify)      shift; cmd_verify "$@" ;;
    apply-stage) shift; cmd_apply_stage "$@" ;;
    *)
        usage
        exit 1
        ;;
esac
