#!/usr/bin/env python3
"""gpu_k_detect — авто-детект K (параллельных GPU-слотов) по свободной VRAM.

arch-2026-10-10-ai-ws-p2-1, Ф-B (R5, locked оператором): прод 32 ГБ сам
подстраивает K; dev 8 ГБ -> K=1; НЕТ таблицы «модель->K». Главное правило
(fail-safe, I13): ЛЮБАЯ неизвестность -> K=1 — не завышаем, ошибка безопасна.

Формула (§7b R5):
    K = max(1, floor((free_vram_mb - weights_mb - resident_mb - safety_mb) / kv_slot_mb))

Источники (host-side; GPU-видимость нужна только хосту, контейнеры не трогаем):
    free_vram_mb  — nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits
                    (несколько GPU -> несколько строк, берём MIN — консервативно)
    weights_mb    — приоритет: --weights-mb N > ollama GET /api/tags (сумма size,
                    байты; живой dev-факт: qwen2.5:7b + qwen2.5vl:7b + mxbai
                    = 10807 MiB) > размер blobs-каталога --models-dir (air-gap:
                    ollama ещё не поднят, но блобы на диске; = тем же «сумма
                    размеров всех моделей»); все источники недоступны -> K=1
    resident_mb   — nvidia-smi --query-compute-apps=used_memory (сумма; в штатный
                    момент детекта наш стек лежит -> здесь чужие GPU-процессы;
                    пустой вывод = 0; при живом нашем ollama его потребление уже
                    вычтено из free и НЕ вычитается здесь повторно -> консервативный
                    запас в безопасную сторону — осознанно)
    safety_mb / kv_slot_mb — константы ниже (CLI-override для калибровки Ф-C)

Выход: JSON {"k": <int>=1, "basis": {free_vram_mb, weights_mb, resident_mb,
safety_mb, kv_slot_mb}} (неизвестное значение = null); --k-only -> голый int
(канал Makefile gateway-render / ansible update.yml). exit 0 всегда (fail-safe
обработан внутри; WARN-диагностика -> stderr, не в stdout).
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import urllib.request
from pathlib import Path

NVIDIA_SMI = "nvidia-smi"
OLLAMA_URL_DEFAULT = "http://127.0.0.1:11435"  # loopback-публикация контейнера ollama
MODELS_DIR_DEFAULT = "data/ollama/models"       # dev-layout; узел передаёт явный путь

MiB = 1048576

SAFETY_MB = 1024
"""Запас «не для слотов» (обоснование): CUDA-контекст ~300 MiB + фрагментация
аллокатора + host-ollama runner на SHARED GPU (прод) — пик ~622 MiB (факт
комментария docker-compose*.yml) + всплески активации моделей при up.
Консервативно 1 GiB; калибровка — --safety-mb (Ф-C, оператор)."""

KV_SLOT_MB = 1024
"""KV-кэш+compute ОДНОГО слота (обоснование): тяжелейшая модель парка —
qwen2.5:7b GQA: 28 слоёв x 4 KV-heads x 128 dim x 2 (K+V) x 2 B = 56 KiB/ток;
ctx 16384 -> ~940 MiB; mxbai (ctx 512) на порядок меньше; compute-буферы
слоёв ~50-100 MiB. Итого <= 1 GiB/слот; калибровка — --kv-slot-mb (Ф-C).
Каждый дополнительный слот множит KV — это и есть деление на kv_slot_mb."""


def _warn(msg: str) -> None:
    print(f"[gpu_k_detect] WARN: {msg}", file=sys.stderr)


def _run_smi(args: list[str]) -> str | None:
    """nvidia-smi args -> stdout; None при отсутствии/ошибке/rc!=0/таймауте."""
    try:
        cp = subprocess.run(
            [NVIDIA_SMI, *args], capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError) as e:
        _warn(f"nvidia-smi недоступен ({e!r})")
        return None
    if cp.returncode != 0:
        _warn(f"nvidia-smi {' '.join(args)} rc={cp.returncode}: {cp.stderr.strip()[:200]}")
        return None
    return cp.stdout


def query_free_mb() -> int | None:
    """MIN free по всем GPU (MiB, int); мусор/пусто -> None."""
    out = _run_smi(["--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    if out is None:
        return None
    vals: list[int] = []
    for line in out.splitlines():
        tok = line.strip()
        if not tok:
            continue
        if not tok.isdigit():
            _warn(f"memory.free мусор: {tok!r}")
            return None
        vals.append(int(tok))
    if not vals:
        _warn("memory.free пусто")
        return None
    return min(vals)


def query_resident_mb() -> int | None:
    """Сумма used_memory чужих GPU-процессов (MiB); пусто -> 0; мусор -> None."""
    out = _run_smi(["--query-compute-apps=used_memory",
                    "--format=csv,noheader,nounits"])
    if out is None:
        return None
    total = 0
    for line in out.splitlines():
        tok = line.strip()
        if not tok:
            continue
        if not tok.isdigit():
            _warn(f"compute-apps used_memory мусор: {tok!r}")
            return None
        total += int(tok)
    return total


def weights_from_api(url: str) -> int | None:
    """Сумма size всех моделей из GET /api/tags (живой контракт: bytes, int).

    = сумме блобов на диске (size в /api/tags — размер блоба модели); удобнее
    API, т.к. не зависит от DATA_ROOT-раскладки. Недоступен -> None.
    """
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — любой канал-сбой = источник недоступен
        _warn(f"/api/tags недоступен ({url}): {e!r}")
        return None
    models = data.get("models") or []
    if not isinstance(models, list):
        _warn("/api/tags: models не список")
        return None
    total = 0
    for m in models:
        size = m.get("size")
        if not isinstance(size, int) or size < 0:
            _warn(f"/api/tags: size мусор у {m.get('name')!r}")
            return None
        total += size
    return (total + MiB - 1) // MiB  # ceil: консервативно


def weights_from_dir(models_dir: str) -> int | None:
    """Сумма размеров blobs-каталога (MiB, ceil) — air-gap-источник (ollama лежит).

    Blobs = веса всех моделей (те же байты, что size в /api/tags). Каталога
    нет/чужая структура -> None (не гадаем).
    """
    blobs = Path(models_dir) / "blobs"
    if not blobs.is_dir():
        _warn(f"blobs-каталог не найден: {blobs}")
        return None
    total = 0
    try:
        for p in blobs.iterdir():
            if p.is_file():
                total += p.stat().st_size
    except OSError as e:
        _warn(f"обход {blobs}失败: {e!r}")
        return None
    return (total + MiB - 1) // MiB


def compute_k(free_mb: int | None, weights_mb: int | None,
              resident_mb: int | None, safety_mb: int, kv_slot_mb: int) -> int:
    """Формула R5 с fail-safe: любое None или неположительный бюджет -> 1."""
    if free_mb is None or weights_mb is None or resident_mb is None:
        return 1
    if kv_slot_mb <= 0:
        return 1
    budget = free_mb - weights_mb - resident_mb - safety_mb
    return max(1, math.floor(budget / kv_slot_mb))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Авто-детект K по free VRAM (Ф-B R5); fail-safe -> K=1.",
    )
    ap.add_argument("--k-only", action="store_true",
                    help="печатьать только K (int) — канал Makefile/ansible")
    ap.add_argument("--weights-mb", type=int, default=None,
                    help="явный override weights (MiB); сильнее API/каталога")
    ap.add_argument("--ollama-url", default=OLLAMA_URL_DEFAULT,
                    help=f"ollama base URL для /api/tags (default {OLLAMA_URL_DEFAULT})")
    ap.add_argument("--models-dir", default=MODELS_DIR_DEFAULT,
                    help=("каталог моделей (blobs) — weights-источник, когда API "
                          f"недоступен (default {MODELS_DIR_DEFAULT})"))
    ap.add_argument("--safety-mb", type=int, default=SAFETY_MB,
                    help=f"запас вне слотов, MiB (default {SAFETY_MB})")
    ap.add_argument("--kv-slot-mb", type=int, default=KV_SLOT_MB,
                    help=f"KV+compute одного слота, MiB (default {KV_SLOT_MB})")
    args = ap.parse_args(argv)

    free_mb = query_free_mb()
    resident_mb = query_resident_mb()

    weights_mb: int | None = None
    if args.weights_mb is not None:
        weights_mb = args.weights_mb
    else:
        weights_mb = weights_from_api(args.ollama_url)
        if weights_mb is None:
            weights_mb = weights_from_dir(args.models_dir)

    k = compute_k(free_mb, weights_mb, resident_mb, args.safety_mb, args.kv_slot_mb)
    basis = {
        "free_vram_mb": free_mb,
        "weights_mb": weights_mb,
        "resident_mb": resident_mb,
        "safety_mb": args.safety_mb,
        "kv_slot_mb": args.kv_slot_mb,
    }
    if args.k_only:
        print(k)
    else:
        print(json.dumps({"k": k, "basis": basis}, ensure_ascii=False))
    return 0  # всегда: fail-safe = k=1 внутри, не rc-ошибка


if __name__ == "__main__":
    sys.exit(main())
