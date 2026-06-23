# Admin Panel — standalone Pterodactyl multi-server controller.
#
# Single-file Flask app for Vercel + local hosting. The module is
# deliberately written so that importing it never executes I/O or
# network calls — config loading and asset lookup happen lazily at
# request time. That way an environment misconfiguration (bad JSON in
# ADMIN_CONFIG_JSON, missing files, etc.) returns a real HTTP error
# instead of a Vercel "could not import" crash page.
#
# Local:
#   pip install -r requirements.txt
#   ADMIN_PASSWORD=secret python index.py
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
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory


# ---------------------------------------------------------------------------
# Constants — pure values, no I/O
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

PTERO_TIMEOUT = float(os.environ.get("PTERO_TIMEOUT") or 15.0)
PTERO_PARALLEL = int(os.environ.get("PTERO_PARALLEL") or 8)

# How long a successful /resources result stays fresh (seconds). Within
# this window the cached status is served without hitting Pterodactyl.
PTERO_CACHE_TTL = float(os.environ.get("PTERO_CACHE_TTL") or 10.0)

# tokens.txt changes rarely, so it can stay fresh much longer than live
# stats. This keeps the customer auto-refresh from re-reading the file
# from the Pterodactyl server every cycle.
TOKENS_CACHE_TTL = float(os.environ.get("TOKENS_CACHE_TTL") or 60.0)

ALLOWED_SIGNALS = ("start", "stop", "restart", "kill")
API_KEY_MASK = "********"

STATIC_FILES = {
    "app.js":       "application/javascript; charset=utf-8",
    "style.css":    "text/css; charset=utf-8",
    "customer.js":  "application/javascript; charset=utf-8",
    "customer.css": "text/css; charset=utf-8",
    "logo.png":     "image/png",
}


# ---------------------------------------------------------------------------
# Lazy state — populated on first request, never at import time
# ---------------------------------------------------------------------------
_STATE = {
    "config": None,         # dict, parsed from env / file
    "config_loaded": False, # was a load attempted?
    "config_error": None,   # str if load failed
}

# Per-panel status cache: {panel_id: {"result": dict, "ts": float}}.
# Guarded by _STATUS_LOCK so the thread pool can write to it safely.
_STATUS_CACHE = {}
_STATUS_LOCK = threading.Lock()

# Per-panel tokens.txt cache: {panel_id: {"lines": list, "ts": float}}.
# Shares _STATUS_LOCK — both are small and rarely contended.
_TOKENS_CACHE = {}

# Single shared thread pool — created once, reused across requests instead
# of spinning up a new pool (and its OS threads) on every /api/panels hit.
_EXECUTOR = ThreadPoolExecutor(max_workers=PTERO_PARALLEL)


def _config_path():
    return Path(os.environ.get("ADMIN_CONFIG") or (BASE_DIR / "config.json"))


def _tokens_path():
    """Locate the bot's tokens.txt.

    Defaults to the file in the repo root (one level above the
    admin-panel folder). Override with the TOKENS_FILE env var."""
    env = os.environ.get("TOKENS_FILE")
    if env:
        return Path(env)
    for root in (BASE_DIR.parent, BASE_DIR, Path.cwd()):
        try:
            cand = (root / "tokens.txt").resolve()
        except Exception:
            continue
        if cand.is_file():
            return cand
    return (BASE_DIR.parent / "tokens.txt")


def _read_token_lines():
    """Return the raw lines of tokens.txt."""
    p = _tokens_path()
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    return text.splitlines()


def _parse_token_line(line):
    """Split a tokens.txt line into (token, channelId)."""
    parts = (line or "").strip().split()
    token = parts[0] if len(parts) >= 1 else ""
    channel = parts[1] if len(parts) >= 2 else ""
    return token, channel


def _mask_bot_token(token):
    """Mask bot token showing only first 10 and last 4 chars."""
    t = str(token or "")
    if len(t) <= 16:
        return API_KEY_MASK if t else ""
    return t[:10] + "…" + t[-4:]


