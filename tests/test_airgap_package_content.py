"""Стражи инварианта контурной изоляции пакета обновления (аудит 2026-10-02, Вариант B).

Инвариант: деплой/обновление переносят ТОЛЬКО код и модели; документы (корпус
знаний) и индексы (data/qdrant) НИКОГДА не переносятся и не затираются — у dev и
prod свои документы.

Тесты:
  (а) pack — whitelist артефактов: нет копирования corpus/docs/data/qdrant,
      модели только из белого списка UPDATE_MODELS_NEEDED;
  (б) apply-stage/unpack — мутируемые пути ⊆ {клон кода, docker load, models-dir,
      staging}, runtime-защита guard_write_path присутствует;
  (в) реальный пакет /kvm/update-bundles/mcp-kb-update-*/ — manifest (images/models)
      и пути дерева чистые (SKIP, если каталога нет);
  (г) «красный при подсадке» — фикстура-сниппет с corpus-артефактом детектируется
      (без реальной подсадки в прод-файлы).
"""

import glob
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OFFLINE_SH = ROOT / "scripts" / "offline-update.sh"
UNPACK_SH = ROOT / "scripts" / "airgap-bundle-unpack.sh"

# Белый список моделей, разрешённых к переносу пакетом (038 §2.4 / аудит №13).
ALLOWED_MODELS = ("mxbai-embed-large", "nomic-embed-text", "qwen2.5:7b")

# Запрещённые мутирующие пути (литеральные маркеры корпуса/индекса/доков).
FORBIDDEN_LITERALS = ("corpus", "docs/", "data/qdrant")
# `knowledge`/`universal` — ТОЛЬКО как path-сегменты: не цепляем `mcp-knowledge`
# (имя кода/образа) и `qdrant/qdrant` (имя образа).
_KNOWLEDGE_SEGMENT = re.compile(r"(?:^|/)knowledge(?=/|$|\s|[\"'`])")
_UNIVERSAL_SEGMENT = re.compile(r"(?:^|/)universal(?=/|$|\s|[\"'`])")


def _read(path):
    return path.read_text(encoding="utf-8")


def _func_body(text, name):
    """Тело bash-функции `name() { … }` (до следующей функции верхнего уровня)."""
    start = text.index(f"{name}() {{")
    rest = text[start + len(f"{name}() {{"):]
    nxt = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_]*\(\) \{", rest)
    return rest[: nxt.start()] if nxt else rest


def _strip_full_line_comments(text):
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def _forbidden_hits(text):
    """→ список нарушений инварианта (мутирующие пути в корпус/индекс/доки)."""
    hits = []
    for tok in FORBIDDEN_LITERALS:
        if tok in text:
            hits.append(tok)
    if _KNOWLEDGE_SEGMENT.search(text):
        hits.append("knowledge/ (корпус)")
    if _UNIVERSAL_SEGMENT.search(text):
        hits.append("universal/ (корпус)")
    return hits


class TestPackWhitelist:
    """(а) pack: whitelist артефактов, модели только из белого списка."""

    def test_models_only_from_whitelist(self):
        text = _read(OFFLINE_SH)
        m = re.search(r"UPDATE_MODELS_NEEDED=\((.*?)\)", text, re.DOTALL)
        assert m, "нет UPDATE_MODELS_NEEDED в offline-update.sh"
        items = re.findall(r'"([^"]+)"', m.group(1))
        assert items == list(ALLOWED_MODELS), (
            f"модели в пакете ({items}) != белого списка {list(ALLOWED_MODELS)}"
        )

    def test_pack_no_corpus_docs_qdrant(self):
        body = _func_body(_read(OFFLINE_SH), "cmd_pack")
        hits = _forbidden_hits(body)
        assert not hits, f"pack копирует корпус/индекс/доки: {hits}"

    def test_pack_writes_expected_artifacts(self):
        body = _func_body(_read(OFFLINE_SH), "cmd_pack")
        for art in ("repo.git", "manifest.json", "CHECKSUMS.sha256", "images/"):
            assert art in body, f"pack не создаёт артефакт пакета {art}"


class TestMutationPaths:
    """(б) apply-stage/unpack: мутируемые пути ⊆ {клон, docker, models-dir, staging}."""

    def test_apply_stage_mutation_subset(self):
        body = _func_body(_read(OFFLINE_SH), "cmd_apply_stage")
        hits = _forbidden_hits(body)
        assert not hits, f"apply-stage пишет в корпус/индекс: {hits}"
        # положительные маркеры разрешённых целей записи
        for target in ("$clone", "$stage", "$models_dir", "docker load"):
            assert target in body, f"apply-stage не пишет в разрешённую цель {target}"
        # runtime-защита путей записи присутствует
        assert "guard_write_path" in body, "нет runtime-защиты guard_write_path"

    def test_unpack_mutation_subset(self):
        text = _strip_full_line_comments(_read(UNPACK_SH))
        hits = _forbidden_hits(text)
        assert not hits, f"unpack пишет в корпус/индекс: {hits}"
        assert "docker load" in text, "unpack не грузит образы через docker load"
        assert "MDEST=" in text, "unpack не пишет модели в $MDEST (models-dir)"


class TestRealPackage:
    """(в) реальный пакет: manifest и пути дерева чистые (SKIP без каталога)."""

    def test_manifest_and_paths_clean(self):
        manifests = sorted(glob.glob("/kvm/update-bundles/mcp-kb-update-*/manifest.json"))
        if not manifests:
            pytest.skip("нет реального пакета /kvm/update-bundles/mcp-kb-update-*/")
        for mf in manifests:
            pkg_dir = Path(mf).parent
            manifest = json.loads(Path(mf).read_text(encoding="utf-8"))
            for img in manifest.get("images", []):
                assert {"name", "id", "file"} <= set(img), f"образ без полей: {img}"
                assert img["file"].startswith("images/"), f"file не образ: {img}"
            for m in manifest.get("models", []):
                assert "name" in m and "digest" in m, f"модель без name/digest: {m}"
                assert "/" not in m["name"], f"имя модели с путём/реестром: {m['name']}"
            bad = [
                str(p.relative_to(pkg_dir))
                for p in pkg_dir.rglob("*")
                if p.is_file() and _forbidden_hits(str(p.relative_to(pkg_dir)))
            ]
            assert not bad, f"в пакете {pkg_dir} есть корпус/индекс-артефакты: {bad}"


class TestRedOnPlantedArtifact:
    """(г) «красный при подсадке»: corpus-артефакт в фикстуре детектируется."""

    @pytest.mark.parametrize(
        "snippet",
        [
            "cp corpus/notes.md pkg/",
            "cp -a ../knowledge /opt/mcp-knowledge/data/",
            "tar -czf pkg.tar.gz ../knowledge/universal/",
            "cp docs/runbook.md pkg/",
            "mkdir -p staging/data/qdrant",
        ],
    )
    def test_forbidden_snippet_detected(self, snippet):
        assert _forbidden_hits(snippet), f"не детектирован корпус-артефакт: {snippet!r}"

    def test_allowed_artifacts_not_flagged(self):
        # код/образ/модели — разрешённые цели, не должны давать ложных срабатываний
        assert not _forbidden_hits(
            "docker save mcp-knowledge-mcp-server:latest | gzip -1"
        )
        assert not _forbidden_hits(
            "cp $pkg/models/blobs/sha256-abc $models_dir/blobs/"
        )
        assert not _forbidden_hits("git -C $clone merge --ff-only FETCH_HEAD")
