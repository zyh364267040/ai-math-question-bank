"""Local read-only web interface for the AI math question bank."""

from .app import app, create_app

__all__ = ["app", "create_app"]
