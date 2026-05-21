# Admin Panel — standalone Pterodactyl multi-server controller.
#
# Completely independent from the owo-dusk bot. Runs as its own Flask app,
# its own port, its own config. Deploy it on a different host — it only
# needs outbound HTTPS access to your Pterodactyl panels.
#
# Quickstart (local):
#   1. python -m venv .venv  &&  .venv\Scripts\activate   (Windows)
#                              source .venv/bin/activate   (Linux/macOS)
#   2. pip install -r requirements.txt
#   3. cp config.example.json config.json   (and edit it)
#   4. python app.py
#
# Environment overrides (optional, take precedence over config.json):
#   ADMIN_PORT           — port to listen on (default 7860)
#   ADMIN_HOST           — bind address     (default 0.0.0.0)
#   ADMIN_PASSWORD       — auth password    (overrides config.json)
#   ADMIN_CONFIG         — path to config   (default ./config.json)
#   ADMIN_CONFIG_JSON    — full config JSON inline (used by serverless
#                          hosts like Vercel where there's no writable
#                          filesystem). Takes precedence over file.
#   ADMIN_CONFIG_B64     — same as ADMIN_CONFIG_JSON but base64-encoded
#                          (handy when env-var size limits or quoting
#                          mangles raw JSON).

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ADMIN_CONFIG") or BASE_DIR / "config.json")

# Tighter per-panel timeout so a slow panel can't blow Vercel's 10s budget.
PTERO_TIMEOUT = float(os.environ.get("PTERO_TIMEOUT") or 5)
PTERO_PARALLEL = int(os.environ.get("PTERO_PARALLEL") or 8)
DEFAULT_PORT = 7860
DEFAULT_HOST = "0.0.0.0"

ALLOWED_SIGNALS = ("start", "stop", "restart", "kill")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _load_config_from_env() -> dict | None:
    """Load config from inline env vars (used on serverless hosts where
    the filesystem is read-only or absent)."""
    raw = os.environ.get("ADMIN_CONFIG_JSON")
    if not raw:
        b64 = os.environ.get("ADMIN_CONFIG_B64")
        if b64:
            try:
                raw = base64.b64decode(b64).decode("utf-8")
            except Exception as e:
                print(f"[admin-panel] ADMIN_CONFIG_B64 decode failed: {e}", file=sys.stderr)
                return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[admin-panel] ADMIN_CONFIG_JSON parse failed: {e}", file=sys.stderr)
        return None


def _load_config() -> dict:
    # 1) Inline env config wins (Vercel / serverless friendly).
    env_cfg = _load_config_from_env()
    if env_cfg is not None:
        return env_cfg

    # 2) Otherwise, look for a file on disk (local / VPS / Docker).
    if not CONFIG_PATH.exists():
        sample = BASE_DIR / "config.example.json"
        msg = (
            f"[admin-panel] Config not found: {CONFIG_PATH}\n"
            f"           Copy {sample.name} to config.json and edit it,\n"
            f"           or set ADMIN_CONFIG_JSON / ADMIN_CONFIG_B64 / ADMIN_CONFIG."
        )
        print(msg, file=sys.stderr)
        return {"password": "", "panels": []}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[admin-panel] Failed to read config: {e}", file=sys.stderr)
        return {"password": "", "panels": []}


CONFIG: dict = _load_config()
CONFIG_MTIME: float = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0


def _maybe_reload_config() -> None:
    """Hot-reload config when it changes.

    On serverless hosts the config comes from env and is effectively
    immutable per-deployment, so we skip the file mtime check there.
    Locally / on a VPS, edit ``config.json`` and the next request picks
    up the change without restarting."""
    global CONFIG, CONFIG_MTIME

    # If config is sourced from env, there's nothing to reload mid-process.
    if os.environ.get("ADMIN_CONFIG_JSON") or os.environ.get("ADMIN_CONFIG_B64"):
        return

    try:
        if not CONFIG_PATH.exists():
            return
        mtime = CONFIG_PATH.stat().st_mtime
        if mtime != CONFIG_MTIME:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                CONFIG = json.load(f)
            CONFIG_MTIME = mtime
    except Exception as e:
        print(f"[admin-panel] reload failed: {e}", file=sys.stderr)


def _resolved_password() -> str:
    return os.environ.get("ADMIN_PASSWORD") or str(CONFIG.get("password") or "")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _check_password() -> bool:
    expected = _resolved_password()
    if not expected:
        # No password set — refuse all auth so an unconfigured deployment
        # never accidentally exposes panel control.
        return False
    given = request.headers.get("password") or ""
    # constant-time compare to avoid trivial timing leaks
    return secrets.compare_digest(str(given), str(expected))


def _unauthorized():
    return jsonify({"status": "error", "message": "Unauthorized"}), 401


# ---------------------------------------------------------------------------
# Panel collection & calls
# ---------------------------------------------------------------------------
def _collect_panels() -> list[dict]:
    """Normalise the configured panels (each entry still carries its real
    API key — never serialise these dicts back to the client)."""
    out = []
    raw = CONFIG.get("panels")
    if not isinstance(raw, list):
        return out
    for idx, p in enumerate(raw):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or f"panel-{idx + 1}").strip() or f"panel-{idx + 1}"
        out.append(
            {
                "id": pid,
                "name": str(p.get("name") or pid),
                "panelUrl": str(p.get("panelUrl") or "").rstrip("/"),
                "serverId": str(p.get("serverId") or "").strip(),
                "clientApiKey": str(p.get("clientApiKey") or "").strip(),
            }
        )
    return out


