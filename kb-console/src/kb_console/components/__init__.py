"""Переиспользуемые UI-компоненты kb-console."""

from __future__ import annotations

from .header import render_header
from .progress_panel import build_scan_progress
from .replace_dialog import show_replace_dialog

__all__ = ["build_scan_progress", "render_header", "show_replace_dialog"]
