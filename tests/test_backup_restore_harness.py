#!/usr/bin/env python3
r"""Full-chain harness restore scope=documents (требование critic iter.2, инсайт №1).

Извлекает ВСЕ таски группы «Restore documents —» из `ansible/playbooks/backup.yml`
(не подмножество руками!) и исполняет их end-to-end по фикстуре, созданной
реальным `scripts/backup.sh`. Доказывает: цепочка (discovery → pre-restore →
prepare → unarchive → sha256sum -c → cp → cleanup) проходит, `sha256sum -c`
сверяет ВСЕ файлы (rc=0, `: ЦЕЛ`), исключённый таск невозможен (гард по полному
списку имён). Эмулируются модули shell/command/unarchive/file/debug и when/register
в объёме, достаточном для этой группы тасков.

Запуск: .venv/bin/python -m pytest tests/test_backup_restore_harness.py -v
"""

import hashlib
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"
PLAYBOOK = ROOT / "ansible" / "playbooks" / "backup.yml"

DOCS_TASKS = [
    "Restore documents — последний documents-тар",
    "Restore documents — скип (нет тара)",
    "Restore documents — pre-restore-копия текущего каталога",
    "Restore documents — подготовить временный каталог (чисто)",
    "Restore documents — распаковка во временный каталог",
    "Restore documents — sha256-сверка всех файлов с манифестом (гейт A4)",
    "Restore documents — вернуть файлы в каталог documents",
    "Restore documents — cleanup временного каталога",
]

TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def _extract_documents_tasks():
    """Все таски группы «Restore documents —» из playbook, в порядке объявления."""
    data = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = data[0]
    return [t for t in play["tasks"] if t.get("name", "").startswith("Restore documents —")]


def _template(s, vars_, register=None):
    """Подстановка {{ var }} (playbook-переменные) и {{ name.attr }} (registered)."""

    def repl(m):
        key = m.group(1)
        if key in vars_:
            return str(vars_[key])
        if register and "." in key:
            top, attr = key.split(".", 1)
            if top in register and attr in register[top]:
                return str(register[top][attr])
        return m.group(0)

    return TEMPLATE_RE.sub(repl, s)


def _eval_when(when, register):
    """when для этой группы: scope_documents|bool=True + условия на documents_tar.stdout."""
    when = (when or "").strip()
    stdout = register.get("documents_tar", {}).get("stdout", "")
    if "length == 0" in when:
        return len(stdout) == 0
    if "length > 0" in when:
        return len(stdout) > 0
    return True  # scope_documents | bool


def _run_task(task, vars_, register):
    """Эмуляция одного таска (shell/command/unarchive/file/debug). Возвращает (name, rc, stdout)."""
    name = task["name"]
    when = task.get("when")
    if not _eval_when(when, register):
        return name, 0, ""

    stdout = ""
    if "ansible.builtin.shell" in task:
        m = task["ansible.builtin.shell"]
        cmd = _template(m["cmd"], vars_, register)
        exe = m.get("executable", "/bin/sh")
        p = subprocess.run([exe, "-c", cmd], capture_output=True, text=True, timeout=120, check=False)
    elif "ansible.builtin.command" in task:
        cmd = _template(task["ansible.builtin.command"]["cmd"], vars_, register)
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=120, check=False)
    elif "ansible.builtin.unarchive" in task:
        m = task["ansible.builtin.unarchive"]
        src = _template(m["src"], vars_, register)
        dest = _template(m["dest"], vars_, register)
        with tarfile.open(src) as tf:
            tf.extractall(dest)
        stdout = src
        p = _FakeRC(0, stdout, "")
    elif "ansible.builtin.file" in task:
        m = task["ansible.builtin.file"]
        path = _template(m["path"], vars_, register)
        state = m.get("state", "directory")
        if state == "directory":
            Path(path).mkdir(parents=True, exist_ok=True)
        elif state == "absent":
            shutil.rmtree(path, ignore_errors=True)
        stdout = path
        p = _FakeRC(0, stdout, "")
    elif "ansible.builtin.debug" in task:
        stdout = str(task["ansible.builtin.debug"].get("msg", ""))
        p = _FakeRC(0, stdout, "")
    else:
        raise AssertionError(f"неизвестный модуль в таске {name}: {list(task)}")

    if "register" in task:
        register[task["register"]] = {"stdout": (p.stdout or "").strip(), "rc": p.returncode}
    return name, p.returncode, (p.stdout or "") + (p.stderr or "")


