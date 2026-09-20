"""Web dashboard (FastAPI + a dependency-free single-page front end)."""

from .app import create_app, get_app, run_server

__all__ = ["create_app", "run_server", "get_app"]