def _write_token_line(line_no, token, channel):
    """Replace the 1-based line_no in tokens.txt. Returns (ok, message)."""
    if not isinstance(line_no, int) or line_no < 1:
        return False, "Invalid token line."
    token = str(token or "").strip()
    channel = str(channel or "").strip()
    if not token:
        return False, "Token is required."
    if " " in token or " " in channel:
        return False, "Token / channel id cannot contain spaces."

    lines = _read_token_lines()
    while len(lines) < line_no:
        lines.append("")
    lines[line_no - 1] = (token + " " + channel).strip()

    p = _tokens_path()
    try:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return False, "Could not write tokens.txt: " + str(e)
    return True, "ok"


def _tokens_path():
    """Locate the bot's tokens.txt.

    Defaults to the file in the repo root (one level above the
    admin-panel folder). Override with the TOKENS_FILE env var when the
    bot lives elsewhere."""
    env = os.environ.get("TOKENS_FILE")
    if env:
        return Path(env)
    # Prefer an existing tokens.txt in any sensible candidate dir.
    for root in (BASE_DIR.parent, BASE_DIR, Path.cwd()):
        try:
            cand = (root / "tokens.txt").resolve()
        except Exception:
            continue
        if cand.is_file():
            return cand
    return (BASE_DIR.parent / "tokens.txt")


def _read_token_lines():
    """Return the raw lines of tokens.txt (without trailing newlines).

    Missing file yields an empty list rather than raising."""
    p = _tokens_path()
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    # Keep all lines; splitlines() drops the trailing newline cleanly.
    return text.splitlines()


def _parse_token_line(line):
    """Split a tokens.txt line into (token, channelId).

    The bot reads each line as ``line.strip().split()`` where the first
    field is the token and the second is the channel id."""
    parts = (line or "").strip().split()
    token = parts[0] if len(parts) >= 1 else ""
    channel = parts[1] if len(parts) >= 2 else ""
    return token, channel


def _mask_token(token):
    """Show only the first 6 and last 4 chars so a customer can verify
    their token without it being fully exposed in the browser."""
    t = str(token or "")
    if len(t) <= 12:
        return API_KEY_MASK if t else ""
    return t[:6] + "…" + t[-4:]


def _write_token_line(line_no, token, channel):
    """Replace the 1-based ``line_no`` in tokens.txt with ``token
    channel``. Pads the file with blank lines if it is shorter. Returns
    (ok, message)."""
    if not isinstance(line_no, int) or line_no < 1:
        return False, "Invalid token line."
    token = str(token or "").strip()
    channel = str(channel or "").strip()
    if not token:
        return False, "Token is required."
    if " " in token or " " in channel:
        return False, "Token / channel id cannot contain spaces."

    lines = _read_token_lines()
    # Grow the list so index line_no-1 exists.
    while len(lines) < line_no:
        lines.append("")
    lines[line_no - 1] = (token + " " + channel).strip()

    p = _tokens_path()
    try:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return False, "Could not write tokens.txt: " + str(e)
    return True, "ok"


def _load_config_now():
    """Parse ADMIN_CONFIG_JSON / B64 / config.json. Always returns a
    dict. Records any error in _STATE['config_error']."""
    _STATE["config_error"] = None

    raw = os.environ.get("ADMIN_CONFIG_JSON")
    if not raw:
        b64 = os.environ.get("ADMIN_CONFIG_B64")
        if b64:
            try:
                raw = base64.b64decode(b64).decode("utf-8")
            except Exception as e:
                _STATE["config_error"] = "ADMIN_CONFIG_B64 decode failed: " + str(e)
                return {"panels": []}

    if raw:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"panels": []}
        except Exception as e:
            _STATE["config_error"] = "ADMIN_CONFIG_JSON parse failed: " + str(e)
            return {"panels": []}

    cp = _config_path()
    if cp.exists():
        try:
            with cp.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {"panels": []}
        except Exception as e:
            _STATE["config_error"] = "config.json read failed: " + str(e)

    return {"panels": []}


def _get_config():
    """Cached config getter. Reloads if config.json mtime changes
    (only meaningful in non-serverless environments)."""
    if not _STATE["config_loaded"]:
        _STATE["config"] = _load_config_now()
        _STATE["config_loaded"] = True
    return _STATE["config"]


def _set_config(new_cfg):
    _STATE["config"] = new_cfg
    _STATE["config_loaded"] = True


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


