"""Guard-тест Н10 (code-2026-10-05-deploy-host-mechanism): rc-семантика backup.sh.

Дефект Н10: create_qdrant_snapshot возвращала ИНВЕРТИРОВАННЫЙ rc
(`local c ok=1` на успехе + `return $ok` => успех -> rc=1, провал создания
снапшота -> rc=0), а вызов `[ "$NO_QDRANT" = false ] && create_qdrant_snapshot`
под `set -e` убивал скрипт молча сразу после «OK: Snapshot validated» —
до SSOT/documents/console. Итог: дефолтный air-gap апдейт
(ansible/playbooks/update.yml, preflight-бэкап; make airgap-update /
update-local без SKIP_BACKUP=1) падал ДО мутаций при УСПЕШНОМ бэкапе,
а провал создания снапшота маскировался как успех (окончательный exit 0).

Контракт (Н10): create_qdrant_snapshot -> rc=0 при полном успехе, rc!=0 при
провале; вызов НЕ прерывает скрипт (аккумуляция QDRANT_RC, как CONSOLE_RC);
итоговый exit=1 при QDRANT_RC=1 или CONSOLE_RC=1 — ПОСЛЕ полного прогона,
с перечнем провалившихся шагов в строке WITH ERRORS.

Статические проверки — stdlib-only. Поведенческие — через ГЕРМЕТИЧНУЮ копию
скрипта во временном дереве (scripts/ + data/): запуск реального пути
scripts/backup.sh тарил бы настоящий .env (secrets-шаг читает $PROJECT_DIR/.env)
— это запрещено (P0-гвард задачи); копия выполняет тот же код без побочек.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"

# Легаси-тело create_qdrant_snapshot (до фикса Н10) — red-control детектора:
# детектор инверсии ОБЯЗАН находить этот паттерн, иначе он ничего не охраняет.
LEGACY_BODY = """
    local collections
    collections=$(curl -s "${QDDRANT_URL}/collections")
    if [ -z "$collections" ]; then
        return 1
    fi
    local c ok=1
    for c in $collections; do
        validate_snapshot "${c}" "${actual}" || ok=0
    done
    return $ok
