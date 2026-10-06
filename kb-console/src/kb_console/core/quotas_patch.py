"""Генератор host-команды/патча правки квот участника (Ф4.5c-2, kb-console).

Консоль НЕ читает и НЕ пишет ``ai_workspace/registry/quotas.yaml`` (в образ
консоли ``ai_workspace`` не копируется, реестр не смонтирован — осознанное
решение Ф4.5c-1). Эта страница-помощник только ГЕНЕРИРУЕТ готовую команду
``make quotas-set …`` и минимальный YAML-патч; применяет команду ОПЕРАТОР
на хосте (CLI: dry-run по умолчанию, ``--apply`` = бэкап .trash + атомарная
запись + пост-валидация; рантайм подхватывает правку по mtime — рестарт
не нужен; git-коммит — за оператором).

Чистое ядро без NiceGUI (тестируется без UI — прецедент pages/queue.py).
Контракты ниже — НАМЕРЕННЫЕ ДУБЛИ SSOT (консоль не импортирует
``ai_workspace``; прецедент Ф2/Ф4.4b): менять синхронно.
"""

from __future__ import annotations

from typing import Any

UNSET: Any = object()
"""«Поле не трогаем» — флаг опускается (ср. CLI ``_UNSET`` в quotas_set.py).

Отличать от ``None``: None = ЯВНЫЙ ``none`` (без личного лимита, YAML null).
"""

PARTICIPANT_ROLES: tuple[str, ...] = ("admin", "member", "guest")
"""SSOT — ai_workspace/registry/quotas.yaml::participants; менять синхронно."""

QUOTA_PRIORITIES: tuple[str, ...] = ("high", "med", "low")
"""SSOT — ai_workspace/registry/quotas.py::PRIORITIES; менять синхронно."""

MODEL_CLASSES: tuple[str, ...] = ("heavy", "fast", "local-only")
"""SSOT — ai_workspace/registry/model_classes.yaml; менять синхронно.

Намеренный дубль (консоль не импортирует ai_workspace): порядок канонический
— в нём сериализуются --grants и YAML-патч. Покрыт тестом на состав/порядок.
"""


def args_safe(text: str) -> str:
    """Экранировать значение для вставки в ``ARGS="…"`` (shell double-quote).

    ``"``, ``$``, `` `` ` ``, ``\\`` получают backslash-префикс. Значения формы
    enum/int и не содержат этих символов (enum-поля валидируются до сборки) —
    это defense-in-depth: команда остаётся корректной shell-строкой, даже если
    состав констант изменится.
    """
    return "".join("\\" + ch if ch in '"$`\\' else ch for ch in str(text))


def _fmt_scalar(value: float | None) -> str:
    """YAML-представление скаляра в стиле quotas.yaml (CLI ``_fmt_scalar``)."""
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _require_int(
    value: object, what: str, *, minimum: int, allow_none: bool
) -> int | None:
    """Проверить число по CLI-контракту ``_parse_nullable_int`` (fail-closed).

    None допустим только там, где CLI понимает ``none``; bool не принимается
    (``isinstance(True, int)`` — ловим явно); float — только целый.
    """
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{what}: none недопустим (ожидается число)")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # ValueError (не TypeError): единый класс ошибок CLI quotas_set — exit 3
        raise ValueError(f"{what}: ожидается целое число, получено {value!r}")  # noqa: TRY004
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{what}: ожидается целое число, получено {value!r}")
        value = int(value)
    if value < minimum:
        raise ValueError(f"{what}: должно быть >= {minimum}, получено {value}")
    return int(value)


def _normalize_grants(grants: list[str] | tuple[str, ...] | None) -> list[str]:
    """Валидировать и привести grants к каноническому порядку MODEL_CLASSES.

    Semantics CLI ``_parse_grants``: dedupe с сохранением порядка — но здесь
    порядок канонический (MODEL_CLASSES), чтобы команда/патч были
    детерминированными независимо от порядка выбора в UI. Unknown класс —
    ValueError (ref-целостность у CLI проверяет validate_quotas, exit 3).
    """
    if grants is None:
        return []
    if not isinstance(grants, (list, tuple)):
        raise ValueError(  # noqa: TRY004 — единый класс ошибок CLI (exit 3)
            f"grants: ожидается список классов, получено {grants!r}"
        )
    known = set(MODEL_CLASSES)
    for item in grants:
        if not isinstance(item, str) or item not in known:
            raise ValueError(
                f"grants: неизвестный класс модели {item!r} "
                f"(допустимы: {', '.join(MODEL_CLASSES)})"
            )
    return [c for c in MODEL_CLASSES if c in set(grants)]


