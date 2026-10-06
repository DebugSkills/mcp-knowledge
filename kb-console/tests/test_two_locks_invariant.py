"""Инвариант двух замков I11: сессии ⟂ зона (arch-2026-10-05-ai-workspace, Ф2 #8).

План (plans/arch-2026-10-05-ai-workspace-plan.md, стр. 117): доказать
ОРТОГОНАЛЬНОСТЬ двух независимых осей доступа AI-верстака:

- Замок A — пространство сессий (``ConversationStore``, I11): серверная
  per-user hard-isolation; ключ ``ws:sess:{user}:{session}``, индекс
  ``ws:sessidx:{user}``. Ни один пользователь не читает/удаляет чужие
  сессии — даже с тем же именем сессии (негатив #8) и даже при крафте
  разделителя ``:`` в user/session (#4).
- Замок B — зонный гейт (``ws_zone``): зона определяется РОЛЬЮ
  (admin→private; editor/contributor/None/unknown→public, fail-closed),
  НЕ сессией/контентом. В tool-loop зона инжектится сервером в
  ``search_knowledge`` и не подменяется аргументами модели.

Ортогональность (главное): смена оси «пользователь» не влияет на ось
«зона», и наоборот. Мета-механика: API замка A не принимает role/zone,
API замка B не принимает user; роль не входит в ключ сессии, user не
входит в зону.

Reuse (без дублей): ``FakeRedis`` — из ``test_conversations``;
``FakeMCP``/``FakeLLM``/``_tool_call_event`` — из ``test_tool_loop``
(прецедент кросс-импорта: tests/test_role_zone_matrix.py:30). Моки —
реальных типов (FakeMCP — подкласс MCPClient, правило 10).

Ассерты — на РЕАЛЬНОМ контенте чужих ключей (носитель), не на счётчиках.
"""

from __future__ import annotations

import inspect
import json
import re

import pytest
from test_conversations import FakeRedis
from test_tool_loop import FakeLLM, FakeMCP, _tool_call_event

from kb_console.core.conversations import ConversationStore
from kb_console.core.tool_loop import SEARCH_TOOL_NAME, run_turn
from kb_console.core.ws_zone import (
    PRIVATE_ZONE,
    PUBLIC_ZONE,
    zone_for_role,
    zones_for_role,
)

USERS = ("u1", "u2")
ROLES = ("admin", "contributor")
ROLE_BASE_ZONE = {"admin": PRIVATE_ZONE, "contributor": PUBLIC_ZONE}

_SESS_KEY_RE = re.compile(r"^ws:sess:[^:]+:[^:]+$")
_IDX_KEY_RE = re.compile(r"^ws:sessidx:[^:]+$")


@pytest.fixture
def fake() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def store(fake: FakeRedis) -> ConversationStore:
    return ConversationStore(fake, clock=fake.time)


def _all_keys(fake_redis: FakeRedis) -> set[str]:
    """Все ключи носителя (внутренние dict'ы тестового фейка — допустимо)."""
    return set(fake_redis._hashes) | set(fake_redis._zsets)


# ── Замок A: изоляция сессий (per-user, HARD) ───────────────────────────


