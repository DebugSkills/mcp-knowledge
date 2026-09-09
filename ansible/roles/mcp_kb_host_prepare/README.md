# mcp_kb_host_prepare

Подготовка **нового** хоста для стека mcp-knowledge с нуля: docker + драйвер NVIDIA +
nvidia-container-toolkit + регистрация nvidia runtime (+ опциональный HTTP-proxy для dockerd).
Задача миграции `code-2026-09-09-001`, часть 2/3.

## Что делает (порядок tasks/main.yml)

| # | Файл | Назначение |
|---|------|-----------|
| 1 | `preflight.yml` | package_facts, assert Debian 12 (bookworm), пробы `nvidia-smi` / `docker --version` |
| 2 | `docker.yml` | docker-ce из офиц. репозитория (GPG → dearmor → keyrings, docker.list, apt, systemd) — только если docker отсутствует |
| 3 | `nvidia_driver.yml` | `nvidia-driver` + `firmware-misc-nonfree` из non-free (deb822 `debian.sources` replace / fallback `apt_repository`), **reboot** при установке |
| 4 | `toolkit.yml` | nvidia-container-toolkit: GPG-ключ **без dearmor** (иначе `NO_PUBKEY DDCAE044F796ECB0`), `$(ARCH)` → `dpkg --print-architecture` (не `ansible_architecture`: x86_64 ≠ amd64) |
| 5 | `nvidia_ctk.yml` | `nvidia-ctk runtime configure` — только если `nvidia` отсутствует в `/etc/docker/daemon.json` (daemon.json старого хоста НЕ копируется — там мёртвый mirror `:5000`) |
| 6 | `proxy.yml` | drop-in `docker.service.d/http-proxy.conf` + daemon-reload (только если прокси задан) |
| 7 | `verify.yml` | `flush_handlers` → assert: `nvidia` в `docker info`, `docker compose version` rc 0, `nvidia-smi` rc 0 |

Handler: `Перезапустить docker` (`systemd` + `daemon_reload`).

## Переменные (defaults/main.yml)

| Переменная | Default | Описание |
|-----------|---------|----------|
| `mcp_kb_host_prepare__install_docker` | `true` | Ставить docker |
| `mcp_kb_host_prepare__docker_packages` | docker-ce, cli, containerd.io, buildx, compose-plugin | Пакеты docker |
| `mcp_kb_host_prepare__install_nvidia_driver` | `true` | Ставить драйвер NVIDIA |
| `mcp_kb_host_prepare__nvidia_driver_package` | `nvidia-driver` | Метапакет драйвера |
| `mcp_kb_host_prepare__nvidia_reboot` | `true` | Reboot (600 c) после установки драйвера |
| `mcp_kb_host_prepare__install_nvidia_toolkit` | `true` | Ставить nvidia-container-toolkit |
| `mcp_kb_host_prepare__nvidia_gpg_url` | nvidia.github.io/.../gpgkey | GPG-ключ NVIDIA (кладётся как есть) |
| `mcp_kb_host_prepare__nvidia_repo_url` | nvidia.github.io/.../nvidia-container-toolkit.list | Репозиторий toolkit |
| `mcp_kb_host_prepare__docker_gpg_url` | download.docker.com/linux/debian/gpg | ASCII-ключ docker (→ dearmor) |
| `mcp_kb_host_prepare__docker_repo` | `deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] ... bookworm stable` | Строка репозитория docker |
| `mcp_kb_host_prepare__proxy` | `docker_proxy` из group_vars | HTTP(S)-прокси dockerd (пусто → шаг пропускается) |
| `mcp_kb_host_prepare__no_proxy` | `docker_no_proxy` из group_vars | NO_PROXY для dockerd |

## Пример использования

```yaml
- name: Подготовка нового хоста для mcp-knowledge (docker + GPU)
  hosts: mcp_kb_new
  gather_facts: true
  become: true
  roles:
    - role: mcp_kb_host_prepare
      tags: [host_prepare]
```

Запуск (человек): `make run PLAYBOOK=playbooks/host-prepare.yml`

## Теги

`host_prepare` (вся роль), `preflight`, `docker`, `nvidia_driver`, `toolkit`,
`nvidia_ctk`, `proxy`, `verify`.

⚠ Гранулярные теги требуют проб из `preflight` (проверка `...probe.rc`) — запускайте
роль целиком. `--tags host_prepare` — безопасно.

## Замечания по идемпотентности

- `docker.yml`/`nvidia_driver.yml` выполняются только при отсутствии компонента (probe rc ≠ 0).
- `gpg --dearmor` защищён `creates:`; `replace`-подстановки не меняют уже изменённый текст.
- `nvidia-ctk` вызывается только при отсутствии `nvidia` в daemon.json.
- Reboot происходит только при фактической установке драйвера (`driver_install is changed`).
- `apt update` кэшируется (`cache_valid_time: 3600`, форс при изменении источников).

## Проверено

- Debian 12 bookworm (assert в preflight; другие ОС не поддерживаются).
- Ключ NVIDIA кладётся без dearmor — apt ≥ 2.4 принимает его напрямую; dearmor-путь
  на этой инфраструктуре давал `NO_PUBKEY DDCAE044F796ECB0`.
