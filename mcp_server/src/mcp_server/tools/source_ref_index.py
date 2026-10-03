"""SourceRefIndex — in-memory индекс sha256 → Source-refs (bibliography Ф3b1, план §3.4:164-173).

Ядро availability-агрегации: для sha256 быстро найти ВСЕ Source-записи
(content_type="source"), ссылающиеся на blob. Предикаты доступа (least-strict
зоны, license fail-closed) — в tools/availability.py (`ref_available`,
`blob_available_indexed`); здесь — только структура и наполнение.

Наполнение: startup-скан SSOT + ведение write-path'ом — проводка в Ф3b2; ядро
принимает уже прочитанные entries (KnowledgeEntry) без какого-либо IO.

Потокобезопасность: кодовая база — async single-threaded (single worker,
config.py:155-161). Все мутации индекса выполняются на event-loop потоке,
внутренние замки НЕ нужны; при многопоточном доступе потребовалась бы внешняя
синхронизация (не планируется). `rescan` атомарен: новые отображения строятся
полностью в локальных словарях и ребиндятся одним присваиванием — частичных
состояний на event-loop потоке не возникает.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceRef:
    """Immutable-снимок Source-записи для availability (§3.4:167-169).

    Поля — ИЗ SSOT frontmatter (НЕ payload, §3.6:225): зона/статус/лицензия.
    `shas` — все blob-ссылки записи (original + canonical + derived), по которым
    индекс отображает этот ref.
    """

    source_id: str
    zone: str = "private"
    status: str = "published"
    public_allowed: bool | None = None
    license: str | None = None
    shas: tuple[str, ...] = ()


def _blob_shas(blobs: dict | None) -> tuple[str, ...]:
    """Дедуплицированные sha256 из blobs {original, canonical, derived[]} (порядок сохранён)."""
    if not isinstance(blobs, dict):
        return ()
    seen: dict[str, None] = {}
    candidates = [blobs.get("original"), blobs.get("canonical")]
    candidates.extend(d for d in (blobs.get("derived") or []) if isinstance(d, dict))
    for blob in candidates:
        if isinstance(blob, dict):
            sha = blob.get("sha256")
            if isinstance(sha, str) and sha:
                seen.setdefault(sha, None)
    return tuple(seen)


def ref_from_entry(entry) -> SourceRef | None:
    """SourceRef из SSOT entry (KnowledgeEntry); None → запись не Source / без id.

    Все поля читаются из frontmatter (SSOT-истина). Не-Source записи
    (content_type ≠ "source") не индексируются (INDEX_EXCLUDED, §3.5).
    """
    fm = getattr(entry, "frontmatter", None)
    if fm is None:
        return None
    if getattr(fm, "content_type", None) != "source":
        return None
    source_id = getattr(fm, "knowledge_id", None)
    if not source_id:
        return None
    return SourceRef(
        source_id=source_id,
        zone=getattr(fm, "zone", None) or "private",
        status=getattr(fm, "status", None) or "published",
        public_allowed=getattr(fm, "public_allowed", None),
        license=getattr(fm, "license", None),
        shas=_blob_shas(getattr(fm, "blobs", None)),
    )


class SourceRefIndex:
    """sha256 → list[SourceRef]: in-memory ref-индекс для least-strict availability."""

    def __init__(self) -> None:
        # sha -> {source_id: ref} — bucket-словарь: O(1) upsert/remove, порядок вставки.
        self._by_sha: dict[str, dict[str, SourceRef]] = {}
        # source_id -> {sha} — обратное отображение для точечного add/remove.
        self._shas_of: dict[str, set[str]] = {}

    @classmethod
    def build(cls, entries) -> "SourceRefIndex":
        """Построить индекс из SSOT entries (полный скан; вызов сканера — Ф3b2)."""
        index = cls()
        index.rescan(entries)
        return index

    def rescan(self, entries) -> None:
        """Атомарная замена содержимого полным сканом entries (см. docstring модуля)."""
        by_sha: dict[str, dict[str, SourceRef]] = {}
        shas_of: dict[str, set[str]] = {}
        for entry in entries:
            ref = ref_from_entry(entry)
            if ref is None:
                continue
            shas_of[ref.source_id] = set(ref.shas)
            for sha in ref.shas:
                by_sha.setdefault(sha, {})[ref.source_id] = ref
        self._by_sha = by_sha
        self._shas_of = shas_of

    def add(self, ref: SourceRef) -> None:
        """Добавить/обновить ref (upsert по source_id) — write-path Ф3b2.

        Повторный add того же source_id заменяет ref во всех ключах и вычищает
        shas, которые запись больше не ссылает (attach/reimport-in-place).
        """
        for sha in self._shas_of.get(ref.source_id, set()) - set(ref.shas):
            bucket = self._by_sha.get(sha)
            if bucket is not None:
                bucket.pop(ref.source_id, None)
                if not bucket:
                    del self._by_sha[sha]
        for sha in ref.shas:
            self._by_sha.setdefault(sha, {})[ref.source_id] = ref
        if ref.shas:
            self._shas_of[ref.source_id] = set(ref.shas)
        else:
            self._shas_of.pop(ref.source_id, None)

    def remove(self, source_id: str) -> int:
        """Снять все refs записи (write-path: delete/cascade — Ф3b2).

        Returns:
            Число удалённых (sha, ref)-пар (0 — записи в индексе не было).
        """
        removed = 0
        for sha in self._shas_of.pop(source_id, set()):
            bucket = self._by_sha.get(sha)
            if bucket is not None:
                if bucket.pop(source_id, None) is not None:
                    removed += 1
                if not bucket:
                    del self._by_sha[sha]
        return removed

    def get(self, sha256: str) -> list[SourceRef]:
        """Все refs, ссылающиеся на blob (§3.4:166). Список-копия; сами refs immutable."""
        bucket = self._by_sha.get(sha256)
        return list(bucket.values()) if bucket else []

    @property
    def referenced_shas(self) -> frozenset[str]:
        """Все sha, на которые ссылается ≥1 Source (orphan-sweep / documents_stats)."""
        return frozenset(self._by_sha)

    def __len__(self) -> int:
        """Число различных проиндексированных blob (sha256-ключей)."""
        return len(self._by_sha)

    def size(self) -> dict[str, int]:
        """Телеметрия наполнения: blobs — distinct sha256, refs — всего (sha, ref)-пар."""
        return {"blobs": len(self._by_sha), "refs": sum(len(b) for b in self._by_sha.values())}

    @property
    def sources(self) -> int:
        """Число проиндексированных Source-записей (distinct source_id; Ф3c3-метрика)."""
        return len(self._shas_of)
