"""Unit-тесты для quality/gates.py (4.2)."""

from __future__ import annotations

from mcp_server.quality.gates import evaluate_frontmatter

# ── Помощники ─────────────────────────────────────────────────

def _valid_md(**overrides) -> str:
    """Генерирует валидный markdown с полным frontmatter.

    Поля со значением None — исключаются из YAML (эмулирует отсутствие поля).
    """
    fm = {
        "knowledge_id": "test-kb-001",
        "domain": "engineering",
        "subject": "python",
        "tags": ["asyncio", "testing"],
        "created_at": "2026-08-01T10:00:00+03:00",
        "updated_at": "2026-08-03T10:00:00+03:00",
        "source": "https://example.com/test",
        "cross_subjects": ["devops"],
        "evergreen": False,
    }
    fm.update(overrides)
    # Собираем YAML вручную, пропуская None-значения
    lines = ["---"]
    for k, v in fm.items():
        if v is None:
            continue  # имитация отсутствующего поля
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append("# Test Knowledge Entry")
    lines.append("")
    lines.append("This is the markdown content.")
    return "\n".join(lines)


class TestValidFrontmatter:
    """Валидный frontmatter проходит gate."""

    def test_all_fields_present(self):
        """Все required + recommended → passed, не blocked."""
        md = _valid_md()
        result = evaluate_frontmatter(md)
        assert result.passed is True
        assert result.blocked is False
        assert len(result.issues) == 0

    def test_all_required_present_no_recommended(self):
        """Только required, без recommended → passed (warn, не block)."""
        md = _valid_md(source=None, cross_subjects=None, evergreen=None)
        result = evaluate_frontmatter(md)
        assert result.passed is True
        assert result.blocked is False
        # Должны быть warnings про отсутствующие recommended
        assert len(result.warnings) >= 1
        assert any("source" in w for w in result.warnings)

    def test_evergreen_true_passes(self):
        """evergreen: true — валидное значение."""
        md = _valid_md(evergreen=True)
        result = evaluate_frontmatter(md)
        assert result.passed is True


class TestMissingRequired:
    """Отсутствие required-полей → block."""

    def test_missing_knowledge_id(self):
        """Без knowledge_id → blocked (Pydantic ValidationError → *frontmatter)."""
        md = _valid_md()
        # Убираем knowledge_id из YAML
        md = md.replace("knowledge_id: test-kb-001\n", "")
        result = evaluate_frontmatter(md)
        assert result.blocked is True
        assert result.passed is False
        # Pydantic валидация ловит отсутствие required-поля → *frontmatter
        assert any("*frontmatter" in i.field for i in result.issues)

    def test_missing_domain(self):
        """Без domain → blocked."""
        md = _valid_md()
        md = md.replace("domain: engineering\n", "")
        result = evaluate_frontmatter(md)
        assert result.blocked is True

    def test_missing_subject(self):
        """Без subject → blocked."""
        md = _valid_md()
        md = md.replace("subject: python\n", "")
        result = evaluate_frontmatter(md)
        assert result.blocked is True

    def test_missing_tags(self):
        """Без tags → blocked."""
        md = _valid_md()
        md = md.replace("tags:", "tags:")  # оставляем ключ но без значений
        # Убираем элементы списка
        import re
        md = re.sub(r"tags:\n(  - .*\n)*", "tags:\n", md)
        result = evaluate_frontmatter(md)
        assert result.blocked is True


class TestMalformedFrontmatter:
    """Искажённый frontmatter → blocked."""

    def test_no_delimiters(self):
        """Нет --- → blocked."""
        md = "# Just markdown\n\nNo frontmatter here."
        result = evaluate_frontmatter(md)
        assert result.blocked is True
        assert any("*frontmatter" in i.field for i in result.issues)

    def test_unclosed_frontmatter(self):
        """Открывающий --- без закрывающего."""
        md = "---\nknowledge_id: test\n# Content"
        result = evaluate_frontmatter(md)
        assert result.blocked is True
        assert any("Unclosed" in i.message for i in result.issues)

    def test_empty_frontmatter(self):
        """Пустой frontmatter между ---."""
        md = "---\n---\n# Content"
        result = evaluate_frontmatter(md)
        assert result.blocked is True
        assert any("Empty" in i.message for i in result.issues)

    def test_empty_content(self):
        """Пустая строка."""
        result = evaluate_frontmatter("")
        assert result.blocked is True

    def test_invalid_yaml(self):
        """Битый YAML."""
        md = "---\nknowledge_id: [unclosed\n---\n# Content"
        result = evaluate_frontmatter(md)
        assert result.blocked is True
        assert any("YAML" in i.message for i in result.issues)

    def test_frontmatter_not_a_dict(self):
        """YAML — не словарь (список)."""
        md = "---\n- item1\n- item2\n---\n# Content"
        result = evaluate_frontmatter(md)
        assert result.blocked is True
        assert any("mapping" in i.message.lower() for i in result.issues)


class TestKnowledgeIdCollision:
    """Проверка коллизий knowledge_id."""

    def test_duplicate_knowledge_id(self):
        """Дубликат существующего ID → blocked."""
        md = _valid_md(knowledge_id="existing-id")
        result = evaluate_frontmatter(md, existing_ids={"existing-id", "other-id"})
        assert result.blocked is True
        assert any("already exists" in i.message for i in result.issues)

    def test_unique_knowledge_id_passes(self):
        """Уникальный ID → passes."""
        md = _valid_md(knowledge_id="new-unique-id")
        result = evaluate_frontmatter(md, existing_ids={"existing-id"})
        assert result.blocked is False
        assert result.passed is True


class TestStrictMode:
    """strict=true: advisory → block."""

    def test_strict_missing_recommended_blocks(self):
        """strict=true + нет source → blocked."""
        md = _valid_md(source=None)
        result = evaluate_frontmatter(md, strict=True)
        assert result.blocked is True
        assert any("source" in i.field for i in result.issues)

    def test_strict_missing_recommended_severity_critical(self):
        """strict=true: severity=critical для recommended."""
        md = _valid_md(source=None, cross_subjects=None)
        result = evaluate_frontmatter(md, strict=True)
        critical_issues = [i for i in result.issues if i.severity == "critical"]
        assert len(critical_issues) >= 2  # source + cross_subjects

    def test_non_strict_recommended_is_warn(self):
        """Без strict: recommended → severity=warn."""
        md = _valid_md(source=None)
        result = evaluate_frontmatter(md, strict=False)
        warn_issues = [i for i in result.issues if i.severity == "warn"]
        assert len(warn_issues) >= 1
