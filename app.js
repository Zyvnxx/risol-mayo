/*
Admin Panel — standalone Pterodactyl multi-server controller.
Self-contained: no shared global state, IIFE wrapper.
*/

(() => {
    "use strict";

    /* ===================================================================
     * Auth bootstrap
     * =================================================================== */
    function ensurePassword() {
        let pwd = localStorage.getItem("adm_password");
        if (!pwd) {
            pwd = window.prompt("Enter Admin Password");
            if (!pwd) {
                document.body.innerHTML =
                    '<div style="padding:48px;text-align:center;font-family:Inter,sans-serif;color:#c8d3ee">' +
                    "Password is required." +
                    "</div>";
                throw new Error("no-password");
            }
            localStorage.setItem("adm_password", pwd);
        }
        return pwd;
    }

    let PASSWORD = "";
    try {
        PASSWORD = ensurePassword();
    } catch (e) { return; }

    /* ===================================================================
     * Constants
     * =================================================================== */
    const REFRESH_MS = 15_000;
    let __refreshTimer = null;
    let __toastTimer = null;
    let __inFlight = false;

    const STATE_META = {
        running:    { txt: "Running",     cls: "is-running"   },
        starting:   { txt: "Starting",    cls: "is-starting"  },
        stopping:   { txt: "Stopping",    cls: "is-stopping"  },
        offline:    { txt: "Offline",     cls: "is-offline"   },
        unknown:    { txt: "Unknown",     cls: "is-unknown"   },
        unreachable:{ txt: "Unreachable", cls: "is-error"     },
    };

    /* ===================================================================
     * Utilities
     * =================================================================== */
    function escapeHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

    function formatBytes(bytes) {
        const n = Number(bytes || 0);
        if (!isFinite(n) || n <= 0) return "0";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let i = 0;
        let v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
    }

    function formatUptimeMs(ms) {
        const n = Number(ms || 0);
        if (!isFinite(n) || n < 1000) return "—";
        const totalSec = Math.floor(n / 1000);
        const d = Math.floor(totalSec / 86400);
        const h = Math.floor((totalSec % 86400) / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;
        if (d > 0) return `${d}d ${h}h`;
        if (h > 0) return `${h}h ${m}m`;
        if (m > 0) return `${m}m ${s}s`;
        return `${s}s`;
    }

    function setStatus(text, kind) {
        const pill = document.getElementById("adm-status-pill");
        const txt  = document.getElementById("adm-status-text");
        if (txt) txt.textContent = text;
        if (!pill) return;
        pill.classList.remove("is-error", "is-loading");
        if (kind === "error")   pill.classList.add("is-error");
        if (kind === "loading") pill.classList.add("is-loading");
    }

    function showToast(text, kind) {
        const t = document.getElementById("adm-toast");
        const txt = document.getElementById("adm-toast-text");
        if (!t || !txt) return;
        txt.textContent = text;
        t.classList.remove("adm-hidden", "is-success", "is-error", "is-warn");
        if (kind) t.classList.add("is-" + kind);
        if (__toastTimer) clearTimeout(__toastTimer);
        __toastTimer = setTimeout(() => t.classList.add("adm-hidden"), 2800);
    }

    /* ===================================================================
     * Fetch helpers
     * =================================================================== */
    async function api(path, { method = "GET", body = null } = {}) {
        try {
            const opts = {
                method,
                headers: { password: PASSWORD, "Content-Type": "application/json" },
            };
            if (body) opts.body = JSON.stringify(body);
            const r = await fetch(path, opts);
            if (r.status === 401 || r.status === 403) {
                localStorage.removeItem("adm_password");
                alert("Wrong password. Please re-enter.");
                location.reload();
                return null;
            }
            const data = await r.json().catch(() => ({}));
            return { ok: r.ok, status: r.status, data };
        } catch (err) {
            console.error("admin api error:", err);
            return { ok: false, status: 0, data: { message: String(err && err.message || err) } };
        }
    }

    /* ===================================================================
     * Refresh / render
     * =================================================================== */
    async function refresh({ silent = false } = {}) {
        if (__inFlight) return;
        __inFlight = true;
        if (!silent) setStatus("Refreshing…", "loading");

        try {
            const res = await api("/api/panels");
            if (!res || !res.ok) {
                setStatus("Disconnected", "error");
                if (!silent) {
                    const msg = (res && res.data && res.data.message) || "Failed to load panels.";
                    showToast(msg, "error");
                }
                return;
            }

            const data = res.data;
            const panels = data.panels || [];
            renderSummary(panels);
            renderGrid(panels);
            setStatus("Online", "ok");
        } finally {
            __inFlight = false;
        }
    }

    function renderSummary(panels) {
        let total = panels.length;
        let running = 0;
        let offline = 0;
        let unreachable = 0;
        for (const p of panels) {
            const s = p.status || {};
            if (!s.reachable) { unreachable++; continue; }
            const st = (s.state || "").toLowerCase();
            if (st === "running" || st === "starting") running++;
            else if (st === "offline" || st === "stopping") offline++;
        }
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = String(v); };
        set("adm-total", total);
        set("adm-running", running);
        set("adm-offline", offline);
        set("adm-unreachable", unreachable);
    }

    function renderGrid(panels) {
        const grid = document.getElementById("adm-grid");
        if (!grid) return;

        if (!panels.length) {
            grid.innerHTML = `
                <div class="adm-empty">
                    <strong>No panels configured.</strong>
                    <p>Edit <code>config.json</code> on the admin host and add entries
                    under <code>panels</code>. Each entry needs <code>id</code>,
                    <code>name</code>, <code>panelUrl</code>, <code>serverId</code>,
                    and <code>clientApiKey</code>.</p>
                    <p>The file reloads automatically on the next request.</p>
                </div>`;
            return;
        }

        grid.innerHTML = panels.map(renderCard).join("");
        bindCardActions();
    }

    function renderCard(p) {
        const s = p.status || {};
        const reachable = !!s.reachable;
        const stateKey = (s.state || (reachable ? "unknown" : "unreachable")).toLowerCase();
        const meta = STATE_META[stateKey] || STATE_META.unknown;

        const cpu     = Number(s.cpuAbsolute || 0);
        const memory  = formatBytes(s.memoryBytes || 0);
        const disk    = formatBytes(s.diskBytes || 0);
        const uptime  = formatUptimeMs(s.uptimeMs || 0);

        const errorBlock = (!reachable && p.configured)
            ? `<div class="adm-card-error">⚠ ${escapeHtml(s.error || "Panel unreachable")}</div>`
            : "";
        const unconfiguredBlock = (!p.configured)
            ? `<div class="adm-card-error">⚠ Panel is not fully configured.</div>`
            : "";

        const stats = (reachable && p.configured) ? `
            <div class="adm-card-stats">
                <div class="adm-card-stat">
                    <span class="adm-card-stat-label">CPU</span>
                    <span class="adm-card-stat-value">${cpu.toFixed(1)}%</span>
                </div>
                <div class="adm-card-stat">
                    <span class="adm-card-stat-label">RAM</span>
                    <span class="adm-card-stat-value">${escapeHtml(memory)}</span>
                </div>
                <div class="adm-card-stat">
                    <span class="adm-card-stat-label">Disk</span>
                    <span class="adm-card-stat-value">${escapeHtml(disk)}</span>
                </div>
                <div class="adm-card-stat">
                    <span class="adm-card-stat-label">Uptime</span>
                    <span class="adm-card-stat-value">${escapeHtml(uptime)}</span>
                </div>
            </div>` : "";

        const isRunning = stateKey === "running" || stateKey === "starting";
        const startDisabled   = !p.configured || isRunning ? "disabled" : "";
        const stopDisabled    = !p.configured || stateKey === "offline" ? "disabled" : "";
        const restartDisabled = !p.configured ? "disabled" : "";

        return `
            <article class="adm-card" data-panel-id="${escapeHtml(p.id)}">
                <header class="adm-card-header">
                    <div class="adm-card-title">
                        <h3>${escapeHtml(p.name)}</h3>
                        <span class="adm-card-id">${escapeHtml(p.id)}</span>
                    </div>
                    <span class="adm-state ${meta.cls}">
                        <span class="adm-state-dot"></span>${escapeHtml(meta.txt)}
                    </span>
                </header>

                <div class="adm-card-meta">
                    <div class="adm-card-row">
                        <span class="adm-card-row-label">Panel</span>
                        <span class="adm-card-row-value" title="${escapeHtml(p.panelUrl || "")}">${escapeHtml(p.panelUrl || "—")}</span>
                    </div>
                    <div class="adm-card-row">
                        <span class="adm-card-row-label">Server ID</span>
                        <span class="adm-card-row-value mono">${escapeHtml(p.serverId || "—")}</span>
                    </div>
                </div>

                ${unconfiguredBlock}
                ${errorBlock}
                ${stats}

                <div class="adm-card-actions">
                    <button class="adm-btn adm-btn-success adm-power-btn" data-signal="start" ${startDisabled}>
                        <span>▶</span> Start
                    </button>
                    <button class="adm-btn adm-btn-danger adm-power-btn" data-signal="stop" ${stopDisabled}>
                        <span>■</span> Stop
                    </button>
                    <button class="adm-btn adm-btn-warn adm-power-btn" data-signal="restart" ${restartDisabled}>
                        <span>↻</span> Restart
                    </button>
                    <button class="adm-btn adm-btn-kill adm-power-btn" data-signal="kill" ${restartDisabled}>
                        <span>✖</span> Kill
                    </button>
                </div>
            </article>`;
    }

    function bindCardActions() {
        document.querySelectorAll(".adm-card .adm-power-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const card   = btn.closest(".adm-card");
                const id     = card && card.dataset.panelId;
                const signal = btn.dataset.signal;
                if (!id || !signal) return;
                powerPanel(id, signal, btn);
            });
        });
    }

    /* ===================================================================
     * Power actions
     * =================================================================== */
    async function powerPanel(id, signal, btn) {
        const verbs = { start: "Start", stop: "Stop", restart: "Restart", kill: "Force-kill" };
        const verb = verbs[signal] || signal;

        if (signal === "stop" || signal === "kill") {
            if (!confirm(`${verb} panel "${id}"?`)) return;
        }

        const original = btn ? btn.innerHTML : null;
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<span>⏳</span> ${verb}ing…`;
        }

        const res = await api("/api/panels/power", {
            method: "POST",
            body: { id, signal },
        });

        if (btn) {
            btn.disabled = false;
            btn.innerHTML = original;
        }

        if (res && res.ok && res.data && res.data.status === "success") {
            showToast(`${verb} sent to ${id}`, "success");
            setTimeout(() => refresh({ silent: true }), 1500);
        } else {
            const msg = (res && res.data && res.data.message) || "Request failed.";
            showToast(`${verb} failed: ${msg}`, "error");
        }
    }

    async function bulkPower(signal) {
        const verbs = { start: "Start", stop: "Stop", restart: "Restart" };
        const verb = verbs[signal] || signal;
        const cards = document.querySelectorAll(".adm-card[data-panel-id]");
        if (!cards.length) {
            showToast("No panels to act on.", "warn");
            return;
        }
        if (!confirm(`${verb} ALL ${cards.length} panel(s)?`)) return;

        showToast(`Sending ${signal} to ${cards.length} panel(s)…`, "warn");

        let okCount = 0;
        for (const card of cards) {
            const id = card.dataset.panelId;
            if (!id) continue;
            const res = await api("/api/panels/power", {
                method: "POST",
                body: { id, signal },
            });
            if (res && res.ok && res.data && res.data.status === "success") okCount++;
        }
        showToast(`${verb}: ${okCount}/${cards.length} succeeded.`,
                  okCount === cards.length ? "success" : "warn");
        setTimeout(() => refresh({ silent: true }), 1500);
    }

    /* ===================================================================
     * Config editor (settings modal)
     * =================================================================== */
    const SETTINGS_FIELDS = [
        { key: "id",            label: "ID",           required: true,                placeholder: "main" },
        { key: "name",          label: "Display Name", required: false,               placeholder: "Main Bot" },
        { key: "panelUrl",      label: "Panel URL",    required: true,  wide: true,   placeholder: "https://panel.example.com", mono: true },
        { key: "serverId",      label: "Server ID",    required: true,                placeholder: "abcd1234", mono: true },
        { key: "clientApiKey",  label: "Client API Key", required: true, mono: true,  placeholder: "ptlc_xxx", masked: true },
        { key: "customerToken", label: "Customer Token", required: false, wide: true, mono: true, placeholder: "(optional, give to customer for /customer login)", masked: true },
        { key: "expiresAt",     label: "Expires At",   required: false,               placeholder: "2025-07-20 (ISO date, enables countdown)", mono: true },
        { key: "tokenLine",     label: "Token Line #", required: false,               placeholder: "1 (line number in tokens.txt, enables token editor)", mono: true },
        { key: "tokensPath",    label: "Tokens Path",  required: false, wide: true,   placeholder: "tokens.txt (path on the Pterodactyl server)", mono: true },
    ];

    let __configState = null;       // current edited config snapshot
    let __configMeta = null;        // {source, vercelSync, missingVercelKeys}

    function openSettingsModal() {
        const m = document.getElementById("adm-settings-modal");
        if (!m) return;
        m.classList.remove("adm-hidden");
        document.body.style.overflow = "hidden";
        loadSettings();
    }

    function closeSettingsModal() {
        const m = document.getElementById("adm-settings-modal");
        if (!m) return;
        m.classList.add("adm-hidden");
        document.body.style.overflow = "";
    }

    async function loadSettings() {
        const list = document.getElementById("adm-settings-list");
        const sourceEl = document.getElementById("adm-settings-source");
        const banner = document.getElementById("adm-settings-banner");
        if (!list) return;

        list.innerHTML = '<div class="adm-loading">Loading config…</div>';

        const res = await api("/api/config");
        if (!res || !res.ok) {
            const msg = (res && res.data && res.data.message) || "Failed to load config.";
            list.innerHTML = `<div class="adm-empty"><strong>${escapeHtml(msg)}</strong></div>`;
            return;
        }

        __configState = res.data.config || { panels: [] };
        if (!Array.isArray(__configState.panels)) __configState.panels = [];
        __configMeta = {
            source: res.data.source,
            vercelSync: !!res.data.vercelSync,
            missingVercelKeys: res.data.missingVercelKeys || [],
        };

        if (sourceEl) {
            const where = __configMeta.vercelSync
                ? "Synced to Vercel env vars (auto-redeploy on save)"
                : __configMeta.source === "env"
                    ? "Read-only: config came from env (no Vercel sync configured)"
                    : "Editing config.json on disk";
            sourceEl.textContent = where;
        }

        if (banner) {
            if (__configMeta.source === "env" && !__configMeta.vercelSync) {
                banner.classList.remove("adm-hidden");
                banner.innerHTML =
                    "<strong>Vercel sync is not configured.</strong> " +
                    "Set <code>VERCEL_TOKEN</code> and <code>VERCEL_PROJECT_ID</code> " +
                    "in this project's environment variables to enable saving from the UI. " +
                    "Until then, edit <code>ADMIN_CONFIG_JSON</code> in Vercel manually and redeploy.";
            } else if (__configMeta.vercelSync) {
                banner.classList.remove("adm-hidden");
                banner.classList.add("is-info");
                banner.innerHTML =
                    "Saving will update <code>ADMIN_CONFIG_JSON</code> on Vercel and trigger a production redeploy. " +
                    "Allow ~30 seconds for the new config to take effect.";
            } else {
                banner.classList.add("adm-hidden");
            }
        }

        renderSettingsList();
    }

    function renderSettingsList() {
        const list = document.getElementById("adm-settings-list");
        const countEl = document.getElementById("adm-panels-count");
        if (!list || !__configState) return;

        const panels = __configState.panels || [];
        if (countEl) countEl.textContent = `${panels.length} panel${panels.length === 1 ? "" : "s"}`;

        if (!panels.length) {
            list.innerHTML = '<div class="adm-empty"><strong>No panels configured.</strong><p>Click "Add panel" to create the first one.</p></div>';
            return;
        }

        list.innerHTML = panels.map((p, i) => renderSettingsRow(p, i)).join("");
        bindSettingsRowEvents();
    }

    function renderSettingsRow(panel, idx) {
        const fields = SETTINGS_FIELDS.map(f => {
            const raw = panel[f.key];
            const val = raw == null ? "" : String(raw);
            const masked = f.masked && val === "********";
            const cls = [
                "adm-field-input",
                f.mono ? "mono" : "",
                masked ? "is-masked" : "",
            ].filter(Boolean).join(" ");
            return `
                <label class="adm-field ${f.wide ? "adm-field-wide" : ""}">
                    <span class="adm-field-label">${escapeHtml(f.label)}${f.required ? " *" : ""}</span>
                    <input type="text"
                           class="${cls}"
                           data-field="${f.key}"
                           data-idx="${idx}"
                           value="${escapeHtml(val)}"
                           placeholder="${escapeHtml(f.placeholder || "")}"
                           autocomplete="off"
                           spellcheck="false">
                </label>`;
        }).join("");

        const title = panel.name || panel.id || `Panel #${idx + 1}`;

        return `
            <article class="adm-settings-row" data-row-idx="${idx}">
                <header class="adm-settings-row-header">
                    <span class="adm-settings-row-title">${escapeHtml(title)}</span>
                    <button class="adm-settings-row-remove" type="button" data-remove-idx="${idx}">Remove</button>
                </header>
                ${fields}
            </article>`;
    }

    function bindSettingsRowEvents() {
        document.querySelectorAll("#adm-settings-list .adm-field-input").forEach(inp => {
            inp.addEventListener("input", e => {
                const idx = Number(e.target.dataset.idx);
                const field = e.target.dataset.field;
                if (Number.isNaN(idx) || !field) return;
                const panels = __configState.panels;
                if (!panels[idx]) return;
                panels[idx][field] = e.target.value;
                // The value is no longer the mask — drop the visual cue.
                if (e.target.classList.contains("is-masked")) {
                    e.target.classList.remove("is-masked");
                }
                // Update the row header title in real time.
                if (field === "name" || field === "id") {
                    const row = e.target.closest(".adm-settings-row");
                    const titleEl = row && row.querySelector(".adm-settings-row-title");
                    if (titleEl) {
                        titleEl.textContent = panels[idx].name || panels[idx].id || `Panel #${idx + 1}`;
                    }
                }
            });
        });

        document.querySelectorAll("[data-remove-idx]").forEach(btn => {
            btn.addEventListener("click", e => {
                const idx = Number(e.target.dataset.removeIdx);
                if (Number.isNaN(idx)) return;
                const panels = __configState.panels;
                if (!panels[idx]) return;
                if (!confirm(`Remove "${panels[idx].name || panels[idx].id}"?`)) return;
                panels.splice(idx, 1);
                renderSettingsList();
            });
        });
    }

    function addPanel() {
        if (!__configState) return;
        if (!Array.isArray(__configState.panels)) __configState.panels = [];

        // Generate a unique id like panel-1, panel-2, …
        const existing = new Set(__configState.panels.map(p => String(p.id || "")));
        let n = __configState.panels.length + 1;
        let pid = `panel-${n}`;
        while (existing.has(pid)) {
            n += 1;
            pid = `panel-${n}`;
        }

        __configState.panels.push({
            id: pid,
            name: pid,
            panelUrl: "",
            serverId: "",
            clientApiKey: "",
        });
        renderSettingsList();
    }

    async function saveSettings() {
        if (!__configState) return;

        // Client-side validation matches the server.
        const panels = __configState.panels || [];
        const seenIds = new Set();
        for (let i = 0; i < panels.length; i++) {
            const p = panels[i];
            const pid = String(p.id || "").trim();
            if (!pid) {
                showToast(`Panel #${i + 1} is missing an ID.`, "error");
                return;
            }
            if (seenIds.has(pid)) {
                showToast(`Duplicate panel ID: ${pid}`, "error");
                return;
            }
            seenIds.add(pid);
        }

        const btn = document.getElementById("adm-settings-save");
        const original = btn ? btn.innerHTML : null;
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = "<span>⏳</span> Saving…";
        }

        const res = await api("/api/config", {
            method: "POST",
            body: { config: __configState },
        });

        if (btn) {
            btn.disabled = false;
            btn.innerHTML = original;
        }

        if (!res) return;
        if (res.ok && res.data && (res.data.status === "success" || res.data.status === "partial")) {
            const msg = res.data.message || "Saved.";
            showToast(msg, res.data.status === "partial" ? "warn" : "success");
            closeSettingsModal();
            // Give Vercel a moment to redeploy, then refresh the panel list.
            // The first refresh is fast and serves as a sanity check; the
            // second one (after ~25s) usually catches the redeployed env.
            setTimeout(() => refresh({ silent: true }), 2_000);
            if (__configMeta && __configMeta.vercelSync) {
                setTimeout(() => refresh({ silent: true }), 25_000);
            }
        } else {
            const msg = (res.data && res.data.message) || "Save failed.";
            showToast(msg, "error");
        }
    }

    /* ===================================================================
     * Boot
     * =================================================================== */
    document.addEventListener("DOMContentLoaded", () => {
        const refreshBtn = document.getElementById("adm-refresh-btn");
        if (refreshBtn) refreshBtn.addEventListener("click", () => refresh({ silent: false }));

        const logoutBtn = document.getElementById("adm-logout-btn");
        if (logoutBtn) logoutBtn.addEventListener("click", () => {
            if (!confirm("Forget password and re-enter?")) return;
            localStorage.removeItem("adm_password");
            location.reload();
        });

        const settingsBtn = document.getElementById("adm-settings-btn");
        if (settingsBtn) settingsBtn.addEventListener("click", openSettingsModal);

        document.querySelectorAll("[data-modal-close]").forEach(el => {
            el.addEventListener("click", closeSettingsModal);
        });
        document.addEventListener("keydown", e => {
            if (e.key === "Escape") closeSettingsModal();
        });

        const addBtn = document.getElementById("adm-add-panel");
        if (addBtn) addBtn.addEventListener("click", addPanel);

        const saveBtn = document.getElementById("adm-settings-save");
        if (saveBtn) saveBtn.addEventListener("click", saveSettings);

        document.querySelectorAll("[data-bulk]").forEach(b => {
            b.addEventListener("click", () => bulkPower(b.dataset.bulk));
        });

        refresh({ silent: false });

        __refreshTimer = setInterval(() => {
            if (typeof document !== "undefined" && document.hidden) return;
            refresh({ silent: true });
        }, REFRESH_MS);

        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) refresh({ silent: true });
        });
    });
})();
