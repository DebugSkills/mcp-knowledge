#!/usr/bin/env python3
"""Пилот «дельта vs 7B»: qwen2.5:7b как гейт качества на REVISE-итерации.

Вопрос эксперимента: может ли малая модель (qwen2.5:7b) работать критиком,
если вместо полного документа v2 ей давать только дельту v1->v2
(изменённые/добавленные секции) плюс предыдущую критику.

Варианты промпта:
    full      — полный документ v2;
    delta     — только изменённые/добавленные секции + критика;
    delta-map — дельта + список всех заголовков v2 без тел.

Ground truth: две пары (fixed -> PASS, not-fixed -> REVISE), данные
самодостаточны (текст «статьи» про MCP/оркестрацию режимов). Клиент
инжектируется (Protocol с chat(prompt) -> str); реальная реализация
переиспользуется из ai_workspace/tools/vp_ab_pilot.py через import.

Exit codes: 0 — ок; 1 — ошибка конфигурации/сети; 2 — по design,
гипотеза не подтвердилась (см. hypothesis_gate).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_BASE_URL = "http://127.0.0.1:11435/v1"
VARIANTS = ("full", "delta", "delta-map")

ROLE_CONTRACT = (
    "Ты — критик технической документации. КОНТРАКТ ОТВЕТА: первая строка "
    "твоего ответа — ровно одно слово PASS или REVISE заглавными буквами, "
    "без markdown-обёртки, кавычек и любых других символов. Обоснование "
    "(если требуется) — со второй строки."
)

VERDICTS = frozenset({"PASS", "REVISE"})

Doc = dict[str, str]  # заголовок секции -> тело секции

if TYPE_CHECKING:  # только для типов; runtime-импорт — в make_client
    from ai_workspace.tools.vp_ab_pilot import OllamaClient


class ChatClient(Protocol):
    """Минимальный контракт LLM-клиента (инжектируется в run_variant)."""

    def chat(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Pair:
    """Ground-truth пара: v1 + предыдущая критика + v2 + ожидаемый вердикт."""

    name: str
    expected: str
    doc_v1: Doc
    doc_v2: Doc
    critique: str


@dataclass(frozen=True, slots=True)
class CallRecord:
    """Метрики одного вызова критика."""

    variant: str
    pair: str
    expected: str
    verdict: str | None
    correct: bool
    prompt_chars: int
    wall_s: float


_PAIR_FIXED = Pair(
    name="fixed",
    expected="PASS",
    doc_v1={
        "Обзор": (
            "База знаний mcp-knowledge хранит записи в Markdown (SSOT), "
            "поисковый индекс Qdrant строится из чанков этих файлов. Запись "
            "создаётся через write_knowledge, обновляется с optimistic "
            "locking по полю version: конфликт версий отказывает записи, "
            "а не перезатирает её."
        ),
        "Переиндексация": (
            "Команда reindex перестраивает индекс напрямую в активных "
            "коллекциях knowledge_private и knowledge_public: старые точки "
            "удаляются, новые грузятся на их место. На время перестроения "
            "поиск недоступен и запросы возвращают пустой результат — это "
            "штатное поведение, отдельная подготовка не предусмотрена."
        ),
        "Зоны доступа": (
            "Каждая запись относится к зоне public или private. Зона "
            "задаётся при создании записи и может меняться курированием "
            "(set_zone). Поиск по умолчанию ограничен зоной токена "
            "подписчика."
        ),
    },
    doc_v2={
        "Обзор": (
            "База знаний mcp-knowledge хранит записи в Markdown (SSOT), "
            "поисковый индекс Qdrant строится из чанков этих файлов. Запись "
            "создаётся через write_knowledge, обновляется с optimistic "
            "locking по полю version: конфликт версий отказывает записи, "
            "а не перезатирает её."
        ),
        "Переиндексация": (
            "Команда reindex перестраивает индекс по blue-green схеме: "
            "новые точки пишутся в коллекцию-кандидат *_v1, параллельно "
            "обслуживается текущая коллекция через алиас. После завершения "
            "загрузки выполняется атомарный свап алиаса "
            "(knowledge_private -> knowledge_private_v1, аналогично public), "
            "затем старая коллекция удаляется. Поиск остаётся доступным на "
            "всём протяжении перестроения."
        ),
        "Зоны доступа": (
            "Каждая запись относится к зоне public или private. Зона "
            "задаётся при создании записи и может меняться курированием "
            "(set_zone). Поиск по умолчанию ограничен зоной токена "
            "подписчика."
        ),
    },
    critique=(
        "REVISE. Секция «Переиндексация» фактически неверна: перестроение "
        "идёт по blue-green схеме — данные пишутся в новую коллекцию *_v1, "
        "затем выполняется атомарный свап алиаса (knowledge_private -> "
        "knowledge_private_v1, аналогично public), поиск остаётся доступным "
        "во время перестроения. Требуется описать blue-green схему и "
        "атомарный свап алиаса вместо «прямой перезаписи с недоступностью "
        "поиска»."
    ),
)

_PAIR_NOT_FIXED = Pair(
    name="not-fixed",
    expected="REVISE",
    doc_v1={
        "Архитектура": (
            "Оркестратор координирует роли через доску задачи .board.md и "
            "делегирует работу субагентам. Перед доменным навыком "
            "загружается orchestrator-core; уровень задачи "
            "(simple/complex/strategic) определяет форму User Gate."
        ),
        "Токены доступа": (
            "Для доступа к внешним сервисам агент-модель сама читает файл "
            ".env из корня проекта, извлекает нужный ключ и подставляет "
            "его в HTTP-запросы. Ключи хранятся в открытом виде и доступны "
            "любой запущенной роли."
        ),
        "Оркестрация режимов": (
            "Режимы (orchestrator, analyst, critic, code-implementer) "
            "образуют конвейер: анализ -> критика -> реализация. Возврат "
            "результата идёт через attempt_completion, метаданные доски — "
            "SSOT состояния задачи."
        ),
        "Хранение артефактов": (
            "Одноразовые скрипты создаются сразу в .trash/, постоянные — "
            "в scripts/ или app/. Заменяемые файлы перемещаются в .trash/ "
            "вместо удаления."
        ),
    },
    doc_v2={
        "Архитектура": (
            "Оркестратор координирует роли через доску задачи .board.md и "
            "делегирует работу субагентам. Перед доменным навыком "
            "загружается orchestrator-core; уровень задачи "
            "(simple/complex/strategic) определяет форму User Gate."
        ),
        "Токены доступа": (
            "Для доступа к внешним сервисам агент самостоятельно открывает "
            ".env, достаёт оттуда ключ и вкладывает его в заголовки "
            "исходящих запросов. Значения ключей лежат в проекте в "
            "открытом виде, любая роль может их прочитать при "
            "необходимости."
        ),
        "Оркестрация режимов": (
            "Режимы (orchestrator, analyst, critic, code-implementer) "
            "образуют конвейер: анализ -> критика -> реализация. Возврат "
            "результата идёт через attempt_completion, метаданные доски — "
            "SSOT состояния задачи."
        ),
        "Мониторинг": (
            "Ошибки стека агрегируются в sink наблюдаемости, недельный "
            "отчёт формируется по приоритетам P0-P3. Перед закрытием "
            "сигнатура должна исчезнуть из окна наблюдения."
        ),
    },
    critique=(
        "REVISE. Секция «Токены доступа» нарушает инвариант безопасности: "
        "модель НИКОГДА не читает .env и не видит значения ключа — токен "
        "извлекает скрипт-обёртка, которая отдаёт секрет только по "
        "явному флагу --allow-secret и точечно. Требуется переписать "
        "секцию: описать скрипт-обёртку, запрет доступа модели к .env и "
        "политику --allow-secret."
    ),
)

PAIRS: tuple[Pair, ...] = (_PAIR_FIXED, _PAIR_NOT_FIXED)


def diff_sections(v1: Doc, v2: Doc) -> dict[str, object]:
    """Секционная дельта v1->v2: added/changed (заголовок -> тело v2), removed."""
    added = {h: b for h, b in v2.items() if h not in v1}
    changed = {h: b for h, b in v2.items() if h in v1 and v1[h] != b}
    removed = sorted(set(v1) - set(v2))
    return {"added": added, "changed": changed, "removed": removed}


def _render_doc(doc: Doc) -> str:
    return "\n\n".join(f"### {head}\n{body}" for head, body in doc.items())


def _render_delta(delta: dict[str, object]) -> str:
    """Человекочитаемая дельта: изменённые, добавленные, удалённые секции."""
    changed = delta.get("changed")
    added = delta.get("added")
    removed = delta.get("removed")
    parts: list[str] = []
    if isinstance(changed, dict) and changed:
        parts.append("Изменённые секции (тело — версия v2):")
        parts.extend(f"### {head}\n{body}" for head, body in changed.items())
    if isinstance(added, dict) and added:
        parts.append("Добавленные секции:")
        parts.extend(f"### {head}\n{body}" for head, body in added.items())
    if isinstance(removed, list) and removed:
        parts.append("Удалённые секции: " + ", ".join(map(str, removed)))
    if not parts:
        parts.append("(изменений нет)")
    return "\n\n".join(parts)


def build_critic_prompt(
    variant: str,
    doc_v2: Doc,
    delta: dict[str, object],
    critique: str,
    headings: list[str],
) -> str:
    """Промпт критика для выбранной вариант-стратегии (full/delta/delta-map)."""
    if variant not in VARIANTS:
        raise ValueError(f"неизвестный вариант: {variant}")
    parts = [
        ROLE_CONTRACT,
        "",
        (
            "Задача: вторая итерация ревью. Документ правили по предыдущей "
            "критике; проверь, устранены ли замечания."
        ),
        "",
        "## Предыдущая критика",
        critique.strip(),
    ]
    if variant == "full":
        parts += ["", "## Документ v2 (полностью)", _render_doc(doc_v2)]
    elif variant == "delta":
        parts += [
            "",
            "## Дельта v1->v2 (только изменённые и добавленные секции)",
            _render_delta(delta),
            "",
            "Секции, не вошедшие в дельту, не менялись.",
        ]
    else:  # delta-map
        parts += [
            "",
            "## Дельта v1->v2 (только изменённые и добавленные секции)",
            _render_delta(delta),
            "",
            "## Карта документа v2 (все заголовки, без тел)",
            "\n".join(f"- {head}" for head in headings),
        ]
    parts += [
        "",
        (
            "Критерий: если все замечания критики учтены — PASS, иначе REVISE "
            "с кратким указанием, что осталось неисправленным."
        ),
    ]
    return "\n".join(parts)


def parse_verdict(text: str) -> str | None:
    """PASS/REVISE из первой строки ответа (после снятия markdown-обёртки).

    Регистронезависимо; None — если контракт первой строки нарушен.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines.pop()
        cleaned = "\n".join(lines).strip()
    if not cleaned:
        return None
    first = cleaned.splitlines()[0].lstrip("`#>*- \t—").strip()
    if not first:
        return None
    token = first.split()[0].strip("`*_#>:,.!?—-").upper()
    return token if token in VERDICTS else None


