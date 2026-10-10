"""Рендер-механика litellm-шаблонов (Ф-B R3, arch-2026-10-10-ai-ws-p2-1).

Проверяет семантику envsubst-рендера Makefile-цели gateway-render и ansible-таски:
  1) int-литерал в max_parallel_requests (НЕ строка — P0-латент str vs int на
     LiteLLM 1.104.0, §6b резолюция б);
  2) whitelist: envsubst '${LITELLM_MAX_PARALLEL}' не трогает чужие ${...}
     (в config.gateway.yml есть ${LITELLM_MASTER_KEY}, ${DEEPSEEK_API_KEY});
  3) python-fallback (О-7, нет gettext-base) даёт тот же результат;
  4) дефолт-рендер K=1 (I13) — источник дефолта теста test_wfq_signature.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ["litellm.config.yaml", "litellm.local_only.config.yaml"]
ENVSUBST = shutil.which("envsubst")


def _envsubst(template: str, k: str) -> str:
    env = dict(os.environ, LITELLM_MAX_PARALLEL=k)
    return subprocess.run(
        ["envsubst", "${LITELLM_MAX_PARALLEL}"],
        input=template, capture_output=True, text=True, env=env, check=True,
    ).stdout


def _pyfallback(template: str, k: str) -> str:
    # тот же однострочник, что в Makefile gateway-render / ansible update.yml
    import sys
    code = ('import os,sys; sys.stdout.write(sys.stdin.read()'
            '.replace("${LITELLM_MAX_PARALLEL}", os.environ["LITELLM_MAX_PARALLEL"]))')
    env = dict(os.environ, LITELLM_MAX_PARALLEL=k)
    return subprocess.run(
        [sys.executable, "-c", code], input=template,
        capture_output=True, text=True, env=env, check=True,
    ).stdout


@pytest.mark.skipif(ENVSUBST is None, reason="envsubst (gettext-base) отсутствует")
@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("k", ["1", "2", "7"])
def test_render_int_literal(name: str, k: str):
    """Рендер с K → max_parallel_requests = int K (int-литерал, не строка)."""
    tpl = (ROOT / f"{name}.in").read_text("utf-8")
    doc = yaml.safe_load(_envsubst(tpl, k))
    local = doc["model_list"][0]["litellm_params"]
    val = local["max_parallel_requests"]
    assert isinstance(val, int) and not isinstance(val, bool), \
        f"{name}: K должен быть int-литералом, получен {type(val).__name__}: {val!r}"
    assert val == int(k)
    assert "${LITELLM_MAX_PARALLEL}" not in _envsubst(tpl, k)  # плейсхолдеров не осталось


@pytest.mark.skipif(ENVSUBST is None, reason="envsubst (gettext-base) отсутствует")
def test_render_whitelist_other_vars_untouched():
    """Whitelist '${LITELLM_MAX_PARALLEL}': чужие ${...} НЕ подставляются."""
    tpl = "a: ${LITELLM_MAX_PARALLEL}\nb: ${DEEPSEEK_API_KEY}\nc: ${LITELLM_MASTER_KEY}\n"
    env = dict(os.environ, LITELLM_MAX_PARALLEL="2",
               DEEPSEEK_API_KEY="SECRET-SHOULD-STAY", LITELLM_MASTER_KEY="MK-STAY")
    out = subprocess.run(
        ["envsubst", "${LITELLM_MAX_PARALLEL}"],
        input=tpl, capture_output=True, text=True, env=env, check=True,
    ).stdout
    assert out == "a: 2\nb: ${DEEPSEEK_API_KEY}\nc: ${LITELLM_MASTER_KEY}\n"


def test_python_fallback_same_result():
    """О-7 fallback (узел без gettext-base): python-замена = envsubst-результат."""
    tpl = (ROOT / "litellm.config.yaml.in").read_text("utf-8")
    a, b = _pyfallback(tpl, "2"), (_envsubst(tpl, "2") if ENVSUBST else None)
    if b is not None:
        assert a == b
    doc = yaml.safe_load(a)
    assert doc["model_list"][0]["litellm_params"]["max_parallel_requests"] == 2


@pytest.mark.parametrize("name", TEMPLATES)
def test_default_render_k1(name: str):
    """Дефолт-рендер шаблона с K=1 (I13) — тот же источник, что в лок-степе
    test_wfq_signature (дефолт = шаблон с подстановкой K=1)."""
    doc = yaml.safe_load(_pyfallback((ROOT / f"{name}.in").read_text("utf-8"), "1"))
    local = next(m for m in doc["model_list"] if m["model_name"] == "local")
    val = local["litellm_params"]["max_parallel_requests"]
    assert isinstance(val, int) and val == 1