def _ptero_resources(panel: dict) -> dict:
    """GET /resources for a single panel. Returns a normalised status
    dict (or an error description)."""
    url = f"{panel['panelUrl']}/api/client/servers/{panel['serverId']}/resources"
    headers = {
        "Authorization": f"Bearer {panel['clientApiKey']}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=PTERO_TIMEOUT)
    except requests.RequestException as e:
        return {"reachable": False, "error": str(e)}

    if not (200 <= r.status_code < 300):
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}
        return {"reachable": False, "error": f"HTTP {r.status_code}", "panel": body}

    try:
        data = r.json().get("attributes", {}) or {}
    except Exception:
        return {"reachable": False, "error": "Invalid JSON from panel"}

    res = data.get("resources") or {}
    return {
        "reachable": True,
        "state": data.get("current_state") or "unknown",
        "isSuspended": bool(data.get("is_suspended", False)),
        "memoryBytes": int(res.get("memory_bytes") or 0),
        "cpuAbsolute": float(res.get("cpu_absolute") or 0.0),
        "diskBytes": int(res.get("disk_bytes") or 0),
        "uptimeMs": int(res.get("uptime") or 0),
        "networkRxBytes": int(res.get("network_rx_bytes") or 0),
        "networkTxBytes": int(res.get("network_tx_bytes") or 0),
    }


def _safe_view(panel: dict, with_status: bool) -> dict:
    out = {
        "id": panel["id"],
        "name": panel["name"],
        "panelUrl": panel["panelUrl"],
        "serverId": panel["serverId"],
        "configured": bool(
            panel["panelUrl"] and panel["serverId"] and panel["clientApiKey"]
        ),
    }
    if with_status and out["configured"]:
        out["status"] = _ptero_resources(panel)
    elif with_status:
        out["status"] = {
            "reachable": False,
            "error": "Panel is not fully configured (panelUrl/serverId/clientApiKey).",
        }
    return out


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "success", "ok": True}), 200


@app.route("/api/panels", methods=["GET"])
def api_panels():
    _maybe_reload_config()
    if not _check_password():
        return _unauthorized()

    with_status = request.args.get("status", "1").lower() not in ("0", "false", "no")
    panels = _collect_panels()

    if with_status and panels:
        # Fan out the per-panel /resources calls so a slow panel can't
        # serialize the whole response. Critical on Vercel where the
        # function has a hard 10s budget on the Hobby plan.
        max_workers = max(1, min(PTERO_PARALLEL, len(panels)))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            public = list(
                ex.map(lambda p: _safe_view(p, with_status=True), panels)
            )
    else:
        public = [_safe_view(p, with_status=False) for p in panels]

    return jsonify(
        {
            "status": "success",
            "panels": public,
            "count": len(public),
            "ts": int(time.time()),
        }
    ), 200


@app.route("/api/panels/power", methods=["POST"])
def api_panels_power():
    _maybe_reload_config()
    if not _check_password():
        return _unauthorized()

    body = request.get_json(silent=True) or {}
    panel_id = str(body.get("id") or "").strip()
    signal = str(body.get("signal") or "").lower()

    if signal not in ALLOWED_SIGNALS:
        return jsonify(
            {
                "status": "error",
                "message": f"Invalid signal (allowed: {', '.join(ALLOWED_SIGNALS)}).",
            }
        ), 400

    panels = _collect_panels()
    panel = next((p for p in panels if p["id"] == panel_id), None)
    if not panel:
        return jsonify(
            {"status": "error", "message": f"Unknown panel id: {panel_id!r}"}
        ), 404

    if not (panel["panelUrl"] and panel["serverId"] and panel["clientApiKey"]):
        return jsonify(
            {
                "status": "error",
                "message": f"Panel {panel['name']!r} is not fully configured.",
            }
        ), 400

    target = f"{panel['panelUrl']}/api/client/servers/{panel['serverId']}/power"
    headers = {
        "Authorization": f"Bearer {panel['clientApiKey']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            target, headers=headers, json={"signal": signal}, timeout=PTERO_TIMEOUT
        )
    except requests.RequestException as e:
        return jsonify(
            {"status": "error", "message": f"Pterodactyl request failed: {e}"}
        ), 502

    if 200 <= resp.status_code < 300:
        return jsonify(
            {
                "status": "success",
                "id": panel_id,
                "name": panel["name"],
                "signal": signal,
                "panelStatus": resp.status_code,
            }
        ), 200

    try:
        panel_msg = resp.json()
    except Exception:
        panel_msg = {"raw": resp.text[:300]}
    return jsonify(
        {
            "status": "error",
            "message": f"Panel rejected the request ({resp.status_code}).",
            "panel": panel_msg,
        }
    ), 502


@app.route("/favicon.ico")
def favicon():
    static_dir = BASE_DIR / "static"
    if (static_dir / "favicon.ico").exists():
        return send_from_directory(str(static_dir), "favicon.ico")
    return ("", 204)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def _resolve_port() -> int:
    raw = os.environ.get("ADMIN_PORT") or CONFIG.get("port") or DEFAULT_PORT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _resolve_host() -> str:
    return os.environ.get("ADMIN_HOST") or str(CONFIG.get("host") or DEFAULT_HOST)


def main():
    port = _resolve_port()
    host = _resolve_host()

    if not _resolved_password():
        print(
            "[admin-panel] WARNING: no password configured. All API calls "
            "will return 401 until you set 'password' in config.json or "
            "ADMIN_PASSWORD env var.",
            file=sys.stderr,
        )

    print(f"[admin-panel] listening on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
