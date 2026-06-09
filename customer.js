/*
Customer portal — token-scoped self-service for a single panel.
*/

(() => {
    "use strict";

    const REFRESH_MS = 15_000;
    const TOKEN_KEY = "cust_token";
    const WARN_DAYS = 7;                 // start warning when <= 7 days remain
    const WARN_SEEN_KEY = "cust_warn_seen"; // localStorage: last day we nagged

    let __token = "";
    let __refreshTimer = null;
    let __toastTimer = null;
    let __inFlight = false;
    let __countdownTimer = null;         // 1s ticker for the live countdown
    let __expiresAtMs = null;            // parsed expiry timestamp (ms) or null

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

    /* ===================== expiry / countdown ===================== */

    // Parse the panel's expiresAt (ISO date or datetime, or epoch
    // seconds/ms) into a timestamp in ms, or null when absent/invalid.
    function parseExpiry(raw) {
        if (raw == null || raw === "") return null;
        if (typeof raw === "number") {
            // Treat 10-digit values as epoch seconds, larger as ms.
            return raw < 1e12 ? raw * 1000 : raw;
        }
        const str = String(raw).trim();
        if (/^\d+$/.test(str)) {
            const n = Number(str);
            return n < 1e12 ? n * 1000 : n;
        }
        // Date-only strings expire at end of that day (local midnight + 1d).
        let iso = str;
        if (/^\d{4}-\d{2}-\d{2}$/.test(str)) iso = str + "T23:59:59";
        const t = Date.parse(iso);
        return isNaN(t) ? null : t;
    }

    // "6d 23h 59m 12s" style remaining-time text.
    function formatRemaining(ms) {
        if (ms <= 0) return "Expired";
        const totalSec = Math.floor(ms / 1000);
        const d = Math.floor(totalSec / 86400);
        const h = Math.floor((totalSec % 86400) / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;
        const pad = n => String(n).padStart(2, "0");
        if (d > 0) return `${d}d ${pad(h)}h ${pad(m)}m ${pad(s)}s`;
        return `${pad(h)}h ${pad(m)}m ${pad(s)}s`;
    }

    // Show a once-per-day warning when the panel has <= WARN_DAYS left.
    function maybeWarnExpiry(daysLeft) {
        if (daysLeft == null) return;
        const todayKey = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
        let lastSeen = "";
        try { lastSeen = localStorage.getItem(WARN_SEEN_KEY) || ""; } catch (_) {}
        if (lastSeen === todayKey) return; // already nagged today

        if (daysLeft <= 0) {
            showToast("Your server time has expired. Please renew.", "error");
        } else if (daysLeft <= WARN_DAYS) {
            const dleft = Math.ceil(daysLeft);
            showToast(`Heads up: only ${dleft} day${dleft === 1 ? "" : "s"} left. Please renew soon.`, "warn");
        } else {
            return; // no warning needed, don't mark as seen
        }
        try { localStorage.setItem(WARN_SEEN_KEY, todayKey); } catch (_) {}
    }

    // Re-render the live countdown text + warning banner state. Called
    // every second by __countdownTimer while a panel with expiry is shown.
    function tickCountdown() {
        const valEl = document.getElementById("cust-countdown-value");
        const wrapEl = document.getElementById("cust-countdown");
        if (!valEl || __expiresAtMs == null) return;

        const remaining = __expiresAtMs - Date.now();
        valEl.textContent = formatRemaining(remaining);

        if (wrapEl) {
            wrapEl.classList.remove("is-warn", "is-expired");
            if (remaining <= 0) wrapEl.classList.add("is-expired");
            else if (remaining <= WARN_DAYS * 86400000) wrapEl.classList.add("is-warn");
        }
    }

    function startCountdown(expiresAtMs) {
        __expiresAtMs = expiresAtMs;
        if (__countdownTimer) {
            clearInterval(__countdownTimer);
            __countdownTimer = null;
        }
        if (__expiresAtMs == null) return;
        tickCountdown();
        __countdownTimer = setInterval(tickCountdown, 1000);
        const daysLeft = (__expiresAtMs - Date.now()) / 86400000;
        maybeWarnExpiry(daysLeft);
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
        if (__countdownTimer) {
            clearInterval(__countdownTimer);
            __countdownTimer = null;
        }
        __expiresAtMs = null;
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

        // ---- Expiry / countdown block ----
        const expiresMs = parseExpiry(panel.expiresAt);
        const countdownBlock = (expiresMs != null)
            ? `<div class="cust-countdown" id="cust-countdown">
                   <span class="cust-countdown-label">Time remaining</span>
                   <span class="cust-countdown-value" id="cust-countdown-value">—</span>
               </div>`
            : "";

        // ---- Token / channel editor block ----
        const hasTokenLine = panel.tokenLine != null && panel.tokenLine !== "";
        const tokenBlock = hasTokenLine
            ? `<section class="cust-token-editor">
                   <header class="cust-token-head">
                       <span class="cust-token-head-title">⚙ Bot token &amp; channel</span>
                       <span class="cust-token-current">${escapeHtml(panel.botToken || "not set")}</span>
                   </header>
                   <form class="cust-token-form" id="cust-token-form" autocomplete="off">
                       <label class="adm-field adm-field-wide">
                           <span class="adm-field-label">Bot token</span>
                           <input id="cust-bot-token" class="adm-field-input mono" type="text"
                                  placeholder="leave blank to keep current"
                                  autocomplete="off" spellcheck="false">
                       </label>
                       <label class="adm-field adm-field-wide">
                           <span class="adm-field-label">Channel ID</span>
                           <input id="cust-channel-id" class="adm-field-input mono" type="text"
                                  placeholder="channel id"
                                  value="${escapeHtml(panel.channelId || "")}"
                                  autocomplete="off" spellcheck="false">
                       </label>
                       <button class="adm-btn adm-btn-success cust-token-save" type="submit">
                           <span>💾</span> Save token
                       </button>
                       <p class="cust-token-hint">
                           Changes are written to <span class="mono">tokens.txt</span>.
                           Restart your server afterward for them to apply.
                       </p>
                   </form>
               </section>`
            : "";

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
                ${countdownBlock}

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

                ${tokenBlock}
            </article>`;

        document.querySelectorAll(".cust-power-btn").forEach(btn => {
            btn.addEventListener("click", () => power(btn.dataset.signal, btn));
        });

        const tokenForm = document.getElementById("cust-token-form");
        if (tokenForm) {
            tokenForm.addEventListener("submit", e => {
                e.preventDefault();
                saveToken(tokenForm);
            });
        }

        // Kick off (or restart) the live countdown for this panel.
        startCountdown(expiresMs);
    }

    async function saveToken(form) {
        const tokenInp = document.getElementById("cust-bot-token");
        const chanInp  = document.getElementById("cust-channel-id");
        const btn = form ? form.querySelector(".cust-token-save") : null;
        const botToken = tokenInp ? tokenInp.value.trim() : "";
        const channelId = chanInp ? chanInp.value.trim() : "";

        if (botToken && /\s/.test(botToken)) {
            showToast("Token cannot contain spaces.", "error");
            return;
        }
        if (channelId && /\s/.test(channelId)) {
            showToast("Channel id cannot contain spaces.", "error");
            return;
        }

        const original = btn ? btn.innerHTML : null;
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<span>⏳</span> Saving…`;
        }

        const res = await api("/api/customer/token", {
            method: "POST",
            body: { botToken, channelId },
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
            showToast(res.data.message || "Token saved", "success");
            if (tokenInp) tokenInp.value = "";
            setTimeout(() => refresh({ silent: true }), 800);
        } else {
            showToast(`Save failed: ${res.data.message || "request failed"}`, "error");
        }
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
