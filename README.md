# Admin Panel

Standalone web dashboard for controlling multiple Pterodactyl game servers
from a single page. Completely independent from any specific bot — you
deploy this on its own host (different machine, different VPS, Render,
Railway, Fly, Vercel, Docker — anything that can run Python or a Python
serverless function).

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

## Project structure (flat — same layout works everywhere)

```
admin-panel/
├── index.py              Flask app + Pterodactyl proxy (the entrypoint)
├── index.html            Landing page
├── app.js                Front-end logic
├── style.css             Styles
├── config.example.json   Copy this to config.json (local/VPS only)
├── requirements.txt
├── vercel.json           Vercel build config
├── Procfile              Heroku/Render/Railway/Fly runner
├── Dockerfile            Optional container build
└── README.md
```

## Setup (local / VPS / Docker)

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
   python index.py
   ```
   Open http://localhost:7860.

The config file is **hot-reloaded** (file mode only). Edit `config.json`,
save it, and the next request picks up the change without restarting.

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

## Deployment

### Plain VPS / shared host

```
# systemd unit (Linux)
[Service]
WorkingDirectory=/opt/admin-panel
ExecStart=/opt/admin-panel/.venv/bin/python index.py
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

The `Procfile` already defines `web: python index.py`. Set the
`ADMIN_PASSWORD` and `ADMIN_CONFIG_JSON` (or commit a `config.json`)
environment variables in the platform UI.

### Vercel (serverless)

Vercel works, but with caveats — it's serverless:
- **No writable filesystem.** You can't ship `config.json` and edit it
  later. Use `ADMIN_CONFIG_JSON` (or `ADMIN_CONFIG_B64`) env vars.
- **10 second hard timeout** per request on the Hobby plan (60s on Pro).
  The listing endpoint hits every panel in parallel, so this only
  matters if a single panel takes >5s to respond.
- **Cold starts** add ~0.5–2s to the first request after idle.
- Outbound HTTPS to your Pterodactyl panels works fine.

Steps:

1. Push the `admin-panel/` folder to a Git repo (GitHub / GitLab /
   Bitbucket).
2. In Vercel, **New Project** → **Import Git Repository**.
3. **Root Directory**: set to `admin-panel` if your repo contains other
   code, otherwise leave as the repo root.
4. **Framework Preset**: leave as **Other** — Vercel will pick up
   `vercel.json` automatically.
5. Add **Environment Variables** in the project settings:
   - `ADMIN_PASSWORD` — login password for the UI
   - `ADMIN_CONFIG_JSON` — full JSON config, single line. Example:
     ```
     {"panels":[{"id":"main","name":"Main Bot","panelUrl":"https://panel.example.com","serverId":"abcd1234","clientApiKey":"ptlc_xxx"}]}
     ```
     If raw JSON is awkward (quoting, length), use `ADMIN_CONFIG_B64`
     instead — encode the JSON with `base64`:
     ```
     # Linux/macOS:
     echo -n '{"panels":[...]}' | base64
     # PowerShell:
     [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes('{"panels":[...]}'))
     ```
6. **Deploy.** Open the assigned URL — the page should prompt for the
   password.

To change panels later: edit `ADMIN_CONFIG_JSON` in Vercel's project
settings and trigger a redeploy. Vercel does not pick up env-var changes
on running instances without a redeploy.

#### In-browser config editor on Vercel

The Settings button in the header opens a config editor that can update
panels directly from the browser. Saving will:
1. Patch the `ADMIN_CONFIG_JSON` env var in your Vercel project, then
2. Trigger a production redeploy so the new config takes effect (~30s).

API keys are masked in the form (shown as `********`); leaving them
masked keeps the original value, so you can edit names/URLs without
re-typing keys.

To enable this, add three more env vars in Vercel:

| Variable             | Where to find it                                                  |
|----------------------|-------------------------------------------------------------------|
| `VERCEL_TOKEN`       | https://vercel.com/account/tokens — create a personal access token |
| `VERCEL_PROJECT_ID`  | Project Settings → General → "Project ID"                          |
| `VERCEL_TEAM_ID`     | Only required if the project lives in a Team (Team Settings → "Team ID") |

Without these, the editor still loads (read-only) but Save fails with
a clear message. Without `VERCEL_TOKEN` no env-var changes are possible.

The token only needs the default scopes. Treat it like a credential —
anyone with this token can modify your Vercel project. Restrict its
expiration to whatever you're comfortable with.

#### Vercel files

- `vercel.json` — runtime config, declares `index.py` as the
  `@vercel/python` build target and rewrites all routes to it.
- `index.py` — Flask app (Vercel imports `app` from this module).
- `index.html`, `app.js`, `style.css` — static assets served by Flask
  via the allowlist in `index.py`.

## Security

- The admin host only needs outbound HTTPS access — it never receives
  callbacks from your panels.
- API keys live only on the admin host. They are **never** sent to the
  browser; the server proxies every action.
- Use a strong `ADMIN_PASSWORD`. Always run behind HTTPS in production
  (nginx, Caddy, Cloudflare Tunnel, or just deploy on Vercel/Render
  which give you HTTPS for free).
- The Pterodactyl client API key only needs power permissions; create a
  dedicated key with the minimum scope you actually use. Restrict
  allowed IPs to your admin host if your panel supports it.

## Pterodactyl API key

In your Pterodactyl panel:

1. Click your avatar → **API Credentials**
2. **Create new** with a memorable description (e.g. "admin-panel")
3. (Optional) Restrict allowed IPs to the admin host
4. Copy the generated key into `panels[].clientApiKey`

The key looks like `ptlc_xxxxxxxxxxxxxxxxx`.
