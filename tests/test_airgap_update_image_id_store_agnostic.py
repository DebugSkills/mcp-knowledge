"""Н11 в ansible-слое: сверка ID образа в update.yml store-агностична.

У плейбука СВОЯ реализация load (не вызывает offline-update.sh), и в ней было
три точки ложной сверки `.Id == manifest.images[].id`: skip-if-loaded, проверка
после `docker load` и пост-контроль перед `up`. На прод-узле (containerd image
store) `.Id` = digest OCI-манифеста из index.json, а `img.id` = config-digest
(overlay2 машины сборки) → ложный STOP после успешного load.

Здесь: (а) статические проверки структуры таск (есть `is_ok` и `img.digest`,
нет старой строгой сверки), (б) ПОВЕДЕНЧЕСКИЕ тесты: shell-блоки таск
рендерятся jinja2 с манифестом-подмножеством и исполняются bash с фейковым
docker (PATH-инъекция, без демона).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
UPDATE_YML = ROOT / "ansible" / "playbooks" / "update.yml"

IMAGE = "mcp-knowledge-mcp-server:latest"
IMG_REL = "images/mcp-knowledge-mcp-server_latest.tar.gz"
CONFIG_DIGEST = "sha256:" + "31d74e19" + "0" * 56      # overlay2: manifest.images[].id
MANIFEST_DIGEST = "sha256:" + "539358cc" + "0" * 56    # containerd: index.json digest
FOREIGN_DIGEST = "sha256:" + "deadbeef" + "0" * 56

LOAD_TASK_PREFIX = "Load: ретег"
POST_TASK_PREFIX = "Load: пост-проверка"

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
state="${FAKE_DOCKER_STATE:?}"
key() { printf '%s' "$1" | tr '/:' '__'; }
case "${1:-}" in
  image)
    name="${@: -1}"
    f="$state/$(key "$name")"
    if [ -f "$f" ]; then cat "$f"; exit 0; fi
    exit 1
    ;;
  load)
    name="${FAKE_DOCKER_NAME:?}"
    printf '%s' "${FAKE_AFTER_LOAD_ID:?}" > "$state/$(key "$name")"
    echo "Loaded image: $name"
    exit 0
    ;;
esac
exit 0
"""


class _PermissiveLoader(yaml.SafeLoader):
    """SafeLoader, игнорирующий неизвестные теги (!vault и др.)."""


_PermissiveLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _tasks():
    doc = yaml.load(UPDATE_YML.read_text(encoding="utf-8"), Loader=_PermissiveLoader)
    return doc[0]["tasks"]


def _task(prefix):
    for t in _tasks():
        if str(t.get("name", "")).startswith(prefix):
            return t
    raise AssertionError("нет таски с name, начинающимся с " + repr(prefix))


def _shell(prefix):
    body = _task(prefix).get("ansible.builtin.shell")
    assert isinstance(body, str), "ожидался однострочный якорь ansible.builtin.shell"
    return body


def _render(prefix, tmp_path, local_stdout=""):
    jinja2 = pytest.importorskip("jinja2")
    ctx = {
        "update_manifest": {"images": [{
            "name": IMAGE, "id": CONFIG_DIGEST, "file": IMG_REL, "digest": MANIFEST_DIGEST,
        }]},
        "image_ids_local": {"results": [{"stdout": local_stdout}]},
        "update_staging_dir": str(tmp_path),
    }
    return jinja2.Template(_shell(prefix), undefined=jinja2.StrictUndefined).render(**ctx)


def _run_shell(script, tmp_path, *, after_load_id=None, present_id=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if present_id:
        (state / IMAGE.replace("/", "_").replace(":", "_")).write_text(present_id)
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_NAME": IMAGE,
        "FAKE_AFTER_LOAD_ID": after_load_id or MANIFEST_DIGEST,
    })
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=env, timeout=60, check=False)


class TestStaticStructure:
    def test_load_task_mentions_digest_and_helper(self):
        body = _shell(LOAD_TASK_PREFIX)
        assert "img.digest" in body, "load-цикл не учитывает img.digest (Н11)"
        assert "is_ok" in body, "load-цикл должен использовать store-агностичный хелпер is_ok"

    def test_no_strict_only_comparison_left(self):
        """Страховка от регресса: строгая сверка только с img.id недопустима."""
        for prefix in (LOAD_TASK_PREFIX, POST_TASK_PREFIX):
            body = _shell(prefix)
            assert '[ "$new_id" = "{{ img.id }}" ]' not in body, prefix
            assert '[ "$local_id" = "{{ img.id }}" ]' not in body, prefix

    def test_post_check_uses_helper(self):
        body = _shell(POST_TASK_PREFIX)
        assert "is_ok" in body and "img.digest" in body, "пост-контроль не store-агностичен"

    def test_drift_summary_mentions_digest(self):
        body = json.dumps(_task("Local-preflight: сводка дрейфа"), ensure_ascii=False)
        assert "item.item.digest" in body or "item.digest" in body, (
            "preflight-сводка дрейфа должна показывать оба ID"
        )


class TestLoadLoopBehavior:
    def test_containerd_id_after_load_accepted(self, tmp_path):
        """Главный регресс Н11-ansible: ID после load = digest OCI-манифеста → не STOP."""
        r = _run_shell(_render(LOAD_TASK_PREFIX, tmp_path), tmp_path)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "ОШИБКА" not in r.stdout, r.stdout

    def test_foreign_id_after_load_rejected(self, tmp_path):
        script = _render(LOAD_TASK_PREFIX, tmp_path)
        r = _run_shell(script, tmp_path, after_load_id=FOREIGN_DIGEST)
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "ОШИБКА: после load ID != manifest" in r.stdout, r.stdout

    def test_already_loaded_containerd_id_is_skipped(self, tmp_path):
        """Реальный сценарий aikb: образ уже загружен, .Id = digest манифеста → SKIP."""
        script = _render(LOAD_TASK_PREFIX, tmp_path, local_stdout=MANIFEST_DIGEST)
        r = _run_shell(script, tmp_path)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "SKIP" in r.stdout and "ID совпал" in r.stdout, r.stdout
        assert "__ALL_SKIPPED__" in r.stdout, r.stdout

    def test_overlay2_id_is_skipped(self, tmp_path):
        script = _render(LOAD_TASK_PREFIX, tmp_path, local_stdout=CONFIG_DIGEST)
        r = _run_shell(script, tmp_path)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "SKIP" in r.stdout, r.stdout


class TestPostCheckBehavior:
    def test_containerd_id_passes_post_check(self, tmp_path):
        script = _render(POST_TASK_PREFIX, tmp_path)
        r = _run_shell(script, tmp_path, present_id=MANIFEST_DIGEST)
        assert r.returncode == 0, (r.stdout, r.stderr)

    def test_missing_image_fails_post_check(self, tmp_path):
        script = _render(POST_TASK_PREFIX, tmp_path)
        r = _run_shell(script, tmp_path)
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "НЕ СООТВЕТСТВУЕТ МАНИФЕСТУ" in r.stdout, r.stdout

    def test_foreign_id_fails_post_check(self, tmp_path):
        script = _render(POST_TASK_PREFIX, tmp_path)
        r = _run_shell(script, tmp_path, present_id=FOREIGN_DIGEST)
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "НЕ СООТВЕТСТВУЕТ МАНИФЕСТУ" in r.stdout, r.stdout
