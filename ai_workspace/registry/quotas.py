"""Квоты participant-ролей AI-верстака — схема + fail-closed фасад (Ф4.1).

trace_id: arch-2026-10-05-ai-workspace. Вариант V2: participant-роли аккаунтов
(admin/member/guest) и их лимиты живут в ``quotas.yaml`` отдельно от node-ролей
режимов (``roles.yaml``). Основание — решения оператора D1-D8 (D2 пресеты
приоритетов, D3 токены/день, D4 ₽/мес hard + per-user зеркало, D6 conc,
D7 сброс 00:00, D8 приоритет per-job + per-account).

Контур валидации — чистая функция ``validate_quotas`` по паттерну
``orchestrator/mode_schema.py``: собирает ВСЕ дефекты (Findings) с кодом и
путём, а не падает на первом KeyError. Фасад ``QuotaRegistry`` fail-closed:
каждая (пере)загрузка валидирует документ, любой finding -> ``RegistryError``
с перечнем; молчаливых дефолтов и частичных состояний нет.

Ref-целостность: ``grants`` ссылаются на КЛАССЫ моделей из
``model_classes.yaml`` (heavy/fast/local-only); local/ext — это полки LiteLLM,
а не классы. «Грант на несуществующий класс» = ошибка загрузки (аналог R4
«грант на несуществующую модель»).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ai_workspace.registry import Registry, RegistryError

__all__ = [
    "BUDGET_FIELDS",
    "CURRENCIES",
    "PARTICIPANT_FIELDS",
    "PRIORITIES",
    "REQUIRED_FIELDS",
    "Budget",
    "Finding",
    "Quota",
    "QuotaRegistry",
    "validate_quotas",
]

#: Маппинг participant-роли на MULT-очередь (решение D2)
PRIORITIES: frozenset[str] = frozenset({"high", "med", "low"})
#: Валюты бюджетов (D4 — RUB; USD — запас на смену провайдера)
CURRENCIES: frozenset[str] = frozenset({"RUB", "USD"})

#: Обязательные ключи документа quotas.yaml
REQUIRED_FIELDS: tuple[str, ...] = ("version", "defaults", "participants", "budgets")
#: Обязательные поля каждой participant-роли
PARTICIPANT_FIELDS: tuple[str, ...] = (
    "priority",
    "tokens_per_day",
    "conc",
    "grants",
)
#: Обязательные поля каждой записи budgets
BUDGET_FIELDS: tuple[str, ...] = (
    "currency",
    "period",
    "limit",
    "per_user_mirror",
    "reconcile",
)

SEVERITY_ERROR = "error"


@dataclass(frozen=True)
class Finding:
    """Замечание валидатора квот; в контуре схемы все severity — ``error``."""

    code: str
    severity: str
    message: str
    path: str


@dataclass(frozen=True)
class Quota:
    """Лимиты participant-роли (D3/D6/D8); ``None`` = только общий лимит K."""

    role: str
    priority: str
    tokens_per_day: int | None
    conc: int | None
    grants: tuple[str, ...]


@dataclass(frozen=True)
class Budget:
    """Глобальный бюджет контура провайдера (D4); сверка с LiteLLM."""

    name: str
    currency: str
    period: str
    limit: int | float
    per_user_mirror: bool
    reconcile: str


def _err(code: str, message: str, path: str) -> Finding:
    return Finding(code=code, severity=SEVERITY_ERROR, message=message, path=path)


def _is_int(value: Any) -> bool:
    """Целое без bool: YAML ``yes``/``no`` -> bool, который наследует int."""
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_quotas(doc: Any, model_classes: Mapping[str, Any]) -> list[Finding]:
    """Проверить документ quotas.yaml по схеме Ф4.1.

    ``model_classes`` — реестр классов моделей (``registry.get("model_classes")``)
    для ref-целостности grants. Чистая функция без I/O.
    """
    findings: list[Finding] = []
    if not isinstance(doc, Mapping):
        return [_err("Q1", "квоты должны быть YAML-отображением (mapping)", "$")]

    # Q1: обязательные ключи верхнего уровня.
    for field in REQUIRED_FIELDS:
        if field not in doc:
            findings.append(
                _err("Q1", f"обязательное поле отсутствует: {field}", field)
            )

    # Q2: version — целое >= 1.
    if "version" in doc:
        version = doc["version"]
        if not _is_int(version) or version < 1:
            findings.append(
                _err(
                    "Q2",
                    f"version должен быть целым >= 1, получено: {version!r}",
                    "version",
                )
            )

    # Q3: defaults.role — непустая строка.
    defaults_role: str | None = None
    defaults = doc.get("defaults")
    if "defaults" in doc:
        if not isinstance(defaults, Mapping):
            findings.append(_err("Q3", "defaults должен быть отображением", "defaults"))
        else:
            role = defaults.get("role")
            if not _nonempty_str(role):
                findings.append(
                    _err(
                        "Q3",
                        f"defaults.role должен быть непустой строкой, "
                        f"получено: {role!r}",
                        "defaults.role",
                    )
                )
            else:
                defaults_role = role

    # Q4: participants — непустое отображение роль -> лимиты.
    participants = doc.get("participants")
    participant_names: set[str] = set()
    if "participants" in doc:
        if not isinstance(participants, Mapping) or not participants:
            findings.append(
                _err(
                    "Q4",
                    "participants должен быть непустым отображением роль -> лимиты",
                    "participants",
                )
            )
        else:
            participant_names = {
                role
                for role in participants
                if isinstance(role, str) and role.strip()
            }
            for role, spec in participants.items():
                path = f"participants.{role}"
                if not _nonempty_str(role):
                    findings.append(
                        _err(
                            "Q3",
                            f"имя роли должно быть непустой строкой, "
                            f"получено: {role!r}",
                            path,
                        )
                    )
                if not isinstance(spec, Mapping):
                    findings.append(
                        _err("Q4", f"роль {role!r} должна быть отображением", path)
                    )
                    continue
                _validate_participant(role, spec, model_classes, findings)

    # Q9: defaults.role существует в participants.
    if (
        defaults_role is not None
        and participant_names
        and defaults_role not in participant_names
    ):
        findings.append(
            _err(
                "Q9",
                f"defaults.role {defaults_role!r} не существует в participants; "
                f"известны: {sorted(participant_names)}",
                "defaults.role",
            )
        )

    # Q10-Q13: бюджеты контуров (обязана быть запись ext, D4).
    budgets = doc.get("budgets")
    if "budgets" in doc:
        if not isinstance(budgets, Mapping) or not budgets:
            findings.append(
                _err(
                    "Q10",
                    "budgets должен быть непустым отображением контур -> лимит",
                    "budgets",
                )
            )
        else:
            if "ext" not in budgets:
                findings.append(
                    _err(
                        "Q10",
                        "budgets обязан содержать запись 'ext' (внешний контур, D4)",
                        "budgets",
                    )
                )
            for name, spec in budgets.items():
                _validate_budget(name, spec, findings)

    return findings


def _validate_participant(
    role: str,
    spec: Mapping[str, Any],
    model_classes: Mapping[str, Any],
    findings: list[Finding],
) -> None:
    """Проверить лимиты одной participant-роли (путь ``participants.{role}``)."""
    path = f"participants.{role}"
    for field in PARTICIPANT_FIELDS:
        if field not in spec:
            findings.append(
                _err("Q1", f"обязательное поле отсутствует: {field}", f"{path}.{field}")
            )

    # Q5: priority — enum (D2, маппинг на MULT-очередь).
    priority = spec.get("priority")
    if "priority" in spec and priority not in PRIORITIES:
        findings.append(
            _err(
                "Q5",
                f"priority должен быть одним из {sorted(PRIORITIES)}, "
                f"получено: {priority!r}",
                f"{path}.priority",
            )
        )

    # Q6: tokens_per_day — целое >= 0 или null (D3; null = только общий K).
    tokens = spec.get("tokens_per_day")
    if (
        "tokens_per_day" in spec
        and tokens is not None
        and (not _is_int(tokens) or tokens < 0)
    ):
            findings.append(
                _err(
                    "Q6",
                    f"tokens_per_day должен быть целым >= 0 или null, "
                    f"получено: {tokens!r}",
                    f"{path}.tokens_per_day",
                )
            )

    # Q7: conc — целое >= 1 или null (D6; null = только общий K).
    conc = spec.get("conc")
    if "conc" in spec and conc is not None and (not _is_int(conc) or conc < 1):
            findings.append(
                _err(
                    "Q7",
                    f"conc должен быть целым >= 1 или null, получено: {conc!r}",
                    f"{path}.conc",
                )
            )

    # Q8: grants — ref-целостность на классы моделей.
    grants = spec.get("grants")
    if "grants" in spec:
        if not isinstance(grants, list) or not grants:
            findings.append(
                _err(
                    "Q8",
                    f"grants должен быть непустым списком классов моделей, "
                    f"получено: {grants!r}",
                    f"{path}.grants",
                )
            )
        else:
            for grant in grants:
                if not _nonempty_str(grant):
                    findings.append(
                        _err(
                            "Q8",
                            f"элемент grants должен быть непустой строкой, "
                            f"получено: {grant!r}",
                            f"{path}.grants",
                        )
                    )
                elif grant not in model_classes:
                    findings.append(
                        _err(
                            "Q8",
                            f"grant на несуществующий класс модели: {grant!r}; "
                            f"известны: {sorted(model_classes)}",
                            f"{path}.grants",
                        )
                    )


def _validate_budget(name: Any, spec: Any, findings: list[Finding]) -> None:
    """Проверить одну запись budgets (путь ``budgets.{name}``)."""
    path = f"budgets.{name}"
    if not isinstance(spec, Mapping):
        findings.append(_err("Q10", f"бюджет {name!r} должен быть отображением", path))
        return
    for field in BUDGET_FIELDS:
        if field not in spec:
            findings.append(
                _err("Q1", f"обязательное поле отсутствует: {field}", f"{path}.{field}")
            )

    # Q11: limit — число > 0 (глобальный hard, D4).
    limit = spec.get("limit")
    if "limit" in spec and (
        isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0
    ):
        findings.append(
            _err(
                "Q11",
                f"limit должен быть числом > 0, получено: {limit!r}",
                f"{path}.limit",
            )
        )

    # Q12: currency — enum.
    currency = spec.get("currency")
    if "currency" in spec and currency not in CURRENCIES:
        findings.append(
            _err(
                "Q12",
                f"currency должен быть одним из {sorted(CURRENCIES)}, "
                f"получено: {currency!r}",
                f"{path}.currency",
            )
        )

    # Q3: period/reconcile — непустые строки.
    for field in ("period", "reconcile"):
        value = spec.get(field)
        if field in spec and not _nonempty_str(value):
            findings.append(
                _err(
                    "Q3",
                    f"{field} должен быть непустой строкой, получено: {value!r}",
                    f"{path}.{field}",
                )
            )

    # Q13: per_user_mirror — bool (зеркало per-user, D4).
    mirror = spec.get("per_user_mirror")
    if "per_user_mirror" in spec and not isinstance(mirror, bool):
        findings.append(
            _err(
                "Q13",
                f"per_user_mirror должен быть bool, получено: {mirror!r}",
                f"{path}.per_user_mirror",
            )
        )


class QuotaRegistry:
    """Fail-closed фасад над ``quotas.yaml``: валидация + hot-reload + доступ.

    Первый доступ валидирует документ; далее каждая перезагрузка (своей
    ``Registry.reload_if_changed`` или внешняя — по identity кэша) повторяет
    валидацию: испорченный hot-reload -> ``RegistryError``, а не тихие
    старые/дефолтные значения.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._doc: Mapping[str, Any] | None = None
        self._quotas: dict[str, Quota] = {}
        self._default_role: str = ""
        self._budgets: dict[str, Budget] = {}

    def _ensure(self) -> None:
        changed = self._registry.reload_if_changed()
        doc = self._registry.get("quotas")
        # identity-проверка ловит внешнюю перезагрузку Registry без смены mtime
        if changed or doc is not self._doc:
            self._reparse(doc)

    def _reparse(self, doc: Mapping[str, Any]) -> None:
        findings = validate_quotas(doc, self._registry.get("model_classes"))
        if findings:
            details = "; ".join(
                f"[{f.code}] {f.path}: {f.message}" for f in findings
            )
            raise RegistryError(
                f"квоты невалидны ({self._registry.path_for('quotas')}): {details}"
            )
        self._quotas = {
            role: Quota(
                role=role,
                priority=spec["priority"],
                tokens_per_day=spec["tokens_per_day"],
                conc=spec["conc"],
                grants=tuple(spec["grants"]),
            )
            for role, spec in doc["participants"].items()
        }
        self._default_role = doc["defaults"]["role"]
        self._budgets = {
            name: Budget(
                name=name,
                currency=spec["currency"],
                period=spec["period"],
                limit=spec["limit"],
                per_user_mirror=spec["per_user_mirror"],
                reconcile=spec["reconcile"],
            )
            for name, spec in doc["budgets"].items()
        }
        self._doc = doc

    def quota_for(self, role: str) -> Quota:
        """Квота роли; неизвестная роль -> квота ``defaults.role`` (не исключение)."""
        self._ensure()
        quota = self._quotas.get(role)
        if quota is None:
            quota = self._quotas[self._default_role]
        return quota

    @property
    def budgets(self) -> Mapping[str, Budget]:
        """Бюджеты контуров (D4); read-only отображение имя -> Budget."""
        self._ensure()
        return MappingProxyType(self._budgets)
