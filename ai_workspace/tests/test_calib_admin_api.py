"""Ф1a-тесты admin-API калибровки (arch-2026-10-09-calib-admin-ui).

Без живого LLM: probe-runner/approve-runner инъектируются (single-flight,
гейты), approve идёт по РЕАЛЬНОМУ ``profile_approve.main`` на tmp-носителях
(реальный контракт fail-closed), полный chain — реальный ``probe_run.main``
на контурном стабе (без ``--live``), модель/GPU — фейки ``http_get``/``gpu_status``.

Чек-лист задачи: auth fail-closed (+``compare_digest`` по источнику);
параллельный ``probe/start`` → 202+409 (single-flight); approve fail-closed
(ceiling без needle; неполный ``calibrated_for``); контракт ``/calib/model``
и ``/calib/gpu`` (мок); метрики без текстов (негативная приватность, I5).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import ai_workspace.calibration.admin_api as admin_api
from ai_workspace.calibration import policy, profiles
from ai_workspace.gpu import GpuStatus

KEY = "test-calib-key-0123"
MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"

GOLDEN_TASKS = [
    {"id": "g01", "zone": "public", "prompt": "Структура статьи про MCP-RAG.",
     "expect_keywords": ["структура"]},
    {"id": "g02", "zone": "public", "prompt": "Черновик раздела про VRAM.",
     "expect_keywords": ["VRAM"]},
]
HELDOUT_TASKS = [
    {"id": "h01", "zone": "public", "prompt": "Процитируй источники.",
     "expect_keywords": ["источник"]},
]


# ── фикстуры/хелперы ───────────────────────────────────────────────────────


def _write_yaml(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _write_tasks(path: Path, tasks: list[dict]) -> Path:
    return _write_yaml(path, {"version": 1, "tasks": tasks})


def _classes(spec: dict | None = None) -> dict:
    base = {
        "shelf": "local",
        "shaping": "compressed",
        "retries": 1,
        "calibration_status": "uncalibrated",
        "calibrated_for": None,
        "active_profile": None,
    }
    base.update(spec or {})
    return base


def _registry_doc(classes: dict) -> dict:
    return {"model_classes": classes}


def _profile_doc(
    profile_id: str = "cal-fast-test-0001",
    *,
    model_class: str = "fast",
    status: str = "draft",
    calibrated_for: dict | None = None,
    flags: tuple[str, ...] = (),
    metrics_extra: dict | None = None,
) -> dict:
    cal = (
        calibrated_for
        if calibrated_for is not None
        else {"model_id": "qwen2.5:7b", "digest": "sha256:abc"}
    )
    metrics: dict = {
        "golden_median_score": 1.0,
        "heldout_score": 1.0,
        "parse_rate": 1.0,
        "rub": 0.0,
        "wall_s": 1.0,
    }
    metrics.update(metrics_extra or {})
    return {
        "schema": "calibration-profile/1",
        "profile_id": profile_id,
        "model_class": model_class,
        "calibrated_for": cal,
        "status": status,
        "version": 1,
        "evidence": {
            "probe_run": "probe-test00000000",
            "golden_manifest": "g" * 64,
            "pricing_manifest": "p" * 64,
            "metrics": metrics,
            "flags": list(flags),
        },
        "scalars": {
            "retries": 0,
            "max_iterations": 1,
            "shaping": "full-context",
            "context_mode": "full",
        },
        "constraints": {"quality_floor": 0.5},
        "created_at": "2026-10-09T00:00:00Z",
        "updated_at": "2026-10-09T00:00:00Z",
    }


def _settings(tmp_path: Path, **kw) -> admin_api.CalibSettings:
    params = dict(
        api_key=KEY,
        profiles_dir=tmp_path / "profiles",
        registry_path=tmp_path / "registry" / "model_classes.yaml",
        reports_dir=tmp_path / "reports",
    )
    params.update(kw)
    return admin_api.CalibSettings(**params)


def _fake_tags_http(models: list[dict]):
    def http_get(url: str) -> dict:
        return {"models": models}
    return http_get


def _headers(key: str = KEY) -> dict:
    return {"X-Calib-Key": key}


def _wait_status(client: TestClient, expected: str, timeout_s: float = 60.0) -> dict:
    """Поллинг /calib/probe/status до ожидаемого статуса (bg в executor)."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get("/calib/probe/status", headers=_headers()).json()
        if last.get("status") == expected:
            return last
        time.sleep(0.05)
    pytest.fail(f"статус не достигнут {expected!r} за {timeout_s}s: {last}")


# ── auth fail-closed (F7): пустой ключ / неверный ключ / compare_digest ────


def test_create_app_refuses_empty_key_fail_closed() -> None:
    """Нет CALIB_API_KEY → отказ создания приложения (fail-closed старт)."""
    with pytest.raises(RuntimeError, match="CALIB_API_KEY"):
        admin_api.create_app(admin_api.CalibSettings(api_key="   "))
    with pytest.raises(RuntimeError, match="fail-closed"):
        admin_api.create_app()  # env не задан (delenv ниже — страховка)


def test_auth_missing_or_wrong_key_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(admin_api.CALIB_API_KEY_ENV, raising=False)
    app = admin_api.create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # без заголовка → 401
        r = client.get("/calib/model")
        assert r.status_code == 401
        # неверный ключ → 401; ключ неутекаем (нет в теле ответа)
        r = client.get("/calib/model", headers=_headers(key="wrong-key"))
        assert r.status_code == 401
        assert "wrong-key" not in r.text and KEY not in r.text
        # верный ключ → не 401
        r = client.get("/calib/model", headers=_headers())
        assert r.status_code == 200


def test_auth_uses_constant_time_compare_digest(monkeypatch, tmp_path) -> None:
    """Сравнение ключа — secrets.compare_digest (источник, key-hygiene)."""
    source = Path(admin_api.__file__).read_text(encoding="utf-8")
    assert "secrets.compare_digest" in source
    # bind строго loopback; workers=1 — инвариант single-flight (§2.3)
    assert 'BIND_HOST = "127.0.0.1"' in source
    assert "workers=1" in source

    calls: list[tuple[bytes, bytes]] = []

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(admin_api.secrets, "compare_digest", spy)
    app = admin_api.create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/calib/model", headers=_headers()).status_code == 200
    assert calls and calls[0][1].decode() == KEY  # сравнили с настройкой, не ==


