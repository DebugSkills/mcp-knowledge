#!/usr/bin/env bash
# =============================================================================
# airgap-pack-subset.sh — air-gap пакет-ПОДМНОЖЕСТВО (код + образы, без моделей).
#
# Зачем: полный `offline-update.sh pack` пересобирает свои образы (docker compose
# build) и тянет внешние (qdrant/caddy/ollama) + модели. Для узла, где эти образы
# уже стоят, а между коммитами нет изменений mcp_server/** / kb-console — достаточно
# подмножества: repo.git (полный bundle --all) + docker save уже имеющихся образов.
#
# Чем отличается от pack (осознанно): НЕ собирает образы (берёт локальные) и потому
# пригоден на грязном рабочем дереве: bundle несёт ЗАКОММИЧЕННОЕ состояние, образ —
# то, что уже лежит в docker. Если дерево грязное, печатается предупреждение:
# образ может не соответствовать target_commit.
#
# Manifest пишет и `images[].id` (config-digest, overlay2), и `images[].digest`
# (digest OCI-манифеста из index.json) — чтобы сверка на узле была store-агностичной
# (containerd image store: Н11).
#
# Использование:
#   scripts/airgap-pack-subset.sh [--out DIR] [--image IMG]... [--tag ISO] [--help]
#     --out DIR     куда положить пакет (default: <repo>/artifacts)
#     --image IMG   добавляемый образ (default: mcp-knowledge-mcp-server:latest
#                   + mcp-knowledge-kb-converter:latest — канонизатор Ф1+; прочие
#                   sidecar'ы, напр. kb-console:prod, — через повторяемый --image)
#     --tag ISO     явный ISO-суффикс имени пакета (default: date -u +%Y%m%dT%H%M%SZ)
# Выход: <out>/mcp-kb-update-<ISO>.tar.gz (+ каталог) — самопроверка
# `offline-update.sh verify` в конце; строки ISO/sha256/target/digest в stdout.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERIFY_SH="$SCRIPT_DIR/offline-update.sh"

OUT="$GIT_ROOT/artifacts"
IMAGES=("mcp-knowledge-mcp-server:latest" "mcp-knowledge-kb-converter:latest" \
        "ghcr.io/berriai/litellm:main-stable@sha256:625981c83410a3ea68eb0697590a57ec1d764d634514d54fa5db0591077ee839" \
        "redis:7-alpine@sha256:bb186d083732f669da90be8b0f975a37812b15e913465bb14d845db72a4e3e08")
ISO=""

die() { echo "ОШИБКА: $*" >&2; exit 1; }
info() { echo "[airgap-pack-subset] $*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --out) shift; OUT="${1:-}" ;;
        --image) shift; IMAGES+=("${1:-}") ;;
        --tag) shift; ISO="${1:-}" ;;
        --help|-h)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "неизвестный аргумент: $1 (см. --help)" ;;
    esac
    shift
done
[ -n "$OUT" ] || die "--out пуст"
[ -n "$ISO" ] || ISO="$(date -u +%Y%m%dT%H%M%SZ)"

[ -f "$VERIFY_SH" ] || die "нет $VERIFY_SH (самопроверка невозможна)"

# дедуп --image с сохранением порядка
declare -A _seen=(); UNIQ=()
for img in "${IMAGES[@]}"; do
    [ -n "$img" ] || continue
    [ -n "${_seen[$img]:-}" ] && continue
    _seen[$img]=1; UNIQ+=("$img")
done
IMAGES=("${UNIQ[@]}")

dirty="$(git -C "$GIT_ROOT" status --porcelain)"
[ -n "$dirty" ] && echo "WARN: дерево грязное — bundle = закоммиченное состояние, образ = локальный docker (может не совпадать с target_commit)." >&2

TARGET="$(git -C "$GIT_ROOT" rev-parse HEAD)"
BRANCH="$(git -C "$GIT_ROOT" rev-parse --abbrev-ref HEAD)"
STAGE="$OUT/mcp-kb-update-$ISO"

info "target=$TARGET branch=$BRANCH out=$OUT iso=$ISO"
info "образы: ${IMAGES[*]}"
mkdir -p "$STAGE/images"

# ── образы: docker save | gzip + id (config-digest) + digest (OCI-манифест) ──
for img in "${IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 \
        || die "образ $img отсутствует локально (соберите/загрузите его или уберите --image)"
    slug="${img//\//_}"; slug="${slug//:/_}"
    rel="images/${slug}.tar.gz"
    info "docker save $img → $rel …"
    docker save "$img" | gzip -1 > "$STAGE/$rel"
done

# ── код: полный bundle --all ──
info "git bundle create repo.git --all …"
git -C "$GIT_ROOT" bundle create "$STAGE/repo.git" --all >/dev/null

# ── manifest.json: один python-проход — id (docker inspect), digest (index.json),
#    sha256 и размер каждого архива ──
python3 - "$STAGE" "$TARGET" "$BRANCH" "${IMAGES[@]}" <<'PY'
import hashlib, json, os, subprocess, sys, tarfile, datetime
stage, target, branch = sys.argv[1:4]
images = []
for img in sys.argv[4:]:
    slug = img.replace("/", "_").replace(":", "_")
    rel = f"images/{slug}.tar.gz"
    path = os.path.join(stage, rel)
    with open(path, "rb") as fh:           # sha256 архива
        sha = hashlib.sha256()
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    digest = ""
    try:                                   # digest OCI-манифеста → containerd-store ID (Н11)
        with tarfile.open(path, "r:gz") as t:
            for m in t:
                if m.name == "index.json":
                    digest = json.load(t.extractfile(m))["manifests"][0]["digest"]
                    break
    except Exception:
        digest = ""
    img_id = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", img],
                            capture_output=True, text=True, check=True).stdout.strip()
    item = {"name": img, "id": img_id, "file": rel, "sha256": sha.hexdigest(),
            "bytes": os.path.getsize(path)}
    if digest:
        item["digest"] = digest
    images.append(item)
manifest = {
    "tool_version": "1",
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "target_commit": target,
    "branch": branch,
    "images": images,
    "models": [],
}
with open(os.path.join(stage, "manifest.json"), "w") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)
print("    manifest: images=%d, target=%s" % (len(images), target))
PY

# ── CHECKSUMS.sha256 (все файлы пакета, кроме самого CHECKSUMS) ──
( cd "$STAGE" && find . -type f ! -name CHECKSUMS.sha256 -exec sha256sum {} + > "$STAGE.CHECKSUMS.tmp" \
  && mv "$STAGE.CHECKSUMS.tmp" CHECKSUMS.sha256 )

# ── итоговый tar ──
tar -C "$OUT" -czf "$OUT/mcp-kb-update-$ISO.tar.gz" "mcp-kb-update-$ISO"

# ── самопроверка штатным верификатором ──
info "самопроверка: offline-update.sh verify …"
bash "$VERIFY_SH" verify "$OUT/mcp-kb-update-$ISO.tar.gz" >/dev/null

echo "ISO=$ISO"
echo "package=$OUT/mcp-kb-update-$ISO.tar.gz"
echo "target=$TARGET"
echo "sha256=$(sha256sum "$OUT/mcp-kb-update-$ISO.tar.gz" | awk '{print $1}')"
python3 - "$STAGE/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
for i in m["images"]:
    print("image=%s id=%s digest=%s" % (i["name"], i.get("id", ""), i.get("digest", "")))
PY
