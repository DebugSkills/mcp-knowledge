"""Прайс внешних моделей — схема + fail-closed фасад (P0-1 ревизии Ф4).

trace_id: arch-2026-10-05-ai-workspace. Закрывает P0-1 критики Ф4: до этого
реестра конверсии токены->деньги не существовало, ``ws:budget:*`` никто не
писал — бюджетный park был недостижим (E1-E4).

Валютная политика (зафиксирована): прайс провайдера в **USD** + фиксированный
курс ``rate_usd_rub`` в самом реестре (PLACEHOLDER, калибрует оператор).
Списание — в **целочисленных микро-₽** (1 ₽ = 10^6 микро-₽): цены за 1M
токенов конвертируются в микро-₽ за 1M при загрузке (``*_per_1m_micro``),
дальше — только целочисленная арифметика (``ShelfPrice.cost_micro``);
float для денег не используется нигде в пути списания.

Ref-целостность: ключи ``shelves`` — ПОЛКИ, обязаны существовать как
``shelf`` какого-то класса ``model_classes.yaml`` (heavy->ext, fast->local);
каждый бюджетный контур ``quotas.yaml: budgets`` обязан иметь прайс (без
прайса списание невозможно — fail-closed на загрузке, а не в рантайме).

Контур валидации — чистая функция ``validate_pricing`` по паттерну
``quotas.py`` (коды, все findings разом); фасад ``PricingRegistry``
fail-closed + hot-reload по identity (паттерн ``QuotaRegistry``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ai_workspace.registry import Registry, RegistryError
from ai_workspace.registry.quotas import SEVERITY_ERROR, Finding

__all__ = [
    "PRICING_CURRENCIES",
    "PRICING_FIELDS",
    "SHELF_FIELDS",
    "PricingRegistry",
    "ShelfPrice",
    "validate_pricing",
]

#: Валюта прайса провайдера: только USD (+ фиксированный курс rate_usd_rub).
#: Прайс провайдера в RUB напрямую не поддержан — DeepSeek публикует USD.
PRICING_CURRENCIES: frozenset[str] = frozenset({"USD"})

#: Валюта бюджет-счётчиков (микро-₽): budgets.*.currency обязан быть RUB —
#: конверсия прайса (USD -> микро-₽) зашита в ShelfPrice (P10).
BUDGET_CURRENCY = "RUB"

#: Обязательные ключи документа pricing.yaml
PRICING_FIELDS: tuple[str, ...] = ("version", "currency", "rate_usd_rub", "shelves")
#: Обязательные поля каждой записи shelves
SHELF_FIELDS: tuple[str, ...] = ("model", "input_per_1m", "output_per_1m", "source")

def _err(code: str, message: str, path: str) -> Finding:
    """Finding-ошибка (паттерн quotas._err; severity в схеме — всегда error)."""
    return Finding(code=code, severity=SEVERITY_ERROR, message=message, path=path)


MICRO_PER_UNIT = 1_000_000
"""Микро-единиц в одной единице валюты (1 ₽ = 10^6 микро-₽; SSOT конверсии)."""


@dataclass(frozen=True)
class ShelfPrice:
    """Цена полки: микро-₽ за 1M входных/выходных токенов (int — деньги без float).

    ``*_per_1m_usd`` — исходный прайс (наблюдение/аудит); ``*_per_1m_micro`` —
    конверсия ``usd * rate_usd_rub`` в микро-₽ при загрузке (единственное
    место с float — округление до целого micro, дальше только int).
    """

    shelf: str
    model: str
    input_per_1m_usd: float
    output_per_1m_usd: float
    input_per_1m_micro: int
    output_per_1m_micro: int
    source: str

    def cost_micro(self, tokens_in: int, tokens_out: int) -> int:
        """Стоимость вызова в микро-₽: ЧИСТАЯ целочисленная арифметика.

        ``tokens * per_1m_micro / 10^6`` с округлением к ближайшему
        (``+500_000`` перед делением). Детерминировано: одинаковый usage ->
        одинаковый int; накопление — только INCRBY int (дрейфа нет).
        """
        if tokens_in < 0 or tokens_out < 0:
            raise ValueError(
                f"токены должны быть >= 0, получено in={tokens_in} out={tokens_out}"
            )
        return (tokens_in * self.input_per_1m_micro + 500_000) // MICRO_PER_UNIT + (
            tokens_out * self.output_per_1m_micro + 500_000
        ) // MICRO_PER_UNIT


def _is_num(value: Any) -> bool:
    """Число без bool (YAML ``yes``/``no`` -> bool, наследующий int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_pricing(
    doc: Any,
    model_classes: Mapping[str, Any],
    budgets: Mapping[str, Any],
) -> list[Finding]:
    """Проверить документ pricing.yaml (чистая функция, без I/O).

    ``model_classes`` — реестр классов (``registry.get("model_classes")``):
    из него выводятся известные ПОЛКИ (``spec['shelf']``) для ref-целостности
    ключей ``shelves``. ``budgets`` — сырой ``quotas.yaml: budgets``: каждый
    бюджетный контур обязан иметь прайс (P7), валюта бюджета — RUB (P10).
    """
    findings: list[Finding] = []
    if not isinstance(doc, Mapping):
        return [_err("P1", "прайс должен быть YAML-отображением (mapping)", "$")]

    for field in PRICING_FIELDS:
        if field not in doc:
            findings.append(_err("P1", f"обязательное поле отсутствует: {field}", field))

    # P2: version — целое >= 1.
    if "version" in doc:
        version = doc["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            findings.append(
                _err("P2", f"version должен быть целым >= 1, получено: {version!r}", "version")
            )

    # P3: currency — только USD (прайс провайдера + фиксированный курс).
    if "currency" in doc and doc["currency"] not in PRICING_CURRENCIES:
        findings.append(
            _err(
                "P3",
                f"currency должен быть {sorted(PRICING_CURRENCIES)} "
                f"(прайс провайдера + rate_usd_rub), получено: {doc['currency']!r}",
                "currency",
            )
        )

    # P4: rate_usd_rub — число > 0 (курс; калибруется оператором).
    if "rate_usd_rub" in doc:
        rate = doc["rate_usd_rub"]
        if not _is_num(rate) or rate <= 0:
            findings.append(
                _err(
                    "P4",
                    f"rate_usd_rub должен быть числом > 0, получено: {rate!r}",
                    "rate_usd_rub",
                )
            )

    known_shelves: set[str] = {
        spec["shelf"]
        for spec in model_classes.values()
        if isinstance(spec, Mapping) and isinstance(spec.get("shelf"), str)
    }

    shelves = doc.get("shelves")
    priced: set[str] = set()
    if "shelves" in doc:
        if not isinstance(shelves, Mapping) or not shelves:
            findings.append(
                _err("P5", "shelves должен быть непустым отображением полка -> прайс", "shelves")
            )
        else:
            for shelf, spec in shelves.items():
                path = f"shelves.{shelf}"
                # P6: ref-целостность — полка известна model_classes.
                if shelf not in known_shelves:
                    findings.append(
                        _err(
                            "P6",
                            f"прайс неизвестной полки: {shelf!r}; известны (model_classes"
                            f".shelf): {sorted(known_shelves)}",
                            path,
                        )
                    )
                if not isinstance(spec, Mapping):
                    findings.append(_err("P5", f"прайс полки {shelf!r} должен быть отображением", path))
                    continue
                priced.add(shelf)
                for field in SHELF_FIELDS:
                    if field not in spec:
                        findings.append(
                            _err("P1", f"обязательное поле отсутствует: {field}", f"{path}.{field}")
                        )
                # P8: model/source — непустые строки; цены — числа >= 0.
                for field in ("model", "source"):
                    if field in spec and not _nonempty_str(spec[field]):
                        findings.append(
                            _err(
                                "P8",
                                f"{field} должен быть непустой строкой, получено: {spec[field]!r}",
                                f"{path}.{field}",
                            )
                        )
                for field in ("input_per_1m", "output_per_1m"):
                    price = spec.get(field)
                    if field in spec and (not _is_num(price) or price < 0):
                        findings.append(
                            _err(
                                "P8",
                                f"{field} должен быть числом >= 0 (USD за 1M токенов), "
                                f"получено: {price!r}",
                                f"{path}.{field}",
                            )
                        )

    # P7: каждый бюджетный контур (quotas.budgets) обязан иметь прайс.
    if isinstance(budgets, Mapping):
        for name in budgets:
            if name not in priced:
                findings.append(
                    _err(
                        "P7",
                        f"бюджетный контур {name!r} (quotas.budgets) без прайса — "
                        f"списание денег невозможно (fail-closed); прайсы: {sorted(priced)}",
                        f"shelves.{name}",
                    )
                )
        # P10: валюта бюджета — RUB (счётчики/лимит в микро-₽; USD-бюджет
        # потребовал бы отдельной конверсии — не поддержан, fail-loud).
        for name, spec in budgets.items():
            if (
                name in priced
                and isinstance(spec, Mapping)
                and spec.get("currency") != BUDGET_CURRENCY
            ):
                findings.append(
                    _err(
                        "P10",
                        f"бюджет {name!r}: currency={spec.get('currency')!r} не поддержан "
                        f"прайс-контуром — списание идёт в микро-{BUDGET_CURRENCY}",
                        f"shelves.{name}",
                    )
                )
    return findings


def _to_per_1m_micro(price_usd: Any, rate: float) -> int:
    """USD за 1M токенов -> микро-₽ за 1M токенов (int; конверсия при загрузке)."""
    return round(float(price_usd) * rate * MICRO_PER_UNIT)


class PricingRegistry:
    """Fail-closed фасад над ``pricing.yaml``: валидация + hot-reload + доступ.

    Паттерн ``QuotaRegistry``: каждая (пере)загрузка валидирует документ;
    identity-кэш ловит внешнюю перезагрузку ``Registry`` без смены mtime;
    испорченный hot-reload -> ``RegistryError`` (без тихих старых значений).

    R3-семантика квот распространяется и на прайс: смена цены/курса НЕ
    пересчитывает уже списанное — новая цена применяется к последующим
    списаниям (журнал хранит фактические micro на момент списания).
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._doc: Mapping[str, Any] | None = None
        self._prices: dict[str, ShelfPrice] = {}

    def _ensure(self) -> None:
        changed = self._registry.reload_if_changed()
        doc = self._registry.get("pricing")
        if changed or doc is not self._doc:
            self._reparse(doc)

    def _reparse(self, doc: Mapping[str, Any]) -> None:
        budgets = self._registry.get("quotas").get("budgets")
        if not isinstance(budgets, Mapping):
            budgets = {}
        findings = validate_pricing(
            doc, self._registry.get("model_classes"), budgets
        )
        if findings:
            details = "; ".join(
                f"[{f.code}] {f.path}: {f.message}" for f in findings
            )
            raise RegistryError(
                f"прайс невалиден ({self._registry.path_for('pricing')}): {details}"
            )
        rate = float(doc["rate_usd_rub"])
        self._prices = {
            shelf: ShelfPrice(
                shelf=shelf,
                model=spec["model"],
                input_per_1m_usd=float(spec["input_per_1m"]),
                output_per_1m_usd=float(spec["output_per_1m"]),
                input_per_1m_micro=_to_per_1m_micro(spec["input_per_1m"], rate),
                output_per_1m_micro=_to_per_1m_micro(spec["output_per_1m"], rate),
                source=spec["source"],
            )
            for shelf, spec in doc["shelves"].items()
        }
        self._doc = doc

    def price_for(self, shelf: str) -> ShelfPrice:
        """Прайс полки; неизвестная полка -> ``RegistryError`` (fail-closed:
        молчаливый пропуск списания = fail-open по деньгам, недопустимо)."""
        self._ensure()
        price = self._prices.get(shelf)
        if price is None:
            raise RegistryError(
                f"прайс полки {shelf!r} не найден; известны: {sorted(self._prices)} "
                "(P0-1: без прайса бюджет-списание невозможно)"
            )
        return price

    @property
    def shelves(self) -> Mapping[str, ShelfPrice]:
        """Read-only отображение полка -> ShelfPrice."""
        self._ensure()
        return MappingProxyType(self._prices)
