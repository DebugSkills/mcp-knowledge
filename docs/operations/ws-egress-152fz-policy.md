---
title: "WS Egress Policy — зона private обрабатывается только локальной моделью (152-ФЗ)"
type: policy
status: active
approved_by: operator
approved_at: 2026-10-07
trace_id: arch-2026-10-05-ai-workspace
created: 2026-10-07
tags: [policy, egress, 152fz, ai-workspace]
---

# Egress-политика AI-верстака: private → только local (152-ФЗ)

**Трасса:** `arch-2026-10-05-ai-workspace` (Ф1/Ф3.9/Ф3.10/Ф6 TODO 5) | **Область:** ai_workspace, шлюз LiteLLM, kb-console/MCP-токены
**Назначение:** письменная фиксация зонного правила обработки данных: контент зоны
`private` не покидает узел и обрабатывается **только локальной моделью**; внешний
провайдер (DeepSeek, полка `ext`) — исключительно для `public`.

## 1. Суть политики (правило)

Данные, отнесённые к зоне `private` (материалы лабов, вложения участников, черновики),
обрабатываются только локальной моделью на полке `local` (ollama, внутренний контур).
Отправка `private` на внешний провайдер запрещена на уровне движка, а не договорённости.
Fallback с local на ext для `private` отсутствует по построению.

> **Правило:** `zone=private` ⇒ полка `local`. Нарушение = дефект (см. §3-регрессию).

## 2. Инвариант и механика (носители, file:line)

| Механизм | Носитель | Что делает |
|---|---|---|
| Инвариант **I5** | `ai_workspace/registry/model_classes.yaml:3` | «private → только local, fallback выключен» — маркер зонного правила |
| Гейт **EgressBlocked** | `ai_workspace/orchestrator/engine.py:110` (класс; экспорт `:64`; raise в `_guard_zone` `:935`) | Зонный предикат в ДВИЖКЕ до вызова LLM: `zone=private` + полка ≠ local → отказ (Ф3.10 hardening: предикат не только в тесте) |
| Полки шлюза | `litellm.config.yaml:11-20` | Два деплоймента: `local` (ollama/qwen2.5:7b) + `ext` (DeepSeek, ключ не коммитится); fallback нет (I5, `:2`) |
| Local-only режим | `litellm.local_only.config.yaml:1-7` | ext-полка отсутствует в конфиге; smoke-критерий: в `/v1/models` только local; `model=ext` → 404 даже с ключом |
| Zone-scope токенов | `mcp_server/src/mcp_server/token_store.py:17-18,55,83-84` | Уровень в теле токена: `s`=subscriber; `subscriber → zone принудительно "public"` (`:259-266`) |
| Белый список subscriber | `mcp_server/src/mcp_server/auth.py:125` (`SUBSCRIBER_TOOLS`) | Subscriber-ключ видит только публичные read-тулы контура A |
| Cost-map фикс | `compose.gateway.yml:21` | `LITELLM_LOCAL_MODEL_COST_MAP: "True"` — шлюз не тянет cost-map с `raw.githubusercontent.com` (см. §4) |

## 3. Регрессия (как политика проверяется)

- **Unit, двойной ассерт zone→egress** (Ф3.9): `ai_workspace/conformance.py:15-16` —
  «отказ при private вне local» **И** «счётчик ext-egress == 0» (отсутствие отказа =
  мутация зонного предиката).
- **Golden-run T-секция с positive control:** `ai_workspace/tools/golden_run.py:366-374` —
  для всех private-задач счётчик ext == 0; запрещённый маршрут `private×ext` → отказ;
  контрольная фаза (`public×ext`) подтверждает, что счётчик работает (не «зелёный ноль»).
- **Линт режимов:** `make modes-validate` (Makefile:541) — runtime-линт L1–L14, включая
  **L14** «critic-gate не на слабом классе: fast запрещён» (`ai_workspace/orchestrator/mode_lint.py:24`).
