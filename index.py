"""Vercel WSGI entrypoint with import-safety wrapper.

This file is intentionally tiny. It tries to import the real Flask app
from ``_app.py``. If that import fails — invalid env-var JSON, missing
module, syntax issue from a botched edit — we fall back to a minimal
WSGI app that returns the actual exception as JSON instead of letting
Vercel render the opaque ``FUNCTION_INVOCATION_FAILED`` page.
"""

import json
import sys
import traceback

# Capture any error that happens while importing the real app.
_IMPORT_ERROR = None
try:
    from _app import app  # noqa: F401  (re-export for Vercel)
except Exception:
    _IMPORT_ERROR = traceback.format_exc()
    print("[admin-panel] _app import failed:\n" + _IMPORT_ERROR, file=sys.stderr)


if _IMPORT_ERROR is not None:
    # Build a minimal WSGI app that always returns the import traceback.
    # This keeps the function alive long enough for the user to see the
    # real error in the browser / DevTools instead of a blank crash page.
    def app(environ, start_response):  # type: ignore[no-redef]
        body = json.dumps(
            {
                "status": "error",
                "stage": "import",
                "message": "Failed to import _app.py",
                "traceback": _IMPORT_ERROR.splitlines()[-30:],
            },
            indent=2,
        ).encode("utf-8")
        start_response(
            "500 Internal Server Error",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("X-Admin-Panel-Stage", "import-failed"),
            ],
        )
        return [body]