def _resolved_password():
    return os.environ.get("ADMIN_PASSWORD") or str(_get_config().get("password") or "")


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
    raw = _get_config().get("panels")
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
            "customerToken": str(p.get("customerToken") or "").strip(),
            "expiresAt": p.get("expiresAt"),  # ISO date string e.g. "2025-07-15"
            "tokenLine": p.get("tokenLine"),  # 1-based line number in tokens.txt
            "tokensPath": str(p.get("tokensPath") or "tokens.txt").strip() or "tokens.txt",
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
    }


def _ptero_resources_cached(panel, ttl=None):
    """Cached wrapper around _ptero_resources.

    Returns a fresh result if the cached one is older than ``ttl`` seconds
    (defaults to PTERO_CACHE_TTL). Successful reads are cached; failures are
    also cached but with a shorter implicit life since the next request will
    retry once TTL passes. This keeps one slow/unreachable panel from being
    re-hit on every refresh within the window."""
    if ttl is None:
        ttl = PTERO_CACHE_TTL
    pid = panel["id"]
    now = time.time()
    with _STATUS_LOCK:
        entry = _STATUS_CACHE.get(pid)
        if entry and (now - entry["ts"]) < ttl:
            return entry["result"]

    result = _ptero_resources(panel)
    with _STATUS_LOCK:
        _STATUS_CACHE[pid] = {"result": result, "ts": time.time()}
    return result


def _invalidate_status(panel_id):
    """Drop the cached status for a panel so the next read is fresh.

    Called after a power action so the UI reflects the new state quickly
    instead of waiting for the TTL to lapse."""
    with _STATUS_LOCK:
        _STATUS_CACHE.pop(panel_id, None)


def _safe_view(panel, with_status):
    out = {
        "id": panel["id"],
        "name": panel["name"],
        "panelUrl": panel["panelUrl"],
        "serverId": panel["serverId"],
        "configured": bool(panel["panelUrl"] and panel["serverId"] and panel["clientApiKey"]),
    }
    if with_status and out["configured"]:
        out["status"] = _ptero_resources_cached(panel)
    elif with_status:
        out["status"] = {"reachable": False, "error": "Panel not fully configured."}
    return out


# ---------------------------------------------------------------------------
# Pterodactyl file access — read/write the bot's tokens.txt on the actual
# game server through the Client API, so it works no matter where this
# admin panel is hosted (Vercel, etc.).
# ---------------------------------------------------------------------------
def _ptero_token_file(panel):
    """Configured path of tokens.txt inside the server, leading-slash form."""
    path = str(panel.get("tokensPath") or "tokens.txt").strip() or "tokens.txt"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _effective_token_line(panel):
    """1-based tokens.txt line this panel's customer manages.

    Falls back to line 1 when the admin has not pinned a specific line,
    so the editor is always usable by the customer."""
    raw = panel.get("tokenLine")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def _ptero_read_token_lines(panel):
    """Read tokens.txt from the Pterodactyl server. Returns (lines, error).

    ``lines`` is a list of strings (no trailing newlines); ``error`` is
    None on success or a human-readable message on failure."""
    if not (panel.get("panelUrl") and panel.get("serverId") and panel.get("clientApiKey")):
        return [], "Panel not configured."
    url = "{}/api/client/servers/{}/files/contents".format(panel["panelUrl"], panel["serverId"])
    try:
        r = requests.get(
            url,
            headers={
                "Authorization": "Bearer " + panel["clientApiKey"],
                "Accept": "application/json",
            },
            params={"file": _ptero_token_file(panel)},
            timeout=PTERO_TIMEOUT,
        )
    except requests.RequestException as e:
        return [], "Read failed: " + str(e)
    if r.status_code == 404:
        # File doesn't exist yet — treat as empty so it can be created.
        return [], None
    if not (200 <= r.status_code < 300):
        return [], "Panel returned HTTP " + str(r.status_code)
    return r.text.splitlines(), None


def _ptero_read_token_lines_cached(panel, ttl=None):
    """Cached wrapper around _ptero_read_token_lines.

    tokens.txt changes only when the customer saves it, so re-reading it
    from the Pterodactyl server on every 15s status refresh is wasteful.
    Successful reads are cached for ``ttl`` seconds (TOKENS_CACHE_TTL).
    Errors are NOT cached so a transient failure retries next time."""
    if ttl is None:
        ttl = TOKENS_CACHE_TTL
    pid = panel["id"]
    now = time.time()
    with _STATUS_LOCK:
        entry = _TOKENS_CACHE.get(pid)
        if entry and (now - entry["ts"]) < ttl:
            return entry["lines"], None

    lines, err = _ptero_read_token_lines(panel)
    if err is None:
        with _STATUS_LOCK:
            _TOKENS_CACHE[pid] = {"lines": lines, "ts": time.time()}
    return lines, err


