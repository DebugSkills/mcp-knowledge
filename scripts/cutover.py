#!/usr/bin/env python3
"""Ф6b (code-2026-10-02-bibliography): идемпотентный cutover-драйвер green-field.

Каждый шаг = зонд состояния (probe) → действие (action) → маркер ``.complete``
с checksum входа (паттерн offsite). Перезапуск с любого шага безопасен:
шаг с валидным маркером пропускается. Деструктивный шаг (снос legacy-корпуса)
НЕ выполняется при стоящем маркере, требует ``--apply`` + ``--confirm-destructive
<token>`` и валидного снапшота шага 2 (checksums сходятся).

Dry-run по умолчанию: печатает план (что будет сделано, какие пути/коллекции),
ничего не изменяет. Реальные действия — только ``--apply``.

Ядро (markers/checksums/plan_teardown/gc_on_empty/orchestration) — чистая логика
без сети; mcp_server импортируется лениво (внутри функций) — скрипт импортируем
и вне контура сервера (тесты, сухая планировка).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

__all__ = [
    "CutoverConfig",
    "CutoverDriver",
    "CutoverError",
    "CutoverRefusal",
    "checksum_tree",
    "gc_on_empty",
    "marker_valid",
    "plan_teardown",
    "read_marker",
    "sha256_text",
    "write_marker",
]

DEFAULT_DESTRUCTIVE_TOKEN = "CUTOVER-CONFIRM"


class CutoverError(Exception):
    """Базовая ошибка драйвера (план/предусловие/GC-аномалия)."""


class CutoverRefusal(CutoverError):
    """Отказ выполнить деструктивный шаг (нет снапшота / confirm)."""


# ── Маркеры + checksums ────────────────────────────────────────


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def checksum_tree(path: Path) -> str:
    """Детерминированный хеш дерева: sorted относительные пути + sha256 контента."""
    if not path.exists():
        return sha256_text(f"<missing:{path}>")
    if path.is_file():
        return _hash_file(path)
    entries = []
    for p in sorted(path.rglob("*")):
        if p.is_file():
            entries.append(f"{p.relative_to(path)}:{_hash_file(p)}")
    return sha256_text("\n".join(entries))


def read_marker(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(path: Path, input_hash: str, **meta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "input_hash": input_hash,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **meta,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def marker_valid(path: Path, input_hash: str) -> bool:
    m = read_marker(path)
    return bool(m) and m.get("input_hash") == input_hash


# ── Конфиг ─────────────────────────────────────────────────────


@dataclass
class CutoverConfig:
    """Пути + внешние команды. Команды по умолчанию None — реальные сетевые/make-
    действия обязаны быть явно заданы (тесты передают записывающие фейки)."""

    knowledge_dir: Path
    documents_dir: Path
    backup_dir: Path
    run_dir: Path
    pdf_cache_dir: Path
    quality_dir: Path
    dlq_dir: Path
    qdrant_url: str = "http://localhost:6333"
    qdrant_collections: list[str] = field(default_factory=lambda: ["knowledge", "knowledge_public"])
    qdrant_aliases: list[str] = field(default_factory=lambda: ["knowledge_private"])
    destructive_token: str = DEFAULT_DESTRUCTIVE_TOKEN

    # Внешние действия (None = не сконфигурировано; apply-шаг упадёт).
    stop_cmd: Optional[Callable[[], None]] = None
    deploy_cmd: Optional[Callable[[], None]] = None
    reindex_cmd: Optional[Callable[[], None]] = None
    smoke_cmd: Optional[Callable[[], dict]] = None
    import_pilot_cmd: Optional[Callable[[], None]] = None
    snapshot_qdrant: Optional[Callable[["CutoverConfig"], None]] = None
    clear_qdrant: Optional[Callable[["CutoverConfig"], None]] = None


# ── Интеграция с mcp_server (ленивые импорты) ──────────────────


def _iter_blob_refs(blobs) -> Iterable[tuple[str, dict]]:
    if not isinstance(blobs, dict):
        return
    for key in ("original", "canonical"):
        ref = blobs.get(key)
        if isinstance(ref, dict):
            yield key, ref
    for ref in blobs.get("derived") or []:
        if isinstance(ref, dict):
            yield "derived", ref


class _ReadOnlyDocumentStore:
    """Read-only «документ-стор» над каталогом блобов (без создания registry/dirs).

    Реализует ровно те 3 метода, что нужны integrity-ядру (exists/_shard_path/
    _iter_blob_files) — чтобы dry-run/probe НЕ мутировали filesystem.
    """

    def __init__(self, root: Path):
        self._root = Path(root)

    def _shard_path(self, sha256: str) -> Path:
        return self._root / sha256[0:2] / sha256[2:4] / sha256

    def exists(self, sha256: str) -> bool:
        return self._shard_path(sha256).is_file()

    def _iter_blob_files(self) -> Iterable[Path]:
        for path in self._root.rglob("*"):
            if path.is_file():
                yield path


def _parse_entries_readonly(knowledge_dir: Path) -> list:
    """Read-only парс .md (staticmethod _parse_text; без создания MarkdownStore/.trash)."""
    from mcp_server.storage.markdown_store import MarkdownStore

    entries = []
    for p in _scan_md_files(knowledge_dir):
        try:
            entries.append(MarkdownStore._parse_text(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — битые файлы пропускаем
            continue
    return entries


def plan_teardown(entries, document_store) -> tuple[set[str], set[str], dict]:
    """Разделить ВСЕ записи корпуса на keep и teardown (F-1, план §6:353).

    keep = Source-записи с ≥1 физически живым blob ∧ «зелёным» integrity
    (нет ни одного дефекта, ссылающегося на их source_id: missing_blob /
    sha_mismatch / canonical_missing / provenance_incomplete / errors).

    teardown = все прочие записи: битые Source (нет blob / дефект integrity)
    И non-Source legacy (книги/collection/pdf-секции/…). Детерминизм — через
    реальную `check_documents` (тот же SSOT-источник, что и контур).

    Записи без knowledge_id — вне классификации (не удаляются по id).
    """
    from mcp_server.tools.documents_integrity import check_documents

    report = check_documents(entries, document_store, create_issues=False)
    defective: set[str] = set()
    for key in (
        "missing_blob",
        "sha_mismatch",
        "canonical_missing",
        "provenance_incomplete",
        "errors",
    ):
        for item in report.get(key) or []:
            sid = item.get("source_id")
            if isinstance(sid, str) and sid:
                defective.add(sid)

    keep: set[str] = set()
    teardown: set[str] = set()
    for entry in entries:
        fm = getattr(entry, "frontmatter", None)
        sid = getattr(fm, "knowledge_id", None) or ""
        if not sid:
            continue  # запись без id — вне классификации (не удаляется по id)
        if getattr(fm, "content_type", None) != "source":
            teardown.add(sid)  # non-Source legacy → снос
            continue
        has_live_blob = False
        for _kind, ref in _iter_blob_refs(getattr(fm, "blobs", None)):
            sha = ref.get("sha256")
            if isinstance(sha, str) and sha and document_store.exists(sha):
                has_live_blob = True
                break
        if has_live_blob and sid not in defective:
            keep.add(sid)
        else:
            teardown.add(sid)
    return keep, teardown, report


def gc_on_empty(document_store, index=None, grace_days: int = 30) -> dict:
    """Шаг 7 (F-2): GC-проход после сноса → инвариант ``deleted == 0``.

    Инвариант шага 7 — **0 удалений** (GC никогда не трогает referenced-блобы).
    ``orphans`` = физические блобы без Source-ref, но в пределах grace — это
    НОРМА после сноса корпуса (kept-Source блобы при ``index=None`` не видны
    как referenced); они сообщаются числом, не роняют шаг.

    Фатально ТОЛЬКО ``_gc_sweep``-кандидаты на фактическое удаление
    (unreferenced ∧ старше grace) — то, что GC реально снёс бы: это аномалия
    (остался неучтённый старый блоб) → CutoverError (стоп).
    """
    from mcp_server.tools.documents_admin import _gc_sweep
    from mcp_server.tools.documents_integrity import orphan_list_from, physical_blobs

    physical = physical_blobs(document_store)
    referenced = set(index.referenced_shas) if index is not None else set()
    orphans = orphan_list_from(physical, referenced)
    sweep = _gc_sweep(document_store, index, grace_days, time.time())
    if sweep["candidates"]:
        raise CutoverError(
            "GC после сноса: ожидалось 0 удалений, но есть sweep-кандидаты "
            f"(unreferenced ∧ старше grace): {len(sweep['candidates'])}"
        )
    return {
        "deleted": 0,
        "orphans": len(orphans),
        "candidates": 0,
        "physical": len(physical),
    }


# ── Драйвер ────────────────────────────────────────────────────


class CutoverDriver:
    """Шаги 2-8 плана §6. Каждый шаг: probe → action → marker."""

    def __init__(self, cfg: CutoverConfig):
        self.cfg = cfg

    # -- helpers -------------------------------------------------

    def _marker(self, step_no: int) -> Path:
        return self.cfg.run_dir / f"{step_no:02d}.complete"

    def _snapshot_marker_valid(self) -> bool:
        return marker_valid(self._marker(2), self._snapshot_input_hash())

    def _snapshot_input_hash(self) -> str:
        return sha256_text(
            checksum_tree(self.cfg.knowledge_dir)
            + "|"
            + checksum_tree(self.cfg.documents_dir)
        )

    def _cmd(self, name: str, cmd: Optional[Callable]) -> Callable:
        if cmd is None:
            raise CutoverError(f"external command not configured: {name}")
        return cmd

    # -- steps ---------------------------------------------------

    def _probe_snapshot(self):
        ih = self._snapshot_input_hash()
        docs_nonempty = self.cfg.documents_dir.exists() and any(self.cfg.documents_dir.iterdir())
        plan = (
            f"git bundle + tar of knowledge/ → {self.cfg.backup_dir}; "
            f"tar of data/documents ({'non-empty' if docs_nonempty else 'empty→skip'}); "
            f"Qdrant snapshot {self.cfg.qdrant_collections} @ {self.cfg.qdrant_url}; checksums"
        )
        return ih, plan

    def _action_snapshot(self):
        cfg = self.cfg
        cfg.backup_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[Path] = []
        bundle = cfg.backup_dir / "knowledge.bundle"
        subprocess.run(
            ["git", "-C", str(cfg.knowledge_dir), "bundle", "create", str(bundle), "--all"],
            check=True,
        )
        artifacts.append(bundle)
        tarball = cfg.backup_dir / "knowledge.tar.gz"
        subprocess.run(
            ["tar", "-czf", str(tarball), "-C", str(cfg.knowledge_dir.parent), cfg.knowledge_dir.name],
            check=True,
        )
        artifacts.append(tarball)
        if cfg.documents_dir.exists() and any(cfg.documents_dir.iterdir()):
            dtar = cfg.backup_dir / "documents.tar.gz"
            subprocess.run(
                ["tar", "-czf", str(dtar), "-C", str(cfg.documents_dir.parent), cfg.documents_dir.name],
                check=True,
            )
            artifacts.append(dtar)
        self._cmd("snapshot_qdrant", cfg.snapshot_qdrant)(cfg)
        lines = [f"{_hash_file(a)}  {a.name}" for a in sorted(artifacts, key=lambda p: p.name)]
        (cfg.backup_dir / "checksums.sha256").write_text("\n".join(lines) + "\n")

    def _probe_stop(self):
        return sha256_text("stop-v1"), "make prod-down (остановка стека)"

    def _action_stop(self):
        self._cmd("stop_cmd", self.cfg.stop_cmd)()

    def _teardown_input_hash(self):
        return sha256_text(
            checksum_tree(self.cfg.knowledge_dir)
            + "|"
            + checksum_tree(self.cfg.documents_dir)
            + "|"
            + ",".join(self.cfg.qdrant_collections)
            + "|"
            + ",".join(self.cfg.qdrant_aliases)
        )

    def _probe_teardown(self):
        keep, teardown, _report = self._load_sources_and_plan()
        ih = self._teardown_input_hash()
        plan = (
            f"снос legacy-корпуса: (к) {len(teardown)} SSOT-записей на удаление "
            f"(битые Source + non-Source legacy); сохранить {len(keep)} Source "
            f"(живой blob ∧ зелёный integrity); "
            f"(к2) поверхности: Qdrant-коллекции {self.cfg.qdrant_collections} + "
            f"алиасы {self.cfg.qdrant_aliases} + _v1-остатки; "
            f"pdf_cache {self.cfg.pdf_cache_dir}; quality-issues/DLQ"
        )
        return ih, plan

    def _load_sources_and_plan(self):
        entries = _parse_entries_readonly(self.cfg.knowledge_dir)
        document_store = _ReadOnlyDocumentStore(self.cfg.documents_dir)
        return plan_teardown(entries, document_store)

    def _action_teardown(self):
        keep, teardown, _report = self._load_sources_and_plan()
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=self.cfg.knowledge_dir)
        if teardown:
            import asyncio

            asyncio.run(store.delete_many(sorted(teardown), commit_message="cutover: legacy corpus teardown"))
        self._cmd("clear_qdrant", self.cfg.clear_qdrant)(self.cfg)
        for d in (self.cfg.pdf_cache_dir, self.cfg.quality_dir, self.cfg.dlq_dir):
            if d.exists():
                for child in d.iterdir():
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        _rmtree(child)

    def _probe_deploy(self):
        return sha256_text("deploy-v1"), "make push (деплой нового кода) + verify-deploy"

    def _action_deploy(self):
        self._cmd("deploy_cmd", self.cfg.deploy_cmd)()

    def _probe_reindex_smoke(self):
        return sha256_text("reindex-smoke-v1"), "пустой reindex + smoke (health/tools/list/пустой поиск/404)"

    def _action_reindex_smoke(self):
        self._cmd("reindex_cmd", self.cfg.reindex_cmd)()
        self._cmd("smoke_cmd", self.cfg.smoke_cmd)()

    def _gc_input_hash(self):
        from mcp_server.tools.documents_integrity import physical_blobs

        ds = _ReadOnlyDocumentStore(self.cfg.documents_dir)
        return sha256_text(",".join(sorted(physical_blobs(ds))))

    def _probe_gc(self):
        return self._gc_input_hash(), "GC-проход на пустом корпусе (0 удалений, orphans=0)"

    def _action_gc(self):
        from mcp_server.storage.document_store import DocumentStore

        ds = DocumentStore(self.cfg.documents_dir, max_gb=1)
        gc_on_empty(ds, index=None, grace_days=30)

    def _probe_pilot(self):
        return sha256_text("pilot-v1"), "пилотный импорт первого реального PDF (полный контур)"

    def _action_pilot(self):
        self._cmd("import_pilot_cmd", self.cfg.import_pilot_cmd)()

    # -- orchestration -------------------------------------------

    def run(
        self,
        *,
        dry_run: bool = True,
        apply: bool = False,
        confirm_destructive: Optional[str] = None,
    ) -> dict:
        """Прогон шагов 2-8. dry-run: план без изменений; apply: реальные действия."""
        results: list[dict] = []

        def step_defs():
            return [
                (2, "snapshot", False, self._probe_snapshot, self._action_snapshot),
                (3, "stop", False, self._probe_stop, self._action_stop),
                (4, "teardown", True, self._probe_teardown, self._action_teardown),
                (5, "deploy", False, self._probe_deploy, self._action_deploy),
                (6, "reindex_smoke", False, self._probe_reindex_smoke, self._action_reindex_smoke),
                (7, "gc", False, self._probe_gc, self._action_gc),
                (8, "pilot", False, self._probe_pilot, self._action_pilot),
            ]

        for step_no, name, destructive, probe, action in step_defs():
            probe_result = probe()
            ih, plan = probe_result[0], probe_result[1]
            marker = self._marker(step_no)
            entry = {"step": step_no, "name": name, "status": None, "detail": ""}

            # 1) idempotency: маркер → skip. Деструктивный шаг (снос) пропускается
            # по ФАКТУ наличия маркера (снос однократен; input_hash фиксирует ЧТО
            # снесли, но не является условием пропуска — снос сам меняет вход).
            # Не-деструктивный шаг пропускается при валидном input_hash.
            if destructive:
                if read_marker(marker) is not None:
                    entry["status"] = "skipped"
                    entry["detail"] = "маркер — деструктивный шаг уже выполнен, пропуск"
                    results.append(entry)
                    continue
            elif marker_valid(marker, ih):
                entry["status"] = "skipped"
                entry["detail"] = "marker valid — пропуск"
                results.append(entry)
                continue

            # 2) деструктивный шаг: гейты снапшота и confirm
            if destructive:
                snapshot_ok = self._snapshot_marker_valid()
                confirm_ok = confirm_destructive == self.cfg.destructive_token
                if not (snapshot_ok and confirm_ok):
                    missing = []
                    if not snapshot_ok:
                        missing.append("валидный снапшот шага 2")
                    if not confirm_ok:
                        missing.append("--confirm-destructive")
                    entry["status"] = "refused"
                    entry["detail"] = (
                        f"снос требует: {', '.join(missing)}. "
                        f"План сноса: {plan}"
                    )
                    results.append(entry)
                    if apply:
                        raise CutoverRefusal(entry["detail"])
                    continue

            # 3) dry-run: только план
            if dry_run:
                entry["status"] = "planned"
                entry["detail"] = plan
                results.append(entry)
                continue

            # 4) apply: действие + маркер
            if not apply:
                entry["status"] = "planned"
                entry["detail"] = plan
                results.append(entry)
                continue
            action()
            write_marker(marker, ih, step=step_no, name=name)
            entry["status"] = "done"
            entry["detail"] = "выполнено, маркер записан"
            results.append(entry)

        return {"dry_run": dry_run, "apply": apply, "steps": results}


# ── Утилиты FS ─────────────────────────────────────────────────


def _scan_md_files(knowledge_dir: Path) -> list[Path]:
    if not knowledge_dir.exists():
        return []
    return sorted(
        p for p in knowledge_dir.rglob("*.md")
        if ".git" not in p.parts and ".trash" not in p.parts
    )


def _rmtree(path: Path) -> None:
    for child in sorted(path.iterdir(), reverse=True):
        if child.is_dir():
            _rmtree(child)
        else:
            child.unlink()
    path.rmdir()


# ── CLI ────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ф6b: идемпотентный green-field cutover-драйвер")
    p.add_argument("--apply", action="store_true", help="реальные действия (по умолчанию dry-run)")
    p.add_argument("--confirm-destructive", metavar="TOKEN", default=None,
                   help="токен для деструктивного сноса (шаг 4)")
    p.add_argument("--run-dir", type=Path, default=None, help="каталог маркеров .complete")
    p.add_argument("--knowledge-dir", type=Path, default=None)
    p.add_argument("--documents-dir", type=Path, default=None)
    p.add_argument("--backup-dir", type=Path, default=None)
    p.add_argument("--pdf-cache-dir", type=Path, default=None)
    p.add_argument("--quality-dir", type=Path, default=None)
    p.add_argument("--dlq-dir", type=Path, default=None)
    p.add_argument("--qdrant-url", default=None)
    p.add_argument("--collections", default=None, help="csv Qdrant-коллекций")
    p.add_argument("--aliases", default=None, help="csv Qdrant-алиасов")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    import os

    root = Path(__file__).resolve().parents[1]
    data_root = Path(os.environ.get("DATA_ROOT", root / "data"))

    cfg = CutoverConfig(
        knowledge_dir=args.knowledge_dir or root / "knowledge",
        documents_dir=args.documents_dir or data_root / "documents",
        backup_dir=args.backup_dir or data_root / "backups" / "cutover",
        run_dir=args.run_dir or data_root / "cutover-run",
        pdf_cache_dir=args.pdf_cache_dir or data_root / "pdf_cache",
        quality_dir=args.quality_dir or data_root / "quality",
        dlq_dir=args.dlq_dir or data_root / "dlq",
        qdrant_url=args.qdrant_url or "http://localhost:6333",
        qdrant_collections=[c.strip() for c in (args.collections or "knowledge,knowledge_public").split(",") if c.strip()],
        qdrant_aliases=[a.strip() for a in (args.aliases or "knowledge_private").split(",") if a.strip()],
        destructive_token=os.environ.get("CUTOVER_DESTRUCTIVE_TOKEN", DEFAULT_DESTRUCTIVE_TOKEN),
    )

    dry_run = not args.apply
    driver = CutoverDriver(cfg)
    try:
        report = driver.run(
            dry_run=dry_run,
            apply=args.apply,
            confirm_destructive=args.confirm_destructive,
        )
    except CutoverRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except CutoverError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    for s in report["steps"]:
        print(f"[{s['step']}] {s['name']}: {s['status']} — {s['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
