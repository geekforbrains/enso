"""Enso web UI — a Starlette app for browsing and editing tasks, jobs, runs, skills."""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