def _invalidate_tokens(panel_id):
    """Drop the cached tokens.txt for a panel so the next read is fresh.

    Called after a customer writes new tokens so the editor reflects the
    saved content immediately."""
    with _STATUS_LOCK:
        _TOKENS_CACHE.pop(panel_id, None)


def _ptero_write_token_lines(panel, lines):
    """Write the given lines back to tokens.txt on the Pterodactyl server.

    Returns (ok, message)."""
    if not (panel.get("panelUrl") and panel.get("serverId") and panel.get("clientApiKey")):
        return False, "Panel not configured."
    url = "{}/api/client/servers/{}/files/write".format(panel["panelUrl"], panel["serverId"])
    content = "\n".join(lines) + "\n"
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": "Bearer " + panel["clientApiKey"],
                "Accept": "application/json",
                "Content-Type": "text/plain",
            },
            params={"file": _ptero_token_file(panel)},
            data=content.encode("utf-8"),
            timeout=PTERO_TIMEOUT,
        )
    except requests.RequestException as e:
        return False, "Write failed: " + str(e)
    if 200 <= r.status_code < 300:
        return True, "ok"
    return False, "Panel returned HTTP " + str(r.status_code) + ": " + r.text[:200]


def _normalize_server_path(path):
    """Normalise an arbitrary destination path on the Pterodactyl server.

    Returns a leading-slash, single-line path with surrounding whitespace
    stripped. Blocks parent-directory traversal so an upload can never
    escape the server's data root."""
    p = str(path or "").strip().replace("\\", "/")
    if not p:
        return None, "Destination path is required."
    # Collapse duplicate slashes and resolve away any "." segments.
    parts = []
    for seg in p.split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            return None, "Path traversal ('..') is not allowed."
        parts.append(seg)
    if not parts:
        return None, "Destination path is required."
    return "/" + "/".join(parts), None


def _ptero_write_file(panel, dest_path, raw_bytes):
    """Write raw bytes to ``dest_path`` on a panel's Pterodactyl server.

    Uses the Client API ``files/write`` endpoint which creates parent
    directories and overwrites any existing file. Returns (ok, message)."""
    if not (panel.get("panelUrl") and panel.get("serverId") and panel.get("clientApiKey")):
        return False, "Panel not configured."
    url = "{}/api/client/servers/{}/files/write".format(panel["panelUrl"], panel["serverId"])
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": "Bearer " + panel["clientApiKey"],
                "Accept": "application/json",
                "Content-Type": "application/octet-stream",
            },
            params={"file": dest_path},
            data=raw_bytes,
            timeout=PTERO_TIMEOUT,
        )
    except requests.RequestException as e:
        return False, "Write failed: " + str(e)
    if 200 <= r.status_code < 300:
        return True, "ok"
    return False, "HTTP " + str(r.status_code) + ": " + r.text[:200]


def _split_root_file(dest_path):
    """Split a leading-slash path into (root_dir, filename) for the
    Pterodactyl decompress API, which wants the directory the archive
    lives in plus the archive's own name."""
    p = dest_path if dest_path.startswith("/") else "/" + dest_path
    idx = p.rstrip("/").rfind("/")
    if idx <= 0:
        return "/", p.strip("/")
    return p[:idx], p[idx + 1:]


def _ptero_decompress(panel, dest_path):
    """Decompress an archive already uploaded to ``dest_path`` on the
    server, extracting it into the same directory. Mirrors Pterodactyl's
    "Unarchive" action via the Client API. Returns (ok, message)."""
    if not (panel.get("panelUrl") and panel.get("serverId") and panel.get("clientApiKey")):
        return False, "Panel not configured."
    root, fname = _split_root_file(dest_path)
    url = "{}/api/client/servers/{}/files/decompress".format(panel["panelUrl"], panel["serverId"])
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": "Bearer " + panel["clientApiKey"],
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"root": root, "file": fname},
            # Extraction can take longer than a simple write, so allow more time.
            timeout=max(PTERO_TIMEOUT, 60.0),
        )
    except requests.RequestException as e:
        return False, "Decompress failed: " + str(e)
    if 200 <= r.status_code < 300:
        return True, "ok"
    return False, "Decompress HTTP " + str(r.status_code) + ": " + r.text[:200]


