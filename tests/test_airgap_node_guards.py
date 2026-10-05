"""Механические гварды деплой-механизма на air-gap узле (без Docker/сети).

Статически (чтение Makefile) + smoke (`make -n`) проверяем, что:
  * deploy/push отказывают при маркере AIRGAP_NODE_MARKER — сборка из
    исходников на узле недопустима, обновление только пакетом;
  * airgap-update поддерживает пост-апдейтный verify (VERIFY=1);
  * ansible update-airgap префлайтит все 5 бинарников на узле.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
ANSIBLE_MAKEFILE = ROOT / "ansible" / "Makefile"

MARKER_VAR = "AIRGAP_NODE_MARKER"
MARKER_DEFAULT = "/etc/mcp-knowledge/airgap-node"
GUARD_TEST = '[ -f "$(AIRGAP_NODE_MARKER)" ]'
NODE_BINS = ("git", "python3", "ansible-playbook", "tar", "docker")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          check=False, **kw)


def recipe_lines(path: Path, target: str) -> str:
    """Рецепт таргета: tab-строки после строки `target:`.

    Пустые строки внутри рецепта пропускаются; первая не-tab содержательная
    строка (целевой заголовок или column-0 комментарий) завершает рецепт.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    head = next((i for i, ln in enumerate(lines)
                 if ln.startswith(f"{target}:")), None)
    assert head is not None, f"таргет {target} не найден в {path}"
    out = []
    for ln in lines[head + 1:]:
        if ln.startswith("\t"):
            out.append(ln)
        elif not ln.strip():
            continue
        else:
            break
    return "\n".join(out)


class TestAirgapNodeGuard:
    """deploy/push: гвард-первой-строкой рецепта (маркер узла → exit 1)."""

    def test_marker_var_defined(self):
        text = MAKEFILE.read_text(encoding="utf-8")
        assert f"{MARKER_VAR} ?= {MARKER_DEFAULT}" in text

    def test_deploy_guard(self):
        r = recipe_lines(MAKEFILE, "deploy")
        assert GUARD_TEST in r, "deploy не проверяет маркер air-gap узла"
        assert "exit 1" in r
        # гвард срабатывает ДО сборки стека
        assert r.index(GUARD_TEST) < r.index("up -d --build")
        # сообщение указывает пакетный путь обновления
        assert "air-gap" in r and "airgap-update" in r

    def test_push_guard(self):
        r = recipe_lines(MAKEFILE, "push")
        assert GUARD_TEST in r, "push не проверяет маркер air-gap узла"
        assert "exit 1" in r
        # гвард срабатывает ДО git push и ДО деплоя
        assert r.index(GUARD_TEST) < r.index("git push")


class TestAirgapUpdateVerify:
    """airgap-update: опциональный пост-апдейтный verify (VERIFY=1)."""

    def test_verify_hook(self):
        r = recipe_lines(MAKEFILE, "airgap-update")
        assert "$(VERIFY)" in r
        assert "scripts/verify-deploy.sh" in r

    def test_help_mentions_verify(self):
        r = run(["make", "help"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        line = next((ln for ln in r.stdout.splitlines()
                     if ln.strip().startswith("airgap-update")), "")
        assert "VERIFY=1" in line, f"help airgap-update без [VERIFY=1]: {line!r}"


class TestAnsibleUpdateAirgapDeps:
    """ansible update-airgap: deps-преflight 5 бинарников первой строкой."""

    def test_deps_preflight_checks_all_bins(self):
        r = recipe_lines(ANSIBLE_MAKEFILE, "update-airgap")
        assert "command -v" in r
        for b in NODE_BINS:
            assert b in r, f"deps-преflight не проверяет {b}"
        assert "exit 1" in r

    def test_deps_preflight_is_first_recipe_line(self):
        r = recipe_lines(ANSIBLE_MAKEFILE, "update-airgap")
        first = r.splitlines()[0]
        # deps-преflight стартует первой строкой — до проверки BUNDLE/распаковки
        assert "for b in" in first and "git" in first
        assert r.index("command -v") < r.index('test -n "$(BUNDLE)"')


class TestMakeSmoke:
    """Smoke без Docker/сети: dry-run и help не падают, гварды видны."""

    def test_make_help(self):
        r = run(["make", "help"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert "deploy" in r.stdout and "push" in r.stdout

    def test_make_n_deploy_shows_guard(self):
        r = run(["make", "-n", "deploy"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert MARKER_DEFAULT in r.stdout, "гвард маркера не виден в -n выводе"

    def test_make_n_ansible_update_airgap_shows_deps(self):
        r = run(["make", "-n", "-C", "ansible", "update-airgap",
                 "BUNDLE=/tmp/x"], cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert "command -v" in r.stdout, "deps-строка не видна в -n выводе"
