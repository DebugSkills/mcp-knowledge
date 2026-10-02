"""Н11: сверка ID образа не зависит от image store (overlay2 vs containerd).

Регресс-тест инцидента 2026-10-02 (aikb, docker 29.8.1, containerd image store):
`apply-stage` делал `docker load`, после чего сверял
`docker image inspect -f '{{.Id}}'` с `manifest.images[].id` (config-digest).
В containerd store тот же образ имеет IMAGE ID = digest OCI-манифеста из
index.json → ложный STOP «.Id != manifest» ПОСЛЕ успешного load и ДО git-merge
(клон оставался на старом HEAD, хотя образ уже загружен).

Тест эмулирует containerd store фейковым `docker` (PATH-инъекция, без демона):
  * `docker load -i f.tar.gz` записывает «ID» образа = digest OCI-манифеста из
    index.json внутри архива (как это делает containerd) и создаёт маркер;
  * `docker image inspect -f '{{.Id}}' NAME` печатает этот ID (или пусто/rc=1,
    если образа ещё нет).
Пакет и клон — минимальные, но настоящие: git bundle, CHECKSUMS.sha256,
manifest.json.

Позитив: manifest несёт и `id` (config-digest), и `digest` (OCI-манифест) →
apply-stage проходит, HEAD клона доезжает до target_commit.
Негатив (страж «проверка не стала no-op»): действительно чужой ID → STOP, rc≠0,
merge не выполняется.
Обратная совместимость: overlay2 store (ID = config-digest) по-прежнему принят.
"""

import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "offline-update.sh"

IMAGE = "mcp-knowledge-mcp-server:latest"
IMG_REL = "images/mcp-knowledge-mcp-server_latest.tar.gz"
CONFIG_DIGEST = "sha256:" + "31d74e19" + "0" * 56     # overlay2 store: manifest.images[].id
MANIFEST_DIGEST = "sha256:" + "539358cc" + "0" * 56   # containerd store: index.json digest
FOREIGN_DIGEST = "sha256:" + "deadbeef" + "0" * 56

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Фейковый docker: только `image inspect -f '{{.Id}}'` и `load -i` (без демона).
set -u
state="${FAKE_DOCKER_STATE:?}"
key() { printf '%s' "$1" | tr '/:' '__'; }
case "${1:-}" in
  image)
    name="${@: -1}"
    f="$state/$(key "$name").id"
    if [ -f "$f" ]; then cat "$f"; exit 0; fi
    exit 1
    ;;
  load)
    file="${@: -1}"
    name="${FAKE_DOCKER_NAME:-mcp-knowledge-mcp-server:latest}"
    pid="$(python3 - "$file" <<'PY'
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
)"
    [ -n "$pid" ] || pid="${FAKE_DOCKER_FALLBACK_ID:?}"
    printf '%s' "$pid" > "$state/$(key "$name").id"
    echo "Loaded image: $name"
    exit 0
    ;;
esac
exit 0
"""


def _git(repo, *args):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    })
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env, check=True)


def _write_image_tar(path, manifest_digest):
    """Минимальный docker-save-подобный архив: tar.gz с index.json."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        payload = json.dumps({
            "schemaVersion": 2,
            "manifests": [{
                "digest": manifest_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": 1,
            }],
        }).encode()
        info = tarfile.TarInfo("index.json")
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))
    path.write_bytes(gzip.compress(buf.getvalue()))


def build_pkg(tmp_path, *, index_digest=MANIFEST_DIGEST, digest_field=MANIFEST_DIGEST):
    """Собрать пакет (manifest.json + CHECKSUMS.sha256 + repo.git + images/) и клон.

    Клон стоит на коммите A, target_commit в пакете — B (A — предок B).
    """
    pkg = tmp_path / "pkg"
    (pkg / "images").mkdir(parents=True)
    img = pkg / IMG_REL
    _write_image_tar(img, index_digest)

    up = tmp_path / "upstream"
    up.mkdir()
    subprocess.run(["git", "init", "-q", str(up)], check=True)
    _git(up, "symbolic-ref", "HEAD", "refs/heads/main")
    (up / "f.txt").write_text("a\n", encoding="utf-8")
    _git(up, "add", "f.txt")
    _git(up, "commit", "-qm", "A")
    prev = _git(up, "rev-parse", "HEAD").stdout.strip()
    (up / "f.txt").write_text("b\n", encoding="utf-8")
    _git(up, "commit", "-qam", "B")
    target = _git(up, "rev-parse", "HEAD").stdout.strip()
    _git(up, "bundle", "create", str(pkg / "repo.git"), "--all")

    cln = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(cln)], check=True)
    _git(cln, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(cln, "fetch", str(pkg / "repo.git"), "main")
    _git(cln, "checkout", "-q", "-B", "main", prev)

    item = {
        "name": IMAGE,
        "id": CONFIG_DIGEST,
        "file": IMG_REL,
        "sha256": hashlib.sha256(img.read_bytes()).hexdigest(),
        "bytes": img.stat().st_size,
    }
    if digest_field is not None:
        item["digest"] = digest_field
    (pkg / "manifest.json").write_text(json.dumps({
        "tool_version": "1",
        "target_commit": target,
        "branch": "main",
        "images": [item],
        "models": [],
    }, indent=2), encoding="utf-8")

    sums = []
    for rel in ["manifest.json", "repo.git", IMG_REL]:
        h = hashlib.sha256((pkg / rel).read_bytes()).hexdigest()
        sums.append(f"{h}  {rel}")
    (pkg / "CHECKSUMS.sha256").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return pkg, cln, target


def run_apply(tmp_path, pkg, cln):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_NAME": IMAGE,
        "FAKE_DOCKER_FALLBACK_ID": MANIFEST_DIGEST,
    })
    return subprocess.run(
        ["bash", str(SCRIPT), "apply-stage", str(pkg),
         "--clone", str(cln), "--stage", str(tmp_path / "stage")],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )


