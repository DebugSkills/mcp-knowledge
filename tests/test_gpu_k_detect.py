#!/usr/bin/env python3
"""Unit-тесты gpu_k_detect.py (Ф-B, arch-2026-10-10-ai-ws-p2-1 R5).

Контракты зафиксированы ЖИВЫМИ пробами на dev-машине 2026-10-10 (правило 10 —
реальный контракт, не выдуманный):
  nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits
      -> "6864" (int MiB; несколько GPU = несколько строк)
  nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits
      -> "782\n316" (MiB по процессам; пустой вывод = GPU без чужих процессов)
  ollama GET /api/tags -> {"models":[{"name":"qwen2.5:7b","size":4683087332},...]}
      (size - байты; сумма по факту dev = 11321896681 B = 10807 MiB ceil)

Fail-safe-инвариант (I13): любая неизвестность (нет nvidia-smi/мусор/пусто/
ошибка/rc!=0/источники weights недоступны) -> k=1.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gpu_k_detect.py"
PY = sys.executable

# Живой ответ dev-ollama :11435 (2026-10-10) - реальный контракт /api/tags
LIVE_TAGS = {
    "models": [
        {"name": "qwen2.5:7b", "size": 4683087332},
        {"name": "qwen2.5vl:7b", "size": 5969245856},
        {"name": "mxbai-embed-large:latest", "size": 669615493},
    ]
}
LIVE_TAGS_MB = math.ceil(sum(m["size"] for m in LIVE_TAGS["models"]) / 1048576)  # 10807


def _fake_nvidia_smi(tmp_path: Path, free_out: str, apps_out: str,
                     rc_free: int = 0, rc_apps: int = 0) -> Path:
    """Каталог с fake nvidia-smi (PATH-подмена); ответы по флагу запроса."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    f = d / "nvidia-smi"
    f.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  *--query-gpu=memory.free*) printf '%s' {shlex.quote(free_out)}; exit {rc_free} ;;\n"
        f"  *--query-compute-apps*) printf '%s' {shlex.quote(apps_out)}; exit {rc_apps} ;;\n"
        "  *) echo 'unsupported query' >&2; exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    f.chmod(0o755)
    return d