def pair_prompt(variant: str, pair: Pair) -> str:
    """Готовый промпт для пары (используется run_variant и dry-run печатью)."""
    delta = diff_sections(pair.doc_v1, pair.doc_v2)
    return build_critic_prompt(
        variant, pair.doc_v2, delta, pair.critique, list(pair.doc_v2)
    )


def run_variant(
    variant: str,
    pair: Pair,
    client: ChatClient | None,
    n: int = 1,
) -> list[CallRecord]:
    """n вызовов критика на (variant x pair); client=None -> dry-run (1 прогон)."""
    prompt = pair_prompt(variant, pair)
    records: list[CallRecord] = []
    for _ in range(1 if client is None else max(1, n)):
        verdict: str | None = None
        wall = 0.0
        if client is not None:
            started = time.monotonic()
            verdict = parse_verdict(client.chat(prompt))
            wall = time.monotonic() - started
        records.append(
            CallRecord(
                variant=variant,
                pair=pair.name,
                expected=pair.expected,
                verdict=verdict,
                correct=verdict == pair.expected,
                prompt_chars=len(prompt),
                wall_s=round(wall, 3),
            )
        )
    return records


def aggregate(records: list[CallRecord]) -> dict[str, object]:
    """Агрегаты по вариантам + delta_ratio = prompt_chars(delta)/prompt_chars(full)."""
    by_variant: dict[str, list[CallRecord]] = {}
    for rec in records:
        by_variant.setdefault(rec.variant, []).append(rec)
    out: dict[str, object] = {}
    for name, recs in sorted(by_variant.items()):
        out[name] = {
            "verdict_compliance": sum(r.verdict is not None for r in recs) / len(recs),
            "accuracy": sum(r.correct for r in recs) / len(recs),
            "mean_prompt_chars": statistics.mean(r.prompt_chars for r in recs),
        }
    delta_stats = out.get("delta")
    full_stats = out.get("full")
    if isinstance(delta_stats, dict) and isinstance(full_stats, dict):
        full_chars = full_stats["mean_prompt_chars"] or 1.0
        out["delta_ratio"] = delta_stats["mean_prompt_chars"] / full_chars
    return out


