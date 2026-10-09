"""Ф2 (arch-2026-10-09-calib-admin-ui): тесты admin-страницы «Калибровка».

Покрывает:
  (а) ROUTES-регистрация /calibration с min_role="admin";
  (б) admin-гейт: non-admin → 403, CalibClient не создаётся;
  (в) probe start: 202/409/400 (CalibClient через httpx.MockTransport —
      без реальной сети; нормализация probe_start_outcome; notify-типы);
  (г) fail-closed approve-индикация: ceiling + needle ниже пола (или без
      needle) → blocked; approve-кнопка disabled;
  (д) рендер метрик metrics-only: score/needle_rate/ceiling/₽/wall;
      тексты прогона НЕ рендерятся (приватность I5);
  плюс: two-step визард (confirm_live / confirm_ext — ОТДЕЛЬНЫЕ шаги),
  gpu/model fail-soft рендер, git-dirty сигнал, approve 422 reason.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from kb_console.pages import ROUTES, calibration
from kb_console.pages.calibration import (
    CalibClient,
    _approve_payload,
    _probe_payload,
    git_dirty_after_apply,
    needle_evidence,
    probe_start_outcome,
)


def _all_text(mock_ui: MagicMock) -> str:
    """Весь отрендеренный текст (label/chip/badge/… args+kwargs) одной строкой."""
    parts: list[str] = []
    for name in ("label", "chip", "badge", "button", "tooltip", "input", "separator"):
        elem = getattr(mock_ui, name, None)
        if elem is None:
            continue
        for call in elem.call_args_list:
            parts.extend(str(a) for a in call.args)
            parts.extend(str(v) for v in call.kwargs.values())
    return "\n".join(parts)


def _client(handler) -> CalibClient:
    """CalibClient на MockTransport — без реальной сети (loopback-контракт)."""
    return CalibClient(
        base_url="http://testserver",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


# ── (а) ROUTES-регистрация ────────────────────────────────────


def test_calibration_route_admin_only():
    """/calibration в ROUTES, min_role="admin" (скрыт из навигации ниже admin)."""
    matches = [r for r in ROUTES if r[0] == "/calibration"]
    assert len(matches) == 1
    assert matches[0][1] == "Калибровка"
    assert matches[0][2] is calibration.build_calibration
    assert matches[0][3] == "admin"


def test_config_defaults():
    """CALIB_API_URL — loopback:8700 (порт admin-API default); ключ — env."""
    from kb_console.config import CALIB_API_KEY, CALIB_API_URL

    assert CALIB_API_URL == "http://127.0.0.1:8700"
    assert isinstance(CALIB_API_KEY, str)


# ── (б) admin-гейт ────────────────────────────────────────────


def test_build_refuses_non_admin():
    """Non-admin: 403-label + ранний return (CalibClient не создаётся)."""
    with (
        patch.object(calibration, "is_admin", return_value=False),
        patch.object(calibration, "ui") as mock_ui,
        patch.object(calibration, "CalibClient") as mock_cls,
    ):
        calibration.build_calibration()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("403" in t for t in labels)
    mock_cls.assert_not_called()


def test_build_renders_for_admin():
    """Admin: страница строится (заголовок), CalibClient создаётся."""
    with (
        patch.object(calibration, "is_admin", return_value=True),
        patch.object(calibration, "ui") as mock_ui,
        patch.object(calibration, "CalibClient") as mock_client_cls,
    ):
        calibration.build_calibration()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Калибровка" in t for t in labels)
    mock_client_cls.assert_called_once()


# ── (в) probe start: 202/409/400 ──────────────────────────────


def test_probe_start_outcome_distinguishes_codes():
    """Нормализация: 202→started, 409→already_running (+detail), 400→rejected."""
    started = probe_start_outcome(202, {"status": "accepted"})
    assert started["outcome"] == "started"
    running = probe_start_outcome(409, {"detail": "single-flight"})
    assert running["outcome"] == "already_running"
    assert "single-flight" in running["message"]
    rejected = probe_start_outcome(400, {"detail": "требуется confirm_live"})
    assert rejected["outcome"] == "rejected"
    assert "confirm_live" in rejected["message"]
    # fastapi-список валидации (422-форма detail) разворачивается в строки
    listed = probe_start_outcome(
        400, {"detail": [{"msg": "field required"}, {"msg": "bad value"}]}
    )
    assert "field required" in listed["message"] and "bad value" in listed["message"]


@pytest.mark.asyncio
async def test_client_probe_start_202_sends_key_and_body():
    """202 проходит как есть; X-Calib-Key и JSON-тело доставляются (loopback)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("X-Calib-Key")
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            202, json={"status": "accepted", "poll": "/calib/probe/status"}
        )

    client = _client(handler)
    code, body = await client.probe_start({"model_class": "fast"})
    await client.close()
    assert code == 202
    assert body["status"] == "accepted"
    assert seen["path"] == "/calib/probe/start"
    assert seen["key"] == "test-key"
    assert seen["json"] == {"model_class": "fast"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [409, 400],
)
async def test_client_probe_start_error_codes_pass_through(status: int):
    """409/400 НЕ превращаются в исключение — код и detail доходят до UI."""
    detail = {"detail": f"причина-{status}"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=detail)

    client = _client(handler)
    code, body = await client.probe_start({})
    await client.close()
    assert code == status
    assert body["detail"] == f"причина-{status}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "notify_type"),
    [(202, "positive"), (409, "warning"), (400, "negative")],
)
async def test_start_probe_notify_types(code: int, notify_type: str):
    """UI-обработка: 202 → positive, 409 → warning («уже идёт»), 400 → negative."""
    client = MagicMock()
    body = {"status": "accepted"} if code == 202 else {"detail": "x"}
    client.probe_start = AsyncMock(return_value=(code, body))
    client.probe_status = AsyncMock(
        return_value={"status": "idle", "report": None, "progress": None}
    )
    with patch.object(calibration, "ui") as mock_ui:
        await calibration._start_probe(client, {}, {})
    types = [c.kwargs.get("type") for c in mock_ui.notify.call_args_list]
    assert notify_type in types


