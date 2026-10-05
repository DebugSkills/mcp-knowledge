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
      prod-compose (ретег-rollback не теряется).

Без Docker/сети: чтение файлов + YAML/regex (стиль
test_airgap_converter_image_lists.py).
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

COMPOSE_PROD = ROOT / "docker-compose.prod.yml"
OFFLINE_UPDATE = ROOT / "scripts" / "offline-update.sh"
OFFLINE_DEPLOY = ROOT / "scripts" / "offline-deploy.sh"
UPDATE_YML = ROOT / "ansible" / "playbooks" / "update.yml"

# Прикладные сервисы prod-стека. Инфраструктура (qdrant/ollama/caddy)
# сознательно вне стражей: update-канон переносит её отдельно (BASE_IMAGES
# содержит qdrant/caddy; ollama живёт на узле с моделями в volume).
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
