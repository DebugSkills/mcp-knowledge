"""Хост-CLI правки quotas.yaml (Ф4.5c-1): dry-run/apply, комментарии, идемпотентность.

trace_id: arch-2026-10-05-ai-workspace. Боевой quotas.yaml не мутируется:
все правки — в tmp-копии каталога реестра. Тесты офлайн (без ws-redis).

Подход: прямой импорт модуля скрипта (importlib по пути файла) и вызов
``main(argv)`` — быстрее и детерминированнее subprocess'а на каждый кейс,
плюс один subprocess-смоук на настоящий CLI-контракт (интерпретатор + флаги).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from ai_workspace.registry import Registry
from ai_workspace.registry.quotas import Finding, validate_quotas

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIR = REPO_ROOT / "ai_workspace" / "registry"
SCRIPT = REPO_ROOT / "scripts" / "quotas_set.py"

_SPEC = importlib.util.spec_from_file_location("quotas_set_cli", SCRIPT)
QS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(QS)


def _copy_registry(tmp_path: Path) -> Path:
    """Рабочая копия реестра в tmp (правки CLI бьют только в копию)."""
    work = tmp_path / "registry"
    shutil.copytree(REGISTRY_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    return work


def _quotas(work: Path) -> Path:
    return work / "quotas.yaml"


def _run(argv: list[str], work: Path, tmp_path: Path) -> tuple[int, dict, str]:
    """Вызов main() с копией реестра и tmp-каталогом бэкапов; (rc, json, stdout)."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    full = [
        *argv,
        "--registry-dir",
        str(work),
    ]
    if argv and argv[0] == "set":  # только set принимает --backup-dir
        full += ["--backup-dir", str(tmp_path / "backups")]
    # --json добавляем всегда: возвращаем распарсенный пейлоад + сырой stdout.
    if "--json" not in full:
        full.append("--json")
    with redirect_stdout(buf):
        rc = QS.main(full)
    payload = json.loads(buf.getvalue())
    return rc, payload, buf.getvalue()


def test_show_json_roles_and_budgets(tmp_path):
    work = _copy_registry(tmp_path)
    rc, payload, _ = _run(["show"], work, tmp_path)
    assert rc == 0
    assert payload["ok"] is True
    assert set(payload["participants"]) >= {"admin", "member", "guest"}
    assert payload["budgets"]["ext"]["limit"] == 3000
    assert payload["defaults"]["role"] == "guest"


def test_dry_run_shows_diff_and_keeps_file_bytes(tmp_path):
    work = _copy_registry(tmp_path)
    before = _quotas(work).read_bytes()
    rc, payload, stdout = _run(
        ["set", "--role", "guest", "--priority", "med", "--tokens", "12345"],
        work,
        tmp_path,
    )
    assert rc == 0
    assert payload["ok"] is True and payload["changed"] is True
    assert payload["applied"] is False
    assert "+    priority: med" in payload["diff"]
    assert "+    tokens_per_day: 12345" in payload["diff"]
    assert "-    tokens_per_day: 50000" in payload["diff"]
    assert _quotas(work).read_bytes() == before  # файл не тронут (байт-в-байт)
    assert not (tmp_path / "backups").exists()  # dry-run: бэкапа нет
    json.loads(stdout)  # stdout — валидный JSON


