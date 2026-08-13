"""Ранжирование dup-пар на потоки 🟢/🟡/🔴 (Фаза 2 dedup, план v3).

Двухпоточная модель рекомендаций для оператора:
- 🟢 green  — высокая уверенность (exact content-hash ИЛИ cosine≥0.97 с guards)
              → пачка «Утвердить все» одним кликом
- 🟡 yellow — сомнительные (антонимы, разная длина, книги/секции)
              → ручной просмотр с визуальным diff
- 🔴 red    — косвенные совпадения → только issues, не в ревью-очередь

Таблица решений R1–R6 (план v3, trace code-2026-08-13-dedup-elimination):

| # | Условие | Поток |
|---|---------|-------|
| R1 | hash_match AND standalone AND same_subject | 🟢 |
| R2 | hash_match BUT (parent OR different subject) | 🟡 |
| R3 | cosine≥0.97 AND same_subject AND len_diff≤0.10 AND standalone AND NOT negation | 🟢 |
| R4 | cosine≥0.92 AND (len_diff>0.10 OR negation OR has_parent) | 🟡 |
| R5 | cosine≥0.92 AND hash_mismatch AND len_diff>0.30 | 🟡 |
| R6 | cosine<0.92 (или отсутствует metadata) | 🔴 |

Negation guard (защита от антонимов): chto-lyubit-ai ≈ chto-ne-lyubit-ai
при cosine 0.995 — «что ИИ любит» / «что ИИ НЕ любит» (контрпример Critic).
"""

from __future__ import annotations

import re

# ── Негационные токены для антоним-guard ──────────────────────
NEGATION_TOKENS: frozenset[str] = frozenset({
    "ne", "not", "anti", "without", "no", "bez", "contra", "non", "не",
})

# Пороги (план v3)
COSINE_GREEN: float = 0.97     # cosine ≥ 0.97 → 🟢 при прочих guards
COSINE_YELLOW: float = 0.92    # cosine ≥ 0.92 → кандидат (🟡/🟢)
LEN_DIFF_TIGHT: float = 0.10   # |a-b|/max ≤ 0.10 → длины согласованы
LEN_DIFF_WIDE: float = 0.30    # > 0.30 → антоним-риск (приоритетный 🟡)

# ── Утилиты ───────────────────────────────────────────────────


def has_negation_pattern(slug_a: str, slug_b: str) -> bool:
    """Антоним-guard: разница токенов slug содержит отрицание."""
    if not slug_a or not slug_b:
        return False
    diff = set(slug_a.split("-")) ^ set(slug_b.split("-"))
    return bool(diff & NEGATION_TOKENS)


def rel_length_diff(len_a: int | None, len_b: int | None) -> float | None:
    """Относительная разница длин: |a-b|/max(a,b). None при отсутствии длин."""
    if not len_a or not len_b:
        return None
    denom = max(len_a, len_b)
    if denom == 0:
        return None
    return abs(len_a - len_b) / denom


