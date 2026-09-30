"""Регрессионный страж: group_vars НЕ должен теряться из-за «тени» каталога.

Инцидент 2026-09-30 (Э5 деплоя на aikb): в `ansible/inventory/group_vars/`
одновременно существовали файл `all.yml` (50 переменных) и каталог `all/`
(с `vault.yml`). Ansible в такой ситуации МОЛЧА загружает только каталог и
полностью игнорирует одноимённый файл — переменные из `all.yml` становились
undefined (`mcp_kb_host_prepare__docker_packages`, `mcp_kb_repos__*`,
`mcp_kb_host_prepare__nvidia_gpg_url`, `errors_guard_*` и др.).

Дефект был невидим, потому что `host_vars/aikb.yml` дублирует часть значений
«для самодостаточности» — ровно поэтому Э5 дошёл до установки пакетов docker
и упал только там.

Стражи:
1. Структурный (детерминированный, без ansible): для любой группы каталог
   `group_vars/<g>/` и файл `group_vars/<g>.yml|.yaml` не должны сосуществовать.
2. Функциональный (best-effort): `ansible-inventory --host aikb` должен
   резолвить переменную, которая есть ТОЛЬКО в групповых vars.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GROUP_VARS = ROOT / "ansible" / "inventory" / "group_vars"
INVENTORY = ROOT / "ansible" / "inventory" / "hosts.yml"

# Переменные, которые определены ТОЛЬКО в групповых vars (в host_vars их нет):
# если «тень» вернётся, они пропадут из резолва.
GROUP_ONLY_VARS = (
    "mcp_kb_host_prepare__docker_packages",
    "mcp_kb_host_prepare__nvidia_gpg_url",
    "mcp_kb_repos__version",
)


def test_no_dir_file_shadowing_in_group_vars():
    """Каталог group_vars/<g>/ и файл group_vars/<g>.yml не должны сосуществовать."""
    assert GROUP_VARS.is_dir(), f"нет каталога {GROUP_VARS}"

    groups = {p.name for p in GROUP_VARS.iterdir() if p.is_dir()}
    groups |= {
        p.stem for p in GROUP_VARS.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    }

    offenders = []
    for g in sorted(groups):
        dir_exists = (GROUP_VARS / g).is_dir()
        file_exists = any(
            (GROUP_VARS / f"{g}{ext}").is_file() for ext in (".yml", ".yaml", ".json")
        )
        if dir_exists and file_exists:
            offenders.append(g)

    assert not offenders, (
        "Ansible МОЛЧА игнорирует файл group_vars/<g>.yml, если рядом есть каталог "
        f"group_vars/<g>/ — переменные пропадают. Группы с тенью: {offenders}. "
        "Переменные держите в group_vars/<g>/main.yml."
    )


def test_group_only_vars_are_resolved():
    """ansible-inventory должен резолвить переменные, которых нет в host_vars."""
    if shutil.which("ansible-inventory") is None:
        pytest.skip("ansible-inventory не установлен в PATH")
    # С зашифрованным vault.yml инвентарь без пароля не читается — тогда проверка
    # не имеет смысла (на этой машине так: vault живёт только на целевом хосте).
    if (GROUP_VARS / "all" / "vault.yml").exists():
        pytest.skip("есть group_vars/all/vault.yml — нужен vault-пароль, тест пропущен")

    proc = subprocess.run(
        ["ansible-inventory", "-i", str(INVENTORY), "--host", "aikb"],
        capture_output=True,
        text=True,
        cwd=ROOT / "ansible",
        timeout=120,
        check=False,  # returncode обрабатываем сами (skip при сбое инвентаря)
    )
    if proc.returncode != 0:
        pytest.skip(f"ansible-inventory вернул {proc.returncode}: {proc.stderr.strip()[:200]}")

    hostvars = json.loads(proc.stdout)
    missing = [v for v in GROUP_ONLY_VARS if v not in hostvars]
    assert not missing, (
        f"переменные групповых vars не резолвятся: {missing}. "
        "Похоже, group_vars снова затенён файлом/каталогом (см. тест выше) "
        "или переменные удалены из group_vars/all/main.yml."
    )
