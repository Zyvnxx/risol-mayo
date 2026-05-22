# Admin Panel — standalone Pterodactyl multi-server controller.
# Single-file Flask app for Vercel + local hosting.
#
# Local:
#   pip install -r requirements.txt
#   ADMIN_PASSWORD=secret ADMIN_CONFIG_JSON='{"panels":[...]}' python index.py
#
# Vercel:
#   Set environment variables in the project settings:
#     ADMIN_PASSWORD, ADMIN_CONFIG_JSON
#   Optional (enables in-browser config editor):
#     VERCEL_TOKEN, VERCEL_PROJECT_ID, VERCEL_TEAM_ID

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

import requests
from flask import Flask, jsonify, request, send_from_directory


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ADMIN_CONFIG") or (BASE_DIR / "config.json"))

PTERO_TIMEOUT = float(os.environ.get("PTERO_TIMEOUT") or 5)
PTERO_PARALLEL = int(os.environ.get("PTERO_PARALLEL") or 8)

ALLOWED_SIGNALS = ("start", "stop", "restart", "kill")
API_KEY_MASK = "********"

STATIC_FILES = {
    "app.js":   "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}


# ---------------------------------------------------------------------------
# Asset lookup — handles Vercel's various unpack layouts
# ---------------------------------------------------------------------------
def _candidate_dirs():
    seen = set()
    out = []
    for cand in (BASE_DIR, BASE_DIR.parent, Path.cwd(), Path("/var/task")):
        try:
            r = cand.resolve()
        except Exception:
            continue
        if r in seen or not r.exists():
            continue
        seen.add(r)
        out.append(r)
    return out


def _find_asset(filename):
    for root in _candidate_dirs():
        p = root / filename
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _load_config():
    raw = os.environ.get("ADMIN_CONFIG_JSON")
    if not raw:
        b64 = os.environ.get("ADMIN_CONFIG_B64")
        if b64:
            try:
                raw = base64.b64decode(b64).decode("utf-8")
            except Exception as e:
                print("[admin-panel] B64 decode failed:", e, file=sys.stderr)
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            print("[admin-panel] JSON parse failed:", e, file=sys.stderr)
            return {"panels": [], "_load_error": str(e)}

    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("[admin-panel] Failed to read config.json:", e, file=sys.stderr)

    return {"panels": []}


CONFIG = _load_config()


def _maybe_reload_config():
    global CONFIG
    if os.environ.get("ADMIN_CONFIG_JSON") or os.environ.get("ADMIN_CONFIG_B64"):
        return
    if not CONFIG_PATH.exists():
        return
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            CONFIG = json.load(f)
    except Exception as e:
        print("[admin-panel] reload failed:", e, file=sys.stderr)


def _resolved_password():
    return os.environ.get("ADMIN_PASSWORD") or str(CONFIG.get("password") or "")


def _check_password():
    expected = _resolved_password()
    if not expected:
        return False
    given = request.headers.get("password") or ""
    return secrets.compare_digest(str(given), str(expected))


def _unauthorized():
    return jsonify({"status": "error", "message": "Unauthorized"}), 401


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
def _collect_panels():
    out = []
    raw = CONFIG.get("panels")
    if not isinstance(raw, list):
        return out
    for idx, p in enumerate(raw):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or ("panel-" + str(idx + 1))).strip() or ("panel-" + str(idx + 1))
        out.append({
            "id": pid,
            "name": str(p.get("name") or pid),
            "panelUrl": str(p.get("panelUrl") or "").rstrip("/"),
            "serverId": str(p.get("serverId") or "").strip(),
            "clientApiKey": str(p.get("clientApiKey") or "").strip(),
        })
    return out


def _ptero_resources(panel):
    url = "{}/api/client/servers/{}/resources".format(panel["panelUrl"], panel["serverId"])
    headers = {
        "Authorization": "Bearer " + panel["clientApiKey"],
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=PTERO_TIMEOUT)
    except requests.RequestException as e:
        return {"reachable": False, "error": str(e)}

    if not (200 <= r.status_code < 300):
        return {"reachable": False, "error": "HTTP " + str(r.status_code)}

    try:
        data = (r.json() or {}).get("attributes") or {}
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