# ── POST /calib/probe/start: гейты + single-flight (202+409) ───────────────


def _blocked_runner(argv_log: list[str], started: threading.Event,
                    release: threading.Event):
    def runner(argv: list[str]) -> int:
        argv_log.extend(argv)
        started.set()
        assert release.wait(timeout=30), "runner не отпущен тестом"
        return 0
    return runner


def test_probe_start_gates_operator_gate(tmp_path) -> None:
    """Без confirm_live / без отдельного confirm_ext (₽, строже CLI) → 400."""
    settings = _settings(tmp_path)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        base = {"model_class": "fast", "heldout": "heldout.yaml"}
        # нет confirm_live → 400 (Operator Gate, аналог --confirm-live)
        r = client.post("/calib/probe/start", json=base, headers=_headers())
        assert r.status_code == 400
        assert "confirm_live" in r.json()["detail"]
        # ext без confirm_ext → 400 (отдельный флаг, СТРОЖЕ CLI)
        r = client.post(
            "/calib/probe/start",
            json={**base, "confirm_live": True, "ext": True},
            headers=_headers(),
        )
        assert r.status_code == 400
        assert "confirm_ext" in r.json()["detail"]
        # runs < MIN_RUNS → 422 (валидация pydantic, спека N>=3)
        r = client.post(
            "/calib/probe/start",
            json={**base, "confirm_live": True, "runs": 2},
            headers=_headers(),
        )
        assert r.status_code == 422
        # аргумент-путь с ведущим '-' → 400 (не argparse-инъекция)
        r = client.post(
            "/calib/probe/start",
            json={**base, "confirm_live": True, "heldout": "--inject"},
            headers=_headers(),
        )
        assert r.status_code == 400


def test_probe_start_single_flight_202_then_409(tmp_path) -> None:
    """Параллельный старт: первый 202, второй 409; после завершения — снова 202."""
    argv_log: list[str] = []
    started, release = threading.Event(), threading.Event()
    settings = _settings(
        tmp_path, probe_runner=_blocked_runner(argv_log, started, release)
    )
    app = admin_api.create_app(settings)
    body = {
        "model_class": "fast",
        "heldout": "/tmp/heldout.yaml",
        "confirm_live": True,
        "live": True,
        "ext": True,
        "confirm_ext": True,
        "runs": 3,
        "zone": "private",
    }
    with TestClient(app) as client:
        r1 = client.post("/calib/probe/start", json=body, headers=_headers())
        assert r1.status_code == 202
        assert r1.json()["status"] == "accepted"
        assert started.wait(timeout=10)  # bg действительно вошёл в runner

        # второй старт, пока первый жив → 409 (single-flight)
        r2 = client.post("/calib/probe/start", json=body, headers=_headers())
        assert r2.status_code == 409
        # статус показывает running, ничего не упало
        assert (
            client.get("/calib/probe/status", headers=_headers()).json()["status"]
            == "running"
        )

        release.set()  # отпускаем прогон
        done = _wait_status(client, "done")
        assert done["exit_code"] == 0
        assert done["report"] is None  # отчёта не писалось (fake-runner)

        # после завершения следующий старт снова 202 (lock освобождён)
        r3 = client.post("/calib/probe/start", json=body, headers=_headers())
        assert r3.status_code == 202
        release.set()  # уже отпущен — runner вернётся сразу
        _wait_status(client, "done")

    # argv передан CLI-канону с гейтами API (не dry-run)
    assert "--confirm-live" in argv_log
    assert "--live" in argv_log and "--ext" in argv_log
    assert "--class" in argv_log and "fast" in argv_log
    assert "--reports-dir" in argv_log and "--profiles-dir" in argv_log


def test_probe_start_runner_exception_marks_failed(tmp_path) -> None:
    """Сбой обвязки (исключение runner) → статус failed, не «зависший» running."""

    def boom(argv: list[str]) -> int:
        raise RuntimeError("executor died")

    settings = _settings(tmp_path, probe_runner=boom)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/probe/start",
            json={"model_class": "fast", "heldout": "h.yaml", "confirm_live": True},
            headers=_headers(),
        )
        assert r.status_code == 202
        failed = _wait_status(client, "failed")
        assert "executor died" in failed["error"]


