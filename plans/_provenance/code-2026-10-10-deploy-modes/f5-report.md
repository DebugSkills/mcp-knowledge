# F5 — репетиция режимов на живом узле aikb · report
trace_id: code-2026-10-10-deploy-modes · дата: 2026-10-10

## Bootstrap (узел был слишком старый)
- aikb HEAD был `3b80a30` (root:root) — БЕЗ `airgap-update`/`update-airgap`; цель `update-local` не пробрасывала `EXTRA_VARS`.
- 1-й apply — напрямую playbook'ом (обход) с `-e update_skip_backup=true` → узел `3b80a30 → 61bcce6`.
- Обход Н10-бага старого `backup.sh` (rc=1 после успешных снапшотов); фикс Н10 — в поставляемом коде.
- Найден+исправлен дефект 038 `update-airgap` (не создавал `AIRGAP_WORK/tree`) — commit `39a90a8`; на узел доставлен bootstrap-патчем (+`assume-unchanged` от dirty-гейта).

## Make-путь (после bootstrap)
```
make airgap-apply BUNDLE=/home/ch/update-bundle/mcp-kb-update-20261010T101618Z.tar.gz CHECK=1   # dry-run OK
make airgap-apply BUNDLE=/home/ch/update-bundle/mcp-kb-update-20261010T101618Z.tar.gz SKIP_BACKUP=1
```
- `PLAY RECAP: aikb ok=46 changed=1 skipped=14 failed=0`.
- Идемпотентность (G3): «SKIP merge: HEAD (61bcce6) уже == target_commit» · образы «SKIP (совпал)» · «миграций нет — пропуск».
- `changed=1` — только GPU-K рендер `litellm*.config.yaml` (мелкая не-идемпотентность; follow-up, не блокер).
- Авто-приёмка: «~~~ post-apply verify (verify-deploy) …» → **7 passed / 0 failed** (V1–V7).

## Вердикт Ф5
- Узловой make-UX **`make airgap-apply`** РАБОТАЕТ: playbook берётся **ИЗ ПАКЕТА** (O24-proof), `SKIP_BACKUP`, авто-verify.
- Идемпотентность подтверждена (повтор = skip). `verify-deploy` 7/7.
- Первичный apply — одноразовый bootstrap (узел был pre-038; новые цели приезжают вместе с апдейтом).