def _safe_view(panel, with_status):
    out = {
        "id": panel["id"],
        "name": panel["name"],
        "panelUrl": panel["panelUrl"],
        "serverId": panel["serverId"],
        "configured": bool(panel["panelUrl"] and panel["serverId"] and panel["clientApiKey"]),
    }
    if with_status and out["configured"]:
        out["status"] = _ptero_resources(panel)
    elif with_status:
        out["status"] = {"reachable": False, "error": "Panel not fully configured."}
    return out


# ---------------------------------------------------------------------------
# Config redact / merge / validate
# ---------------------------------------------------------------------------
def _redact_config(cfg):
    out = deepcopy(cfg) if isinstance(cfg, dict) else {}
    if isinstance(out.get("password"), str) and out["password"]:
        out["password"] = API_KEY_MASK
    panels = out.get("panels")
    if isinstance(panels, list):
        for p in panels:
            if isinstance(p, dict) and p.get("clientApiKey"):
                p["clientApiKey"] = API_KEY_MASK
    return out


def _merge_unmasked(original, incoming):
    if not isinstance(incoming, dict) or not isinstance(original, dict):
        return incoming
    out = {}
    for k, v in incoming.items():
        if v == API_KEY_MASK and k in original:
            out[k] = original[k]
        elif isinstance(v, list):
            orig_list = original.get(k) if isinstance(original.get(k), list) else []
            orig_by_id = {}
            for item in orig_list:
                if isinstance(item, dict) and item.get("id") is not None:
                    orig_by_id[str(item["id"])] = item
            new_list = []
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


def _validate_config(cfg):
    if not isinstance(cfg, dict):
        return False, "Config must be an object."
    panels = cfg.get("panels")
    if panels is None:
        return True, ""
    if not isinstance(panels, list):
        return False, "`panels` must be an array."
    seen = set()
    for i, p in enumerate(panels):
        if not isinstance(p, dict):
            return False, "panels[" + str(i) + "] must be an object."
        pid = str(p.get("id") or "").strip()
        if not pid:
            return False, "panels[" + str(i) + "].id is required."
        if pid in seen:
            return False, "Duplicate panel id: " + pid
        seen.add(pid)
    return True, ""


# ---------------------------------------------------------------------------
# Vercel API integration
# ---------------------------------------------------------------------------
def _vercel_creds():
    token = os.environ.get("VERCEL_TOKEN")
    pid = os.environ.get("VERCEL_PROJECT_ID")
    if not token or not pid:
        return None
    team = os.environ.get("VERCEL_TEAM_ID") or None
    targets_raw = os.environ.get("VERCEL_ENV_TARGET", "production,preview,development")
    targets = [t.strip() for t in targets_raw.split(",") if t.strip()]
    return token, pid, team, targets


def _vercel_api(method, path, json_body=None, params=None):
    creds = _vercel_creds()
    if creds is None:
        raise RuntimeError("Vercel creds missing")
    token, _, team, _ = creds
    if team:
        params = dict(params or {})
        params.setdefault("teamId", team)
    return requests.request(
        method,
        "https://api.vercel.com" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        params=params,
        json=json_body,
        timeout=8,
    )


def _vercel_upsert_env(key, value):
    creds = _vercel_creds()
    if creds is None:
        return False, "Vercel creds missing"
    _, pid, _, targets = creds
    try:
        listing = _vercel_api("GET", "/v9/projects/" + pid + "/env", params={"decrypt": "false"})
    except requests.RequestException as e:
        return False, "List env failed: " + str(e)
    if listing.status_code != 200:
        return False, "List env HTTP " + str(listing.status_code) + ": " + listing.text[:200]

    existing = None
    for e in (listing.json() or {}).get("envs") or []:
        if e.get("key") == key:
            existing = e
            break

    if existing:
        try:
            r = _vercel_api(
                "PATCH",
                "/v9/projects/" + pid + "/env/" + existing["id"],
                json_body={"value": value, "target": targets, "type": "encrypted"},
            )
        except requests.RequestException as e:
            return False, "Update env failed: " + str(e)
    else:
        try:
            r = _vercel_api(
                "POST",
                "/v10/projects/" + pid + "/env",
                json_body={"key": key, "value": value, "type": "encrypted", "target": targets},
                params={"upsert": "true"},
            )
        except requests.RequestException as e:
            return False, "Create env failed: " + str(e)

    if 200 <= r.status_code < 300:
        return True, "ok"
    return False, "HTTP " + str(r.status_code) + ": " + r.text[:300]


