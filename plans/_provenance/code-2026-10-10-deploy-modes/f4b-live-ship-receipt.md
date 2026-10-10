# F4(b) — живой прогон транспорта lup→jump→aikb (G1 закрыт) · receipt
trace_id: code-2026-10-10-deploy-modes · дата: 2026-10-10

## Канон (диспетчер)
make airgap MODE=code-net STEP=ship HOST=ch@46.17.105.195 RSYNC_PATH="ssh aikb rsync" DEST=/home/ch/update-bundle

## Найденный дефект (G1) и фикс — commit e92fffe
- Симптом: `bash: line 1: 46.17.105.195: command not found` + `rsync code 12`.
- Корень: `airgap-bundle-ship.sh` cmd_host задавал `-e "ssh -o BatchMode=yes $JUMP"`, а rsync САМ
  добавляет host из цели (`$JUMP:$DEST`) → на jump запускалась команда-хост.
- Фикс: `-e "ssh -o BatchMode=yes"` (хост задаёт спецификация цели).

## Результат (носитель)
- 1-й пакет (Ф1, target=a0f10b6, 612 654 627 B): передан полностью; sha256 на aikb =
  `b74ae62847b947b3ec95ba833240e4a2160f17fc865dcda047315364ff253261` = локальный. rc=0.
- Пакет для Ф5 (текущий HEAD, target=61bcce6, 612 664 811 B): передан; sha256 на aikb =
  `9ce1837816ecd3977b655a974d6cb0c228d5237b72b22c3f3ed4cb9ab17e36b7` = локальный. rc=0.
- Расположение на aikb: `/home/ch/update-bundle/mcp-kb-update-20261010T101618Z.tar.gz`.
- Канал: ~0.6–1.0 МБ/с (resumable). Длинные фоновые передачи прерывались между ходами →
  докачка `rsync --append-verify` (успешно).

**G1 закрыт** (nested-jump rsync-транспорт + sha256-сверка на aikb).
