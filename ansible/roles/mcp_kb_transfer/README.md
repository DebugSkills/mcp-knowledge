# mcp_kb_transfer

Перенос docker-стека mcp-knowledge (mcp-server, kb-console, qdrant, ollama) со старого хоста (`mcp_kb_old`) на новый (`mcp_kb_new`) без интернета на new.
Четыре последовательных этапа: `pack` (docker save на old) → `bulk` (rsync данных + docker load на new) → `stop` (заморозка old) → `cutover` (финальный rsync + запуск new).

## Ключевые файлы

- `tasks/`: `main.yml`, `pack.yml`, `bulk.yml`, `stop.yml`, `cutover.yml`
- `defaults/main.yml` — переменные роли (stage, roots, images, excludes)
- `meta/main.yml`

Операционные шаги и переменные — в приватном runbook (вне публичного репозитория).

---
**v1.1** | 2026-09-19 | Превращён в указатель на приватный runbook (вне публичного репозитория)
