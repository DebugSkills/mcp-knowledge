"""Тесты классификатора изменений scripts/classify-changes.sh (make push-fast).

Спека: classify-changes.sh печатает РОВНО один токен (config-only | code | none)
с exit 0 и решает, можно ли пушить без код-гейтов. Fail-safe: неизвестное → code.

Герметичность: каждый тест создаёт собственный git-репозиторий (tmp_path),
git-вызовы и запуск скрипта идут с явным cwd=tmp_path, чтобы не задеть
реальный репозиторий проекта. Путь к скрипту — абсолютный (ROOT/scripts/...).
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLASSIFY = ROOT / "scripts" / "classify-changes.sh"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def init_repo(tmp_path) -> str:
    """Инициализировать git-репозиторий с базовым коммитом; вернуть SHA базы."""
    git(tmp_path, "init", "-b", "main", "-q")
    git(tmp_path, "config", "user.email", "tester@example.com")
    git(tmp_path, "config", "user.name", "tester")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "base.txt")
    git(tmp_path, "commit", "-q", "-m", "base")
    return git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def classify(repo, base):
    """Запустить скрипт в cwd=repo с явным BASE_REF (абсолютный путь к скрипту)."""
    return subprocess.run(["bash", str(CLASSIFY), base],
                          cwd=str(repo), capture_output=True, text=True,
                          check=False)


def commit(repo, paths):
    for p in paths:
        p = repo / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")


# ── (a) только config-пути (ansible/ + docs/*.md) → config-only ──
def test_config_only(tmp_path):
    base = init_repo(tmp_path)
    commit(tmp_path, ["ansible/x.yml", "docs/y.md"])
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "config-only"


# ── (b) код-файл mcp_server/src/x.py → code ──
def test_code_file(tmp_path):
    base = init_repo(tmp_path)
    commit(tmp_path, ["mcp_server/src/x.py"])
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "code"


# ── (c) смешанный (ansible + kb-console) → code ──
def test_mixed(tmp_path):
    base = init_repo(tmp_path)
    commit(tmp_path, ["ansible/x.yml", "kb-console/src/x.py"])
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "code"


# ── (d) BASE_REF не существует → code + непустой stderr (fail-safe) ──
def test_base_ref_missing(tmp_path):
    init_repo(tmp_path)
    r = classify(tmp_path, "origin/no-such-ref")
    assert r.returncode == 0
    assert r.stdout.strip() == "code"
    assert r.stderr.strip() != ""


# ── (e) нет изменений → none ──
def test_no_changes(tmp_path):
    base = init_repo(tmp_path)
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "none"


# ── (f) staged-правка код-файла (без коммита) → code ──
def test_staged_code_file(tmp_path):
    base = init_repo(tmp_path)
    p = tmp_path / "mcp_server" / "src" / "x.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x=1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")  # staged, НЕ коммитим
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "code"


# ── (g) Makefile в изменённых → code ──
def test_makefile(tmp_path):
    base = init_repo(tmp_path)
    commit(tmp_path, ["Makefile"])
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "code"


# ── (h) только untracked-файл → none (untracked не уходит пушем) ──
def test_untracked_only(tmp_path):
    base = init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("x\n", encoding="utf-8")  # НЕ git add
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "none"


# ── (i) README.md + .knowledge/x.md в корне → config-only ──
def test_readme_and_knowledge(tmp_path):
    base = init_repo(tmp_path)
    commit(tmp_path, ["README.md", ".knowledge/x.md"])
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "config-only"


# ── (j) остальные allow-list ветки (plans/, AGENTS.md, .gitignore) → config-only ──
def test_remaining_allow_list(tmp_path):
    base = init_repo(tmp_path)
    commit(tmp_path, ["plans/plan.md", "AGENTS.md", ".gitignore"])
    r = classify(tmp_path, base)
    assert r.returncode == 0
    assert r.stdout.strip() == "config-only"