"""


def _text() -> str:
    return BACKUP.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """Тело bash-функции: от `name() {` до первой строки `}` в колонке 0."""
    lines = source.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{name}() {{"):
            start = i + 1
            break
    assert start is not None, f"функция {name}() не найдена в backup.sh"
    body: list[str] = []
    for line in lines[start:]:
        if line == "}":
            return "\n".join(body)
        body.append(line)
    raise AssertionError(f"не найден закрывающий '}}' функции {name}()")


def _strip_comments(body: str) -> str:
    """Убрать строки-комментарии: детектор анализирует КОД, а не документацию
    (комментарий Fix-а упоминает легаси-паттерн `ok=1`/`return $ok` как историю)."""
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def _inversion_violations(body: str) -> list:
    """Детектор Н10: список нарушений прямой rc-семантики в теле функции."""
    violations = []
    body = _strip_comments(body)
    if re.search(r"\bok=1\b", body):
        violations.append("инвертированный аккумулятор `ok=1` (успех закодирован как 1)")
    if re.search(r"return \$ok\b", body):
        violations.append("`return $ok` — инвертированный возврат (успех -> rc=1)")
    if not re.search(r"\brc=0\b", body):
        violations.append("нет аккумулятора `rc=0` (прямой: 0 = успех)")
    if not re.search(r'return "\$rc"', body):
        violations.append('нет `return "$rc"` (прямой возврат накопленного rc)')
    return violations


# --- (а) функция: прямая rc-семантика ---


class TestCreateQdrantSnapshotRc:
    def test_no_inverted_rc_in_function(self):
        body = _function_body(_text(), "create_qdrant_snapshot")
        violations = _inversion_violations(body)
        assert not violations, (
            "create_qdrant_snapshot нарушает контракт Н10 (rc=0 при успехе): "
            + "; ".join(violations)
        )

    def test_failure_paths_set_rc_nonzero(self):
        """Провал создания снапшота обязан ставить rc=1 (раньше ставил ok=0 -> rc=0)."""
        body = _function_body(_text(), "create_qdrant_snapshot")
        assert re.search(r"validate_snapshot [^\n]*\|\| rc=1", body), (
            "провал validate_snapshot должен аккумулироваться как rc=1 "
            "(раньше `|| ok=0` маскировал провал успехом)"
        )
        assert re.search(r"rc=1", body.split("else")[-1]), (
            "ветка провала создания снапшота (пустое имя) должна ставить rc=1"
        )

    def test_early_diagnostic_server_not_running_kept(self):
        """Ранняя диагностика «сервер не запущен / нет коллекций» сохранена (return 1)."""
        body = _function_body(_text(), "create_qdrant_snapshot")
        assert "server not running" in body, "потеряно WARN-сообщение о незапущенном сервере"
        assert re.search(r"return 1\b", body), (
            "ранний отказ (пустой список коллекций) должен возвращать rc!=0"
        )

    def test_red_control_detector_catches_legacy(self):
        """Red-control: детектор обязан ловить легаси-тело Н10 (иначе он пустышка)."""
        violations = _inversion_violations(LEGACY_BODY)
        assert any("ok=1" in v for v in violations), "детектор не ловит `ok=1`"
        assert any("return $ok" in v for v in violations), "детектор не ловит `return $ok`"
        assert not re.search(r"\brc=0\b", LEGACY_BODY)

    def test_comments_do_not_trip_detector(self):
        """Комментарий, документирующий легаси-паттерн, не должен триггерить детектор."""
        commented = "# раньше было ok=1 и return $ok (Н10)\nlocal c rc=0\nreturn \"$rc\"\n"
        assert _inversion_violations(commented) == []


# --- (б) вызов: аккумуляция вместо тихой смерти под set -e ---


class TestCallSiteAccumulates:
    def test_call_accumulates_qdrant_rc(self):
        text = _text()
        assert re.search(r"create_qdrant_snapshot \|\| QDRANT_RC=1", text), (
            "вызов должен аккумулировать провал (`create_qdrant_snapshot || QDRANT_RC=1`), "
            "как console-ветка (`backup_console_state || CONSOLE_RC=1`)"
        )
        assert re.search(r"^QDRANT_RC=0$", text, re.MULTILINE), (
            "нет инициализации `QDRANT_RC=0` перед вызовом"
        )

    def test_no_bare_call_under_set_e(self):
        """Голый AND-список `[ ... ] && create_qdrant_snapshot` под set -e — запрещён.

        Именно он валил скрипт молча: функция — последняя команда списка,
        её не-0 статус триггерит set -e сразу после успешной валидации.
        """
        bare = re.findall(r"&&\s*create_qdrant_snapshot\s*$", _text(), re.MULTILINE)
        assert not bare, (
            "найден голый вызов create_qdrant_snapshot в конце AND-списка "
            "(тихая смерть скрипта под set -e — Н10): " + repr(bare)
        )

    def test_console_pattern_still_accumulates(self):
        """Правильный паттерн console-ветки (эталон Н10) не сломан."""
        assert re.search(r"backup_console_state \|\| CONSOLE_RC=1", _text())


# --- (в) итоговый exit ---


class TestFinalExit:
    def test_exit_accounts_for_both_accumulators(self):
        text = _text()
        assert re.search(
            r'if \[ "\$QDRANT_RC" -ne 0 \] \|\| \[ "\$CONSOLE_RC" -ne 0 \]', text
        ), "итоговый if обязан учитывать QDRANT_RC и CONSOLE_RC одновременно"

    def test_with_errors_message_lists_failed_steps(self):
        text = _text()
        assert '"qdrant-snapshot"' in text, (
            "финальное сообщение должно поимённо называть проваленный qdrant-шаг"
        )
        assert re.search(r"WITH ERRORS \(\$\{failed_steps\}", text), (
            "финальное сообщение WITH ERRORS должно перечислять провалившиеся шаги"
        )
        assert re.search(r'echo "=== Backup completed: \$\{TIMESTAMP\} ==="', text), (
            "успешный путь обязан завершаться строкой «Backup completed»"
        )


# --- шапка: контракт exit-кодов ---


class TestHeaderContract:
    def test_header_documents_exit_codes(self):
        header = "\n".join(_text().splitlines()[:15])
        assert "Н10" in header, "в шапке нет ссылки на Н10"
        assert "все шаги" in header, "шапка должна фиксировать: exit 0 = все шаги ок"
        assert "довед" in header, (
            "шапка должна фиксировать: exit 1 = прогон доведён до конца, шаги провалились"
        )


# --- поведенческое: герметичная копия, без реального .env / data ---


def _run_hermetic(args, qdrant_url="http://127.0.0.1:1"):
    """Копия backup.sh во временное дерево -> PROJECT_DIR=tmp -> .env/secrets изолированы.

    DATA_ROOT по дефолту = $PROJECT_DIR/data = tmp/data — реальный data/ не трогаем.
    """
    tmp = tempfile.mkdtemp(prefix="kb-h10-rc-")
    try:
        (Path(tmp) / "scripts").mkdir()
        dst = Path(tmp) / "scripts" / "backup.sh"
        shutil.copy2(BACKUP, dst)
        env = dict(os.environ)
        env["QDRANT_URL"] = qdrant_url
        proc = subprocess.run(
            ["bash", str(dst)] + list(args),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        return proc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestBehavioralHermetic:
    def test_qdrant_down_run_completes_with_rc1(self):
        """Qdrant недоступен: прогон доведён до конца, последующие шаги сделаны, rc=1.

        До фикса Н10 скрипт умирал молча НА qdrant-шаге: console/secrets/errors/
        rotate не выполнялись, финального сообщения не было.
        """
        proc = _run_hermetic(["--no-ssot"])
        out = proc.stdout + proc.stderr
        assert proc.returncode == 1, (
            f"ожидался итоговый rc=1 (qdrant недоступен), получен {proc.returncode}; "
            f"вывод:\n{out}"
        )
        assert "WARN: Qdrant snapshot failed" in out, "не отработала ветка «сервер не запущен»"
        assert "Backing up console state" in out, (
            "прогон оборван до console-шага — тихая смерть Н10 не устранена"
        )
        assert "Backing up secrets" in out, "прогон оборван до secrets-шага"
        assert "Rotating backups" in out, "прогон оборван до rotate-шага"
        assert "WITH ERRORS" in out and "qdrant-snapshot" in out, (
            "нет финального сообщения WITH ERRORS с перечнем провалившихся шагов"
        )
        assert "=== Backup completed:" not in out, (
            "ложная строка успеха при проваленном qdrant-шаге"
        )

    def test_all_skip_green_path_rc0(self):
        """Все шаги-скипы (--no-qdrant --no-ssot): rc=0 и строка успеха."""
        proc = _run_hermetic(["--no-ssot", "--no-qdrant"])
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, (
            f"ожидался rc=0, получен {proc.returncode}; вывод:\n{out}"
        )
        assert "=== Backup completed:" in out, "нет финальной строки успеха"
