"""Deploy-Delta a1 (bibliography Ф1): образ канонизатора mcp-knowledge-kb-converter:latest
во ВСЕХ air-gap save/build-списках и pack-скрипте.

Канонизатор kb-converter (sidecar Ф1) обязателен в любом air-gap переносе стека:
без него air-gap-контур не сможет канонизировать документные форматы → canonical
PDF. Каждая точка, где перечисляются образы для build/save, обязана его включать.

Стражи:
  (а) статически — каждый save/build-список содержит converter;
  (б) детектор «забывчивости» — согласованность save-list ↔ build-list: каждый
      образ, собранный `docker compose build` / `docker build`, обязан попасть в
      save-список (BASE_IMAGES / docker save). Новый образ в build не должен
      «забываться» в save;
  (в) bash -n синтаксис отредактированных скриптов.

Без Docker/сети: только чтение файлов + парсинг (стиль test_airgap_package_content.py).
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONVERTER = "mcp-knowledge-kb-converter:latest"

OFFLINE_UPDATE = ROOT / "scripts" / "offline-update.sh"
OFFLINE_DEPLOY = ROOT / "scripts" / "offline-deploy.sh"
PACK_SUBSET = ROOT / "scripts" / "airgap-pack-subset.sh"
UPDATE_YML = ROOT / "ansible" / "playbooks" / "update.yml"


def _read(path):
    return path.read_text(encoding="utf-8")


def _bash_array(text, name):
    """→ список элементов bash-массива `name=( ... )` (по кавычкам)."""
    m = re.search(rf"{name}=\((.*?)\)", text, re.DOTALL)
    assert m, f"нет массива {name}"
    return re.findall(r'"([^"]+)"', m.group(1))


class TestOfflineUpdate:
    def test_base_images_include_converter(self):
        imgs = _bash_array(_read(OFFLINE_UPDATE), "BASE_IMAGES")
        assert CONVERTER in imgs, f"BASE_IMAGES без {CONVERTER}: {imgs}"

    def test_rollback_images_include_converter(self):
        imgs = _bash_array(_read(OFFLINE_UPDATE), "ROLLBACK_IMAGES")
        assert CONVERTER in imgs, f"ROLLBACK_IMAGES без {CONVERTER}: {imgs}"

    def test_build_services_include_converter(self):
        text = _read(OFFLINE_UPDATE)
        assert re.search(r"build mcp-server kb-console kb-converter", text), (
            "docker compose build не собирает kb-converter"
        )

    def test_save_matches_build_consistency(self):
        """(б) Детектор: каждый self-образ из build обязан быть в BASE_IMAGES (save)."""
        text = _read(OFFLINE_UPDATE)
        m = re.search(r"build\s+([a-z0-9-]+(?:\s+[a-z0-9-]+)+)", text)
        assert m, "нет docker compose build"
        services = m.group(1).split()
        base = _bash_array(text, "BASE_IMAGES")
        for svc in services:
            assert any(svc in img for img in base), (
                f"service '{svc}' собран build, но отсутствует в BASE_IMAGES (save)"
            )


class TestUpdateYml:
    def test_build_includes_converter(self):
        text = _read(UPDATE_YML)
        assert "build mcp-server kb-console kb-converter" in text, (
            "update.yml build не собирает kb-converter"
        )

    def test_rollback_case_includes_converter(self):
        text = _read(UPDATE_YML)
        assert ("mcp-knowledge-mcp-server:latest|kb-console:prod"
                "|mcp-knowledge-kb-converter:latest") in text, (
            "case-switch retag :prev не знает kb-converter (rollback-образ потеряется)"
        )


class TestOfflineDeploy:
    def test_converter_var_defined(self):
        text = _read(OFFLINE_DEPLOY)
        assert 'KB_CONVERTER_IMAGE="mcp-knowledge-kb-converter:latest"' in text

    def test_converter_built(self):
        text = _read(OFFLINE_DEPLOY)
        assert 'docker build -t "$KB_CONVERTER_IMAGE" "$GIT_ROOT/kb-converter"' in text

    def test_converter_saved(self):
        text = _read(OFFLINE_DEPLOY)
        assert ('docker save "$MCP_IMAGE" "$QDRANT_IMAGE" "$KB_CONSOLE_IMAGE" '
                '"$KB_CONVERTER_IMAGE"') in text, (
            "docker save не включает kb-converter → образ не попадёт в images.tar"
        )

    def test_build_and_save_consistent(self):
        """(б) Детектор: каждый `docker build -t $VAR` обязан попасть в `docker save`."""
        text = _read(OFFLINE_DEPLOY)
        built = re.findall(r'docker build -t "\$(\w+)"', text)
        assert "KB_CONVERTER_IMAGE" in built, "kb-converter не собирается"
        saved = next(ln for ln in text.splitlines() if "docker save" in ln)
        for var in built:
            assert f'"${var}"' in saved, f"образ ${var} собран, но не сохранён"

    def test_step_numbering_consistent(self):
        text = _read(OFFLINE_DEPLOY)
        assert "[1/6]" in text, "prepare() должен считать 6 шагов"
        assert "[1/5]" not in text, "остался баг нумерации [1/5]"


class TestPackSubset:
    def test_default_images_include_converter(self):
        text = _read(PACK_SUBSET)
        m = re.search(r"^IMAGES=\((.*?)\)", text, re.DOTALL | re.MULTILINE)
        assert m, "нет дефолтного IMAGES"
        imgs = re.findall(r'"([^"]+)"', m.group(1))
        assert CONVERTER in imgs, f"дефолтный IMAGES без {CONVERTER}: {imgs}"


class TestSyntax:
    @pytest.mark.parametrize("script", [OFFLINE_UPDATE, OFFLINE_DEPLOY, PACK_SUBSET])
    def test_bash_syntax(self, script):
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True,
                           text=True, timeout=60, check=False)
        assert r.returncode == 0, r.stderr