def _run(tmp_path: Path, *args: str, path_dir: Path | None = None,
         path_prepend: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path_dir is not None:
        env["PATH"] = f"{path_dir}{os.pathsep}{env.get('PATH', '')}"
    elif not path_prepend:
        env["PATH"] = "/nonexistent-gpu-k-test"
    return subprocess.run(
        [PY, str(SCRIPT), *args], capture_output=True, text=True,
        env=env, timeout=60, cwd=str(tmp_path),
    )


@pytest.fixture
def tags_server():
    """Локальный HTTP-сервер с ЖИВОЙ формой /api/tags (реальный контракт)."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(LIVE_TAGS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # тишина в pytest-выводе
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


# -- нормальные сценарии ------------------------------------------------------

def test_normal_k_computed_and_basis(tmp_path):
    """Нормал: free 32000/16000 (2 GPU -> min), resident 1098, weights 7000 -> k=6."""
    d = _fake_nvidia_smi(tmp_path, "32000\n16000\n", "782\n316\n")
    r = _run(tmp_path, "--weights-mb", "7000", path_dir=d)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    # K = max(1, floor((16000 - 7000 - 1098 - 1024) / 1024)) = floor(6878/1024) = 6
    assert out["k"] == 6
    assert out["basis"] == {
        "free_vram_mb": 16000, "weights_mb": 7000, "resident_mb": 1098,
        "safety_mb": 1024, "kv_slot_mb": 1024,
    }


def test_apps_empty_resident_zero(tmp_path):
    """Пустой compute-apps = GPU без чужих процессов -> resident 0 (валидно, не fail)."""
    d = _fake_nvidia_smi(tmp_path, "32000", "")
    r = _run(tmp_path, "--weights-mb", "7000", path_dir=d)
    out = json.loads(r.stdout)
    assert out["k"] == 23  # floor((32000-7000-0-1024)/1024)
    assert out["basis"]["resident_mb"] == 0


def test_weights_from_api_real_contract(tmp_path, tags_server):
    """weights из /api/tags - живая форма ответа (size, байты), сумма -> MiB ceil."""
    d = _fake_nvidia_smi(tmp_path, "32000", "1098")
    r = _run(tmp_path, "--ollama-url", tags_server, path_dir=d)
    out = json.loads(r.stdout)
    assert out["basis"]["weights_mb"] == LIVE_TAGS_MB  # 10807
    assert out["k"] == math.floor((32000 - LIVE_TAGS_MB - 1098 - 1024) / 1024)  # 18


def test_weights_from_blobs_dir(tmp_path):
    """weights из blobs-каталога (air-gap: ollama лежит, API недоступен).

    Изоляция от живого dev-ollama: --ollama-url в закрытый порт (иначе API-канал
    приоритетнее каталога и перехватит источник). Файлы малые: 1 MiB + 1 B ->
    ceil = 2 MiB.
    """
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "sha256-aaa").write_bytes(b"\0" * 1048576)
    (blobs / "sha256-bbb").write_bytes(b"\0")
    d = _fake_nvidia_smi(tmp_path, "32000", "")
    r = _run(tmp_path, "--ollama-url", "http://127.0.0.1:1",
             "--models-dir", str(tmp_path), path_dir=d)
    out = json.loads(r.stdout)
    assert out["basis"]["weights_mb"] == 2
    assert out["k"] == 30  # floor((32000 - 2 - 0 - 1024) / 1024)


def test_weights_priority_arg_over_api(tmp_path, tags_server):
    """--weights-mb сильнее /api/tags (аргумент = операторский override)."""
    d = _fake_nvidia_smi(tmp_path, "32000", "")
    r = _run(tmp_path, "--weights-mb", "1000", "--ollama-url", tags_server, path_dir=d)
    out = json.loads(r.stdout)
    assert out["basis"]["weights_mb"] == 1000


def test_negative_budget_clips_to_k1(tmp_path):
    """Бюджет < 0 (free меньше weights+safety) -> k=1 (max(1, floor))."""
    d = _fake_nvidia_smi(tmp_path, "1000", "")
    r = _run(tmp_path, "--weights-mb", "7000", path_dir=d)
    assert json.loads(r.stdout)["k"] == 1


def test_kv_safety_override(tmp_path):
    """CLI-override констант калибровки (Ф-C: оператор подстраивает)."""
    d = _fake_nvidia_smi(tmp_path, "32000", "")
    r = _run(tmp_path, "--weights-mb", "7000", "--safety-mb", "512",
             "--kv-slot-mb", "2048", path_dir=d)
    out = json.loads(r.stdout)
    assert out["k"] == math.floor((32000 - 7000 - 0 - 512) / 2048)  # 11
    assert out["basis"]["safety_mb"] == 512 and out["basis"]["kv_slot_mb"] == 2048


def test_k_only_prints_bare_int(tmp_path):
    """--k-only -> голый int в stdout (канал Makefile/ansible)."""
    d = _fake_nvidia_smi(tmp_path, "32000", "")
    r = _run(tmp_path, "--k-only", "--weights-mb", "7000", path_dir=d)
    assert r.returncode == 0
    assert r.stdout.strip() == "23"
    int(r.stdout.strip())  # парсится как int


# -- fail-safe -> k=1 (инвариант I13) ----------------------------------------

@pytest.mark.parametrize("free_out,rc", [
    ("abc", 0),          # мусор
    ("", 0),             # пусто
    ("6864", 1),         # rc != 0
    ("12.5", 0),         # float-мусор (ожидаем int MiB)
    ("[N/A]", 0),        # недоступно
], ids=["garbage", "empty", "rc1", "float", "na"])
def test_free_broken_k1(tmp_path, free_out, rc):
    d = _fake_nvidia_smi(tmp_path, free_out, "", rc_free=rc)
    r = _run(tmp_path, "--weights-mb", "7000", path_dir=d)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["k"] == 1
    assert out["basis"]["free_vram_mb"] is None  # неизвестность честно видна


def test_no_nvidia_smi_in_path_k1(tmp_path):
    """nvidia-smi отсутствует в PATH -> k=1."""
    r = _run(tmp_path, "--weights-mb", "7000", path_prepend=False)
    assert r.returncode == 0
    assert json.loads(r.stdout)["k"] == 1


def test_apps_garbage_k1(tmp_path):
    """Мусор в compute-apps ([N/A]) -> resident неизвестен -> k=1 (не завышаем)."""
    d = _fake_nvidia_smi(tmp_path, "32000", "[N/A]")
    r = _run(tmp_path, "--weights-mb", "7000", path_dir=d)
    assert json.loads(r.stdout)["k"] == 1


def test_apps_rc_nonzero_k1(tmp_path):
    d = _fake_nvidia_smi(tmp_path, "32000", "782", rc_apps=1)
    r = _run(tmp_path, "--weights-mb", "7000", path_dir=d)
    assert json.loads(r.stdout)["k"] == 1


def test_weights_all_sources_fail_k1(tmp_path):
    """Ни API, ни каталога, ни аргумента -> weights неизвестны -> k=1."""
    d = _fake_nvidia_smi(tmp_path, "32000", "")
    r = _run(tmp_path, "--ollama-url", "http://127.0.0.1:1",
             "--models-dir", str(tmp_path / "no-such-dir"), path_dir=d)
    out = json.loads(r.stdout)
    assert out["k"] == 1
    assert out["basis"]["weights_mb"] is None


# -- формула напрямую (import) ------------------------------------------------

def test_formula_import():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import gpu_k_detect as g  # noqa: PLC0415
        assert g.compute_k(32000, 7000, 512, 1024, 1024) == 22
        assert g.compute_k(1000, 7000, 0, 1024, 1024) == 1    # клиппинг
        assert g.compute_k(32000, 7000, 0, 1024, 1024) == 23
        # ровно на границе слота: (23936)/1024 = 23.375 -> 23
        assert g.compute_k(23936 + 7000 + 1024, 7000, 0, 1024, 1024) == 23
        assert g.SAFETY_MB == 1024 and g.KV_SLOT_MB == 1024
    finally:
        sys.path.pop(0)
