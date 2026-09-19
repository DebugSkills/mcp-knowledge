# mcp_kb_host_prepare

Подготовка нового хоста для стека mcp-knowledge: docker + драйвер NVIDIA + nvidia-container-toolkit + регистрация nvidia runtime (+ опциональный HTTP-proxy для dockerd).
GPU нужен ollama-КОНТЕЙНЕРУ стека (модели живут в контейнере); ollama на хост не ставится.

## Ключевые файлы

- `tasks/`: `main.yml`, `preflight.yml`, `docker.yml`, `nvidia_driver.yml`, `toolkit.yml`, `nvidia_ctk.yml`, `proxy.yml`, `verify.yml`
- `handlers/main.yml` — перезапуск docker
- `defaults/main.yml` — переменные роли
- `templates/`: `docker.list.j2`, `http-proxy.conf.j2`
- `meta/main.yml`

Операционные шаги и переменные — в приватном runbook (вне публичного репозитория).
