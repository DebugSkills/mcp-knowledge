"""CLI-утилиты для обслуживания MCP Knowledge Server.

- reindex: полный переиндекс из Markdown SSOT
- dlq-replay: возврат задач из DLQ в очередь
- token: управление токенами доступа (create/list/revoke/rotate) — W4.5,
  план two-zone-access §2.3: самодокументируемый формат mcp_<уровень><зона>_<secret>
  (v1.5) + визуальные бейджи (v1.3)

Используются из Makefile: `make reindex`, `make dlq-replay`.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .token_store import LEVEL_CODES, ZONE_CODES, TokenStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_knowledge.cli")


async def reindex():
    """Полный переиндекс Qdrant из Markdown SSOT.

    Использование: python -m mcp_server.cli reindex
    """
    from .embedding import EmbeddingManager
    from .indexing import IndexingPipeline, MarkdownChunker
    from .storage import MarkdownStore, QdrantClient

    logger.info("=== REINDEX: полная перестройка Qdrant из Markdown SSOT ===")

    store = MarkdownStore()
    qdrant = QdrantClient()
    qdrant.ensure_collection(force_recreate=False)

    embedder = EmbeddingManager()
    await embedder.initialize()

    chunker = MarkdownChunker()
    pipeline = IndexingPipeline(store, qdrant, embedder, chunker)
    await pipeline.start()

    try:
        result = await pipeline.reindex_all()
        logger.info("REINDEX завершён: %s", result)
        return result
    finally:
        await pipeline.stop()
        qdrant.close()


async def dlq_replay():
    """Возврат задач из DLQ-директории в индекс.

    Читает JSON-файлы из data/dlq/ и пытается повторно проиндексировать.

    Использование: python -m mcp_server.cli dlq-replay
    """
    from .config import settings
    from .embedding import EmbeddingManager
    from .indexing import IndexingPipeline, MarkdownChunker
    from .storage import MarkdownStore, QdrantClient

    dlq_dir = Path(settings.DLQ_DIR)
    if not dlq_dir.exists():
        logger.info("DLQ-директория пуста: %s", dlq_dir)
        return {"replayed": 0}

    dlq_files = list(dlq_dir.glob("*.json"))
    if not dlq_files:
        logger.info("Нет задач в DLQ")
        return {"replayed": 0}

    logger.info("=== DLQ REPLAY: %d задач ===", len(dlq_files))

    store = MarkdownStore()
    qdrant = QdrantClient()
    qdrant.ensure_collection(force_recreate=False)

    embedder = EmbeddingManager()
    await embedder.initialize()

    chunker = MarkdownChunker()
    pipeline = IndexingPipeline(store, qdrant, embedder, chunker)
    await pipeline.start()

    replayed = 0
    failed = 0

    try:
        for dlq_file in dlq_files:
            try:
                data = json.loads(dlq_file.read_text(encoding="utf-8"))
                knowledge_id = data.get("knowledge_id")
                if not knowledge_id:
                    logger.warning("DLQ-файл без knowledge_id: %s", dlq_file)
                    continue

                entry = await store.read(knowledge_id)
                if entry is None:
                    logger.warning("Запись %s не найдена в SSOT — удаляю DLQ", knowledge_id)
                    dlq_file.unlink()
                    continue

                # Повторная индексация
                chunks = chunker.chunk(
                    knowledge_id=entry.frontmatter.knowledge_id,
                    content=entry.content,
                )
                if chunks:
                    texts = [ch.content for ch in chunks]
                    loop = asyncio.get_running_loop()
                    vectors = await loop.run_in_executor(
                        None, embedder.embed_sync, texts
                    )
                    # Собираем Qdrant points
                    import uuid

                    from .storage.schema import (
                        ZONE_PRIVATE,
                        build_payload_point,
                        collection_for_zone,
                    )
                    fm = entry.frontmatter
                    # W2.13: зона из frontmatter SSOT → зональная коллекция
                    zone = getattr(fm, "zone", None) or ZONE_PRIVATE
                    points = []
                    for ch, vector in zip(chunks, vectors):
                        point = build_payload_point(
                            point_id=str(uuid.uuid4()),
                            vector=vector,
                            knowledge_id=fm.knowledge_id,
                            chunk_id=ch.chunk_id,
                            content=ch.content,
                            domain=fm.domain,
                            subject=fm.subject,
                            project=fm.project,
                            tags=fm.tags,
                            cross_subjects=fm.cross_subjects,
                            section_header=ch.section_header,
                            chunk_index=ch.chunk_index,
                            updated_at=fm.updated_at.isoformat(),
                            parent_knowledge_id=getattr(fm, "parent_knowledge_id", None),
                            content_type=getattr(fm, "content_type", None),
                            zone=zone,
                        )
                        points.append(point)
                    qdrant.upsert_points(
                        points, collection_name=collection_for_zone(zone)
                    )

                dlq_file.unlink()  # Успешно — удаляем DLQ-файл
                replayed += 1
                logger.info("DLQ replay OK: %s", knowledge_id)

            except Exception as e:  # noqa: BLE001
                logger.error("DLQ replay failed for %s: %s", dlq_file, e)
                failed += 1

    finally:
        await pipeline.stop()
        qdrant.close()

    result = {"replayed": replayed, "failed": failed}
    logger.info("DLQ REPLAY завершён: %s", result)
    return result


# ── Токены: визуальные бейджи и вывод (W4.5, план §2.3) ──────
# Легенда (v1.3): subscriber 🟢 · read 🔵 · import 🟠 · write 🔴;
# зона public «A» 🟢 · private «B» 🔴 · both «A+B» ⚪;
# статус active ✅ · revoked ⛔ · expired ⏳ · expiring soon ⚠️ (≤7 дней).

_LEVEL_BADGES = {
    "subscriber": ("🟢", "32"),
    "read": ("🔵", "34"),
    "import": ("🟠", "38;5;208"),
    "write": ("🔴", "31"),
}
_ZONE_BADGES = {
    "public": ("A", "32"),
    "private": ("B", "31"),
    "both": ("A+B", "37"),
}
_STATUS_BADGES = {
    "active": ("✅", "32"),
    "revoked": ("⛔", "90"),
    "expired": ("⏳", "90"),
    "expiring_soon": ("⚠️", "33"),
}
_LEVEL_NAMES = {
    "subscriber": "подписчик",
    "read": "чтение",
    "import": "импорт",
    "write": "запись",
}
_ZONE_NAMES = {
    "public": "только public",
    "private": "только private",
    "both": "обе зоны",
}
_LEVEL_BY_CODE = {code: level for level, code in LEVEL_CODES.items()}
_ZONE_BY_CODE = {code: zone for zone, code in ZONE_CODES.items()}

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _use_color() -> bool:
    """ANSI-подсветка только на tty (пайп/тесты → plain)."""
    return sys.stdout.isatty()


def _c(text: str, code: str, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def _pad(text: str, width: int) -> str:
    """Дополнение без учёта ANSI-escape-последовательностей."""
    visible = _ANSI_RE.sub("", text)
    return text + " " * max(0, width - len(visible))


def _expires_type(value: str) -> datetime:
    """argparse type: YYYY-MM-DD → aware datetime UTC."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"неверная дата {value!r} (ожидается YYYY-MM-DD)",
        ) from exc