@pytest.mark.asyncio
async def test_start_probe_network_error_fail_soft():
    """Сетевая ошибка запуска — negative-уведомление, НЕ исключение наружу."""
    client = MagicMock()
    client.probe_start = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch.object(calibration, "ui") as mock_ui:
        await calibration._start_probe(client, {}, {})
    types = [c.kwargs.get("type") for c in mock_ui.notify.call_args_list]
    assert "negative" in types


# ── (г) fail-closed approve-индикация ─────────────────────────


def test_needle_evidence_fail_closed_matrix():
    """ceiling + needle<пола → blocked; ceiling без needle → blocked (нечем
    решать); ceiling + needle≥пола → не blocked (решение по needle);
    без ceiling низкий needle тут не блокирует (рамка D6 — гейт прогона)."""
    blocked_low = needle_evidence({"flags": ["ceiling"], "needle_rate": 0.1})
    assert blocked_low["blocked"] and blocked_low["ceiling"]
    assert "ниже" in blocked_low["reason"] or "<" in blocked_low["reason"]

    blocked_none = needle_evidence({"flags": ["ceiling"]})
    assert blocked_none["blocked"] and not blocked_none["available"]

    ok_ceiling = needle_evidence({"flags": ["ceiling"], "needle_rate": 0.556})
    assert not ok_ceiling["blocked"] and ok_ceiling["ceiling"]
    assert ok_ceiling["available"] and ok_ceiling["rate"] == pytest.approx(0.556)

    low_no_ceiling = needle_evidence({"needle_rate": 0.1})
    assert not low_no_ceiling["blocked"] and not low_no_ceiling["ceiling"]

    assert not needle_evidence(None)["blocked"]
    assert not needle_evidence({"needle_rate": "junk"})["available"]


def test_render_probe_panel_blocks_approve_when_fail_closed():
    """done + ceiling + низкий needle: предупреждение fail-closed, кнопка disabled."""
    state = {
        "probe": {
            "status": "done",
            "report": {
                "flags": ["ceiling"],
                "needle_rate": 0.2,
                "golden_median_score": 1.0,
            },
        },
        "profile_id": "cal-fast-qwen25-7b-0001",
    }
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_probe_panel(state, client=None)
    text = _all_text(mock_ui)
    assert "fail-closed" in text
    approve_calls = [
        c for c in mock_ui.button.call_args_list if "approve" in " ".join(map(str, c.args))
    ]
    assert approve_calls, "approve-кнопка обязана рендериться"
    approve_calls[0].return_value.disable.assert_called_once()


