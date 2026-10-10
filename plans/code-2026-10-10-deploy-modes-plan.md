---
title: "Единый механизм деплоя на удалённый air-gap хост (3 режима) · единый Makefile-UX обе стороны"
type: phase-plan
trace_id: code-2026-10-10-deploy-modes
status: active
tier: 🟡
owner: orchestrator
decider: operator
reviewer: critic
created: 2026-10-10
updated: 2026-10-10
revision: "REV.1 (approved by User Gate 2026-10-10: вариант B + вход `airgap` + ОВ1–ОВ5 по рекомендациям); critic plan_only iter2 PASS 0.88 (0.79→0.88)"
phases_total: 5
phase_current: 2
progress: "1/5 (20%)"
source_analysis: "plans/_provenance/code-2026-10-10-deploy-modes/ (портфель v1.1/REV.2 + критика iter1 REVISE 0.79 / iter2 PASS 0.88)"
tags: [phase-plan, code, airgap, deploy, makefile, usb, network]
---

# ПЛАН: единый механизм деплоя на удалённый air-gap узел `aikb` (3 явных режима)

## 🎯 ЦЕЛЬ
Свести существующий air-gap деплой-механизм (трасса 038) к **ТРЁМ явным, различимым режимам** с **единым Makefile-UX на обеих сторонах** (источник `lup` + узел `aikb`, «на узле разворачивается тоже через Makefile»). Вариант **B «единый диспетчер»**. Канон: `.knowledge/docs/methodology/phase-plan-protocol.md`.

Режимы: **(1) `full-usb`** — код+модели через флешку · **(2) `full-net`** — код+модели по сети (resumable) · **(3) `code-net`** — только код по сети (быстро; фиксы/фичи без моделей).

**Контурный инвариант (неприкосновенен):** между контурами — ТОЛЬКО код и модели; корпус знаний и индексы Qdrant — НИКОГДА.

## 🗳️ Решения оператора (decision-record, User Gate 2026-10-10)
| # | Решение | Выбор |
|---|---------|-------|
| H0 | Вариант архитектуры | **B — единый диспетчер** (`make airgap MODE=…`); ядро 038 не переписываем |
| ОВ1 | Имя входа диспетчера | **`make airgap MODE=full-usb\|full-net\|code-net`** (`deploy`/`bundle` заняты) |
| ОВ2 | Режим 3 | **отдельный режим** `code-net` (движок `airgap-pack-subset.sh`) |
| ОВ3 | Ранбук | матрица в `make help`/`make airgap`; детали — раздел «3 режима» в `docs/operations/airgap-first-install.md`; `make airgap-runbook` печатает |
| ОВ4 | Легаси 003 | **удалить** pack-часть (target `bundle` → offline-deploy.sh prepare); **сохранить** `verify` (E2E S1–S19) |
| ОВ5 | Guard `code-net` | **fail-closed**: модели изменились → отказ с подсказкой `full-net` |
| ОВ-rsync | rsync | доступен на aikb (3.2.7), но **не обязателен**; pipe-fallback `--pipe-via` сохраняется; канон nested-jump `--rsync-path "ssh aikb rsync"` |

## 📋 Предусловия
- Worktree `feat/deploy-modes` (изолированно от main; в main НЕ коммитить).
- Узел `aikb`: air-gap, доступ только вложенным jump (`ssh jump 'ssh aikb …'`); rsync 3.2.7 присутствует; DATA_ROOT=/opt/mcp-knowledge/data; модели bind-mount.
- Protected trade-offs: контурный инвариант · ядро 038 не переписываем · Operator Gate на живых прогонах (Ф4/Ф5) и удалении имён (Ф3) · resumable/докачка сохраняется.

## 🗺️ Карта фаз
| # | Фаза | Артефакт | Критерий ✅ | Бюджет |
|---|------|----------|------------|--------|
| Ф1 | Режимная матрица + диспетчер на источнике | `scripts/deploy-modes.sh` + цель `airgap` | ✅ `make airgap MODE=code-net STEP=pack` создаёт пакет; `MODE=invalid` → rc≠0; без MODE — матрица | 1с |
| Ф2 | Узловая сторона (развёртывание через Makefile) | Makefile-секция узла | ✅ `airgap-first` CHECK=1 печатает план; `make help` показывает обе стороны | 1с |
| Ф3 | Гигиена имён | чистый help | ✅ нет режимно-дублирующих имён; grep не находит ссылок на удалённые | 1с |
| Ф4 | rsync-решение + ранбук · **Operator Gate (прогон)** | doc + Makefile-ранбук | ✅ прогон `full-net STEP=ship` (nested-jump) lup→aikb (закрывает G1); `airgap-runbook` печатает матрицу | 1с |
| Ф5 | Репетиция режимов · **Operator Gate (живой узел)** | `f5-report` | ✅ apply идемпотентен; compose/Caddyfile из свежего клона; verify-deploy 7/7 | 1с |

**Бюджет:** ~5–7 сессий. Твёрдые пороги: Б1≤1 · Б2≤2 · Б3≤8 · Б5≤25 строк; Б4 — калибруемый ориентир; Б6 доска ≤300 на выходе фазы.

## 📊 ПРОГРЕСС
| Этап | Фазы | Готово | Прогресс % |
|------|------|--------|-----------|
| Механизм деплоя (3 режима) | Ф1–Ф5 | 1 / 5 | 20% |

