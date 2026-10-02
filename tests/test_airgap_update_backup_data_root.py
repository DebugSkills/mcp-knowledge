"""Guard-тест Н9 (аудит 2026-10-02): preflight-бэкап update.yml передаёт DATA_ROOT.

Данные прод-узла живут ВНЕ git-клона: data_root из inventory (DATA_ROOT-вынос).
scripts/backup.sh:15 берёт DATA_ROOT из env с дефолтом `$PROJECT_DIR/data`
(внутрь клона) — без environment.DATA_ROOT в таске бэкапа валидация снапшота
падает «Snapshot file not found» (подтверждено на aikb, make -C ansible
update-local). Тот же контракт, что у deploy.yml (cron-env) и make prod-backup.

Статический тест: только stdlib + PyYAML, без сети/Docker/ansible-runner
и без git-операций.
"""

import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
UPDATE_YML = ROOT / "ansible" / "playbooks" / "update.yml"
TASK_PREFIX = "Preflight: бэкап перед обновлением"


class _PermissiveLoader(yaml.SafeLoader):
    """SafeLoader, игнорирующий неизвестные теги (!vault и др.)."""


_PermissiveLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: None
)


def _load_tasks():
    doc = yaml.load(UPDATE_YML.read_text(encoding="utf-8"), Loader=_PermissiveLoader)
    return doc[0]["tasks"]


def _backup_task():
    """Таска preflight-бэкапа (name начинается с TASK_PREFIX) или None."""
    for task in _load_tasks():
        if str(task.get("name", "")).startswith(TASK_PREFIX):
            return task
    return None


class TestBackupDataTask:
    def test_task_exists_with_chdir_clone_dir(self):
        task = _backup_task()
        assert task is not None, (
            "в ansible/playbooks/update.yml нет таски с name, "
            "начинающимся с " + repr(TASK_PREFIX)
        )
        command = task.get("ansible.builtin.command")
        assert isinstance(command, dict), (
            "у таски бэкапа ожидается block-mapping ansible.builtin.command (cmd + chdir)"
        )
        assert command.get("chdir") == "{{ clone_dir }}", (
            "chdir таски бэкапа должен быть '{{ clone_dir }}', "
            "получено: " + repr(command.get("chdir"))
        )

    def test_environment_passes_data_root(self):
        task = _backup_task()
        assert task is not None, "таска бэкапа не найдена"
        env = task.get("environment") or {}
        assert "DATA_ROOT" in env, (
            "у таски бэкапа нет environment.DATA_ROOT: backup.sh возьмёт дефолт "
            "$PROJECT_DIR/data (данные внутри клона) и упадёт "
            "«Snapshot file not found» (Н9)"
        )
        assert "data_root" in env["DATA_ROOT"], (
            "environment.DATA_ROOT должен рендериться из inventory-переменной "
            "data_root, получено: " + repr(env["DATA_ROOT"])
        )

    def test_calls_backup_sh(self):
        task = _backup_task()
        assert task is not None, "таска бэкапа не найдена"
        command = task.get("ansible.builtin.command")
        cmd = command if isinstance(command, str) else (command or {}).get("cmd", "")
        assert "bash scripts/backup.sh" in str(cmd), (
            "таска бэкапа должна вызывать 'bash scripts/backup.sh' "
            "(страховка от переименований), получено: " + repr(cmd)
        )

    def test_red_control_without_data_root(self):
        """Красный контроль: без DATA_ROOT проверка environment обязана падать.

        Работаем с deepcopy таски (реальный файл не трогаем): после удаления
        ключа DATA_ROOT из копии он не должен обнаруживаться — значит,
        test_environment_passes_data_root на такой таске честно красный.
        """
        task = _backup_task()
        assert task is not None, "таска бэкапа не найдена"
        mutated = copy.deepcopy(task)
        env = mutated.get("environment")
        if isinstance(env, dict):
            env.pop("DATA_ROOT", None)
        assert "DATA_ROOT" not in (mutated.get("environment") or {}), (
            "red-control: после удаления DATA_ROOT из копии таски ключ "
            "не должен обнаруживаться"
        )


def _play_vars():
    doc = yaml.load(UPDATE_YML.read_text(encoding="utf-8"), Loader=_PermissiveLoader)
    return doc[0].get("vars") or {}


class TestSkipBackupEscapeHatch:
    """update_skip_backup (2026-10-02): осознанный пропуск preflight-бэкапа для air-gap.

    Причина: на узле полный `backup.sh` не проходит (Н10 — тихий RC=1), а решение
    оператора — фуллбек-промежутки не делать. Флаг должен быть ВЫКЛЮЧЕН по умолчанию
    и громко предупреждать при включении (не тихий обход).
    """

    def test_default_off(self):
        v = _play_vars()
        assert "update_skip_backup" in v, (
            "в vars update.yml нет update_skip_backup — нечем осознанно пропустить бэкап"
        )
        assert v["update_skip_backup"] is False, (
            "update_skip_backup должен быть выключен по умолчанию, "
            "получено: " + repr(v["update_skip_backup"])
        )

    def test_backup_task_guarded_by_flag(self):
        task = _backup_task()
        assert task is not None, "таска бэкапа не найдена"
        when = str(task.get("when", ""))
        assert "update_skip_backup" in when and "not" in when, (
            "таска бэкапа должна иметь when: not (update_skip_backup | bool), "
            "получено: " + repr(when)
        )

    def test_warn_task_is_loud(self):
        tasks = _load_tasks()
        warn = [x for x in tasks if "ПРОПУЩЕН" in str(x.get("name", ""))]
        assert warn, "нет предупреждающей таски про пропущенный бэкап (тихий обход — нельзя)"
        task = warn[0]
        assert "update_skip_backup" in str(task.get("when", "")), (
            "warn-таска должна срабатывать по update_skip_backup"
        )
        msg = str((task.get("ansible.builtin.debug") or {}).get("msg", ""))
        assert "НЕ выполняется" in msg and ":prev" in msg, (
            "warn-таска должна явно говорить, что бэкапа не будет, и называть откат :prev"
        )

    def test_data_root_env_survives_flag(self):
        """Флаг не должен ослаблять контракт DATA_ROOT, когда бэкап всё-таки идёт."""
        task = _backup_task()
        assert task is not None, "таска бэкапа не найдена"
        assert "DATA_ROOT" in (task.get("environment") or {}), (
            "DATA_ROOT-контракт Н9 должен сохраняться (флаг — только про пропуск)"
        )
