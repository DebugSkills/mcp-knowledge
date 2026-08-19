"""V1: Caller-audit — защита от coroutine-leak и дрейфа async-вызовов.

Регрессия 13.25: hybrid_split стала async + получила обязательный аргумент
embedder, но pdf_preprocessor.py звал её без await и без нового аргумента.
Баг жил в ветке `len > 4000`, которую не покрывал ни один тест — поэтому
дошёл до production.

Этот тест сканирует ВЕСЬ src/ по AST и падает на ЛЮБОМ вызове async-функции
проекта, который не обёрнут в `await` / `asyncio.create_task` / `asyncio.run` —
независимо от покрытия веток. Ловит класс «сигнатура/async-статус изменился,
вызывающий код не обновлён» на CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "mcp_server"

# asyncio-обёртки, которым можно передавать корутины без await
ALLOWED_COROUTINE_WRAPPERS = {"create_task", "run"}

# Документированные исключения: (файл-относительно-src, имя_вызова).
# Только для ложных срабатываний — НЕ для обхода реальных багов.
WHITELIST: set[tuple[str, str]] = set()


def _collect_async_names() -> set[str]:
    """Имена всех async-функций проекта (по AST)."""
    names: set[str] = set()
    for py in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                names.add(node.name)
    return names


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST | None]:
    """child → parent для проверки контекста Call."""
    parents: dict[ast.AST, ast.AST | None] = {tree: None}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _is_asyncio_wrapper(call: ast.Call) -> bool:
    """Call — это asyncio.create_task(...)/asyncio.run(...)."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in ALLOWED_COROUTINE_WRAPPERS:
        return False
    value = func.value
    return (
        (isinstance(value, ast.Name) and value.id == "asyncio")
        or (isinstance(value, ast.Attribute) and value.attr == "asyncio")
    )


def _iter_project_async_calls(
    tree: ast.AST,
    async_names: set[str],
    parents: dict[ast.AST, ast.AST | None],
):
    """Call-узлы, которые являются вызовами async-функций проекта.

    Учитывает контекст класса: self.X(...) проверяется только если класс
    реально определяет X как async (sync-метод своего класса — не наш async).
    """
    class_async: dict[ast.ClassDef, set[str]] = {}
    class_sync: dict[ast.ClassDef, set[str]] = {}
    module_sync: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AsyncFunctionDef):
                    class_async.setdefault(node, set()).add(stmt.name)
                elif isinstance(stmt, ast.FunctionDef):
                    class_sync.setdefault(node, set()).add(stmt.name)
        elif isinstance(node, ast.FunctionDef):
            module_sync.add(node.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in async_names and func.id not in module_sync:
                yield node, func.id
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in async_names
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            # Находим ближайший класс для self.X
            cls = _enclosing_class(node, parents)
            if cls is not None and func.attr in class_sync.get(cls, set()):
                continue  # sync-метод своего класса — не наш async
            if cls is not None and func.attr in class_async.get(cls, set()):
                yield node, func.attr


def _enclosing_class(node: ast.AST, parents: dict[ast.AST, ast.AST | None]):
    """Ближайший ClassDef-предок узла (или None)."""
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.ClassDef):
            return cur
        cur = parents.get(cur)
    return None


def _scan_module(py: Path, async_names: set[str]) -> list[str]:
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    parents = _parent_map(tree)
    rel = str(py.relative_to(SRC_ROOT.parent))  # mcp_server/src/mcp_server/...

    violations: list[str] = []
    for call, name in _iter_project_async_calls(tree, async_names, parents):
        if (rel, name) in WHITELIST:
            continue
        parent = parents.get(call)
        if isinstance(parent, ast.Await):
            continue
        if isinstance(parent, ast.Call) and _is_asyncio_wrapper(parent):
            continue
        violations.append(
            f"{rel}:{call.lineno}:{call.col_offset}: "
            f"async-вызов `{name}(...)` не awaited "
            f"(ожидался await / asyncio.create_task / asyncio.run)"
        )
    return violations


def test_caller_audit_all_async_calls_awaited():
    """Ни один async-вызов проекта не должен остаться без await."""
    async_names = _collect_async_names()
    assert async_names, "AST-скан не нашёл async-функций — проверь SRC_ROOT"

    violations: list[str] = []
    for py in SRC_ROOT.rglob("*.py"):
        violations.extend(_scan_module(py, async_names))

    assert not violations, (
        "Найдены async-вызовы без await (coroutine-leak):\n"
        + "\n".join(violations)
        + "\nИсправь вызов или добавь await. WHITELIST — только для "
        "документированных ложных срабатываний."
    )


def test_caller_audit_sanity_scan_covers_pdf_preprocessor():
    """Санити: аудит реально видит async-вызовы в pdf_preprocessor."""
    import asyncio

    from mcp_server.content.pdf_preprocessor import PDFPreprocessor

    py = SRC_ROOT / "content" / "pdf_preprocessor.py"
    async_names = _collect_async_names()
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    calls = list(_iter_project_async_calls(
        tree, async_names, _parent_map(tree)
    ))
    # self._fallback_per_page, self._make_sections_from_text, hybrid_split...
    names = {n for _, n in calls}
    assert "hybrid_split" in names
    assert "_fallback_per_page" in names
    # Ключевые методы должны быть async (13.25 регрессия: были sync)
    assert asyncio.iscoroutinefunction(PDFPreprocessor._fallback_per_page)
    assert asyncio.iscoroutinefunction(PDFPreprocessor._make_sections_from_text)
