"""Реестры AI-верстака: roles/tools/gates/model_classes/shapes + quotas.

quotas (Ф4.1) — participant-роли и квоты; node-роли режимов остаются в
roles.yaml и квотами не управляются.

Ф3.5a-1 (trace_id: arch-2026-10-05-ai-workspace). Данные — YAML-файлы в
каталоге реестра, по одному на kind. Контракты полей проверяет валидатор
(Ф3.5a-2), потребляет движок режимов (Ф3.5b) — здесь только загрузка.

Контракт hot-reload: reload_if_changed() сравнивает mtime каждого файла с
кэшем и при первом изменении перечитывает каталог целиком. Отсутствующий
или битый YAML -> RegistryError с путём и причиной (fail-closed), без
молчаливых дефолтов и частичных состояний.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import yaml

__all__ = ["Registry", "RegistryError"]


class RegistryError(Exception):
    """Fail-closed ошибка реестра: файл недоступен, битый YAML, неизвестный kind."""


class Registry:
    """Каталог YAML-реестров режимов с кэшем и hot-reload по mtime."""

    kinds: ClassVar[tuple[str, ...]] = (
        "roles",
        "tools",
        "gates",
        "model_classes",
        "shapes",
        "quotas",  # Ф4.1: participant-роли/квоты; схема — registry/quotas.py
    )

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)
        self._cache: dict[str, dict] = {}
        self._mtimes: dict[str, float] = {}

    def path_for(self, kind: str) -> Path:
        """Путь файла реестра для kind."""
        self._ensure_kind(kind)
        return self.dir / f"{kind}.yaml"

    def load(self) -> None:
        """Перечитать все реестры в кэш; любой дефект файла -> RegistryError."""
        cache: dict[str, dict] = {}
        mtimes: dict[str, float] = {}
        for kind in self.kinds:
            path = self.path_for(kind)
            try:
                mtime = path.stat().st_mtime
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise RegistryError(f"реестр недоступен: {path}: {exc}") from exc
            except yaml.YAMLError as exc:
                raise RegistryError(f"битый YAML: {path}: {exc}") from exc
            if not isinstance(raw, dict):
                got = type(raw).__name__
                raise RegistryError(
                    f"реестр должен быть YAML-отображением: {path}: получен {got}"
                )
            cache[kind] = raw
            mtimes[kind] = mtime
        self._cache = cache
        self._mtimes = mtimes

    def get(self, kind: str) -> dict:
        """Данные реестра по kind; при первом обращении — ленивая загрузка."""
        self._ensure_kind(kind)
        if not self._cache:
            self.load()
        return self._cache[kind]

    def reload_if_changed(self) -> bool:
        """Перечитать каталог, если mtime любого файла изменился.

        True — перезагрузка выполнена; False — ничего не менялось. Пропавший
        файл считается изменением и приводит к RegistryError в load()
        (fail-closed).
        """
        if not self._mtimes:
            self.load()
            return True
        for kind in self.kinds:
            path = self.path_for(kind)
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                raise RegistryError(f"реестр недоступен: {path}: {exc}") from exc
            if mtime != self._mtimes.get(kind):
                self.load()
                return True
        return False

    def _ensure_kind(self, kind: str) -> None:
        if kind not in self.kinds:
            raise RegistryError(
                f"неизвестный kind реестра: {kind!r}; ожидается один из {self.kinds}"
            )