class TestImageIdStoreAgnostic:
    def test_containerd_store_id_accepted(self, tmp_path):
        """Главный регресс Н11: containerd ID (= digest манифеста) больше не ложный STOP."""
        pkg, cln, target = build_pkg(tmp_path)
        r = run_apply(tmp_path, pkg, cln)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "apply-stage завершён" in r.stdout, r.stdout
        assert "загружено 1" in r.stderr, r.stderr
        assert _git(cln, "rev-parse", "HEAD").stdout.strip() == target

    def test_foreign_id_still_rejected(self, tmp_path):
        """Страж: посторонний ID по-прежнему STOP (проверка не стала no-op)."""
        pkg, cln, target = build_pkg(tmp_path, index_digest=FOREIGN_DIGEST)
        r = run_apply(tmp_path, pkg, cln)
        assert r.returncode != 0, r.stdout
        assert "не совпал" in r.stderr, r.stderr
        assert "STOP" in r.stderr, r.stderr
        assert _git(cln, "rev-parse", "HEAD").stdout.strip() != target

    def test_overlay2_store_id_still_accepted(self, tmp_path):
        """Обратная совместимость: ID = config-digest (overlay2) принимается как раньше."""
        pkg, cln, target = build_pkg(tmp_path, index_digest=CONFIG_DIGEST)
        r = run_apply(tmp_path, pkg, cln)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _git(cln, "rev-parse", "HEAD").stdout.strip() == target

    def test_legacy_package_without_digest_field(self, tmp_path):
        """Пакет старого формата (без images[].digest) + overlay2 ID — не сломан."""
        pkg, cln, target = build_pkg(tmp_path, index_digest=CONFIG_DIGEST,
                                     digest_field=None)
        r = run_apply(tmp_path, pkg, cln)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert _git(cln, "rev-parse", "HEAD").stdout.strip() == target


class TestImageIdAcceptableHelper:
    def test_helper_semantics(self, tmp_path):
        """Юнит-уровень: обе грани (config-digest | OCI-манифест) + пустой digest."""
        harness = (
            "set -u; SCRIPT=$1; "
            "eval \"$(python3 - \"$SCRIPT\" <<'PY'\n"
            "import re, sys\n"
            "src = open(sys.argv[1], encoding='utf-8').read()\n"
            "body = re.search(r'image_id_acceptable\\(\\) \\{.*?\\n\\}', src, re.S).group(0)\n"
            "print(body)\n"
            "PY\n"
            ")\"; "
            "image_id_acceptable a a '' && echo same-ok; "
            "image_id_acceptable b a b && echo digest-ok; "
            "image_id_acceptable b a '' && echo UNEXPECTED || echo empty-digest-rejected; "
            "image_id_acceptable '' a b && echo UNEXPECTED || echo empty-id-rejected"
        )
        r = subprocess.run(["bash", "-c", harness, "bash", str(SCRIPT)],
                           capture_output=True, text=True, timeout=60, check=False)
        assert r.returncode == 0, r.stderr
        assert "same-ok" in r.stdout
        assert "digest-ok" in r.stdout
        assert "empty-digest-rejected" in r.stdout
        assert "empty-id-rejected" in r.stdout
        assert "UNEXPECTED" not in r.stdout


class TestWritePathGuardRegression:
    """Регресс 2026-10-02: guard_write_path возвращал rc=1 при «не корпус» → под
    `set -e` apply-stage падал СРАЗУ и БЕЗ вывода (ложный STOP). Тесты выше
    (containerd/overlay2) его уже покрывают, здесь — явный контракт защиты."""

    def test_corpus_clone_rejected_with_message(self, tmp_path):
        pkg, _cln, _target = build_pkg(tmp_path)
        corpus = tmp_path / "knowledge"          # basename == knowledge → корпус
        (corpus / ".git").mkdir(parents=True)
        r = subprocess.run(
            ["bash", str(SCRIPT), "apply-stage", str(pkg), "--clone", str(corpus)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert r.returncode != 0
        assert "внутрь корпуса" in r.stderr, r.stderr
        assert "STOP" in r.stderr, r.stderr

    def test_index_clone_rejected_with_message(self, tmp_path):
        """Путь внутри data/qdrant (индекс) — тоже STOP, а не тихий обрыв."""
        pkg, _cln, _target = build_pkg(tmp_path)
        idx = tmp_path / "data" / "qdrant" / "clone"
        (idx / ".git").mkdir(parents=True)
        r = subprocess.run(
            ["bash", str(SCRIPT), "apply-stage", str(pkg), "--clone", str(idx)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert r.returncode != 0
        assert "внутрь корпуса" in r.stderr, r.stderr
