---
doc-type: runbook
topic: calib-admin-api
tags: [ai-workspace, mcp-knowledge, calibration, operations, arch-2026-10-09-calib-admin-ui]
value-rating: high
target-level: intermediate
sources:
  - plans/arch-2026-10-09-calib-admin-ui-plan.md
  - ai_workspace/calibration/admin_api.py
  - ai_workspace/deploy/calib-admin-api.service
  - docs/operations/error-sources.md
---

# 🎛 Runbook: calib-admin-api — host-сервис admin-API калибровки (Ф1)

## 1. Назначение

Тонкий host-side HTTP-слой для **UI-калибровки системы под текущую модель**
(arch-2026-10-09-calib-admin-ui): страница kb-console `/calibration` (Ф2) ходит
на этот API по loopback. API — обёртки над готовыми CLI-канонами
`probe-run` / `profile-approve` (те же гейты и writeback, ноль дублей логики):
текущая модель/дрейф (`GET /calib/model`), VRAM-preflight (`GET /calib/gpu`),
фоновый probe (`POST /calib/probe/start` + `/calib/probe/status`),
approve (`POST /calib/approve`).

**Не входит:** живой LLM-прогон из этого runbook — только Operator Gate
(`confirm_live`/`confirm_ext` в теле запроса, строже CLI).

## 2. Инварианты

| Инвариант | Что это |
|---|---|
| **bind 127.0.0.1** | сервис слушает только loopback; kb-console в `network_mode: host` ходит по localhost (прецедент `MCP_SERVER_URL`) ⇒ **UFW-правила не нужны и не заводить** |
| **workers=1** | `asyncio.Lock` single-flight НЕ межпроцессный; `--workers 1` зафиксирован в unit и `main()` — многопроцессный запуск сломал бы 409-гейт |
| **CALIB_API_KEY fail-closed** | пустой ключ → отказ старта приложения; сравнение `secrets.compare_digest`; ключ НИКОГДА не логируется и не возвращается в ответах |
| **Operator Gate** | `confirm_live` (аналог `--confirm-live`) обязателен для любого прогона; ext-полка (₽) — отдельный `confirm_ext` |

## 3. Env-файл `/etc/calib-admin-api.env`

Создаётся оператором (root:root 0600; systemd читает его сам — отсутствие
файла = отказ старта, fail-closed):

```bash
sudo install -m 600 /dev/null /etc/calib-admin-api.env
sudo tee /etc/calib-admin-api.env >/dev/null <<'ENV'
CALIB_API_KEY=<секрет — НЕ вставлять из буфера/чата>
CALIB_API_PORT=8700
# опционально: GPU-слоты ws-redis для /calib/gpu (нет — fail-soft)
# WS_REDIS_URL=redis://127.0.0.1:6379/0
ENV
sudo chmod 600 /etc/calib-admin-api.env
```

- **Ключ задавать только так** — не `echo` в общий шелл-истории, не в коммит,
  не в чат. Генерация: `openssl rand -hex 32` → сразу в файл выше.
- **Никогда не печатать** содержимое файла (`cat`/`grep CALIB` по нему — нельзя).
- Смена ключа = правка файла + `sudo systemctl restart calib-admin-api`.
- `CALIB_API_PORT` — обязательная строка (default в коде 8700; unit передаёт
  `${CALIB_API_PORT}` в uvicorn — пустое значение уронит старт).

## 4. Установка и запуск (Operator Gate)

Unit лежит в репо: `ai_workspace/deploy/calib-admin-api.service`. Установка —
вручную оператором (в ansible-деплой НЕ входит):

```bash
sudo cp ai_workspace/deploy/calib-admin-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now calib-admin-api   # старт — решение оператора
systemctl status calib-admin-api --no-pager
```

Пути в unit (`WorkingDirectory`, `ExecStart`, `User=ladmin`) соответствуют
хосту dev-стека `/kvm/mcp-knowledge/mcp-knowledge` — на другом хосте
скорректировать до копирования.

Останов/перезапуск/отключение:

```bash
sudo systemctl stop calib-admin-api        # останов (фоновый probe будет прерван)
sudo systemctl restart calib-admin-api     # после правки env-файла
sudo systemctl disable --now calib-admin-api && sudo rm /etc/systemd/system/calib-admin-api.service && sudo systemctl daemon-reload
```

