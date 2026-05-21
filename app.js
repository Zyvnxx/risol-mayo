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
