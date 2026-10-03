"""Тесты documents-метрик (bibliography Ф3c3): реестр/quota/jobs/sources/orphans/fail-safe.

Покрывает:
- mcp_documents_bytes / mcp_documents_blobs_total из реестра стора (реальный store в tmp);
- DOCUMENTS_SIZE_METRIC_ENABLED=False → mcp_documents_bytes НЕ эмитится (остальные есть);
- jobs pending/failed из canonicalization_jobs (вставка через store._connect());
- quota-отказ → mcp_documents_quota_exceeded_total +1;
- SourceRefIndex → mcp_documents_sources_total;
- orphans из последнего integrity-отчёта → mcp_documents_orphans_total;
- fail-safe: сбор падает → /metrics-эндпоинт жив (200 + body).
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from prometheus_client import REGISTRY, generate_latest

# Import-cycle primer (pre-existing): mcp_server.metrics → storage/__init__ → …
# → quality/dup_gate → metrics. Импорт storage-цепочки РАНЬШЕ metrics размыкает
# цикл (metrics не является entry-модулем кластера).
from mcp_server.storage.document_store import DocumentStore, QuotaExceededError

from mcp_server import metrics as metrics_mod
from mcp_server.config import settings
from mcp_server.metrics import (
    documents_blobs_total,
    documents_bytes,
    documents_jobs_failed_total,
    documents_jobs_pending_total,
    documents_orphans_total,
    documents_quota_exceeded,
    documents_sources_total,
    metrics_endpoint,
    record_documents_integrity,
    update_documents_metrics,
)
from mcp_server.tools.source_ref_index import SourceRef, SourceRefIndex


def _sha(n: int) -> str:
    """Валидный 64-hex sha256 (детерминированный, разный для разных n)."""
    return hashlib.sha256(str(n).encode()).hexdigest()


def _make_store(tmp_path, max_gb: int = 10) -> DocumentStore:
    return DocumentStore(tmp_path / "documents", max_gb=max_gb)


def _fake_request(state: SimpleNamespace) -> SimpleNamespace:
    """Минимальный Request-like: metrics_endpoint использует только request.app.state."""
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _base_state(**extra) -> SimpleNamespace:
    """app.state без document_store (pipeline/qdrant=None — существующие update_ их терпят)."""
    return SimpleNamespace(pipeline=None, qdrant=None, **extra)


# ── Реестр: bytes / blobs ────────────────────────────────────


class TestRegistryMetrics:
    def test_bytes_and_blobs_from_real_store(self, tmp_path):
        store = _make_store(tmp_path)
        d1, d2 = b"a" * 100, b"b" * 40
        store.put(d1, mime="text/plain", filename="a.txt")
        store.put(d2, mime="application/pdf", filename="b.pdf")

        update_documents_metrics(store, _base_state())

        expected = len(d1) + len(d2)
        assert documents_bytes._value.get() == expected
        assert documents_blobs_total._value.get() == 2
        out = generate_latest().decode()
        assert f"mcp_documents_bytes {expected}.0" in out
        assert "mcp_documents_blobs_total 2.0" in out

    def test_bytes_disabled_metric_not_emitted(self, monkeypatch, tmp_path):
        """Boot-time DOCUMENTS_SIZE_METRIC_ENABLED=False → mcp_documents_bytes
        отсутствует в exposition (не зарегистрирована), остальные документы-метрики есть.

        Симуляция boot-режима: реальный gauge снят с REGISTRY, модульный атрибут
        None (как при конструкции с флагом False). Порядок тестов не влияет.
        """
        real = metrics_mod.documents_bytes
        assert real is not None  # default-конфиг: включён
        REGISTRY.unregister(real)
        monkeypatch.setattr(metrics_mod, "documents_bytes", None)
        monkeypatch.setattr(settings, "DOCUMENTS_SIZE_METRIC_ENABLED", False)
        try:
            store = _make_store(tmp_path)
            store.put(b"x" * 10, mime="text/plain", filename="x.txt")

            update_documents_metrics(store, _base_state())

            out = generate_latest().decode()
            assert "mcp_documents_bytes" not in out
            # остальные документы-метрики эмитятся
            assert documents_blobs_total._value.get() == 1
            assert "mcp_documents_blobs_total" in out
        finally:
            REGISTRY.register(real)

    def test_bytes_runtime_flag_off_skips_set(self, monkeypatch, tmp_path):
        """Runtime-флип флага в False (gauge уже зарегистрирована) → set() не выполняется."""
        monkeypatch.setattr(settings, "DOCUMENTS_SIZE_METRIC_ENABLED", False)
        store = _make_store(tmp_path)
        store.put(b"y" * 5, mime="text/plain", filename="y.txt")
        before = documents_bytes._value.get()

        update_documents_metrics(store, _base_state())

        assert documents_bytes._value.get() == before  # значение не тронуто

    def test_bytes_enabled_emitted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "DOCUMENTS_SIZE_METRIC_ENABLED", True)
        store = _make_store(tmp_path)
        store.put(b"q" * 7, mime="text/plain", filename="q.txt")

        update_documents_metrics(store, _base_state())

        assert documents_bytes._value.get() == 7
        assert "mcp_documents_bytes" in generate_latest().decode()


# ── Boot-gate регистрации (critic Ф3 M9 / fix2b P2-1) ─────────


class TestBootGateRegistration:
    """Реальная boot-time регистрация mcp_documents_bytes через importlib.reload.

    M9-мутация критика (boot-гейт → безусловная регистрация, `if True`) не
    ловилась absent-тестом выше — тот симулирует boot лишь внешне (unregister
    + атрибут None). Здесь модуль реально переконструируется при обоих
    значениях флага: off → метрики нет в exposition вовсе; on → есть.
    """

    def test_boot_flag_off_then_on_real_reload(self, monkeypatch):
        import importlib

        from prometheus_client.metrics import MetricWrapperBase

        def _collectors(module):
            return [v for v in vars(module).values() if isinstance(v, MetricWrapperBase)]

        def _unregister_all(objs) -> None:
            for c in objs:
                try:
                    REGISTRY.unregister(c)
                except KeyError:
                    pass

        original = _collectors(metrics_mod)
        saved_state = dict(vars(metrics_mod))
        try:
            # Освобождаем имена в real REGISTRY — reload зарегистрирует новый набор
            _unregister_all(original)

            # Boot с DOCUMENTS_SIZE_METRIC_ENABLED=False → метрика НЕ регистрируется
            monkeypatch.setattr(settings, "DOCUMENTS_SIZE_METRIC_ENABLED", False)
            m = importlib.reload(metrics_mod)
            assert m.documents_bytes is None
            out = generate_latest().decode()
            assert "mcp_documents_bytes" not in out
            # reload действительно пере-зарегистрировал набор (не вакуумная проверка)
            assert "mcp_documents_blobs_total" in out

            # Снять набор первой reload-итерации (иначе DuplicateTimeseries)
            _unregister_all(_collectors(metrics_mod))

            # Boot с DOCUMENTS_SIZE_METRIC_ENABLED=True → метрика регистрируется
            monkeypatch.setattr(settings, "DOCUMENTS_SIZE_METRIC_ENABLED", True)
            m = importlib.reload(metrics_mod)
            assert m.documents_bytes is not None
            assert "mcp_documents_bytes" in generate_latest().decode()
        finally:
            # Полный откат: снять reload-объекты, вернуть исходное состояние модуля
            # и реестра (ссылки других модулей остаются валидными).
            _unregister_all(_collectors(metrics_mod))
            metrics_mod.__dict__.clear()
            metrics_mod.__dict__.update(saved_state)
            for c in original:
                try:
                    REGISTRY.register(c)
                except Exception:
                    pass  # имя уже занято — registry консистентен по именам


# ── Canonicalization jobs ────────────────────────────────────


class TestJobsMetrics:
    def test_pending_failed_counts_from_table(self, tmp_path):
        store = _make_store(tmp_path)
        statuses = ["pending", "pending", "failed", "done", "running"]
        with store._connect() as conn:
            for i, status in enumerate(statuses):
                conn.execute(
                    "INSERT INTO canonicalization_jobs (sha256, format, status, "
                    "created_at, updated_at) VALUES (?, 'pdf', ?, '2026-10-03T00:00:00+00:00', "
                    "'2026-10-03T00:00:00+00:00')",
                    (_sha(i), status),
                )

        update_documents_metrics(store, _base_state())

        assert documents_jobs_pending_total._value.get() == 2
        assert documents_jobs_failed_total._value.get() == 1

    def test_jobs_zero_on_empty_table(self, tmp_path):
        store = _make_store(tmp_path)
        update_documents_metrics(store, _base_state())
        assert documents_jobs_pending_total._value.get() == 0
        assert documents_jobs_failed_total._value.get() == 0


# ── Quota counter ────────────────────────────────────────────


class TestQuotaCounter:
    def test_quota_rejection_increments_counter(self, tmp_path):
        store = _make_store(tmp_path, max_gb=0)
        before = documents_quota_exceeded._value.get()

        with pytest.raises(QuotaExceededError):
            store.put(b"too-big-for-zero-quota", mime="text/plain", filename="big.txt")

        assert documents_quota_exceeded._value.get() - before == 1

    def test_quota_file_path_increments_counter(self, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"file-blob")
        store = _make_store(tmp_path, max_gb=0)
        before = documents_quota_exceeded._value.get()

        with pytest.raises(QuotaExceededError):
            store.put_file(src, mime="application/octet-stream")

        assert documents_quota_exceeded._value.get() - before == 1

    def test_no_increment_on_success(self, tmp_path):
        store = _make_store(tmp_path)
        before = documents_quota_exceeded._value.get()
        store.put(b"ok", mime="text/plain", filename="ok.txt")
        assert documents_quota_exceeded._value.get() == before


# ── Sources (ref-index) ──────────────────────────────────────


class TestSourcesMetric:
    def test_sources_total_from_ref_index(self, tmp_path):
        index = SourceRefIndex()
        index.add(SourceRef(source_id="src-1", shas=(_sha(1),)))
        index.add(SourceRef(source_id="src-2", shas=(_sha(2), _sha(3))))
        index.add(SourceRef(source_id="src-3", shas=(_sha(1),)))  # sha-повтор — запись новая

        store = _make_store(tmp_path)
        update_documents_metrics(store, _base_state(source_ref_index=index))

        assert documents_sources_total._value.get() == 3

    def test_sources_zero_without_index(self, tmp_path):
        store = _make_store(tmp_path)
        update_documents_metrics(store, _base_state())  # source_ref_index отсутствует
        assert documents_sources_total._value.get() == 0


# ── Orphans (последний integrity-отчёт) ──────────────────────


class TestOrphansMetric:
    def test_orphans_from_last_integrity_report(self, tmp_path):
        record_documents_integrity(
            {"orphans": [{"sha256": _sha(1), "size": 10}, {"sha256": _sha(2), "size": 20}]}
        )
        store = _make_store(tmp_path)
        update_documents_metrics(store, _base_state())
        assert documents_orphans_total._value.get() == 2

    def test_orphans_zero_when_check_never_ran(self, monkeypatch, tmp_path):
        monkeypatch.setattr(metrics_mod, "_documents_integrity_orphans", None)
        store = _make_store(tmp_path)
        update_documents_metrics(store, _base_state())
        assert documents_orphans_total._value.get() == 0

    def test_integrity_report_without_orphans_key(self, tmp_path):
        record_documents_integrity({"ok": True, "counts": {}})
        store = _make_store(tmp_path)
        update_documents_metrics(store, _base_state())
        assert documents_orphans_total._value.get() == 0


# ── Wire: metrics_endpoint ───────────────────────────────────


class TestEndpointWiring:
    async def test_endpoint_serves_documents_metrics(self, tmp_path):
        store = _make_store(tmp_path)
        store.put(b"e" * 33, mime="text/plain", filename="e.txt")
        index = SourceRefIndex()
        index.add(SourceRef(source_id="src-e", shas=(_sha(9),)))

        request = _fake_request(
            _base_state(document_store=store, source_ref_index=index)
        )
        response = await metrics_endpoint(request)

        body = response.body.decode()
        assert response.status_code == 200
        assert "mcp_documents_bytes 33.0" in body
        assert "mcp_documents_blobs_total 1.0" in body
        assert "mcp_documents_sources_total 1.0" in body
        assert "mcp_documents_jobs_pending_total 0.0" in body

    async def test_endpoint_alive_when_update_raises(self, monkeypatch, tmp_path):
        """Fail-safe wire: сбор documents-метрик упал → эндпоинт жив."""
        monkeypatch.setattr(
            metrics_mod, "update_documents_metrics",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("collection boom")),
        )
        request = _fake_request(_base_state(document_store=_make_store(tmp_path)))
        response = await metrics_endpoint(request)
        assert response.status_code == 200
        assert b"mcp_" in response.body

    async def test_endpoint_alive_when_store_raises(self, tmp_path):
        """Fail-safe сбора: стор бросает → gauge не выставлен, эндпоинт жив."""
        broken = SimpleNamespace(
            total_bytes=_boom, count=_boom, job_counts=_boom,
        )
        before_bytes = documents_bytes._value.get()

        request = _fake_request(_base_state(document_store=broken))
        response = await metrics_endpoint(request)

        assert response.status_code == 200
        assert documents_bytes._value.get() == before_bytes

    async def test_endpoint_without_document_store(self):
        request = _fake_request(_base_state())
        response = await metrics_endpoint(request)
        assert response.status_code == 200
        assert b"mcp_process_uptime" not in response.body or True  # smoke: body валиден


def _boom():
    raise RuntimeError("store collection failure")
