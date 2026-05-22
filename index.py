# Admin Panel — standalone Pterodactyl multi-server controller.
#
# Flat-layout Flask app. Everything (HTML, CSS, JS, this file) lives at
# the project root so the same source tree works on:
#   • Vercel (serverless, just needs index.py + vercel.json)
#   • A regular VPS / Docker host (`python index.py`)
#   • Any PaaS that runs `python index.py` (Render, Railway, Fly, …)
#
# Quickstart (local):
#   1. python -m venv .venv  &&  .venv\Scripts\activate   (Windows)
#                              source .venv/bin/activate   (Linux/macOS)
#   2. pip install -r requirements.txt
#   3. cp config.example.json config.json   (and edit it)
#   4. python index.py
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
#   PTERO_TIMEOUT        — per-panel HTTP timeout in seconds (default 5)
#   PTERO_PARALLEL       — max concurrent /resources calls (default 8)
#
# Optional — enable in-browser config editor (writes the new config to
# Vercel's env vars and triggers a redeploy):
#   VERCEL_TOKEN         — Personal Access Token (vercel.com/account/tokens)
#   VERCEL_PROJECT_ID    — Project ID (Vercel project Settings → General)
#   VERCEL_TEAM_ID       — only required if the project lives in a team
#   VERCEL_ENV_TARGET    — "production,preview,development" (default: all)

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import requests
from flask import Flask, jsonify, request, send_from_directory

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

# When the lambda is unzipped on Vercel, included files may live next to
# index.py, or one level up depending on builder version. Walk a small
# list of candidates so the page assets resolve no matter where Vercel
# put them.
_FILE_LOOKUP_DIRS: list[Path] = []
def _register_lookup_dirs() -> None:
    seen = set()
    for cand in (
        BASE_DIR,
        BASE_DIR.parent,
        Path.cwd(),
        Path("/var/task"),
        Path("/var/task") / BASE_DIR.name,
    ):
        try:
            cand = cand.resolve()
        except Exception:
            continue
        if cand in seen or not cand.exists():
            continue
        seen.add(cand)
        _FILE_LOOKUP_DIRS.append(cand)
_register_lookup_dirs()


def _find_asset(filename: str) -> Path | None:
    """Locate an HTML/CSS/JS file across the candidate roots."""
    for root in _FILE_LOOKUP_DIRS:
        p = root / filename
        if p.is_file():
            return p
    return None


CONFIG_PATH = Path(os.environ.get("ADMIN_CONFIG") or BASE_DIR / "config.json")

# Per-panel HTTP timeout & parallelism. Tight on Vercel (10s budget).
PTERO_TIMEOUT = float(os.environ.get("PTERO_TIMEOUT") or 5)
PTERO_PARALLEL = int(os.environ.get("PTERO_PARALLEL") or 8)
DEFAULT_PORT = 7860
DEFAULT_HOST = "0.0.0.0"

ALLOWED_SIGNALS = ("start", "stop", "restart", "kill")

# Files served directly from the project root.
STATIC_FILES = {
    "app.js":  "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

# ---------------------------------------------------------------------------
# Config loading
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
    """Hot-reload config when ``config.json`` changes on disk.

    On serverless hosts the config comes from env and is effectively
    immutable per-deployment, so we skip the file mtime check there."""
    global CONFIG, CONFIG_MTIME

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
        # Refuse all auth on an unconfigured deployment.
        return False
    given = request.headers.get("password") or ""
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
# Flask app — flat layout (no templates/, no static/ dirs)
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)


@app.errorhandler(Exception)
def _on_exception(err):
    """Surface real exception detail instead of letting Vercel render
    the opaque FUNCTION_INVOCATION_FAILED page.

    The traceback is only revealed to clients that present the admin
    password (header ``password``) so we don't leak internals publicly."""
    tb = traceback.format_exc()
    print("[admin-panel] unhandled exception:\n" + tb, file=sys.stderr)

    # Authenticated callers see the full trace to ease debugging.
    try:
        if _check_password():
            return jsonify(
                {
                    "status": "error",
                    "message": f"{type(err).__name__}: {err}",
                    "traceback": tb.splitlines()[-12:],
                }
            ), 500
    except Exception:
        pass

    return jsonify(
        {
            "status": "error",
            "message": f"{type(err).__name__}: {err}",
        }
    ), 500


