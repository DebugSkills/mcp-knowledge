"""Unit tests: qdrant-client version pin — защита от дрейфа клиента (Task 1).

Проверяет:
  (a) pyproject.toml: qdrant-client содержит upper bound <1.15
  (b) Dockerfile: pip install строка содержит qdrant-client>=1.13.0,<1.15

Философия: guard-тесты без runtime-оверхеда — читают статические файлы сборки.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib  # Python 3.11+ built-in

MCP_SERVER_DIR = Path(__file__).parents[2]  # tests/unit → mcp_server/


class TestQdrantVersionPin:
    """Тесты pin-инварианта qdrant-client."""

    def test_pyproject_upper_bound(self) -> None:
        """pyproject.toml должен содержать upper bound <1.15 для qdrant-client."""
        pyproject_path = MCP_SERVER_DIR / "pyproject.toml"
        assert pyproject_path.is_file(), f"pyproject.toml not found at {pyproject_path}"

        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        deps: list[str] = data.get("project", {}).get("dependencies", [])
        qdrant_dep = [d for d in deps if isinstance(d, str) and "qdrant-client" in d]

        assert qdrant_dep, "qdrant-client not found in pyproject.toml dependencies"
        dep_str = qdrant_dep[0]
        assert "<1.15" in dep_str, (
            f"qdrant-client pin missing upper bound <1.15: {dep_str!r}"
        )

    def test_dockerfile_pin(self) -> None:
        """Dockerfile pip install строка должна содержать qdrant-client>=1.13.0,<1.15."""
        dockerfile_path = MCP_SERVER_DIR / "Dockerfile"
        assert dockerfile_path.is_file(), f"Dockerfile not found at {dockerfile_path}"

        content = dockerfile_path.read_text(encoding="utf-8")
        # Ищем строку с pip install ... qdrant-client ...
        pip_lines = [l for l in content.splitlines() if "pip install" in l and "qdrant-client" in l]

        assert pip_lines, "No pip install line with qdrant-client found in Dockerfile"
        # Должны быть кавычки вокруг qdrant-client>=1.13.0,<1.15
        any_pinned = any(
            re.search(r'qdrant-client\s*[><=]', l) is not None
            or 'qdrant-client>=' in l
            or '"qdrant-client' in l
            for l in pip_lines
        )
        assert any_pinned, (
            f"qdrant-client in Dockerfile missing version pin: {pip_lines[0]!r}"
        )
        # Строже: ищем полную строку >=1.13.0,<1.15 (с кавычками или без)
        any_exact = any(
            "qdrant-client>=1.13.0,<1.15" in l
            for l in pip_lines
        )
        assert any_exact, (
            f"Dockerfile pip install missing exact pin 'qdrant-client>=1.13.0,<1.15': "
            f"{pip_lines[0]!r}"
        )
