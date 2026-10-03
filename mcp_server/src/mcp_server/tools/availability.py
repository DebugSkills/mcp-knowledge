"""Availability-движок (bibliography Ф1/Ф3b1, план §3.4).

Единый предикат доступа к blob = f(zone, status, license/public_allowed, blob exists):
- `canonical_present` — auth-free (canonical blob физически есть) — состояние стора.
- `blob_available(sha256, source, auth, exists_fn)` — per-request по ОДНОЙ Source-записи (Ф1).
- `blob_available_indexed(sha256, auth, exists_fn=…, index=…)` — least-strict агрегация
  по ВСЕМ refs из `SourceRefIndex` (Ф3b1 — чистое ядро; проводка startup/write-path — Ф3b2).
- `license=unknown` → fail-closed (метаданные есть, viewer нет).

Reason-классификация `canonical_present=false` (`blob_missing` и др., §3.4:192) —
слой ответа HTTP/ingest (Ф4): предикаты возвращают строгий bool, отдельный слой
причин в ядре НЕ вводится. `source` — dict frontmatter Source-записи; `auth` —
AuthInfo или dict с key_level.
"""

from __future__ import annotations

from ..content.source import is_public_license
from .source_ref_index import SourceRef, SourceRefIndex

# Уровни ключей с доступом к обеим зонам (subscriber — только public).
_FULL_ZONE_LEVELS = {"read", "editor", "write", "import"}


def _auth_level(auth) -> str:
    if isinstance(auth, dict):
        return auth.get("level") or auth.get("key_level") or ""
    return getattr(auth, "key_level", "") or ""


def auth_zones(auth) -> set[str]:
    """Зоны, доступные аутентифицированному ключу (subscriber → {public})."""
    level = _auth_level(auth)
    if level == "subscriber":
        return {"public"}
    if level in _FULL_ZONE_LEVELS:
        return {"public", "private"}
    # Без ключа/неизвестный уровень — только public (fail-closed).
    return {"public"}


def canonical_present(blobs: dict | None, exists_fn) -> bool:
    """canonical != null ∧ blob_exists(canonical.sha256) — auth-free.

    Args:
        blobs: frontmatter.blobs (dict с ключом canonical).
        exists_fn: callable(sha256) -> bool (обычно store.exists).
    """
    canonical = (blobs or {}).get("canonical")
    if not isinstance(canonical, dict):
        return False
    sha256 = canonical.get("sha256")
    if not sha256:
        return False
    return bool(exists_fn(sha256))


def source_accessible(source: dict, auth) -> bool:
    """level_a: зона/статус ЗАПИСИ (Source) — auth-зависимая доступность метаданных."""
    zone = source.get("zone", "private")
    status = source.get("status", "published")
    return zone in auth_zones(auth) and status != "deprecated"


def _ref_from_source(source: dict) -> SourceRef:
    """SourceRef из frontmatter-dict ОДНОЙ Source-записи (Ф1-путь, без shas)."""
    return SourceRef(
        source_id=str(source.get("source_id") or source.get("knowledge_id") or ""),
        zone=source.get("zone", "private"),
        status=source.get("status", "published"),
        public_allowed=source.get("public_allowed"),
        license=source.get("license"),
    )


def ref_available(ref: SourceRef, auth) -> bool:
    """∃-компонент §3.4:167-169 — ОДИН ref даёт доступ к blob.

    zone ∈ zones(auth) ∧ status ≠ deprecated (SSOT) ∧
    (zone=public → public_allowed ∧ license ∈ {own, cc-*, licensed}).
    license=unknown/restricted/None в public-зоне → отказ (fail-closed, О-3).
    """
    if ref.zone not in auth_zones(auth):
        return False
    if ref.status == "deprecated":
        return False
    if ref.zone == "public":
        if ref.public_allowed is False:
            return False
        if not is_public_license(ref.license):
            return False  # unknown/restricted/None → fail-closed
    return True


def blob_available(sha256: str, source: dict, auth, exists_fn) -> bool:
    """level_b по ОДНОЙ Source-записи (Ф1-сигнатура): exists ∧ ref_available.

    Делегирует общему ядру `ref_available` — семантика single-source и
    индексированной агрегации не расходятся (одни и те же гейты).
    """
    if not exists_fn(sha256):
        return False
    return ref_available(_ref_from_source(source), auth)


def blob_available_indexed(sha256: str, auth, *, exists_fn, index: SourceRefIndex) -> bool:
    """level_b по индексу (§3.4:164-170): blob_exists ∧ ∃ ref: ref_available.

    Шаги канона: (1) blob_exists — integrity-приоритет, нет blob → недоступно;
    (2) refs = index[sha256]; (3) ∃ ref, проходящий зону/статус/license-гейт.
    Least-strict: blob с public- и private-refs доступен по public только при
    разрешающем license; удаление единственного public-ref → немедленный отказ.
    """
    if not exists_fn(sha256):
        return False
    return any(ref_available(ref, auth) for ref in index.get(sha256))
