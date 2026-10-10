"""S1 (Ф6-5, arch-2026-10-10-ai-ws-acceptance): чат — АКТИВНЫЙ tool-loop.

Сценарий (задание Ф6 сессии 5): admin на /chat вводит запрос, триггерящий
``search_knowledge`` → страница стримит ответ (assistant-сообщение
наполняется инкрементально) → снапшот финального артефакта (текст ответа
+ показанные цитаты) пишется в plans/_provenance/arch-2026-10-10-ai-ws-acceptance/.

Слои проверки (по носителям, не по представлениям):

1. **Серверное доказательство tool-loop**: дельта счётчика
   ``mcp_tool_requests_total{tool="search_knowledge"}`` на MCP-сервере
   (/metrics) до/после хода ≥ 1 — модель РЕАЛЬНО ходила в поиск; текст
   ответа с «цитатами» без вызова инструмента таким не является.
2. **Стрим**: сэмплы длины assistant-сообщения в ходе генерации — ≥2
   различных непустых длин (дельты отражаются на носителе, спека §8 E/H).
3. **Терминальное состояние**: статус «готово» (не пустой = ошибка);
   fail-soft путь «Ошибка стрима» на happy-path запрещён.
4. **Целостность артефакта**: ответ непустой и не является сырым JSON
   tool-вызова; цитаты из KB — в тексте ответа (страница /chat не имеет
   отдельного citation-виджета: цитирование — часть markdown-ответа).
5. **0 ошибок консоли** (детектор conftest, teardown).

Контур: self-инстанс per-user (e2e_chat_server) с ЖИВЫМИ бэкендами:
MCP :8000 (WS_MCP_KEY из env раннера) + LiteLLM-контейнер (local=
qwen2.5:7b ⇒ 0₽). Недоступность бэкендов → skip с причиной (e2e_chat_backends).

Таймауты: локальный 7B-стрим не мгновенный (load+2 итерации+поиск;
live-замер ~17s) — бюджет 180s, поллинг 250ms.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

# ── Реальные контракты (якоря импортом, НЕ копии в тесте) ──────────
from kb_console.core.ws_zone import zone_for_role
from kb_console.pages.chat import _zone_caption

#: Запрос, триггерящий search_knowledge: явное требование поиска в KB
#: (системное сообщение tool_loop уже описывает инструмент; live-пробы
#: сессии 5 подтверждают срабатывание на этом запросе).
CHAT_QUERY = (
    "Найди в базе знаний сообщества через инструмент search_knowledge "
    "материалы про протокол MCP и перечисли названия найденных записей."
)

#: Статусные маркеры страницы (pages/chat.py:: _run_turn).
_STATUS_STREAMING = "стрим…"
_STATUS_DONE = "готово"
_STREAM_ERROR_NOTIFY = "Ошибка стрима"

#: Бюджет хода: локальный qwen2.5:7b, до 2 LLM-итераций + MCP-поиск.
_TURN_BUDGET_S = 180.0
_POLL_MS = 250

#: Директория снапшотов (provenance трассы приёмки).
_PROV_DIR = (
    Path(__file__).resolve().parents[3]
    / "plans"
    / "_provenance"
    / "arch-2026-10-10-ai-ws-acceptance"
)


def _snapshot_artifact(
    *,
    role: str,
    zone: str,
    query: str,
    answer: str,
    samples: list[int],
    counter_before: int,
    counter_after: int,
    console_errors_n: int,
) -> Path:
    """Записать снапшот финального артефакта S1 (ответ + цитаты)."""
    ts = time.strftime("%Y%m%dT%H%M%S")
    path = _PROV_DIR / f"f6-s1-artifact-{ts}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    quoted = [ln.strip() for ln in answer.splitlines() if "«" in ln or "»" in ln]
    citations_block = (
        "\n".join(f"- {q}" for q in quoted)
        if quoted
        else "- (строки с «кавычками-цитатами» в ответе не обнаружены; "
        "цитирование — в структуре списка найденных записей выше)"
    )
    path.write_text(
        "# S1 — снапшот финального артефакта чата (Ф6, сессия 5)\n\n"
        f"- **ts:** {ts}\n"
        f"- **контур:** self-инстанс per-user, роль {role}, зона выборки {zone}\n"
        f"- **запрос:** {query!r}\n"
        "- **бэкенды:** MCP http://127.0.0.1:8000 (health ok) · "
        "LiteLLM → ollama qwen2.5:7b (model local, 0₽)\n"
        f"- **стрим:** {len(samples)} сэмплов длины {samples}\n"
        "- **search_knowledge (MCP /metrics):** "
        f"{counter_before} → {counter_after} (+{counter_after - counter_before})\n"
        f"- **целостность:** ответ непустой (len={len(answer)}), "
        f"fail-soft нет, ошибок консоли {console_errors_n}\n\n"
        "## Ответ ассистента (verbatim)\n\n"
        f"{answer}\n\n"
        "## Показанные цитаты\n\n"
        f"{citations_block}\n",
        encoding="utf-8",
    )
    return path


def test_s1_chat_toolloop_stream_artifact(
    chat_as_admin, console_errors, e2e_chat_backends
) -> None:
    """S1 активный: tool-loop (search_knowledge) + стрим + снапшот артефакта."""
    session = chat_as_admin
    page: Page = session.page
    zone = zone_for_role(session.role)

    counter_before = e2e_chat_backends.search_knowledge_calls()

    page.goto(f"{session.url}/chat")
    # Бейдж прозрачности I5 (реальный форматтер + резолвер)
    page.get_by_text(_zone_caption(session.role, zone), exact=False).wait_for(
        state="visible"
    )

    page.get_by_placeholder("Спросите что-нибудь…").fill(CHAT_QUERY)
    page.get_by_role("button", name="Отправить").click()

    answer = page.locator("div.nicegui-markdown").first
    done_marker = page.locator(".text-caption.text-grey").filter(
        has_text=_STATUS_DONE
    )
    samples: list[int] = []
    deadline = time.monotonic() + _TURN_BUDGET_S
    done = False
    while time.monotonic() < deadline:
        body = page.inner_text("body")
        if _STREAM_ERROR_NOTIFY in body:
            pytest.fail(
                f"fail-soft путь на happy-path: {_STREAM_ERROR_NOTIFY}; "
                f"answer={answer.inner_text()[:300]!r}",
                pytrace=False,
            )
        text = answer.inner_text()
        if text.strip() and (not samples or len(text) != samples[-1]):
            samples.append(len(text))
        if done_marker.count():
            done = True
            break
        page.wait_for_timeout(_POLL_MS)

    assert done, (
        f"статус «{_STATUS_DONE}» не наступил за {_TURN_BUDGET_S}s; "
        f"сэмплы={samples}; ответ={answer.inner_text()[:300]!r}"
    )

    final = answer.inner_text().strip()
    # Целостность артефакта: непустой содержательный ответ, не сырой JSON вызова
    assert len(final) >= 40, f"ответ пуст/тривиален: {final!r}"
    assert not final.startswith("{") and not final.startswith("```json"), (
        f"носитель показывает tool-call JSON вместо ответа: {final[:200]!r}"
    )
    # Стрим реально наполнял носитель (спека §8 E/H: дельты → element.update)
    assert len(set(samples)) >= 2, f"стрим не наблюдался: сэмплы={samples}"

    # Серверное доказательство: search_knowledge реально вызывался в этом ходе
    counter_after = e2e_chat_backends.search_knowledge_calls()
    assert counter_after >= counter_before + 1, (
        f"tool-loop не ходил в MCP: счётчик {counter_before} → {counter_after}; "
        f"ответ мог быть галлюцинацией без поиска"
    )

    path = _snapshot_artifact(
        role=session.role,
        zone=zone,
        query=CHAT_QUERY,
        answer=final,
        samples=samples,
        counter_before=counter_before,
        counter_after=counter_after,
        console_errors_n=len(console_errors.errors),
    )
    assert path.is_file() and path.stat().st_size > 0, f"снапшот не записан: {path}"

    assert console_errors.errors == [], console_errors.summary()
