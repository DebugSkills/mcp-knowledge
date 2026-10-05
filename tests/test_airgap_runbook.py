"""Тесты air-gap ранбука 038: документ + make-таргет (без сети).

Проверяем, что документ-ранбук существует, содержит ключевые маркеры, а
`make airgap-runbook` печатает его целиком (единый источник — документ,
без дублирования текста в Makefile).
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "operations" / "airgap-first-install.md"
MAKEFILE = ROOT / "Makefile"

MARKERS = (
    "bundle-pack", "bundle-ship-usb", "bundle-ship-net",
    "airgap-bundle-unpack.sh", "--data-root", "deploy.yml",
    "/health", "update-local", "0.62 MiB/s", "tmux",
    # аудит контурной изоляции 2026-10-02: инвариант + guard bootstrap
    "Инвариант контурной изоляции", "bootstrap_reindex", "data/qdrant",
)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          check=False, **kw)


class TestRunbookDoc:
    def test_exists_and_has_markers(self):
        assert RUNBOOK.exists(), f"нет {RUNBOOK}"
        text = RUNBOOK.read_text(encoding="utf-8")
        for m in MARKERS:
            assert m in text, f"нет маркера '{m}' в ранбуке"

    def test_reasonable_length(self):
        n = len(RUNBOOK.read_text(encoding="utf-8").splitlines())
        # Лимит поднят с 220 до 340 (аудит 2026-10-02), затем до 360 (make-поток)
        # Раздел «Весь поток — через таргеты make»: pack→ship→update одной командой
        # «Инвариант контурной изоляции» + шаг «после bootstrap выключить флаг».
        # Затем до 380: Шаг 8 — пост-апдейт проверка на узле (verify-deploy) +
        # маркер air-gap узла /etc/mcp-knowledge/airgap-node
        # (трасса code-2026-10-05-deploy-host-mechanism).
        assert 80 <= n <= 380, f"ранбук подозрительной длины: {n} строк"


class TestMakeTarget:
    def test_target_present(self):
        text = MAKEFILE.read_text(encoding="utf-8")
        assert "airgap-runbook:" in text
        # таргет cat-ает документ, а не дублирует текст
        assert "docs/operations/airgap-first-install.md" in text

    def test_visible_in_make_help(self):
        r = run(["make", "help"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert "airgap-runbook" in r.stdout

    def test_make_airgap_runbook_prints_doc(self):
        r = run(["make", "airgap-runbook"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert len(r.stdout.splitlines()) >= 80
        assert "airgap-bundle-unpack.sh" in r.stdout
        assert "0.62 MiB/s" in r.stdout