def test_apply_changes_values_and_preserves_comments(tmp_path):
    work = _copy_registry(tmp_path)
    orig = _quotas(work).read_bytes()
    rc, payload, _ = _run(
        [
            "set",
            "--role",
            "guest",
            "--priority",
            "med",
            "--tokens",
            "99999",
            "--conc",
            "2",
            "--grants",
            "heavy,fast,local-only",
            "--budget-ext",
            "4500",
            "--apply",
        ],
        work,
        tmp_path,
    )
    assert rc == 0
    assert payload["applied"] is True and payload["changed"] is True

    # значения изменились и документ валиден (авторитет — validate_quotas)
    doc = yaml.safe_load(_quotas(work).read_text(encoding="utf-8"))
    guest = doc["participants"]["guest"]
    assert guest["priority"] == "med"
    assert guest["tokens_per_day"] == 99999
    assert guest["conc"] == 2
    assert guest["grants"] == ["heavy", "fast", "local-only"]
    assert doc["budgets"]["ext"]["limit"] == 4500
    findings = validate_quotas(doc, Registry(work).get("model_classes"))
    assert findings == []

    # бэкап существует и содержит исходник
    backups = list((tmp_path / "backups").glob("quotas-pre-set-*.yaml"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == orig

    # комментарии на месте (документация D1-D8 / PLACEHOLDER не потеряна)
    text = _quotas(work).read_text(encoding="utf-8")
    assert "PLACEHOLDER" in text
    assert "# D2" in text
    assert "# D4" in text  # бюджеты (заголовок файла упоминает D4)
    assert "grants — ref-целостность" in text  # шапка-пояснение цела

    # порядок ключей сохранён
    assert list(doc) == ["version", "defaults", "participants", "budgets"]
    assert list(doc["participants"]) == ["admin", "member", "guest"]


def test_none_becomes_yaml_null(tmp_path):
    work = _copy_registry(tmp_path)
    rc, _payload, _ = _run(
        ["set", "--role", "member", "--priority", "med", "--tokens", "none", "--conc", "none", "--apply"],
        work,
        tmp_path,
    )
    assert rc == 0
    doc = yaml.safe_load(_quotas(work).read_text(encoding="utf-8"))
    member = doc["participants"]["member"]
    assert member["tokens_per_day"] is None
    assert member["conc"] is None
    assert validate_quotas(doc, Registry(work).get("model_classes")) == []
    # текстовая форма — литерал null, а не пустое значение
    assert "    tokens_per_day: null" in _quotas(work).read_text(encoding="utf-8")


def test_idempotent_second_apply_exit4(tmp_path):
    work = _copy_registry(tmp_path)
    argv = ["set", "--role", "member", "--priority", "high", "--tokens", "250000", "--apply"]
    rc1, p1, _ = _run(argv, work, tmp_path)
    assert rc1 == 0 and p1["applied"] is True
    after_first = _quotas(work).read_bytes()

    rc2, p2, _ = _run(argv, work, tmp_path)
    assert rc2 == 4
    assert p2["ok"] is True and p2["changed"] is False
    assert _quotas(work).read_bytes() == after_first  # файл не переписан
    backups = list((tmp_path / "backups").glob("quotas-pre-set-*.yaml"))
    assert len(backups) == 1  # второго бэкапа не появилось


def test_invalid_priority_exit3_file_intact(tmp_path):
    work = _copy_registry(tmp_path)
    before = _quotas(work).read_bytes()
    rc, payload, _ = _run(["set", "--role", "guest", "--priority", "ultra"], work, tmp_path)
    assert rc == 3
    assert payload["ok"] is False
    assert _quotas(work).read_bytes() == before  # байт-в-байт цел


def test_bogus_grants_exit3_ref_integrity(tmp_path):
    work = _copy_registry(tmp_path)
    before = _quotas(work).read_bytes()
    rc, payload, _ = _run(
        ["set", "--role", "guest", "--priority", "low", "--grants", "bogus"],
        work,
        tmp_path,
    )
    assert rc == 3
    assert any(f["code"] == "Q8" for f in payload["findings"])  # ref-целостность
    assert _quotas(work).read_bytes() == before


def test_unknown_role_exit3(tmp_path):
    work = _copy_registry(tmp_path)
    rc, payload, _ = _run(["set", "--role", "nobody", "--priority", "low"], work, tmp_path)
    assert rc == 3
    assert "не найдена" in payload["error"]


def test_apply_rollback_on_postvalidation_failure(tmp_path, monkeypatch):
    """Пост-валидация после записи отказала → файл восстановлен из бэкапа."""
    work = _copy_registry(tmp_path)
    orig = _quotas(work).read_bytes()
    real_validate = QS.validate_quotas
    calls = {"n": 0}

    def flaky(doc, model_classes):
        calls["n"] += 1
        if calls["n"] >= 2:  # первый вызов (pre) — истина, пост-проверка — отказ
            return [Finding(code="Q99", severity="error", message="forced", path="$")]
        return real_validate(doc, model_classes)

    monkeypatch.setattr(QS, "validate_quotas", flaky)
    rc, payload, _ = _run(
        ["set", "--role", "guest", "--priority", "med", "--apply"], work, tmp_path
    )
    monkeypatch.undo()
    assert rc == 3
    assert "пост-валидация" in payload["error"]
    assert "восстановлено" in payload["error"]
    assert _quotas(work).read_bytes() == orig  # SSOT откачен, не сломан


def test_human_mode_show_and_dry_run(tmp_path, capsys):
    """Без --json — человеко-читаемый вывод того же смысла."""
    work = _copy_registry(tmp_path)
    before = _quotas(work).read_bytes()
    rc = QS.main(
        ["set", "--role", "member", "--priority", "high", "--registry-dir", str(work),
         "--backup-dir", str(tmp_path / "bk")]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN" in out
    assert "+    priority: high" in out
    assert "Registry.reload_if_changed()" in out  # подсказка про mtime
    assert _quotas(work).read_bytes() == before

    rc = QS.main(["show", "--registry-dir", str(work)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "participant-роли:" in out
    assert "member: priority=med" in out
    assert "ext: 3000 RUB/month" in out


def test_subprocess_cli_contract(tmp_path):
    """Смоук настоящего CLI: python scripts/quotas_set.py show --json."""
    work = _copy_registry(tmp_path)
    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "show",
            "--json",
            "--registry-dir",
            str(work),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert payload["ok"] is True
    assert set(payload["participants"]) == {"admin", "member", "guest"}