def test_render_probe_panel_approve_enabled_when_done():
    """done + needle-доказательство: кнопка доступна (disable НЕ вызван)."""
    state = {
        "probe": {
            "status": "done",
            "report": {"needle_rate": 0.6, "golden_median_score": 0.9},
        },
        "profile_id": "cal-fast-qwen25-7b-0002",
    }
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_probe_panel(state, client=None)
    approve_calls = [
        c for c in mock_ui.button.call_args_list if "approve" in " ".join(map(str, c.args))
    ]
    assert approve_calls
    approve_calls[0].return_value.disable.assert_not_called()


@pytest.mark.parametrize("status", ["idle", "running", "failed"])
def test_render_probe_panel_approve_disabled_until_done(status: str):
    """Пока статус ≠ done — approve disabled (метрики только по завершении)."""
    state = {"probe": {"status": status, "report": None}}
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_probe_panel(state, client=None)
    approve_calls = [
        c for c in mock_ui.button.call_args_list if "approve" in " ".join(map(str, c.args))
    ]
    assert approve_calls
    approve_calls[0].return_value.disable.assert_called_once()


@pytest.mark.asyncio
async def test_confirm_approve_422_shows_reason():
    """422 fail-closed: причина из detail показана (negative); confirm=True в теле."""
    state = {
        "profile_id": "cal-fast-x-0003",
        "probe": {"status": "done", "report": {"needle_rate": 0.6}},
    }
    client = MagicMock()
    client.approve = AsyncMock(
        return_value=(422, {"detail": "ceiling без needle-доказательства"})
    )
    click_handlers: dict[str, Any] = {}

    class _FakeDialog:
        """Awaitable диалог: `await dialog` кликает «✅ Применить» (HITL пройден)."""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            return None

        def __await__(self):
            async def _run() -> None:
                handler = click_handlers.get("✅ Применить")
                if handler is not None:
                    handler()

            return _run().__await__()

    with patch.object(calibration, "ui") as mock_ui:

        def _button(label, on_click=None, **_kw):
            click_handlers[str(label)] = on_click
            return MagicMock()

        mock_ui.button.side_effect = _button
        mock_ui.dialog.return_value = _FakeDialog()
        await calibration._confirm_approve(client, state)

    types = [c.kwargs.get("type") for c in mock_ui.notify.call_args_list]
    assert "negative" in types
    msgs = " ".join(str(a) for c in mock_ui.notify.call_args_list for a in c.args)
    assert "needle" in msgs
    sent = client.approve.call_args.args[0]
    assert sent["profile_id"] == "cal-fast-x-0003"
    assert sent["confirm"] is True  # HITL пройден → применение, не dry-run
    assert sent["ceiling_ok"] is False  # ceiling-флага в отчёте нет
    assert state["approve_result"]["code"] == 422
    assert state["approve_result"]["dirty"] is False


@pytest.mark.asyncio
async def test_confirm_approve_applied_sets_git_dirty():
    """200 applied: positive-уведомление + git-dirty сигнал (UI-запись)."""
    state = {
        "profile_id": "cal-fast-x-0005",
        "probe": {"status": "done", "report": {"flags": ["ceiling"], "needle_rate": 0.6}},
    }
    client = MagicMock()
    client.approve = AsyncMock(
        return_value=(200, {"applied": True, "exit_code": 0, "output": "written"})
    )
    click_handlers: dict[str, Any] = {}
    checkbox_handlers: dict[str, Any] = {}

    class _FakeDialog:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            return None

        def __await__(self):
            async def _run() -> None:
                # оператор отмечает ceiling_ok (флаг ceiling в отчёте есть)…
                for handler in checkbox_handlers.values():
                    if handler is not None:
                        handler(MagicMock(value=True))
                # …и нажимает «Применить»
                handler = click_handlers.get("✅ Применить")
                if handler is not None:
                    handler()

            return _run().__await__()

    with patch.object(calibration, "ui") as mock_ui:

        def _button(label, on_click=None, **_kw):
            click_handlers[str(label)] = on_click
            return MagicMock()

        def _checkbox(label, on_change=None, **_kw):
            checkbox_handlers[str(label)] = on_change
            return MagicMock()

        mock_ui.button.side_effect = _button
        mock_ui.checkbox.side_effect = _checkbox
        mock_ui.dialog.return_value = _FakeDialog()
        await calibration._confirm_approve(client, state)

    types = [c.kwargs.get("type") for c in mock_ui.notify.call_args_list]
    assert "positive" in types
    sent = client.approve.call_args.args[0]
    assert sent["ceiling_ok"] is True  # явное решение оператора по ceiling
    assert state["approve_result"]["dirty"] is True  # git-dirty после записи


