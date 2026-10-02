"""`make update-airgap` — air-gap апдейт одной командой (регресс к ручному флоу).

Контекст (2026-10-02, aikb): распаковку пакета, ручной ff-merge код-клона и запуск
playbook ИЗ app-клона оператор делал руками, а флаг пропуска бэкапа надо было
помнить (-e update_skip_backup=true). Цель обязана делать всё сама:

  1. требовать BUNDLE (usage + rc!=0, без побочных эффектов);
  2. брать playbook ИЗ ПАКЕТА (O24-proof: локальный ansible на узле устаревает);
  3. SKIP_BACKUP=1 → -e update_skip_backup=true; CHECK=1 → --check --diff;
  4. работать с tar.gz (распаковка) и с каталогом пакета;
  5. не писать в корпус/Qdrant (workspace только в AIRGAP_WORK, по умолчанию /var/tmp).

Проверки — через `make -n` (печать плана, без исполнения) и один безопасный
запуск-гвард без BUNDLE.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / "ansible"
MAKEFILE = ANSIBLE / "Makefile"


def make(*args, dry_run=True):
    cmd = ["make", "-C", str(ANSIBLE)]
    if dry_run:
        cmd.append("-n")
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)


class TestGuardAndPhony:
    def test_requires_bundle_without_side_effects(self, tmp_path):
        work = tmp_path / "aw"
        r = make("update-airgap", f"AIRGAP_WORK={work}", dry_run=False)
        assert r.returncode != 0, r.stdout
        assert "usage: make update-airgap BUNDLE=" in (r.stdout + r.stderr), r.stdout
        assert not work.exists(), "гвард BUNDLE должен срабатывать ДО любых записей"

    def test_phony_declares_target(self):
        text = MAKEFILE.read_text(encoding="utf-8")
        phony = text.split(".PHONY:")[1].split("\n\n")[0]
        assert "update-airgap" in phony, "update-airgap должен быть в .PHONY"


class TestPlan:
    def test_playbook_taken_from_package(self):
        plan = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz").stdout
        assert "repo.git" in plan and "FETCH_HEAD -- ansible" in plan, plan
        assert "playbooks/update.yml" in plan, plan
        assert "-e update_source=local" in plan, plan

    def test_tarball_is_unpacked(self):
        plan = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz").stdout
        assert 'tar -xzf "/tmp/pkg.tar.gz"' in plan, plan

    def test_directory_package_supported(self):
        plan = make("update-airgap", "BUNDLE=/tmp/update-bundle").stdout
        assert 'PKG="/tmp/update-bundle"' in plan or "PKG=/tmp/update-bundle" in plan, plan

    def test_inventory_from_makefile_dir(self):
        plan = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz").stdout
        assert str(ANSIBLE / "inventory") + "/" in plan, plan

    def test_skip_backup_switch(self):
        on = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz", "SKIP_BACKUP=1").stdout
        off = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz").stdout
        assert "-e update_skip_backup=true" in on, on
        assert "-e update_skip_backup=true" not in off, off

    def test_check_switch(self):
        on = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz", "CHECK=1").stdout
        off = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz").stdout
        assert "--check --diff" in on, on
        assert "--check --diff" not in off, off

    def test_does_not_write_into_corpus(self):
        plan = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz", "SKIP_BACKUP=1").stdout
        assert "data/qdrant" not in plan, "цель не должна трогать индекс Qdrant"
        assert "AIRGAP_WORK ?= /var/tmp/" in MAKEFILE.read_text(encoding="utf-8"), (
            "workspace цели по умолчанию должен быть вне репозитория (/var/tmp)"
        )


class TestExtraVarsPassthrough:
    def test_update_local_accepts_extra_vars(self):
        plan = make("update-local", "BUNDLE=/tmp/pkg.tar.gz",
                    'EXTRA_VARS=-e update_skip_backup=true').stdout
        assert "-e update_skip_backup=true" in plan, plan
        assert "-e update_source=local" in plan, plan

    def test_update_local_check_accepts_extra_vars(self):
        plan = make("update-local-check", "BUNDLE=/tmp/pkg.tar.gz",
                    'EXTRA_VARS=-e update_skip_backup=true').stdout
        assert "--check --diff" in plan and "-e update_skip_backup=true" in plan, plan


ROOT_MAKEFILE = ROOT / "Makefile"
PACK_SUBSET = ROOT / "scripts" / "airgap-pack-subset.sh"


def make_root(*args, dry_run=True):
    cmd = ["make", "-C", str(ROOT)]
    if dry_run:
        cmd.append("-n")
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)


class TestInventoryDirOverride:
    """update-airgap играет от inventory, который задан явно (на узле он приватный,
    лежит вне репозитория — /root/mcp-knowledge/ansible/inventory)."""

    def test_default_is_makefile_dir(self):
        plan = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz").stdout
        assert f'-i "{ANSIBLE}/inventory/"' in plan, plan

    def test_override_is_used(self):
        plan = make("update-airgap", "BUNDLE=/tmp/pkg.tar.gz",
                    "INVENTORY_DIR=/tmp/priv-inv/").stdout
        assert '-i "/tmp/priv-inv/"' in plan, plan
        assert f'-i "{ANSIBLE}/inventory/"' not in plan, plan


class TestRootTargets:
    """Корневой Makefile: весь air-gap-поток — через таргеты (pack → ship → update)."""

    def test_airgap_pack_calls_subset_script(self):
        plan = make_root("airgap-pack", "ARGS=--out /tmp/x").stdout
        assert "scripts/airgap-pack-subset.sh" in plan and "--out /tmp/x" in plan, plan

    def test_airgap_update_requires_bundle(self, tmp_path):
        r = make_root("airgap-update", "BUNDLE=", dry_run=False)
        assert r.returncode != 0
        assert "usage: make airgap-update BUNDLE=" in (r.stdout + r.stderr), r.stdout

    def test_airgap_update_forwards_flags(self):
        plan = make_root("airgap-update", "BUNDLE=/tmp/p.tar.gz", "SKIP_BACKUP=1",
                         "CHECK=1").stdout
        assert "update-airgap" in plan, plan
        assert 'BUNDLE="/tmp/p.tar.gz"' in plan, plan
        assert "SKIP_BACKUP=1" in plan and "CHECK=1" in plan, plan
        assert "INVENTORY_DIR=" in plan, plan

    def test_airgap_inventory_default_is_private_ansible(self):
        text = ROOT_MAKEFILE.read_text(encoding="utf-8")
        assert "airgap-inventory ?=" in text, "нужна переменная выбора inventory узла"
        plan = make_root("airgap-update", "BUNDLE=/tmp/p.tar.gz").stdout
        assert 'INVENTORY_DIR="/root/mcp-knowledge/ansible/inventory/"' in plan, plan

    def test_prod_update_local_passes_extra_vars(self):
        plan = make_root("prod-update-local", "BUNDLE=/tmp/p.tar.gz",
                         'EXTRA_VARS=-e update_skip_backup=true').stdout
        assert "-e update_skip_backup=true" in plan, plan


class TestSubsetPackerScript:
    """Пакет-подмножество — постоянный скрипт (был одноразовый /tmp-скрипт)."""

    def test_help_ok_and_usage(self):
        r = subprocess.run(["bash", str(PACK_SUBSET), "--help"], capture_output=True,
                           text=True, timeout=60, check=False)
        assert r.returncode == 0, r.stderr
        assert "airgap-pack-subset.sh" in r.stdout and "--out" in r.stdout, r.stdout

    def test_unknown_flag_fails(self):
        r = subprocess.run(["bash", str(PACK_SUBSET), "--nope"], capture_output=True,
                           text=True, timeout=60, check=False)
        assert r.returncode != 0
        assert "неизвестный аргумент" in r.stderr, r.stderr

    def test_executable_and_self_verifies(self):
        import os as _os
        assert _os.access(PACK_SUBSET, _os.X_OK), "скрипт должен быть исполняемым"
        body = PACK_SUBSET.read_text(encoding="utf-8")
        assert "verify" in body and "offline-update.sh" in body, (
            "после сборки скрипт обязан самопроверяться штатным верификатором"
        )

    def test_manifest_has_both_image_ids(self):
        """id (config-digest) И digest (OCI-манифест) — иначе Н11 вернётся."""
        body = PACK_SUBSET.read_text(encoding="utf-8")
        assert '"id": img_id' in body, body
        assert 'item["digest"] = digest' in body, body
        assert "index.json" in body, "digest берётся из index.json docker save"
