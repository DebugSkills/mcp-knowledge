# modes/ — режимы AI-верстака (drop-in)

Процедура добавления режима (спека §2, код движка НЕ правится):

1. Положить `modes/<mode>.yaml` по схеме §1 (пример-фикстура:
   `tests/fixtures/modes/valid_statya.yaml`).
2. `make modes-validate` — схем-валидация (контур (а), Ф3.5a-2): обязательные
   поля, enum kind/contract, совместимость shape x contract из
   `registry/shapes.yaml`, уникальность id узлов, концы edges существуют.
3. Golden run — прогон режима на эталонной задаче (`tests/test_mode_statya.py`, Ф3.6+).

Первый реальный режим — **`statya.yaml`** (Ф3.6): analyst → **structure** (human-gate:
`on_approve: critic`, `on_edit: analyst`) → critic (`on_revise: analyst`, max 3) → editor →
citer (`mcp.citation_attach`, strict) → **publish** (human-gate: `on_approve: null` = финал,
`on_edit: editor`). Гейт, отвеченный `approve`, становится pass-through (не спрашиваем
повторно после REVISE-петли); ответ `edit` показывает гейт снова после переработки, а
текст правки попадает во вход адресата (`on_edit`/`on_approve`) и меняет `effect_id`.

**L11 (Ф3.9)** — промпты model-agnostic: `prompt_overrides` обязан быть пустым, а строки
режима не должны упоминать модели (qwen/deepseek/gpt/glm/…). Это делает parity T/I
достоверным: промпт одинаков на local и ext, слабая модель не тянет промпт вниз
(взамен фантомного `prompt_overrides`, P1-3 паттерна Local-First).
