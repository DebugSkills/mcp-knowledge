"""Mode engine AI-верстака: исполнение графа режима (Ф3.5b-2).

Спека: plans/_provenance/arch-2026-10-05-ai-workspace/…-mode-engine-spec.md §3, §5.

Инварианты плана (REV.12): **I4** — эффект имеет ``compute_effect_id(job,node,effect)``
без attempt, повтор шага отдаёт кэш (ledger), а не повторяет эффект; **I9** — секции
пишет только engine через ``BoardStore.write_sections`` CAS, версия доски живёт в
``job.board_versions["board"]``; **I3** — статус меняет только ``JobStore``
(продвижение курсора — ``JobStore.patch`` без смены статуса); **human-gate** —
``running→waiting_human`` + single-use ``resume_token``, ответ ``approve|edit|reject``,
``edit`` пишется секцией через CAS.

Зависимости инъектируются (jobs/boards/llm/mcp/ledger/registry) → граф исполним на
фейках в юнит-тестах и на ws-redis + LiteLLM + MCP в проде.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ai_workspace.orchestrator.graph import (
    EngineError,
    ModeGraph,
    Node,
    UnknownNode,
    UnsupportedNode,
    load_mode,
)
from ai_workspace.orchestrator.job import JobRecord, JobState, compute_effect_id
from ai_workspace.orchestrator.ledger import Ledger, MemoryLedger, RedisLedger

__all__ = [
    "EgressBlocked",
    "EngineError",
    "EngineResult",
    "LLMClient",
    "MemoryLedger",
    "ModeEngine",
    "ModeGraph",
    "NodeFailure",
    "RedisLedger",
    "TokenInvalid",
    "ToolClient",
    "UnknownNode",
    "UnsupportedNode",
    "load_mode",
]

MAX_STEPS = 64
"""Предохранитель от бесконечного графа (не заменяет max_iterations критика)."""


class NodeFailure(EngineError):
    """Узел исчерпал retry и уронил шаг (job -> failed)."""


class TokenInvalid(EngineError):
    """resume_token не найден или уже использован (single-use)."""


class EgressBlocked(EngineError):
    """Зонный гейт сработал на границе вызова LLM: private не уходит в ext.

    Hardening паттерна Local-First (Ф3.10, P1 критика): зонный предикат живёт
    в ДВИЖКЕ, а не только в тестовом примитиве — иначе реальный маршрут
    zone→egress не покрыт.
    """


# ── порты (инъектируемые зависимости) ────────────────────────────────────


class LLMClient(Protocol):
    """Шлюз LiteLLM (минимальный контракт движка)."""

    def complete(
        self,
        *,
        role: str,
        model_class: str,
        prompt: str,
        inputs: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
    ) -> str: ...


class ToolClient(Protocol):
    """MCP-клиент (mcp-knowledge или другой сервер)."""

    def call(self, *, tool: str, args: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class EngineResult:
    """Итог прогона движка до терминала, паузы или предохранителя."""

    status: Literal["done", "paused", "failed", "stopped"]
    node: str
    board_version: int = 0
    resume_token: str | None = None
    verdict: str | None = None
    artifact_id: str | None = None
    detail: str = ""


class _Pause(Exception):
    """Внутренний сигнал human-gate: прогон останавливается, job -> waiting_human."""

    def __init__(self, node_id: str, token: str, prompt: str) -> None:
        super().__init__(node_id)
        self.node_id = node_id
        self.token = token
        self.prompt = prompt


class ModeEngine:
    """Исполняет граф режима над job-store и board-store с идемпотентными эффектами."""

    def __init__(
        self,
        *,
        jobs: Any,
        boards: Any,
        graph: ModeGraph,
        llm: LLMClient,
        mcp: ToolClient,
        ledger: Ledger,
        registry: Any | None = None,
        artifacts: Any | None = None,
        decoding: Any | None = None,
        seed_loader: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.jobs = jobs
        self.boards = boards
        self.graph = graph
        self.llm = llm
        self.mcp = mcp
        self.ledger = ledger
        self.registry = registry
        self.artifacts = artifacts
        self.decoding = decoding
        self.seed_loader = seed_loader
        self.clock = clock

    # ── публичный API ────────────────────────────────────────────────────

    def run(self, job_id: str, *, epoch: int, max_steps: int = MAX_STEPS) -> EngineResult:
        """Прогнать job до терминала/паузы/предохранителя."""
        rec = self.jobs.get(job_id)
        if rec.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            return EngineResult(status=self._terminal_status(rec.state), node=rec.cursor,
                                board_version=self._board_version(rec))
        if rec.state is JobState.WAITING_HUMAN:
            return EngineResult(status="paused", node=rec.cursor,
                                board_version=self._board_version(rec),
                                detail="job ожидает ответа человека")

        if rec.state in (JobState.QUEUED, JobState.SLEEPING, JobState.PREEMPTED):
            rec = self.jobs.transition(job_id, JobState.RUNNING, expect_version=rec.version, epoch=epoch)

        for _ in range(max_steps):
            node = self.graph.node(rec.cursor or self.graph.start())
            try:
                rec, verdict, next_id = self._run_node(job_id, rec, node, epoch)
            except _Pause as pause:
                cur = self.jobs.get(job_id)  # версия могла сдвинуться внутри узла
                rec = self.jobs.transition(
                    job_id, JobState.WAITING_HUMAN, expect_version=cur.version, epoch=epoch,
                    patch={"cursor": pause.node_id},
                )
                return EngineResult(status="paused", node=pause.node_id,
                                    board_version=self._board_version(rec),
                                    resume_token=pause.token, detail=pause.prompt)
            except EngineError as exc:
                cur = self.jobs.get(job_id)  # секция/версия могли обновиться до провала
                rec = self.jobs.transition(job_id, JobState.FAILED, expect_version=cur.version,
                                           epoch=epoch, patch={"cursor": node.id})
                return EngineResult(status="failed", node=node.id,
                                    board_version=self._board_version(rec), detail=str(exc))

            if next_id is None:
                rec = self.jobs.transition(job_id, JobState.DONE, expect_version=rec.version, epoch=epoch)
                return EngineResult(status="done", node=node.id,
                                    board_version=self._board_version(rec), verdict=verdict,
                                    artifact_id=self._persist_artifact(job_id, rec))
            rec = self.jobs.patch(job_id, expect_version=rec.version, epoch=epoch,
                                  patch={"cursor": next_id})
        return EngineResult(status="stopped", node=rec.cursor,
                            board_version=self._board_version(rec),
                            detail=f"предохранитель max_steps={max_steps}")

    def seed(self, job_id: str, sections: Mapping[str, str], *, epoch: int) -> JobRecord:
        """Записать входные секции job'а (``brief`` и пр.) до первого шага."""
        rec = self.jobs.get(job_id)
        versions = dict(rec.board_versions or {})
        new_v = self.boards.write_sections(
            dict(sections),
            expect_version=int(versions.get("board", 0)),
            writer_node="input",
            single_writer=True,
        )
        versions["board"] = int(new_v)
        return self.jobs.patch(
            job_id, expect_version=rec.version, epoch=epoch, patch={"board_versions": versions}
        )

    def resume(
        self,
        job_id: str,
        *,
        epoch: int,
        token: str,
        decision: str = "approve",
        edit: str | None = None,
    ) -> EngineResult:
        """Ответ человека на human-gate: ``approve`` | ``edit`` | ``reject``.

        ``edit`` пишется секцией gate-узла через board CAS (single-writer).
        Токен single-use: повторное предъявление → ``TokenInvalid``.
        """
        consumed = self.ledger.consume_token(job_id, token)
        if consumed is None:
            raise TokenInvalid(f"resume-токен для job {job_id!r} недействителен или использован")
        node_id = consumed
        rec = self.jobs.get(job_id)
        if rec.state is not JobState.WAITING_HUMAN:
            raise EngineError(f"job {job_id!r} не в waiting_human (state={rec.state.value})")
        # Durable-ответ гейта: approve делает его pass-through при повторном входе
        # (REVISE-петля), edit — показать снова после переработки.
        self.ledger.put(job_id, f"gate:{node_id}", {"decision": decision})

        if decision == "reject":
            rec = self.jobs.transition(job_id, JobState.FAILED, expect_version=rec.version,
                                       epoch=epoch, patch={"cursor": node_id})
            return EngineResult(status="failed", node=node_id,
                                board_version=self._board_version(rec), detail="человек отклонил")

        if decision == "edit":
            if edit is None:
                raise EngineError("decision='edit' требует непустой edit-текст")
            rec = self._write_section(job_id, rec, node_id, edit, epoch)

        rec = self.jobs.transition(job_id, JobState.RUNNING, expect_version=rec.version,
                                   epoch=epoch, patch={"cursor": node_id})
        next_id = self._gate_target(self.graph.node(node_id), decision)
        if next_id is None:
            rec = self.jobs.transition(job_id, JobState.DONE, expect_version=rec.version, epoch=epoch)
            return EngineResult(status="done", node=node_id, board_version=self._board_version(rec),
                                artifact_id=self._persist_artifact(job_id, rec))
        rec = self.jobs.patch(job_id, expect_version=rec.version, epoch=epoch, patch={"cursor": next_id})
        return self.run(job_id, epoch=epoch)

    # ── шаг узла ─────────────────────────────────────────────────────────

    def _run_node(
        self, job_id: str, rec: JobRecord, node: Node, epoch: int
    ) -> tuple[JobRecord, str | None, str | None]:
        kind = node.kind
        if kind == "llm-step":
            return self._llm_step(job_id, rec, node, epoch)
        if kind == "tool-step":
            return self._tool_step(job_id, rec, node, epoch)
        if kind == "critic-gate":
            return self._critic_gate(job_id, rec, node, epoch)
        if kind == "human-gate":
            answered = self.ledger.get(job_id, f"gate:{node.id}")
            if answered is not None and answered.get("decision") == "approve":
                # Гейт уже утверждён (напр. повторный вход через REVISE-петлю) —
                # не спрашиваем человека снова, идём по approve-ветке.
                return rec, None, self._gate_target(node, "approve")
            token = self.ledger.issue_token(job_id, node.id)
            raise _Pause(node.id, token, str(node.get("prompt", "Требуется подтверждение")))
        raise UnsupportedNode(f"kind {kind!r} (узел {node.id!r}) — Ф3.8+ (fork/join)")

    def _llm_step(self, job_id, rec, node, epoch):
        inputs = self._inputs(rec, node)
        prompt = self._prompt(node, inputs)
        eff = compute_effect_id(job_id, node.id, "llm:" + self._digest({"prompt": prompt, "inputs": inputs}))

        cached = self.ledger.get(job_id, f"fx:{eff}")
        if cached is not None:
            output = str(cached["output"])
        else:
            output = self._call_llm_with_retry(rec, node, prompt, inputs)
            self.ledger.put(job_id, f"fx:{eff}", {"output": output})

        section = self._out_section(node)
        rec = self._write_section(job_id, rec, section, output, epoch)
        return rec, None, self.graph.next_for(node.id)

    def _tool_step(self, job_id, rec, node, epoch):
        tool = node.get("tool")
        inputs = self._inputs(rec, node)
        args = {"inputs": inputs, "policy": node.get("policy")}
        eff = compute_effect_id(job_id, node.id, "tool:" + self._digest({"tool": tool, "args": args}))

        cached = self.ledger.get(job_id, f"fx:{eff}")
        if cached is not None:
            payload = cached["result"]
        else:
            try:
                payload = self.mcp.call(tool=tool, args=args)
            except Exception as exc:
                raise NodeFailure(f"tool-step {node.id!r}: {tool} упал: {exc}") from exc
            self.ledger.put(job_id, f"fx:{eff}", {"result": payload})

        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        rec = self._write_section(job_id, rec, self._out_section(node), text, epoch)
        return rec, None, self.graph.next_for(node.id)

    def _critic_gate(self, job_id, rec, node, epoch):
        inputs = self._inputs(rec, node)
        prompt = self._prompt(node, inputs)
        eff = compute_effect_id(job_id, node.id, "critic:" + self._digest({"prompt": prompt}))
        cached = self.ledger.get(job_id, f"fx:{eff}")
        if cached is not None:
            output = str(cached["output"])
        else:
            output = self._call_llm_with_retry(rec, node, prompt, inputs)
            self.ledger.put(job_id, f"fx:{eff}", {"output": output})

        verdict = self._parse_verdict(node, output)
        rec = self._write_section(job_id, rec, self._out_section(node), output, epoch)

        if verdict != "REVISE":
            return rec, verdict, self.graph.next_for(node.id, verdict)

        # REVISE: цикл на on_revise с ограничением max_iterations (§4б п.8).
        it_key = f"iter:{node.id}"
        counted = self.ledger.get(job_id, it_key) or {"n": 0}
        n = int(counted.get("n", 0)) + 1
        max_iter = int(node.get("max_iterations", 1))
        if n >= max_iter:
            raise NodeFailure(
                f"critic-gate {node.id!r}: достигнут max_iterations={max_iter} "
                f"на verdict=REVISE (on_max_iter: fail)"
            )
        self.ledger.put(job_id, it_key, {"n": n})
        return rec, verdict, node.get("on_revise", self.graph.start())

    # ── вспомогательное ──────────────────────────────────────────────────

    def shelf_for(self, model_class: str) -> str:
        """Полка по классу модели: реестр ``model_classes`` (heavy→ext, fast→local)."""
        if self.registry is None:
            return "local"
        try:
            classes = self.registry.get("model_classes") or {}
        except Exception:  # noqa: BLE001 — реестр необязателен для исполнения
            return "local"
        spec = classes.get(model_class) or {}
        shelf = spec.get("shelf")
        if shelf:
            return str(shelf)
        return "local"  # local-only / неизвестный класс — безопасный дефолт

    def _guard_zone(self, rec: JobRecord, node: Node, shelf: str) -> None:
        """Зонный гейт ДО вызова модели: ``zone=private`` не уходит на внешнюю полку."""
        if rec.zone == "private" and shelf != "local":
            raise EgressBlocked(
                f"zone=private недопустим вне local: узел {node.id!r} резолвится в полку {shelf!r} "
                f"(model_class={node.get('model_class')!r})"
            )

    def _call_llm_with_retry(self, rec: JobRecord, node: Node, prompt: str, inputs: Mapping[str, str]) -> str:
        model_class = str(node.get("model_class", "fast"))
        self._guard_zone(rec, node, self.shelf_for(model_class))
        params = self.decoding.as_params() if self.decoding is not None else {}
        attempts = int(node.get("retry", 0))
        last: Exception | None = None
        for _ in range(attempts + 1):
            try:
                return self.llm.complete(
                    role=str(node.get("role", node.id)),
                    model_class=model_class,
                    prompt=prompt,
                    inputs=dict(inputs),
                    params=params,
                )
            except Exception as exc:  # noqa: BLE001 — ошибка шлюза LLM = повод для retry
                last = exc
        raise NodeFailure(f"llm-step {node.id!r}: исчерпан retry={attempts}: {last}")

    def _inputs(self, rec: JobRecord, node: Node) -> dict[str, str]:
        """Входы узла = секции доски по ``inputs`` (+ критика из ревизионной петли).

        Секция-критика (``on_revise: <этот узел>``) добавляется, если уже есть на
        доске: иначе повторный прогон дал бы тот же ``effect_id`` (кэш) и петля
        REVISE не двигалась бы. Отсутствующая обязательная секция → fail-closed.
        """
        _, sections = self.boards.read()
        wanted = list(node.get("inputs") or [])
        out = {name: sections[name] for name in wanted if name in sections}
        for feedback in self._feedback_sections(node.id):
            if feedback in sections:
                out[feedback] = sections[feedback]
        if not wanted:
            out = {**sections, **out} if out else dict(sections)
        missing = [name for name in wanted if name not in sections]
        if missing:
            raise NodeFailure(
                f"узел {node.id!r}: нет входных секций {missing} на доске "
                f"(есть: {sorted(sections)})"
            )
        return out

    def _persist_artifact(self, job_id: str, rec: JobRecord) -> str | None:
        """Сохранить готовый документ в artifact-store (Ф3.7, I13).

        Источник — ``output.section``/``output.sections`` режима (композиция частей:
        документ + блок цитат citer'а).
        Нет store/секции/содержимого → ``None`` (KB не засоряется молча).
        """
        if self.artifacts is None:
            return None
        output = self.graph.doc.get("output") or {}
        wanted: list[str] = []
        if output.get("section"):
            wanted.append(str(output["section"]))
        wanted.extend(str(x) for x in (output.get("sections") or []))
        if not wanted:
            return None
        _, board = self.boards.read()
        parts = [board[name] for name in wanted if board.get(name)]
        if not parts:
            return None
        # Документ + приложения (напр. документ и блок цитат citer'а): части
        # идут отдельными секциями доски, артефакт — их композиция.
        content = "\n\n".join(parts)
        record = self.artifacts.save(
            content,
            user=rec.user,
            job_id=job_id,
            type=str(output.get("type", "document")),
            zone=rec.zone,
            mode=rec.mode,
            title=f"{rec.mode}:{job_id}",
        )
        return str(record.id)

    def _gate_target(self, node: Node, decision: str) -> str | None:
        """Куда идти после ответа человека: ``on_approve``/``on_edit`` узла, иначе edge.

        ``on_approve: null`` (или несуществующий узел) → обычное ребро графа
        (для финального gate это ``None`` → job done).
        """
        target = node.get(f"on_{decision}")
        if isinstance(target, str) and target in self.graph.nodes:
            return target
        return self.graph.next_for(node.id)

    def _feedback_sections(self, node_id: str) -> list[str]:
        """Секции-замечания, адресованные ``node_id``: критика (on_revise) и правки людей.

        Без этого повторный вход в узел дал бы тот же ``effect_id`` (кэш) и не
        потребил бы ни вердикт критика, ни правку оператора (gate on_edit/on_approve).
        """
        result: list[str] = []
        for other in self.graph.nodes.values():
            critic_loop = other.kind == "critic-gate" and other.get("on_revise") == node_id
            human_edit = other.kind == "human-gate" and node_id in (
                other.get("on_edit"),
                other.get("on_approve"),
            )
            if critic_loop or human_edit:
                result.append(self._out_section(other))
        return result

    def _prompt(self, node: Node, inputs: Mapping[str, str]) -> str:
        role = str(node.get("role", node.id))
        parts: list[str] = []
        seed_skill = None
        if self.registry is not None:
            try:
                seed_skill = (self.registry.get("roles") or {}).get(role, {}).get("seed_skill")
            except Exception:  # noqa: BLE001 — реестр необязателен для исполнения
                seed_skill = None
        if seed_skill and self.seed_loader is not None:
            parts.append(self.seed_loader(str(seed_skill)))
        parts.append(f"# РОЛЬ: {role}\n# УЗЕЛ: {node.id} ({node.kind})")
        for name, value in inputs.items():
            parts.append(f"## {name}\n{value}")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_verdict(node: Node, output: str) -> str:
        verdicts = [str(v).upper() for v in (node.get("verdicts") or ["PASS", "REVISE"])]
        for line in output.splitlines():
            token = line.strip().strip("*_# ").split(":")[0].strip().upper()
            if token in verdicts:
                return token
            for v in verdicts:
                if line.strip().upper().startswith(v):
                    return v
        raise NodeFailure(f"critic-gate {node.id!r}: вердикт не распознан в выводе (ожидались {verdicts})")

    def _out_section(self, node: Node) -> str:
        """Секция-выход узла: ``outputs[0]``, иначе ``verdict`` (critic-gate), иначе id.

        Согласовано с ``mode_lint._effective_outputs`` (critic-gate → ``verdict``);
        токены ``outputs`` берутся как есть (``document+refs`` — одна секция), чтобы
        не нарушать single-writer по секциям.
        """
        outputs = list(node.get("outputs") or [])
        if outputs:
            return str(outputs[0])
        if node.kind == "critic-gate":
            return "verdict"
        return node.id

    def _write_section(self, job_id: str, rec: JobRecord, section: str, value: str, epoch: int) -> JobRecord:
        """Записать секцию через board CAS и зафиксировать версию в job (I9)."""
        expect = self._board_version(rec)
        try:
            new_v = self.boards.write_sections(
                {section: value}, expect_version=expect, writer_node=section, single_writer=True
            )
        except Exception as exc:
            if type(exc).__name__ != "StaleBoard":
                raise
            cur, _ = self.boards.read()
            new_v = self.boards.write_sections(
                {section: value}, expect_version=cur, writer_node=section, single_writer=True
            )
        versions = dict(rec.board_versions or {})
        versions["board"] = int(new_v)
        return self.jobs.patch(job_id, expect_version=rec.version, epoch=epoch,
                               patch={"board_versions": versions})

    @staticmethod
    def _board_version(rec: JobRecord) -> int:
        return int((rec.board_versions or {}).get("board", 0))

    @staticmethod
    def _terminal_status(state: JobState) -> Literal["done", "failed"]:
        return "done" if state is JobState.DONE else "failed"

    @staticmethod
    def _digest(payload: Mapping[str, Any]) -> str:
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]
