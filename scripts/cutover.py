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
    "checksum_documents",
    "checksum_knowledge",
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


def checksum_tree(
    path: Path, skip: Callable[[Path], bool] | None = None
) -> str:
    """Детерминированный хеш дерева: sorted относительные пути + sha256 контента.

    ``skip`` — опциональный предикат (Path -> bool): True = исключить файл из
    снапшота. Нужен, чтобы вывести из «входа» derived/VCS-метаданные (SQLite-
    реестр документ-стора, .git/), чьи байты меняются независимо от контента и
    потому не являются входом снапшота. По умолчанию исключений нет.
    """
    if not path.exists():
        return sha256_text(f"<missing:{path}>")
    if path.is_file():
        return _hash_file(path)
    entries = []
    for p in sorted(path.rglob("*")):
        if p.is_file():
            if skip is not None and skip(p):
                continue
            entries.append(f"{p.relative_to(path)}:{_hash_file(p)}")
    return sha256_text("\n".join(entries))


# SQLite-реестр документ-стора (derived/rebuildable, см. document_store).
_SQLITE_REGISTRY_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal")


def _is_sqlite_registry_file(p: Path) -> bool:
    """True для файлов SQLite-реестра (registry.db*): derived/rebuildable, не вход."""
    return p.name.endswith(_SQLITE_REGISTRY_SUFFIXES)


def _is_git_metadata(p: Path) -> bool:
    """True для файлов под .git/: VCS-метаданные, не контент."""
    return ".git" in p.parts


def checksum_documents(path: Path) -> str:
    """Хеш контент-адресуемых блобов documents-стора.

    Инвариант маркера = «вход не изменился». SQLite-реестр (registry.db /
    -wal / -shm / -journal) — derived/rebuildable (см. DocumentStore), его
    байты меняются между записью маркера и прогоном независимо от контента,
    поэтому НЕ входят во «вход снапшота». Вход = сами блобы
    ``data/documents/<ab>/<cd>/<sha256-64>`` — они остаются покрытыми.
    """
    return checksum_tree(path, skip=_is_sqlite_registry_file)


def _is_derived_index_file(p: Path) -> bool:
    """True для derived-индексов knowledge (`*.gen.yaml`).

    INDEX.gen.yaml / _INDEX.gen.yaml регенерируются работающим сервером
    (derived, rebuildable) и не являются входом снапшота — иначе любая
    переиндексация фонового процесса инвалидировала бы снапшот (найдено на
    Ф6c: дрейф *_INDEX.gen.yaml между снапшотом и apply).
    """
    return p.name.endswith(".gen.yaml")


def checksum_knowledge(path: Path) -> str:
    """Хеш SSOT-контента knowledge-дерева (исключая .git/ и derived *.gen.yaml)."""
    return checksum_tree(
        path, skip=lambda p: _is_git_metadata(p) or _is_derived_index_file(p)
    )


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
    qdrant_collections: list[str] = field(
        default_factory=lambda: ["knowledge_public", "knowledge_private"]
    )
    qdrant_aliases: list[str] = field(default_factory=list)
    destructive_token: str = DEFAULT_DESTRUCTIVE_TOKEN

    # C2-wiring: окружение + внешние действия.
    legacy_collections: list[str] = field(default_factory=lambda: ["knowledge_v1"])
    legacy_collection_prefixes: tuple[str, ...] = ("knowledge_e2e_",)
    compose_files: list[str] = field(default_factory=list)
    services_to_stop: list[str] = field(
        default_factory=lambda: ["mcp-server", "kb-console", "kb-console-tls"]
    )
    mcp_base_url: str = "http://localhost:8000"
    env_file: Path = Path(".env")
    pilot_pdf: Path = Path("pilot.pdf")
    pilot_domain: str = "networking"
    pilot_subject: str = "http3"
    chown_owner: str = "ladmin:ladmin"
    chown_targets: list[Path] = field(default_factory=list)

    # Внешние действия (None = не сконфигурировано; apply-шаг упадёт).
    stop_cmd: Optional[Callable[[], None]] = None
    deploy_cmd: Optional[Callable[[], None]] = None
    reindex_cmd: Optional[Callable[[], None]] = None
    smoke_cmd: Optional[Callable[[], dict]] = None
    import_pilot_cmd: Optional[Callable[[], None]] = None
    snapshot_qdrant: Optional[Callable[["CutoverConfig"], Optional[list[Path]]]] = None
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