class TestLockAIsolation:
    def test_same_session_name_no_cross_user_text_leak(
        self, store: ConversationStore
    ) -> None:
        """#1: один session='s1' у двоих — каждый видит ТОЛЬКО свои тексты."""
        store.append("userA", "s1", {"role": "user", "content": "A-SECRET: план private"})
        store.append("userA", "s1", "A-SECRET: сырая строка")
        store.append("userB", "s1", {"role": "user", "content": "B-OWN: публичный вопрос"})
        store.append("userB", "s1", "B-OWN: черновик")
        hist_a = store.history("userA", "s1")
        hist_b = store.history("userB", "s1")
        assert hist_a == [
            {"role": "user", "content": "A-SECRET: план private"},
            "A-SECRET: сырая строка",
        ]
        assert hist_b == [
            {"role": "user", "content": "B-OWN: публичный вопрос"},
            "B-OWN: черновик",
        ]
        # пересечение текстов пусто (носитель, не счётчик)
        assert {str(m) for m in hist_a} & {str(m) for m in hist_b} == set()
        assert "B-OWN" not in json.dumps(hist_a, ensure_ascii=False)
        assert "A-SECRET" not in json.dumps(hist_b, ensure_ascii=False)

    def test_sessions_delete_purge_are_per_user(self, store: ConversationStore) -> None:
        """#2: индексы независимы; delete/purge юзера A не трогают сессии B."""
        store.append("A", "s1", "a-1")
        store.append("A", "s2", "a-2")
        store.append("B", "s1", "b-1")
        store.append("B", "s3", "b-3")
        assert sorted(store.sessions("A")) == ["s1", "s2"]
        assert sorted(store.sessions("B")) == ["s1", "s3"]

        # delete("A","s1") не трогает s1 у B
        store.delete("A", "s1")
        assert store.history("A", "s1") == []
        assert store.sessions("A") == ["s2"]
        assert store.history("B", "s1") == ["b-1"], "контент B обязан уцелеть"
        assert "s1" in store.sessions("B")

        # purge_user("A") не трогает B
        assert store.purge_user("A") == 1  # s1 уже снята delete'ом — в индексе A осталась только s2
        assert store.sessions("A") == []
        assert sorted(store.sessions("B")) == ["s1", "s3"]
        assert store.history("B", "s1") == ["b-1"]
        assert store.history("B", "s3") == ["b-3"]

    def test_key_schema_every_session_key_carries_user(
        self, store: ConversationStore, fake: FakeRedis
    ) -> None:
        """#3: структурно ключи ровно ws:sess:{user}:{session} / ws:sessidx:{user}."""
        store.append("userA", "s1", "a")
        store.append("userB", "s1", "b")
        store.append("userB", "s2", "b2")
        assert _all_keys(fake) == {
            "ws:sess:userA:s1",
            "ws:sess:userB:s1",
            "ws:sess:userB:s2",
            "ws:sessidx:userA",
            "ws:sessidx:userB",
        }
        for key in fake._hashes:
            assert _SESS_KEY_RE.match(key), f"невалидный ключ сессии: {key!r}"
            assert key.split(":")[2] in {"userA", "userB"}, "нет user-компоненты"
        for key in fake._zsets:
            assert _IDX_KEY_RE.match(key), f"невалидный ключ индекса: {key!r}"
            assert key.split(":")[2] in {"userA", "userB"}
        # «беспользовательных» ключей сессий (ws:sess:{session}) нет
        assert not [k for k in _all_keys(fake) if re.match(r"^ws:sess:[^:]+$", k)]
        # контент в носителе разнесён по user-ключам
        assert "a" in fake._hashes["ws:sess:userA:s1"].values()
        assert "b" in fake._hashes["ws:sess:userB:s1"].values()
        assert "b" not in fake._hashes["ws:sess:userA:s1"].values()

    @pytest.mark.parametrize(
        ("user", "session"),
        [
            ("a", "b:c"),  # разделитель в session
            ("a:b", "c"),  # разделитель в user
            ("", "s"),  # пустой user
            ("a", ""),  # пустая сессия
            ("a:b:c", "s"),  # множественный крафт
        ],
    )
    def test_separator_injection_rejected_before_redis(
        self, fake: FakeRedis, user: str, session: str
    ) -> None:
        """#4: ':'/пустые части → ValueError ДО Redis; коллизия недостижима.

        «a»+«b:c» и «a:b»+«c» дали бы ОДИН ключ ws:sess:a:b:c (чужое
        пространство имён) — валидатор _validate_part отвергает ОБЕ стороны,
        носитель не тронут.
        """
        store = ConversationStore(fake, clock=fake.time)
        with pytest.raises(ValueError):
            store.append(user, session, "m")
        assert _all_keys(fake) == set(), "отказ обязан быть ДО обращения к Redis"


# ── Замок B: зона от роли (fail-closed) ─────────────────────────────────


