"""Квоты participant-ролей (Ф4.1): схема, fail-closed, hot-reload, fallback.

trace_id: arch-2026-10-05-ai-workspace. Боевой quotas.yaml не мутируется:
все правки — в tmp-копии каталога реестра (_copy_registry).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_workspace.registry import Registry, RegistryError
from ai_workspace.registry.quotas import Quota, QuotaRegistry, validate_quotas

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"


def _copy_registry(tmp_path: Path) -> Path:
    """Рабочая копия реестра в tmp (мутации mtime/контента бьют только в копию)."""
    work = tmp_path / "registry"
    shutil.copytree(REGISTRY_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    return work


def _valid_doc() -> dict[str, Any]:
    """Минимальный валидный документ квот (зеркало боевой схемы)."""
    return {
        "version": 1,
        "defaults": {"role": "guest"},
        "participants": {
            "admin": {
                "priority": "high",
                "tokens_per_day": None,
                "conc": None,
                "grants": ["heavy", "fast", "local-only"],
            },
            "member": {
                "priority": "med",
                "tokens_per_day": 200000,
                "conc": 2,
                "grants": ["heavy", "fast", "local-only"],
            },
            "guest": {
                "priority": "low",
                "tokens_per_day": 50000,
                "conc": 1,
                "grants": ["fast", "local-only"],
            },
        },
        "budgets": {
            "ext": {
                "currency": "RUB",
                "period": "month",
                "limit": 3000,
                "per_user_mirror": True,
                "reconcile": "nightly",
            }
        },
    }


def _write_quotas(work: Path, doc: dict[str, Any]) -> None:
    """Перезаписать quotas.yaml в tmp-копии + сдвиг mtime (гранулярность ФС)."""
    path = work / "quotas.yaml"
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _quota_error(tmp_path: Path, mutate) -> str:
    """Текст RegistryError для документа, испорченного мутацией ``mutate``."""
    work = _copy_registry(tmp_path)
    doc = _valid_doc()
    mutate(doc)
    _write_quotas(work, doc)
    book = QuotaRegistry(Registry(work))
    with pytest.raises(RegistryError) as excinfo:
        book.quota_for("member")
    return str(excinfo.value)


# --- 1. Боевой реестр грузится, маппинги верны -------------------------------


def test_live_registry_loads() -> None:
    book = QuotaRegistry(Registry(REGISTRY_DIR))
    member = book.quota_for("member")
    assert member.conc == 2
    assert member.priority == "med"
    assert member.tokens_per_day == 200000
    assert set(member.grants) == {"heavy", "fast", "local-only"}

    admin = book.quota_for("admin")
    assert admin.priority == "high"
    assert admin.tokens_per_day is None  # только общий GPU-бюджет K
    assert admin.conc is None  # только общий K файла-очереди

    guest = book.quota_for("guest")
    assert guest.priority == "low"
    assert guest.conc == 1
    assert "heavy" not in guest.grants  # local-полка: ext-класс не выдан

    assert set(book.budgets) == {"ext"}
    ext = book.budgets["ext"]
    assert (ext.currency, ext.limit, ext.period) == ("RUB", 3000, "month")
    assert ext.per_user_mirror is True
    assert ext.reconcile == "nightly"


def test_valid_doc_has_no_findings() -> None:
    reg = Registry(REGISTRY_DIR)
    assert validate_quotas(reg.get("quotas"), reg.get("model_classes")) == []


def test_quota_is_frozen() -> None:
    book = QuotaRegistry(Registry(REGISTRY_DIR))
    with pytest.raises(dataclasses.FrozenInstanceError):
        book.quota_for("member").conc = 5  # type: ignore[misc]


def test_budgets_readonly() -> None:
    book = QuotaRegistry(Registry(REGISTRY_DIR))
    with pytest.raises(TypeError):
        book.budgets["ext"] = book.budgets["ext"]  # type: ignore[index]


# --- 2. Fail-closed по каждому пункту схемы ----------------------------------


def test_fail_closed_bad_priority(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["participants"]["member"]["priority"] = "urgent"

    msg = _quota_error(tmp_path, mutate)
    assert "priority" in msg and "urgent" in msg


def test_fail_closed_negative_tokens(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["participants"]["member"]["tokens_per_day"] = -1

    assert "tokens_per_day" in _quota_error(tmp_path, mutate)


def test_fail_closed_bool_tokens(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["participants"]["member"]["tokens_per_day"] = True

    assert "tokens_per_day" in _quota_error(tmp_path, mutate)


def test_fail_closed_zero_conc(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["participants"]["member"]["conc"] = 0

    assert "conc" in _quota_error(tmp_path, mutate)


def test_fail_closed_missing_participant_field(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        del doc["participants"]["member"]["conc"]

    assert "conc" in _quota_error(tmp_path, mutate)


def test_fail_closed_grant_unknown_class(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        # local/ext — ПОЛКИ LiteLLM, а не классы: реалистичная ошибка ref-цели
        doc["participants"]["guest"]["grants"] = ["local", "ext"]

    msg = _quota_error(tmp_path, mutate)
    assert "grants" in msg and "local" in msg and "ext" in msg


def test_fail_closed_empty_grants(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["participants"]["member"]["grants"] = []

    assert "grants" in _quota_error(tmp_path, mutate)


def test_fail_closed_unlimited_defaults_role(tmp_path: Path) -> None:
    """Q14 (P2-1 критики Ф4): фолбэк без личных лимитов (defaults.role со
    стороны admin: tokens/conc = null) — эскалация неизвестных ролей;
    least-privilege запрещает."""

    def mutate(doc: dict[str, Any]) -> None:
        doc["defaults"]["role"] = "admin"  # admin: tokens_per_day/conc null

    msg = _quota_error(tmp_path, mutate)
    assert "Q14" in msg and "least-privilege" in msg


def test_fail_closed_defaults_role_missing(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["defaults"]["role"] = "bot"

    msg = _quota_error(tmp_path, mutate)
    assert "defaults.role" in msg and "bot" in msg


def test_fail_closed_zero_limit(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["budgets"]["ext"]["limit"] = 0

    assert "limit" in _quota_error(tmp_path, mutate)


def test_fail_closed_bad_currency(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["budgets"]["ext"]["currency"] = "EUR"

    assert "currency" in _quota_error(tmp_path, mutate)


def test_fail_closed_empty_reconcile(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["budgets"]["ext"]["reconcile"] = ""

    assert "reconcile" in _quota_error(tmp_path, mutate)


def test_fail_closed_missing_budgets(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        del doc["budgets"]

    assert "budgets" in _quota_error(tmp_path, mutate)


def test_fail_closed_bad_version(tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["version"] = 0

    assert "version" in _quota_error(tmp_path, mutate)


def test_fail_closed_not_a_mapping(tmp_path: Path) -> None:
    work = _copy_registry(tmp_path)
    (work / "quotas.yaml").write_text("- просто\n- список\n", encoding="utf-8")
    book = QuotaRegistry(Registry(work))
    with pytest.raises(RegistryError):
        book.budgets  # noqa: B018 — доступ к property ради fail-closed проверки


# --- 3. Hot-reload по mtime ---------------------------------------------------


def test_hot_reload_visible(tmp_path: Path) -> None:
    work = _copy_registry(tmp_path)
    reg = Registry(work)
    book = QuotaRegistry(reg)
    assert book.quota_for("member").conc == 2

    doc = _valid_doc()
    doc["participants"]["member"]["conc"] = 3
    doc["participants"]["member"]["tokens_per_day"] = 123000
    _write_quotas(work, doc)

    assert reg.reload_if_changed() is True  # реестр перечитал каталог
    assert book.quota_for("member").conc == 3  # фасад увидел новое
    assert book.quota_for("member").tokens_per_day == 123000
    assert reg.reload_if_changed() is False  # повторно — без изменений


def test_hot_reload_autodetected_by_facade(tmp_path: Path) -> None:
    work = _copy_registry(tmp_path)
    book = QuotaRegistry(Registry(work))
    assert book.quota_for("guest").tokens_per_day == 50000

    doc = _valid_doc()
    doc["participants"]["guest"]["tokens_per_day"] = 60000
    _write_quotas(work, doc)

    # фасад сам замечает mtime-изменение при следующем доступе
    assert book.quota_for("guest").tokens_per_day == 60000


def test_hot_reload_fail_closed_on_bad_edit(tmp_path: Path) -> None:
    work = _copy_registry(tmp_path)
    book = QuotaRegistry(Registry(work))
    assert book.quota_for("member").conc == 2

    doc = _valid_doc()
    doc["participants"]["member"]["grants"] = ["ext"]  # класс не существует
    _write_quotas(work, doc)

    with pytest.raises(RegistryError) as excinfo:
        book.quota_for("member")
    assert "grants" in str(excinfo.value)


# --- 4. Неизвестная роль -> квота defaults.role -------------------------------


def test_unknown_role_falls_back_to_default() -> None:
    book = QuotaRegistry(Registry(REGISTRY_DIR))
    fallback = book.quota_for("нет-такой-роли")
    assert isinstance(fallback, Quota)
    assert fallback == book.quota_for("guest")
    assert fallback.role == "guest"  # defaults.role = guest, без исключения
