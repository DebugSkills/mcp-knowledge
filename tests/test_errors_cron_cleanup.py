"""018 (code-2026-09-25-018, §7.8): one-shot миграция legacy cron-ключей.

T8: селекция / dry-run / confirm / backup / идемпотентность / валидность
JSON / байт-идентичность нетронутых / P0-P1-cron не тронуты / orphan
alert_state (P3-N2) / «план пуст → backup не пишется» (P3-N1).
Все числа динамические (считаются по фикстуре, без хардкода 61/62);
синтетический sink во tmp_path — живой прод-sink не трогается.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cli():
    return _load("errors_cleanup_cron_legacy", "scripts/errors_cleanup_cron_legacy.py")


@pytest.fixture(scope="module")
def ec():
    return _load("errors_collect", "scripts/errors_collect.py")


def _agg(prio, status, msg, source="cron_log"):
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "priority": prio, "class": "T", "first_seen": fresh, "last_seen": fresh,
        "count_total": 10, "daily": {}, "actors": ["cron:collector"],
        "sources": [source],
        "last_example": {"ts": fresh, "message": msg},
        "status": status, "fixed_at": None,
    }


def _sig(ec, line):
    return ec.make_signature("cron_log", "CRON", None, line)


@pytest.fixture()
def sink(tmp_path, ec):
    """Синтетический sink: 4 ключа.

    * legacy exit=0 P2/active (полная строка с dur= — ПОД удаление);
    * канонический heartbeat P3/active (БЕЗ dur= — не выбирается);
    * [CRON] exit=1 P0/active (legacy-форма c dur=, но P0 — НЕ трогаем НИКОГДА);
    * чужая docker P2/active (не-cron — не выбирается).
    """
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    legacy_sig = _sig(ec, f"[CRON] job=collector exit=0 dur=0s ts={fresh}")
    canon_sig = _sig(ec, "[CRON] job=collector exit=0")
    p0_sig = _sig(ec, f"[CRON] job=collector exit=1 dur=2s ts={fresh}")
    aggs = {
        legacy_sig: _agg("P2", "active", "[CRON] job=collector exit=0 dur=0s …"),
        canon_sig: _agg("P3", "active", "[CRON] job=collector exit=0"),
        p0_sig: _agg("P0", "active", "[CRON] job=collector exit=1 dur=2s …"),
        "docker_logs|-|foreign p2": _agg("P2", "active", "foreign", source="docker_logs"),
    }
    # критерий отбора виден в самой фикстуре (динамически, §7.8-1):
    assert " dur=" in legacy_sig and " dur=" not in canon_sig
    ec.atomic_write_json(tmp_path / "aggregates" / "signatures.json", aggs)
    return {"path": tmp_path, "legacy": legacy_sig, "canon": canon_sig,
            "p0": p0_sig, "aggs": aggs}


class TestCronLegacyCleanupT8:
    def test_dry_run_keeps_file_and_selects_one_legacy(self, cli, sink, capsys):
        """Dry-run: файл байт-не-изменён; выбран ровно 1 legacy (P2/active)."""
        before = (sink["path"] / "aggregates" / "signatures.json").read_bytes()
        rc = cli.main(["--sink", str(sink["path"]),
                       "--trash-dir", str(sink["path"] / "trash")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "план удаления (P2+active): 1" in out
        assert sink["legacy"][:60] in out  # план показывает сам ключ
        # файл не изменён; backup при dry-run не пишется
        assert (sink["path"] / "aggregates" / "signatures.json").read_bytes() == before
        assert not (sink["path"] / "trash").exists() or \
            not list((sink["path"] / "trash").glob("signatures-cron-cleanup-*.json"))

    def test_confirm_deletes_only_legacy_validates_and_backups(self, cli, sink, capsys):
        """--confirm: удалён ТОЛЬКО legacy; нетронутые dict-идентичны; JSON
        валиден; backup создан и байт-равен исходному (P3-N1)."""
        agg_path = sink["path"] / "aggregates" / "signatures.json"
        trash = sink["path"] / "trash"
        before_bytes = agg_path.read_bytes()
        before = json.loads(before_bytes)
        rc = cli.main(["--sink", str(sink["path"]), "--confirm",
                       "--trash-dir", str(trash)])
        assert rc == 0
        assert "CLEANED: 1 " in capsys.readouterr().out
        after = json.loads(agg_path.read_text())  # JSON валиден после записи
        # keys_before − deleted == keys_after; удалён ровно выбранный legacy
        assert set(after) == set(before) - {sink["legacy"]}
        # P0/P1-cron и чужие ключи не тронуты (dict-идентичность)
        for s in after:
            assert after[s] == before[s]
        assert after[sink["p0"]]["priority"] == "P0"
        assert sink["canon"] in after
        # backup: создан, байт-равен исходному (до правки)
        backups = list(trash.glob("signatures-cron-cleanup-*.json"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == before_bytes

    def test_confirm_idempotent_and_empty_plan_writes_no_backup(self, cli, sink, capsys):
        """Повторный запуск: план 0 (идемпотентность), backup НЕ пишется
        (ветка «план пуст», P3-N1)."""
        trash = sink["path"] / "trash"
        cli.main(["--sink", str(sink["path"]), "--confirm", "--trash-dir", str(trash)])
        capsys.readouterr()  # вывод первого прогона не мешает ассертам повтора
        first = (sink["path"] / "aggregates" / "signatures.json").read_bytes()
        rc = cli.main(["--sink", str(sink["path"]), "--confirm", "--trash-dir", str(trash)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "план удаления (P2+active): 0" in out
        assert "CLEANED" not in out  # ничего не удалялось — changed_when не сработает
        assert (sink["path"] / "aggregates" / "signatures.json").read_bytes() == first
        assert len(list(trash.glob("signatures-cron-cleanup-*.json"))) == 1

    def test_selection_never_touches_p0(self, cli, sink, capsys):
        """Мутация-страховка селекции: P0-cron (legacy-форма c dur=) НЕ
        выбирается даже в плане — приоритетный фильтр обязателен."""
        rc = cli.main(["--sink", str(sink["path"]),
                       "--trash-dir", str(sink["path"] / "trash")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "план удаления (P2+active): 1" in out
        assert sink["p0"][:60] not in out.split("план удаления")[1]

    def test_clean_alert_state_removes_orphans_with_backup(self, cli, sink, capsys):
        """P3-N2: --clean-alert-state (только с --confirm) вычищает orphan
        CRON-записи alert_state.json, с бэкапом; чужие записи остаются."""
        alert_path = sink["path"] / "alert_state.json"
        alert_before = {sink["legacy"]: {"status": "known"},
                        "docker_logs|-|other": {"status": "new"}}
        alert_path.write_text(json.dumps(alert_before), encoding="utf-8")
        trash = sink["path"] / "trash"
        rc = cli.main(["--sink", str(sink["path"]), "--confirm",
                       "--trash-dir", str(trash), "--clean-alert-state"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "orphan" in out
        alert_after = json.loads(alert_path.read_text())
        assert sink["legacy"] not in alert_after
        assert "docker_logs|-|other" in alert_after
        abackups = list(trash.glob("alert-state-cron-cleanup-*.json"))
        assert len(abackups) == 1
        assert abackups[0].read_bytes() == \
            json.dumps(alert_before).encode()  # бэкап до правки


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