class TestLockBZoneFromRole:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("admin", PRIVATE_ZONE),
            ("editor", PUBLIC_ZONE),
            ("contributor", PUBLIC_ZONE),
            (None, PUBLIC_ZONE),  # нет роли → fail-closed
            ("unknown", PUBLIC_ZONE),  # неизвестная роль → fail-closed
            ("", PUBLIC_ZONE),  # пустая роль → fail-closed
        ],
    )
    def test_zone_for_role_matrix(self, role: str | None, expected: str) -> None:
        """#5: зона — функция ТОЛЬКО роли; ниже admin и при unknown — public."""
        assert zone_for_role(role) == expected

    def test_zones_for_role_private_only_for_admin(self) -> None:
        """#5: admin — базовая зона private (+добор public); не-admin — только public.

        Контракт модуля: zones_for_role('admin') == ('private', 'public') —
        базовая зона роли первой (у admin это private), затем добор public
        (сервисный ключ read+zone=both). В формулировке задачи «==('private',')»
        подразумевалась базовая зона — SSOT-контракт проверяем фактический.
        """
        assert zones_for_role("admin") == (PRIVATE_ZONE, PUBLIC_ZONE)
        assert zones_for_role("admin")[0] == PRIVATE_ZONE  # базовая — private
        for role in ("editor", "contributor", None, "unknown", ""):
            assert zones_for_role(role) == (PUBLIC_ZONE,), (
                f"{role!r} не должен получать private"
            )

    @pytest.mark.parametrize("server_zone", [PUBLIC_ZONE, PRIVATE_ZONE])
    async def test_run_turn_injects_zone_model_args_ignored(
        self, server_zone: str
    ) -> None:
        """#6: инжектируемая зона уходит в search_knowledge как params['zone'];
        zone из аргументов модели (JSON-строка И dict) игнорируется."""
        opposite = PRIVATE_ZONE if server_zone == PUBLIC_ZONE else PUBLIC_ZONE
        for args_form in (
            json.dumps({"query": "два замка", "zone": opposite}),
            {"query": "два замка", "zone": opposite},
        ):
            fake_mcp = FakeMCP(result={"results": [{"knowledge_id": "k1"}]})
            llm = FakeLLM(
                [
                    [_tool_call_event("c1", args_form)],
                    ["Готово"],
                ]
            )
            text = await run_turn(
                [{"role": "user", "content": "найди"}],
                session_id=f"lockB-{server_zone}-{type(args_form).__name__}",
                zone=server_zone,
                mcp_client=fake_mcp,
                llm_stream=llm,
            )
            assert text == "Готово"
            assert len(fake_mcp.calls) == 1
            name, params = fake_mcp.calls[0]
            assert name == SEARCH_TOOL_NAME
            assert params["zone"] == server_zone, "зона вызова — серверная, не из args"
            assert params["query"] == "два замка"


# ── Ортогональность: сессии ⟂ зона ──────────────────────────────────────


class TestOrthogonality:
    @pytest.mark.parametrize("role", ROLES)
    @pytest.mark.parametrize("user", USERS)
    def test_matrix_zone_per_role_isolation_per_user(
        self, store: ConversationStore, user: str, role: str
    ) -> None:
        """#7: для фиксированной роли зона идентична у u1/u2 (зона ⟂ пользователь);
        для фиксированного пользователя изоляция одинакова у обеих ролей
        (сессии ⟂ роль)."""
        other = "u2" if user == "u1" else "u1"
        # ось B: зона зависит только от роли — не от user
        assert zone_for_role(role) == ROLE_BASE_ZONE[role]
        assert zones_for_role(role)[0] == ROLE_BASE_ZONE[role]
        # ось A: изоляция держится под любой ролью (роль не влияет на замок A)
        store.append(other, "s1", f"{other}-SECRET под ролью {role}")
        store.append(user, "s1", f"{user}-OWN под ролью {role}")
        assert store.history(user, "s1") == [f"{user}-OWN под ролью {role}"]
        assert store.history(other, "s1") == [f"{other}-SECRET под ролью {role}"]
        assert store.sessions(user) == ["s1"]
        assert store.sessions(other) == ["s1"]

    def test_meta_session_keys_do_not_depend_on_role(
        self, store: ConversationStore, fake: FakeRedis
    ) -> None:
        """Мета: набор ключей сессий при смене роли НЕ меняется — роль не
        входит в ключ сессии (ключи — функция только (user, session))."""
        store.append("u1", "s1", "m1")
        keys_before = frozenset(_all_keys(fake))
        # «пользователь сменил роль» и пишет дальше — форма ключей та же
        store.append("u1", "s1", "m2")
        keys_after = frozenset(_all_keys(fake))
        assert keys_before == keys_after == {"ws:sess:u1:s1", "ws:sessidx:u1"}
        for role in ("admin", "editor", "contributor"):
            assert not any(role in k for k in keys_before), "роль не компонента ключа"

    def test_meta_zone_does_not_depend_on_user_or_session_state(
        self, store: ConversationStore
    ) -> None:
        """Мета: результат zone_for_role не меняется ни от пользователя, ни от
        сессионного состояния (user не входит в зону)."""
        zone_before = {role: zone_for_role(role) for role in ROLES}
        store.append("u1", "s1", "m1")
        store.append("u2", "s1", "m2")
        store.delete("u1", "s1")
        store.purge_user("u2")
        zone_after = {role: zone_for_role(role) for role in ROLES}
        assert zone_after == zone_before

    def test_meta_lock_a_api_has_no_role_or_zone_axis(self) -> None:
        """Мета-механика: API замка A не принимает role/zone — ось зоны
        недостижима из сессионного замка."""
        for method in ("append", "history", "sessions", "delete", "purge_user"):
            params = inspect.signature(getattr(ConversationStore, method)).parameters
            assert "role" not in params and "zone" not in params, method

    def test_meta_lock_b_api_has_no_user_or_session_axis(self) -> None:
        """Мета-механика: API замка B не принимает user/session — ось
        пользователя недостижима из зонного замка."""
        for fn in (zone_for_role, zones_for_role):
            params = inspect.signature(fn).parameters
            assert not {"user", "username", "session"} & set(params), fn.__name__