## 5. Health-проверка

```bash
CALIB_API_KEY=$(sudo grep -h '^CALIB_API_KEY=' /etc/calib-admin-api.env | cut -d= -f2-)
curl --noproxy '*' -s -H "X-Calib-Key: $CALIB_API_KEY" \
  http://127.0.0.1:${CALIB_API_PORT:-8700}/calib/model | head -c 400
```

Ожидаемо: HTTP 200 + JSON (`shelf`/`active`/`drift`/`classes`).
`--noproxy '*'` обязателен (корп. прокси перехватывает localhost).

## 6. Логи и маркеры

```bash
journalctl -u calib-admin-api -f                       # поток
journalctl -u calib-admin-api --since '-1h' | grep '\[CALIB-API\]'
```

Единый префикс **`[CALIB-API]`**: `start host=… port=… workers=1` ·
`auth-refused` · `probe-start model_class=… runs=… zone=… live=… ext=…` ·
`probe-finish exit=N | error=TypeName` · `approve-start profile_id=… confirm=…` ·
`approve-finish exit=N applied=…`. Секретов в маркерах нет by construction.

## 7. Типовые ошибки

| Код | Причина | Действие |
|---|---|---|
| **401** | нет/неверный `X-Calib-Key` | сверить ключ в env-файле (не печатая: `sudo grep -c '^CALIB_API_KEY=.' /etc/calib-admin-api.env` → 1); в логе `auth-refused` |
| **400** | Operator Gate: нет `confirm_live` / `ext` без `confirm_ext` / аргумент-путь с ведущим `-` | осознанно подтвердить в UI (Ф2) или curl-телом |
| **409** | probe уже выполняется (single-flight, workers=1) | дождаться `/calib/probe/status` → `done`/`failed`; второй параллельный прогон невозможен by design |
| **422** | approve fail-closed: ceiling без needle-доказательства / неполный `calibrated_for` / гейты CLI | прогнать probe с needle до approve; детали — в stdout CLI-канона (поле `output` ответа) |
| **500** | сбой записи approve — **носители откачены** (two-носительная запись) | смотреть журнал; повторить после устранения причины |
| старт-отказ юнита | нет `/etc/calib-admin-api.env` / пустой ключ / нет `.venv` | `journalctl -u calib-admin-api -n 50`: fail-closed `CALIB_API_KEY` |

## 8. VRAM-preflight перед живым прогоном

`GET /calib/gpu` (с ключом): ollama `/api/ps` (загруженные в VRAM модели +
`size_vram`) и ws-redis GPU-слоты. Оба источника **fail-soft**:
`"available": false` ≠ 5xx — предупреждение, решение за оператором.
Перед `live`-прогоном убедиться, что полка не делит VRAM с фоновыми моделями.

## 9. Git-dirty после UI-записей (ожидаемо!)

Approve/probe-writeback пишут **tracked-носители репо**:
`ai_workspace/registry/model_classes.yaml`, `ai_workspace/calibration/profiles/*`
(отчёты `calibration/reports/` — в `.gitignore`). После UI-сессии рабочее
дерево **грязное — это нормальный исход**, не сбой:

- kb-console (Ф2) покажет бейдж «git dirty»;
- оператор проверяет `git status` / `git diff` и фиксирует изменения штатным
  конвейером (`make push`); коммитить носители калибровки вручную не нужно
  отдельным механизмом — они едут обычным код-пушем.

## 10. Наблюдаемость (E5)

Строка реестра: `service:calib-admin-api` в `docs/operations/error-sources.md`
(раздел «Host-сервисы (systemd)») — статус **gap**: stdout сервиса идёт в
journald, вне docker_logs-сбора `errors_collect.py`; диагностика —
`journalctl -u calib-admin-api` (маркеры §6). При автоматизации — добавить
journald-source в коллектор (см. причину в gap-строке).

## 11. Что дальше (план Ф2–Ф4)

Страница kb-console `/calibration` (Ф2), живая пара base↔variant (Ф3),
drift-карточка + история (Ф4) — `plans/arch-2026-10-09-calib-admin-ui-plan.md`.

---
**v1.0** | 2026-10-09 | Ф1b: unit + runbook + E5-строка + маркеры [CALIB-API] | trace: arch-2026-10-09-calib-admin-ui
