"""Desktop (PyQt6) application."""

from .bridge import CoreThread, UiBridge

__all__ = ["CoreThread", "UiBridge", "run_gui"]


def run_gui(*args, **kwargs):
    """Lazy import so ``netpilot`` works without PyQt6 installed."""
    from .app import run_gui as _run_gui

    return _run_gui(*args, **kwargs)