def _vercel_trigger_redeploy():
    creds = _vercel_creds()
    if creds is None:
        return False, "Vercel creds missing"
    _, pid, _, _ = creds
    try:
        listing = _vercel_api(
            "GET", "/v6/deployments",
            params={"projectId": pid, "limit": 1, "target": "production"},
        )
    except requests.RequestException as e:
        return False, "List deployments failed: " + str(e)
    if listing.status_code != 200:
        return False, "List HTTP " + str(listing.status_code)

    deps = (listing.json() or {}).get("deployments") or []
    if not deps:
        return False, "No production deployments yet."
    latest = deps[0]
    body = {
        "name": latest.get("name") or "admin-panel",
        "deploymentId": latest.get("uid"),
        "target": "production",
    }
    try:
        r = _vercel_api("POST", "/v13/deployments", json_body=body)
    except requests.RequestException as e:
        return False, "Redeploy failed: " + str(e)
    if 200 <= r.status_code < 300:
        d = r.json() or {}
        return True, d.get("url") or "queued"
    return False, "HTTP " + str(r.status_code) + ": " + r.text[:300]


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)


@app.errorhandler(Exception)
def _on_exception(err):
    tb = traceback.format_exc()
    print("[admin-panel] exception:\n" + tb, file=sys.stderr)
    payload = {"status": "error", "message": type(err).__name__ + ": " + str(err)}
    try:
        if _check_password():
            payload["traceback"] = tb.splitlines()[-15:]
    except Exception:
        pass
    return jsonify(payload), 500


@app.route("/")
def home():
    target = _find_asset("index.html")
    if target is None:
        return ("index.html not found in lambda. Visit /_debug.", 500)
    return send_from_directory(str(target.parent), target.name)


@app.route("/<path:filename>")
def root_file(filename):
    if filename in STATIC_FILES:
        target = _find_asset(filename)
        if target is None:
            return ("", 404)
        return send_from_directory(str(target.parent), target.name, mimetype=STATIC_FILES[filename])
    if filename in ("favicon.ico", "favicon.png"):
        target = _find_asset(filename)
        if target is None:
            return ("", 204)
        return send_from_directory(str(target.parent), target.name)
    return ("", 404)