# ── Совместный негатив #8: оба замка одновременно ───────────────────────


class TestJointNegativeSameSessionId:
    async def test_contributor_with_admin_session_id_both_locks_hold(self) -> None:
        """#8: contributor с session_id, РАВНЫМ session_id админа:
        (а) его зона остаётся public (замок B не подделать совпавшим id);
        (б) его history НЕ содержит сообщений админа (замок A).
        Оба замка держатся одновременно.
        """
        fake = FakeRedis()
        store = ConversationStore(fake, clock=fake.time)
        same = "shared-session"

        # Админ: ход с зоной его роли (private) + секретная реплика в сессию
        store.append(
            "admin-user", same, {"role": "user", "content": "ADMIN-SECRET: приватный аудит"}
        )
        admin_mcp = FakeMCP(result={"results": []})
        admin_llm = FakeLLM(
            [
                [_tool_call_event("c-a", {"query": "внутренний регламент"})],
                ["готово-админ"],
            ]
        )
        assert (
            await run_turn(
                [{"role": "user", "content": "найди внутренний регламент"}],
                session_id=same,
                zone=zone_for_role("admin"),
                mcp_client=admin_mcp,
                llm_stream=admin_llm,
            )
            == "готово-админ"
        )
        assert admin_mcp.calls[0][1]["zone"] == PRIVATE_ZONE

        # Contributor: ТОТ ЖЕ session_id + попытка протащить zone=private в args
        store.append(
            "contrib-user", same, {"role": "user", "content": "CONTRIB-OWN: публичный вопрос"}
        )
        contrib_mcp = FakeMCP(result={"results": []})
        contrib_llm = FakeLLM(
            [
                [
                    _tool_call_event(
                        "c-c", {"query": "публичные лекции", "zone": "private"}
                    )
                ],
                ["готово-контрибьютор"],
            ]
        )
        assert (
            await run_turn(
                [{"role": "user", "content": "найди публичные лекции"}],
                session_id=same,  # РАВЕН session_id админа
                zone=zone_for_role("contributor"),
                mcp_client=contrib_mcp,
                llm_stream=contrib_llm,
            )
            == "готово-контрибьютор"
        )

        # (а) замок B: зона вызова contributor — public, несмотря на совпавший id
        # и на zone=private в аргументах модели
        assert contrib_mcp.calls[0][1]["zone"] == PUBLIC_ZONE
        # (б) замок A: истории разнесены по user, утечки текстов нет
        admin_hist = store.history("admin-user", same)
        contrib_hist = store.history("contrib-user", same)
        assert len(admin_hist) == 1 and len(contrib_hist) == 1
        assert "ADMIN-SECRET" not in json.dumps(contrib_hist, ensure_ascii=False)
        assert "CONTRIB-OWN" not in json.dumps(admin_hist, ensure_ascii=False)
        # и структурно: два разных ключа в носителе
        assert set(fake._hashes) >= {
            "ws:sess:admin-user:shared-session",
            "ws:sess:contrib-user:shared-session",
        }