def hypothesis_gate(agg: dict[str, object]) -> int | None:
    """2 — гипотеза не подтвердилась: delta хуже full на >0.2 accuracy
    или delta нарушает контракт вердикта чаще 20% (compliance < 0.8)."""
    delta = agg.get("delta")
    if not isinstance(delta, dict):
        return None
    full = agg.get("full")
    accuracy_full = full["accuracy"] if isinstance(full, dict) else 1.0
    if delta["accuracy"] < accuracy_full - 0.2:
        return 2
    if delta["verdict_compliance"] < 0.8:
        return 2
    return None


class _OllamaPilotClient:
    """Адаптер vp_ab_pilot.OllamaClient под контракт chat(prompt) -> str.

    Переиспользует сетевой слой пилота V-P (retry x2, usage-журнал),
    а не копирует его; role/model_class фиксируются для метки полки.
    """

    def __init__(self, inner: OllamaClient) -> None:
        self._inner = inner

    def chat(self, prompt: str) -> str:
        return str(
            self._inner.complete(
                role="critic",
                model_class="vs_delta_pilot",
                prompt=prompt,
                inputs={},
            )
        )


def make_client(model: str, base_url: str) -> ChatClient:
    """Реальный клиент — OllamaClient из vp_ab_pilot (import, не копия).

    base_url здесь — префикс OpenAI-API (по умолчанию
    http://127.0.0.1:11435/v1), как в ТЗ; OllamaClient ждёт полный
    эндпоинт, поэтому достраиваем /chat/completions.
    """
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from ai_workspace.tools.vp_ab_pilot import OllamaClient

    endpoint = base_url.rstrip("/") + "/chat/completions"
    return _OllamaPilotClient(OllamaClient(url=endpoint, model=model))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Пилот «дельта vs 7B»: критик qwen2.5:7b по дельте документа",
    )
    parser.add_argument(
        "--variant",
        choices=["all", *VARIANTS],
        default="all",
        help="какую prompt-стратегию гонять (all — все три)",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=2,
        help="повторов на (вариант x пару)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=Path, default=None, help="JSON-отчёт")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="без сети: печатает промпты и метрики (verdict=NONE)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    variants = list(VARIANTS) if args.variant == "all" else [args.variant]

    client: ChatClient | None = None
    if not args.dry_run:
        try:
            client = make_client(args.model, args.base_url)
        except Exception as exc:  # noqa: BLE001
            print(f"[config] не удалось собрать клиент: {exc}", file=sys.stderr)
            return 1

    records: list[CallRecord] = []
    for variant in variants:
        for pair in PAIRS:
            if args.dry_run:
                print("=" * 72)
                print(
                    f"[PROMPT] variant={variant} pair={pair.name} "
                    f"expected={pair.expected}"
                )
                print(pair_prompt(variant, pair))
            try:
                records.extend(run_variant(variant, pair, client, args.n))
            except Exception as exc:  # noqa: BLE001
                print(f"[network] {variant}/{pair.name}: {exc}", file=sys.stderr)
                return 1

    agg = aggregate(records)
    print("\n== МЕТРИКИ ==")
    for rec in records:
        verdict = rec.verdict if rec.verdict is not None else "NONE"
        print(
            f"{rec.variant:<9} {rec.pair:<10} expected={rec.expected:<6} "
            f"verdict={verdict:<6} correct={rec.correct!s:<5} "
            f"prompt_chars={rec.prompt_chars} wall_s={rec.wall_s}"
        )
    for name, value in agg.items():
        print(f"aggregate {name}: {value}")

    if args.out is not None:
        payload = {
            "model": args.model,
            "base_url": args.base_url,
            "dry_run": args.dry_run,
            "n": args.n,
            "records": [asdict(rec) for rec in records],
            "aggregate": agg,
        }
        args.out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"отчёт: {args.out}")

    if args.dry_run:
        return 0
    gate = hypothesis_gate(agg)
    if gate is not None:
        print(
            "[gate] гипотеза не подтвердилась: см. aggregate delta "
            "(accuracy/verdict_compliance)",
            file=sys.stderr,
        )
    return gate if gate is not None else 0


if __name__ == "__main__":
    sys.exit(main())
