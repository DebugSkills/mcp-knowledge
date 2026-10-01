"""Subprocess-тесты обёрток air-gap бандла 038: pack + unpack.

Без Docker (в CI идёт без демона): проверяем --help/--dry-run/usage/rc-коды
и синтаксис. Грязное дерево эмулируется подменой `git` в PATH фейковым
скриптом — тест устойчив к текущему состоянию репозитория.

Стиль — как test_errors_shell.py: subprocess, без сети/сна.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_SH = ROOT / "scripts" / "airgap-bundle-pack.sh"
UNPACK_SH = ROOT / "scripts" / "airgap-bundle-unpack.sh"
CLEAN_SRC_SH = ROOT / "scripts" / "airgap-clean-src.sh"
MAKEFILE = ROOT / "Makefile"

# фейковый git: печатает непустой `status --porcelain` (эмуляция грязного дерева)
FAKE_GIT_DIRTY = """#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "status" ]; then
    echo " M scripts/fake-dirty.sh"
    echo "?? fake-untracked.txt"
    exit 0
  fi
done
exit 0
"""


def sh(script, *args, env_over=None):
    env = dict(os.environ)
    env.update(env_over or {})
    return subprocess.run(["bash", str(script), *args], capture_output=True,
                          text=True, env=env, timeout=60, check=False)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          check=False, **kw)


def fake_git_env(tmp_path):
    """PATH-инъекция фейкового git: непустой `status --porcelain` (грязное дерево)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(FAKE_GIT_DIRTY, encoding="utf-8")
    fake.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


class TestHelp:
    def test_pack_help_flags(self):
        r = sh(PACK_SH, "--help")
        assert r.returncode == 0, r.stderr
        assert "--out" in r.stdout and "--dry-run" in r.stdout

    def test_unpack_help_flags(self):
        r = sh(UNPACK_SH, "--help")
        assert r.returncode == 0, r.stderr
        assert "--bundle" in r.stdout and "--dry-run" in r.stdout


class TestPackDryRun:
    def test_plan_and_no_output_created(self, tmp_path):
        out = tmp_path / "out"
        r = sh(PACK_SH, "--dry-run", "--out", str(out))
        assert r.returncode == 0, r.stderr
        assert "ПЛАН" in r.stdout and "[1/7]" in r.stdout
        assert not out.exists()  # --out не создан


class TestUnpackDryRun:
    def test_plan_and_files_untouched(self, tmp_path):
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        manifest = bundle / "manifest.json"
        manifest.write_text('{"images": []}\n', encoding="utf-8")
        r = sh(UNPACK_SH, "--bundle", str(bundle), "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "ПЛАН" in r.stdout and "[1/6]" in r.stdout
        assert manifest.read_text() == '{"images": []}\n'  # файлы не тронуты
        assert sorted(p.name for p in bundle.iterdir()) == ["manifest.json"]


class TestUnpackRequiresBundle:
    def test_missing_bundle_rc2_usage(self):
        r = sh(UNPACK_SH)
        assert r.returncode == 2
        assert "--bundle" in r.stderr

    def test_unreadable_bundle_rc2(self, tmp_path):
        r = sh(UNPACK_SH, "--bundle", str(tmp_path / "nope.tar.gz"))
        assert r.returncode == 2
        assert "нечитаем" in r.stderr


class TestPackNoCleanSrcDirty:
    def test_dirty_tree_with_no_clean_src_rc2(self, tmp_path):
        r = sh(PACK_SH, "--no-clean-src", "--out", str(tmp_path / "out"),
               env_over=fake_git_env(tmp_path))
        assert r.returncode == 2
        combined = r.stdout + r.stderr
        assert "--no-clean-src" in combined
        assert "грязн" in combined.lower()

    def test_no_worktree_is_deprecated_alias(self, tmp_path):
        """--no-worktree — deprecated-алиас --no-clean-src: тот же rc 2."""
        r = sh(PACK_SH, "--no-worktree", "--out", str(tmp_path / "out"),
               env_over=fake_git_env(tmp_path))
        assert r.returncode == 2
        assert "грязн" in (r.stdout + r.stderr).lower()


class TestPackUsesCleanSrc:
    def test_pack_calls_clean_src_script(self):
        """pack использует airgap-clean-src.sh (клон), а НЕ git worktree."""
        text = PACK_SH.read_text(encoding="utf-8")
        assert "airgap-clean-src.sh" in text
        assert "git worktree" not in text

    def test_pack_calls_clean_src_with_force(self):
        """pack передаёт --force — перезапись собственного временного клона."""
        line = next(ln for ln in PACK_SH.read_text().splitlines()
                    if "GIT_ROOT/scripts/airgap-clean-src.sh" in ln)
        assert "--force" in line

    def test_pack_trap_cleanup_pack_src(self):
        """trap на EXIT INT TERM подчищает .pack-src при выходе/прерывании."""
        text = PACK_SH.read_text(encoding="utf-8")
        assert "trap cleanup EXIT INT TERM" in text
        assert ".pack-src" in text


class TestBashSyntax:
    def test_both_scripts_bash_n(self):
        for s in (PACK_SH, UNPACK_SH, CLEAN_SRC_SH):
            r = run(["bash", "-n", str(s)])
            assert r.returncode == 0, r.stderr


class TestMakefile:
    def test_targets_present(self):
        text = MAKEFILE.read_text(encoding="utf-8")
        assert "bundle-pack:" in text
        assert "bundle-unpack:" in text

    def test_visible_in_make_help(self):
        r = run(["make", "help"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert "bundle-pack" in r.stdout
        assert "bundle-unpack" in r.stdout