def test_approve_payload_contract():
    """Тело approve: confirm=True (HITL пройден), ceiling_ok эхом, reason."""
    payload = _approve_payload("cal-fast-y-0004", ceiling_ok=True)
    assert payload["profile_id"] == "cal-fast-y-0004"
    assert payload["confirm"] is True
    assert payload["ceiling_ok"] is True
    assert payload["reason"]


# ── (д) metrics-only рендер метрик ────────────────────────────


def test_render_report_metrics_metrics_only():
    """score/needle_rate/ceiling/₽/wall рендерятся; тексты прогона — НИКОГДА."""
    report = {
        "run_id": "probe-20261009-0001",
        "model_id": "qwen2.5:7b",
        "digest": "d" * 32,
        "golden_median_score": 0.9123,
        "golden_dispersion": 0.05,
        "heldout_score": 0.88,
        "parse_rate": 1.0,
        "parse_rate_defined": True,
        "rub": 1.234,
        "wall_s": 612.3,
        "n_runs": 3,
        "flags": ["ceiling"],
        "needle_rate": 0.556,
        # «загрязняющие» ключи — контрактом запрещены в ответах API;
        # если вдруг окажутся, UI рендерить их не должен (I5):
        "document": "SECRET-DOCUMENT-TEXT",
        "draft": "SECRET-DRAFT-TEXT",
        "critic_fragment": "SECRET-CRITIC-FRAGMENT",
    }
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_report_metrics(report)
    text = _all_text(mock_ui)
    assert "0.9123" in text  # score
    assert "0.5560" in text  # needle_rate
    assert "ceiling" in text
    assert "₽ 1.23" in text
    assert "612.3" in text  # wall
    for secret in ("SECRET-DOCUMENT-TEXT", "SECRET-DRAFT-TEXT", "SECRET-CRITIC-FRAGMENT"):
        assert secret not in text


def test_render_report_metrics_sparse_no_crash():
    """Спарс/пустой отчёт — fail-soft, без падения (прочерки вместо чисел)."""
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_report_metrics({})
        calibration._render_report_metrics(None)  # type: ignore[arg-type]
    text = _all_text(mock_ui)
    assert "—" in text


# ── визард: two-step confirm-live + ОТДЕЛЬНЫЙ ext-₽ ───────────


def test_probe_payload_gates():
    """Payload: confirm_live/confirm_ext — ОТДЕЛЬНЫЕ флаги (зеркало ProbeStartBody)."""
    payload = _probe_payload(
        model_class="fast",
        heldout="/data/heldout",
        runs=3,
        live=True,
        ext=True,
        confirm_live=True,
        confirm_ext=True,
    )
    assert payload == {
        "model_class": "fast",
        "heldout": "/data/heldout",
        "runs": 3,
        "live": True,
        "ext": True,
        "confirm_live": True,
        "confirm_ext": True,
    }


def test_wizard_two_separate_dialogs():
    """Два РАЗНЫХ диалога: шаг 1 confirm-live раньше шага 2 ext-₽ (source-контракт)."""
    src = inspect.getsource(calibration._confirm_probe_launch)
    assert "Шаг 1" in src and "confirm-live" in src
    assert "Шаг 2" in src and "ext" in src
    assert src.index("Шаг 1 из 2") < src.index("Шаг 2 из 2")
    # шаг 2 показывается только при ext (ранний return до него)
    assert "if not ext_requested:" in src and "return True" in src


def test_validate_probe_params():
    """Обязательные поля + runs ≥ 3 + ведущий '-' отклоняется (зеркало 400)."""
    ok = {
        "model_class": "fast",
        "heldout": "/h",
        "runs": 3,
    }
    assert _validate(ok) == []
    assert _validate({**ok, "model_class": ""}) != []
    assert _validate({**ok, "heldout": ""}) != []
    assert _validate({**ok, "runs": 2}) != []
    assert _validate({**ok, "heldout": "-evil"}) != []