def _has_nonempty_blob_refs(blobs) -> bool:
    """True, если у записи есть хотя бы один blob-ref с sha256 (Source «с блобами»)."""
    for _kind, ref in _iter_blob_refs(blobs):
        sha = ref.get("sha256")
        if isinstance(sha, str) and sha:
            return True
    return False


def _references_kept_source(fm, kept_sources: set[str]) -> bool:
    """True, если ``source_id`` (поле) ИЛИ ``source_refs[]`` указывают на kept-Source."""
    sid_field = getattr(fm, "source_id", None)
    if isinstance(sid_field, str) and sid_field in kept_sources:
        return True
    for ref in getattr(fm, "source_refs", None) or []:
        if not isinstance(ref, dict):
            continue
        rid = ref.get("source_id")
        if isinstance(rid, str) and rid in kept_sources:
            return True
    return False


def plan_teardown(entries, document_store) -> tuple[set[str], set[str], dict]:
    """Разделить ВСЕ записи корпуса на keep и teardown (F-1, план §6:353).

    keep =
      1) Source-записи с ≥1 физически живым blob ∧ «зелёным» integrity
         (нет ни одного дефекта по их source_id: missing_blob / sha_mismatch /
         canonical_missing / provenance_incomplete / errors);
      2) записи, чьи ``source_refs[]`` / ``source_id`` указывают на kept-Source
         (секции RFC и пр.);
      3) корневые коллекции (``content_type: collection``), чьи дети попали в keep.

    teardown = все прочие записи: битые Source (нет blob / дефект integrity)
    И non-Source legacy (книги/collection/pdf-секции/…) без kept-связей.

    Fail-closed (P1): если blob-store деградирован (0 физических блобов) при
    наличии хотя бы одного Source с непустыми refs → CutoverRefusal — отказ
    вместо «снести всё». Записи без knowledge_id — вне классификации
    (не удаляются по id). Детерминизм — через реальную ``check_documents``.
    """
    from mcp_server.tools.documents_integrity import check_documents, physical_blobs

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

    # ── Fail-closed guard: деградация blob-store → стоп до любых мутаций ──
    source_with_refs = any(
        getattr(getattr(e, "frontmatter", None), "content_type", None) == "source"
        and _has_nonempty_blob_refs(getattr(getattr(e, "frontmatter", None), "blobs", None))
        for e in entries
    )
    if source_with_refs and not physical_blobs(document_store):
        raise CutoverRefusal(
            "снос запрещён: blob-store деградирован (0 физических блобов) при "
            "наличии Source с непустыми refs — отказ вместо сноса всего корпуса"
        )

    # ── проход 1: kept Sources (живой blob ∧ зелёный) ──
    kept_sources: set[str] = set()
    for entry in entries:
        fm = getattr(entry, "frontmatter", None)
        sid = getattr(fm, "knowledge_id", None) or ""
        if not sid:
            continue
        if getattr(fm, "content_type", None) != "source":
            continue
        has_live_blob = False
        for _kind, ref in _iter_blob_refs(getattr(fm, "blobs", None)):
            sha = ref.get("sha256")
            if isinstance(sha, str) and sha and document_store.exists(sha):
                has_live_blob = True
                break
        if has_live_blob and sid not in defective:
            kept_sources.add(sid)

    keep: set[str] = set(kept_sources)

    # ── проход 2: секции со source_refs/source_id на kept-Source ──
    kept_parents: set[str] = set()
    for entry in entries:
        fm = getattr(entry, "frontmatter", None)
        sid = getattr(fm, "knowledge_id", None) or ""
        if not sid or sid in keep:
            continue
        if _references_kept_source(fm, kept_sources):
            keep.add(sid)
            parent = getattr(fm, "parent_knowledge_id", None)
            if isinstance(parent, str) and parent:
                kept_parents.add(parent)

    # ── проход 3: корневые коллекции с детьми в keep ──
    for entry in entries:
        fm = getattr(entry, "frontmatter", None)
        sid = getattr(fm, "knowledge_id", None) or ""
        if not sid or sid in keep:
            continue
        if getattr(fm, "content_type", None) == "collection" and sid in kept_parents:
            keep.add(sid)

    # ── teardown = всё с id, не попавшее в keep ──
    teardown: set[str] = set()
    for entry in entries:
        fm = getattr(entry, "frontmatter", None)
        sid = getattr(fm, "knowledge_id", None) or ""
        if sid and sid not in keep:
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