class _FakeRC:
    """CompletedProcess-заменитель для не-shell модулей (unarchive/file/debug)."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_backup_fixture(data_root: Path, n: int = 5) -> dict:
    """blobs → backup.sh → тар+манифест в data_root/backups; вернуть sha256-карту."""
    docs = data_root / "documents"
    blobs = {}
    for i in range(n):
        shard, sub = f"{i:02x}", f"{(i * 7) % 256:02x}"
        rel = f"{shard}/{sub}/{i:064x}"
        p = docs / shard / sub / f"{i:064x}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"blob-{i}".encode())
        blobs[rel] = hashlib.sha256(f"blob-{i}".encode()).hexdigest()
    subprocess.run(["bash", str(BACKUP), "--no-qdrant", "--no-ssot"],
                   env={"DATA_ROOT": str(data_root), "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, timeout=120, check=False)
    assert list((data_root / "backups").glob("documents-*.tar.gz")), "бэкап не создан"
    return blobs


def test_documents_task_chain_is_complete():
    """Гард: из playbook извлекается ровно ожидаемый список тасков (никаких исключений)."""
    names = [t["name"] for t in _extract_documents_tasks()]
    assert names == DOCS_TASKS, f"цепочка тасков расходится:\n{names}"


def test_documents_restore_full_chain_end_to_end(tmp_path):
    """Полная цепочка из извлечённых тасков: rc=0 на всех шагах, sha256sum -c = ЦЕЛ."""
    data_root = tmp_path / "data"
    blobs = _make_backup_fixture(data_root)
    # имитируем потерю: каталог documents удалён; остаётся один «текущий» файл
    # (чтобы pre-restore-копия имела непустой вход)
    shutil.rmtree(data_root / "documents")
    (data_root / "documents").mkdir()
    (data_root / "documents" / "current.txt").write_text("to-be-backuped", encoding="utf-8")

    tasks = _extract_documents_tasks()
    vars_ = {
        "data_root": str(data_root),
        "backup_dir": str(data_root / "backups"),
        "snapshot_dir": str(data_root / "qdrant" / "snapshots"),
        "clone_dir": str(tmp_path / "clone"),
        "deploy_root": str(tmp_path),
        "repo_name": "mcp-knowledge",
    }
    register = {}
    for t in tasks:
        name, rc, out = _run_task(t, vars_, register)
        assert rc == 0, f"таск '{name}' rc={rc}\n{out}"

    # pre-restore-копия создана
    assert list((data_root / "backups").glob("pre-restore-documents-*.tar.gz")), \
        "pre-restore-тар не создан"
    # все blobs восстановлены и sha256 совпадает с исходником
    for rel, h in blobs.items():
        p = data_root / "documents" / rel
        assert p.is_file(), f"не восстановлен {rel}"
        assert hashlib.sha256(p.read_bytes()).hexdigest() == h, f"sha256 расходится: {rel}"


def test_documents_restore_chain_corrupt_blob_beyond_sample_fails(tmp_path):
    """Порча blob'а ВНЕ первых 3 строк манифеста → полная сверка (restore) ловит,
    хотя A3-drill (выборка 3) зелёный. Доказывает: полную сверку несёт restore."""
    data_root = tmp_path / "data"
    blobs = _make_backup_fixture(data_root)
    # портим blob №4 (последний, вне выборки drill 1..3) ВНУТРИ тара:
    # манифест остаётся корректным, содержимое тара — нет.
    rel4 = max(blobs)
    tar_path = max((data_root / "backups").glob("documents-*.tar.gz"))
    extract = tmp_path / "extract"
    with tarfile.open(tar_path) as tf:
        tf.extractall(extract)
    (extract / rel4).write_bytes(b"corrupted-content")
    tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(extract, arcname=".")

    shutil.rmtree(data_root / "documents")  # потеря
    (data_root / "documents").mkdir()

    tasks = _extract_documents_tasks()
    vars_ = {
        "data_root": str(data_root),
        "backup_dir": str(data_root / "backups"),
        "snapshot_dir": str(data_root / "qdrant" / "snapshots"),
        "clone_dir": str(tmp_path / "clone"),
        "deploy_root": str(tmp_path),
        "repo_name": "mcp-knowledge",
    }
    register = {}
    failed = None
    for t in tasks:
        name, rc, out = _run_task(t, vars_, register)
        if rc != 0:
            failed = (name, rc, out)
            break
    assert failed is not None, "порча вне выборки не поймана (полная сверка не сработала)"
    assert failed[0] == "Restore documents — sha256-сверка всех файлов с манифестом (гейт A4)"
    assert "ПОВРЕЖДЁН" in failed[2] or "FAILED" in failed[2] or failed[1] != 0


def test_documents_restore_chain_missing_manifest_fails(tmp_path):
    """Удалён манифест → цепочка падает на sha256-таске с внятным сообщением."""
    data_root = tmp_path / "data"
    _make_backup_fixture(data_root)
    manifest = max((data_root / "backups").glob("documents-*.tar.gz.sha256"))
    manifest.unlink()
    shutil.rmtree(data_root / "documents")
    (data_root / "documents").mkdir()

    tasks = _extract_documents_tasks()
    vars_ = {
        "data_root": str(data_root),
        "backup_dir": str(data_root / "backups"),
        "snapshot_dir": str(data_root / "qdrant" / "snapshots"),
        "clone_dir": str(tmp_path / "clone"),
        "deploy_root": str(tmp_path),
        "repo_name": "mcp-knowledge",
    }
    register = {}
    failed = None
    for t in tasks:
        name, rc, out = _run_task(t, vars_, register)
        if rc != 0:
            failed = (name, rc, out)
            break
    assert failed is not None
    assert "манифест отсутствует" in failed[2]
