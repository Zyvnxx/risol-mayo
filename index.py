"""Vercel serverless entrypoint.

Vercel detects ``api/index.py`` (or any file under ``api/``) as a Python
serverless function. It expects a callable named ``app`` (or a request
``handler``) at module scope. We import the Flask app and re-export it.

The repo's ``vercel.json`` rewrites every request to this function so
Flask can handle routing for both the static page (``/``) and the API
endpoints (``/api/...``).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable so ``import app`` finds ../app.py
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402  (import after sys.path tweak)

# Vercel's @vercel/python runtime looks for `app` (WSGI callable).
__all__ = ["app"]