# ── Внешние действия (C2-wiring) ───────────────────────────────


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _compose_cmd(cfg: "CutoverConfig") -> list[str]:
    cmd = ["docker", "compose"]
    for f in cfg.compose_files:
        cmd += ["-f", f]
    return cmd


def _read_env_key(env_file: Path, var: str) -> str:
    """Первый ключ из .env (значение НИКОГДА не логируется/не печатается)."""
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(var + "="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            val = val.lstrip("[").rstrip("]")
            return val.split(",")[0].strip().strip('"').strip("'")
    return ""


def _http_json(method: str, url: str, payload: Optional[dict] = None, timeout: int = 120) -> dict:
    import urllib.request

    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else {}


def _download(url: str, dest: Path, timeout: int = 300) -> None:
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=timeout) as resp, dest.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)


def _mcp_rpc(base_url: str, key: str, method: str, params: dict, timeout: int = 300) -> dict:
    """Generic JSON-RPC вызов MCP (tools/list, tools/call) c X-API-Key."""
    import urllib.request

    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/mcp",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-API-Key": key,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    if raw.lstrip().startswith(("event:", "data:")):
        raw = "".join(ln[5:] for ln in raw.splitlines() if ln.startswith("data:"))
    d = json.loads(raw)
    if "error" in d:
        raise CutoverError(f"MCP {method}: {d['error']}")
    return d.get("result", d)


def _mcp_tool(base_url: str, key: str, tool: str, args: dict, timeout: int = 300) -> dict:
    res = _mcp_rpc(base_url, key, "tools/call", {"name": tool, "arguments": args}, timeout)
    if isinstance(res, dict) and "content" in res and res["content"]:
        text = res["content"][0].get("text")
        try:
            return json.loads(text or "{}")
        except ValueError:
            return {"_text": text}
    return res if isinstance(res, dict) else {"_result": res}


def _upload_file(base_url: str, key: str, path: Path) -> str:
    """POST /upload (multipart) → путь PDF на сервере."""
    import urllib.request
    import uuid

    boundary = "----cutover" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/pdf\r\n\r\n",
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        base_url.rstrip("/") + "/upload",
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-API-Key": key,
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        res = json.loads(resp.read().decode() or "{}")
    pdf_path = res.get("path") or res.get("pdf_path") or res.get("file_path")
    if not pdf_path:
        raise CutoverError(f"upload: сервер не вернул путь PDF ({sorted(res)})")
    return str(pdf_path)


def _cmd_docker_stop(cfg: "CutoverConfig") -> None:
    _run([*_compose_cmd(cfg), "stop", *cfg.services_to_stop])


def _cmd_deploy(cfg: "CutoverConfig") -> None:
    _run(["make", "deploy"])


def _cmd_reindex(cfg: "CutoverConfig") -> None:
    key = _read_env_key(cfg.env_file, "MCP_WRITE_KEYS")
    if not key:
        raise CutoverError("reindex: нет admin-ключа (MCP_WRITE_KEYS) в " + str(cfg.env_file))
    res = _mcp_tool(cfg.mcp_base_url, key, "reindex", {})
    if res.get("error"):
        raise CutoverError(f"reindex: {res['error']}")