def _parse_cosine_from_detail(detail: str) -> float | None:
    """Fallback: извлечь cosine из строки detail ('cosine=0.974')."""
    m = re.search(r"cosine=([0-9.]+)", detail or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def extract_target_kid(detail: str) -> str:
    """Извлечь target knowledge_id из detail ('Possible duplicate of <kid> ...')."""
    m = re.search(r"Possible duplicate of ([^\s()]+)", detail or "")
    return m.group(1) if m else ""


# ── Ранжирование ──────────────────────────────────────────────


def rank_pair(metadata: dict | None, detail: str = "") -> str:
    """Классифицировать dup-пару: 'green' | 'yellow' | 'red'.

    Args:
        metadata: структурированные сигналы из Фазы 1 (cosine, content_hash,
            content_length, target_content_hash, target_content_length,
            slug_negation, standalone, target_standalone, subject).
        detail: строка detail (fallback для cosine, когда metadata пустая).

    Returns:
        "green" (пачка «утвердить все») | "yellow" (сомнительная) | "red".
    """
    meta = metadata or {}

    # R6: без cosine — косвенное совпадение
    cosine = meta.get("cosine")
    if cosine is None:
        cosine = _parse_cosine_from_detail(detail)
    if cosine is None or cosine < COSINE_YELLOW:
        return "red"

    content_hash = meta.get("content_hash")
    target_hash = meta.get("target_content_hash")
    hash_match = bool(content_hash and target_hash and content_hash == target_hash)
    same_subject = True  # subject всегда совпадает внутри пары скана
    standalone = bool(meta.get("standalone", False))
    target_standalone = bool(meta.get("target_standalone", False))
    both_standalone = standalone and target_standalone
    negation = bool(meta.get("slug_negation", False))
    len_diff = rel_length_diff(meta.get("content_length"), meta.get("target_content_length"))

    # R1: байт-идентичный standalone-контент → 🟢 (FP=0)
    if hash_match and both_standalone and same_subject:
        return "green"

    # R2: hash совпал, но parent/subject → 🟡 (cross-collection риск)
    if hash_match:
        return "yellow"

    # R3: cosine ≥ 0.97 + длины согласованы + standalone + не антоним → 🟢
    if (
        cosine >= COSINE_GREEN
        and both_standalone
        and not negation
        and (len_diff is None or len_diff <= LEN_DIFF_TIGHT)
    ):
        return "green"

    # R5: cosine ≥ 0.92 + hash НЕ совпал + длина сильно различается → 🟡 (антоним-риск)
    if len_diff is not None and len_diff > LEN_DIFF_WIDE:
        return "yellow"

    # R4: остальные с cosine ≥ 0.92 → 🟡
    return "yellow"


def recommend_canonical(metadata: dict | None, source_kid: str, target_kid: str) -> str:
    """Рекомендовать каноническую запись пары.

    Приоритет (план v3): полнее (больше content_length) → standalone →
    target (на кого ссылается issue) как финальный разумный дефолт.

    Returns:
        knowledge_id канона.
    """
    meta = metadata or {}
    len_a = meta.get("content_length")
    len_b = meta.get("target_content_length")
    if len_a is not None and len_b is not None and len_a != len_b:
        return source_kid if len_a > len_b else target_kid
    # Standalone предпочтительнее секций книги
    a_std = bool(meta.get("standalone", False))
    b_std = bool(meta.get("target_standalone", False))
    if a_std != b_std:
        return source_kid if a_std else target_kid
    return target_kid  # дефолт — на кого ссылается issue


def group_green_batches(
    pairs: list[dict], max_batch: int = 25
) -> list[list[dict]]:
    """Сгруппировать 🟢-пары в батчи для «Утвердить все» (кластер по subject)."""
    by_subject: dict[str, list[dict]] = {}
    for p in pairs:
        s = p.get("subject", "")
        by_subject.setdefault(s, []).append(p)

    batches: list[list[dict]] = []
    for group in by_subject.values():
        for i in range(0, len(group), max_batch):
            batches.append(group[i:i + max_batch])
    return batches


def summarize_signals(metadata: dict | None) -> dict:
    """Компактная сводка сигналов для UI (только релевантное)."""
    meta = metadata or {}
    cosine = meta.get("cosine")
    if cosine is None:
        cosine = None
    return {
        "cosine": round(cosine, 3) if cosine is not None else None,
        "hash_match": bool(
            meta.get("content_hash") and meta.get("target_content_hash")
            and meta.get("content_hash") == meta.get("target_content_hash")
        ),
        "len_diff": rel_length_diff(
            meta.get("content_length"), meta.get("target_content_length")
        ),
        "slug_negation": bool(meta.get("slug_negation", False)),
        "standalone": bool(meta.get("standalone", False)),
        "target_standalone": bool(meta.get("target_standalone", False)),
    }
