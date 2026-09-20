"""Frozen-build entry point for the desktop application."""

from netpilot.desktop import run_gui

if __name__ == "__main__":
    raise SystemExit(run_gui())
