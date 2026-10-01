"""Subprocess-тесты airgap-bundle-ship.sh (038) — БЕЗ сети/USB.

Проверяем --help/usage/--dry-run/--checklist-only/rc-коды. Для --host
подменяем `ssh` фейком в PATH (имитация недоступного хоста). Стиль — как
test_airgap_bundle_scripts.py (subprocess, tmp_path, без сети/сна).
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIP_SH = ROOT / "scripts" / "airgap-bundle-ship.sh"
MAKEFILE = ROOT / "Makefile"

# фейковый ssh: имитация недоступного хоста (exit 255)
FAKE_SSH_FAIL = """#!/usr/bin/env bash
echo "ssh: connect to host failed" >&2
exit 255
"""


def sh(*args, env_over=None, cwd=None):
    env = dict(os.environ)
    env.update(env_over or {})
    return subprocess.run(["bash", str(SHIP_SH), *args], capture_output=True,
                          text=True, env=env, timeout=60, check=False, cwd=cwd)


def touch(p: Path, size: int = 32) -> Path:
    """Файл-заглушка артефакта (содержимое не важно — preflight читает только stat)."""
    p.write_bytes(b"\x1f\x8b" + b"0" * max(0, size - 2))
    return p


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          check=False, **kw)


def fake_ssh_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    f = bin_dir / "ssh"
    f.write_text(FAKE_SSH_FAIL, encoding="utf-8")
    f.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


class TestHelp:
    def test_help_flags(self):
        r = sh("--help")
        assert r.returncode == 0, r.stderr
        for flag in ("--usb", "--host", "--pipe-via", "--dry-run"):
            assert flag in r.stdout


class TestNoMode:
    def test_no_mode_rc2_usage(self):
        r = sh()
        assert r.returncode == 2
        assert "--usb" in r.stderr and "--host" in r.stderr


class TestUsb:
    def test_nonexistent_mountpoint_rc2(self, tmp_path):
        r = sh("--usb", str(tmp_path / "nope"))
        assert r.returncode == 2
        assert "не существует" in r.stderr

    def test_dry_run_usb_creates_nothing(self, tmp_path):
        r = sh("--dry-run", "--usb", str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert "ПЛАН" in r.stdout and "[1/4]" in r.stdout
        assert list(tmp_path.iterdir()) == []  # ничего не создано


class TestPipeDryRun:
    def test_dry_run_pipe_plan_mentions_split_and_sha(self, tmp_path):
        touch(tmp_path / "mcp-kb-update-20260101T000000Z.tar.gz")
        touch(tmp_path / "mcp-kb-models-20260101T000000Z.tar.gz")
        before = sorted(p.name for p in tmp_path.iterdir())
        r = sh("--dry-run", "--pipe-via", "ch@jump", "--src", str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert "split" in r.stdout and "sha256" in r.stdout
        assert sorted(p.name for p in tmp_path.iterdir()) == before  # dry-run ничего не создаёт


class TestHostPreflight:
    def test_fake_ssh_rc_nonzero_no_crash(self, tmp_path):
        r = sh("--host", "ch@jump", "--src", str(tmp_path),
               env_over=fake_ssh_env(tmp_path))
        assert r.returncode != 0
        combined = r.stdout + r.stderr
        assert "ssh rc=" in combined or "подключиться" in combined


class TestChecklistOnly:
    def test_checklist_mentions_unpack_and_deploy(self):
        r = sh("--checklist-only")
        assert r.returncode == 0, r.stderr
        assert "airgap-bundle-unpack.sh" in r.stdout
        assert "deploy.yml" in r.stdout


class TestBashSyntax:
    def test_bash_n(self):
        r = run(["bash", "-n", str(SHIP_SH)])
        assert r.returncode == 0, r.stderr


class TestMakefile:
    def test_targets_present(self):
        text = MAKEFILE.read_text(encoding="utf-8")
        assert "bundle-ship-usb:" in text
        assert "bundle-ship-net:" in text

    def test_visible_in_make_help(self):
        r = run(["make", "help"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert "bundle-ship-usb" in r.stdout
        assert "bundle-ship-net" in r.stdout


class TestPreflight:
    """Preflight: подмена маски по CWD, легаси-имя, обязательная пара update+models.

    Боевой инцидент 2026-09-30: `for pat in $FILES` разворачивал маску по CWD,
    и легаси-бандл в корне репо подменял `*.tar.gz` → `mcp-kb-airgap-bundle.tar.gz`.
    """

    def test_mask_not_hijacked_by_cwd_glob(self, tmp_path):
        src = tmp_path / "bundles"; src.mkdir()
        touch(src / "mcp-kb-update-20260101T000000Z.tar.gz")
        touch(src / "mcp-kb-models-20260101T000000Z.tar.gz")
        decoy_cwd = tmp_path / "repo"; decoy_cwd.mkdir()
        touch(decoy_cwd / "mcp-kb-airgap-bundle.tar.gz")
        r = sh("--dry-run", "--pipe-via", "ch@jump", "--src", str(src),
               cwd=str(decoy_cwd))
        assert r.returncode == 0, r.stderr
        assert "mcp-kb-update-20260101T000000Z.tar.gz" in r.stdout
        assert "mcp-kb-models-20260101T000000Z.tar.gz" in r.stdout
        assert "mcp-kb-airgap-bundle.tar.gz" not in r.stdout

    def test_legacy_bundle_rejected(self, tmp_path):
        src = tmp_path / "bundles"; src.mkdir()
        touch(src / "mcp-kb-airgap-bundle.tar.gz")
        r = sh("--dry-run", "--pipe-via", "ch@jump", "--src", str(src),
               "--files", "mcp-kb-airgap-bundle.tar.gz")
        assert r.returncode == 1
        assert "НЕ бандл offline-update" in r.stderr

    def test_pair_required_by_default(self, tmp_path):
        src = tmp_path / "bundles"; src.mkdir()
        touch(src / "python-3.11-slim.tar.gz")
        r = sh("--dry-run", "--pipe-via", "ch@jump", "--src", str(src))
        assert r.returncode == 1
        assert "нет пары" in r.stderr

    def test_missing_mask_reports_no_files(self, tmp_path):
        src = tmp_path / "bundles"; src.mkdir()
        r = sh("--dry-run", "--pipe-via", "ch@jump", "--src", str(src),
               "--files", "nope-*.tar.gz")
        assert r.returncode == 1
        assert "нет файлов" in r.stderr

    def test_explicit_files_skips_pair_requirement(self, tmp_path):
        src = tmp_path / "bundles"; src.mkdir()
        touch(src / "mcp-kb-models-20260101T000000Z.tar.gz")
        r = sh("--dry-run", "--pipe-via", "ch@jump", "--src", str(src),
               "--files", "mcp-kb-models-*.tar.gz")
        assert r.returncode == 0, r.stderr

    def test_preflight_shows_sizes_and_eta(self, tmp_path):
        src = tmp_path / "bundles"; src.mkdir()
        touch(src / "mcp-kb-update-20260101T000000Z.tar.gz", 4 * 1024 * 1024)
        touch(src / "mcp-kb-models-20260101T000000Z.tar.gz", 2 * 1024 * 1024)
        r = sh("--dry-run", "--pipe-via", "ch@jump", "--src", str(src))
        assert r.returncode == 0, r.stderr
        assert "суммарно 6 МиБ" in r.stdout
        assert "ETA pipe-режима" in r.stdout
