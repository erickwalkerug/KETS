"""KETS website entrypoint.

The trading engine remains in bot.py. This module exposes the Flask application
to Gunicorn without exposing the strategy code to website visitors.
"""
from bot import app

__all__ = ["app"]