def _ptero_delete_file(panel, dest_path):
    """Delete a single file at ``dest_path`` via the Client API. Used to
    clean up an archive after it has been extracted. Returns (ok, message)."""
    if not (panel.get("panelUrl") and panel.get("serverId") and panel.get("clientApiKey")):
        return False, "Panel not configured."
    root, fname = _split_root_file(dest_path)
    url = "{}/api/client/servers/{}/files/delete".format(panel["panelUrl"], panel["serverId"])
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": "Bearer " + panel["clientApiKey"],
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"root": root, "files": [fname]},
            timeout=PTERO_TIMEOUT,
        )
    except requests.RequestException as e:
        return False, "Delete failed: " + str(e)
    if 200 <= r.status_code < 300:
        return True, "ok"
    return False, "Delete HTTP " + str(r.status_code) + ": " + r.text[:200]


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
            if isinstance(p, dict):
                if p.get("clientApiKey"):
                    p["clientApiKey"] = API_KEY_MASK
                if p.get("customerToken"):
                    p["customerToken"] = API_KEY_MASK
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
        return True, (r.json() or {}).get("url") or "queued"
    return False, "HTTP " + str(r.status_code) + ": " + r.text[:300]


# ---------------------------------------------------------------------------
# Flask app — defined AFTER all helpers so static analysers find it cleanly
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
        return ("index.html not found in deployed bundle. Visit /_debug.", 500)
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
        "ok": True,
        "base_dir": str(BASE_DIR),
        "cwd": str(Path.cwd()),
        "lookup_dirs": dirs,
        "found_assets": {
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
        "config_load_error": _STATE["config_error"],
    }), 200


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "success", "ok": True}), 200


@app.route("/api/panels", methods=["GET"])
def api_panels():
    if not _check_password():
        return _unauthorized()
    with_status = request.args.get("status", "1").lower() not in ("0", "false", "no")
    panels = _collect_panels()
    if with_status and panels:
        # Reuse the shared pool instead of building a new one per request.
        public = list(_EXECUTOR.map(lambda p: _safe_view(p, True), panels))
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
        _invalidate_status(panel_id)
        return jsonify({"status": "success", "id": panel_id, "name": panel["name"], "signal": signal}), 200
    return jsonify({
        "status": "error",
        "message": "Panel rejected (" + str(resp.status_code) + "): " + resp.text[:200],
    }), 502


