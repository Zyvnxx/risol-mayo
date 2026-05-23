/*
Customer portal — token-scoped self-service for a single panel.
*/

(() => {
    "use strict";

    const REFRESH_MS = 15_000;
    const TOKEN_KEY = "cust_token";

    let __token = "";
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

    /* ===================== utilities ===================== */
    function escapeHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

    function formatBytes(bytes) {
        const n = Number(bytes || 0);
        if (!isFinite(n) || n <= 0) return "0";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let i = 0, v = n;
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
        const pill = document.getElementById("cust-status-pill");
        const txt  = document.getElementById("cust-status-text");
        if (txt) txt.textContent = text;
        if (!pill) return;
        pill.classList.remove("is-error", "is-loading");
        if (kind === "error")   pill.classList.add("is-error");
        if (kind === "loading") pill.classList.add("is-loading");
    }

    function showToast(text, kind) {
        const t = document.getElementById("cust-toast");
        const txt = document.getElementById("cust-toast-text");
        if (!t || !txt) return;
        txt.textContent = text;
        t.classList.remove("adm-hidden", "is-success", "is-error", "is-warn");
        if (kind) t.classList.add("is-" + kind);
        if (__toastTimer) clearTimeout(__toastTimer);
        __toastTimer = setTimeout(() => t.classList.add("adm-hidden"), 2800);
    }

    /* ===================== auth ===================== */
    function showLogin(errorMsg) {
        document.getElementById("cust-login").classList.remove("adm-hidden");
        document.getElementById("cust-panel-wrap").classList.add("adm-hidden");
        document.getElementById("cust-refresh-btn").classList.add("adm-hidden");
        document.getElementById("cust-logout-btn").classList.add("adm-hidden");
        const errEl = document.getElementById("cust-login-error");
        if (errorMsg) {
            errEl.textContent = errorMsg;
            errEl.classList.remove("adm-hidden");
        } else {
            errEl.classList.add("adm-hidden");
        }
        setStatus("Sign in", "loading");
        // Focus token field for fast re-entry.
        const inp = document.getElementById("cust-token-input");
        if (inp) inp.focus();
    }

    function showPanel() {
        document.getElementById("cust-login").classList.add("adm-hidden");
        document.getElementById("cust-panel-wrap").classList.remove("adm-hidden");
        document.getElementById("cust-refresh-btn").classList.remove("adm-hidden");
        document.getElementById("cust-logout-btn").classList.remove("adm-hidden");
    }

    function logout(silent) {
        if (!silent && !confirm("Sign out and forget your token?")) return;
        try { localStorage.removeItem(TOKEN_KEY); } catch (_) {}
        __token = "";
        if (__refreshTimer) {
            clearInterval(__refreshTimer);
            __refreshTimer = null;
        }
        showLogin();
    }

    /* ===================== API ===================== */
    async function api(path, { method = "GET", body = null } = {}) {
        try {
            const opts = {
                method,
                headers: {
                    "customer-token": __token,
                    "Content-Type": "application/json",
                },
            };
            if (body) opts.body = JSON.stringify(body);
            const r = await fetch(path, opts);
            const data = await r.json().catch(() => ({}));
            return { ok: r.ok, status: r.status, data };
        } catch (err) {
            console.error("customer api error:", err);
            return { ok: false, status: 0, data: { message: String(err && err.message || err) } };
        }
    }

    async function refresh({ silent = false } = {}) {
        if (!__token) return;
        if (__inFlight) return;
        __inFlight = true;
        if (!silent) setStatus("Refreshing…", "loading");

        try {
            const res = await api("/api/customer/me");
            if (res.status === 401) {
                logout(true);
                showLogin("Token rejected. Please sign in again.");
                return;
            }
            if (!res.ok) {
                setStatus("Disconnected", "error");
                if (!silent) {
                    showToast(res.data.message || "Failed to load.", "error");
                }
                return;
            }

            const panel = res.data.panel;
            if (!panel) {
                setStatus("Disconnected", "error");
                renderPanelError("No panel attached to this token.");
                return;
            }

            renderPanel(panel);
            setStatus("Online", "ok");
        } finally {
            __inFlight = false;
        }
    }

    function renderPanelError(msg) {
        const el = document.getElementById("cust-panel");
        if (!el) return;
        el.innerHTML = `
            <div class="cust-panel-card">
                <div class="cust-panel-error">${escapeHtml(msg)}</div>
            </div>`;
    }

    function renderPanel(panel) {
        const el = document.getElementById("cust-panel");
        if (!el) return;

        const s = panel.status || {};
        const reachable = !!s.reachable;
        const stateKey = (s.state || (reachable ? "unknown" : "unreachable")).toLowerCase();
        const meta = STATE_META[stateKey] || STATE_META.unknown;

        const cpu    = Number(s.cpuAbsolute || 0);
        const memory = formatBytes(s.memoryBytes || 0);
        const disk   = formatBytes(s.diskBytes || 0);
        const uptime = formatUptimeMs(s.uptimeMs || 0);

        const errorBlock = (!reachable && panel.configured)
            ? `<div class="cust-panel-error">⚠ ${escapeHtml(s.error || "Server unreachable")}</div>`
            : "";
        const unconfiguredBlock = (!panel.configured)
            ? `<div class="cust-panel-error">⚠ Server is not fully configured. Contact admin.</div>`
            : "";

        const isRunning = stateKey === "running" || stateKey === "starting";
        const startDisabled   = !panel.configured || isRunning ? "disabled" : "";
        const stopDisabled    = !panel.configured || stateKey === "offline" ? "disabled" : "";
        const restartDisabled = !panel.configured ? "disabled" : "";

        el.innerHTML = `
            <article class="cust-panel-card">
                <header class="cust-panel-header">
                    <div>
                        <h2 class="cust-panel-name">${escapeHtml(panel.name || panel.id)}</h2>
                        <div class="cust-panel-id">${escapeHtml(panel.id)}</div>
                    </div>
                    <span class="adm-state ${meta.cls}">
                        <span class="adm-state-dot"></span>${escapeHtml(meta.txt)}
                    </span>
                </header>

                ${unconfiguredBlock}
                ${errorBlock}

                <div class="cust-panel-stats">
                    <div class="cust-panel-stat">
                        <span class="cust-panel-stat-label">CPU</span>
                        <span class="cust-panel-stat-value">${cpu.toFixed(1)}%</span>
                    </div>
                    <div class="cust-panel-stat">
                        <span class="cust-panel-stat-label">RAM</span>
                        <span class="cust-panel-stat-value">${escapeHtml(memory)}</span>
                    </div>
                    <div class="cust-panel-stat">
                        <span class="cust-panel-stat-label">Disk</span>
                        <span class="cust-panel-stat-value">${escapeHtml(disk)}</span>
                    </div>
                    <div class="cust-panel-stat">
                        <span class="cust-panel-stat-label">Uptime</span>
                        <span class="cust-panel-stat-value">${escapeHtml(uptime)}</span>
                    </div>
                </div>

                <div class="cust-panel-actions">
                    <button class="adm-btn adm-btn-success cust-power-btn" data-signal="start" ${startDisabled}>
                        <span>▶</span> Start
                    </button>
                    <button class="adm-btn adm-btn-warn cust-power-btn" data-signal="restart" ${restartDisabled}>
                        <span>↻</span> Restart
                    </button>
                    <button class="adm-btn adm-btn-danger cust-power-btn" data-signal="stop" ${stopDisabled}>
                        <span>■</span> Stop
                    </button>
                </div>
            </article>`;

        document.querySelectorAll(".cust-power-btn").forEach(btn => {
            btn.addEventListener("click", () => power(btn.dataset.signal, btn));
        });
    }

    async function power(signal, btn) {
        const verbs = { start: "Start", stop: "Stop", restart: "Restart" };
        const verb = verbs[signal] || signal;

        if (signal === "stop") {
            if (!confirm("Stop your server?")) return;
        }

        const original = btn ? btn.innerHTML : null;
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<span>⏳</span> ${verb}ing…`;
        }

        const res = await api("/api/customer/power", {
            method: "POST",
            body: { signal },
        });

        if (btn) {
            btn.disabled = false;
            btn.innerHTML = original;
        }

        if (res.status === 401) {
            logout(true);
            showLogin("Session expired. Please sign in again.");
            return;
        }

        if (res.ok && res.data && res.data.status === "success") {
            showToast(`${verb} sent`, "success");
            setTimeout(() => refresh({ silent: true }), 1500);
        } else {
            showToast(`${verb} failed: ${res.data.message || "request failed"}`, "error");
        }
    }

    /* ===================== boot ===================== */
    function tryLoginWith(token) {
        __token = token.trim();
        if (!__token) {
            showLogin("Token is required.");
            return;
        }
        try { localStorage.setItem(TOKEN_KEY, __token); } catch (_) {}
        showPanel();
        refresh({ silent: false }).then(() => {
            // If the token was rejected, refresh() will have called logout
            // and showLogin already, so nothing else to do here.
        });

        if (!__refreshTimer) {
            __refreshTimer = setInterval(() => {
                if (typeof document !== "undefined" && document.hidden) return;
                refresh({ silent: true });
            }, REFRESH_MS);
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        // Login form submit
        const form = document.getElementById("cust-login-form");
        if (form) {
            form.addEventListener("submit", e => {
                e.preventDefault();
                const inp = document.getElementById("cust-token-input");
                if (!inp) return;
                tryLoginWith(inp.value);
            });
        }

        const refreshBtn = document.getElementById("cust-refresh-btn");
        if (refreshBtn) refreshBtn.addEventListener("click", () => refresh({ silent: false }));

        const logoutBtn = document.getElementById("cust-logout-btn");
        if (logoutBtn) logoutBtn.addEventListener("click", () => logout(false));

        document.addEventListener("visibilitychange", () => {
            if (!document.hidden && __token) refresh({ silent: true });
        });

        // Auto-login if a token was stored from a previous session.
        let stored = "";
        try { stored = localStorage.getItem(TOKEN_KEY) || ""; } catch (_) {}
        if (stored) {
            tryLoginWith(stored);
        } else {
            showLogin();
        }
    });
})();
