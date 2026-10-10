"""Deploy-Delta (code-2026-10-05-deploy-host-mechanism): имена образов
docker-compose.prod.yml сведены к канону 038-air-gap.

Легаси-путь (offline-deploy.sh + docker-compose.prod.yml) исторически собирал
сервер как mcp-knowledge-server:prod, тогда как канонический air-gap-путь
(offline-update.sh + ansible/playbooks/update.yml) оперирует
mcp-knowledge-mcp-server:latest. Расхождение: узел, поставленный legacy-бандлом,
при 038-апдейте не получал :prev-ретег (имя вне case-списка update.yml) →
rollback-hint ссылался на несуществующий тег; build: у kb-converter в prod-файле
дал «тихую» попытку сборки на узле без интернета.

Стражи (вердикт критика P1-2):
  (а) каждый image: прикладного сервиса prod-compose присутствует в BASE_IMAGES
      scripts/offline-update.sh (пакет update-bundle переносит всё);
  (б) серверный тег prod-compose == MCP_IMAGE из scripts/offline-deploy.sh
      (legacy-бандл и канон не расходятся);
  (в) в docker-compose.prod.yml НЕТ build: ни у одного сервиса
      (air-gap = только готовые образы, никакой «тихой» сборки офлайн);
  (г) :prev-case ansible/playbooks/update.yml покрывает все прикладные образы
      prod-compose (ретег-rollback не теряется);
  (д) R4 (arch-2026-10-10-ai-ws-p2-1): digest-пин ollama согласован во всех
      носителях — 6-й элемент BASE_IMAGES (tag@sha256) == image: ollama в
      compose×2 == digest в комментариях compose×2 (+ негатив-пробы мутаций).

Без Docker/сети: чтение файлов + YAML/regex (стиль
test_airgap_converter_image_lists.py).
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

COMPOSE_PROD = ROOT / "docker-compose.prod.yml"
COMPOSE_DEV = ROOT / "docker-compose.yml"
OFFLINE_UPDATE = ROOT / "scripts" / "offline-update.sh"
OFFLINE_DEPLOY = ROOT / "scripts" / "offline-deploy.sh"
UPDATE_YML = ROOT / "ansible" / "playbooks" / "update.yml"

# Прикладные сервисы prod-стека. Инфраструктура (qdrant/caddy) сознательно
# вне стражей (а)-(г); ollama с arch-2026-10-10-ai-ws-p2-1 — обязательный 6-й
# элемент BASE_IMAGES и охраняется пин-стражем TestOllamaPinConsistency (д/R4).
APP_SERVICES = ("mcp-server", "kb-console", "kb-converter")


def _read(path):
    return path.read_text(encoding="utf-8")


def _bash_array(text, name):
    """→ список элементов bash-массива `name=( ... )` (по кавычкам)."""
    m = re.search(rf"{name}=\((.*?)\)", text, re.DOTALL)
    assert m, f"нет массива {name}"
    return re.findall(r'"([^"]+)"', m.group(1))


def _prod_services():
    """→ {service: image} прикладных сервисов prod-compose."""
    doc = yaml.safe_load(_read(COMPOSE_PROD))
    services = doc["services"]
    missing = [s for s in APP_SERVICES if s not in services]
    assert not missing, f"в prod-compose нет сервисов {missing}"
    return {s: services[s]["image"] for s in APP_SERVICES}


class TestProdComposeImagesInBaseImages:
    def test_app_images_in_base_images(self):
        """(а) Каждый прикладной образ prod-compose — в BASE_IMAGES (update-bundle)."""
        base = _bash_array(_read(OFFLINE_UPDATE), "BASE_IMAGES")
        for svc, img in _prod_services().items():
            assert img in base, (
                f"образ '{img}' (сервис {svc}) из docker-compose.prod.yml "
                f"отсутствует в BASE_IMAGES offline-update.sh: {base}"
            )


class TestLegacyDeployMatchesCompose:
    def test_server_image_matches_offline_deploy(self):
        """(б) MCP_IMAGE offline-deploy.sh == образу mcp-server в prod-compose."""
        m = re.search(r'MCP_IMAGE="([^"]+)"', _read(OFFLINE_DEPLOY))
        assert m, "в offline-deploy.sh нет MCP_IMAGE=..."
        legacy = m.group(1)
        compose_img = _prod_services()["mcp-server"]
        assert legacy == compose_img, (
            f"расхождение имён сервера: offline-deploy.sh MCP_IMAGE={legacy!r} "
            f"!= docker-compose.prod.yml image={compose_img!r}"
        )


class TestProdComposeNoBuild:
    def test_no_build_in_any_service(self):
        """(в) Air-gap prod-compose: build: запрещён — только готовые образы."""
        doc = yaml.safe_load(_read(COMPOSE_PROD))
        offenders = [s for s, cfg in doc["services"].items() if "build" in cfg]
        assert not offenders, (
            f"в docker-compose.prod.yml есть build: у сервисов {offenders} — "
            "на air-gap-узле без интернета это тихая попытка сборки вместо fail-loud"
        )


class TestPrevRetagCoversAppImages:
    def test_prev_case_covers_all_app_images(self):
        """(г) :prev-ретег из update.yml знает каждый прикладной образ prod-compose."""
        text = _read(UPDATE_YML)
        m = re.search(
            r'case "\{\{ img\.name \}\}" in\s*\n\s*([^\n)]+)\)', text
        )
        assert m, "в update.yml нет case-switch :prev-ретега"
        prev_list = m.group(1).strip().split("|")
        for svc, img in _prod_services().items():
            assert img in prev_list, (
                f"образ '{img}' (сервис {svc}) вне :prev-case update.yml — "
                f"rollback-ретег не будет создан: {prev_list}"
            )


# ═══ R4 (arch-2026-10-10-ai-ws-p2-1): пин ollama согласован во всех носителях ═══
# Форма пина (R1): BASE_IMAGES хранит composite ref «tag@sha256:<hex>»,
# compose×2 — тег в image: + digest в комментарии «# … ollama/ollama:<tag>@sha256:<hex>».
# Комментарий над массивом BASE_IMAGES (без скобок внутри) — иначе ленивый
# regex _bash_array обрежет массив на первой «)» и 6-й элемент исчезнет.

OLLAMA_PREFIX = "ollama/ollama:"
_OLLAMA_PIN_RE = re.compile(rf"{OLLAMA_PREFIX}[0-9.]+@sha256:([0-9a-f]{{64}})")


def _assert_ollama_pin(offline_update, compose_dev, compose_prod):
    """Страж (д): (а) тег-часть == image: ollama в compose×2; (б) 6-й элемент
    BASE_IMAGES содержит @sha256: (hex64); (в) digest-комментарий compose×2 ==
    digest BASE_IMAGES. Путь-параметры → переиспользуется негатив-пробами на
    tmp-мутациях (боевые файлы не правятся)."""
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
    """(д)/R4: digest-пин ollama един во всех носителях air-gap-переноса."""

    def test_ollama_pin_consistent_across_carriers(self):
        _assert_ollama_pin(OFFLINE_UPDATE, COMPOSE_DEV, COMPOSE_PROD)


class TestOllamaPinGuardsCatchMutations:
    """Негатив-проба R4: мутация фикстур в tmp → каждый страж КРАСНЫЙ
    (без красного негатива страж не принят — критик Ф-A2)."""

    @staticmethod
    def _base_pin():
        ref = [i for i in _bash_array(_read(OFFLINE_UPDATE), "BASE_IMAGES")
               if i.startswith(OLLAMA_PREFIX)][0]
        return ref, ref.split("@sha256:", 1)

    @staticmethod
    def _mutated(tmp_path, src, name, old, new):
        """tmp-копия носителя с заменой old→new (боевые файлы НЕ правятся)."""
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
