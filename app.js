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
    let __toastTimer = null;
    let __inFlight = false;

    const STATE_META = {
        running:    { txt: "Running",     cls: "is-running"   },
        starting:   { txt: "Starting",    cls: "is-starting"  },
        stopping:   { txt: "Stopping",    cls: "is-stopping"  },
        offline:    { txt: "Offline",     cls: "is-offline"   },
        unknown:    { txt: "Unknown",     cls: "is-unknown"   },
        unreachable:{ txt: "Unreachable", cls: "is-error"     },
        checking:   { txt: "Checking…",   cls: "is-unknown"   },
    };

    // Per-card signature cache so we only touch the DOM when something
    // actually changed. Keyed by panel id.
    const __cardSig = new Map();

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
    async function api(path, { method = "GET", body = null, timeoutMs = 30_000 } = {}) {
        const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
        const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
        try {
            const isForm = (typeof FormData !== "undefined") && (body instanceof FormData);
            const opts = {
                method,
                // For FormData, let the browser set Content-Type (with the
                // multipart boundary). Only send JSON content-type otherwise.
                headers: isForm
                    ? { password: PASSWORD }
                    : { password: PASSWORD, "Content-Type": "application/json" },
            };
            if (ctrl) opts.signal = ctrl.signal;
            if (body) opts.body = isForm ? body : JSON.stringify(body);
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
            const aborted = err && err.name === "AbortError";
            console.error("admin api error:", err);
            return {
                ok: false,
                status: 0,
                data: { message: aborted ? "Request timed out." : String(err && err.message || err) },
            };
        } finally {
            if (timer) clearTimeout(timer);
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
            // Phase 1: fetch the panel list WITHOUT status. This returns
            // almost instantly (no Pterodactyl calls) so the cards appear
            // right away instead of waiting for the slowest panel.
            const res = await api("/api/panels?status=0");
            if (!res || !res.ok) {
                setStatus("Disconnected", "error");
                if (!silent) {
                    const msg = (res && res.data && res.data.message) || "Failed to load panels.";
                    showToast(msg, "error");
                }
                return;
            }

            const panels = (res.data && res.data.panels) || [];
            renderGrid(panels);
            renderSummary(panels);
            setStatus("Online", "ok");

            // Phase 2: fetch each panel's live status independently, in
            // parallel. Fast panels update immediately; one slow/unreachable
            // panel no longer blocks the rest.
            fetchAllStatuses(panels);
        } finally {
            __inFlight = false;
        }
    }

    // Fetch live status for every panel in parallel and patch each card
    // as its response lands. Updates the summary once all settle.
    function fetchAllStatuses(panels) {
        if (!panels.length) return;
        const merged = panels.map(p => ({ ...p }));
        const byId = new Map(merged.map(p => [p.id, p]));

        const jobs = panels.map(p =>
            api("/api/panels/status", { method: "POST", body: { id: p.id } }).then(res => {
                let full;
                if (res && res.ok && res.data && res.data.panel) {
                    full = res.data.panel;
                } else {
                    // The status fetch failed (timeout, network, 5xx, …).
                    // Mark the card unreachable instead of leaving it stuck
                    // on "Checking…" forever.
                    const msg = (res && res.data && res.data.message) || "Status request failed.";
                    full = { ...p, status: { reachable: false, error: msg } };
                }
                const slot = byId.get(p.id);
                if (slot) Object.assign(slot, full);
                upsertCard(full);
            }).catch(err => {
                // Defensive: even an unexpected throw shouldn't strand a card.
                const full = {
                    ...p,
                    status: { reachable: false, error: String(err && err.message || err) },
                };
                const slot = byId.get(p.id);
                if (slot) Object.assign(slot, full);
                upsertCard(full);
            })
        );

        Promise.allSettled(jobs).then(() => renderSummary(merged));
    }

    function renderSummary(panels) {
        let total = panels.length;
        let running = 0;
        let offline = 0;
        let unreachable = 0;
        for (const p of panels) {
            if (p.status == null) continue;   // status not in yet — don't miscount
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
            __cardSig.clear();
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

        const wanted = new Set(panels.map(p => String(p.id)));

        // Remove cards (and stale signatures) for panels that no longer exist.
        grid.querySelectorAll(".adm-card[data-panel-id]").forEach(card => {
            if (!wanted.has(card.dataset.panelId)) {
                __cardSig.delete(card.dataset.panelId);
                card.remove();
            }
        });
        // Drop the empty-state placeholder if it's lingering.
        const placeholder = grid.querySelector(".adm-empty, .adm-loading");
        if (placeholder) placeholder.remove();

        // Upsert each panel in order, only touching the DOM when changed.
        let prev = null;
        for (const p of panels) {
            const node = upsertCard(p, /*reorderAfter*/ prev);
            if (node) prev = node;
        }
    }

    // Build a compact signature of everything that affects a card's render.
    function cardSignature(p) {
        const s = p.status || {};
        return [
            p.name, p.id, p.panelUrl, p.serverId, p.configured ? 1 : 0,
            p.status == null ? "?" : "1",
            s.reachable ? 1 : 0, s.state || "",
            Math.round(Number(s.cpuAbsolute || 0) * 10),
            s.memoryBytes || 0, s.diskBytes || 0,
            Math.round(Number(s.uptimeMs || 0) / 1000),
            s.error || "",
        ].join("|");
    }

    // Insert or update a single card in place. Returns the card element.
    function upsertCard(p, reorderAfter) {
        const grid = document.getElementById("adm-grid");
        if (!grid) return null;
        const id = String(p.id);
        const sig = cardSignature(p);
        const existing = grid.querySelector(`.adm-card[data-panel-id="${cssEscape(id)}"]`);

        if (existing && __cardSig.get(id) === sig) {
            return existing; // nothing changed — leave the DOM untouched
        }

        const html = renderCard(p);
        if (existing) {
            existing.outerHTML = html;
        } else {
            grid.insertAdjacentHTML("beforeend", html);
        }
        __cardSig.set(id, sig);
        return grid.querySelector(`.adm-card[data-panel-id="${cssEscape(id)}"]`);
    }

    // Minimal CSS attribute-value escaper (panel ids are short/simple, but
    // be safe against quotes/backslashes in selectors).
    function cssEscape(s) {
        return String(s).replace(/["\\]/g, "\\$&");
    }

    function renderCard(p) {
        const hasStatus = p.status != null;          // phase-2 data arrived?
        const s = p.status || {};
        const reachable = !!s.reachable;
        let stateKey;
        if (!p.configured) {
            stateKey = "unreachable";
        } else if (!hasStatus) {
            stateKey = "checking";                    // still waiting on phase 2
        } else {
            stateKey = (s.state || (reachable ? "unknown" : "unreachable")).toLowerCase();
        }
        const meta = STATE_META[stateKey] || STATE_META.unknown;

        const cpu     = Number(s.cpuAbsolute || 0);
        const memory  = formatBytes(s.memoryBytes || 0);
        const disk    = formatBytes(s.diskBytes || 0);
        const uptime  = formatUptimeMs(s.uptimeMs || 0);

        const errorBlock = (hasStatus && !reachable && p.configured)
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

        const sig = cardSignature(p);
        return `
            <article class="adm-card" data-panel-id="${escapeHtml(p.id)}" data-sig="${escapeHtml(sig)}">
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

    // Event delegation: bound once on the grid container, so re-rendering
    // individual cards never leaves us re-attaching handlers (or leaking).
    function bindCardActions() {
        const grid = document.getElementById("adm-grid");
        if (!grid || grid.__powerBound) return;
        grid.__powerBound = true;
        grid.addEventListener("click", e => {
            const btn = e.target.closest(".adm-power-btn");
            if (!btn || !grid.contains(btn)) return;
            const card   = btn.closest(".adm-card");
            const id     = card && card.dataset.panelId;
            const signal = btn.dataset.signal;
            if (!id || !signal) return;
            powerPanel(id, signal, btn);
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
     * Upload file to all panels
     * =================================================================== */
    function openUploadModal() {
        const m = document.getElementById("adm-upload-modal");
        if (!m) return;
        m.classList.remove("adm-hidden");
        document.body.style.overflow = "hidden";
        const results = document.getElementById("adm-upload-results");
        if (results) { results.classList.add("adm-hidden"); results.innerHTML = ""; }
        loadUploadTargets();
    }

    function closeUploadModal() {
        const m = document.getElementById("adm-upload-modal");
        if (!m) return;
        m.classList.add("adm-hidden");
        document.body.style.overflow = "";
    }

    // Populate the per-panel checkbox list from the current config.
    async function loadUploadTargets() {
        const listEl = document.getElementById("adm-upload-target-list");
        if (!listEl) return;
        listEl.innerHTML = '<div class="adm-loading">Loading panels…</div>';
        const res = await api("/api/panels?status=0");
        const panels = (res && res.ok && res.data && res.data.panels) || [];
        if (!panels.length) {
            listEl.innerHTML = '<div class="adm-empty"><strong>No panels configured.</strong></div>';
            return;
        }
        listEl.innerHTML = panels.map(p => `
            <label class="adm-upload-target">
                <input type="checkbox" class="adm-upload-target-cb" value="${escapeHtml(p.id)}"
                       ${p.configured ? "checked" : "disabled"}>
                <span>${escapeHtml(p.name)} <span class="adm-upload-target-id">${escapeHtml(p.id)}</span></span>
                ${p.configured ? "" : '<span class="adm-upload-target-warn">not configured</span>'}
            </label>`).join("");
    }

    async function sendUpload() {
        const fileInput = document.getElementById("adm-upload-file");
        const pathInput = document.getElementById("adm-upload-path");
        const allCb = document.getElementById("adm-upload-all");
        const decompressCb = document.getElementById("adm-upload-decompress");
        const deleteCb = document.getElementById("adm-upload-delete");
        const btn = document.getElementById("adm-upload-send");
        const results = document.getElementById("adm-upload-results");

        const file = fileInput && fileInput.files && fileInput.files[0];
        if (!file) { showToast("Choose a file first.", "error"); return; }

        const path = (pathInput && pathInput.value || "").trim();
        if (!path) { showToast("Enter a destination path.", "error"); return; }

        const decompress = !!(decompressCb && decompressCb.checked);
        const deleteArchive = !!(deleteCb && deleteCb.checked);

        // Resolve target ids (null = server treats as all).
        let ids = null;
        if (!(allCb && allCb.checked)) {
            ids = Array.from(document.querySelectorAll(".adm-upload-target-cb:checked"))
                .map(cb => cb.value);
            if (!ids.length) { showToast("Select at least one panel.", "error"); return; }
        }

        const original = btn ? btn.innerHTML : null;
        if (btn) { btn.disabled = true; btn.innerHTML = "<span>⏳</span> Uploading…"; }

        // Send as multipart/form-data: streams the file as binary so we
        // avoid base64 (which inflates size ~33% and can freeze the tab
        // on large files).
        const fd = new FormData();
        fd.append("file", file, file.name);
        fd.append("path", path);
        fd.append("decompress", decompress ? "true" : "false");
        fd.append("deleteArchive", deleteArchive ? "true" : "false");
        if (ids) fd.append("ids", JSON.stringify(ids));

        const res = await api("/api/panels/upload", {
            method: "POST",
            body: fd,
            // Extraction can take a while across many panels — allow longer.
            timeoutMs: decompress ? 120_000 : 60_000,
        });

        if (btn) { btn.disabled = false; btn.innerHTML = original; }

        if (!res || !res.ok || !res.data) {
            const msg = (res && res.data && res.data.message) || "Upload failed.";
            showToast(msg, "error");
            return;
        }

        const d = res.data;
        if (results) {
            results.classList.remove("adm-hidden");
            const rows = (d.results || []).map(r => `
                <div class="adm-upload-result-row ${r.ok ? "is-ok" : "is-err"}">
                    <span class="adm-upload-result-icon">${r.ok ? "✓" : "✕"}</span>
                    <span class="adm-upload-result-name">${escapeHtml(r.name)} <span class="adm-upload-target-id">${escapeHtml(r.id)}</span></span>
                    <span class="adm-upload-result-msg">${escapeHtml(r.message || "")}</span>
                </div>`).join("");
            results.innerHTML = `
                <div class="adm-upload-result-head">
                    Wrote <code>${escapeHtml(d.dest || path)}</code>
                    (${escapeHtml(String(d.size))} bytes)${d.decompress ? " + unarchive" : ""} — ${escapeHtml(String(d.okCount))}/${escapeHtml(String(d.total))} ok
                </div>${rows}`;
        }

        showToast(`Upload: ${d.okCount}/${d.total} panel(s) ok.`,
                  d.okCount === d.total ? "success" : "warn");
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

        const uploadBtn = document.getElementById("adm-upload-btn");
        if (uploadBtn) uploadBtn.addEventListener("click", openUploadModal);

        document.querySelectorAll("[data-upload-close]").forEach(el => {
            el.addEventListener("click", closeUploadModal);
        });

        const uploadAll = document.getElementById("adm-upload-all");
        const uploadList = document.getElementById("adm-upload-target-list");
        if (uploadAll && uploadList) {
            uploadAll.addEventListener("change", () => {
                uploadList.classList.toggle("adm-hidden", uploadAll.checked);
            });
        }

        const uploadSend = document.getElementById("adm-upload-send");
        if (uploadSend) uploadSend.addEventListener("click", sendUpload);

        const uploadDecompress = document.getElementById("adm-upload-decompress");
        const uploadDeleteWrap = document.getElementById("adm-upload-delete-wrap");
        if (uploadDecompress && uploadDeleteWrap) {
            uploadDecompress.addEventListener("change", () => {
                uploadDeleteWrap.style.display = uploadDecompress.checked ? "" : "none";
            });
        }

        document.querySelectorAll("[data-modal-close]").forEach(el => {
            el.addEventListener("click", closeSettingsModal);
        });
        document.addEventListener("keydown", e => {
            if (e.key === "Escape") { closeSettingsModal(); closeUploadModal(); }
        });

        const addBtn = document.getElementById("adm-add-panel");
        if (addBtn) addBtn.addEventListener("click", addPanel);

        const saveBtn = document.getElementById("adm-settings-save");
        if (saveBtn) saveBtn.addEventListener("click", saveSettings);

        document.querySelectorAll("[data-bulk]").forEach(b => {
            b.addEventListener("click", () => bulkPower(b.dataset.bulk));
        });

        bindCardActions();   // delegate power clicks once for the grid's lifetime
        refresh({ silent: false });   // initial load only — no auto-refresh after this
    });
})();