def build_quotas_patch(
    *,
    role: str,
    priority: str,
    tokens: float | None = UNSET,
    conc: float | None = UNSET,
    grants: list[str] | tuple[str, ...] | None = UNSET,
    budget_ext: float | None = UNSET,
    apply: bool = False,
) -> dict[str, str]:
    """Собрать host-команду ``make quotas-set`` + минимальный YAML-патч.

    Вход (fail-closed, семантика ошибок = CLI ``quotas_set.py`` exit 3):
      - ``role`` — participant-роль (admin|member|guest); UI-роль доступа
        консоли (admin/editor/contributor) — ДРУГОЕ понятие, гейт страницы;
      - ``priority`` — high|med|low (обязателен, как в CLI);
      - ``tokens``/``conc`` — UNSET (не трогать) | None (``none`` → YAML
        null, «только общий бюджет K») | целое (>= 0 / >= 1);
      - ``grants`` — UNSET/None/[] (не трогать) | подмножество классов
        моделей; сериализуется в каноническом порядке MODEL_CLASSES;
      - ``budget_ext`` — UNSET (не трогать) | число > 0 (₽, budgets.ext.limit;
        none-семантики у CLI для бюджета НЕТ — ValueError);
      - ``apply`` — True добавляет `` --apply`` (без него CLI = dry-run).

    Выход: ``{"command" | "dry_run_command" | "patch"}`` — точные строки.
    ``command`` с ``apply=False`` совпадает с ``dry_run_command``.
    """
    if not isinstance(role, str) or role not in PARTICIPANT_ROLES:
        raise ValueError(
            f"role: ожидается одна из {', '.join(PARTICIPANT_ROLES)}, "
            f"получено {role!r}"
        )
    if priority not in QUOTA_PRIORITIES:
        raise ValueError(
            f"priority: ожидается одна из {', '.join(QUOTA_PRIORITIES)}, "
            f"получено {priority!r}"
        )

    # ── флаги в порядке argparse CLI: role → priority → tokens → conc →
    #    grants → budget-ext (→ apply). None → 'none'; UNSET → флаг опущен.
    parts: list[str] = [
        "set",
        f"--role {args_safe(role)}",
        f"--priority {args_safe(priority)}",
    ]
    patch_fields: list[tuple[str, str]] = [("priority", priority)]

    if tokens is not UNSET:
        tokens_v = _require_int(tokens, "--tokens", minimum=0, allow_none=True)
        parts.append(f"--tokens {'none' if tokens_v is None else tokens_v}")
        patch_fields.append(("tokens_per_day", _fmt_scalar(tokens_v)))

    if conc is not UNSET:
        conc_v = _require_int(conc, "--conc", minimum=1, allow_none=True)
        parts.append(f"--conc {'none' if conc_v is None else conc_v}")
        patch_fields.append(("conc", _fmt_scalar(conc_v)))

    if grants is not UNSET:
        grants_v = _normalize_grants(grants)
        if grants_v:
            parts.append(f"--grants {args_safe(','.join(grants_v))}")
            patch_fields.append(("grants", "[" + ", ".join(grants_v) + "]"))

    if budget_ext is not UNSET:
        budget_v = _require_int(budget_ext, "--budget-ext", minimum=1, allow_none=False)
        if budget_v is not None and budget_v <= 0:  # pragma: no cover — guarded above
            raise ValueError("--budget-ext: должно быть > 0")
        parts.append(f"--budget-ext {budget_v}")

    args_line = " ".join(parts)
    dry_run = f'make quotas-set ARGS="{args_line}"'
    command = f'make quotas-set ARGS="{args_line} --apply"' if apply else dry_run

    # ── минимальный YAML-патч: только touch-поля, отступы как в quotas.yaml
    #    (participants → роль → поля; budgets.ext.limit — отдельной секцией).
    lines: list[str] = ["participants:", f"  {role}:"]
    lines += [f"    {name}: {value}" for name, value in patch_fields]
    if budget_ext is not UNSET:
        budget_v = _require_int(budget_ext, "--budget-ext", minimum=1, allow_none=False)
        lines += ["budgets:", "  ext:", f"    limit: {_fmt_scalar(budget_v)}"]
    patch = "\n".join(lines) + "\n"

    return {"command": command, "dry_run_command": dry_run, "patch": patch}