def _cmd_smoke(cfg: "CutoverConfig") -> dict:
    health = _http_json("GET", cfg.mcp_base_url.rstrip("/") + "/health")
    status = health.get("status")
    if status not in ("healthy", "ok", "degraded"):
        raise CutoverError(f"smoke: /health status={status!r}")
    key = _read_env_key(cfg.env_file, "MCP_READ_KEYS") or _read_env_key(cfg.env_file, "MCP_WRITE_KEYS")
    tools = _mcp_rpc(cfg.mcp_base_url, key, "tools/list", {})
    names = [t.get("name") for t in (tools.get("tools") or [])]
    if len(names) < 30:
        raise CutoverError(f"smoke: tools/list вернул {len(names)} (<30)")
    return {"status": status, "tools": len(names)}


def _cmd_pilot_import(cfg: "CutoverConfig") -> None:
    if not cfg.pilot_pdf.exists():
        raise CutoverError(f"pilot: PDF не найден: {cfg.pilot_pdf}")
    key = _read_env_key(cfg.env_file, "MCP_IMPORT_KEYS") or _read_env_key(cfg.env_file, "MCP_WRITE_KEYS")
    if not key:
        raise CutoverError("pilot: нет import-ключа (MCP_IMPORT_KEYS/MCP_WRITE_KEYS)")
    pdf_path = _upload_file(cfg.mcp_base_url, key, cfg.pilot_pdf)
    res = _mcp_tool(
        cfg.mcp_base_url,
        key,
        "import_content",
        {
            "content": "",
            "content_type": "pdf",
            "pdf_path": pdf_path,
            "domain": cfg.pilot_domain,
            "subject": cfg.pilot_subject,
        },
        timeout=900,
    )
    if res.get("error") or res.get("ok") is False:
        raise CutoverError(f"pilot import: {res.get('error') or res}")


def _cmd_qdrant_snapshot(cfg: "CutoverConfig") -> list[Path]:
    dest = cfg.backup_dir / "qdrant-snapshots"
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in cfg.qdrant_collections:
        res = _http_json("POST", f"{cfg.qdrant_url}/collections/{name}/snapshots")
        sname = ((res.get("result") or {}) if isinstance(res, dict) else {}).get("name")
        if not sname:
            raise CutoverError(f"qdrant snapshot: нет name для {name}: {res}")
        target = dest / f"{name}-{sname}"
        _download(f"{cfg.qdrant_url}/collections/{name}/snapshots/{sname}", target)
        written.append(target)
    return written


def _cmd_qdrant_clear_legacy(cfg: "CutoverConfig") -> None:
    info = _http_json("GET", f"{cfg.qdrant_url}/collections")
    names = [c.get("name") for c in ((info.get("result") or {}).get("collections") or [])]
    aim = _http_json("GET", f"{cfg.qdrant_url}/aliases")
    aliased = {
        a.get("collection_name")
        for a in ((aim.get("result") or {}).get("aliases") or [])
    }
    prefixes = cfg.legacy_collection_prefixes or ()
    targets = [
        n for n in names
        if n and (n in cfg.legacy_collections or (prefixes and n.startswith(prefixes)))
        and n not in aliased
    ]
    for n in targets:
        _http_json("DELETE", f"{cfg.qdrant_url}/collections/{n}")


