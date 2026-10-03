"""update_source — курирование Source-метаданных (bibliography Ф3b2, план §3.6).

Отдельного update_source-инструмента до Ф3b2 не существовало (grep-факт):
license/public_allowed менялись только пересозданием записи. Здесь —
SSOT-обновление метаданных Source (MarkdownStore.update metadata-путь, тот же,
что у set_zone) + НЕМЕДЛЕННАЯ инвалидация availability-индекса (рескан):
license/public_allowed — гейты `ref_available` (fail-closed О-3), задержка
обновления кеша = ложный доступ/ложный отказ.

Зона НЕ параметризована (принципиально): смена зоны — set_zone
(tools/admin.py) с полной каскадной семантикой W1.7; здесь только
контентно-лицензионное курирование.

MCP/HTTP-проводка инструмента — Ф4 (новых поверхностей в Ф3b2 нет).

Пайплайн записи (SSOT-first, по образцу set_zone):
1. store.read → запись существует ∧ content_type=="source";
2. метаданные: license и/или public_allowed (оба None → отказ);
   public_allowed не задан при смене license → ПЕРЕВЫВОДИТСЯ из license
   (ingest-семантика register_source: cc-* → True, restricted → False);
3. store.update(metadata=…) — frontmatter + git-коммит;
4. refresh_source_ref_index(app_state) — индекс из SSOT немедленно;
5. data_version += 1 + audit(action="update_source").
"""

from __future__ import annotations

import logging

from ..content.source import is_public_license
from ..quality.audit import write_audit
from .source_ref_runtime import refresh_source_ref_index

logger = logging.getLogger("mcp_knowledge.tools.source_ops")


async def update_source(
    app_state,
    source_id: str,
    *,
    license: str | None = None,
    public_allowed: bool | None = None,
    reason: str | None = None,
) -> dict:
    """Обновить license/public_allowed Source-записи (SSOT + availability-индекс).

    Args:
        app_state: состояние приложения (store, source_ref_index, data_version).
        source_id: id Source-записи (src-<sha256_16>).
        license: новое значение license (own | cc-* | licensed | restricted | …).
        public_allowed: явное значение; None при смене license → перевывод
            из license (ingest-семантика).
        reason: причина (audit.jsonl).

    Returns:
        {knowledge_id, license, public_allowed, version, ok: True} |
        {error: …} — запись не найдена / не Source / нечего обновлять.
    """
    store = getattr(app_state, "store", None)
    if store is None or not hasattr(store, "read"):
        return {"error": "SSOT store unavailable"}

    if not source_id:
        return {"error": "Missing required parameter: 'source_id'"}

    entry = await store.read(source_id)
    if entry is None:
        return {"error": f"Knowledge entry not found: '{source_id}'"}
    if getattr(entry.frontmatter, "content_type", None) != "source":
        return {
            "error": (
                f"Not a Source record (content_type="
                f"{getattr(entry.frontmatter, 'content_type', None)!r}): {source_id}"
            ),
        }

    metadata: dict = {}
    if license is not None:
        metadata["license"] = license
        if public_allowed is None:
            public_allowed = is_public_license(license)  # ingest-семантика
    if public_allowed is not None:
        metadata["public_allowed"] = public_allowed
    if not metadata:
        return {"error": "Nothing to update: pass license and/or public_allowed"}

    updated = await store.update(source_id, metadata=metadata)

    # Ф3b2: availability-индекс — немедленно из SSOT (fail-closed гейты
    # license/public_allowed не должны отдавать устаревшие решения).
    refresh_result = await refresh_source_ref_index(app_state)

    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass  # best-effort

    write_audit(
        action="update_source",
        knowledge_id=source_id,
        actor="operator",
        reason=reason or "source metadata update",
        metadata={**metadata, "index_refreshed": refresh_result.get("refreshed", False)},
    )
    logger.info(
        "update_source: %s metadata=%s (index_refreshed=%s)",
        source_id, metadata, refresh_result.get("refreshed"),
    )
    return {
        "knowledge_id": source_id,
        "license": updated.frontmatter.license,
        "public_allowed": updated.frontmatter.public_allowed,
        "version": updated.frontmatter.version,
        "index_refreshed": refresh_result.get("refreshed", False),
        "ok": True,
    }
