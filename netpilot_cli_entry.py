"""Frozen-build entry point for the CLI.

PyInstaller needs a real script to analyse; this simply forwards to the console
script declared in ``pyproject.toml``.
"""

from netpilot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