def _validate(params: dict) -> list[str]:
    base = {"live": False, "ext": False}
    return calibration._validate_probe_params({**base, **params})


# ── карточки модели/GPU: fail-soft рендер ─────────────────────


def test_render_model_card_real_shape():
    """Рендер на РЕАЛЬНОЙ форме /calib/model (shelf/active/drift/classes)."""
    model = {
        "shelf": {"model_id": "qwen2.5:7b", "digest": "abc123"},
        "active": {
            "model_class": "fast",
            "profile_id": "cal-fast-qwen25-7b-01",
            "profile_status": "calibrated",
            "version": 2,
            "calibrated_for": {"model_id": "qwen2.5:7b", "digest": "abc123"},
        },
        "drift": {
            "status": "t2",
            "blocked": False,
            "reason": "digest_mismatch",
            "marks": ["digest"],
            "live_probe": "warn",
        },
        "classes": {
            "fast": {"shelf": "local", "rule": "r", "calibration_status": "calibrated",
                     "active_profile": "cal-fast-qwen25-7b-01", "calibrated_for": {}},
        },
    }
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_model_card(model, None)
    text = _all_text(mock_ui)
    assert "qwen2.5:7b" in text
    assert "cal-fast-qwen25-7b-01" in text
    assert "drift: t2" in text
    assert "fast: calibrated" in text


def test_render_model_card_fail_soft():
    """Ошибка API / спарс-данные — бейдж «недоступен», без падения."""
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_model_card(None, "ConnectError: refused")
        calibration._render_model_card(None, None)
        calibration._render_model_card({}, None)
        calibration._render_model_card({"shelf": None, "active": None, "drift": {}}, None)
    assert mock_ui.badge.called


def test_render_gpu_card_fail_soft():
    """GPU: недоступные источники — серые бейджи (fail-soft), без падения."""
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_gpu_card(None, "ConnectError: boom")
        calibration._render_gpu_card(
            {"ollama_ps": {"available": False}, "gpu_slots": {"available": False}},
            None,
        )
        calibration._render_gpu_card(
            {
                "ollama_ps": {
                    "available": True,
                    "models": [{"name": "qwen2.5:7b", "size_vram": 4700000000}],
                },
                "gpu_slots": {
                    "available": True,
                    "capacity": 1,
                    "used": 1,
                    "free": 0,
                    "lease_ms": 60000,
                    "holdings": {"embed": ["job-1"]},
                },
            },
            None,
        )
    text = _all_text(mock_ui)
    assert "недоступно" in text  # серые бейджи обоих источников
    assert "qwen2.5:7b" in text and "1 из 1" in text  # живые данные


def test_render_probe_panel_git_dirty_badge():
    """git-dirty сигнал после applied-записи → бейдж + подсказка runbook."""
    state = {
        "probe": {"status": "done", "report": {"needle_rate": 0.6}},
        "approve_result": {"code": 200, "body": {"applied": True}, "dirty": True},
    }
    with patch.object(calibration, "ui") as mock_ui:
        calibration._render_probe_panel(state, client=None)
    text = _all_text(mock_ui)
    assert "git-dirty" in text
    assert "calib-admin-api-runbook" in text


def test_git_dirty_signal():
    """applied=True → dirty; маркер dirty в выводе CLI → dirty; иначе нет."""
    assert git_dirty_after_apply(True, "") is True
    assert git_dirty_after_apply(False, "repo is dirty after writeback") is True
    assert git_dirty_after_apply(False, "clean") is False
    assert git_dirty_after_apply(False, "") is False


