# Admin Panel

Standalone web dashboard for controlling multiple Pterodactyl game servers
from a single page. Completely independent from any specific bot — you
deploy this on its own host (different machine, different VPS, Render,
Railway, Fly, Docker, anything).

It connects to each Pterodactyl panel over the public HTTPS API. The
admin host only needs **outbound** internet access.

## What it does

- Lists every Pterodactyl server you've configured
- Shows live status (running / starting / stopping / offline / unreachable)
- Live CPU%, RAM, disk, uptime per server
- Per-server actions: Start, Stop, Restart, Kill
- Bulk actions: Start All, Restart All, Stop All
- Auto-refresh every 15s while the tab is visible
- Password-gated; API keys never leave the server

## Project structure

```
admin-panel/
├── app.py                   Flask app + Pterodactyl proxy
├── requirements.txt
├── config.example.json      Copy this to config.json
├── Dockerfile               Optional container build
├── Procfile                 Heroku/Railway/Render-style runner
├── templates/
│   └── index.html
└── static/
    ├── style.css
    └── app.js
```

## Setup

1. **Create a virtual env** (recommended)

   Windows:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   ```
   Linux/macOS:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

3. **Create your config**
   ```
   copy config.example.json config.json     (Windows)
   cp   config.example.json config.json     (Linux/macOS)
   ```
   Edit `config.json`:
   - `password` — login password for the admin web UI
   - `host` / `port` — bind address & port (default `0.0.0.0` / `7860`)
   - `panels[]` — one entry per Pterodactyl server

   For each panel entry:
   - `id` — short unique identifier (used as the action key)
   - `name` — display name
   - `panelUrl` — root URL of your Pterodactyl panel (no trailing slash)
   - `serverId` — short ID from the panel (the part after `/server/`)
   - `clientApiKey` — Client API key (Pterodactyl → Account → API Credentials)

4. **Run**
   ```
   python app.py
   ```
   Open http://localhost:7860 (or whatever host/port you configured).

The config file is **hot-reloaded**. Edit `config.json`, save it, and the
next refresh on the page picks up the change without restarting.

## Environment overrides

These take precedence over `config.json`:

| Variable             | Purpose                                                        | Default          |
|----------------------|----------------------------------------------------------------|------------------|
| `ADMIN_PORT`         | Port to listen on                                              | `7860`           |
| `ADMIN_HOST`         | Bind address                                                   | `0.0.0.0`        |
| `ADMIN_PASSWORD`     | Auth password                                                  | (from config)    |
| `ADMIN_CONFIG`       | Path to `config.json`                                          | `./config.json`  |
| `ADMIN_CONFIG_JSON`  | Inline JSON config (serverless-friendly, no file needed)       | —                |
| `ADMIN_CONFIG_B64`   | Same as above but base64-encoded                               | —                |
| `PTERO_TIMEOUT`      | Per-panel HTTP timeout in seconds                              | `5`              |
| `PTERO_PARALLEL`     | Max concurrent /resources calls                                | `8`              |

Useful for running with a process manager or PaaS where the password
should not live in the file system.

## Deployment notes

### Plain VPS / shared host

Use a process manager so the app restarts on crash:

```
# systemd unit (Linux)
[Service]
WorkingDirectory=/opt/admin-panel
ExecStart=/opt/admin-panel/.venv/bin/python app.py
Environment=ADMIN_PASSWORD=your-secret
Restart=always
```

Put it behind nginx or Caddy for HTTPS.

### Docker

```
docker build -t admin-panel .
docker run -d \
  -p 7860:7860 \
  -e ADMIN_PASSWORD=your-secret \
  -v $(pwd)/config.json:/app/config.json:ro \
  --name admin-panel \
  admin-panel
```

### Render / Railway / Fly.io

The `Procfile` already defines `web: python app.py`. Set the
`ADMIN_PASSWORD` environment variable in the platform UI and either commit
your `config.json` or upload it as a secret file mounted at
`/app/config.json`.

### Vercel (serverless)

Vercel works, but with caveats — Vercel is serverless, so:
- No writable / persistent filesystem. You can't ship `config.json`
  alongside the function and edit it later. Use the env-var config path
  instead (`ADMIN_CONFIG_JSON` or `ADMIN_CONFIG_B64`).
- 10 second hard timeout per request on the Hobby plan (60s on Pro). If
  any of your panels takes more than ~5s to respond, the whole listing
  call may exceed budget. Per-panel calls now run in parallel, so this
  only matters if a single panel is slow.
- Cold starts add ~0.5–2s on the first request after idle.
- Outbound HTTPS to your Pterodactyl panels works fine.

Steps:

1. Push the `admin-panel/` folder to a Git repo (GitHub / GitLab /
   Bitbucket).
2. In Vercel, **New Project** → **Import Git Repository**.
3. Set the **Root Directory** to `admin-panel` if your repo contains
   other code. Otherwise leave it as the repo root.
4. Framework Preset: **Other** (Vercel will auto-detect Python from
   `vercel.json`).
5. Add **Environment Variables**:
   - `ADMIN_PASSWORD` — login password for the UI
   - `ADMIN_CONFIG_JSON` — full JSON config (without the `password`
     field, since `ADMIN_PASSWORD` env var overrides it). Example:
     ```json
     {"panels":[{"id":"main","name":"Main Bot","panelUrl":"https://panel.example.com","serverId":"abcd1234","clientApiKey":"ptlc_xxx"}]}
     ```
     If raw JSON is awkward in the env-var UI (quoting / size), use
     `ADMIN_CONFIG_B64` instead with the base64-encoded version:
     ```
     echo -n '{"panels":[...]}' | base64 -w0
     ```
6. Deploy. Open the assigned URL — the page should prompt for the
   password.
7. To change panels, update `ADMIN_CONFIG_JSON` in Vercel project
   settings and redeploy (or just trigger a redeploy — Vercel doesn't
   pick up env-var changes on existing instances).

Files relevant for Vercel:
- `vercel.json` — runtime config + 10s `maxDuration`
- `api/index.py` — serverless entrypoint that re-exports the Flask app
- `.vercelignore` — keeps the deployed bundle small

## Security

- The admin host only needs outbound HTTPS access — it never receives
  callbacks from your panels.
- API keys live only in `config.json` on the admin host. They are
  **never** sent to the browser; the server proxies every action.
- Use a strong `password` (or an `ADMIN_PASSWORD` env var). Always run
  behind HTTPS in production (nginx, Caddy, Cloudflare Tunnel).
- The Pterodactyl client API key only needs power permissions; create a
  dedicated key with the minimum scope you actually use.

## Pterodactyl API key

In your Pterodactyl panel:

1. Click your avatar → **API Credentials**
2. **Create new** with a memorable description (e.g. "admin-panel")
3. (Optional) Restrict allowed IPs to the admin host
4. Copy the generated key into `panels[].clientApiKey`

The key looks like `ptlc_xxxxxxxxxxxxxxxxx`.