def _token_status(rec, now: datetime) -> str:
    if not rec.active:
        return "revoked"
    if rec.expires_at is not None and rec.expires_at <= now:
        return "expired"
    if rec.expires_at is not None and rec.expires_at <= now + timedelta(days=7):
        return "expiring_soon"
    return "active"


def _get_store(tokens_dir: str | None) -> TokenStore:
    """TokenStore с --tokens-dir или дефолтом settings.TOKENS_DIR."""
    if tokens_dir is None:
        from .config import settings

        tokens_dir = settings.TOKENS_DIR
    return TokenStore(tokens_dir=tokens_dir)


def _decode_prefix(prefix: str, color: bool) -> str:
    """mcp_sa_ → «🟢 подписчик, только public» (расшифровка v1.5).

    Коды берём из LEVEL_CODES/ZONE_CODES (token_store) — легенда одна.
    """
    level = _LEVEL_BY_CODE.get(prefix[4], "")
    zone = _ZONE_BY_CODE.get(prefix[5], "")
    badge, level_code = _LEVEL_BADGES.get(level, ("❓", "0"))
    _, zone_code = _ZONE_BADGES.get(zone, ("?", "0"))
    return (
        f"{_c(prefix, level_code, color)} → {badge} "
        f"{_c(_LEVEL_NAMES.get(level, '?'), level_code, color)}, "
        f"{_c(_ZONE_NAMES.get(zone, '?'), zone_code, color)}"
    )


def _print_created(
    token_id: str,
    plaintext: str,
    level: str,
    note: str,
    expires_at: datetime | None,
    color: bool,
) -> None:
    """Печать созданного токена: plaintext один раз, с подсветкой (v1.5/v1.3)."""
    prefix = plaintext[:7]  # mcp_XX_
    secret = plaintext[7:]
    print(f"✅ Токен создан: {token_id}")
    print(f"🔑 {_c(prefix, _LEVEL_BADGES[level][1], color)}{secret}")
    print(f"   {_decode_prefix(prefix, color)}")
    badges = ["✅ active"]
    if expires_at is not None:
        badges.append(f"⏳ expires {expires_at:%Y-%m-%d}")
    if note:
        badges.append(f"note: {note}")
    print("   " + " · ".join(badges))
    if level in ("write", "import"):
        print("   " + _c(
            f"🔴 ВНИМАНИЕ: {level}-ключ — выдавать ТОЛЬКО команде", "31", color,
        ))
    print()


def _cmd_create(store: TokenStore, args, color: bool) -> None:
    if args.level == "subscriber" and args.zone != "public":
        print("ℹ️  subscriber: зона принудительно public (план §2.3).")
    try:
        token_id, plaintext = store.create(
            args.level, args.zone, note=args.note, expires_at=args.expires,
        )
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        sys.exit(1)
    _print_created(token_id, plaintext, args.level, args.note, args.expires, color)