@app.route("/_debug")
def debug_info():
    dirs = []
    for d in _candidate_dirs():
        try:
            files = sorted([p.name for p in d.iterdir() if p.is_file()])[:30]
        except Exception:
            files = []
        dirs.append({"path": str(d), "files": files})
    return jsonify({
        "base_dir": str(BASE_DIR),
        "cwd": str(Path.cwd()),
        "lookup_dirs": dirs,
        "found": {
            n: (str(_find_asset(n)) if _find_asset(n) else None)
            for n in ("index.html", "app.js", "style.css")
        },
        "env_set": {
            k: bool(os.environ.get(k))
            for k in ("ADMIN_PASSWORD", "ADMIN_CONFIG_JSON", "ADMIN_CONFIG_B64",
                      "VERCEL_TOKEN", "VERCEL_PROJECT_ID", "VERCEL_TEAM_ID")
        },
        "vercel_sync_enabled": _vercel_creds() is not None,
        "config_panels": len(_collect_panels()),
        "config_load_error": CONFIG.get("_load_error"),
    }), 200


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
        workers = max(1, min(PTERO_PARALLEL, len(panels)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            public = list(ex.map(lambda p: _safe_view(p, True), panels))
    else:
        public = [_safe_view(p, False) for p in panels]
    return jsonify({
        "status": "success",
        "panels": public,
        "count": len(public),
        "ts": int(time.time()),
    }), 200


@app.route("/api/panels/power", methods=["POST"])
def api_panels_power():
    _maybe_reload_config()
    if not _check_password():
        return _unauthorized()
    body = request.get_json(silent=True) or {}
    panel_id = str(body.get("id") or "").strip()
    signal = str(body.get("signal") or "").lower()
    if signal not in ALLOWED_SIGNALS:
        return jsonify({"status": "error", "message": "Invalid signal."}), 400

    panel = next((p for p in _collect_panels() if p["id"] == panel_id), None)
    if panel is None:
        return jsonify({"status": "error", "message": "Unknown panel id."}), 404
    if not (panel["panelUrl"] and panel["serverId"] and panel["clientApiKey"]):
        return jsonify({"status": "error", "message": "Panel not configured."}), 400

    target = "{}/api/client/servers/{}/power".format(panel["panelUrl"], panel["serverId"])
    try:
        resp = requests.post(
            target,
            headers={
                "Authorization": "Bearer " + panel["clientApiKey"],
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"signal": signal},
            timeout=PTERO_TIMEOUT,
        )
    except requests.RequestException as e:
        return jsonify({"status": "error", "message": "Pterodactyl request failed: " + str(e)}), 502

    if 200 <= resp.status_code < 300:
        return jsonify({
            "status": "success",
            "id": panel_id,
            "name": panel["name"],
            "signal": signal,
        }), 200
    return jsonify({
        "status": "error",
        "message": "Panel rejected (" + str(resp.status_code) + "): " + resp.text[:200],
    }), 502


@app.route("/api/config", methods=["GET"])
def api_config_get():
    _maybe_reload_config()
    if not _check_password():
        return _unauthorized()
    return jsonify({
        "status": "success",
        "config": _redact_config(CONFIG),
        "source": "env" if (os.environ.get("ADMIN_CONFIG_JSON") or os.environ.get("ADMIN_CONFIG_B64")) else "file",
        "vercelSync": _vercel_creds() is not None,
        "missingVercelKeys": [
            k for k in ("VERCEL_TOKEN", "VERCEL_PROJECT_ID")
            if not os.environ.get(k)
        ],
    }), 200


@app.route("/api/config", methods=["POST"])
def api_config_save():
    _maybe_reload_config()
    if not _check_password():
        return _unauthorized()

    body = request.get_json(silent=True) or {}
    incoming = body.get("config")
    if not isinstance(incoming, dict):
        return jsonify({"status": "error", "message": "Missing `config` object."}), 400

    merged = _merge_unmasked(CONFIG if isinstance(CONFIG, dict) else {}, incoming)
    ok, msg = _validate_config(merged)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400

    serialized = json.dumps(merged, separators=(",", ":"))

    if _vercel_creds() is not None:
        ok_env, env_msg = _vercel_upsert_env("ADMIN_CONFIG_JSON", serialized)
        if not ok_env:
            return jsonify({"status": "error", "message": "Vercel env update failed: " + env_msg}), 502
        ok_dep, dep_msg = _vercel_trigger_redeploy()
        if not ok_dep:
            return jsonify({
                "status": "partial",
                "message": "Saved but redeploy failed: " + dep_msg,
            }), 200
        return jsonify({
            "status": "success",
            "message": "Saved. Redeploy queued — refresh in ~30s.",
            "deployment": dep_msg,
        }), 200

    # Disk fallback (local / VPS).
    try:
        CONFIG_PATH.write_text(json.dumps(merged, indent=4, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return jsonify({
            "status": "error",
            "message": "Could not write " + CONFIG_PATH.name + ": " + str(e)
                       + ". On serverless hosts, set VERCEL_TOKEN + VERCEL_PROJECT_ID.",
        }), 500
    global CONFIG
    CONFIG = merged
    return jsonify({"status": "success", "message": "Config saved to disk."}), 200


# ---------------------------------------------------------------------------
# Local entrypoint (Vercel imports `app` directly).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("ADMIN_PORT") or CONFIG.get("port") or 7860)
    host = os.environ.get("ADMIN_HOST") or str(CONFIG.get("host") or "0.0.0.0")
    if not _resolved_password():
        print("[admin-panel] WARN: no ADMIN_PASSWORD set", file=sys.stderr)
    print("[admin-panel] http://{}:{}".format(host, port))
    app.run(host=host, port=port, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Vercel WSGI entry-points.
# Vercel's @vercel/python statically scans this file for one of these
# names, so expose all three as aliases of the same Flask app.
# ---------------------------------------------------------------------------
application = app
handler = app
