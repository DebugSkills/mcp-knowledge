"""Subprocess-тесты airgap-clean-src.sh (038) на настоящем throwaway-репо.

Без Docker: git init в tmp_path, коммит на ветке main. Проверяем, что
`git clone --local` сохраняет имя ветки (manifest.branch != HEAD) и даёт
чистое дерево — в отличие от git worktree (detached HEAD).

Стиль — как test_errors_shell.py: subprocess, без сети/сна.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLEAN_SRC = ROOT / "scripts" / "airgap-clean-src.sh"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60, check=False)


def make_repo(path, dirty=False):
    """Создать git-репо на ветке main с одним коммитом (опц. грязный файл)."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    git(path, "checkout", "-q", "-b", "main")
    f = path / "f.txt"
    f.write_text("committed\n", encoding="utf-8")
    git(path, "add", "f.txt")
    git(path, "commit", "-q", "-m", "init")
    if dirty:
        f.write_text("dirty-uncommitted\n", encoding="utf-8")
    return path


def sh(*args):
    return subprocess.run(["bash", str(CLEAN_SRC), *args],
                          capture_output=True, text=True, timeout=60, check=False)


class TestClone:
    def test_clone_preserves_branch_and_clean(self, tmp_path):
        src = make_repo(tmp_path / "src")
        to = tmp_path / "clone"
        r = sh("--from", str(src), "--to", str(to))
        assert r.returncode == 0, r.stderr
        assert "branch=main" in r.stdout
        assert git(to, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        assert git(to, "status", "--porcelain").stdout.strip() == ""

    def test_dirty_source_clone_still_clean(self, tmp_path):
        src = make_repo(tmp_path / "src", dirty=True)
        to = tmp_path / "clone"
        r = sh("--from", str(src), "--to", str(to))
        assert r.returncode == 0, r.stderr
        # клон чистый, а содержимое = закоммиченному состоянию (не «dirty»)
        assert git(to, "status", "--porcelain").stdout.strip() == ""
        assert (to / "f.txt").read_text() == "committed\n"


class TestCommitMismatch:
    def test_wrong_commit_rc2(self, tmp_path):
        src = make_repo(tmp_path / "src")
        r = sh("--from", str(src), "--to", str(tmp_path / "clone"),
               "--commit", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        assert r.returncode == 2
        assert "--commit" in r.stderr and "!=" in r.stderr


class TestDryRun:
    def test_dry_run_creates_nothing(self, tmp_path):
        src = make_repo(tmp_path / "src")
        to = tmp_path / "clone"
        r = sh("--from", str(src), "--to", str(to), "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "ПЛАН" in r.stdout
        assert not to.exists()


class TestUsage:
    def test_help_rc0(self):
        r = sh("--help")
        assert r.returncode == 0
        for flag in ("--from", "--to", "--commit", "--force"):
            assert flag in r.stdout

    def test_missing_from_rc2(self):
        r = sh("--to", "/tmp/x")
        assert r.returncode == 2
        assert "--from" in r.stderr

    def test_not_a_repo_rc2(self, tmp_path):
        not_repo = tmp_path / "notrepo"
        not_repo.mkdir()
        r = sh("--from", str(not_repo), "--to", str(tmp_path / "clone"))
        assert r.returncode == 2
        assert "не git" in r.stderr.lower()

    def test_to_exists_rc2(self, tmp_path):
        src = make_repo(tmp_path / "src")
        to = tmp_path / "clone"
        to.mkdir()
        r = sh("--from", str(src), "--to", str(to))
        assert r.returncode == 2
        assert "уже существует" in r.stderr


class TestForce:
    def test_force_recreates_existing_to(self, tmp_path):
        src = make_repo(tmp_path / "src")
        to = tmp_path / "clone"
        to.mkdir()
        (to / "stale.txt").write_text("старый мусор\n", encoding="utf-8")
        r = sh("--from", str(src), "--to", str(to), "--force")
        assert r.returncode == 0, r.stderr
        assert (to / ".git").is_dir()          # пересоздан как клон
        assert not (to / "stale.txt").exists()  # старый контент заменён
        assert git(to, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"

    def test_force_root_refused(self, tmp_path):
        src = make_repo(tmp_path / "src")
        r = sh("--from", str(src), "--to", "/", "--force")
        assert r.returncode == 2
        assert (src / ".git").is_dir()  # --from жив

    def test_force_to_equals_from_refused(self, tmp_path):
        src = make_repo(tmp_path / "src")
        r = sh("--from", str(src), "--to", str(src), "--force")
        assert r.returncode == 2
        assert (src / ".git").is_dir()  # --from не удалён

    def test_force_parent_of_from_refused(self, tmp_path):
        src = make_repo(tmp_path / "src")
        r = sh("--from", str(src), "--to", str(tmp_path), "--force")
        assert r.returncode == 2
        assert (src / ".git").is_dir()