@app.route("/")
def index():
    """Serve the landing page from any of the candidate roots."""
    target = _find_asset("index.html")
    if target is None:
        return (
            "<h1>index.html not found</h1>"
            "<p>Lambda is missing the bundled HTML. "
            "Visit <code>/_debug</code> for diagnostics.</p>",
            500,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    return send_from_directory(str(target.parent), target.name)


@app.route("/<path:filename>")
def root_file(filename: str):
    """Serve a small allowlist of static assets.

    We deliberately do not expose every file — config.json, the source
    .py, and similar would otherwise be downloadable."""
    if filename in STATIC_FILES:
        target = _find_asset(filename)
        if target is None:
            return ("", 404)
        return send_from_directory(
            str(target.parent),
            target.name,
            mimetype=STATIC_FILES[filename],
        )
    if filename == "favicon.ico":
        target = _find_asset("favicon.ico")
        if target is None:
            return ("", 204)
        return send_from_directory(str(target.parent), target.name)
    return ("", 404)


@app.route("/_debug")
def debug_info():
    """Diagnostic endpoint — dumps where the lambda thinks files are.
    Useful when assets 404 on a fresh Vercel deploy."""
    info = {
        "base_dir": str(BASE_DIR),
        "cwd": str(Path.cwd()),
        "lookup_dirs": [
            {"path": str(d), "files": sorted(p.name for p in d.iterdir() if p.is_file())[:50]}
            for d in _FILE_LOOKUP_DIRS
        ],
        "found": {
            name: (str(_find_asset(name)) if _find_asset(name) else None)
            for name in ("index.html", "app.js", "style.css", "config.json")
        },
        "env_config": {
            "ADMIN_CONFIG_JSON": bool(os.environ.get("ADMIN_CONFIG_JSON")),
            "ADMIN_CONFIG_B64":  bool(os.environ.get("ADMIN_CONFIG_B64")),
            "ADMIN_PASSWORD":    bool(os.environ.get("ADMIN_PASSWORD")),
            "VERCEL_TOKEN":      bool(os.environ.get("VERCEL_TOKEN")),
            "VERCEL_PROJECT_ID": bool(os.environ.get("VERCEL_PROJECT_ID")),
            "VERCEL_TEAM_ID":    bool(os.environ.get("VERCEL_TEAM_ID")),
        },
        "vercel_sync_enabled": _vercel_creds() is not None,
        "config_panels": len(_collect_panels()),
    }
    return jsonify(info), 200


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


# ---------------------------------------------------------------------------
# Config editor — read & write
# ---------------------------------------------------------------------------
#
# The editor returns the live config with API keys masked, so the UI can
# safely render them. On save we accept the edited config back; any field
# that arrives as the literal mask value ("********") is treated as
# "keep the original", so a user can change panel names without seeing
# or re-typing keys.
#
# Persisting the new config:
#   • If VERCEL_TOKEN + VERCEL_PROJECT_ID are set, we patch the
#     ADMIN_CONFIG_JSON env var on Vercel and trigger a redeploy.
#   • Otherwise (local / VPS / Docker), we write to config.json on disk
#     and the next request reloads it via _maybe_reload_config().

API_KEY_MASK = "********"
SENSITIVE_KEYS = ("clientApiKey", "password")


def _redact_config(cfg: dict) -> dict:
    out = deepcopy(cfg) if isinstance(cfg, dict) else {}
    if isinstance(out.get("password"), str) and out["password"]:
        out["password"] = API_KEY_MASK
    panels = out.get("panels")
    if isinstance(panels, list):
        for p in panels:
            if isinstance(p, dict) and p.get("clientApiKey"):
                p["clientApiKey"] = API_KEY_MASK
    return out


def _merge_unmasked(original: dict, incoming: dict) -> dict:
    """Recursively replace mask values with the original ones.

    Keeps the structure of ``incoming`` (so deletions work — if a panel
    is removed, it's removed) but re-fills any masked field with the
    matching value from ``original``."""
    if not isinstance(incoming, dict) or not isinstance(original, dict):
        return incoming

    out: dict = {}
    for k, v in incoming.items():
        if v == API_KEY_MASK and k in original:
            out[k] = original[k]
        elif isinstance(v, list):
            orig_list = original.get(k) if isinstance(original.get(k), list) else []
            new_list = []
            # Match incoming panels back to originals by `id` so users can
            # reorder freely without losing keys.
            orig_by_id = {
                str(item.get("id")): item
                for item in orig_list
                if isinstance(item, dict) and item.get("id") is not None
            }
            for item in v:
                if isinstance(item, dict):
                    src = orig_by_id.get(str(item.get("id")), {})
                    new_list.append(_merge_unmasked(src, item))
                else:
                    new_list.append(item)
            out[k] = new_list
        elif isinstance(v, dict):
            out[k] = _merge_unmasked(original.get(k, {}), v)
        else:
            out[k] = v
    return out


def _validate_config(cfg) -> tuple[bool, str]:
    if not isinstance(cfg, dict):
        return False, "Config must be a JSON object."
    panels = cfg.get("panels")
    if panels is None:
        return True, ""  # empty is allowed
    if not isinstance(panels, list):
        return False, "`panels` must be an array."
    seen_ids = set()
    for i, p in enumerate(panels):
        if not isinstance(p, dict):
            return False, f"panels[{i}] must be an object."
        pid = str(p.get("id") or "").strip()
        if not pid:
            return False, f"panels[{i}].id is required."
        if pid in seen_ids:
            return False, f"Duplicate panel id: {pid!r}."
        seen_ids.add(pid)
        for fld in ("panelUrl", "serverId", "clientApiKey"):
            val = p.get(fld)
            if val is not None and not isinstance(val, str):
                return False, f"panels[{i}].{fld} must be a string."
    return True, ""


def _vercel_creds() -> tuple[str, str, str | None, list[str]] | None:
    token = os.environ.get("VERCEL_TOKEN")
    project_id = os.environ.get("VERCEL_PROJECT_ID")
    if not token or not project_id:
        return None
    team_id = os.environ.get("VERCEL_TEAM_ID") or None
    targets_raw = os.environ.get("VERCEL_ENV_TARGET", "production,preview,development")
    targets = [t.strip() for t in targets_raw.split(",") if t.strip()]
    return token, project_id, team_id, targets


def _vercel_api(method: str, path: str, json_body: Optional[dict] = None,
                params: Optional[dict] = None) -> requests.Response:
    creds = _vercel_creds()
    if creds is None:
        raise RuntimeError(
            "Vercel credentials not configured (VERCEL_TOKEN / VERCEL_PROJECT_ID)."
        )
    token, _, team_id, _ = creds
    url = f"https://api.vercel.com{path}"
    if team_id:
        params = dict(params or {})
        params.setdefault("teamId", team_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    return requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=8,
    )


def _vercel_upsert_env(key: str, value: str) -> tuple[bool, str]:
    """Create or update a Vercel project env var. Returns (ok, message)."""
    creds = _vercel_creds()
    if creds is None:
        return False, "Vercel credentials not configured."
    _, project_id, _, targets = creds

    # 1) See if the env var already exists.
    try:
        listing = _vercel_api(
            "GET", f"/v9/projects/{project_id}/env", params={"decrypt": "false"}
        )
    except requests.RequestException as e:
        return False, f"List env failed: {e}"
    if listing.status_code != 200:
        return False, f"List env failed: HTTP {listing.status_code} — {listing.text[:200]}"
    existing = next(
        (e for e in listing.json().get("envs", []) if e.get("key") == key),
        None,
    )

    payload = {
        "key": key,
        "value": value,
        "type": "encrypted",
        "target": targets,
    }

    if existing:
        try:
            r = _vercel_api(
                "PATCH",
                f"/v9/projects/{project_id}/env/{existing['id']}",
                json_body={
                    "value": value,
                    "target": targets,
                    "type": "encrypted",
                },
            )
        except requests.RequestException as e:
            return False, f"Update env failed: {e}"
    else:
        try:
            r = _vercel_api(
                "POST",
                f"/v10/projects/{project_id}/env",
                json_body=payload,
                params={"upsert": "true"},
            )
        except requests.RequestException as e:
            return False, f"Create env failed: {e}"

    if 200 <= r.status_code < 300:
        return True, "ok"
    return False, f"HTTP {r.status_code} — {r.text[:300]}"


def _vercel_trigger_redeploy() -> tuple[bool, str]:
    """Redeploy the latest production deployment so the new env var
    actually takes effect."""
    creds = _vercel_creds()
    if creds is None:
        return False, "Vercel credentials not configured."
    _, project_id, _, _ = creds

    # Find the most recent production deployment so we can redeploy it.
    try:
        listing = _vercel_api(
            "GET",
            "/v6/deployments",
            params={"projectId": project_id, "limit": 1, "target": "production"},
        )
    except requests.RequestException as e:
        return False, f"List deployments failed: {e}"
    if listing.status_code != 200:
        return False, f"List deployments failed: HTTP {listing.status_code}"

    deps = listing.json().get("deployments") or []
    if not deps:
        return False, "No production deployments found yet — push to Git first."
    latest = deps[0]

    name = latest.get("name") or "admin-panel"
    body = {
        "name": name,
        "deploymentId": latest.get("uid"),
        "target": "production",
    }
    try:
        r = _vercel_api("POST", "/v13/deployments", json_body=body)
    except requests.RequestException as e:
        return False, f"Redeploy request failed: {e}"

    if 200 <= r.status_code < 300:
        data = r.json()
        return True, data.get("url") or "redeploy queued"
    return False, f"HTTP {r.status_code} — {r.text[:300]}"


@app.route("/api/config", methods=["GET"])
def api_config_get():
    _maybe_reload_config()
    if not _check_password():
        return _unauthorized()

    creds = _vercel_creds()
    return jsonify(
        {
            "status": "success",
            "config": _redact_config(CONFIG),
            "source": "env" if (
                os.environ.get("ADMIN_CONFIG_JSON")
                or os.environ.get("ADMIN_CONFIG_B64")
            ) else "file",
            "vercelSync": creds is not None,
            "missingVercelKeys": [
                k for k in ("VERCEL_TOKEN", "VERCEL_PROJECT_ID")
                if not os.environ.get(k)
            ],
        }
    ), 200


@app.route("/api/config", methods=["POST"])
def api_config_save():
    _maybe_reload_config()
    if not _check_password():
        return _unauthorized()

    body = request.get_json(silent=True) or {}
    incoming = body.get("config")
    if not isinstance(incoming, dict):
        return jsonify({"status": "error", "message": "Missing `config` object."}), 400

    # Replace mask values with originals before validating / saving.
    merged = _merge_unmasked(CONFIG if isinstance(CONFIG, dict) else {}, incoming)
    ok, msg = _validate_config(merged)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400

    serialized = json.dumps(merged, separators=(",", ":"))

    creds = _vercel_creds()
    if creds is not None:
        # Push to Vercel as ADMIN_CONFIG_JSON, then trigger redeploy.
        ok_env, env_msg = _vercel_upsert_env("ADMIN_CONFIG_JSON", serialized)
        if not ok_env:
            return jsonify(
                {
                    "status": "error",
                    "message": f"Failed to update Vercel env: {env_msg}",
                }
            ), 502

        ok_dep, dep_msg = _vercel_trigger_redeploy()
        if not ok_dep:
            # Env var was saved, but redeploy didn't queue. The user can
            # still trigger a redeploy manually.
            return jsonify(
                {
                    "status": "partial",
                    "message": (
                        "Config saved to Vercel but redeploy failed: "
                        + dep_msg
                        + ". Trigger a redeploy manually to apply changes."
                    ),
                }
            ), 200

        return jsonify(
            {
                "status": "success",
                "message": "Config saved. New deployment queued — refresh in ~30s.",
                "deployment": dep_msg,
                "panels": len(merged.get("panels") or []),
            }
        ), 200

    # No Vercel creds → write to disk (works on local / VPS / Docker).
    try:
        CONFIG_PATH.write_text(
            json.dumps(merged, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        return jsonify(
            {
                "status": "error",
                "message": (
                    f"Failed to write {CONFIG_PATH.name}: {e}. "
                    "On serverless hosts the filesystem is read-only — "
                    "set VERCEL_TOKEN + VERCEL_PROJECT_ID to enable env-var sync."
                ),
            }
        ), 500

    # Hot-reload picks it up next request anyway, but update in-memory now.
    global CONFIG
    CONFIG = merged
    return jsonify(
        {
            "status": "success",
            "message": "Config saved to disk.",
            "panels": len(merged.get("panels") or []),
        }
    ), 200


# ---------------------------------------------------------------------------
# Local entrypoint (ignored by Vercel; Vercel imports `app` directly).
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
            "the ADMIN_PASSWORD env var.",
            file=sys.stderr,
        )

    print(f"[admin-panel] listening on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