def _cmd_list(store: TokenStore, args, color: bool) -> None:
    records = store.list()
    if args.json:
        now = datetime.now(timezone.utc)
        out = [
            {
                "id": rec.id,
                "level": rec.level,
                "zone": rec.zone,
                "status": _token_status(rec, now),
                "active": rec.active,
                "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
                "note": rec.note,
                "scope": rec.scope,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "source": rec.source,
            }
            for rec in records
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not records:
        print("Токенов нет.")
        return
    now = datetime.now(timezone.utc)
    header = f"{'ID':<20} {'Уровень':<16} {'Зона':<6} {'Статус':<18} {'Expires':<12} Note"
    print(header)
    print("-" * len(header))
    for rec in records:
        status = _token_status(rec, now)
        badge, level_code = _LEVEL_BADGES.get(rec.level, ("❓", "0"))
        zone_badge, zone_code = _ZONE_BADGES.get(rec.zone, ("?", "0"))
        status_badge, status_code = _STATUS_BADGES[status]
        level_cell = _pad(_c(f"{badge} {rec.level}", level_code, color), 16)
        zone_cell = _pad(_c(zone_badge, zone_code, color), 6)
        status_cell = _pad(_c(f"{status_badge} {status}", status_code, color), 18)
        expires = f"{rec.expires_at:%Y-%m-%d}" if rec.expires_at else "—"
        print(f"{rec.id:<20} {level_cell} {zone_cell} {status_cell} {expires:<12} {rec.note}")


def _cmd_revoke(store: TokenStore, args) -> None:
    rec = store.revoke(args.token_id)
    if rec is None:
        print(f"Токен не найден: {args.token_id}")
        sys.exit(1)
    print(f"⛔ Токен {rec.id} отозван (уровень {rec.level}, зона {rec.zone}).")


def _cmd_rotate(store: TokenStore, args, color: bool) -> None:
    rec = store.get(args.token_id)
    if rec is None:
        print(f"Токен не найден: {args.token_id}")
        sys.exit(1)
    if not rec.active:
        print(f"ℹ️  Токен {rec.id} уже неактивен — создаю новый с теми же параметрами.")
    store.revoke(rec.id)
    print(f"⛔ Старый токен {rec.id} отозван.")
    token_id, plaintext = store.create(
        rec.level, rec.zone, note=rec.note, expires_at=rec.expires_at,
    )
    print()
    _print_created(token_id, plaintext, rec.level, rec.note, rec.expires_at, color)


def cmd_token(argv: list[str]) -> None:
    """Команда token: create/list/revoke/rotate (W4.5, план §2.3)."""
    parser = argparse.ArgumentParser(
        prog="python -m mcp_server.cli token",
        description="Управление токенами доступа (формат mcp_<уровень><зона>_<secret>).",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_create = sub.add_parser("create", help="Создать токен (plaintext печатается один раз)")
    p_create.add_argument("--level", required=True, choices=sorted(LEVEL_CODES),
                          help="уровень доступа")
    p_create.add_argument("--zone", default="public", choices=sorted(ZONE_CODES),
                          help="зона (subscriber → всегда public)")
    p_create.add_argument("--note", default="", help="человекочитаемая подпись")
    p_create.add_argument("--expires", type=_expires_type, default=None,
                          help="дата истечения YYYY-MM-DD")
    p_create.add_argument("--tokens-dir", default=None, help="каталог токен-стора")

    p_list = sub.add_parser("list", help="Список токенов (таблица или --json)")
    p_list.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    p_list.add_argument("--tokens-dir", default=None, help="каталог токен-стора")

    p_revoke = sub.add_parser("revoke", help="Отозвать токен")
    p_revoke.add_argument("token_id", help="ID токена (tok_...)")
    p_revoke.add_argument("--tokens-dir", default=None, help="каталог токен-стора")

    p_rotate = sub.add_parser("rotate", help="Отозвать и создать новый с теми же level/zone/note")
    p_rotate.add_argument("token_id", help="ID токена (tok_...)")
    p_rotate.add_argument("--tokens-dir", default=None, help="каталог токен-стора")

    args = parser.parse_args(argv)
    color = _use_color()
    store = _get_store(args.tokens_dir)
    if args.subcommand == "create":
        _cmd_create(store, args, color)
    elif args.subcommand == "list":
        _cmd_list(store, args, color)
    elif args.subcommand == "revoke":
        _cmd_revoke(store, args)
    elif args.subcommand == "rotate":
        _cmd_rotate(store, args, color)


def main():
    """Точка входа: python -m mcp_server.cli <command>"""
    if len(sys.argv) < 2:
        print("Usage: python -m mcp_server.cli <reindex|dlq-replay|token>")
        sys.exit(1)

    command = sys.argv[1]
    if command == "reindex":
        asyncio.run(reindex())
    elif command == "dlq-replay":
        asyncio.run(dlq_replay())
    elif command == "token":
        cmd_token(sys.argv[2:])
    else:
        print(f"Неизвестная команда: {command}")
        print("Доступные команды: reindex, dlq-replay, token")
        sys.exit(1)


if __name__ == "__main__":
    main()
