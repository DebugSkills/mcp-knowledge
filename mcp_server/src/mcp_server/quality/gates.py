"""Pre-write quality gates — frontmatter validation (4.2).

Validates YAML frontmatter of a markdown knowledge entry BEFORE it is written
to the SSOT. Two classes of checks (E5):

(a) required — контракт схемы SSOT: absence → 409 BLOCK всегда
(b) recommended — advisory: absence → severity=warn, не блокирует
    При strict=true → advisory warn повышается до block.

Validation is performed on the Pydantic KnowledgeFrontmatter model,
so fields with default_factory (cross_subjects, tags, ...) are never "missing".
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from mcp_server.models import KnowledgeFrontmatter

logger = logging.getLogger("mcp_knowledge.quality.gates")

# ── Required fields (контракт SSOT — BLOCK всегда) ──────────
REQUIRED_FIELDS: tuple[str, ...] = (
    "knowledge_id",
    "domain",
    "subject",
    "tags",
    "created_at",
    "updated_at",
)

# ── Recommended fields (advisory WARN — только advisory) ────
RECOMMENDED_FIELDS: tuple[str, ...] = (
    "source",
    "cross_subjects",
    "evergreen",
)


# ── Pydantic models ──────────────────────────────────────────

class GateIssue(BaseModel):
    """Единичное замечание gate."""

    field: str = Field(..., description="Имя поля или '*frontmatter'")
    severity: Literal["critical", "warn"] = Field(
        ..., description="critical = блокирует, warn = advisory"
    )
    message: str = Field(..., description="Человекочитаемое описание")


class GateResult(BaseModel):
    """Результат проверки frontmatter-gate."""

    passed: bool = Field(..., description="Все critical-проверки пройдены")
    blocked: bool = Field(
        ..., description="Запись заблокирована (critical или strict+warn)"
    )
    issues: list[GateIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ── Помощники ────────────────────────────────────────────────

def _parse_frontmatter_dict(content: str) -> tuple[Optional[dict], Optional[str]]:
    """Парсит YAML frontmatter из markdown-строки.

    Возвращает (dict | None, error_message | None).
    Ожидает формат:
        ---
        key: value
        ---
        Markdown content...
    """
    if not content or not content.strip():
        return None, "Empty content"

    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return None, "No frontmatter delimiters found (must start with ---)"

    # Находим закрывающий ---
    after_first = stripped[3:]  # после первого ---
    end_idx = after_first.find("\n---")
    if end_idx == -1:
        return None, "Unclosed frontmatter (missing closing ---)"

    yaml_str = after_first[:end_idx].strip()
    if not yaml_str:
        return None, "Empty frontmatter (no fields between --- delimiters)"

    try:
        fm_dict = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"

    if not isinstance(fm_dict, dict):
        return None, "Frontmatter must be a YAML mapping (key: value pairs)"

    return fm_dict, None


def _check_knowledge_id_collision(
    knowledge_id: str, existing_ids: set[str]
) -> Optional[str]:
    """Проверяет коллизию knowledge_id с уже существующими записями."""
    if knowledge_id in existing_ids:
        return f"knowledge_id '{knowledge_id}' already exists (duplicate)"
    return None


# ── Главная функция ──────────────────────────────────────────

def evaluate_frontmatter(
    content: str,
    *,
    strict: bool = False,
    existing_ids: Optional[set[str]] = None,
) -> GateResult:
    """Валидирует YAML frontmatter markdown-записи.

    Args:
        content: полный markdown с YAML frontmatter.
        strict: если True, advisory-warn повышается до block.
        existing_ids: множество существующих knowledge_id для проверки коллизий.

    Returns:
        GateResult с passed/blocked/issues/warnings.
    """
    issues: list[GateIssue] = []
    warnings: list[str] = []
    blocked: bool = False

    # Шаг 1: парсинг frontmatter
    fm_dict, parse_error = _parse_frontmatter_dict(content)
    if parse_error:
        issues.append(
            GateIssue(
                field="*frontmatter",
                severity="critical",
                message=parse_error,
            )
        )
        return GateResult(passed=False, blocked=True, issues=issues, warnings=warnings)

    assert fm_dict is not None  # для mypy

    # Шаг 2: валидация через Pydantic KnowledgeFrontmatter
    try:
        fm = KnowledgeFrontmatter(**fm_dict)
    except Exception as e:
        # Pydantic ValidationError — извлекаем понятные сообщения
        errors = str(e)
        # Пытаемся извлечь имена полей из ошибки валидации
        issues.append(
            GateIssue(
                field="*frontmatter",
                severity="critical",
                message=f"Frontmatter validation failed: {errors}",
            )
        )
        return GateResult(passed=False, blocked=True, issues=issues, warnings=warnings)

    # Шаг 3: проверка required-полей (по факту их присутствия в fm_dict)
    # Pydantic уже отвалидировал модель, но проверяем raw-словарь
    # чтобы поймать поля, которых нет в модели но они обязательны по контракту
    for field_name in REQUIRED_FIELDS:
        if field_name not in fm_dict or fm_dict[field_name] is None:
            issues.append(
                GateIssue(
                    field=field_name,
                    severity="critical",
                    message=f"Required field '{field_name}' is missing",
                )
            )
            blocked = True

    # Шаг 4: проверка recommended-полей
    for field_name in RECOMMENDED_FIELDS:
        if field_name not in fm_dict or fm_dict[field_name] is None:
            severity: Literal["critical", "warn"] = "critical" if strict else "warn"
            msg = f"Recommended field '{field_name}' is missing"
            issues.append(GateIssue(field=field_name, severity=severity, message=msg))
            if strict:
                blocked = True
            else:
                warnings.append(msg)

    # Шаг 5: проверка уникальности knowledge_id
    if existing_ids is not None:
        collision_error = _check_knowledge_id_collision(
            fm.knowledge_id, existing_ids
        )
        if collision_error:
            issues.append(
                GateIssue(
                    field="knowledge_id",
                    severity="critical",
                    message=collision_error,
                )
            )
            blocked = True

    # Собираем результат
    passed = not blocked
    return GateResult(passed=passed, blocked=blocked, issues=issues, warnings=warnings)