class CutoverDriver:
    """Шаги 2-8 плана §6. Каждый шаг: probe → action → marker."""

    def __init__(self, cfg: CutoverConfig):
        self.cfg = cfg

    # -- helpers -------------------------------------------------

    def _marker(self, step_no: int) -> Path:
        return self.cfg.run_dir / f"{step_no:02d}.complete"

    def _snapshot_marker_valid(self) -> bool:
        return marker_valid(self._marker(2), self._snapshot_input_hash())

    def apply_snapshot_ok(self, pre_input_hash: Optional[str]) -> bool:
        """True, если снапшот (шаг 2) валиден относительно предусловия прогона.

        Снос меняет дерево, поэтому сверяем маркер шага 2 с хешем на СТАРТЕ apply,
        а не с текущим состоянием (иначе гейт ложно срабатывал бы после teardown).
        """
        return pre_input_hash is not None and marker_valid(self._marker(2), pre_input_hash)

    def _snapshot_input_hash(self) -> str:
        return sha256_text(
            checksum_knowledge(self.cfg.knowledge_dir)
            + "|"
            + checksum_documents(self.cfg.documents_dir)
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
        extra = self._cmd("snapshot_qdrant", cfg.snapshot_qdrant)(cfg) or []
        artifacts.extend(Path(e) for e in extra)
        lines = [
            f"{_hash_file(a)}  {a.relative_to(cfg.backup_dir)}"
            for a in sorted(artifacts, key=lambda p: str(p))
        ]
        (cfg.backup_dir / "checksums.sha256").write_text("\n".join(lines) + "\n")

    def _probe_stop(self):
        services = ", ".join(self.cfg.services_to_stop)
        return sha256_text("stop-v1"), f"docker compose stop {services} (остановка стека)"

    def _action_stop(self):
        self._cmd("stop_cmd", self.cfg.stop_cmd)()

    def _teardown_input_hash(self):
        return sha256_text(
            checksum_knowledge(self.cfg.knowledge_dir)
            + "|"
            + checksum_documents(self.cfg.documents_dir)
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
            f"(битые Source + non-Source legacy); сохранить {len(keep)} записей "
            f"(Source + зависимые секции + корень); "
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

            deleted = asyncio.run(
                store.delete_many(sorted(teardown), commit_message="cutover: legacy corpus teardown")
            )
            if deleted != len(teardown):
                raise CutoverError(
                    f"частичный снос: удалено {deleted} из {len(teardown)} SSOT-записей"
                )
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

    def _probe_preflight(self):
        return sha256_text("preflight-v1"), (
            "git-доступ к knowledge-репо (sudo safe.directory при need) + "
            f"остановка стека: {', '.join(self.cfg.services_to_stop)}"
        )

    def _action_preflight(self):
        kd = self.cfg.knowledge_dir
        if not (kd / ".git").exists():
            # Не git-репозиторий (напр. tmp-фикстура теста) — проверка не применима.
            return
        proc = subprocess.run(
            ["git", "-C", str(kd), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise CutoverError(
                "git недоступен для knowledge-репо (dubious ownership?). Выполните: "
                f"sudo git config --global --add safe.directory {kd}"
            )

    def _probe_post_chown(self):
        targets = " ".join(str(p) for p in self.cfg.chown_targets) or "<none>"
        return sha256_text("chown-v1"), f"chown -R {self.cfg.chown_owner} {targets}"

    def _action_post_chown(self):
        targets = [p for p in self.cfg.chown_targets if p.exists()]
        if targets:
            _run(["chown", "-R", self.cfg.chown_owner, *[str(p) for p in targets]])

    # -- orchestration -------------------------------------------

    def run(
        self,
        *,
        dry_run: bool = True,
        apply: bool = False,
        confirm_destructive: Optional[str] = None,
        only_steps: Optional[set[int]] = None,
    ) -> dict:
        """Прогон шагов 2-8. dry-run: план без изменений; apply: реальные действия."""
        results: list[dict] = []
        # Fail-closed база: хеш «входа» на момент СТАРТА apply. Снос меняет дерево,
        # поэтому сверяем снапшот с предусловием прогона, а не с текущим состоянием.
        pre_snapshot_hash = self._snapshot_input_hash() if apply else None

        def step_defs():
            return [
                (1, "preflight", False, self._probe_preflight, self._action_preflight),
                (2, "snapshot", False, self._probe_snapshot, self._action_snapshot),
                (3, "stop", False, self._probe_stop, self._action_stop),
                (4, "teardown", True, self._probe_teardown, self._action_teardown),
                (5, "deploy", False, self._probe_deploy, self._action_deploy),
                (6, "reindex_smoke", False, self._probe_reindex_smoke, self._action_reindex_smoke),
                (7, "gc", False, self._probe_gc, self._action_gc),
                (8, "pilot", False, self._probe_pilot, self._action_pilot),
                (9, "post_chown", False, self._probe_post_chown, self._action_post_chown),
            ]

        for step_no, name, destructive, probe, action in step_defs():
            if only_steps and step_no not in only_steps:
                results.append({
                    "step": step_no, "name": name, "status": "skipped",
                    "detail": "вне выборки --only-steps",
                })
                continue
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

            # 1b) fail-closed: любой apply после снапшота требует валидный шаг 2
            # относительно предусловия прогона (см. pre_snapshot_hash).
            if apply and step_no > 2 and not self.apply_snapshot_ok(pre_snapshot_hash):
                entry["status"] = "refused"
                entry["detail"] = (
                    "apply запрещён: снапшот шага 2 не валиден относительно старта "
                    "(вход knowledge/documents изменился после снапшота либо CHECKSUMS "
                    "отсутствуют). Пересоберите снапшот: ARGS=\"--only-steps 2\""
                )
                results.append(entry)
                raise CutoverRefusal(entry["detail"])

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
    p.add_argument("--env-file", default=None, help="путь к .env (ключи читает сам скрипт)")
    p.add_argument("--pilot-pdf", default=None, help="PDF пилотного импорта (шаг 8)")
    p.add_argument("--only-steps", default=None,
                   help="csv номеров шагов для частичного прогона (напр. 1,2 = снапшот)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    import os

    repo = Path(__file__).resolve().parents[1]          # <repo>/mcp-knowledge/mcp-knowledge
    base = repo.parent                                   # <repo>/mcp-knowledge
    env = os.environ
    data_root = Path(env.get("DATA_ROOT", base / "data"))

    cfg = CutoverConfig(
        knowledge_dir=args.knowledge_dir or Path(env.get("MCP_KNOWLEDGE_DIR", base / "knowledge")),
        documents_dir=args.documents_dir or data_root / "documents",
        backup_dir=args.backup_dir or data_root / "backups" / "cutover",
        run_dir=args.run_dir or data_root / "cutover-run",
        pdf_cache_dir=args.pdf_cache_dir or data_root / "pdf_cache",
        quality_dir=args.quality_dir or data_root / "quality",
        dlq_dir=args.dlq_dir or data_root / "dlq",
        qdrant_url=args.qdrant_url or env.get("QDRANT_URL", "http://localhost:6333"),
        qdrant_collections=[c.strip() for c in (args.collections or "knowledge_public,knowledge_private").split(",") if c.strip()],
        qdrant_aliases=[a.strip() for a in (args.aliases or "").split(",") if a.strip()],
        destructive_token=env.get("CUTOVER_DESTRUCTIVE_TOKEN", DEFAULT_DESTRUCTIVE_TOKEN),
        env_file=Path(args.env_file) if args.env_file else (repo / ".env"),
        pilot_pdf=Path(args.pilot_pdf) if args.pilot_pdf else Path(
            env.get("CUTOVER_PILOT_PDF", str(repo / ".trash" / "2026-10-04-f6c-acc" / "rfc9111.pdf"))
        ),
        chown_targets=[
            base / "knowledge" / ".git",
            base / "knowledge" / ".trash",
            data_root / "documents",
        ],
    )
    cfg.stop_cmd = lambda: _cmd_docker_stop(cfg)
    cfg.deploy_cmd = lambda: _cmd_deploy(cfg)
    cfg.reindex_cmd = lambda: _cmd_reindex(cfg)
    cfg.smoke_cmd = lambda: _cmd_smoke(cfg)
    cfg.import_pilot_cmd = lambda: _cmd_pilot_import(cfg)
    cfg.snapshot_qdrant = _cmd_qdrant_snapshot
    cfg.clear_qdrant = _cmd_qdrant_clear_legacy

    dry_run = not args.apply
    only_steps = (
        {int(x) for x in args.only_steps.split(",") if x.strip()}
        if args.only_steps else None
    )
    driver = CutoverDriver(cfg)
    try:
        report = driver.run(
            dry_run=dry_run,
            apply=args.apply,
            confirm_destructive=args.confirm_destructive,
            only_steps=only_steps,
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