@pytest.mark.asyncio
async def test_client_get_endpoints_shapes():
    """GET model/gpu/probe_status проходят через один клиент (loopback+ключ)."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/calib/model":
            return httpx.Response(200, json={"shelf": None, "active": None,
                                             "drift": {"status": "ok"}, "classes": {}})
        if request.url.path == "/calib/gpu":
            return httpx.Response(200, json={"ollama_ps": {"available": False},
                                             "gpu_slots": {"available": False}})
        if request.url.path == "/calib/probe/status":
            return httpx.Response(200, json={"status": "idle", "report": None,
                                             "progress": None, "run_id": None,
                                             "started_at": None, "finished_at": None,
                                             "exit_code": None, "error": None})
        raise AssertionError(request.url.path)

    client = _client(handler)
    assert (await client.model())["drift"]["status"] == "ok"
    assert (await client.gpu())["gpu_slots"]["available"] is False
    assert (await client.probe_status())["status"] == "idle"
    await client.close()
    assert paths == ["/calib/model", "/calib/gpu", "/calib/probe/status"]


# ── реальный рендер (nicegui.testing-харнесс — без mock ui) ────


@pytest.mark.filterwarnings(
    # teardown-артефакт nicegui.testing-харнесса (Outbox.loop), не наш код
    "ignore:coroutine 'Outbox.loop' was never awaited:RuntimeWarning",
)
class TestFirstPaint:
    """Первичная отрисовка /calibration в реальном DOM-дереве NiceGUI.

    Ловит класс «refreshable не отрисован / сигнатура элемента неверна»,
    который mock-ui тесты выше не видят (прецедент test_requests_page).
    """

    async def test_admin_first_paint_with_fail_soft_api(self, ui_user):
        """admin + недоступный admin-API: карточки с бейджами «недоступен»,
        визард и статус-панель отрисованы; approve disabled; страница жива."""
        from nicegui import ui

        user = ui_user
        broken = MagicMock()
        broken.model = AsyncMock(side_effect=httpx.ConnectError("refused"))
        broken.gpu = AsyncMock(side_effect=httpx.ConnectError("refused"))
        broken.probe_status = AsyncMock(
            return_value={
                "status": "idle", "run_id": None, "started_at": None,
                "finished_at": None, "exit_code": None, "error": None,
                "progress": None, "report": None,
            }
        )
        broken.close = AsyncMock()

        with (
            patch.object(calibration, "is_admin", return_value=True),
            patch.object(calibration, "CalibClient", return_value=broken),
        ):
            ui.page("/t-calib-failsoft")(calibration.build_calibration)
            await user.open("/t-calib-failsoft")
            await user.should_see("Калибровка системы под текущую модель")
            await user.should_see("admin-API недоступен")  # fail-soft бейджи
            await user.should_see("probe: idle")
            await user.should_see("Запустить probe")  # визард отрисован
        broken.close.assert_not_awaited()  # до disconnect клиент жив

    async def test_admin_first_paint_with_live_facts(self, ui_user):
        """admin + живые факты: полка/профиль/drift-бейдж в DOM."""
        from nicegui import ui

        user = ui_user
        healthy = MagicMock()
        healthy.model = AsyncMock(
            return_value={
                "shelf": {"model_id": "qwen2.5:7b", "digest": "abc123"},
                "active": {
                    "model_class": "fast",
                    "profile_id": "cal-fast-qwen25-7b-01",
                    "profile_status": "calibrated",
                    "version": 2,
                    "calibrated_for": {"model_id": "qwen2.5:7b", "digest": "abc123"},
                },
                "drift": {
                    "status": "ok", "blocked": False, "reason": None,
                    "marks": [], "live_probe": "ok",
                },
                "classes": {},
            }
        )
        healthy.gpu = AsyncMock(
            return_value={
                "ollama_ps": {"available": True, "models": []},
                "gpu_slots": {
                    "available": True, "capacity": 1, "used": 0, "free": 1,
                    "lease_ms": 60000, "holdings": {},
                },
            }
        )
        healthy.probe_status = AsyncMock(
            return_value={"status": "idle", "report": None, "progress": None}
        )
        healthy.close = AsyncMock()

        with (
            patch.object(calibration, "is_admin", return_value=True),
            patch.object(calibration, "CalibClient", return_value=healthy),
        ):
            ui.page("/t-calib-live")(calibration.build_calibration)
            await user.open("/t-calib-live")
            await user.should_see("Полка: qwen2.5:7b")
            await user.should_see("Активный профиль: cal-fast-qwen25-7b-01")
            await user.should_see("drift: ok")
            await user.should_see("GPU-слоты: занято 0 из 1")

    async def test_non_admin_first_paint_403(self, ui_user):
        """non-admin: 403-сообщение, клиент API не создаётся."""
        from nicegui import ui

        user = ui_user
        with (
            patch.object(calibration, "is_admin", return_value=False),
            patch.object(calibration, "CalibClient") as mock_cls,
        ):
            ui.page("/t-calib-403")(calibration.build_calibration)
            await user.open("/t-calib-403")
            await user.should_see("403")
        mock_cls.assert_not_called()