@app.route("/api/panels/upload", methods=["POST"])
def api_panels_upload():
    """Upload a single file to a destination path on every configured panel.

    Accepts either:
      * multipart/form-data with a ``file`` part plus form fields
        ``path``, ``decompress``, ``deleteArchive``, ``ids`` (JSON array
        string). Preferred — streams binary, no base64 bloat or UI freeze.
      * JSON: {"path", "filename", "contentB64", "ids", "decompress"} —
        kept as a fallback for older clients.

    Writes the same file to every targeted panel via the Pterodactyl
    Client API in parallel. Returns a per-panel result list so the UI can
    show which servers succeeded and which failed.

    Optional ``decompress: true`` extracts the uploaded archive (zip, tar,
    tar.gz, …) into its directory afterwards, like Pterodactyl's Unarchive
    action, then removes the archive file."""
    if not _check_password():
        return _unauthorized()

    raw_path = None
    filename = ""
    raw_bytes = None
    decompress = False
    delete_after = True
    wanted_ids = None

    upload = request.files.get("file") if request.files else None
    if upload is not None:
        # ---- multipart/form-data path (binary, preferred) ----
        filename = str(upload.filename or "").strip()
        raw_bytes = upload.read()
        form = request.form
        raw_path = form.get("path")
        decompress = str(form.get("decompress") or "").lower() in ("1", "true", "yes", "on")
        delete_after = str(form.get("deleteArchive") or "true").lower() in ("1", "true", "yes", "on")
        ids_raw = form.get("ids")
        if ids_raw:
            try:
                parsed = json.loads(ids_raw)
                if isinstance(parsed, list):
                    wanted_ids = parsed
            except Exception:
                wanted_ids = None
    else:
        # ---- JSON base64 fallback ----
        body = request.get_json(silent=True) or {}
        raw_path = body.get("path")
        filename = str(body.get("filename") or "").strip()
        content_b64 = body.get("contentB64")
        decompress = bool(body.get("decompress"))
        delete_after = bool(body.get("deleteArchive", True))
        wanted_ids = body.get("ids")
        if not content_b64:
            return jsonify({"status": "error", "message": "Missing file content."}), 400
        try:
            raw_bytes = base64.b64decode(content_b64)
        except Exception as e:
            return jsonify({"status": "error", "message": "Bad base64 content: " + str(e)}), 400

    delete_after = bool(decompress and delete_after)

    if raw_bytes is None or len(raw_bytes) == 0:
        return jsonify({"status": "error", "message": "Empty or missing file."}), 400

    dest, err = _normalize_server_path(raw_path)
    if err:
        return jsonify({"status": "error", "message": err}), 400

    # If the destination looks like a directory (trailing slash on input or
    # no filename component), append the original filename.
    looks_like_dir = str(raw_path or "").rstrip().endswith("/")
    if looks_like_dir:
        if not filename:
            return jsonify({"status": "error", "message": "Path is a directory but no filename was given."}), 400
        safe_name = filename.replace("\\", "/").split("/")[-1].strip()
        if not safe_name or safe_name in (".", ".."):
            return jsonify({"status": "error", "message": "Invalid filename."}), 400
        dest = (dest.rstrip("/") + "/" + safe_name) if dest != "/" else "/" + safe_name

    # Optional subset of panels by id.
    id_filter = None
    if isinstance(wanted_ids, list) and wanted_ids:
        id_filter = {str(x) for x in wanted_ids}

    panels = _collect_panels()
    targets = []
    for p in panels:
        if id_filter is not None and p["id"] not in id_filter:
            continue
        targets.append(p)

    if not targets:
        return jsonify({"status": "error", "message": "No matching panels."}), 400

    def _do(panel):
        if not (panel["panelUrl"] and panel["serverId"] and panel["clientApiKey"]):
            return {"id": panel["id"], "name": panel["name"], "ok": False, "message": "Panel not configured."}
        ok, msg = _ptero_write_file(panel, dest, raw_bytes)
        if not ok:
            return {"id": panel["id"], "name": panel["name"], "ok": False, "message": msg}
        if not decompress:
            return {"id": panel["id"], "name": panel["name"], "ok": True, "message": "ok"}
        # Extract the archive into its directory, then optionally remove it.
        dok, dmsg = _ptero_decompress(panel, dest)
        if not dok:
            return {"id": panel["id"], "name": panel["name"], "ok": False,
                    "message": "Uploaded but extract failed: " + dmsg}
        if delete_after:
            _ptero_delete_file(panel, dest)  # best-effort cleanup; ignore result
        return {"id": panel["id"], "name": panel["name"], "ok": True, "message": "extracted"}

    results = list(_EXECUTOR.map(_do, targets))
    ok_count = sum(1 for r in results if r["ok"])

    return jsonify({
        "status": "success" if ok_count == len(results) else "partial",
        "dest": dest,
        "size": len(raw_bytes),
        "decompress": decompress,
        "okCount": ok_count,
        "total": len(results),
        "results": results,
    }), 200


@app.route("/api/panels/status", methods=["POST"])
def api_panel_status():
    """Live status for a single panel, looked up by id in the JSON body.

    Sending the id in the body (instead of the URL path) avoids edge
    routers mangling ids that contain spaces, '#', '/', etc. — which made
    the path-based route return "Unknown panel id" for those panels."""
    if not _check_password():
        return _unauthorized()
    body = request.get_json(silent=True) or {}
    pid = str(body.get("id") or "").strip()
    panel = next((p for p in _collect_panels() if p["id"] == pid), None)
    if panel is None:
        return jsonify({"status": "error", "message": "Unknown panel id."}), 404
    return jsonify({
        "status": "success",
        "panel": _safe_view(panel, True),
        "ts": int(time.time()),
    }), 200