- **Egress-счётчик (destination-aware):** iptables-цепочка `WS_EGRESS_CNT`, «лестница
  RETURN» без негаций (nf_tables запрещает >1 `-d`), последнее правило = счётчик
  публичного egress; guard `iptables -S | grep -c '^-A' == 6`. Метод и протокол —
  `plans/_provenance/arch-2026-10-05-ai-workspace/Ф6-todo5-egress-qa.md`.

## 4. Known-остаток (осознанные границы, без замалчивания)

- **Шлюз сам egress'ил при local_only:** v2-прогон дал 56 пкт / 3709 B — LiteLLM при
  старте тянет model cost map с `raw.githubusercontent.com` (дифференциальная атрибуция:
  A=56 → B=0 при positive control C=66; дефект `F6-EGRESS-1`). **Фикс:**
  `LITELLM_LOCAL_MODEL_COST_MAP: "True"` в `compose.gateway.yml:21` — проверено, B=0.
- **Провижининг ≠ runtime:** загрузка моделей/образов (`ollama pull`, `docker pull`)
  требует сети на этапе развёртывания. Политика покрывает RUNTIME-обработку данных,
  не провижининг.
- **Вне политики:** внешние сервисы сообщества (VK, Timepad, почта) — отдельный контур,
  данная политика их не регулирует.

## 5. 152-ФЗ (граница ответственности)

**Инженерные гарантии:** private-контент не покидает узел (§2-механика); вложения
участников попадают в private; ключи внешних провайдеров — серверные, не раздаются.
**Оговорка:** этот документ — инженерная политика, НЕ юридическое заключение;
юридическая интерпретация 152-ФЗ и тексты согласий — за оператором.

Чек-лист для юр-оценки (нормы закона НЕ придумываем):
- [ ] Класс субъектов и категорий данных в private-зоне (персональные? иные?)
- [ ] Правовое основание обработки для каждого источника вложений
- [ ] Требуется ли согласие и его форма — оценка юриста
- [ ] Сроки хранения/уничтожения private-данных (retention)
- [ ] Учёт операторских процессов (ответы на запросы субъектов)

## 6. Связь с решением по subscriber-каналу

Политика действует в обоих состояниях: **статус-кво** — MCP internal-only, участники
ходят через kb-console UI, MCP-ключей у пользователей нет (REV.7,
`plans/arch-2026-10-05-ai-workspace-plan.md:309`); **при выдаче subscriber-токенов** —
зонная механика та же (token_store принуждает `subscriber → public`,
`SUBSCRIBER_TOOLS` — белый список), но требуется **повторный ingress/egress-тест**
перед первой выдачей.

**✅ Решение оператора 2026-10-07: вариант (1) — per-user subscriber-токены.**
Участники верстака (D1=5), работающие через свои MCP-клиенты (Kilo/Cline/Claude Code и др.), получают **персональные** subscriber-токены (`level=s`, зона принудительно `public`, `SUBSCRIBER_TOOLS`, rate-limit 45/мин, авто-деактивация Q9).
Следствия: (а) MCP должен быть **доступен с их узлов** (ingress); (б) **runbook выдачи/отзыва** + связь токен↔участник (минимальные персональные данные, retention); (в) **повторный ingress/egress-тест** до первой выдачи; (г) `private`-данные недоступны по построению (зона форсируется), поэтому I5 не нарушается.
> Итог для REV.7: «MCP internal-only, у пользователей MCP-ключей нет» **изменено** — участникам выдаются public-only subscriber-токены.

## 7. Связанное

| Документ | Связь |
|---|---|
| `compose.gateway.yml` | Пин образа шлюза (I12), cost-map фикс `:21` |
| `litellm.config.yaml` / `litellm.local_only.config.yaml` | Полки local/ext и local-only режим |
| `plans/_provenance/arch-2026-10-05-ai-workspace/Ф6-todo5-egress-qa.md` | Метод egress-счётчика, прогоны A/B/C, F6-EGRESS-1 |
| `docs/operations/ws-redis-limits.md` | Смежный runbook контура (образец структуры) |

---
**v1.0 (draft)** | 2026-10-07 | Создано в трассе `arch-2026-10-05-ai-workspace` (Ф6; политика egress/152-ФЗ).
