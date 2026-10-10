"""Deploy-Delta a1 (bibliography Ф1): образ канонизатора mcp-knowledge-kb-converter:latest
во ВСЕХ air-gap save/build-списках и pack-скрипте.

Канонизатор kb-converter (sidecar Ф1) обязателен в любом air-gap переносе стека:
без него air-gap-контур не сможет канонизировать документные форматы → canonical
PDF. Каждая точка, где перечисляются образы для build/save, обязана его включать.

Стражи:
  (а) статически — каждый save/build-список содержит converter;
  (д) R4 (arch-2026-10-10-ai-ws-p2-1) — digest-пин ollama (6-й элемент
      BASE_IMAGES, tag@sha256) согласован с image:/digest-комментарием
      ollama в compose×2 (+ негатив-пробы мутаций в tmp);
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
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONVERTER = "mcp-knowledge-kb-converter:latest"

OFFLINE_UPDATE = ROOT / "scripts" / "offline-update.sh"
OFFLINE_DEPLOY = ROOT / "scripts" / "offline-deploy.sh"
COMPOSE_DEV = ROOT / "docker-compose.yml"
COMPOSE_PROD = ROOT / "docker-compose.prod.yml"
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


# ═══ R4 (arch-2026-10-10-ai-ws-p2-1): пин ollama согласован с BASE_IMAGES ═══
# 6-й элемент BASE_IMAGES — composite ref «tag@sha256:<hex>»; compose×2 держат
# тег в image: + digest в комментарии. Аналог схемы из
# test_airgap_prod_compose_images.py (дублирование умышленное — файлы
# самодостаточны, стиль репо).

OLLAMA_PREFIX = "ollama/ollama:"
_OLLAMA_PIN_RE = re.compile(rf"{OLLAMA_PREFIX}[0-9.]+@sha256:([0-9a-f]{{64}})")


def _assert_ollama_pin(offline_update, compose_dev, compose_prod):
    refs = [i for i in _bash_array(_read(offline_update), "BASE_IMAGES")
            if i.startswith(OLLAMA_PREFIX)]
    assert len(refs) == 1, (
        f"в BASE_IMAGES ожидался ровно один ollama-образ (6-й элемент): {refs}"
    )
    ref = refs[0]
    assert "@sha256:" in ref, (
        f"(б) ollama-элемент BASE_IMAGES без digest-пина (@sha256:): {ref!r}"
    )
    tag, digest = ref.split("@sha256:", 1)
    assert re.fullmatch(r"[0-9a-f]{64}", digest), (
        f"(б) digest ollama в BASE_IMAGES не hex64: {digest!r}"
    )
    for compose in (compose_dev, compose_prod):
        text = _read(compose)
        doc = yaml.safe_load(text)
        svc = (doc.get("services") or {}).get("ollama")
        assert isinstance(svc, dict) and "image" in svc, (
            f"в {compose.name} нет сервиса ollama с image:"
        )
        assert svc["image"] == tag, (
            f"(а) {compose.name}: image ollama {svc['image']!r} != тег-части "
            f"BASE_IMAGES {tag!r} (срез до @sha256:)"
        )
        m = _OLLAMA_PIN_RE.search(text)
        assert m, (
            f"(в) в {compose.name} нет digest-комментария вида "
            f"'# … {OLLAMA_PREFIX}<tag>@sha256:<hex>'"
        )
        assert m.group(1) == digest, (
            f"(в) {compose.name}: digest в комментарии {m.group(1)} != digest "
            f"BASE_IMAGES {digest}"
        )


class TestOllamaPinConsistency:
    """(д)/R4: ollama-строка согласована с BASE_IMAGES во всех носителях."""

    def test_ollama_pin_consistent_across_carriers(self):
        _assert_ollama_pin(OFFLINE_UPDATE, COMPOSE_DEV, COMPOSE_PROD)


class TestOllamaPinGuardsCatchMutations:
    """Негатив-проба R4: мутации фикстур в tmp → каждый страж КРАСНЫЙ."""

    @staticmethod
    def _base_pin():
        ref = [i for i in _bash_array(_read(OFFLINE_UPDATE), "BASE_IMAGES")
               if i.startswith(OLLAMA_PREFIX)][0]
        return ref, ref.split("@sha256:", 1)

    @staticmethod
    def _mutated(tmp_path, src, name, old, new):
        text = _read(src)
        assert old in text, f"мутация не применима: {old!r} нет в {src.name}"
        dst = tmp_path / name
        dst.write_text(text.replace(old, new), encoding="utf-8")
        return dst

    def test_wrong_tag_in_prod_compose_detected(self, tmp_path):
        """Тег 0.20.3 в image: prod-compose → страж (а) красный."""
        ref, (tag, digest) = self._base_pin()
        bad = self._mutated(tmp_path, COMPOSE_PROD, "docker-compose.prod.yml",
                            f"image: {tag}", f"image: {tag.replace('0.20.2', '0.20.3')}")
        with pytest.raises(AssertionError, match=r"\(а\)"):
            _assert_ollama_pin(OFFLINE_UPDATE, COMPOSE_DEV, bad)

    def test_lost_digest_in_base_images_detected(self, tmp_path):
        """Потеря @sha256: у 6-го элемента BASE_IMAGES → страж (б) красный."""
        ref, (tag, digest) = self._base_pin()
        bad = self._mutated(tmp_path, OFFLINE_UPDATE, "offline-update.sh", ref, tag)
        with pytest.raises(AssertionError, match=r"\(б\)"):
            _assert_ollama_pin(bad, COMPOSE_DEV, COMPOSE_PROD)

    def test_wrong_hex_in_compose_comment_detected(self, tmp_path):
        """Другой hex в digest-комментарии prod-compose → страж (в) красный."""
        ref, (tag, digest) = self._base_pin()
        bad = self._mutated(tmp_path, COMPOSE_PROD, "docker-compose.prod.yml",
                            f"@sha256:{digest}", f"@sha256:{'f' * 64}")
        with pytest.raises(AssertionError, match=r"\(в\)"):
            _assert_ollama_pin(OFFLINE_UPDATE, COMPOSE_DEV, bad)
