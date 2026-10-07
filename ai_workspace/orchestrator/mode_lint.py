"""Runtime lint L1-L12 для режимов AI-верстака (Ф3.5a-3; L11 — Ф3.9; L12 — Ф6-a 6a.3).

Спека: plans/_provenance/arch-2026-10-05-ai-workspace/
       arch-2026-10-05-ai-workspace-mode-engine-spec.md §4б.

Контракт: ``validate_lint(doc, registry, *, base_dir) -> list[Finding]``;
``Finding`` реиспользуется из ``mode_schema``; все severity — ``error``,
коды ``L1..L12``. Каждое правило — чистая функция (кроме L2, читающей ФС).

Правила:
  L1  DAG ацикличен (цикл допустим только у critic-gate с on_revise+max_iterations)
  L2  роли узлов есть в roles.yaml; seed-скилл существует
  L3  инструменты есть в tools.yaml
  L4  выход узла N покрывает вход N+1
  L5  human-gate обязателен для analyst-strategic / brainstorm
  L6  citation-политика: document -> citer(strict); verdict/transcript -> citer запрещён
  L7  model_class резолвится; private -> только local-only
  L8  терминируемость: critic.max_iterations; human-gate.timeout+on_timeout
  L9  fork/join сбалансированы
  L10 board-контракт: single-writer по секциям (writes)
  L11 промпты model-agnostic: пустой prompt_overrides, нет упоминаний моделей
  L12 inputs объявлен и непуст у узлов-потребителей (llm/tool/critic-gate)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_workspace.orchestrator.mode_schema import Finding

_SEVERITY = "error"
_REPO_ROOT = Path(__file__).resolve().parents[2]
CITER_TOOL = "mcp.citation_attach"


def _f(code: str, message: str, path: str = "") -> Finding:
    return Finding(code=code, severity=_SEVERITY, message=message, path=path)


def _nodes(doc: dict) -> list[dict]:
    return [n for n in (doc.get("nodes") or []) if isinstance(n, dict)]


def _by_id(doc: dict) -> dict[str, dict]:
    return {str(n.get("id")): n for n in _nodes(doc) if n.get("id")}


def _edges(doc: dict) -> list[tuple[str, str]]:
    """Разобрать edges: ``a->b`` и ``critic|PASS->editor`` -> (src, dst)."""
    out: list[tuple[str, str]] = []
    for raw in doc.get("edges") or []:
        if not isinstance(raw, str) or "->" not in raw:
            continue
        left, right = raw.split("->", 1)
        src = left.split("|", 1)[0].strip()
        dst = right.strip()
        out.append((src, dst))
    return out


def _effective_outputs(node: dict) -> set[str]:
    """Выходы узла: outputs + verdicts + 'verdict' для critic-gate."""
    res = {str(x) for x in (node.get("outputs") or [])}
    res |= {str(x) for x in (node.get("verdicts") or [])}
    if node.get("kind") == "critic-gate":
        res.add("verdict")
    return res


def validate_lint(doc: dict, registry: Any, *, base_dir: Path | None = None) -> list[Finding]:
    """Запустить L1-L12; вернуть список Finding (severity=error)."""
    findings: list[Finding] = []
    for fn in (
        lint_l1, lint_l2, lint_l3, lint_l4, lint_l5,
        lint_l6, lint_l7, lint_l8, lint_l9, lint_l10, lint_l11, lint_l12,
    ):
        findings.extend(fn(doc, registry, base_dir))
    return findings


def lint_l1(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """DAG ацикличен; цикл допустим ТОЛЬКО как critic-gate on_revise."""
    nodes = _by_id(doc)
    allowed: set[tuple[str, str]] = set()
    for src, dst in _edges(doc):
        n = nodes.get(src, {})
        if n.get("kind") == "critic-gate" and n.get("max_iterations") and n.get("on_revise") == dst:
            allowed.add((src, dst))
    # Топосортировка по рёбрам, кроме разрешённых циклов критика.
    adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    for src, dst in _edges(doc):
        if (src, dst) in allowed:
            continue
        if src in adj:
            adj[src].append(dst)
    state: dict[str, int] = {}  # 0=новая, 1=в стеке, 2=готова

    def dfs(u: str) -> bool:
        state[u] = 1
        for v in adj.get(u, []):
            if state.get(v, 0) == 1:
                return False
            if state.get(v, 0) == 0 and not dfs(v):
                return False
        state[u] = 2
        return True

    for nid in adj:
        if state.get(nid, 0) == 0 and not dfs(nid):
            return [_f("L1", "граф содержит цикл вне разрешённого critic-gate(on_revise)", "edges")]
    return []


def lint_l2(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """Роли узлов есть в roles.yaml; seed-скилл существует."""
    roles = registry.get("roles") or {}
    root = Path(base_dir) if base_dir else _REPO_ROOT
    out: list[Finding] = []
    for node in _nodes(doc):
        role = node.get("role")
        if role and role not in roles:
            out.append(_f("L2", f"неизвестная роль: {role}", f"nodes.{node.get('id')}"))
        seed = node.get("seed")
        if seed:
            path = root / str(seed)
            if str(seed).startswith("skills/"):
                slug = str(seed).split("/", 1)[1]
                path = root / ".knowledge" / "skills" / slug / "SKILL.md"
            if not path.exists():
                out.append(_f("L2", f"seed-скилл не найден: {seed}", f"nodes.{node.get('id')}.seed"))
    for role, meta in roles.items():
        skill = (meta or {}).get("seed_skill")
        if skill and not (root / str(skill)).exists():
            out.append(_f("L2", f"seed_skill роли {role} не найден: {skill}", f"roles.{role}"))
    return out


def lint_l3(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """Инструменты tool-step и doc.tools существуют в tools.yaml."""
    tools = registry.get("tools") or {}
    out: list[Finding] = []
    for node in _nodes(doc):
        if node.get("kind") == "tool-step":
            tool = node.get("tool")
            if tool and tool not in tools:
                out.append(_f("L3", f"неизвестный инструмент: {tool}", f"nodes.{node.get('id')}.tool"))
    for tool in doc.get("tools") or []:
        if tool not in tools:
            out.append(_f("L3", f"неизвестный инструмент в tools: {tool}", "tools"))
    return out


def lint_l4(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """Вход узла покрыт выходами ПРЕДШЕСТВЕННИКОВ по цепочке (brief — вход режима).

    Покрытие транзитивное: `draft`, сделанный analyst, легитимно читается editor
    (analyst -> critic -> editor) — иначе любая цепочка с накоплением контекста
    ложна. Проверяется «доступное» множество = выходы всех предков узла.
    """
    nodes = _by_id(doc)
    preds: dict[str, set[str]] = {nid: set() for nid in nodes}
    for src, dst in _edges(doc):
        if dst in preds:
            preds[dst].add(src)
    memo: dict[str, set[str]] = {}

    def avail(nid: str, seen: frozenset[str]) -> set[str]:
        """Выходы всех предков узла (без самого узла), с защитой от циклов.

        Кэш — ТОЛЬКО для верхнеуровневых вызовов (``seen`` пуст): при цикле
        результат зависит от точки входа, и кэш по одному nid даёт ложные
        пропуски (найдено honest-red фикстуром L1).
        """
        if not seen and nid in memo:
            return memo[nid]
        if nid in seen:
            return set()
        res: set[str] = set()
        for p in preds.get(nid, ()):
            res |= _effective_outputs(nodes.get(p, {}))
            res |= avail(p, seen | {nid})
        if not seen:
            memo[nid] = res
        return res

    out: list[Finding] = []
    for src, dst in _edges(doc):
        dn = nodes.get(dst)
        if dn is None:
            continue
        needed = {str(x) for x in (dn.get("inputs") or [])} - {"brief"}
        missing = needed - (_effective_outputs(nodes.get(src, {})) | avail(dst, frozenset()))
        if missing:
            out.append(_f(
                "L4",
                f"{src}->{dst}: вход не покрыт выходом предшественников ({', '.join(sorted(missing))})",
                "edges",
            ))
    return out


def lint_l5(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """human-gate обязателен для analyst-strategic и brainstorm."""
    if doc.get("shape") not in {"analyst-strategic", "brainstorm"}:
        return []
    if any(n.get("kind") == "human-gate" for n in _nodes(doc)):
        return []
    return [_f("L5", f"shape {doc.get('shape')} требует human-gate", "nodes")]


def lint_l6(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """Citation-политика по contract."""
    contract = doc.get("contract")
    citers = [n for n in _nodes(doc) if n.get("kind") == "tool-step" and n.get("tool") == CITER_TOOL]
    if contract == "document":
        strict = [n for n in citers if n.get("policy") == "strict"]
        if not strict:
            return [_f("L6", "contract=document требует citer (mcp.citation_attach, policy: strict)", "nodes")]
    elif contract in {"verdict", "transcript"} and citers:
        return [_f("L6", f"contract={contract} запрещает citer", "nodes")]
    return []


def lint_l7(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """model_class резолвится; private -> только local-only."""
    classes = registry.get("model_classes") or {}
    zone = doc.get("zone", "public")
    out: list[Finding] = []
    for node in _nodes(doc):
        mc = node.get("model_class")
        if not mc:
            continue
        if mc not in classes:
            out.append(_f("L7", f"неизвестный model_class: {mc}", f"nodes.{node.get('id')}"))
            continue
        if zone == "private" and mc != "local-only":
            out.append(_f(
                "L7",
                f"private-режим требует local-only, а узел {node.get('id')} использует {mc}",
                f"nodes.{node.get('id')}.model_class",
            ))
    return out


def lint_l8(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """Терминируемость: critic.max_iterations; human-gate.timeout+on_timeout."""
    out: list[Finding] = []
    for node in _nodes(doc):
        kind = node.get("kind")
        if kind == "critic-gate":
            mi = node.get("max_iterations")
            if not isinstance(mi, int) or mi < 1:
                out.append(_f("L8", f"critic-gate {node.get('id')}: нет max_iterations", f"nodes.{node.get('id')}"))
        elif kind == "human-gate":
            nid = node.get("id")
            if not node.get("timeout"):
                out.append(_f("L8", f"human-gate {nid}: нет timeout", f"nodes.{nid}"))
            if node.get("on_timeout") not in {"sleep", "abort"}:
                out.append(_f("L8", f"human-gate {nid}: on_timeout должен быть sleep|abort", f"nodes.{nid}"))
    return out


def lint_l9(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """fork/join сбалансированы (парность по количеству)."""
    nodes = _nodes(doc)
    forks = [n for n in nodes if n.get("kind") == "fork"]
    joins = [n for n in nodes if n.get("kind") == "join"]
    if len(forks) != len(joins):
        return [_f("L9", f"fork/join не сбалансированы: fork={len(forks)}, join={len(joins)}", "nodes")]
    return []


MODEL_NAME_MARKERS: tuple[str, ...] = (
    "qwen", "deepseek", "gpt-", "gpt4", "gpt-5", "glm", "llama", "mistral",
    "claude", "gigachat", "yandexgpt", "ollama/",
)
"""Маркеры model-specific промптов (P1-3 паттерна Local-First): 7B-хаки запрещены."""


def _walk_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    """Все строки документа с путями (для L11)."""
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for key, item in value.items():
            out.extend(_walk_strings(item, f"{path}.{key}"))
        return out
    if isinstance(value, list):
        out = []
        for i, item in enumerate(value):
            out.extend(_walk_strings(item, f"{path}[{i}]"))
        return out
    return []


def lint_l11(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """Промпты model-agnostic: пустой ``prompt_overrides`` и нет упоминаний моделей.

    Паттерн Local-First (P1-3): ``prompt_overrides`` (ветки под конкретную модель)
    заменены механизируемым требованием — промпт одинаков для local и ext, иначе
    parity-ассерт движка недостоверен, а слабая модель тянет промпт вниз.
    """
    out: list[Finding] = []
    overrides = doc.get("prompt_overrides")
    if overrides:
        out.append(_f(
            "L11",
            "prompt_overrides должен быть пустым: промпты model-agnostic (P1-3)",
            "prompt_overrides",
        ))
    for path, text in _walk_strings(doc):
        low = text.lower()
        for marker in MODEL_NAME_MARKERS:
            if marker in low:
                out.append(_f(
                    "L11",
                    f"model-specific упоминание {marker!r} — промпт должен быть model-agnostic",
                    path,
                ))
                break
    return out


def lint_l10(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """Board single-writer: две секции writes без single_writer: false у разных узлов."""
    owners: dict[str, str] = {}
    out: list[Finding] = []
    for node in _nodes(doc):
        if node.get("single_writer") is False:
            continue
        nid = str(node.get("id"))
        for section in node.get("writes") or []:
            prev = owners.get(str(section))
            if prev is not None and prev != nid:
                out.append(_f(
                    "L10",
                    f"секцию {section} пишут два узла ({prev}, {nid})",
                    f"nodes.{nid}.writes",
                ))
            else:
                owners[str(section)] = nid
    return out


INPUT_CONSUMING_KINDS: tuple[str, ...] = ("llm-step", "tool-step", "critic-gate")
"""Узлы, чей промпт строится из секций-``inputs`` (движок ``engine._inputs``).

``human-gate`` исключён: его ``_inputs`` не вызывается — движок спрашивает
человека, а не собирает промпт (см. ``engine._run_node``).
"""


def lint_l12(doc: dict, registry: Any, base_dir: Path | None) -> list[Finding]:
    """``inputs`` объявлен и непуст у узлов-потребителей (Ф6-a 6a.3).

    Отсутствие ключа и пустой список движок трактует одинаково —
    ``engine._inputs`` молча подставляет ВЕСЬ борд в промпт; на дефицитной
    local-полке это вымывает бюджет контекста (analysis Ф6-a). Правило
    требует явный список секций; шейпинг сжимает данные, но не лечит
    неявную зависимость от всего борта.
    """
    out: list[Finding] = []
    for node in _nodes(doc):
        if node.get("kind") not in INPUT_CONSUMING_KINDS:
            continue
        inputs = node.get("inputs")
        if inputs:
            continue
        reason = "пустой inputs" if inputs is not None else "нет ключа inputs"
        out.append(_f(
            "L12",
            f"узел {node.get('id')} ({node.get('kind')}): {reason} — "
            "движок подставит весь борд; задайте явный список секций",
            f"nodes.{node.get('id')}.inputs",
        ))
    return out