## Фаза 1 — Режимная матрица + диспетчер на источнике
- 📥 портфель §2-B/§5-Ф1; Makefile:57-98 (deploy-семейство) + :154-217 (airgap-семейство).
- 🔧 `scripts/deploy-modes.sh` (валидатор MODE + маппинг «режим → pack-движок/ship-метод») + цель `airgap` `[STEP=pack|ship|all]`; режимная матрица «режим × шаг × сторона»; help-группировка (legacy → internal); для `code-net` передавать `--files 'mcp-kb-update-*.tar.gz'`; guard ОВ5 (модели изменились → отказ); предупреждение об общем outdir (subset/полный — одно имя tar).
- ✅ `make airgap MODE=code-net STEP=pack` создаёт пакет (факт: файлы `mcp-kb-update-*`); `MODE=invalid` → rc≠0 с понятным текстом; `make airgap` без MODE печатает матрицу; **MODE-less `make deploy` не изменён** (dry-run эквивалентен прежнему стеку).
- 📤 Makefile + `scripts/deploy-modes.sh`; смоук-лог. `[Б1=1 · Б2=2 · Б3=6]`

## Фаза 2 — Узловая сторона (развёртывание через Makefile)
- 📥 Ф1; ранбук Шаг 5; `ansible/Makefile:59-95` (update-airgap).
- 🔧 `airgap-first` (обёртка `deploy.yml` с air-gap флагами) · `airgap-apply` (делегирует `update-airgap`, `VERIFY=1` по умолчанию) · verify = существующий `verify-deploy` (:404; `airgap-verify` НЕ трогаем — занят E2E S1-S19). **O24:** узловые цели обязаны работать из СТАРОГО клона узла (тонкий шим). **Residual:** зеркальный lup-guard «не запускать узловые цели на lup»; deprecated-метка на `airgap-update`.
- ✅ `make airgap-first CHECK=1`/dry-run печатает план; `make help` показывает ОБЕ стороны; узловая цель из старого клона не падает.
- 📤 Makefile-секция узла. `[Б1=1 · Б2=2 · Б3=5]`

## Фаза 3 — Гигиена имён
- 📥 Ф1-2.
- 🔧 legacy-имена → internal-группа help, затем удаление отдельным проверяемым шагом; снести deprecated `prod-verify`; **секвенция первого апдейта (RT-4):** удаление `airgap-update` — только после подтверждённого перехода узла на новые цели (первый апдейт существующего узла идёт старым именем).
- ✅ help без режимно-дублирующих имён; grep: тесты/скрипты не ссылаются на удалённые имена.
- 📤 чистый help. `[Б1=1 · Б2=2 · Б3=4]`

## Фаза 4 — rsync-решение + ранбук · **Operator Gate (прогон на канале)**
- 📥 G1-G2; `scripts/airgap-bundle-ship.sh`.
- 🔧 канон в доке/help: `--host` (rsync) — основной для net, но с `--rsync-path "ssh aikb rsync"` (ДЕФОЛТ `-e "ssh $JUMP"` + `--rsync-path rsync` пишет на JUMP-хост — НЕ использовать, ship:191,197-198); `--pipe-via` — гарантированный fallback (rc=3 = STOP без авто-pipe); детали — раздел «3 режима» в `docs/operations/airgap-first-install.md`; `make airgap-runbook` печатает матрицу.
- ✅ **обязательный прогон** `make airgap MODE=full-net STEP=ship` (канонический `--rsync-path`) из lup → прогресс + sha256-сверка НА aikb (закрывает G1, ship:204-209); `make airgap-runbook` печатает матрицу.
- 📤 doc + Makefile-ранбук + запись решения. `[Б1=1 · Б2=2 · Б3=5]`

## Фаза 5 — Репетиция режимов · **Operator Gate (живой узел)**
- 📥 Ф1-4.
- 🔧 end-to-end `code-net` на узле (по гейту оператора); смоук G3 (subset → `airgap-apply`).
- ✅ apply идемпотентен (повтор = skip); **G4-выход:** после code-net apply compose/Caddyfile узла — ИЗ СВЕЖЕГО КЛОНА (`git fetch`+ff-only), `verify-deploy` 7/7; лог в provenance.
- 📤 `f5-report`. `[Б1=1 · Б2=2 · Б3=5]`

## 🚫 НЕ покрывает
- Изменения ansible-плейбуков (кроме обёрток `airgap-first`/`airgap-apply`).
- Производительность канала (данность ≈0.62 MiB/s; докачка — обязательна).
- Перенос корпуса знаний/индексов Qdrant (запрещён контурным инвариантом).
- **Реализацию** — план фиксируется; реализация ПОЗЖЕ (в этой сессии НЕ начинается).

## 🔗 Ссылки
- Портфель: `plans/_provenance/code-2026-10-10-deploy-modes/analiz-deploy-modes-portfolio.md` (v1.1/REV.2)
- Критика: `…/critique-deploy-modes-portfolio.md` (REVISE 0.79) + `…-iter2.md` (PASS 0.88)
- Ранбук: `docs/operations/airgap-first-install.md` · Бэклог: `.tmp/backlog/2026-10-02-airgap-ops-followups.md`
- Архив 038: `plans/_archive/038/` · Доска: `.board.md` / `.boardData.md`

---
**REV.1** | 2026-10-10 | Создан из портфеля v1.1/REV.2 (Analyst), критики iter2 PASS 0.88 (Critic plan_only) и решений User Gate (вариант B, вход `airgap`, ОВ1–ОВ5). 5 фаз. | trace_id: code-2026-10-10-deploy-modes
