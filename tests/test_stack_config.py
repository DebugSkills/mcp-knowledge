"""Тесты Ф5: stack.settings.yaml + scripts/stack_config.py (trace: arch-2026-10-10-ws-airgap-layers).

R6: файл-слой каскада · parity-дефолтов (split по конусам) · exit-коды helper · write-инварианты.
Офлайн, без сети.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "stack_config.py"


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=env, check=False)


def load_mod():
    spec = importlib.util.spec_from_file_location("stack_config", HELPER)
    m = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(m)
    return m


# ── 1. helper: базовый контур ──

def test_show_lists_three_keys_with_sources():
    r = run("show")
    assert r.returncode == 0, r.stderr
    for k in ("ws.local_model", "ws.local_ollama_base", "gateway.max_parallel"):
        assert k in r.stdout
    assert "[file]" in r.stdout and "[default]" in r.stdout


def test_get_quiet_value():
    r = run("get", "ws.local_model", "--quiet")
    assert r.returncode == 0 and r.stdout.strip() == "qwen3:30b-a3b-instruct-2507-q4_K_M"


def test_whitelist_exit3():
    assert run("set", "foo.bar=1").returncode == 3


def test_int_validation_exit3():
    assert run("set", "gateway.max_parallel=0", "--apply").returncode == 3


def test_dry_run_leaves_file_intact():
    before = (ROOT / "stack.settings.yaml").read_text()
    assert run("set", "ws.local_model=__TEST__").returncode == 0
    assert (ROOT / "stack.settings.yaml").read_text() == before


# ── 2. helper: правка в tmp (apply / идемпотентность / битый YAML / комментарии) ──

@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    m = load_mod()
    f = tmp_path / "stack.settings.yaml"
    f.write_text("version: 1\n\nws:\n  # комментарий сохранить\n  local_model: old\n", encoding="utf-8")
    monkeypatch.setattr(m, "SETTINGS", f)
    monkeypatch.setattr(m, "DOTENV", tmp_path / ".env")
    monkeypatch.setattr(m, "TRASH", tmp_path / ".trash")
    monkeypatch.setattr(m, "ROOT", tmp_path)
    return m, f


def test_apply_then_idempotent_exit4(tmp_env, capsys):
    m, f = tmp_env
    class A: assignment = "ws.local_model=new"; apply = True
    assert m.cmd_set(A()) == 0
    assert "local_model: new" in f.read_text()
    assert "# комментарий сохранить" in f.read_text()          # комментарий цел
    assert list((f.parent / ".trash").glob("stack.settings.yaml.*.bak"))  # бэкап
    assert m.cmd_set(A()) == 4                                # идемпотентно


def test_broken_yaml_get_exit5(tmp_env):
    m, f = tmp_env
    f.write_text("ws:\n  bad: : :\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        m.cmd_get(type("A", (), {"key": "ws.local_model", "quiet": True})())
    assert e.value.code == 5


def test_missing_file_get_exit0(tmp_env):
    m, f = tmp_env
    f.unlink()
    assert m.cmd_get(type("A", (), {"key": "ws.local_model", "quiet": True})()) == 0


def test_resolve_env_above_file(tmp_env, monkeypatch):
    m, _f = tmp_env
    monkeypatch.setenv("WS_LOCAL_MODEL", "ENVWINS")
    data = {"ws": {"local_model": "FILE"}}
    val, src = m.resolve("ws.local_model", data)
    assert (val, src) == ("ENVWINS", "env")


# ── 3. parity-дефолтов (анти-дрейф; split по конусам для ollama_base) ──

def _grab(pattern, text):
    m = re.search(pattern, text)
    assert m, f"не найдено: {pattern}"
    return m.group(1)


def test_parity_defaults_model_and_ollama():
    sfile = (ROOT / "stack.settings.yaml").read_text()
    mkf = (ROOT / "Makefile").read_text()
    j2 = (ROOT / "ansible" / "templates" / ".env.j2").read_text()
    gv = (ROOT / "ansible" / "inventory" / "group_vars" / "all" / "main.yml").read_text()
    hv = (ROOT / "ansible" / "inventory" / "host_vars" / "aikb.yml").read_text()

    model = "qwen3:30b-a3b-instruct-2507-q4_K_M"
    assert _grab(r"local_model:\s*(\S+)", sfile) == model
    assert _grab(r"WS_LOCAL_MODEL_DEFAULT\s*:=\s*(\S+)", mkf) == model
    assert model in j2 and model in gv  # .env.j2 + group_vars fallback

    # ollama_base — split by design: dev-пара (file, Makefile-DEFAULT) vs prod-пара (.env.j2/group_vars/host_vars)
    assert _grab(r"local_ollama_base:\s*(\S+)", sfile) == "mcp-knowledge-ollama:11434"
    assert _grab(r"WS_LOCAL_OLLAMA_BASE_DEFAULT\s*:=\s*(\S+)", mkf) == "mcp-knowledge-ollama:11434"
    prod = "host.docker.internal:11434"
    assert prod in j2 and prod in gv and prod in hv