@app.route("/api/panels/<panel_id>", methods=["GET"])
def api_panel_one(panel_id):
    """Status for a single panel.

    Lets the browser fetch each panel independently so a fast panel
    renders immediately instead of waiting for the slowest one in a
    combined response."""
    if not _check_password():
        return _unauthorized()
    pid = str(panel_id or "").strip()
    panel = next((p for p in _collect_panels() if p["id"] == pid), None)
    if panel is None:
        return jsonify({"status": "error", "message": "Unknown panel id."}), 404
    return jsonify({
        "status": "success",
        "panel": _safe_view(panel, True),
        "ts": int(time.time()),
    }), 200


@app.route("/api/config", methods=["GET"])
def api_config_get():
    if not _check_password():
        return _unauthorized()
    cfg = _get_config()
    return jsonify({
        "status": "success",
        "config": _redact_config(cfg),
        "source": "env" if (os.environ.get("ADMIN_CONFIG_JSON") or os.environ.get("ADMIN_CONFIG_B64")) else "file",
        "vercelSync": _vercel_creds() is not None,
        "missingVercelKeys": [
            k for k in ("VERCEL_TOKEN", "VERCEL_PROJECT_ID")
            if not os.environ.get(k)
        ],
    }), 200


@app.route("/api/config", methods=["POST"])
def api_config_save():
    if not _check_password():
        return _unauthorized()

    body = request.get_json(silent=True) or {}
    incoming = body.get("config")
    if not isinstance(incoming, dict):
        return jsonify({"status": "error", "message": "Missing `config` object."}), 400

    cfg = _get_config()
    merged = _merge_unmasked(cfg if isinstance(cfg, dict) else {}, incoming)
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
            return jsonify({"status": "partial", "message": "Saved but redeploy failed: " + dep_msg}), 200
        return jsonify({
            "status": "success",
            "message": "Saved. Redeploy queued — refresh in ~30s.",
            "deployment": dep_msg,
        }), 200

    try:
        _config_path().write_text(json.dumps(merged, indent=4, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return jsonify({
            "status": "error",
            "message": "Could not write config: " + str(e)
                       + ". On serverless hosts, set VERCEL_TOKEN + VERCEL_PROJECT_ID.",
        }), 500
    _set_config(merged)
    return jsonify({"status": "success", "message": "Config saved to disk."}), 200


# ---------------------------------------------------------------------------
# Customer portal — token-scoped self-service for a single panel
# ---------------------------------------------------------------------------
#
# Each panel can be assigned a ``customerToken`` (see admin Settings).
# A customer presents that token in the ``customer-token`` request
# header and only sees / controls the panel that owns the token.
# Tokens are matched in constant time to avoid leaking which token
# values exist.

def _customer_token_from_request():
    return (request.headers.get("customer-token") or "").strip()


def _resolve_customer_panel(token):
    """Find the single panel that this customer token belongs to.

    Returns the raw panel dict (with secrets) or None. Comparisons use
    secrets.compare_digest to avoid timing leaks."""
    if not token:
        return None
    for p in _collect_panels():
        ct = p.get("customerToken") or ""
        if not ct:
            continue
        # Length-equal check first to keep compare_digest happy on
        # different lengths.
        if secrets.compare_digest(str(token), str(ct)):
            return p
    return None


def _customer_safe_view(panel, with_status):
    """Public-safe view of a panel for a customer.

    The customer never sees the panel's API key, the customer token,
    or the raw panel URL (we trim it to the host so they can verify
    it's their server without learning the panel's full structure)."""
    base = _safe_view(panel, with_status=with_status)
    # Strip secrets that should never reach the customer browser.
    base.pop("panelUrl", None)
    # Add expiration info for countdown timer
    base["expiresAt"] = panel.get("expiresAt")
    # Token editor: always available for a configured panel. The customer
    # manages the whole tokens.txt for their own server — one bot per line
    # in "TOKEN CHANNELID" form. We read the current content straight from
    # the Pterodactyl server via the Client API.
    if base.get("configured"):
        base["tokensEditable"] = True
        if with_status:  # only hit the panel when a live view is requested
            lines, err = _ptero_read_token_lines_cached(panel)
            if err:
                base["tokensText"] = ""
                base["tokensError"] = err
            else:
                # Drop trailing blank lines for a cleaner textarea.
                while lines and not lines[-1].strip():
                    lines.pop()
                base["tokensText"] = "\n".join(lines)
                base["tokensCount"] = sum(1 for ln in lines if ln.strip())
    return base


@app.route("/customer")
def customer_home():
    target = _find_asset("customer.html")
    if target is None:
        return ("customer.html not found in deployed bundle.", 500)
    return send_from_directory(str(target.parent), target.name)


@app.route("/api/customer/me", methods=["GET"])
def api_customer_me():
    """Return the panel that the presented token owns, with live status."""
    token = _customer_token_from_request()
    panel = _resolve_customer_panel(token)
    if panel is None:
        return jsonify({"status": "error", "message": "Invalid token"}), 401

    return jsonify({
        "status": "success",
        "panel": _customer_safe_view(panel, with_status=True),
        "ts": int(time.time()),
    }), 200


@app.route("/api/customer/power", methods=["POST"])
def api_customer_power():
    """Send a power signal to the panel owned by the presented token.

    Customers cannot specify which panel to act on — we look it up
    from the token, so they can only ever control their own server."""
    token = _customer_token_from_request()
    panel = _resolve_customer_panel(token)
    if panel is None:
        return jsonify({"status": "error", "message": "Invalid token"}), 401

    body = request.get_json(silent=True) or {}
    signal = str(body.get("signal") or "").lower()
    if signal not in ALLOWED_SIGNALS:
        return jsonify({"status": "error", "message": "Invalid signal."}), 400

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
            "id": panel["id"],
            "name": panel["name"],
            "signal": signal,
        }), 200
    return jsonify({
        "status": "error",
        "message": "Panel rejected (" + str(resp.status_code) + "): " + resp.text[:200],
    }), 502


