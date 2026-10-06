"""Ф4.5c-2: тесты страницы «Квоты» — генератор host-команды/патча (kb-console).

Консоль НЕ читает/не пишет quotas.yaml (в образ ai_workspace не копируется) —
страница только ГЕНЕРИРУЕТ команду ``make quotas-set …`` + YAML-патч; применяет
оператор на хосте. Покрывает:
  (а) билдер core/quotas_patch.py: точные строки команд (минимальный, полный
      с --apply, none → --tokens none, budget-ext), dry_run_command
      без --apply; точный минимальный YAML-патч;
  (б) grants: канонический порядок MODEL_CLASSES + dedupe (CLI-семантика
      _parse_grants); константа-дубль синхронна model_classes.yaml;
  (в) экранирование/отклонение: enum-поля (role/priority/grants) со
      пробелом/кавычкой/мусором → ValueError (fail-closed, CLI exit 3);
      args_safe экранирует shell-метасимволы для ARGS="…";
  (г) числа: CLI-контракты (tokens >= 0, conc >= 1, budget-ext > 0, none
      недопустим для бюджета), bool не является int-значением;
  (д) ROUTES: /quotas с min_role admin (прецедент /tokens, /users, /documents —
      форма раскрывает лимиты/бюджет и генерирует host-команды);
  (е) UI: admin-гейт (403 non-admin по прецеденту tokens.py), смоук-рендер
      admin, tri-state поля (не менять/число/none) → входы билдера,
      копирование в буфер (ui.clipboard.write).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kb_console.core.quotas_patch import (
    MODEL_CLASSES,
    PARTICIPANT_ROLES,
    QUOTA_PRIORITIES,
    UNSET,
    args_safe,
    build_quotas_patch,
)
from kb_console.pages import ROUTES, quotas

# ── (а) билдер: точные строки команд и патча ────────────────────────────


def test_builder_minimal_role_priority_only():
    """Минимум (только обязательные флаги): точная строка, dry-run == command,
    патч — только priority."""
    r = build_quotas_patch(role="guest", priority="low")
    expected = 'make quotas-set ARGS="set --role guest --priority low"'
    assert r["command"] == expected
    assert r["dry_run_command"] == expected
    assert r["patch"] == "participants:\n  guest:\n    priority: low\n"


def test_builder_full_case_with_apply():
    """Полный кейс: все флаги в порядке CLI (role→priority→tokens→conc→grants
    →budget-ext→apply); None → none; grants в каноническом порядке."""
    r = build_quotas_patch(
        role="member",
        priority="high",
        tokens=300000,
        conc=None,
        grants=["fast", "heavy"],
        budget_ext=500,
        apply=True,
    )
    assert r["command"] == (
        "make quotas-set ARGS="
        '"set --role member --priority high --tokens 300000 --conc none '
        '--grants heavy,fast --budget-ext 500 --apply"'
    )
    # dry-run — та же команда БЕЗ --apply
    assert r["dry_run_command"] == (
        "make quotas-set ARGS="
        '"set --role member --priority high --tokens 300000 --conc none '
        '--grants heavy,fast --budget-ext 500"'
    )
    assert r["patch"] == (
        "participants:\n"
        "  member:\n"
        "    priority: high\n"
        "    tokens_per_day: 300000\n"
        "    conc: null\n"
        "    grants: [heavy, fast]\n"
        "budgets:\n"
        "  ext:\n"
        "    limit: 500\n"
    )


def test_builder_tokens_none_and_budget_only():
    """tokens=None → ``--tokens none``; conc/grants не заданы → флаги опущены;
    только budget-ext → патч без touch-полей conc/grants."""
    r = build_quotas_patch(
        role="admin", priority="med", tokens=None, budget_ext=1200, apply=False
    )
    assert r["command"] == (
        'make quotas-set ARGS="set --role admin --priority med '
        '--tokens none --budget-ext 1200"'
    )
    assert r["dry_run_command"] == r["command"]  # apply=False → command и есть dry-run
    assert "--apply" not in r["command"]
    assert r["patch"] == (
        "participants:\n"
        "  admin:\n"
        "    priority: med\n"
        "    tokens_per_day: null\n"
        "budgets:\n"
        "  ext:\n"
        "    limit: 1200\n"
    )


def test_builder_apply_flag_only_difference():
    """apply=True добавляет ровно `` --apply``; патч не зависит от apply."""
    dry = build_quotas_patch(role="guest", priority="high", tokens=10, apply=False)
    wet = build_quotas_patch(role="guest", priority="high", tokens=10, apply=True)
    # --apply добавляется ВНУТРИ кавычек ARGS="… --apply"
    assert wet["command"] == dry["command"][:-1] + ' --apply"'
    assert wet["command"].endswith('--apply"')
    assert wet["dry_run_command"] == dry["command"]
    assert wet["patch"] == dry["patch"]


# ── (б) grants: порядок/dedupe + константа-дубль SSOT ───────────────────


def test_builder_grants_canonical_order_and_dedupe():
    """grants переупорядочиваются в порядок MODEL_CLASSES (heavy,fast,
    local-only) и дедупятся — семантика CLI _parse_grants."""
    r = build_quotas_patch(
        role="guest",
        priority="low",
        grants=["local-only", "fast", "heavy", "fast"],
    )
    assert "--grants heavy,fast,local-only" in r["command"]
    assert "grants: [heavy, fast, local-only]" in r["patch"]


def test_builder_empty_grants_omits_flag():
    """Пустой выбор grants = «не менять» → флаг опущен (CLI не принимает пустой список)."""
    r = build_quotas_patch(role="guest", priority="low", grants=[])
    assert "--grants" not in r["command"]
    assert "grants" not in r["patch"]


def test_model_classes_constant_sync_with_registry():
    """Константа-дубль (SSOT — ai_workspace/registry/model_classes.yaml; менять
    синхронно): состав и порядок heavy,fast,local-only."""
    assert MODEL_CLASSES == ("heavy", "fast", "local-only")


def test_priorities_and_participant_roles_constants():
    """PRIORITIES — SSOT quotas.py::PRIORITIES; participant-роли — SSOT
    quotas.yaml::participants (admin|member|guest)."""
    assert QUOTA_PRIORITIES == ("high", "med", "low")
    assert PARTICIPANT_ROLES == ("admin", "member", "guest")


# ── (в) экранирование / отклонение (fail-closed) ────────────────────────


def test_builder_rejects_role_with_space():
    """Роль со пробелом НЕ из enum → ValueError (CLI exit 3: роль не найдена)."""
    with pytest.raises(ValueError, match="role"):
        build_quotas_patch(role="team lead", priority="low")


def test_builder_rejects_role_with_quote():
    """Кавычка в роли → отклонено до сборки команды (мусор в ARGS невозможен)."""
    with pytest.raises(ValueError):
        build_quotas_patch(role='mem"ber', priority="low")


def test_builder_rejects_unknown_grant():
    """Неизвестный класс модели → ValueError (ref-целостность — CLI exit 3)."""
    with pytest.raises(ValueError, match="grants"):
        build_quotas_patch(role="guest", priority="low", grants=["gpu-plus", "heavy"])


def test_builder_rejects_unknown_priority():
    with pytest.raises(ValueError, match="priority"):
        build_quotas_patch(role="guest", priority="urgent")


def test_args_safe_escapes_shell_metachars():
    """Экранирование для ARGS="…": ``"``, ``$``, `` ` ``, ``\\`` — backslash;
    строка не разрывает двойные кавычки shell (defense-in-depth для значений
    вне enum-контроля)."""
    raw = 'a"b$c`d\\e'
    assert args_safe(raw) == 'a\\"b\\$c\\`d\\\\e'
    # экранированная кавычка не разрывает ARGS="…": ровно backslash+quote
    assert args_safe('x"y') == 'x\\"y'


# ── (г) числа: CLI-контракты ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tokens", -1),  # _parse_nullable_int(minimum=0)
        ("conc", 0),  # _parse_nullable_int(minimum=1)
        ("budget_ext", 0),  # _parse_number: > 0
        ("budget_ext", -5),
        ("tokens", 1.5),  # дробное недопустимо для tokens/conc
        ("conc", 2.5),
        ("tokens", True),  # bool — не int-значение (isinstance(bool, int))
    ],
)
def test_builder_rejects_bad_numbers(field: str, value: object) -> None:
    kw = {field: value}
    with pytest.raises(ValueError):
        build_quotas_patch(role="guest", priority="low", **kw)


def test_builder_rejects_none_budget():
    """CLI не имеет none-семантики для бюджета (число > 0) → ValueError."""
    with pytest.raises(ValueError, match="budget"):
        build_quotas_patch(role="guest", priority="low", budget_ext=None)


def test_builder_accepts_integral_float_numbers():
    """Целочисленный float из ui.number (напр. 500.0) → нормализуется в int."""
    r = build_quotas_patch(
        role="guest", priority="low", tokens=100.0, conc=2.0, budget_ext=500.0
    )
    assert "--tokens 100 --conc 2 --budget-ext 500" in r["command"]


# ── (д) ROUTES ──────────────────────────────────────────────────────────


def test_quotas_route_admin_only():
    """/quotas в ROUTES ровно один раз: label «Квоты», min_role admin.

    Обоснование admin: форма раскрывает лимиты/бюджет и генерирует host-команды
    записи SSOT — прецедент admin-страниц /tokens, /users, /documents.
    """
    matches = [r for r in ROUTES if r[0] == "/quotas"]
    assert len(matches) == 1
    assert matches[0][1] == "Квоты"
    assert matches[0][3] == "admin"
    assert callable(matches[0][2])


# ── (е) UI: гейт, смоук, tri-state, буфер ───────────────────────────────


def test_build_quotas_refuses_non_admin():
    """Non-admin: 403-label + ранний return (прецедент tokens.py/documents.py)."""
    with (
        patch.object(quotas, "is_admin", return_value=False),
        patch.object(quotas, "ui") as mock_ui,
    ):
        quotas.build_quotas()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("403" in t for t in labels)


def test_build_quotas_renders_for_admin():
    """Admin: страница строится — заголовок, предупреждающая плашка (оператор
    применяет на хосте; консоль SSOT не пишет), форма; без redis/сервера."""
    with (
        patch.object(quotas, "is_admin", return_value=True),
        patch.object(quotas, "ui") as mock_ui,
    ):
        quotas.build_quotas()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Квоты" in t for t in labels)
    assert any("оператор" in t.lower() for t in labels)  # плашка про применение на хосте
    assert any("mtime" in t.lower() for t in labels)  # hot-reload подсказка
    # селекторы формы: participant-роль и приоритет
    select_args = [str(c.kwargs.get("options", "")) + str(c.args) for c in mock_ui.select.call_args_list]
    assert any("admin" in a and "member" in a and "guest" in a for a in select_args)
    assert any("high" in a and "med" in a and "low" in a for a in select_args)


def test_tri_state_number_semantics():
    """Tri-state поле: не трогаем → UNSET; none-чекбокс → None; число → int;
    включено без значения → ValueError с именем поля."""
    assert quotas.tri_state_number(False, None, False, field="tokens", minimum=0) is UNSET
    assert quotas.tri_state_number(True, None, True, field="tokens", minimum=0) is None
    assert quotas.tri_state_number(True, 300.0, False, field="tokens", minimum=0) == 300
    with pytest.raises(ValueError, match="tokens"):
        quotas.tri_state_number(True, None, False, field="tokens", minimum=0)


def test_optional_number_semantics():
    """Числовое поле без none-варианта (budget): не трогаем → UNSET; число →
    int; включено без значения → ValueError."""
    assert quotas.optional_number(False, None, field="budget-ext") is UNSET
    assert quotas.optional_number(True, 500.0, field="budget-ext") == 500
    with pytest.raises(ValueError, match="budget-ext"):
        quotas.optional_number(True, None, field="budget-ext")


def test_copy_to_clipboard_uses_nicegui_api():
    """Кнопка «Скопировать» → ui.clipboard.write (NiceGUI ≥2.10; в образе
    3.15) + positive-notify. Textarea остаётся read-only фолбэком: clipboard
    API работает только в secure context (HTTPS/localhost) — на plain-HTTP
    консоли копирование вручную."""
    with patch.object(quotas, "ui") as mock_ui:
        quotas.copy_to_clipboard("make quotas-set ARGS=…")
    mock_ui.clipboard.write.assert_called_once_with("make quotas-set ARGS=…")
    assert mock_ui.notify.call_args.kwargs.get("type") == "positive"


def test_render_result_shows_commands_and_patch():
    """Результат: (а) команда с --apply, (б) dry-run, (в) YAML-патч — каждый
    в read-only textarea + кнопка копирования."""
    result = build_quotas_patch(
        role="member", priority="high", tokens=300000, apply=True
    )
    with patch.object(quotas, "ui") as mock_ui:
        quotas.render_result(result)
    textarea_values = [
        " ".join(str(a) for a in c.args) + str(c.kwargs.get("value", ""))
        for c in mock_ui.textarea.call_args_list
    ]
    assert any(result["command"] in v for v in textarea_values)
    assert any(result["dry_run_command"] in v for v in textarea_values)
    assert any(result["patch"] in v for v in textarea_values)
    assert mock_ui.button.call_count >= 3  # «Скопировать» у каждого блока
