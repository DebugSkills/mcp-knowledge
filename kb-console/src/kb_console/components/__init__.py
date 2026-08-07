"""Переиспользуемые UI-компоненты kb-console."""

from __future__ import annotations

from .header import render_header
from .progress_panel import build_scan_progress

__all__ = ["build_scan_progress", "render_header"]
