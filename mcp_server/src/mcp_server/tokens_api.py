"""W5: admin API управления токенами (kb-console backend).

Эндпоинты (write-only, X-API-Key с key_level ∈ {write, import}):
- GET    /tokens            — список записей (без key_hash, с маской)
- POST   /tokens            — создать (возвращает plaintext ОДИН раз)
- POST   /tokens/{id}/revoke — отозвать
- PATCH  /tokens/{id}       — обновить note/expires_at
- POST   /tokens/{id}/rotate — rotate (revoke + create с теми же параметрами)

Заметки:
- plaintext возвращается только из create/rotate; в списке — маскированный
  префикс mcp_<l><z>_ + хвост (подсказка для человека, не секрет).
- Запись содержит key_hash — в ответы НЕ попадает.
- Аутентификация: AuthMiddleware ставит request.state.auth (AuthInfo);
  уровень ниже write → 403.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from mcp_server.token_store import LEVEL_CODES, ZONE_CODES, _UNSET

logger = logging.getLogger("mcp_knowledge.tokens_api")

router = APIRouter(prefix="/tokens", tags=["tokens"])

ADMIN_LEVELS = {"write", "import"}


def _require_admin(request: Request) -> None:
    """Только write/import-уровень (kb-console ходит с write-ключом)."""
    auth = getattr(request.state, "auth", None)
    level = getattr(auth, "key_level", "") if auth is not None else ""
    if level not in ADMIN_LEVELS:
        raise HTTPException(status_code=403, detail="tokens API requires write/import key")


def _get_store(request: Request):
    store = getattr(request.app.state, "token_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="token_store not initialized")
    return store


def _public_record(rec) -> dict:
    """Сериализация записи без key_hash; маска префикса для UI (v1.5)."""
    return {
        "id": rec.id,
        "level": rec.level,
        "zone": rec.zone,
        "active": rec.active,
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        "note": rec.note,
        "source": rec.source,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "last_used_at": rec.last_used_at.isoformat() if rec.last_used_at else None,
        "mask": f"mcp_{_LEVEL_CODE(rec.level)}{_ZONE_CODE(rec.zone)}_****",
    }


def _LEVEL_CODE(level: str) -> str:  # noqa: N802 — зеркало token_store
    return LEVEL_CODES.get(level, "?")


def _ZONE_CODE(zone: str) -> str:  # noqa: N802
    return ZONE_CODES.get(zone, "?")


@router.get("")
async def list_tokens(request: Request) -> dict:
    """Список токенов (без key_hash/plaintext)."""
    _require_admin(request)
    store = _get_store(request)
    return {"tokens": [_public_record(r) for r in store.list()]}


@router.post("")
async def create_token(request: Request, body: dict) -> dict:
    """Создать токен. body: level, zone, note?, expires_at? (ISO) / expires?"""
    _require_admin(request)
    store = _get_store(request)

    level = body.get("level", "")
    zone = body.get("zone", "both")
    note = body.get("note", "")
    expires = body.get("expires_at") or body.get("expires")
    expires_dt = None
    if expires:
        try:
            expires_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"bad expires_at: {exc}") from exc

    try:
        token_id, plaintext = store.create(
            level=level, zone=zone, note=note, expires_at=expires_dt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("tokens API: created %s (level=%s zone=%s)", token_id, level, zone)
    return {"id": token_id, "plaintext": plaintext, "mask": _public_record(store.get(token_id))["mask"]}


@router.post("/{token_id}/revoke")
async def revoke_token(token_id: str, request: Request) -> dict:
    """Отозвать токен (active=False)."""
    _require_admin(request)
    store = _get_store(request)
    rec = store.get(token_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"token not found: {token_id}")
    store.set_active(token_id, False)
    logger.info("tokens API: revoked %s", token_id)
    return {"id": token_id, "revoked": True}


@router.patch("/{token_id}")
async def patch_token(token_id: str, request: Request, body: dict) -> dict:
    """Обновить note/expires_at (PATCH {note?, expires_at?, active?})."""
    _require_admin(request)
    store = _get_store(request)

    expires_at = body.get("expires_at")
    if expires_at:
        try:
            expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"bad expires_at: {exc}") from exc
    elif "expires_at" in body:
        expires_at = None  # явное очищение

    rec = store.update_meta(
        token_id,
        note=body.get("note"),
        expires_at=expires_at if "expires_at" in body else _UNSET,
        active=body.get("active"),
    )
    if rec is None:
        raise HTTPException(status_code=404, detail=f"token not found: {token_id}")

    logger.info("tokens API: patched %s", token_id)
    return _public_record(rec)


@router.post("/{token_id}/rotate")
async def rotate_token(token_id: str, request: Request) -> dict:
    """Rotate: revoke старого + create нового с теми же level/zone/note/expires."""
    _require_admin(request)
    store = _get_store(request)
    rec = store.get(token_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"token not found: {token_id}")

    store.set_active(token_id, False)
    new_id, new_plaintext = store.create(
        level=rec.level, zone=rec.zone, note=rec.note, expires_at=rec.expires_at,
    )
    logger.info("tokens API: rotated %s → %s", token_id, new_id)
    return {"id": new_id, "plaintext": new_plaintext, "old_id": token_id}
