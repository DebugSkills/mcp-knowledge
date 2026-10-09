"""Страница «Калибровка» — admin-UI калибровки системы под текущую модель.

arch-2026-10-09-calib-admin-ui Ф2: тонкий клиент host-side admin-API
(``ai_workspace/calibration/admin_api.py``, bind 127.0.0.1:8700, заголовок
``X-Calib-Key``). Карточки модели/GPU → визард probe (two-step HITL) →
поллинг статуса → approve; Ф3b — live-пара base↔variant (``/calib/pair/*``)
+ запись решения P5 (``/calib/record``); Ф4b — кнопка «ре-калибровать»
(предзаполнение визарда probe, БЕЗ запуска) + история отчётов
(``/calib/reports``). Ноль логики калибровки на стороне
UI — все гейты живут в admin-API/CLI-канонах (``probe_run``/
``profile_approve``/``variant_pair``).

Инварианты (план §«protected», НЕ ослаблять):

- **admin-only**: runtime-гейт ``is_admin()`` (403-паттерн documents.py) +
  ``min_role="admin"`` в ROUTES (навигация скрыта ниже admin);
- **two-step confirm-live**: шаг 1 — живой прогон (``confirm_live``), шаг 2 —
  ОТДЕЛЬНОЕ подтверждение ext-₽ (``confirm_ext``; строже CLI, где оба гейтит
  один ``--confirm-live``);
- **approve fail-closed** (F-2а): флаг ``ceiling`` при needle ниже пола (или
  без needle-замера) → предупреждение + ЗАПРЕТ применения в UI (сервер
  откажет 422 независимо — UI не слабее CLI);
- **P5 всегда за оператором**: UI только предлагает/исполняет подтверждённое;
- **metrics-only** (приватность I5): рендерятся ТОЛЬКО явные поля whitelist
  (``_REPORT_FIELDS`` admin_api.py); тексты прогона не отображаются никогда.

Паттерны (quality.py/documents.py): ``@ui.refreshable`` + ``ui.timer``
поллинг; HITL-диалог — bare await, non-persistent; ``on_disconnect`` →
``timer.cancel()`` + закрытие http-клиента.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from nicegui import ui

from ..config import CALIB_API_KEY, CALIB_API_URL
from ..core.identity import is_admin
from ..core.utils import human_size

#: Runbook эксплуатации admin-API (путь в репозитории mcp-knowledge)
RUNBOOK_PATH = "docs/operations/calib-admin-api-runbook.md"

#: Пол needle_rate (SSOT: ai_workspace/calibration/policy.py
#: NEEDLE_RATE_FLOOR; kb-console не импортирует ai_workspace — зеркало,
#: синхронизировать при смене политики)
NEEDLE_RATE_FLOOR: float = 0.5

#: Минимум прогонов probe (SSOT: ai_workspace/calibration/probe.py MIN_RUNS)
MIN_RUNS: int = 3

#: Дефолтная пара mode-YAML (SSOT: ai_workspace/tools/variant_pair.py
#: DEFAULT_BASE_MODE/DEFAULT_VARIANT_MODE — statya / statya.deep;
#: kb-console не импортирует ai_workspace — зеркало, синхронизировать
#: при смене конвенций §7.2)
PAIR_BASE_DEFAULT: str = "ai_workspace/modes/statya.yaml"
PAIR_VARIANT_DEFAULT: str = "ai_workspace/modes/statya.deep.yaml"

#: Зона пары по умолчанию (PairStartBody.zone; выбора зоны в UI нет)
PAIR_ZONE_DEFAULT: str = "public"

#: Интервал поллинга статуса probe (сек; loopback — дёшево)
PROBE_POLL_INTERVAL: float = 2.0

#: Таймаут запросов к admin-API (loopback)
REQUEST_TIMEOUT: float = 15.0


# ── HTTP-клиент admin-API (module-level — тестируемо) ──────────


class CalibClient:
    """Тонкий клиент admin-API калибровки (GET/POST, заголовок X-Calib-Key).

    ``trust_env=False`` — API строго loopback, env-прокси игнорируются
    (прецедент tests/_local_http.py). Сетевые ошибки пробрасываются —
    вызывающий код решает, как показать (fail-soft баннер).
    """

    def __init__(
        self,
        base_url: str = CALIB_API_URL,
        api_key: str = CALIB_API_KEY,
        timeout: float = REQUEST_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Calib-Key": api_key},
            timeout=timeout,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str) -> dict[str, Any]:
        resp = await self._client.get(path)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def model(self) -> dict[str, Any]:
        """GET /calib/model — факты полки + активный профиль + drift."""
        return await self._get_json("/calib/model")

    async def gpu(self) -> dict[str, Any]:
        """GET /calib/gpu — VRAM (ollama /api/ps) + ws-lease GPU-слоты."""
        return await self._get_json("/calib/gpu")

    async def probe_status(self) -> dict[str, Any]:
        """GET /calib/probe/status — state + partial + итог (whitelist)."""
        return await self._get_json("/calib/probe/status")

    async def probe_start(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST /calib/probe/start → (код, тело): 202 started / 409 single-flight / 400 гейт."""
        resp = await self._client.post("/calib/probe/start", json=payload)
        return resp.status_code, _body_of(resp)

    async def approve(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST /calib/approve → (код, тело): 200 / 422 fail-closed / 400 / 500."""
        resp = await self._client.post("/calib/approve", json=payload)
        return resp.status_code, _body_of(resp)

    async def pair_start(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST /calib/pair/start → 202 accepted / 409 single-flight / 400 гейт."""
        resp = await self._client.post("/calib/pair/start", json=payload)
        return resp.status_code, _body_of(resp)

    async def pair_status(self) -> dict[str, Any]:
        """GET /calib/pair/status — статус + отчёты плеч (whitelist) + reasons."""
        return await self._get_json("/calib/pair/status")

    async def record(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST /calib/record → 200 / 400 гейт / 409 нет пары / 422 fail-closed."""
        resp = await self._client.post("/calib/record", json=payload)
        return resp.status_code, _body_of(resp)

    async def reports(self, limit: int = 20) -> dict[str, Any] | None:
        """GET /calib/reports?limit=N — история отчётов (Ф4b, fail-soft).

        Намеренное отклонение от контракта «ошибки пробрасываются»:
        история — вторичная панель, сетевой сбой/не-dict → None
        («нет отчётов»), карточки модели/GPU живут своим контрактом.
        """
        try:
            resp = await self._client.get(
                "/calib/reports", params={"limit": int(limit)}
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None


def _body_of(resp: httpx.Response) -> dict[str, Any]:
    """Тело ответа → dict (не-JSON → detail с текстом; fail-soft)."""
    try:
        data = resp.json()
    except ValueError:
        return {"detail": resp.text}
    return data if isinstance(data, dict) else {"detail": str(data)}


def _detail(body: dict[str, Any]) -> str:
    """Причина из error-тела (FastAPI ``detail``: str | список валидации)."""
    if not isinstance(body, dict):
        return str(body) or "нет деталей"
    detail = body.get("detail")
    if isinstance(detail, list):  # 422 pydantic-валидация
        msgs = [str(e.get("msg", e)) for e in detail if isinstance(e, dict)]
        return "; ".join(msgs) or "ошибка валидации"
    return str(detail) if detail else "нет деталей"


# ── Чистые хелперы (тестируемы без NiceGUI) ────────────────────


def probe_start_outcome(status_code: int, body: dict[str, Any]) -> dict[str, str]:
    """202/409/400 → нормализованный исход визарда (started/already_running/rejected)."""
    if status_code == 202:
        return {"outcome": "started", "message": "прогон запущен"}
    if status_code == 409:
        return {
            "outcome": "already_running",
            "message": f"probe уже выполняется (single-flight): {_detail(body)}",
        }
    return {
        "outcome": "rejected",
        "message": f"запуск отклонён ({status_code}): {_detail(body)}",
    }



def pair_start_outcome(status_code: int, body: dict[str, Any]) -> dict[str, str]:
    """202/409/400 → нормализованный исход визарда пары
    (started/already_running/rejected)."""
    if status_code == 202:
        return {"outcome": "started", "message": "пара запущена"}
    if status_code == 409:
        return {
            "outcome": "already_running",
            "message": f"пара уже выполняется (single-flight): {_detail(body)}",
        }
    return {
        "outcome": "rejected",
        "message": f"запуск пары отклонён ({status_code}): {_detail(body)}",
    }


def record_outcome(status_code: int, body: dict[str, Any]) -> dict[str, str]:
    """200/409/400/422 → (notify-тип, сообщение) записи P5 (fail-soft UI).

    422 — fail-closed отказ CLI-гейтов: запись НЕ выполнена, причина
    показывается оператору. 409 — нет завершённой пары.
    """
    if status_code == 200:
        status = body.get("status") if isinstance(body, dict) else None
        return {
            "outcome": "recorded",
            "notify": "positive",
            "message": f"Решение P5 записано: {status}",
        }
    if status_code == 409:
        return {
            "outcome": "no_pair",
            "notify": "warning",
            "message": f"нет завершённой пары: {_detail(body)}",
        }
    if status_code == 422:
        return {
            "outcome": "refused",
            "notify": "negative",
            "message": f"запись НЕ выполнена (fail-closed): {_detail(body)}",
        }
    if status_code == 400:
        return {
            "outcome": "gate",
            "notify": "negative",
            "message": f"запись отклонена (400): {_detail(body)}",
        }
    return {
        "outcome": "error",
        "notify": "negative",
        "message": f"ошибка записи ({status_code}): {_detail(body)}",
    }

def needle_evidence(report: dict[str, Any] | None) -> dict[str, Any]:
    """Needle-доказательство из отчёта probe (metrics-only).

    Возвращает ``{"available": bool, "rate": float | None, "ceiling": bool,
    "blocked": bool, "reason": str}``.

    ``blocked=True`` — fail-closed (F-2а): флаг ``ceiling`` (структурный скор
    насыщен) при needle_rate ниже пола NEEDLE_RATE_FLOOR или БЕЗ needle-замера
    («нечем решать») → применение запрещено в UI; сервер откажет 422
    независимо. Без ceiling низкий needle здесь НЕ блокирует (рамка D6
    прогона — отдельный гейт, паритет policy.propose_scalars).
    """
    report = report if isinstance(report, dict) else {}
    raw_rate = report.get("needle_rate")
    rate = (
        float(raw_rate)
        if isinstance(raw_rate, (int, float)) and not isinstance(raw_rate, bool)
        else None
    )
    flags = report.get("flags") if isinstance(report.get("flags"), (list, tuple)) else []
    ceiling = "ceiling" in flags
    if ceiling and (rate is None or rate < NEEDLE_RATE_FLOOR):
        why = (
            "needle-замера нет (нечем решать)"
            if rate is None
            else f"needle_rate={rate:.4f} < пола {NEEDLE_RATE_FLOOR}"
        )
        return {
            "available": rate is not None,
            "rate": rate,
            "ceiling": True,
            "blocked": True,
            "reason": f"ceiling (скор насыщен), а {why}",
        }
    return {
        "available": rate is not None,
        "rate": rate,
        "ceiling": ceiling,
        "blocked": False,
        "reason": "",
    }


def git_dirty_after_apply(applied: bool, output: str) -> bool:
    """Сигнал git-dirty после UI-записи (план §инварианты: approve меняет
    носители profiles/registry → репо станет dirty).

    ``applied=True`` (запись выполнена) ИЛИ маркер ``dirty`` в выводе CLI.
    """
    return bool(applied) or "dirty" in (output or "").lower()


def _num(value: Any, spec: str = ".4f") -> str:
    """Число для метрик или «—» (None/не-число — fail-soft)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return format(float(value), spec)


def _validate_probe_params(params: dict[str, Any]) -> list[str]:
    """Валидация визарда: обязательные поля, runs ≥ MIN_RUNS, без ведущего '-'.

    Зеркало отказов admin_api (400): значения с ведущим '-' не принимаются.
    """
    errors: list[str] = []
    if not params.get("model_class"):
        errors.append("заполните model_class")
    if not params.get("heldout"):
        errors.append("заполните heldout (путь)")
    for key in ("model_class", "heldout"):
        if str(params.get(key, "")).startswith("-"):
            errors.append(f"{key}: значение с ведущим '-' не принимается")
    runs = params.get("runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < MIN_RUNS:
        errors.append(f"runs ≥ {MIN_RUNS} (медиана при N≥3)")
    return errors


def _probe_payload(
    *,
    model_class: str,
    heldout: str,
    runs: int,
    live: bool,
    ext: bool,
    confirm_live: bool,
    confirm_ext: bool,
) -> dict[str, Any]:
    """Тело POST /calib/probe/start (зеркало ProbeStartBody admin_api.py).

    ``confirm_live``/``confirm_ext`` — флаги Operator Gate: шаг 1 / шаг 2
    визарда (ext-₽ — ОТДЕЛЬНОЕ подтверждение, строже CLI).
    """
    return {
        "model_class": model_class,
        "heldout": heldout,
        "runs": runs,
        "live": live,
        "ext": ext,
        "confirm_live": confirm_live,
        "confirm_ext": confirm_ext,
    }


def _approve_payload(profile_id: str, *, ceiling_ok: bool) -> dict[str, Any]:
    """Тело POST /calib/approve (зеркало ApproveBody admin_api.py).

    ``confirm=True`` — HITL-диалог пройден: применяем (dry-run из UI не
    нужен — превью показывает сам диалог). ``ceiling_ok`` — явное решение
    оператора по флагу ceiling (аналог --ceiling-ok).
    """
    return {
        "profile_id": profile_id,
        "confirm": True,
        "ceiling_ok": ceiling_ok,
        "reason": "kb-console /calibration (operator P5)",
    }


def _validate_pair_params(params: dict[str, Any]) -> list[str]:
    """Валидация визарда пары: обязательные поля, без ведущего '-'.

    Зеркало отказов admin_api (400): значения с ведущим '-' не принимаются.
    """
    errors: list[str] = []
    if not params.get("model_class"):
        errors.append("заполните model_class")
    if not params.get("heldout"):
        errors.append("заполните heldout (путь)")
    for key in ("base", "variant", "model_class", "heldout"):
        if str(params.get(key, "")).startswith("-"):
            errors.append(f"{key}: значение с ведущим '-' не принимается")
    return errors


def _pair_payload(
    *,
    base: str,
    variant: str,
    model_class: str,
    heldout: str,
    live: bool,
    confirm_live: bool,
) -> dict[str, Any]:
    """Тело POST /calib/pair/start (зеркало PairStartBody admin_api.py).

    ``runs``/``zone`` — канонические дефолты сервера (runs=MIN_RUNS —
    медиана при N≥3; zone=public); выбор зоны в UI не выставляется.
    ``confirm_live`` — Operator Gate шага 1 визарда (live-расход — шаг 2,
    строже CLI по аналогии с probe-визардом).
    """
    return {
        "base": base,
        "variant": variant,
        "model_class": model_class,
        "heldout": heldout,
        "runs": MIN_RUNS,
        "zone": PAIR_ZONE_DEFAULT,
        "live": live,
        "confirm_live": confirm_live,
    }


def _record_payload(variant: str, status: str) -> dict[str, Any]:
    """Тело POST /calib/record (зеркало RecordBody admin_api.py).

    ``confirm=True`` — HITL-диалог пройден (без него запрос не
    формируется). ``base``/``model_class`` не передаём: сервер берёт
    параметры последней пары и сверяет variant (CV7 — запись обязана
    ссылаться на её отчёты).
    """
    return {
        "variant": variant,
        "status": status,
        "confirm": True,
        "decided_by": "operator",
    }

# ── Рендер-хелперы (чистые, тестируемы на спарс-данных) ────────


def _render_model_card(
    model: dict[str, Any] | None,
    error: str | None,
    on_recalibrate: Any = None,
) -> None:
    """Карточка «Текущая модель»: полка (id/digest), активный профиль, drift T1–T3.

    Ф4b: при drift-акценте (``blocked``/``t1|t2|t3``) кнопка «Ре-калибровать»
    подсвечена негативно; клик предзаполняет визард probe классом активной
    модели — прогон НЕ запускается (Operator Gate в визарде).

    Форма — реальный ответ GET /calib/model (admin_api.calib_model):
    shelf{model_id,digest}|null · active{model_class,profile_id,profile_status,
    version,calibrated_for}|null · drift{status(ok|t1|t2|t3),blocked,reason,
    marks,live_probe(ok|warn|blocked)} · classes{...}. Спарс-данные — fail-soft.
    """
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Текущая модель").classes("text-h6 q-mb-sm")
        if error:
            ui.badge("admin-API недоступен").props("color=grey")
            ui.label(str(error)).classes("text-caption text-grey")
            return
        model = model if isinstance(model, dict) else {}

        shelf = model.get("shelf") if isinstance(model.get("shelf"), dict) else {}
        if shelf.get("model_id"):
            ui.label(f"Полка: {shelf.get('model_id')}").classes("text-body1")
            if shelf.get("digest"):
                ui.label(f"digest: {str(shelf['digest'])[:16]}…").classes(
                    "text-caption text-grey"
                )
        else:
            ui.label("Полка: факты недоступны").classes("text-grey text-caption")

        active = model.get("active") if isinstance(model.get("active"), dict) else {}
        if active.get("profile_id"):
            ui.label(
                f"Активный профиль: {active.get('profile_id')} "
                f"(класс {active.get('model_class', '?')}, статус "
                f"{active.get('profile_status', '?')}, версия {active.get('version', '?')})"
            ).classes("text-body2")
        else:
            ui.label("Активный профиль: нет").classes("text-grey text-caption")

        drift = model.get("drift") if isinstance(model.get("drift"), dict) else {}
        status = str(drift.get("status") or "?")
        drift_color = {"ok": "green", "t1": "red", "t2": "warning", "t3": "orange"}.get(
            status, "grey"
        )
        with ui.row().classes("items-center gap-1 q-mt-xs flex-wrap"):
            ui.badge(f"drift: {status}").props(f"color={drift_color}")
            if drift.get("blocked"):
                ui.badge("blocked (status-gate)").props("color=negative")
            hint = drift.get("live_probe")
            if hint == "blocked":
                ui.badge("live probe: запрещён").props("color=negative")
            elif hint == "warn":
                ui.badge("live probe: осторожно").props("color=warning")
            else:
                ui.badge("live probe: ок").props("color=green")
            marks = drift.get("marks") if isinstance(drift.get("marks"), list) else []
            for mark in marks:
                ui.chip(str(mark)).props("outline dense size=sm")
        if drift.get("reason"):
            ui.label(f"причина: {drift['reason']}").classes("text-caption text-grey")

        # Ф4b: акцент + «Ре-калибровать» (предзаполнение визарда, НЕ запуск)
        if on_recalibrate is not None:
            drift_accent = bool(drift.get("blocked")) or status in {"t1", "t2", "t3"}
            recal_btn = ui.button("♻ Ре-калибровать", on_click=on_recalibrate)
            recal_btn.props(
                "dense flat color=" + ("negative" if drift_accent else "grey")
            )
            recal_btn.tooltip(
                "Предзаполнить визард probe классом активной модели; запуск — "
                "Operator Gate в визарде (two-step confirm)"
            )

        classes = model.get("classes") if isinstance(model.get("classes"), dict) else {}
        if classes:
            with ui.row().classes("gap-1 flex-wrap q-mt-xs"):
                for name, spec in classes.items():
                    spec = spec if isinstance(spec, dict) else {}
                    ui.chip(
                        f"{name}: {spec.get('calibration_status', '?')}"
                    ).props("outline dense size=sm").tooltip(
                        f"rule={spec.get('rule', '?')}, "
                        f"active_profile={spec.get('active_profile', '—')}"
                    )


def _render_gpu_card(gpu: dict[str, Any] | None, error: str | None) -> None:
    """Карточка GPU/VRAM: ollama /api/ps + ws-lease GPU-слоты (оба fail-soft).

    Форма — реальный ответ GET /calib/gpu: ollama_ps{available, models[
    {name,digest,size_vram,expires_at}]} · gpu_slots{available, capacity,
    used, free, lease_ms, holdings}. Недоступность источника — серый бейдж,
    НЕ падение (наблюдение решает оператор до живого запуска).
    """
    with ui.card().classes("w-full q-mb-md"):
        ui.label("GPU / VRAM").classes("text-h6 q-mb-sm")
        if error:
            ui.badge("admin-API недоступен").props("color=grey")
            ui.label(str(error)).classes("text-caption text-grey")
            return
        gpu = gpu if isinstance(gpu, dict) else {}

        ollama = gpu.get("ollama_ps") if isinstance(gpu.get("ollama_ps"), dict) else {}
        if ollama.get("available"):
            models = ollama.get("models") if isinstance(ollama.get("models"), list) else []
            if models:
                with ui.row().classes("gap-1 flex-wrap"):
                    for m in models:
                        m = m if isinstance(m, dict) else {}
                        vram = human_size(m.get("size_vram"))
                        ui.chip(f"{m.get('name', '?')} · {vram}").props(
                            "outline dense size=sm"
                        )
            else:
                ui.label("ollama: моделей в VRAM нет").classes("text-caption text-grey")
        else:
            ui.badge("ollama /api/ps: недоступно").props("color=grey")

        slots = gpu.get("gpu_slots") if isinstance(gpu.get("gpu_slots"), dict) else {}
        if slots.get("available"):
            ui.label(
                f"GPU-слоты: занято {slots.get('used', '?')} из "
                f"{slots.get('capacity', '?')} (свободно {slots.get('free', '?')})"
            ).classes("text-body2")
            holdings = (
                slots.get("holdings") if isinstance(slots.get("holdings"), dict) else {}
            )
            for kind, refs in holdings.items():
                n = len(refs) if isinstance(refs, list) else refs
                ui.label(f"· {kind}: {n}").classes("text-caption text-grey")
        else:
            ui.badge("ws:lease:gpu: недоступно").props("color=grey")


def _render_report_metrics(report: dict[str, Any]) -> None:
    """Метрики итогового отчёта — строго metrics-only (приватность I5).

    Рендерятся ТОЛЬКО явные поля whitelist ответа admin-API
    (``_REPORT_FIELDS`` в admin_api.py: golden_median_score, needle_rate,
    rub, wall_s, heldout_score, parse_rate, golden_dispersion, n_runs,
    flags, run_id, model_id, digest). Прочие ключи словаря (тексты прогона)
    игнорируются — даже если окажутся в ответе, на экран не попадают.
    """
    report = report if isinstance(report, dict) else {}
    with ui.row().classes("gap-1 flex-wrap q-mt-sm"):
        ui.chip(f"score {_num(report.get('golden_median_score'))}").props(
            "outline dense size=sm"
        )
        ui.chip(f"needle_rate {_num(report.get('needle_rate'))}").props(
            "outline dense size=sm"
        )
        flags = report.get("flags") if isinstance(report.get("flags"), (list, tuple)) else []
        if "ceiling" in flags:
            ui.badge("ceiling").props("color=orange").tooltip(
                "структурный скор насыщен — решение по needle"
            )
        else:
            ui.chip("ceiling: нет").props("outline dense size=sm")
        ui.chip(f"₽ {_num(report.get('rub'), '.2f')}").props("outline dense size=sm")
        ui.chip(f"wall {_num(report.get('wall_s'), '.1f')} с").props(
            "outline dense size=sm"
        )
        ui.chip(f"heldout {_num(report.get('heldout_score'))}").props(
            "outline dense size=sm"
        )
        ui.chip(f"parse {_num(report.get('parse_rate'))}").props("outline dense size=sm")
        ui.chip(f"disp {_num(report.get('golden_dispersion'))}").props(
            "outline dense size=sm"
        )
        ui.chip(f"n_runs {report.get('n_runs', '—')}").props("outline dense size=sm")
    with ui.row().classes("gap-2"):
        if report.get("run_id"):
            ui.label(f"run_id: {report['run_id']}").classes("text-caption text-grey")
        if report.get("model_id"):
            ui.label(f"model: {report['model_id']}").classes("text-caption text-grey")
        if report.get("digest"):
            ui.label(f"digest: {str(report['digest'])[:16]}…").classes(
                "text-caption text-grey"
            )


def _render_probe_panel(state: dict[str, Any], client: CalibClient | None) -> None:
    """Панель статуса/прогресса/метрик + ряд approve.

    Перерисовывается поллингом (@ui.refreshable). Approve-кнопка disabled,
    пока статус ≠ done или нет отчёта, И заблокирована при fail-closed
    (ceiling без needle-доказательства). Ввод profile_id живёт в state —
    значение переживает перерисовку.
    """
    probe = state.get("probe") if isinstance(state.get("probe"), dict) else {}
    status_value = str(probe.get("status") or "idle")
    report = probe.get("report") if isinstance(probe.get("report"), dict) else None
    evidence = needle_evidence(report)

    with ui.card().classes("w-full q-mb-md"):
        ui.label("Прогон · метрики · approve").classes("text-h6 q-mb-sm")

        badge_color = {"idle": "grey", "running": "blue", "done": "green", "failed": "red"}
        ui.badge(f"probe: {status_value}").props(
            f"color={badge_color.get(status_value, 'grey')}"
        )
        if status_value == "running":
            ui.spinner("dots", size="sm")

        if probe.get("run_id"):
            ui.label(f"run_id: {probe['run_id']}").classes("text-caption text-grey")
        if probe.get("started_at"):
            ui.label(f"старт: {str(probe['started_at'])[:19]}").classes(
                "text-caption text-grey"
            )
        if probe.get("finished_at"):
            ui.label(f"финиш: {str(probe['finished_at'])[:19]}").classes(
                "text-caption text-grey"
            )
        progress = probe.get("progress") if isinstance(probe.get("progress"), dict) else None
        if progress is not None:
            if "segments_done" in progress:
                ui.label(f"segments done: {progress.get('segments_done')}").classes(
                    "text-caption text-grey"
                )
            if progress.get("partial_error"):
                ui.label(
                    f"partial битый: {progress['partial_error']}"
                ).classes("text-caption text-orange")
        if probe.get("exit_code") is not None:
            ui.label(f"exit: {probe.get('exit_code')}").classes("text-caption text-grey")
        if probe.get("error"):
            ui.label(f"ошибка: {probe['error']}").classes("text-negative text-caption")

        if report is not None:
            _render_report_metrics(report)
        elif status_value in ("idle", "running"):
            ui.label(
                "Метрики появятся по завершении прогона (metrics-only, без текстов)."
            ).classes("text-caption text-grey")

        # needle-доказательство + fail-closed предупреждение (F-2а)
        if evidence["available"]:
            ok = (evidence["rate"] or 0.0) >= NEEDLE_RATE_FLOOR
            sign = "≥" if ok else "<"
            ui.label(
                f"Needle-доказательство: needle_rate={_num(evidence['rate'])} "
                f"{sign} пола {NEEDLE_RATE_FLOOR}"
            ).classes("text-body2 " + ("text-positive" if ok else "text-negative"))
        if evidence["blocked"]:
            ui.label(
                f"⛔ fail-closed: применение запрещено — {evidence['reason']}"
            ).classes("text-negative text-body2")

        # git-dirty бейдж после UI-записи (план §инварианты)
        approve_result = (
            state.get("approve_result")
            if isinstance(state.get("approve_result"), dict)
            else None
        )
        if approve_result is not None and approve_result.get("dirty"):
            ui.badge("⚠ git-dirty").props("color=orange")
            ui.label(
                f"Носители калибровки изменены — зафиксируйте вручную "
                f"(runbook: {RUNBOOK_PATH})"
            ).classes("text-caption text-orange")

        ui.separator()

        ui.label("Approve профиля (P5 — оператор)").classes("text-subtitle1 q-mb-xs")
        can_approve = status_value == "done" and report is not None and not evidence["blocked"]
        ui.input(
            "profile_id",
            value=state.get("profile_id", ""),
            on_change=lambda e: state.__setitem__("profile_id", e.value),
        ).props("dense").classes("w-96").tooltip(
            "draft-профиль прогона (см. логи probe / runbook)"
        )

        async def _on_approve() -> None:
            if client is not None:
                await _confirm_approve(client, state)

        approve_btn = ui.button("✅ Применить (approve)", on_click=_on_approve)
        approve_btn.props("flat color=positive")
        if can_approve:
            approve_btn.tooltip("HITL-подтверждение → POST /calib/approve")
        else:
            approve_btn.disable()
            approve_btn.tooltip(
                "прогон не завершён (метрики появятся по done)"
                if status_value != "done"
                else "применение запрещено (fail-closed: ceiling без needle)"
            )


def _render_pair_arm(title: str, arm: dict[str, Any] | None) -> None:
    """Метрики одного плеча пары — metrics-only (whitelist _REPORT_FIELDS).

    Плечо = рука прогона (base/variant); рендер через общий
    ``_render_report_metrics`` — только whitelist-поля, тексты прогона
    не отображаются никогда (приватность I5).
    """
    arm = arm if isinstance(arm, dict) else None
    with ui.row().classes("items-center gap-2 q-mt-xs"):
        ui.label(title).classes("text-subtitle2")
        if arm is None:
            ui.label("нет отчёта").classes("text-caption text-grey")
    if arm is not None:
        _render_report_metrics(arm)


def _render_pair_panel(state: dict[str, Any], client: CalibClient | None) -> None:
    """Панель пары base↔variant: статус · метрики плеч · reasons · ряд P5.

    Перерисовывается поллингом (@ui.refreshable). Кнопки записи P5
    активны ТОЛЬКО при status=done (409 «нет завершённой пары» — гейт
    сервера; UI не слабее CLI). ``reasons`` — бумажка §7.3 (подсказка
    оператору), НЕ решение: P5 всегда за оператором.
    """
    pair = state.get("pair") if isinstance(state.get("pair"), dict) else {}
    status_value = str(pair.get("status") or "idle")

    with ui.card().classes("w-full q-mb-md"):
        ui.label("Пара base↔variant · метрики · решение P5").classes(
            "text-h6 q-mb-sm"
        )
        badge_color = {"idle": "grey", "running": "blue", "done": "green", "failed": "red"}
        ui.badge(f"pair: {status_value}").props(
            f"color={badge_color.get(status_value, 'grey')}"
        )
        if status_value == "running":
            ui.spinner("dots", size="sm")
        if pair.get("run_id"):
            ui.label(f"run_id: {pair['run_id']}").classes("text-caption text-grey")
        if pair.get("started_at"):
            ui.label(f"старт: {str(pair['started_at'])[:19]}").classes(
                "text-caption text-grey"
            )
        if pair.get("finished_at"):
            ui.label(f"финиш: {str(pair['finished_at'])[:19]}").classes(
                "text-caption text-grey"
            )
        if pair.get("exit_code") is not None:
            ui.label(f"exit: {pair.get('exit_code')}").classes("text-caption text-grey")
        if pair.get("error"):
            ui.label(f"ошибка: {pair['error']}").classes("text-negative text-caption")
        progress = pair.get("progress") if isinstance(pair.get("progress"), dict) else None
        if progress is not None:
            if "segments_done" in progress:
                ui.label(f"segments done: {progress.get('segments_done')}").classes(
                    "text-caption text-grey"
                )
            if progress.get("partial_error"):
                ui.label(
                    f"partial битый: {progress['partial_error']}"
                ).classes("text-caption text-orange")

        _render_pair_arm("base", pair.get("base"))
        _render_pair_arm("variant", pair.get("variant"))
        if pair.get("base") is None and pair.get("variant") is None:
            ui.label(
                "Метрики плеч появятся по завершении пары "
                "(metrics-only, без текстов)."
            ).classes("text-caption text-grey")

        passed = pair.get("passed")
        if isinstance(passed, bool):
            if passed:
                ui.badge("критерий §7.3: passed").props("color=green")
            else:
                ui.badge("критерий §7.3: not passed").props("color=orange")
        reasons = pair.get("reasons") if isinstance(pair.get("reasons"), list) else []
        if reasons:
            with ui.row().classes("gap-1 flex-wrap q-mt-xs"):
                for reason in reasons:
                    ui.chip(str(reason)).props("outline dense size=sm")

        record_result = (
            state.get("record_result")
            if isinstance(state.get("record_result"), dict)
            else None
        )
        if record_result is not None:
            if record_result.get("code") == 200:
                ui.badge(f"записано: {record_result.get('status')}").props(
                    "color=green"
                )
            else:
                body = (
                    record_result.get("body")
                    if isinstance(record_result.get("body"), dict)
                    else {}
                )
                ui.label(
                    f"запись НЕ выполнена ({record_result.get('code')}): "
                    f"{_detail(body)}"
                ).classes("text-negative text-caption")

        ui.separator()

        ui.label("Решение P5 (promoted/rejected) — всегда за оператором").classes(
            "text-subtitle1 q-mb-xs"
        )
        ui.input(
            "variant (mode-YAML записи)",
            value=state.get("record_variant", ""),
            on_change=lambda e: state.__setitem__("record_variant", e.value),
        ).props("dense").classes("w-64").tooltip(
            "должен совпадать с variant прогнанной пары (CV7)"
        )
        can_record = status_value == "done"

        async def _on_promote() -> None:
            if client is not None:
                await _confirm_record(client, state, "promoted")

        async def _on_reject() -> None:
            if client is not None:
                await _confirm_record(client, state, "rejected")

        with ui.row().classes("gap-2"):
            promote_btn = ui.button("⬆ promoted (P5)", on_click=_on_promote)
            promote_btn.props("flat color=positive")
            reject_btn = ui.button("⬇ rejected (P5)", on_click=_on_reject)
            reject_btn.props("flat color=negative")
            if can_record:
                promote_btn.tooltip("HITL-подтверждение → POST /calib/record")
                reject_btn.tooltip("HITL-подтверждение → POST /calib/record")
            else:
                promote_btn.disable()
                reject_btn.disable()
                promote_btn.tooltip("пара не завершена — запись только по done")
                reject_btn.tooltip("пара не завершена — запись только по done")


# ── Ф4b: «ре-калибровать» + история отчётов (module-level — тестируемо) ──


def recalibrate_prefill(model: dict[str, Any] | None) -> dict[str, str] | None:
    """Предзаполнение визарда probe из /calib/model (Ф4b).

    Класс АКТИВНОГО профиля → ``{"model_class": ...}``; нет активного
    профиля/класса → None (предзаполнять нечем). Прогон НЕ запускается —
    Operator Gate (two-step confirm) остаётся за оператором.
    """
    model = model if isinstance(model, dict) else {}
    active = model.get("active") if isinstance(model.get("active"), dict) else {}
    cls = str(active.get("model_class") or "").strip()
    return {"model_class": cls} if cls else None


def _on_recalibrate_click(model: dict[str, Any] | None, class_input: Any) -> None:
    """Клик «Ре-калибровать»: предзаполнить визард, НЕ запускать прогон."""
    prefill = recalibrate_prefill(model)
    if prefill is None or class_input is None:
        ui.notify(
            "Нет активного класса модели — заполните визард вручную",
            type="warning",
        )
        return
    class_input.set_value(prefill["model_class"])
    ui.notify(
        f"Визард probe предзаполнен: model_class={prefill['model_class']}. "
        "Запуск — за оператором (Operator Gate)",
        type="info",
    )


#: Колонки истории (Ф4b): whitelist ``_REPORT_FIELDS`` admin_api + ``ts``;
#: только эти поля рендерятся — тексты прогона не едут в историю (I5)
REPORTS_TABLE_FIELDS: tuple[str, ...] = (
    "ts", "run_id", "model_id", "golden_median_score", "heldout_score",
    "needle_rate", "flags", "rub", "wall_s", "n_runs",
)

#: Формат чисел по колонкам истории (прочие — строка/«—»)
_REPORTS_NUM_SPEC: dict[str, str] = {
    "golden_median_score": ".4f",
    "heldout_score": ".4f",
    "needle_rate": ".4f",
    "rub": ".2f",
    "wall_s": ".1f",
}

#: Варианты limit истории («Обновить» перезабирает с выбранным)
REPORTS_LIMIT_CHOICES: dict[int, str] = {10: "10", 20: "20", 50: "50"}

#: limit по умолчанию (паритет default admin_api.calib_reports)
REPORTS_LIMIT_DEFAULT: int = 20


def _report_row(report: dict[str, Any]) -> dict[str, Any]:
    """Строка таблицы истории — ТОЛЬКО колонки REPORTS_TABLE_FIELDS.

    Числа — по _REPORTS_NUM_SPEC, flags — через запятую, ts — до секунд;
    прочие ключи отчёта (тексты прогона) отбрасываются (metrics-only, I5).
    """
    row: dict[str, Any] = {}
    for field in REPORTS_TABLE_FIELDS:
        value = report.get(field)
        if field in _REPORTS_NUM_SPEC:
            row[field] = _num(value, _REPORTS_NUM_SPEC[field])
        elif field == "flags":
            row[field] = (
                ",".join(str(f) for f in value)
                if isinstance(value, (list, tuple)) and value
                else "—"
            )
        elif field == "ts":
            row[field] = str(value)[:19] if value else "—"
        elif field == "n_runs":
            row[field] = (
                value
                if isinstance(value, int) and not isinstance(value, bool)
                else "—"
            )
        else:
            row[field] = str(value) if value is not None else "—"
    return row


def _render_reports_panel(data: dict[str, Any] | None) -> None:
    """История калибровок — таблица метрик (Ф4b, metrics-only).

    Форма — реальный ответ GET /calib/reports (admin_api.calib_reports):
    ``{"reports": [{..._REPORT_FIELDS..., "ts": ISO-mtime}], "total": N}``
    (mtime DESC, limit clamp 1..200). Рендерятся ТОЛЬКО поля
    REPORTS_TABLE_FIELDS; любые прочие ключи (тексты прогона) на экран
    не попадают (приватность I5). None/пусто → «нет отчётов» (fail-soft).
    """
    data = data if isinstance(data, dict) else {}
    reports = data.get("reports") if isinstance(data.get("reports"), list) else []
    if not reports:
        ui.label("нет отчётов").classes("text-caption text-grey")
        return
    rows = [_report_row(r) for r in reports if isinstance(r, dict)]
    columns = [
        {"name": f, "label": f, "field": f, "align": "left"}
        for f in REPORTS_TABLE_FIELDS
    ]
    ui.table(columns=columns, rows=rows, row_key="run_id").classes("w-full")
    total = data.get("total")
    if isinstance(total, int):
        ui.label(f"всего отчётов: {total}").classes("text-caption text-grey")


# ── Действия (module-level — тестируемо) ───────────────────────


async def _confirm_probe_launch(params: dict[str, Any]) -> bool:
    """Визард two-step HITL: (1) confirm-live, (2) ОТДЕЛЬНО — ext-₽.

    Шаг 2 показывается ТОЛЬКО при ext-запросе; True — оба необходимых
    подтверждения получены (UI строго НЕ слабее CLI: ext гейтится отдельно).
    Bare await, non-persistent (паттерн quality.py).
    """
    ext_requested = bool(params.get("ext"))
    step1: dict[str, bool] = {"confirmed": False}
    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("Шаг 1 из 2 · Подтверждение живого прогона (confirm-live)").classes(
            "text-h6"
        )
        ui.label(
            f"класс: {params.get('model_class')} · heldout: {params.get('heldout')} · "
            f"runs: {params.get('runs')}\n"
            f"live: {'да' if params.get('live') else 'нет'} · "
            f"ext-полка: {'ДА (₽)' if ext_requested else 'нет'}"
        ).classes("text-body2 q-mt-sm")
        ui.label(
            "Живой прогон будет запущен — Operator Gate (аналог --confirm-live)."
        ).classes("text-caption text-grey q-mt-sm q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            def _confirm_step1(dlg=dialog) -> None:
                step1["confirmed"] = True
                dlg.close()

            ui.button("✅ Подтверждаю живой прогон", on_click=_confirm_step1).props(
                "flat color=warning"
            )
    await dialog
    if not step1["confirmed"]:
        return False
    if not ext_requested:
        return True

    # шаг 2 — ОТДЕЛЬНЫЙ диалог: расход внешнего бюджета (₽), строже CLI
    step2: dict[str, bool] = {"confirmed": False}
    with ui.dialog() as dialog2, ui.card().classes("q-pa-md"):
        ui.label("Шаг 2 из 2 · Внешний бюджет (ext-₽)").classes("text-h6")
        ui.label(
            "ext-полка расходует РЕАЛЬНЫЕ ₽ (внешний API). Отдельное "
            "подтверждение confirm_ext — строже CLI."
        ).classes("text-body2 q-mt-sm q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog2.close).props("flat")

            def _confirm_step2(dlg=dialog2) -> None:
                step2["confirmed"] = True
                dlg.close()

            ui.button("₽ Подтверждаю расход внешнего бюджета", on_click=_confirm_step2).props(
                "flat color=negative"
            )
    await dialog2
    return step2["confirmed"]


async def _start_probe(
    client: CalibClient, payload: dict[str, Any], state: dict[str, Any]
) -> None:
    """POST /calib/probe/start + разбор 202/409/400 (панель обновит поллинг)."""
    try:
        code, body = await client.probe_start(payload)
    except Exception as exc:
        ui.notify(f"Сетевая ошибка запуска probe: {exc}", type="negative")
        return
    outcome = probe_start_outcome(code, body)
    if outcome["outcome"] == "started":
        ui.notify("Probe запущен — прогресс ниже", type="positive")
    elif outcome["outcome"] == "already_running":
        ui.notify(f"{outcome['message']} — текущий прогон показан ниже", type="warning")
    else:
        ui.notify(outcome["message"], type="negative")
    try:
        state["probe"] = await client.probe_status()
    except Exception:
        pass  # поллинг-таймер подхватит статус следующим тиком (fail-soft)


async def _confirm_approve(client: CalibClient, state: dict[str, Any]) -> None:
    """Approve: HITL-диалог (needle-доказательство + ceiling_ok) → POST.

    200 → notify + git-dirty сигнал в state (бейдж на панели); 422 →
    причина fail-closed отказа; прочее → ошибка. P5 остаётся за оператором.
    """
    profile_id = (state.get("profile_id") or "").strip()
    if not profile_id:
        ui.notify("Введите profile_id", type="warning")
        return
    probe = state.get("probe") if isinstance(state.get("probe"), dict) else {}
    report = probe.get("report") if isinstance(probe.get("report"), dict) else None
    evidence = needle_evidence(report)
    if evidence["blocked"]:
        ui.notify(f"Применение запрещено (fail-closed): {evidence['reason']}", type="negative")
        return

    ceiling = evidence["ceiling"]
    confirmed: dict[str, Any] = {"ok": False, "ceiling_ok": False}
    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("✅ Approve профиля").classes("text-h6")
        ui.label(f"Профиль: {profile_id}").classes("text-body2 q-mt-sm")
        if evidence["available"]:
            ui.label(
                f"Needle-доказательство: needle_rate={_num(evidence['rate'])} "
                f"≥ пола {NEEDLE_RATE_FLOOR}"
            ).classes("text-body2 text-positive")
        else:
            ui.label("Needle-набор не прогонялся (needle_rate в отчёте нет)").classes(
                "text-caption text-grey"
            )
        if ceiling:
            ui.label(
                "Флаг ceiling: структурный скор насыщен — решение по needle."
            ).classes("text-caption text-orange")
            ui.checkbox(
                "ceiling_ok — подтверждаю применение по needle-доказательству",
                on_change=lambda e: confirmed.__setitem__("ceiling_ok", bool(e.value)),
            ).props("dense")
        ui.label(
            "Профиль будет применён (реестр + профиль; решение P5 — ваше)."
        ).classes("text-caption text-grey q-mt-sm q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            def _confirm(dlg=dialog) -> None:
                confirmed["ok"] = True
                dlg.close()

            ui.button("✅ Применить", on_click=_confirm).props("flat color=positive")
    await dialog
    if not confirmed["ok"]:
        return
    if ceiling and not confirmed["ceiling_ok"]:
        ui.notify("ceiling: требуется явное подтверждение ceiling_ok", type="warning")
        return

    try:
        code, body = await client.approve(_approve_payload(profile_id, ceiling_ok=confirmed["ceiling_ok"]))
    except Exception as exc:
        ui.notify(f"Сетевая ошибка approve: {exc}", type="negative")
        return
    applied = code == 200 and bool(body.get("applied"))
    if code == 200:
        ui.notify("Профиль применён" if applied else "Готово (не применён)", type="positive")
    elif code == 422:
        ui.notify(f"Fail-closed отказ: {_detail(body)}", type="negative")
    else:
        ui.notify(f"Ошибка approve ({code}): {_detail(body)}", type="negative")
    state["approve_result"] = {
        "code": code,
        "body": body,
        "dirty": git_dirty_after_apply(applied, str(body.get("output", ""))),
    }


async def _confirm_pair_launch(params: dict[str, Any]) -> bool:
    """Визард two-step HITL пары: (1) confirm-live, (2) ОТДЕЛЬНО — live-расход.

    Шаг 2 показывается ТОЛЬКО при live-запросе (реальный инференс ОБОИХ
    плеч пары); True — все необходимые подтверждения получены. Bare await,
    non-persistent (паттерн _confirm_probe_launch).
    """
    live_requested = bool(params.get("live"))
    step1: dict[str, bool] = {"confirmed": False}
    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label(
            "Шаг 1 из 2 · Подтверждение пары base↔variant (confirm-live)"
        ).classes("text-h6")
        ui.label(
            f"base: {params.get('base')}\nvariant: {params.get('variant')}\n"
            f"класс: {params.get('model_class')} · heldout: {params.get('heldout')}\n"
            f"live: {'да' if live_requested else 'нет'}"
        ).classes("text-body2 q-mt-sm")
        ui.label(
            "Пара будет запущена — Operator Gate (аналог --confirm-live)."
        ).classes("text-caption text-grey q-mt-sm q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            def _confirm_step1(dlg=dialog) -> None:
                step1["confirmed"] = True
                dlg.close()

            ui.button("✅ Подтверждаю запуск пары", on_click=_confirm_step1).props(
                "flat color=warning"
            )
    await dialog
    if not step1["confirmed"]:
        return False
    if not live_requested:
        return True

    # шаг 2 — ОТДЕЛЬНЫЙ диалог: live-расход обоих плеч (строже CLI)
    step2: dict[str, bool] = {"confirmed": False}
    with ui.dialog() as dialog2, ui.card().classes("q-pa-md"):
        ui.label("Шаг 2 из 2 · Живой расход (live)").classes("text-h6")
        ui.label(
            "live-прогон гоняет РЕАЛЬНЫЙ инференс на ОБОИХ плечах пары "
            "(base и variant) — отдельное подтверждение расхода."
        ).classes("text-body2 q-mt-sm q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog2.close).props("flat")

            def _confirm_step2(dlg=dialog2) -> None:
                step2["confirmed"] = True
                dlg.close()

            ui.button(
                "🔥 Подтверждаю live-расход обоих плеч", on_click=_confirm_step2
            ).props("flat color=negative")
    await dialog2
    return step2["confirmed"]


async def _start_pair(
    client: CalibClient, payload: dict[str, Any], state: dict[str, Any]
) -> None:
    """POST /calib/pair/start + разбор 202/409/400 (панель обновит поллинг)."""
    try:
        code, body = await client.pair_start(payload)
    except Exception as exc:
        ui.notify(f"Сетевая ошибка запуска пары: {exc}", type="negative")
        return
    outcome = pair_start_outcome(code, body)
    if outcome["outcome"] == "started":
        ui.notify("Пара запущена — прогресс ниже", type="positive")
    elif outcome["outcome"] == "already_running":
        ui.notify(f"{outcome['message']} — текущая пара показана ниже", type="warning")
    else:
        ui.notify(outcome["message"], type="negative")
    try:
        state["pair"] = await client.pair_status()
    except Exception:
        pass  # поллинг-таймер подхватит статус следующим тиком (fail-soft)


async def _confirm_record(
    client: CalibClient, state: dict[str, Any], decision: str
) -> None:
    """Запись P5: HITL-диалог → POST /calib/record с confirm=true.

    200 → positive + бейдж на панели; 422 → причина fail-closed отказа
    (запись НЕ выполнена); 409 → нет завершённой пары; 400 → гейт.
    Решение P5 — оператора: API только исполняет подтверждённое.
    """
    variant = (state.get("record_variant") or "").strip()
    if not variant:
        ui.notify("Укажите variant (mode-YAML) для записи", type="warning")
        return
    pair = state.get("pair") if isinstance(state.get("pair"), dict) else {}
    confirmed: dict[str, bool] = {"ok": False}
    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label(f"⚖ Решение P5: {decision}").classes("text-h6")
        ui.label(f"variant: {variant}").classes("text-body2 q-mt-sm")
        passed = pair.get("passed")
        if isinstance(passed, bool):
            ui.label(
                f"Критерий §7.3: {'passed' if passed else 'not passed'} "
                "(подсказка, НЕ решение)"
            ).classes("text-caption text-grey")
        reasons = pair.get("reasons") if isinstance(pair.get("reasons"), list) else []
        for reason in reasons[:5]:
            ui.label(f"· {reason}").classes("text-caption text-grey")
        ui.label(
            "Решение и ответственность — ваши; API только исполняет "
            "(confirm=true)."
        ).classes("text-caption text-grey q-mt-sm q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            def _confirm(dlg=dialog) -> None:
                confirmed["ok"] = True
                dlg.close()

            ui.button(f"✅ Записать {decision}", on_click=_confirm).props(
                "flat color=warning"
            )
    await dialog
    if not confirmed["ok"]:
        return
    try:
        code, body = await client.record(_record_payload(variant, decision))
    except Exception as exc:
        ui.notify(f"Сетевая ошибка записи: {exc}", type="negative")
        return
    outcome = record_outcome(code, body)
    ui.notify(outcome["message"], type=outcome["notify"])
    state["record_result"] = {"code": code, "body": body, "status": decision}


# ── Сборка страницы ─────────────────────────────────────────────


def build_calibration() -> None:
    """Построить admin-страницу «Калибровка» (Ф2).

    Admin-only: runtime-гейт is_admin() (навигация уже скрыта min_role="admin"
    в ROUTES — паттерн documents.py).
    """
    if not is_admin():
        ui.label("⛔ 403: калибровка доступна только администраторам.").classes(
            "text-h6 text-negative"
        )
        ui.label("Обратитесь к администратору консоли.").classes("text-body1 text-grey")
        return

    ui.label("Калибровка системы под текущую модель").classes("text-h4 q-mb-md")
    ui.label(f"Runbook: {RUNBOOK_PATH}").classes("text-caption text-grey q-mb-md").tooltip(
        "Операционный гайд (путь в репозитории mcp-knowledge)"
    )

    state: dict[str, Any] = {
        "model": None,
        "model_error": None,
        "gpu": None,
        "gpu_error": None,
        "probe": {},
        "profile_id": "",
        "approve_result": None,
        "pair": {},
        "record_variant": "",
        "record_result": None,
        "reports": None,
        "reports_limit": REPORTS_LIMIT_DEFAULT,
    }
    client = CalibClient()

    # Ф4b: holder ссылок на поля визарда (class_input создаётся ниже;
    # клик «Ре-калибровать» резолвит input в момент клика, не сборки)
    wizard_refs: dict[str, Any] = {}

    def _on_recalibrate() -> None:
        _on_recalibrate_click(state.get("model"), wizard_refs.get("class_input"))

    @ui.refreshable
    def render_model_card() -> None:
        _render_model_card(
            state.get("model"),
            state.get("model_error"),
            on_recalibrate=_on_recalibrate,
        )

    @ui.refreshable
    def render_gpu_card() -> None:
        _render_gpu_card(state.get("gpu"), state.get("gpu_error"))

    @ui.refreshable
    def render_probe_panel() -> None:
        _render_probe_panel(state, client)

    @ui.refreshable
    def render_pair_panel() -> None:
        _render_pair_panel(state, client)

    async def refresh_facts() -> None:
        """Карточки модели/GPU: fail-soft — сетевая ошибка не роняет страницу."""
        try:
            state["model"] = await client.model()
            state["model_error"] = None
        except Exception as exc:
            state["model"] = None
            state["model_error"] = f"{type(exc).__name__}: {exc}"
        try:
            state["gpu"] = await client.gpu()
            state["gpu_error"] = None
        except Exception as exc:
            state["gpu"] = None
            state["gpu_error"] = f"{type(exc).__name__}: {exc}"
        render_model_card.refresh()
        render_gpu_card.refresh()

    async def poll_probe() -> None:
        """Поллинг статуса probe (ui.timer): сетевой сбой — тихий пропуск тика."""
        try:
            state["probe"] = await client.probe_status()
        except Exception:
            return
        render_probe_panel.refresh()

    async def poll_pair() -> None:
        """Поллинг статуса пары (общий таймер): сбой — тихий пропуск тика."""
        try:
            state["pair"] = await client.pair_status()
        except Exception:
            return
        render_pair_panel.refresh()

    async def poll_both() -> None:
        """Один таймер на обе секции (probe + pair) — cleanup тоже один."""
        await poll_probe()
        await poll_pair()

    # ── Карточки фактов ──
    render_model_card()
    render_gpu_card()

    # ── Визард probe (two-step HITL: confirm-live → ext-₽ отдельно) ──
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Прогон probe (двухшаговое подтверждение)").classes("text-h6 q-mb-sm")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            class_input = (
                ui.input("model_class", placeholder="например fast")
                .props("dense")
                .classes("w-40")
            )
            wizard_refs["class_input"] = class_input
            heldout_input = (
                ui.input("heldout (путь)", placeholder="/path/to/heldout")
                .props("dense")
                .classes("w-64")
            )
            runs_input = (
                ui.number("runs", value=float(MIN_RUNS), min=MIN_RUNS, step=1)
                .props("dense")
                .classes("w-24")
            )
            live_check = ui.checkbox("live").props("dense")
            ext_check = ui.checkbox("ext (внешние ₽)").props("dense")

        async def _on_probe_click() -> None:
            params: dict[str, Any] = {
                "model_class": (class_input.value or "").strip(),
                "heldout": (heldout_input.value or "").strip(),
                "runs": int(runs_input.value or MIN_RUNS),
                "live": bool(live_check.value),
                "ext": bool(ext_check.value),
            }
            errors = _validate_probe_params(params)
            if errors:
                ui.notify("; ".join(errors), type="warning")
                return
            confirmed = await _confirm_probe_launch(params)
            if not confirmed:
                ui.notify("Запуск не подтверждён (гейт отменён)", type="info")
                return
            await _start_probe(
                client,
                _probe_payload(
                    **params, confirm_live=True, confirm_ext=params["ext"]
                ),
                state,
            )

        ui.button("🚀 Запустить probe", on_click=_on_probe_click).props(
            "flat color=warning"
        )

    # ── Статус/метрики/approve (поллинг) ──
    render_probe_panel()

    # ── Визард пары base↔variant (two-step HITL) ──
    with ui.card().classes("w-full q-mb-md"):
        ui.label("Пара base↔variant (двухшаговое подтверждение)").classes(
            "text-h6 q-mb-sm"
        )
        with ui.row().classes("items-center gap-2 flex-wrap"):
            pair_base_input = (
                ui.input("base (mode-YAML)", value=PAIR_BASE_DEFAULT)
                .props("dense")
                .classes("w-64")
            )
            pair_variant_input = (
                ui.input("variant (mode-YAML)", value=PAIR_VARIANT_DEFAULT)
                .props("dense")
                .classes("w-64")
            )
            pair_class_input = (
                ui.input("model_class", placeholder="например fast")
                .props("dense")
                .classes("w-40")
            )
            pair_heldout_input = (
                ui.input("heldout (путь)", placeholder="/path/to/heldout")
                .props("dense")
                .classes("w-64")
            )
            pair_live_check = ui.checkbox("live").props("dense")

        async def _on_pair_click() -> None:
            params: dict[str, Any] = {
                "base": (pair_base_input.value or "").strip() or PAIR_BASE_DEFAULT,
                "variant": (pair_variant_input.value or "").strip()
                or PAIR_VARIANT_DEFAULT,
                "model_class": (pair_class_input.value or "").strip(),
                "heldout": (pair_heldout_input.value or "").strip(),
                "live": bool(pair_live_check.value),
            }
            errors = _validate_pair_params(params)
            if errors:
                ui.notify("; ".join(errors), type="warning")
                return
            confirmed = await _confirm_pair_launch(params)
            if not confirmed:
                ui.notify("Запуск пары не подтверждён (гейт отменён)", type="info")
                return
            state["record_variant"] = params["variant"]
            await _start_pair(
                client, _pair_payload(**params, confirm_live=True), state
            )

        ui.button("🚀 Запустить пару", on_click=_on_pair_click).props(
            "flat color=warning"
        )

    # ── Статус/метрики пары + решение P5 (поллинг) ──
    render_pair_panel()

    # ── История калибровок (Ф4b: /calib/reports, metrics-only) ──
    @ui.refreshable
    def render_reports_panel() -> None:
        _render_reports_panel(state.get("reports"))

    async def refresh_reports() -> None:
        """История: fail-soft — сбой запроса не роняет страницу."""
        try:
            state["reports"] = await client.reports(
                limit=int(state.get("reports_limit") or REPORTS_LIMIT_DEFAULT)
            )
        except Exception:
            state["reports"] = None
        render_reports_panel.refresh()

    with ui.card().classes("w-full q-mb-md"):
        with ui.row().classes("items-center gap-2 q-mb-sm"):
            ui.label("История калибровок").classes("text-h6")
            limit_select = (
                ui.select(REPORTS_LIMIT_CHOICES, value=REPORTS_LIMIT_DEFAULT)
                .props("dense")
                .classes("w-20")
            )
            ui.button("Обновить", on_click=refresh_reports).props("flat")

        async def _on_limit_change(e: Any) -> None:
            state["reports_limit"] = int(e.value)
            await refresh_reports()

        limit_select.on_value_change(_on_limit_change)
        render_reports_panel()

    poll_timer = ui.timer(PROBE_POLL_INTERVAL, poll_both)

    async def _bootstrap() -> None:
        await refresh_facts()
        await poll_both()
        await refresh_reports()

    ui.timer(0.1, _bootstrap, once=True)

    def _cleanup() -> None:
        poll_timer.cancel()
        asyncio.create_task(client.close())

    ui.context.client.on_disconnect(_cleanup)