def test_probe_start_full_chain_stub_metrics_only(tmp_path) -> None:
    """Полный chain через РЕАЛЬНЫЙ probe_run.main (контурный стаб, без --live):
    отчёт записан, статус done, ответ — whitelist метрик БЕЗ текстов (I5)."""
    golden = _write_tasks(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_tasks(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    settings = _settings(tmp_path)  # probe_runner=None → реальный CLI-канон
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/probe/start",
            json={
                "model_class": "fast",
                "mode": str(MODE),
                "golden": str(golden),
                "heldout": str(heldout),
                "runs": 3,
                "confirm_live": True,  # стаб (live=False): движок не живой
            },
            headers=_headers(),
        )
        assert r.status_code == 202
        done = _wait_status(client, "done", timeout_s=180)
        assert done["exit_code"] in (0, 1)  # 1 = рамка D6 не применена — норма
        report = done["report"]
        assert report is not None, f"отчёт не прочитан: {done}"
        # контракт M1–M7: whitelist-поля итогового ProbeReport
        for key in (
            "run_id", "model_id", "digest", "golden_median_score",
            "golden_dispersion", "heldout_score", "parse_rate", "rub",
            "wall_s", "n_runs", "flags",
        ):
            assert key in report, f"нет метрики {key}"
        assert report["run_id"].startswith("probe-")
        # приватность I5 (негатив): тексты прогона не едут никуда
        for forbidden in ("document", "draft", "critic_fragment",
                          "verdict", "detail", "node_events", "live_sample",
                          "q_report"):
            assert forbidden not in report
        assert "Контурный стаб-ответ" not in json.dumps(done)  # тексты стаба
        # прогресс из partial: сегменты посчитаны через load_partial
        progress = done["progress"]
        assert progress is not None
        assert progress.get("segments_done", 0) >= 9  # (2 golden + 1 heldout)×3


# ── POST /calib/approve: fail-closed через реальный profile_approve.main ───


def _approve_env(tmp_path: Path, profile: dict, classes: dict | None = None):
    """tmp-носители: профиль + реестр; возвращает настройки и пути."""
    if classes is None:
        classes = _registry_doc(
            {"fast": _classes({"shelf": "local"})}
        )
    reg_path = _write_yaml(tmp_path / "registry" / "model_classes.yaml", classes)
    profile_path = _write_yaml(
        tmp_path / "profiles" / f"{profile['profile_id']}.yaml", profile
    )
    settings = _settings(
        tmp_path,
        profiles_dir=tmp_path / "profiles",
        registry_path=reg_path,
    )
    return settings, profile_path, reg_path


def _post_approve(client: TestClient, body: dict):
    return client.post("/calib/approve", json=body, headers=_headers())


def test_approve_ceiling_without_needle_refused(tmp_path) -> None:
    """F-2а: ceiling-флаг без needle-доказательства → 422, носители не тронуты."""
    profile = _profile_doc(flags=("ceiling",))  # needle_rate нет
    settings, profile_path, reg_path = _approve_env(tmp_path, profile)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = _post_approve(client, {"profile_id": profile["profile_id"], "confirm": True})
        assert r.status_code == 422
        assert "ceiling" in r.json()["detail"]
    # fail-closed: НИЧЕГО не написано — профиль draft v1, реестр не тронут,
    # аудита нет (решение не было легитимным)
    doc = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert doc["status"] == "draft" and doc["version"] == 1
    reg = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    assert reg["model_classes"]["fast"]["calibration_status"] == "uncalibrated"
    assert not (tmp_path / "profiles" / "approve_audit.jsonl").exists()


def test_approve_ceiling_with_needle_proof_applied(tmp_path) -> None:
    """α: needle_rate >= NEEDLE_RATE_FLOOR — ceiling-профиль применяется
    без escape-флагов (needle-доказательство различимости, M4)."""
    profile = _profile_doc(
        flags=("ceiling",),
        metrics_extra={"needle_rate": policy.NEEDLE_RATE_FLOOR + 0.1},
    )
    settings, profile_path, reg_path = _approve_env(tmp_path, profile)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = _post_approve(client, {"profile_id": profile["profile_id"], "confirm": True})
        assert r.status_code == 200
        assert r.json()["applied"] is True
    doc = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert doc["status"] == "calibrated" and doc["version"] == 2
    reg = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    cls = reg["model_classes"]["fast"]
    assert cls["calibration_status"] == "calibrated"
    assert cls["active_profile"] == profile["profile_id"]
    assert cls["calibrated_for"] == {"model_id": "qwen2.5:7b", "digest": "sha256:abc"}


def test_approve_ceiling_ok_escape_audited(tmp_path) -> None:
    """Escape (аналог --ceiling-ok) с reason → применение + аудит ДО носителей."""
    profile = _profile_doc(flags=("ceiling",))
    settings, profile_path, _ = _approve_env(tmp_path, profile)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = _post_approve(
            client,
            {
                "profile_id": profile["profile_id"],
                "confirm": True,
                "ceiling_ok": True,
                "reason": "оператор осознанно применяет ceiling-замер",
            },
        )
        assert r.status_code == 200
    audit = tmp_path / "profiles" / "approve_audit.jsonl"
    assert audit.is_file()
    record = json.loads(audit.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["action"] == "ceiling_approve"
    assert record["profile_id"] == profile["profile_id"]
    assert yaml.safe_load(profile_path.read_text(encoding="utf-8"))["status"] == "calibrated"


def test_approve_incomplete_calibrated_for_refused(tmp_path) -> None:
    """F-2а: пустой model_id/digest → 422; --force+reason (аудит) → применение."""
    profile = _profile_doc(calibrated_for={"model_id": "", "digest": ""})
    settings, profile_path, _ = _approve_env(tmp_path, profile)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = _post_approve(client, {"profile_id": profile["profile_id"], "confirm": True})
        assert r.status_code == 422
        assert "calibrated_for" in r.json()["detail"]
        assert yaml.safe_load(profile_path.read_text(encoding="utf-8"))["status"] == "draft"

        # escape: force + reason (зеркаро CLI) → применение с аудитом
        r = _post_approve(
            client,
            {
                "profile_id": profile["profile_id"],
                "confirm": True,
                "force": True,
                "reason": "факт полки не наблюдаем, применяется осознанно",
            },
        )
        assert r.status_code == 200
    record = json.loads(
        (tmp_path / "profiles" / "approve_audit.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()[-1]
    )
    assert record["action"] == "force_approve"


def test_approve_unknown_profile_refused_and_dry_run_default(tmp_path) -> None:
    """Неизвестный профиль → 422; без confirm — dry-run: план, носители целы."""
    profile = _profile_doc()
    settings, profile_path, reg_path = _approve_env(tmp_path, profile)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = _post_approve(client, {"profile_id": "cal-missing-0000", "confirm": True})
        assert r.status_code == 422
        # dry-run по умолчанию: exit 0, ничего не применено
        r = _post_approve(client, {"profile_id": profile["profile_id"]})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is False and "ПЛАН" in body["output"]
    assert yaml.safe_load(profile_path.read_text(encoding="utf-8"))["status"] == "draft"
    reg = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    assert reg["model_classes"]["fast"]["active_profile"] is None


# ── GET /calib/model и /calib/gpu: контракт (моки) + приватность ────────────


def _model_env(tmp_path: Path, *, digest: str = "sha256:abc"):
    """Активная калибровка fast + профиль calibrated + совпадающая полка.

    Реестр — ПЛОСКИЙ (прод-контракт ``registry/model_classes.yaml`` для
    ``Registry``/``runtime.active_calibration``); approve-эндпоинт тестируется
    отдельно на ОБЁРНУТОЙ форме (контракт profile_approve CLI).
    """
    profile = _profile_doc(status="calibrated")
    flat = {
        "fast": _classes(
            {
                "calibration_status": "calibrated",
                "active_profile": profile["profile_id"],
                "calibrated_for": {
                    "model_id": "qwen2.5:7b", "digest": digest,
                },
            }
        )
    }
    reg_path = _write_yaml(tmp_path / "registry" / "model_classes.yaml", flat)
    _write_yaml(
        tmp_path / "profiles" / f"{profile['profile_id']}.yaml", profile
    )
    tags = _fake_tags_http([{"name": "qwen2.5:7b", "digest": digest}])
    settings = _settings(
        tmp_path,
        registry_path=reg_path,
        profiles_dir=tmp_path / "profiles",
        http_get=tags,
    )
    return settings, profile


def test_model_endpoint_contract_ok_drift(tmp_path) -> None:
    settings, profile = _model_env(tmp_path)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.get("/calib/model", headers=_headers())
        assert r.status_code == 200
        body = r.json()
        # контракт S1: полка + активный профиль + drift + классы
        assert body["shelf"] == {"model_id": "qwen2.5:7b", "digest": "sha256:abc"}
        assert body["active"]["model_class"] == "fast"
        assert body["active"]["profile_id"] == profile["profile_id"]
        assert body["active"]["profile_status"] == "calibrated"
        assert body["drift"]["status"] == "ok"
        assert body["drift"]["blocked"] is False
        assert body["drift"]["live_probe"] == "ok"
        cls = body["classes"]["fast"]
        assert cls["shelf"] == "local"
        assert cls["calibration_status"] == "calibrated"
        # приватность: только whitelist-поля, никаких текстов
        dumped = json.dumps(body)
        for forbidden in ("document", "draft", "critic_fragment", "prompt"):
            assert forbidden not in dumped


def test_model_endpoint_drift_t1_blocks_live_probe(tmp_path) -> None:
    """Digest полки ≠ calibrated_for → T1: live_probe=blocked (F8: запрет)."""
    settings, _ = _model_env(tmp_path, digest="sha256:OTHER")
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/model", headers=_headers()).json()
        assert body["drift"]["status"] == "t1"
        assert body["drift"]["reason"] == "digest_mismatch"
        assert body["drift"]["live_probe"] == "blocked"


def test_model_endpoint_without_active_profile(tmp_path) -> None:
    """Нет активного профиля → active=None, drift ok (паритет F1)."""
    reg_path = _write_yaml(
        tmp_path / "registry" / "model_classes.yaml",
        {"fast": _classes()},  # плоская форма (прод-контракт)
    )
    settings = _settings(
        tmp_path,
        registry_path=reg_path,
        http_get=_fake_tags_http([{"name": "qwen2.5:7b", "digest": "sha256:abc"}]),
    )
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/model", headers=_headers()).json()
        assert body["active"] is None
        assert body["drift"]["status"] == "ok"
        assert body["shelf"]["model_id"] == "qwen2.5:7b"


def test_model_endpoint_accepts_wrapped_registry_shape(tmp_path) -> None:
    """Обёрнутая форма файла (контракт approve CLI) читается так же —
    ``_read_classes`` нормализует обе формы носителя."""
    profile = _profile_doc(status="calibrated")
    wrapped = _registry_doc(
        {
            "fast": _classes(
                {
                    "calibration_status": "calibrated",
                    "active_profile": profile["profile_id"],
                    "calibrated_for": {"model_id": "qwen2.5:7b", "digest": "sha256:abc"},
                }
            )
        }
    )
    reg_path = _write_yaml(tmp_path / "registry" / "model_classes.yaml", wrapped)
    _write_yaml(tmp_path / "profiles" / f"{profile['profile_id']}.yaml", profile)
    settings = _settings(
        tmp_path,
        registry_path=reg_path,
        profiles_dir=tmp_path / "profiles",
        http_get=_fake_tags_http([{"name": "qwen2.5:7b", "digest": "sha256:abc"}]),
    )
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/model", headers=_headers()).json()
        assert body["active"]["profile_id"] == profile["profile_id"]
        assert body["drift"]["status"] == "ok"


def test_model_endpoint_reads_real_prod_flat_registry(tmp_path) -> None:
    """Дефолтный путь (РЕАЛЬНЫЙ прод model_classes.yaml, плоская форма):
    классы на месте, без активного профиля — active=None, drift ok.
    http_get — фейк (герметичность; полка не трогается)."""
    real_registry = (
        Path(__file__).resolve().parents[1] / "registry" / "model_classes.yaml"
    )
    settings = _settings(
        tmp_path,
        registry_path=real_registry,
        http_get=_fake_tags_http([{"name": "qwen2.5:7b", "digest": "sha256:x"}]),
    )
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/model", headers=_headers()).json()
        assert set(body["classes"]) >= {"fast", "heavy", "fast-full"}
        assert body["classes"]["fast"]["shelf"] == "local"
        assert body["classes"]["local-only"] == {"rule": "zone", "calibrated_for": None}
        # в прод-реестре сейчас нет active_profile → паритет F1
        assert body["active"] is None
        assert body["drift"]["status"] == "ok"


def test_gpu_endpoint_contract_and_failsoft(tmp_path) -> None:
    """R1-preflight: ollama /api/ps + ws-lease слоты; оба источника fail-soft."""
    ps_payload = {
        "models": [
            {
                "name": "qwen2.5:7b", "digest": "sha256:z", "size": 9,
                "size_vram": 5_300_000_000, "expires_at": "2026-10-09T20:00:00Z",
            },
            {"junk": True},  # битая запись отбрасывается, не роняет эндпоинт
        ]
    }

    def http_get(url: str) -> dict:
        assert url.endswith("/api/ps")
        return ps_payload

    gpu = GpuStatus(
        capacity=1, used=1, free=0, lease_ms=90_000,
        holdings={"embed": ["nightly-video"], "vision": [], "reindex": []},
    )
    settings = _settings(tmp_path, http_get=http_get, gpu_status=lambda: gpu)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/gpu", headers=_headers()).json()
        assert body["ollama_ps"]["available"] is True
        model = body["ollama_ps"]["models"][0]
        assert model["name"] == "qwen2.5:7b"
        assert model["size_vram"] == 5_300_000_000
        assert "expires_at" in model and "digest" in model
        slots = body["gpu_slots"]
        assert slots["available"] is True
        assert slots["used"] == 1 and slots["free"] == 0
        assert slots["holdings"]["embed"] == ["nightly-video"]


def test_gpu_endpoint_fail_soft_when_sources_down(tmp_path) -> None:
    """Полка/ws-redis недоступны → available:false у источника, не 5xx."""

    def http_get(url: str) -> dict:
        raise OSError("ollama down")

    def gpu_down() -> GpuStatus:
        raise ConnectionError("ws-redis down")

    settings = _settings(tmp_path, http_get=http_get, gpu_status=gpu_down)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.get("/calib/gpu", headers=_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["ollama_ps"] == {"available": False}
        assert body["gpu_slots"] == {"available": False}


def test_probe_status_idle_initially(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/probe/status", headers=_headers()).json()
        assert body["status"] == "idle"
        assert body["report"] is None and body["progress"] is None


def test_admin_api_reuses_canonical_modules_not_copies() -> None:
    """DBD: дефолтные раннеры — сами CLI-каноны (ноль дублей логики)."""
    source = Path(admin_api.__file__).read_text(encoding="utf-8")
    assert "probe_run.main" in source
    assert "profile_approve.main" in source
    assert "drift_mod.detect" in source or "drift.detect" in source
    assert "facts_for" in source and "load_partial" in source
    #asic: никто не копирует NEEDLE_RATE_FLOOR значением — гейт живёт в CLI
    assert "NEEDLE_RATE_FLOOR = 0" not in source


# ── Ф1b: маркеры наблюдаемости [CALIB-API] (E5, journald) ──────────────────


def test_log_event_marker_format_and_key_hygiene(capsys) -> None:
    """Единый префикс [CALIB-API] k=v; ключ в маркеры не попадает."""
    admin_api._log_event("auth-refused")
    admin_api._log_event("probe-start", model_class="fast", runs=3)
    out = capsys.readouterr().out
    assert out.count("[CALIB-API] auth-refused\n") == 1
    assert "[CALIB-API] probe-start model_class=fast runs=3\n" in out
    assert KEY not in out


def test_start_marker_in_create_app_once_no_key(tmp_path, capsys, monkeypatch) -> None:
    """Регресс observability: systemd/uvicorn зовёт ``create_app --factory``
    БЕЗ main() — маркер старта пишет само построение приложения: ровно один
    раз, host/port/workers, БЕЗ значения ключа (runbook §6, key-hygiene)."""
    monkeypatch.delenv(admin_api.CALIB_API_PORT_ENV, raising=False)
    app = admin_api.create_app(_settings(tmp_path))
    assert app is not None
    out = capsys.readouterr().out
    assert out.count("[CALIB-API] start") == 1  # ровно один, не дважды
    assert out.splitlines() == [
        f"[CALIB-API] start host=127.0.0.1 port={admin_api.DEFAULT_PORT} workers=1"
    ]
    assert KEY not in out  # секрет — никогда в маркерах


def test_start_marker_port_env_valid_and_garbage_safe(
    tmp_path, capsys, monkeypatch
) -> None:
    """Порт из env; мусор в env НЕ роняет построение — дефолт."""
    monkeypatch.setenv(admin_api.CALIB_API_PORT_ENV, "9701")
    admin_api.create_app(_settings(tmp_path))
    out = capsys.readouterr().out
    assert out.count("[CALIB-API] start") == 1
    assert "port=9701" in out

    monkeypatch.setenv(admin_api.CALIB_API_PORT_ENV, "junk-not-a-port")
    admin_api.create_app(_settings(tmp_path))
    out = capsys.readouterr().out
    assert out.count("[CALIB-API] start") == 1
    assert f"port={admin_api.DEFAULT_PORT}" in out


def test_main_does_not_duplicate_start_marker() -> None:
    """main() вызывает create_app() — дубль маркера убран (source-гвард)."""
    source = Path(admin_api.__file__).read_text(encoding="utf-8")
    main_src = source.split("def main()", 1)[1]
    assert '_log_event("start"' not in main_src


def test_markers_auth_refused_probe_start_finish(tmp_path, capsys) -> None:
    """401 → auth-refused; 202 → probe-start; финиш → probe-finish exit=0."""
    settings = _settings(tmp_path, probe_runner=lambda argv: 0)
    app = admin_api.create_app(settings)
    body = {
        "model_class": "fast",
        "heldout": "heldout.yaml",
        "confirm_live": True,
        "runs": 3,
    }
    with TestClient(app) as client:
        r = client.get("/calib/model", headers=_headers(key="wrong"))
        assert r.status_code == 401
        capsys.readouterr()  # отсечь всё до прогонов-событий
        r = client.post("/calib/probe/start", json=body, headers=_headers())
        assert r.status_code == 202
        _wait_status(client, "done")
    out = capsys.readouterr().out
    assert "[CALIB-API] auth-refused" not in out  # отсечён (до readouterr)
    assert "[CALIB-API] probe-start model_class=fast runs=3" in out
    assert "[CALIB-API] probe-start" in out and "heldout" not in out.split(
        "[CALIB-API] probe-start"
    )[1].splitlines()[0]
    assert "[CALIB-API] probe-finish exit=0" in out
    assert KEY not in out  # key-hygiene: секрет — никогда в маркерах


def test_markers_approve_start_finish(tmp_path, capsys) -> None:
    """approve: approve-start (без reason) + approve-finish exit/applied."""
    settings = _settings(tmp_path)
    _write_tasks(settings.reports_dir / "x.yaml", HELDOUT_TASKS)  # шум-каталог
    profile = _profile_doc()
    _write_yaml(settings.profiles_dir / "cal-fast-test-0001.yaml", profile)
    _write_yaml(
        settings.registry_path,
        _registry_doc({"fast": _classes({"active_profile": "cal-fast-test-0001"})}),
    )
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/approve",
            json={"profile_id": "cal-fast-test-0001", "confirm": False,
                  "reason": "secret-reason-must-not-leak"},
            headers=_headers(),
        )
        assert r.status_code == 200, r.text
    out = capsys.readouterr().out
    assert "[CALIB-API] approve-start profile_id=cal-fast-test-0001 confirm=False" in out
    assert "secret-reason-must-not-leak" not in out
    assert "[CALIB-API] approve-finish exit=0 applied=False" in out

# ── Ф3a: пара base↔variant (/calib/pair/*) + запись P5 (/calib/record) ──────

VARIANT = Path(__file__).resolve().parents[1] / "modes" / "statya.deep.yaml"


def _pair_body(**extra) -> dict:
    """Тело pair/start: явные base/variant + обязательные class/heldout."""
    body = {
        "base": str(MODE), "variant": str(VARIANT),
        "model_class": "fast", "heldout": "/tmp/heldout.yaml",
        "confirm_live": True,
    }
    body.update(extra)
    return body


def _wait_pair_status(client: TestClient, expected: str,
                      timeout_s: float = 60.0) -> dict:
    """Поллинг /calib/pair/status до ожидаемого статуса (bg в executor)."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get("/calib/pair/status", headers=_headers()).json()
        if last.get("status") == expected:
            return last
        time.sleep(0.05)
    pytest.fail(f"pair-статус не достигнут {expected!r} за {timeout_s}s: {last}")


def _pair_report_doc(run_id: str, golden: float, heldout: float,
                     dispersion: float = 0.0, extra: dict | None = None) -> dict:
    """JSON-отчёт плеча: метрики + ЗАПРЕЩЁННОЕ текстовое поле (негатив I5)."""
    doc = {
        "run_id": run_id, "model_id": "qwen2.5:7b", "digest": "sha256:abc",
        "golden_manifest": "g" * 64, "pricing_manifest": "p" * 64,
        "golden_median_score": golden, "golden_dispersion": dispersion,
        "heldout_score": heldout, "parse_rate": 1.0, "parse_rate_defined": True,
        "rub": 0.0, "wall_s": 1.0, "n_runs": 3, "flags": [],
        "document": "SECRET-PAIR-TEXT",  # вне whitelist — не должно утекать
    }
    doc.update(extra or {})
    return doc


def _pair_writer_runner(docs: tuple[dict, ...], reports_dir: Path):
    """Fake pair_runner: пишет отчёты плеч на носитель (как variant_pair)."""
    def runner(argv: list[str]) -> int:
        reports_dir.mkdir(parents=True, exist_ok=True)
        for i, doc in enumerate(docs):
            path = reports_dir / f"{doc['run_id']}.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            stamp = time.time() + i  # base раньше variant (порядок плеч)
            os.utime(path, (stamp, stamp))
        return 0
    return runner


def test_pair_status_idle_initially(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/pair/status", headers=_headers()).json()
        assert body["status"] == "idle"
        assert body["base"] is None and body["variant"] is None
        assert body["reasons"] is None and body["passed"] is None
        assert body["progress"] is None and "report" not in body


def test_pair_start_gates_confirm_live_and_injection(tmp_path) -> None:
    """Нет confirm_live → 400; путь с ведущим '-' → 400; runs<3 → 422."""
    settings = _settings(tmp_path)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/pair/start",
            json=_pair_body(confirm_live=False), headers=_headers(),
        )
        assert r.status_code == 400
        assert "confirm_live" in r.json()["detail"]
        r = client.post(
            "/calib/pair/start",
            json=_pair_body(base="--inject"), headers=_headers(),
        )
        assert r.status_code == 400
        r = client.post(
            "/calib/pair/start", json=_pair_body(runs=2), headers=_headers(),
        )
        assert r.status_code == 422
        # гейты не тронули состояние — пара не стартовала
        assert (
            client.get("/calib/pair/status", headers=_headers()).json()["status"]
            == "idle"
        )


def test_pair_start_single_flight_independent_from_probe(tmp_path) -> None:
    """202 → повтор 409 (свой single-flight); probe-состояние НЕ делится."""
    pair_argv: list[str] = []
    probe_argv: list[str] = []
    pair_started, pair_release = threading.Event(), threading.Event()
    probe_started, probe_release = threading.Event(), threading.Event()
    settings = _settings(
        tmp_path,
        pair_runner=_blocked_runner(pair_argv, pair_started, pair_release),
        probe_runner=_blocked_runner(probe_argv, probe_started, probe_release),
    )
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r1 = client.post("/calib/pair/start", json=_pair_body(), headers=_headers())
        assert r1.status_code == 202
        assert r1.json() == {"status": "accepted", "poll": "/calib/pair/status"}
        assert pair_started.wait(timeout=10)  # bg вошёл в runner

        # пара занята → 409 (single-flight); статус running
        r2 = client.post("/calib/pair/start", json=_pair_body(), headers=_headers())
        assert r2.status_code == 409
        assert (
            client.get("/calib/pair/status", headers=_headers()).json()["status"]
            == "running"
        )
        # probe-состояние независимо: старт probe при живой паре → 202
        rp = client.post(
            "/calib/probe/start",
            json={"model_class": "fast", "heldout": "h.yaml",
                  "confirm_live": True},
            headers=_headers(),
        )
        assert rp.status_code == 202
        assert probe_started.wait(timeout=10)

        probe_release.set()
        pair_release.set()
        done = _wait_pair_status(client, "done")
        assert done["exit_code"] == 0
        _wait_status(client, "done")  # probe тоже завершился

    # argv передан CLI-канону variant_pair (гейты API пройдены)
    assert "--confirm-live" in pair_argv and "--reports-dir" in pair_argv
    assert "--base" in pair_argv and "--variant" in pair_argv
    assert "--class" in pair_argv and "fast" in pair_argv
    assert "--heldout" in pair_argv and "/tmp/heldout.yaml" in pair_argv


def test_pair_status_done_reports_and_reasons(tmp_path) -> None:
    """done: отчёты плеч (whitelist) + reasons РЕАЛЬНОГО evaluate_promotion."""
    base_doc = _pair_report_doc("probe-pairbase00001", 0.5, 0.5)
    var_doc = _pair_report_doc(
        "probe-pairvar00001", 0.9, 0.88, dispersion=0.05,
        extra={"needle_rate": None},
    )
    settings = _settings(
        tmp_path,
        pair_runner=_pair_writer_runner(
            (base_doc, var_doc), tmp_path / "reports"
        ),
    )
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/pair/start",
            json=_pair_body(quality_floor=0.7), headers=_headers(),
        )
        assert r.status_code == 202
        done = _wait_pair_status(client, "done")
    assert done["exit_code"] == 0
    # отчёты плеч — whitelist-метрики; base — первый по времени, variant — второй
    for side, doc in (("base", base_doc), ("variant", var_doc)):
        report = done[side]
        assert report is not None, f"нет отчёта {side}: {done}"
        for key in ("run_id", "golden_median_score", "heldout_score",
                    "golden_dispersion", "rub", "wall_s", "flags"):
            assert key in report, f"нет метрики {key}"
        assert report["run_id"] == doc["run_id"]
    # reasons — фактические поля критерия §7.3 (база ниже пола, вариант выше,
    # heldout в дисперсии → passed; needle=None не гейтится без ceiling)
    assert done["passed"] is True
    assert done["reasons"] == []
    assert done["run_id"] == "probe-pairvar00001"  # статус = плечо variant


def test_pair_metrics_only_no_texts_leak(tmp_path) -> None:
    """Негативная приватность I5: тексты отчётов не едут ни в плечи, ни в reasons."""
    base_doc = _pair_report_doc("probe-privbase0001", 0.9, 0.9)  # база выше пола
    var_doc = _pair_report_doc("probe-privvar0001", 0.9, 0.5, dispersion=0.05)
    settings = _settings(
        tmp_path,
        pair_runner=_pair_writer_runner(
            (base_doc, var_doc), tmp_path / "reports"
        ),
    )
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/pair/start",
            json=_pair_body(quality_floor=0.7), headers=_headers(),
        )
        assert r.status_code == 202
        done = _wait_pair_status(client, "done")
    dumped = json.dumps(done)
    assert "SECRET-PAIR-TEXT" not in dumped
    for side in ("base", "variant"):
        for forbidden in ("document", "draft", "critic_fragment",
                          "q_report", "live_sample", "verdict"):
            assert forbidden not in done[side], f"утёк {forbidden} в {side}"
    # критерий честно НЕ пройден: база не проваливает пол + расхождение
    # held-out; reasons — строки-метрики, без текстов прогона
    assert done["passed"] is False
    assert done["reasons"] and all(isinstance(x, str) for x in done["reasons"])
    assert any("quality_floor" in x for x in done["reasons"])
    assert any("held-out" in x for x in done["reasons"])
    assert "SECRET-PAIR-TEXT" not in " ".join(done["reasons"])


# ── POST /calib/record: P5 — только исполнение подтверждённого решения ─────


def test_record_requires_confirm(tmp_path) -> None:
    """P5: без confirm=true → 400 (гейт срабатывает ДО state-проверки)."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    settings = _settings(tmp_path, pair_runner=runner)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/record",
            json={"variant": str(VARIANT), "status": "promoted"},
            headers=_headers(),
        )
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"]
    assert calls == []  # ничего не исполнялось


def test_record_without_finished_pair_refused(tmp_path) -> None:
    """Нет завершённой пары в процессе → 409 (запись ссылается на её отчёты)."""
    settings = _settings(tmp_path, pair_runner=lambda argv: 0)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/calib/record",
            json={"variant": str(VARIANT), "status": "promoted",
                  "confirm": True},
            headers=_headers(),
        )
        assert r.status_code == 409


def _record_env(tmp_path, record_runner=None):
    """Завершённая пара (mock exit 0) + record-runner по '--record' в argv."""
    def pair_and_record(argv: list[str]) -> int:
        if "--record" in argv:
            return record_runner(argv) if record_runner is not None else 0
        return 0

    settings = _settings(tmp_path, pair_runner=pair_and_record)
    app = admin_api.create_app(settings)
    return app


def test_record_success_argv_and_response(tmp_path) -> None:
    """confirm=true после пары → CLI-канон записи; 200 recorded/status/output."""
    argv_log: list[str] = []

    def record_runner(argv: list[str]) -> int:
        argv_log.extend(argv)
        print("решение записано")
        return 0

    app = _record_env(tmp_path, record_runner)
    with TestClient(app) as client:
        assert (
            client.post(
                "/calib/pair/start",
                json=_pair_body(golden="/tmp/golden.yaml",
                                needle="/tmp/needle.yaml", live=True),
                headers=_headers(),
            ).status_code
            == 202
        )
        _wait_pair_status(client, "done")
        r = client.post(
            "/calib/record",
            json={"variant": str(VARIANT), "status": "promoted",
                  "confirm": True, "base": str(MODE),
                  "model_class": "fast", "decided_by": "operator-andrey"},
            headers=_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["recorded"] is True
        assert body["status"] == "promoted"
        assert "решение записано" in body["output"]
    # argv: канон записи + параметры ПОСЛЕДНЕЙ пары (resume — без пересчёта)
    for token in ("--record", "promoted", "--confirm-record", "--resume",
                  "--confirm-live", "--reports-dir", "--heldout",
                  "/tmp/heldout.yaml", "--golden", "/tmp/golden.yaml",
                  "--needle", "/tmp/needle.yaml", "--live", "--class", "fast",
                  "--decided-by", "operator-andrey"):
        assert token in argv_log, f"нет {token} в argv записи"
    assert "--dry-run" not in argv_log


def test_record_fail_closed_exit2_to_422(tmp_path) -> None:
    """fail-closed отказ CLI (exit 2: CV0–CV7) → 422 с деталью из stderr."""

    def record_runner(argv: list[str]) -> int:
        print("ОТКАЗ записи (реестр не тронут): CV7: отчёт probe не существует",
              file=sys.stderr)
        return 2

    app = _record_env(tmp_path, record_runner)
    with TestClient(app) as client:
        assert (
            client.post("/calib/pair/start", json=_pair_body(),
                        headers=_headers()).status_code
            == 202
        )
        _wait_pair_status(client, "done")
        r = client.post(
            "/calib/record",
            json={"variant": str(VARIANT), "status": "rejected",
                  "confirm": True},
            headers=_headers(),
        )
        assert r.status_code == 422
        assert "CV7" in r.json()["detail"]


def test_record_mismatched_pair_refused(tmp_path) -> None:
    """variant ≠ последняя пара → 400 (CV7: запись ссылается на её отчёты)."""
    app = _record_env(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post("/calib/pair/start", json=_pair_body(),
                        headers=_headers()).status_code
            == 202
        )
        _wait_pair_status(client, "done")
        other = str(MODE.with_name("statya.local.yaml"))
        r = client.post(
            "/calib/record",
            json={"variant": other, "status": "promoted", "confirm": True},
            headers=_headers(),
        )
        assert r.status_code == 400
        assert "расходятся" in r.json()["detail"]


def test_markers_pair_and_record(tmp_path, capsys) -> None:
    """Маркеры Ф3a: pair-start/pair-finish/record-start/record-finish."""
    settings = _settings(tmp_path, pair_runner=lambda argv: 0)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        assert (
            client.post("/calib/pair/start", json=_pair_body(),
                        headers=_headers()).status_code
            == 202
        )
        _wait_pair_status(client, "done")
        r = client.post(
            "/calib/record",
            json={"variant": str(VARIANT), "status": "promoted",
                  "confirm": True},
            headers=_headers(),
        )
        assert r.status_code == 200
    out = capsys.readouterr().out
    assert "[CALIB-API] pair-start model_class=fast runs=3 zone=public live=False" in out
    assert "[CALIB-API] pair-finish exit=0" in out
    assert "[CALIB-API] record-start variant=statya.deep status=promoted" in out
    assert "[CALIB-API] record-finish exit=0 status=promoted" in out
    assert KEY not in out  # key-hygiene

# ── Ф4a: GET /calib/reports — история отчётов (mtime DESC, metrics-only) ───


def _write_probe_report(reports_dir: Path, run_id: str, *, dt: float) -> Path:
    """JSON-отчёт на носителе: метрики + запрещённое текстовое поле (I5)."""
    doc = _pair_report_doc(run_id, 0.9, 0.88, dispersion=0.02)
    path = reports_dir / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    stamp = time.time() + dt
    os.utime(path, (stamp, stamp))
    return path


def test_reports_empty_or_missing_dir_fail_soft(tmp_path) -> None:
    """Нет/пустой reports_dir → {"reports": [], "total": 0} (fail-soft, не 500)."""
    settings = _settings(tmp_path)  # каталог reports не существует
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.get("/calib/reports", headers=_headers())
        assert r.status_code == 200
        assert r.json() == {"reports": [], "total": 0}
        settings.reports_dir.mkdir(parents=True)  # пустой существующий
        assert client.get("/calib/reports", headers=_headers()).json() == {
            "reports": [], "total": 0,
        }


def test_reports_desc_order_whitelist_fields_and_ts(tmp_path) -> None:
    """Несколько отчётов: свежие сверху (mtime DESC); поля — только
    whitelist + ts; ts — ISO-время mtime файла."""
    settings = _settings(tmp_path)
    reports = settings.reports_dir
    _write_probe_report(reports, "probe-old000000001", dt=0.0)
    newest = _write_probe_report(reports, "probe-new000000001", dt=10.0)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        body = client.get("/calib/reports", headers=_headers()).json()
    assert body["total"] == 2
    assert [r["run_id"] for r in body["reports"]] == [
        "probe-new000000001", "probe-old000000001",
    ]
    first = body["reports"][0]
    # metrics-only: только whitelist-поля + ts, ничего сверх
    assert set(first) <= set(admin_api._REPORT_FIELDS) | {"ts"}
    for key in ("run_id", "model_id", "digest", "golden_manifest",
                "pricing_manifest", "golden_median_score",
                "golden_dispersion", "heldout_score", "parse_rate",
                "parse_rate_defined", "rub", "wall_s", "n_runs", "flags"):
        assert key in first, f"нет метрики {key}"
    ts = datetime.fromisoformat(first["ts"].replace("Z", "+00:00"))
    assert abs(ts.timestamp() - newest.stat().st_mtime) < 1.0


def test_reports_skip_partial_and_broken_json(tmp_path) -> None:
    """partial-снапшоты, битый JSON и не-словарь пропускаются (fail-soft)."""
    settings = _settings(tmp_path)
    reports = settings.reports_dir
    _write_probe_report(reports, "probe-good00000001", dt=5.0)
    # partial живого прогона: матчится glob'ом probe-*.json — исключаем
    (reports / "probe-live00000001.partial.json").write_text(
        json.dumps({"segments": []}), encoding="utf-8"
    )
    (reports / "probe-broken000001.json").write_text(
        "{битый json", encoding="utf-8"
    )
    (reports / "probe-notdict0001.json").write_text("[1, 2]", encoding="utf-8")
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.get("/calib/reports", headers=_headers())
        assert r.status_code == 200  # битые носители ≠ 500
        body = r.json()
    assert body["total"] == 1
    assert [x["run_id"] for x in body["reports"]] == ["probe-good00000001"]


def test_reports_limit_clamp(tmp_path) -> None:
    """limit: default 20; 0 → 1; 1000 → 200 (потолок); total — до limit."""
    settings = _settings(tmp_path)
    reports = settings.reports_dir
    for i in range(205):
        _write_probe_report(reports, f"probe-clamp{i:08d}", dt=float(i))
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        # default 20
        body = client.get("/calib/reports", headers=_headers()).json()
        assert body["total"] == 205 and len(body["reports"]) == 20
        assert body["reports"][0]["run_id"] == "probe-clamp00000204"
        # 0 → clamp к 1
        body = client.get(
            "/calib/reports", params={"limit": 0}, headers=_headers()
        ).json()
        assert body["total"] == 205 and len(body["reports"]) == 1
        # 1000 → clamp к 200; срез сохраняет свежесть (mtime DESC)
        body = client.get(
            "/calib/reports", params={"limit": 1000}, headers=_headers()
        ).json()
        assert body["total"] == 205 and len(body["reports"]) == 200
        assert body["reports"][0]["run_id"] == "probe-clamp00000204"
        assert body["reports"][-1]["run_id"] == "probe-clamp00000005"


def test_reports_metrics_only_no_text_leak(tmp_path) -> None:
    """I5: секретное текстовое поле JSON-отчёта не утекает в историю."""
    settings = _settings(tmp_path)
    _write_probe_report(settings.reports_dir, "probe-secr00000001", dt=0.0)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        r = client.get("/calib/reports", headers=_headers())
        assert r.status_code == 200
    dumped = r.text
    assert "SECRET-PAIR-TEXT" not in dumped
    for forbidden in ("document", "draft", "critic_fragment", "q_report",
                      "live_sample", "verdict"):
        assert forbidden not in dumped, f"утёк {forbidden}"


def test_reports_marker_and_key_hygiene(tmp_path, capsys) -> None:
    """Маркер reports-list total=N (кол-во); ключ не попадает в маркеры."""
    settings = _settings(tmp_path)
    _write_probe_report(settings.reports_dir, "probe-mark00000001", dt=0.0)
    app = admin_api.create_app(settings)
    with TestClient(app) as client:
        assert client.get("/calib/reports", headers=_headers()).status_code == 200
    out = capsys.readouterr().out
    assert "[CALIB-API] reports-list total=1" in out
    assert KEY not in out
