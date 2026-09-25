"""Общая настройка тестов kb-console.

Импорт _local_http выставляет NO_PROXY для localhost при коллекции тестов
(trace code-2026-09-25-021): без этого локальные smoke-проверки уходят на
внешний HTTP(S)_PROXY и падают при снятом NO_PROXY.
"""
from __future__ import annotations

from _local_http import local_get  # side-effect: NO_PROXY для localhost

__all__ = ["local_get"]
