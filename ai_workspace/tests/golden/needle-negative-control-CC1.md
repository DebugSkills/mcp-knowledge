# Negative-control CC1 — приёмка различимости needle-метрики (В2-B 2f)

> **СТАТУС: ЗАГОТОВКА.** Живой прогон — **Operator Gate**, вне сессии
> реализации (GPU/время; см. probe-run `--confirm-live`). Файл фиксирует
> сценарий и **критерий приёмки В2** до прогона, чтобы нельзя было
> подогнать критерий под результат.

## Что такое CC1

Известная инверсия контура (см. `plans/_provenance/arch-2026-10-08-f7-calibration/analiz-Ф7-robust-remaining.md` §2, probe-suite-spec §3.2):
на 7B-модели **compressed-контекст проигрывает full-контексту** на длинных
входах — слабая модель теряет факты в средней трети входа, а структурный
скор этого не видит (секции непустые, маркеры на месте → score высокий).

## Сценарий контроля (воспроизведение инверсии)

Два живых probe-run на ОДНОЙ модели (`qwen2.5:7b`), отличающихся только
рычагом shaping (context-ось H1), оба с needle-набором:

```bash
# плечо A — полный контекст (ожидаемо ХОРОШАЯ конфигурация)
.venv/bin/python -m ai_workspace.tools.probe_run \
  --mode modes/statya.yaml --class fast --live --confirm-live \
  --heldout ai_workspace/tests/golden/heldout-set.yaml \
  --needle ai_workspace/tests/golden/needle-set.yaml \
  --profiles-dir /tmp/kilo/cc1-full

# плечо B — compressed-контекст (ожидаемо ПЛОХАЯ конфигурация)
#   вариант режима со shaping: compressed (mode-variant, variants.py §7.2)
.venv/bin/python -m ai_workspace.tools.probe_run \
  --mode modes/statya.<compressed-variant>.yaml --class fast --live --confirm-live \
  --heldout ai_workspace/tests/golden/heldout-set.yaml \
  --needle ai_workspace/tests/golden/needle-set.yaml \
  --profiles-dir /tmp/kilo/cc1-compressed
```

Сравниваются `needle_rate` двух `ProbeReport` (поля `evidence.metrics`
draft-профилей; N≥3 прогонов на задание — медианы/доли осмысленны).

## Критерий приёмки В2 (фиксируется ДО прогона)

Метрика **различима**, если выполняется:

1. `needle_rate(full) >= NEEDLE_RATE_FLOOR` (пол — `calibration/policy.py`,
   draft 0.6; оператор вправе утвердить своё значение ДО прогона);
2. `needle_rate(compressed) < needle_rate(full)` — падение у плохой
   конфигурации **сравнимо или больше** шума: при N прогонов на задание
   разница ≥ 1 needle-проверки на каждые 3 задания (т.е. вне «одного
   кванта» 1/(tasks×runs) объяснить случайностью нельзя — устойчивая
   направленность, не единичный выброс).

**НЕ ловит** (разница в пределах одного кванта / в обе стороны / у bad
конфигурации needle_rate не ниже) ⇒ **различимость НЕ доказана**:
needle-набор/порог дорабатываются, **В3c (живая пара promotion) НЕ
запускать** — promotion-гейт не имеет права опираться на метрику, не
различающую заведомо плохую конфигурацию (F-3ii).

## Куда записать результат

`plans/_provenance/arch-2026-10-08-f7-calibration/` — краткий отчёт:
run_id обоих плеч, needle_rate, вердикт «различима/нет», решение оператора
по порогу NEEDLE_RATE_FLOOR (утвердить/править). Итог обязателен до Э-фазы
promotion-экспериментов (порядок 2a→2e→**2f**→В3c по анализу В2).