@app.route("/api/customer/token", methods=["POST"])
def api_customer_token():
    """Let a customer rewrite their server's whole tokens.txt.

    One bot per line in ``TOKEN CHANNELID`` form (channel optional).
    Blank lines are dropped. The file is written back to the customer's
    own Pterodactyl server via the Client API — they can never touch
    another customer's server."""
    token = _customer_token_from_request()
    panel = _resolve_customer_panel(token)
    if panel is None:
        return jsonify({"status": "error", "message": "Invalid token"}), 401

    if not (panel["panelUrl"] and panel["serverId"] and panel["clientApiKey"]):
        return jsonify({"status": "error", "message": "Panel not configured."}), 400

    body = request.get_json(silent=True) or {}
    raw_text = body.get("tokensText")
    if raw_text is None:
        return jsonify({"status": "error", "message": "Missing tokensText."}), 400

    # Normalise line endings and validate each non-empty line.
    in_lines = str(raw_text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out_lines = []
    for i, line in enumerate(in_lines):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        bot_token = parts[0]
        channel = parts[1] if len(parts) >= 2 else ""
        if len(parts) > 2:
            return jsonify({
                "status": "error",
                "message": "Line {}: expected only TOKEN and CHANNELID.".format(i + 1),
            }), 400
        out_lines.append((bot_token + " " + channel).strip())

    if not out_lines:
        return jsonify({"status": "error", "message": "At least one token line is required."}), 400

    ok, msg = _ptero_write_token_lines(panel, out_lines)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 502

    # Refresh the cached copy so the next read reflects the new tokens.
    _invalidate_tokens(panel["id"])
    return jsonify({
        "status": "success",
        "message": "Saved {} bot{} to the server. Restart it for changes to take effect.".format(
            len(out_lines), "" if len(out_lines) == 1 else "s"),
        "tokensCount": len(out_lines),
    }), 200


# ---------------------------------------------------------------------------
# Vercel WSGI aliases — Vercel scans this file for one of these names.
# Define them at the absolute end so the static scanner finds them
# regardless of how the rest of the file is parsed.
# ---------------------------------------------------------------------------
application = app
handler = app


# ---------------------------------------------------------------------------
# Local entrypoint (Vercel ignores __main__)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("ADMIN_PORT") or _get_config().get("port") or 7860)
    host = os.environ.get("ADMIN_HOST") or str(_get_config().get("host") or "0.0.0.0")
    if not _resolved_password():
        print("[admin-panel] WARN: no ADMIN_PASSWORD set", file=sys.stderr)
    print("[admin-panel] http://{}:{}".format(host, port))
    app.run(host=host, port=port, debug=False, use_reloader=False)
