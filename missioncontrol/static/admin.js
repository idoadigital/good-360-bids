// Mission Control admin — frontend.
// Vanilla JS, no build step. Talks to /api/auth/* and /api/admin/* on same origin.

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

let CURRENT_USER = null;

async function api(path, opts = {}) {
    const res = await fetch(path, {
        credentials: 'same-origin',
        headers: opts.body ? {'Content-Type': 'application/json', ...(opts.headers || {})} : opts.headers,
        ...opts,
    });
    if (res.status === 401) { window.location = '/login'; throw new Error('unauth'); }
    return res;
}

// ---------------- Tabs ----------------

const TAB_TITLES = {
    scans: 'Scans',
    purchases: 'Purchases',
    notifications: 'Notifications',
    analytics: 'Analytics',
    customers: 'Customers',
    'customer-detail': 'Customer',
    users: 'Users',
    settings: 'Settings',
    testbuy: 'Test buy',
    'test-detail': 'Test run',
    liveview: 'Live View',
    'product-detail': 'Product',
    audit: 'Audit',
    import: 'Import CSV',
};

// Sub-pages reachable from a parent panel — keep the parent highlighted in
// the sidebar while the child is active (e.g. Import is a child of Settings).
const PARENT_TABS = {
    import: 'settings',
    'customer-detail': 'customers',
    'test-detail': 'testbuy',
    'product-detail': 'scans',
};

// View Transitions: when supported, wrapping the DOM mutation in
// document.startViewTransition() makes the browser cross-fade the
// outgoing and incoming panel using snapshots. Falls through to the
// existing CSS fadeUp keyframe on browsers without the API.
const _prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

// ---- Scroll-driven + cascade section reveal --------------------------
// Top-level panel sections start hidden (CSS, gated on html.motion-ready)
// and become visible when `.is-revealed` is added. The first time you
// land on a panel, sections above the fold cascade in with a stagger;
// anything below the fold is handed off to IntersectionObserver and
// reveals as you scroll to it. Returning to the same panel skips the
// cascade (already-revealed nodes keep their state) so re-navigation
// doesn't feel busy.
const REVEAL_SELECTOR = '.panel-header, .metric-strip, .surface-card';
const _revealedPanels = new Set();

const _scrollRevealObserver = ('IntersectionObserver' in window)
    ? new IntersectionObserver((entries) => {
        for (const e of entries) {
            if (e.isIntersecting) {
                e.target.classList.add('is-revealed');
                _scrollRevealObserver.unobserve(e.target);
            }
        }
      }, { threshold: 0.05, rootMargin: '0px 0px -8% 0px' })
    : null;

function _cascadeRevealPanel(panel) {
    // Run after the browser has laid out the (possibly newly-active) panel
    // so getBoundingClientRect returns accurate coordinates.
    requestAnimationFrame(() => {
        const targets = [...panel.querySelectorAll(REVEAL_SELECTOR)];
        const vh = window.innerHeight;
        let cascadeIdx = 0;
        targets.forEach((el) => {
            if (el.classList.contains('is-revealed')) return;
            const rect = el.getBoundingClientRect();
            const aboveFold = rect.top < vh * 0.92;
            if (aboveFold) {
                // Cap the stagger so content-heavy panels don't drag on.
                const delay = Math.min(cascadeIdx++, 6) * 70;
                setTimeout(() => el.classList.add('is-revealed'), delay);
            } else if (_scrollRevealObserver) {
                _scrollRevealObserver.observe(el);
            } else {
                el.classList.add('is-revealed');
            }
        });
    });
}

function revealActivePanelSections(opts = {}) {
    const panel = document.querySelector('.panel.active');
    if (!panel) return;
    const name = panel.dataset.panel;
    const forceCascade = !!opts.forceCascade;
    if (forceCascade || !_revealedPanels.has(name)) {
        _revealedPanels.add(name);
        _cascadeRevealPanel(panel);
    } else {
        // Repeat visit: surface everything immediately (no cascade, but
        // also re-attach the scroll observer to anything still pending).
        panel.querySelectorAll(REVEAL_SELECTOR).forEach(el => {
            if (!el.classList.contains('is-revealed')) {
                el.classList.add('is-revealed');
            }
        });
    }
}

function _runTabSwitch(name) {
    const parent = PARENT_TABS[name] || name;
    // Active state is a sidebar concept; only mutate sidebar items.
    $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.tab === parent));
    $$('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
    $('#crumbCurrent').textContent = TAB_TITLES[name] || name;
    loaders[name] && loaders[name]();
}

// Programmatic navigation helper for code paths that activate a panel
// outside the sidebar tab-click flow (customer-detail, test-detail,
// product-detail, the back-button from product-detail). Critical to call
// `revealActivePanelSections()` here: without it the panel becomes
// `.active` but its `.surface-card` children stay at opacity:0 forever
// (the motion-ready reveal pre-state) and the page looks empty.
function activatePanelDirect(name, crumb) {
    const parent = PARENT_TABS[name] || name;
    $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.tab === parent));
    $$('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
    $('#crumbCurrent').textContent = crumb || TAB_TITLES[name] || name;
    revealActivePanelSections({ forceCascade: true });
}

function switchTab(name) {
    if (document.startViewTransition && !_prefersReducedMotion.matches) {
        const t = document.startViewTransition(() => _runTabSwitch(name));
        // Run the section cascade once the cross-fade has actually finished
        // so the stagger plays on the live DOM, not under a snapshot.
        t.finished.then(() => revealActivePanelSections()).catch(() => {});
    } else {
        _runTabSwitch(name);
        revealActivePanelSections();
    }
}

$$('.tab').forEach(t => t.addEventListener('click', (e) => {
    switchTab(t.dataset.tab);
    // Drop focus on pointer clicks so the sidebar's :focus-within rule
    // releases and the rail collapses back to its 72px resting width.
    // Keyboard activations (Enter/Space, detail === 0) keep focus so
    // keyboard users don't lose their place in the nav.
    if (e.detail !== 0 && typeof e.currentTarget.blur === 'function') {
        e.currentTarget.blur();
    }
}));

// ---------------- Boot ----------------

(async () => {
    const state = await api('/api/auth/state').then(r => r.json());
    if (!state.authenticated) { window.location = '/login'; return; }
    CURRENT_USER = state.user;

    $('#userEmail').textContent = state.user.email;
    $('#userRole').textContent = state.user.role.replace('_', ' ');
    $('#userAvatar').textContent = (state.user.email || '?').charAt(0);

    // Show/hide super-admin only controls.
    const isSuper = state.user.role === 'super_admin';
    $$('.super-only').forEach(el => el.dataset.hidden = isSuper ? 'false' : 'true');

    applySandboxBanner(state.sandbox_mode);

    loadScans();
    loadSidebarBadges();
    loadSystemStatus();
    setInterval(loadSystemStatus, 30_000);
    // Cheap re-check so a toggle from another tab/operator updates the banner
    // even on long-lived dashboard sessions.
    setInterval(refreshSandboxBanner, 60_000);

    // Kick off the initial section cascade on the default active panel.
    revealActivePanelSections({ forceCascade: true });
})();

// ---------------- Sandbox banner ----------------

function applySandboxBanner(active) {
    const banner = $('#sandboxBanner');
    if (!banner) return;
    document.body.dataset.sandbox = active ? 'on' : 'off';
    banner.hidden = !active;
}

async function refreshSandboxBanner() {
    try {
        const s = await fetch('/api/auth/state', {credentials: 'same-origin'}).then(r => r.json());
        applySandboxBanner(!!s.sandbox_mode);
    } catch { /* network blip — leave the banner as-is */ }
}

$('#logoutBtn').addEventListener('click', async () => {
    await api('/api/auth/logout', {method: 'POST'});
    window.location = '/login';
});

// ---------------- System status (top-right pill) ----------------

async function loadSystemStatus() {
    try {
        const r = await api('/api/status').then(r => r.json());
        const monitor = r.monitor || {};
        const sys = r.system || {};
        const dot = $('#systemDot');
        const text = $('#systemText');
        if (sys.status === 'healthy') {
            dot.className = 'dot';
            text.textContent = `system online · ${sys.uptime_hours ?? 0}h`;
        } else if (sys.status === 'degraded') {
            dot.className = 'dot warn';
            text.textContent = 'system degraded';
        } else {
            dot.className = 'dot err';
            text.textContent = 'system down';
        }
    } catch {
        $('#systemDot').className = 'dot err';
        $('#systemText').textContent = 'unreachable';
    }
}

// ---------------- Restart system button ----------------

$('#restartBtn').addEventListener('click', async () => {
    const ok = window.confirm(
        'Restart all services?\n\n' +
        'Scanner services (monitor, daemon, watchdog, telegram-bot, intake) ' +
        'cycle first, then this dashboard restarts itself. ' +
        'The page will be unreachable for ~10 seconds.'
    );
    if (!ok) return;

    const btn = $('#restartBtn');
    const label = $('#restartBtnLabel');
    btn.disabled = true;
    label.textContent = 'Restarting…';
    $('#systemDot').className = 'dot warn';
    $('#systemText').textContent = 'restarting…';

    try {
        const r = await api('/api/admin/restart', {method: 'POST', body: '{}'}).then(r => r.json());
        if (!r.ok) throw new Error(r.reason || 'restart failed');
    } catch (err) {
        // The self-restart kills this response mid-flight by design, which
        // throws here. That's expected — start polling for the dashboard
        // to come back up.
        console.warn('restart POST ended (expected if self-restart fired):', err);
    }

    // Poll until /api/status responds healthy again, then reload so badges
    // and scan tables re-fetch against the fresh backend.
    const started = Date.now();
    const POLL_TIMEOUT_MS = 90_000;
    const tick = async () => {
        try {
            const s = await fetch('/api/status', {credentials: 'same-origin'}).then(r => r.json());
            if (s?.system?.status === 'healthy') { window.location.reload(); return; }
        } catch { /* still down — keep polling */ }
        if (Date.now() - started > POLL_TIMEOUT_MS) {
            label.textContent = 'Restart timed out';
            btn.disabled = false;
            return;
        }
        setTimeout(tick, 2000);
    };
    setTimeout(tick, 4000);
});

// ---------------- Sidebar badges ----------------

async function loadSidebarBadges() {
    try {
        const s = await api('/api/admin/scans?count_only=1').then(r => r.json());
        $('#scansBadge').textContent = s.data?.count ?? 0;
    } catch {}
    try {
        const p = await api('/api/admin/purchases?limit=1000').then(r => r.json());
        $('#purchasesBadge').textContent = (p.data || []).length;
    } catch {}
    try {
        const c = await api('/api/admin/customers').then(r => r.json());
        $('#customersBadge').textContent = (c.data || []).length;
    } catch {}
    try {
        const n = await api('/api/admin/notifications?limit=1').then(r => r.json());
        const sum = n.summary || {};
        const errs = (sum.error || 0);
        const warns = (sum.warn || 0);
        const badge = $('#notificationsBadge');
        if (errs)        { badge.textContent = errs; badge.style.color = 'var(--err)'; }
        else if (warns)  { badge.textContent = warns; badge.style.color = 'var(--warn)'; }
        else             { badge.textContent = (n.data || []).length || '—'; badge.style.color = ''; }
    } catch {}
}

// ---------------- Scans ----------------

// State for the live-check view across refreshes.
const SCAN_STATE = {
    checks: [],          // grouped check objects (all of them)
    checkPage: 0,        // page index into the *filtered* list, newest-first
    checkFilter: '',     // '', 'found', 'no-new', 'error'
    scans: [],           // detected-truck rows (raw)
    scansPage: 0,        // page index into `scans`, newest-first
    perPage: 10,
    expandedChecks: new Set(),  // start_iso of expanded checks (so refresh keeps them open)
    expandedScans: new Set(),   // index keys for expanded detected-truck rows
    lastHbIso: null,            // ISO string of most recent heartbeat we've seen
    lastHbAtMs: null,           // wall-clock ms when we *received* that heartbeat
    intervalSec: null,          // inferred between-scan interval
};

function filteredChecks() {
    const f = SCAN_STATE.checkFilter;
    if (!f) return SCAN_STATE.checks;
    return SCAN_STATE.checks.filter(c => c.outcome === f);
}

async function loadScans() {
    // Pull scan summary, log tail, roster queue, login telemetry, and the
    // per-scan screenshot buckets in parallel.
    const [r1, r2, r3, r4] = await Promise.all([
        api('/api/admin/scans?limit=200&slim=1').then(r => r.json()),
        api('/api/admin/scans/log-tail?n=200').then(r => r.json()).catch(() => ({data: {lines: []}})),
        api('/api/admin/roster/queue').then(r => r.json()).catch(() => ({data: {}})),
        api('/api/admin/login-attempts?limit=10').then(r => r.json()).catch(() => ({data: [], summary: {}})),
        fetchScanShotBuckets(),
        fetchScanCaptureBuckets(),
    ]);
    renderRoster(r3.data || {});
    renderLoginHealth(r4.data || [], r4.summary || {});
    const data = r1.data || {};
    const scans = (data.scans || []);
    const hb = data.heartbeat;
    const services = data.services || [];
    const tail = (r2.data || {});

    SCAN_STATE.scans = scans;

    // ---- Metric strip ----
    const hbIso = hb?.last_success || hb?.last_scan || (scans.length ? scans[scans.length - 1].time : null);
    if (hbIso) {
        const ago = secondsSince(hbIso);
        $('#metricLastScan').textContent = formatRelTime(ago) + ' ago';
        $('#metricLastScanMeta').textContent = formatHumanTime(hbIso);
        $('#hbStatus').textContent = `heartbeat: ${formatRelTime(ago)} ago · ${formatHumanTime(hbIso)}`;
        // Track new heartbeats so the countdown can re-zero from this point.
        if (hbIso !== SCAN_STATE.lastHbIso) {
            const prev = SCAN_STATE.lastHbIso;
            if (prev) {
                // Clamp the observed gap to [15s, 120s]. A monitor restart
                // leaves a 5-minute gap between the last pre-restart
                // heartbeat and the first post-restart one — that lone
                // sample used to poison the EWMA up to "next check in 300s"
                // even though normal cadence is 30. Hard cap prevents
                // restart events from leaking into the inference.
                const rawDelta = secondsSince(prev) - secondsSince(hbIso);
                const delta = Math.max(15, Math.min(120, rawDelta));
                // Higher learning rate (0.6 weight on new sample) so the
                // EWMA recovers in 2–3 scans after any outlier instead of
                // dozens.
                SCAN_STATE.intervalSec = SCAN_STATE.intervalSec
                    ? Math.round(SCAN_STATE.intervalSec * 0.4 + delta * 0.6)
                    : delta;
            }
            SCAN_STATE.lastHbIso = hbIso;
            SCAN_STATE.lastHbAtMs = Date.now();
        }
    } else {
        $('#metricLastScan').textContent = '—';
        $('#metricLastScanMeta').textContent = 'no scans yet';
        $('#hbStatus').textContent = 'heartbeat: no data';
    }
    tickCountdown();

    const errCount = tail.error_count || 0;
    $('#metricErrorCount').textContent = errCount;
    const errCard = $('#metricErrorCount').closest('.metric-card');
    if (errCount > 0) {
        errCard.classList.add('primary');
        $('#metricErrorMeta').textContent = `${tail.warn_count || 0} warnings · ${tail.ok_count || 0} successes`;
    } else if ((tail.total || 0) === 0) {
        $('#metricErrorMeta').textContent = 'no log activity yet';
    } else {
        $('#metricErrorMeta').textContent = `${tail.total} lines · clean`;
    }

    const withLogin = scans.filter(s => s.login_ok != null);
    if (withLogin.length) {
        const ok = withLogin.filter(s => s.login_ok === true).length;
        const pct = Math.round((ok / withLogin.length) * 100);
        $('#metricLoginHealth').textContent = pct + '%';
        $('#metricLoginMeta').textContent = `${ok}/${withLogin.length} logins ok`;
    } else {
        $('#metricLoginHealth').textContent = '—';
        $('#metricLoginMeta').textContent = 'no login telemetry yet';
    }

    // ---- Services grid ----
    const sg = $('#servicesGrid');
    if (services.length) {
        sg.innerHTML = `<div class="services-grid">${services.map(s => {
            const cls = s.running ? 'ok' : (s.state === 'unknown' ? 'idle' : 'err');
            const labelTxt = s.running
                ? (s.health === 'unhealthy' ? 'running (unhealthy)' : 'running')
                : (s.state || 'stopped');
            return `<div class="service-chip">
                <span class="dot-${cls}"></span>
                <span class="svc-name">${escape(s.name)}</span>
                <span class="svc-state">${escape(labelTxt)}</span>
            </div>`;
        }).join('')}</div>`;
    } else {
        sg.innerHTML = `<em style="color:var(--text-mute)">docker introspection unavailable</em>`;
    }

    // ---- Live log tail (grouped into structured per-check rows) ----
    const lines = tail.lines || [];
    SCAN_STATE.checks = groupChecks(lines);
    // Newest first, matching scrollTop=bottom semantics of the old <pre>.
    SCAN_STATE.checks.reverse();
    if (SCAN_STATE.checkPage >= Math.max(1, Math.ceil(SCAN_STATE.checks.length / SCAN_STATE.perPage))) {
        SCAN_STATE.checkPage = 0;
    }
    renderCheckList();
    const shown = filteredChecks().length;
    const total = SCAN_STATE.checks.length;
    $('#tailMeta').textContent = total
        ? (SCAN_STATE.checkFilter
            ? `${shown} of ${total} checks · ${tail.error_count || 0} err · ${tail.warn_count || 0} warn`
            : `${total} check${total === 1 ? '' : 's'} · ${tail.error_count || 0} err · ${tail.warn_count || 0} warn · ${tail.ok_count || 0} ok`)
        : 'no log activity';

    // ---- Detected trucks table (paginated, expandable) ----
    if (SCAN_STATE.scansPage >= Math.max(1, Math.ceil(SCAN_STATE.scans.length / SCAN_STATE.perPage))) {
        SCAN_STATE.scansPage = 0;
    }
    renderScansTable();

    // Products refresh on its own slower cadence (see PRODUCTS_REFRESH_MS).
    // Skipping it here keeps the 5-second scan refresh from flashing
    // the products table to "Loading…" every tick.
    maybeRefreshProducts();
}

// ---------------- Grouping log lines into per-check events ----------------

// Lines coming from the monitor are stdout chunks. A "check" begins with
// "Checking Good360 Amazon truckloads..." and ends with "Result: ..." or just
// before the next start. We group them so the UI can show one collapsible
// row per scan instead of a wall of raw text.

function groupChecks(lines) {
    const checks = [];
    let cur = null;
    const startRe = /Checking Good360 Amazon truckloads/i;
    const tsRe = /^\s*\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]?/;

    function startCheck(line) {
        const m = line.match(tsRe);
        cur = {
            startedIso: m ? m[1].replace(' ', 'T') : null,
            startedRaw: m ? m[1] : null,
            lines: [],
            tracked: 0,
            excluded: 0,
            available: 0,
            errors: 0,
            warnings: 0,
            outcome: 'running',
        };
        checks.push(cur);
    }

    for (const l of lines) {
        const text = l.line || '';
        if (startRe.test(text)) {
            startCheck(text);
        }
        if (!cur) {
            // Pre-amble line before any check started — synthesize a holding bucket.
            startCheck(text);
        }
        cur.lines.push(l);
        if (l.severity === 'error') cur.errors++;
        else if (l.severity === 'warn') cur.warnings++;
        if (/\[TRACKED\]/.test(text)) cur.tracked++;
        if (/\[EXCLUDED\]|\[skipped\]/i.test(text)) cur.excluded++;
        if (/-> AVAILABLE/.test(text)) cur.available++;
        if (/Check complete - no new trucks|No new available tracked/i.test(text)) cur.outcome = 'no-new';
        if (/found new available|new tracked truck/i.test(text)) cur.outcome = 'found';
        if (/Result:/i.test(text) && cur.outcome === 'running') cur.outcome = 'done';
        if (l.severity === 'error') cur.outcome = 'error';
    }
    return checks;
}

// Translate raw monitor stdout into a friendlier rendering: drop the
// repetitive timestamp prefix (already shown in the row header), pretty-
// print the well-known [AUTO-BUY: ACTIVE …] / [TRACKED] / [skipped] /
// [EXCLUDED] patterns, and leave anything unrecognized as-is so we never
// hide information.
function prettyLogLine(raw) {
    let s = String(raw || '');
    // Strip leading "[YYYY-MM-DD HH:MM:SS]" + whitespace
    s = s.replace(/^\s*\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?\]\s*/, '');

    // [AUTO-BUY: ACTIVE - Auto-buying: A + B + C (max $X)]
    let m = s.match(/^\[?\s*AUTO-BUY:\s*ACTIVE\s*-\s*Auto-buying:\s*(.*?)\s*\(max\s*\$([\d,\.]+)\)\s*\]?\s*$/i);
    if (m) {
        const targets = m[1].split('+').map(t => t.trim()).filter(Boolean).join(', ');
        return {
            kind: 'autobuy',
            html: `<span class="logfmt-icon">⚡</span><span class="logfmt-label">Auto-buy ON</span>` +
                  `<span class="logfmt-meta">targets: ${escape(targets)} · max $${escape(m[2])}</span>`,
        };
    }

    // [AUTO-BUY: PAUSED] / [AUTO-BUY: OFF]
    if (/^\[?\s*AUTO-BUY:\s*(PAUSED|OFF|DISABLED)/i.test(s)) {
        return { kind: 'autobuy-off',
            html: `<span class="logfmt-icon">⏸</span><span class="logfmt-label">Auto-buy OFF</span>` };
    }

    // "Auto-buy active"
    if (/^Auto-buy active$/i.test(s)) return null;   // redundant with the chip above

    // "Found 6 trucks:"
    m = s.match(/^Found\s+(\d+)\s+trucks?:?\s*$/i);
    if (m) return { kind: 'found', html: `<span class="logfmt-icon">🚚</span><span class="logfmt-label">Found ${m[1]} truck${m[1]==='1'?'':'s'}</span>` };

    // "[TRACKED] X -> Not available" / "-> AVAILABLE"
    m = s.match(/^\[TRACKED\]\s*(.*?)\s*->\s*(.+?)\s*$/i);
    if (m) {
        const isAvail = /AVAILABLE/i.test(m[2]) && !/Not available/i.test(m[2]);
        return {
            kind: isAvail ? 'tracked-avail' : 'tracked',
            html: `<span class="logfmt-icon">${isAvail ? '✅' : '·'}</span>` +
                  `<span class="logfmt-label">Tracked</span>` +
                  `<span class="logfmt-truck">${escape(m[1])}</span>` +
                  `<span class="logfmt-meta">${isAvail ? 'available' : 'not available'}</span>`,
        };
    }

    // "[skipped] X -> AVAILABLE"
    m = s.match(/^\[skipped\]\s*(.*?)\s*->\s*(.+?)\s*$/i);
    if (m) {
        const isAvail = /AVAILABLE/i.test(m[2]) && !/Not available/i.test(m[2]);
        return {
            kind: 'skipped',
            html: `<span class="logfmt-icon">⤴︎</span>` +
                  `<span class="logfmt-label">Excluded</span>` +
                  `<span class="logfmt-truck">${escape(m[1])}</span>` +
                  `<span class="logfmt-meta">${isAvail ? 'available' : 'not available'}</span>`,
        };
    }

    // "[EXCLUDED] Softline truck skipped: X"
    m = s.match(/^\[EXCLUDED\]\s*(.+?)\s*$/i);
    if (m) {
        return { kind: 'excluded',
            html: `<span class="logfmt-icon">🚫</span><span class="logfmt-label">Excluded</span><span class="logfmt-meta">${escape(m[1])}</span>` };
    }

    // "Check complete - no new trucks" / "Result: No new available trucks"
    if (/^(Check complete\s*-\s*no new trucks|No new available tracked.*found|Result:\s*No new available trucks)\.?$/i.test(s)) {
        return { kind: 'no-new', html: `<span class="logfmt-icon">·</span><span class="logfmt-label">No new available trucks</span>` };
    }

    // "Heartbeat written"
    if (/^Heartbeat written$/i.test(s)) {
        return { kind: 'heartbeat', html: `<span class="logfmt-icon">♥</span><span class="logfmt-label">Heartbeat written</span>` };
    }

    // "Starting check at ..." / "Checking Good360 Amazon truckloads..."
    if (/^(Starting check at\b|Checking Good360 Amazon truckloads)/i.test(s)) {
        return null;   // redundant with the row header timestamp + title
    }

    // Default: keep the raw text, just escaped.
    return { kind: 'raw', html: escape(s) };
}

function checkSummaryPills(c) {
    const pills = [];
    if (c.errors)   pills.push(`<span class="check-stat-pill err">${c.errors} err</span>`);
    if (c.warnings) pills.push(`<span class="check-stat-pill warn">${c.warnings} warn</span>`);
    pills.push(`<span class="check-stat-pill info">${c.tracked} tracked</span>`);
    if (c.available) pills.push(`<span class="check-stat-pill warn">${c.available} avail</span>`);
    if (c.outcome === 'no-new') pills.push(`<span class="check-stat-pill ok">no new</span>`);
    else if (c.outcome === 'found') pills.push(`<span class="check-stat-pill ok">found</span>`);
    else if (c.outcome === 'error') pills.push(`<span class="check-stat-pill err">error</span>`);
    return pills.join('');
}

function renderCheckList() {
    const el = $('#checkList');
    const all = filteredChecks();
    const total = all.length;
    if (!total) {
        const msg = SCAN_STATE.checkFilter
            ? `no checks match the "${SCAN_STATE.checkFilter}" filter`
            : 'no log activity yet';
        el.innerHTML = `<div class="check-empty"><em>${msg}</em></div>`;
        $('#checkPager').hidden = true;
        return;
    }
    const pages = Math.max(1, Math.ceil(total / SCAN_STATE.perPage));
    SCAN_STATE.checkPage = Math.min(SCAN_STATE.checkPage, pages - 1);
    const start = SCAN_STATE.checkPage * SCAN_STATE.perPage;
    const slice = all.slice(start, start + SCAN_STATE.perPage);

    el.innerHTML = slice.map((c, i) => {
        const id = c.startedIso || `idx-${start + i}`;
        const expanded = SCAN_STATE.expandedChecks.has(id);
        const timeLabel = c.startedIso
            ? `<span class="check-title-time" title="${escape(c.startedRaw)}">${escape(formatHumanTime(c.startedIso))}</span>`
            : '<span class="check-title-time">—</span>';
        const headline = describeCheck(c);
        const body = c.lines.map(l => {
            const cls = l.severity === 'error' ? 'log-err' :
                        l.severity === 'warn'  ? 'log-warn' :
                        l.severity === 'ok'    ? 'log-ok'   : 'log-info';
            const pretty = prettyLogLine(l.line);
            if (pretty === null) return '';   // suppressed (redundant with header)
            const cls2 = pretty.kind === 'raw' ? '' : ` logfmt-${pretty.kind}`;
            return `<div class="check-line ${cls}${cls2}">${pretty.html}</div>`;
        }).filter(Boolean).join('');
        return `
            <div class="check-row${expanded ? ' expanded' : ''}" data-check-id="${escape(id)}">
                <div class="check-row__head">
                    <span class="check-chevron">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
                    </span>
                    <span class="check-row__title">${timeLabel}<span>${escape(headline)}</span></span>
                    <span class="check-row__summary">${checkSummaryPills(c)}</span>
                </div>
                <div class="check-row__body">${body || '<em style="color:var(--text-mute)">(no lines captured)</em>'}</div>
            </div>
        `;
    }).join('');

    el.querySelectorAll('.check-row').forEach(row => {
        row.querySelector('.check-row__head').addEventListener('click', () => {
            const id = row.dataset.checkId;
            if (SCAN_STATE.expandedChecks.has(id)) SCAN_STATE.expandedChecks.delete(id);
            else SCAN_STATE.expandedChecks.add(id);
            row.classList.toggle('expanded');
        });
    });

    // Pager
    const pager = $('#checkPager');
    pager.hidden = pages <= 1;
    $('#checkPagerInfo').textContent = `${SCAN_STATE.checkPage + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = SCAN_STATE.checkPage === 0;
    pager.querySelector('[data-pg="next"]').disabled = SCAN_STATE.checkPage >= pages - 1;
}

function describeCheck(c) {
    if (c.outcome === 'error') return `Check failed (${c.errors} error${c.errors === 1 ? '' : 's'})`;
    if (c.outcome === 'no-new') return `Check complete · no new tracked trucks (${c.tracked} scanned)`;
    if (c.outcome === 'found')  return `Check complete · new truck detected`;
    if (c.outcome === 'done')   return `Check complete (${c.tracked} scanned)`;
    return `Check in progress (${c.tracked} so far)`;
}

// ---------------- Detected trucks: paginated + expandable ----------------

function renderScansTable() {
    const tb = $('#scansTable tbody');
    tb.innerHTML = '';
    const all = [...SCAN_STATE.scans].reverse();   // newest first
    const total = all.length;
    if (!total) {
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">No scans yet — start the monitor service to begin collecting telemetry.</div></td></tr>`;
        $('#scansPager').hidden = true;
        return;
    }
    const pages = Math.max(1, Math.ceil(total / SCAN_STATE.perPage));
    SCAN_STATE.scansPage = Math.min(SCAN_STATE.scansPage, pages - 1);
    const start = SCAN_STATE.scansPage * SCAN_STATE.perPage;
    const slice = all.slice(start, start + SCAN_STATE.perPage);

    for (let i = 0; i < slice.length; i++) {
        const s = slice[i];
        const key = s.time || `idx-${start + i}`;
        const expanded = SCAN_STATE.expandedScans.has(key);
        const tr = document.createElement('tr');
        tr.className = 'scan-row' + (expanded ? ' expanded' : '');
        tr.dataset.scanKey = key;
        const loginPill = s.login_ok === false ? '<span class="pill err">fail</span>'
                        : s.login_ok === true  ? '<span class="pill ok">ok</span>'
                        : '<span class="pill idle">—</span>';
        tr.innerHTML = `
            <td class="expand-cell">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
            </td>
            <td class="mono" data-label="Time" title="${escape(s.time || '')}">${escape(s.time ? formatHumanTime(s.time) : '—')}</td>
            <td data-label="Org">${escape(s.org_id || '—')}</td>
            <td data-label="Login">${loginPill}</td>
            <td data-label="Truck">${escape(s.title || '—')}</td>
            <td data-label="Status"><span class="pill ${pillClass(s.status)}">${escape(s.status || 'scan')}</span></td>
        `;
        tb.appendChild(tr);

        const dt = document.createElement('tr');
        dt.className = 'scan-detail' + (expanded ? ' shown' : '');
        dt.dataset.scanKey = key;
        const shotStrip = renderScanShotStrip(key);
        const captureStrip = renderScanCaptureStrip(key);
        dt.innerHTML = `<td colspan="6"><dl class="scan-detail__grid">
            <dt>captured</dt><dd>${escape(s.time || '—')}</dd>
            <dt>org</dt><dd>${escape(s.org_id || '—')}</dd>
            <dt>truck</dt><dd>${escape(s.title || '—')}</dd>
            <dt>status</dt><dd>${escape(s.status || '—')}</dd>
            <dt>login</dt><dd>${s.login_ok === true ? 'ok' : s.login_ok === false ? 'fail' : 'unknown'}</dd>
            <dt>price</dt><dd>${s.price != null ? '$' + Number(s.price).toLocaleString() : '—'}</dd>
            <dt>url</dt><dd>${s.url ? `<a href="${escape(s.url)}" target="_blank" rel="noopener">${escape(s.url)}</a>` : '—'}</dd>
        </dl>${captureStrip}${shotStrip}</td>`;
        tb.appendChild(dt);

        dt.querySelectorAll('.shot-card').forEach(card => {
            card.addEventListener('click', (e) => {
                e.stopPropagation();
                openScanShot(card.dataset.scanKey, Number(card.dataset.shotIdx));
            });
        });

        tr.addEventListener('click', () => {
            const k = tr.dataset.scanKey;
            if (SCAN_STATE.expandedScans.has(k)) SCAN_STATE.expandedScans.delete(k);
            else SCAN_STATE.expandedScans.add(k);
            tr.classList.toggle('expanded');
            dt.classList.toggle('shown');
        });

        // Small badge on the main row when this scan has screenshots — gives
        // users a visible signal without needing to expand every row.
        const shotCount = shotsForScan(key).length;
        if (shotCount) {
            const truckCell = tr.querySelector('[data-label="Truck"]');
            const badge = document.createElement('span');
            badge.className = 'scan-shot-badge';
            badge.title = `${shotCount} screenshot${shotCount === 1 ? '' : 's'}`;
            badge.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="14" rx="2"/><circle cx="12" cy="13" r="3"/><path d="M9 6l1.5-2h3L15 6"/></svg><span>${shotCount}</span>`;
            truckCell.appendChild(badge);
        }
    }

    const pager = $('#scansPager');
    pager.hidden = pages <= 1;
    $('#scansPagerInfo').textContent = `${SCAN_STATE.scansPage + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = SCAN_STATE.scansPage === 0;
    pager.querySelector('[data-pg="next"]').disabled = SCAN_STATE.scansPage >= pages - 1;
}

// ---------------- Pager wiring ----------------

document.querySelectorAll('#checkPager .pager-btn, #scansPager .pager-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const pager = btn.closest('.pagination');
        const isCheck = pager.id === 'checkPager';
        const dir = btn.dataset.pg === 'next' ? 1 : -1;
        if (isCheck) SCAN_STATE.checkPage += dir;
        else        SCAN_STATE.scansPage += dir;
        if (isCheck) renderCheckList();
        else        renderScansTable();
    });
});

document.querySelectorAll('#checkFilters .filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#checkFilters .filter-pill')
            .forEach(b => b.classList.toggle('active', b === btn));
        SCAN_STATE.checkFilter = btn.dataset.filterCheck || '';
        SCAN_STATE.checkPage = 0;
        renderCheckList();
    });
});

// ---------------- Countdown + in-flight pulse ----------------

// We don't know the exact monitor cadence from the frontend, so we infer it
// from successive heartbeat deltas in loadScans(). Default to 60s until we
// have a measurement. The pulse turns on once we're past the expected next
// scan time but haven't yet seen the new heartbeat.

function tickCountdown() {
    const meta = $('#metricCountdownMeta');
    const value = $('#metricCountdown');
    const pulse = $('#scanPulse');
    if (!SCAN_STATE.lastHbIso) {
        value.textContent = '—';
        meta.textContent = 'waiting for first scan';
        if (pulse) pulse.hidden = true;
        return;
    }
    const interval = SCAN_STATE.intervalSec || 60;
    const elapsed = secondsSince(SCAN_STATE.lastHbIso);
    const remaining = interval - elapsed;
    if (remaining > 0) {
        value.textContent = `in ${remaining}s`;
        meta.textContent = `inferred interval · ${interval}s`;
        if (pulse) pulse.hidden = true;
    } else {
        value.textContent = 'now';
        const overdueBy = -remaining;
        meta.textContent = overdueBy < 30
            ? `check in flight…`
            : `overdue by ${formatRelTime(overdueBy)}`;
        if (pulse) pulse.hidden = false;
    }
}

setInterval(tickCountdown, 1000);

// ---------------- Human-readable timestamps ----------------

function formatHumanTime(iso) {
    if (!iso) return '—';
    let d;
    try { d = new Date(iso); } catch { return String(iso); }
    if (isNaN(d.getTime())) return String(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
    const isYesterday = d.toDateString() === yesterday.toDateString();
    const time = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    if (sameDay) return `Today · ${time}`;
    if (isYesterday) return `Yesterday · ${time}`;
    return d.toLocaleDateString([], {month: 'short', day: 'numeric'}) + ' · ' + time;
}

// ---- Auto-refresh wiring for the Scans tab ----
const SCAN_REFRESH_MS = 5000;
let _scanTimer = null;
function startScanAutoRefresh() {
    stopScanAutoRefresh();
    if (!$('#scanAutoRefresh')?.checked) return;
    _scanTimer = setInterval(() => {
        if (document.querySelector('.panel.active')?.dataset.panel === 'scans' &&
            !document.hidden) {
            loadScans().catch(e => console.error('[scans] refresh failed', e));
        }
    }, SCAN_REFRESH_MS);
}
function stopScanAutoRefresh() {
    if (_scanTimer) { clearInterval(_scanTimer); _scanTimer = null; }
}
document.getElementById('scanAutoRefresh')?.addEventListener('change', () => {
    if ($('#scanAutoRefresh').checked) startScanAutoRefresh(); else stopScanAutoRefresh();
});
document.getElementById('scanRefreshBtn')?.addEventListener('click', () => loadScans());
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && $('#scanAutoRefresh')?.checked) startScanAutoRefresh();
});
// Kick off auto-refresh when the page first lands on Scans (the default tab).
setTimeout(startScanAutoRefresh, 1000);

// ---------------- Purchases ----------------

// Track which purchase rows are expanded across refreshes.
const PURCHASE_EXPANDED = new Set();

const PURCH_STATE = {
    q: '', since: '', until: '',
    offset: 0, perPage: 25,
    total: 0,
    _debounceTimer: null,
};

function _purchasesParams() {
    const p = new URLSearchParams({
        limit:  String(PURCH_STATE.perPage),
        offset: String(PURCH_STATE.offset),
        days:   '365',   // wide net by default; date filters refine within
    });
    if (PURCH_STATE.q)     p.set('q', PURCH_STATE.q);
    if (PURCH_STATE.since) p.set('since', PURCH_STATE.since);
    if (PURCH_STATE.until) p.set('until', PURCH_STATE.until);
    return p.toString();
}

async function loadPurchases() {
    const r = await api('/api/admin/purchases?' + _purchasesParams()).then(r => r.json());
    const purchases = r.data || [];
    const stats = r.stats || {};
    PURCH_STATE.total = Number(r.total || 0);

    $('#purchOk').textContent    = stats.ok ?? 0;
    $('#purchFail').textContent  = stats.fail ?? 0;
    $('#purchTotal').textContent = '$' + Number(stats.spend || 0).toLocaleString(undefined, {maximumFractionDigits: 2});

    const meta = $('#purchSummaryMeta');
    if (meta) {
        const start = PURCH_STATE.offset + (purchases.length ? 1 : 0);
        const end   = PURCH_STATE.offset + purchases.length;
        meta.textContent = PURCH_STATE.total
            ? `showing ${start}–${end} of ${PURCH_STATE.total}`
            : 'no matching attempts';
    }

    const tb = $('#purchasesTable tbody');
    tb.innerHTML = '';
    if (!purchases.length) {
        tb.innerHTML = `<tr><td colspan="8"><div class="empty-state">No purchase attempts match the current filters.</div></td></tr>`;
        _renderPurchPager();
        return;
    }
    const isSuper = CURRENT_USER && CURRENT_USER.role === 'super_admin';
    for (const p of purchases) {
        const status = displayStatus(p);
        const key = `${p.ts || ''}|${p.truck || ''}|${p.attempt_id || p.id || ''}`;
        const expanded = PURCHASE_EXPANDED.has(key);

        // The Detail cell was rendering raw error blobs / JSON tracebacks
        // unreadable on a single line. Now it's a one-line summary; the
        // full picture lives in the expanded panel below.
        const summary = _purchaseSummary(p);
        const delId = p.attempt_id != null ? p.attempt_id : (p.id != null ? p.id : '');

        // Phase 5 — operator abort / redirect. Roster rows only.
        const isRosterRow = (p.source || 'roster') === 'roster';
        const canAbort = isSuper && isRosterRow && p.status === 'in_progress' && delId !== '';
        const abortPending = isRosterRow && p.status === 'in_progress' && Number(p.abort_requested);
        const truckLive = ['detected', 'assigned'].includes(String(p.truck_event_status || '').toLowerCase());
        const canRedirect = isSuper && isRosterRow && p.status === 'aborted_operator'
            && truckLive && p.truck_event_id != null;

        const tr = document.createElement('tr');
        tr.className = 'purchase-row' + (expanded ? ' expanded' : '');
        tr.dataset.key = key;
        tr.innerHTML = `
            <td class="expand-cell">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
            </td>
            <td class="mono" data-label="Time" title="${escape(p.ts || '')}">${escape(p.ts ? formatHumanTime(p.ts) : '—')}</td>
            <td data-label="Org">${escape(p.org_id || '—')}</td>
            <td data-label="Truck">${escape(p.truck || '—')}</td>
            <td class="num" data-label="Total">${p.total != null ? '$' + Number(p.total).toFixed(2) : '—'}</td>
            <td data-label="Status"><span class="pill ${pillClass(status)}">${escape(status.toLowerCase())}</span>${abortPending ? ' <span class="pill warn" title="Abort flag set — takes effect only if a pre-Place-Order checkpoint is still ahead">abort requested</span>' : ''}</td>
            <td class="mono" data-label="Detail" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escape(summary)}</td>
            <td class="super-only" data-hidden="${isSuper ? 'false' : 'true'}">
                ${canAbort && !abortPending ? `<button type="button" class="row-abort-btn" title="Abort this purchase (only possible before Place Order)" data-abort-id="${escape(String(delId))}">⛔ Abort</button>` : ''}
                ${canRedirect ? `<button type="button" class="row-redirect-btn" title="Redirect this truck to another org" data-redirect-event="${escape(String(p.truck_event_id))}">Redirect truck →</button>` : ''}
                ${delId !== '' ? `<button type="button" class="row-delete-btn" title="Delete this attempt" data-del-source="${escape(p.source || 'roster')}" data-del-id="${escape(String(delId))}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                </button>` : ''}
            </td>
        `;
        tb.appendChild(tr);

        const dt = document.createElement('tr');
        dt.className = 'purchase-detail' + (expanded ? ' shown' : '');
        dt.innerHTML = `<td colspan="8">${_purchaseDetailBody(p)}</td>`;
        tb.appendChild(dt);

        tr.addEventListener('click', (e) => {
            // Don't expand when a row action button (or anything inside it) is clicked.
            if (e.target.closest('.row-delete-btn, .row-abort-btn, .row-redirect-btn')) return;
            if (PURCHASE_EXPANDED.has(key)) PURCHASE_EXPANDED.delete(key);
            else                            PURCHASE_EXPANDED.add(key);
            tr.classList.toggle('expanded');
            dt.classList.toggle('shown');
            if (dt.classList.contains('shown')) {
                // Lazy-load the capture JSON the first time this row opens.
                if (p.capture_path) {
                    const slot = dt.querySelector('[data-capture-slot]');
                    if (slot && !slot.dataset.loaded) {
                        slot.dataset.loaded = '1';
                        _renderCaptureInline(slot, p.capture_path);
                    }
                }
                // Lazy-load the AI diagnosis. The endpoint caches per
                // attempt, so a hot row just re-renders the stored answer.
                const dslot = dt.querySelector('[data-diagnose-slot]');
                if (dslot && !dslot.dataset.loaded) {
                    dslot.dataset.loaded = '1';
                    _renderDiagnoseInline(dslot);
                }
            }
        });

        const abortBtn = tr.querySelector('.row-abort-btn');
        if (abortBtn) abortBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!window.confirm('Abort this purchase? If the order was already placed it cannot be stopped — the abort takes effect only before Place Order.')) return;
            abortBtn.disabled = true;
            try {
                const res = await api(`/api/admin/purchases/roster/${encodeURIComponent(abortBtn.dataset.abortId)}/abort`, {method: 'POST'}).then(r => r.json());
                if (!res.success) throw new Error(res.error || 'abort failed');
                loadPurchases();
            } catch (err) {
                abortBtn.disabled = false;
                window.alert('Abort failed: ' + (err.message || err));
            }
        });

        const redirBtn = tr.querySelector('.row-redirect-btn');
        if (redirBtn) redirBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            openRedirectModal(redirBtn.dataset.redirectEvent, p);
        });

        const delBtn = tr.querySelector('.row-delete-btn');
        if (delBtn) delBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!window.confirm(`Delete this purchase attempt?\n\n${p.org_id || '—'} — ${p.truck || '—'}\n${p.ts || ''}\n\nThis cannot be undone.`)) return;
            delBtn.disabled = true;
            try {
                const res = await api(`/api/admin/purchases/${delBtn.dataset.delSource}/${encodeURIComponent(delBtn.dataset.delId)}`, {method: 'DELETE'}).then(r => r.json());
                if (!res.success) throw new Error(res.error || 'delete failed');
                loadPurchases();
                loadSidebarBadges();
            } catch (err) {
                delBtn.disabled = false;
                window.alert('Delete failed: ' + (err.message || err));
            }
        });
    }

    _renderPurchPager();
}

function _renderPurchPager() {
    const pager = $('#purchPager');
    if (!pager) return;
    const pages = Math.max(1, Math.ceil(PURCH_STATE.total / PURCH_STATE.perPage));
    const currentPage = Math.floor(PURCH_STATE.offset / PURCH_STATE.perPage);
    pager.hidden = pages <= 1;
    $('#purchPagerInfo').textContent = `${currentPage + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = currentPage === 0;
    pager.querySelector('[data-pg="next"]').disabled = (PURCH_STATE.offset + PURCH_STATE.perPage) >= PURCH_STATE.total;
}

// ---- Purchases toolbar wiring ---------------------------------------
function _purchaseFiltersChanged({ resetOffset = true } = {}) {
    if (resetOffset) PURCH_STATE.offset = 0;
    clearTimeout(PURCH_STATE._debounceTimer);
    PURCH_STATE._debounceTimer = setTimeout(() => loadPurchases().catch(e => console.error('[purchases]', e)), 200);
}

document.addEventListener('DOMContentLoaded', () => {
    const search = $('#purchSearch');
    const since  = $('#purchSince');
    const until  = $('#purchUntil');
    const reset  = $('#purchReset');
    if (search) search.addEventListener('input', () => { PURCH_STATE.q = search.value.trim(); _purchaseFiltersChanged(); });
    if (since)  since.addEventListener('change', () => { PURCH_STATE.since = since.value; _purchaseFiltersChanged(); });
    if (until)  until.addEventListener('change', () => { PURCH_STATE.until = until.value; _purchaseFiltersChanged(); });
    if (reset)  reset.addEventListener('click', () => {
        PURCH_STATE.q = ''; PURCH_STATE.since = ''; PURCH_STATE.until = '';
        if (search) search.value = '';
        if (since)  since.value  = '';
        if (until)  until.value  = '';
        _purchaseFiltersChanged();
    });
    document.querySelectorAll('#purchPager .pager-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const dir = btn.dataset.pg === 'next' ? 1 : -1;
            PURCH_STATE.offset = Math.max(0, PURCH_STATE.offset + dir * PURCH_STATE.perPage);
            loadPurchases().catch(e => console.error('[purchases]', e));
        });
    });
    _wireRedirectModal();
});

// ---- Redirect-truck modal (Phase 5) ---------------------------------
// Opens from an aborted attempt whose truck is still live. Default is
// "next in queue" (roster picks); the dropdown offers only currently
// eligible orgs (active + in rotation + not cooling), same source as the
// roster queue panel. The server re-validates the target either way.
function openRedirectModal(eventId, p) {
    const modal = $('#redirectModal');
    if (!modal) return;
    modal.dataset.eventId = String(eventId);
    const sub = $('#redirectModalSub');
    if (sub) sub.textContent = `${p?.truck || p?.truck_title || 'Truck'} — pick who gets it.`;
    const nextRadio = modal.querySelector('input[name="redirectTarget"][value="next"]');
    if (nextRadio) nextRadio.checked = true;
    const sel = $('#redirectOrgSelect');
    sel.disabled = true;
    sel.innerHTML = '<option value="">Loading…</option>';
    modal.hidden = false;
    api('/api/admin/roster/queue').then(r => r.json()).then(r => {
        const rows = (r.data && r.data.queue) || [];
        sel.innerHTML = rows.length
            ? rows.map(c => `<option value="${escape(String(c.id))}">${escape(c.organization_name || c.full_name || String(c.id))}</option>`).join('')
            : '<option value="">(no eligible orgs)</option>';
        _syncRedirectSelectState();
    }).catch(() => {
        sel.innerHTML = '<option value="">(failed to load orgs)</option>';
    });
}

function _syncRedirectSelectState() {
    const modal = $('#redirectModal');
    if (!modal) return;
    const mode = modal.querySelector('input[name="redirectTarget"]:checked')?.value;
    $('#redirectOrgSelect').disabled = mode !== 'org';
}

function _wireRedirectModal() {
    const modal = $('#redirectModal');
    if (!modal) return;
    modal.querySelectorAll('input[name="redirectTarget"]').forEach(r =>
        r.addEventListener('change', _syncRedirectSelectState));
    modal.querySelector('[data-close]').addEventListener('click', () => { modal.hidden = true; });
    modal.addEventListener('click', (ev) => { if (ev.target === modal) modal.hidden = true; });
    const go = $('#redirectModalGo');
    go.addEventListener('click', async () => {
        const eventId = modal.dataset.eventId;
        const mode = modal.querySelector('input[name="redirectTarget"]:checked')?.value;
        const payload = mode === 'org'
            ? { org_id: $('#redirectOrgSelect').value }
            : { next_in_queue: true };
        if (mode === 'org' && !payload.org_id) {
            window.alert('Pick an org from the list (or use Next in queue).');
            return;
        }
        go.disabled = true;
        try {
            const res = await api(`/api/admin/truck-events/${encodeURIComponent(eventId)}/redirect`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
            }).then(r => r.json());
            if (!res.success) throw new Error(res.error || 'redirect failed');
            modal.hidden = true;
            window.alert(`Redirect dispatched (${res.target}). The new attempt will appear in this list shortly.`);
            loadPurchases().catch(e => console.error('[purchases]', e));
        } catch (err) {
            window.alert('Redirect failed: ' + (err.message || err));
        } finally {
            go.disabled = false;
        }
    });
}

async function _renderDiagnoseInline(slot, { refresh = false } = {}) {
    const source = slot.dataset.diagSource;
    const id     = slot.dataset.diagId;
    const body   = slot.querySelector('.diagnose-body');
    const btn    = slot.querySelector('.diagnose-refresh');
    if (!source || !id || !body) return;

    body.innerHTML = `<span class="diagnose-loading">Analyzing failure…</span>`;
    if (btn) btn.hidden = true;

    const url = `/api/admin/purchases/${encodeURIComponent(source)}/${encodeURIComponent(id)}/diagnose${refresh ? '?refresh=1' : ''}`;
    let payload;
    try {
        const r = await api(url);
        payload = await r.json();
    } catch (e) {
        body.innerHTML = `<span class="diagnose-error">Could not run diagnosis: ${escape(String(e))}</span>`;
        if (btn) btn.hidden = false;
        return;
    }

    if (!payload?.success) {
        body.innerHTML = `<span class="diagnose-error">${escape(payload?.error || 'diagnosis failed')}</span>`;
        if (btn) btn.hidden = false;
        return;
    }

    const d        = payload.data || {};
    const cached   = !!payload.cached;
    const action   = d.suggested_action ? `<div class="diagnose-action"><span class="diagnose-action-label">Next step</span> ${escape(d.suggested_action)}</div>` : '';
    const sources  = d.similar_count
        ? `<span class="diagnose-meta">${d.similar_count} similar past failure${d.similar_count === 1 ? '' : 's'} considered</span>`
        : `<span class="diagnose-meta">no prior similar failures</span>`;
    const generated = d.generated_at ? ` · cached ${escape(formatHumanTime(d.generated_at))}` : '';
    const modelTag  = d.model ? `<span class="diagnose-model mono">${escape(d.model)}</span>` : '';
    body.innerHTML = `
        <p class="diagnose-text">${escape(d.diagnosis || '')}</p>
        ${action}
        <div class="diagnose-footer">${sources}${cached ? generated : ''} ${modelTag}</div>
    `;
    if (btn) {
        btn.hidden = false;
        btn.onclick = (e) => { e.stopPropagation(); _renderDiagnoseInline(slot, { refresh: true }); };
    }
}


async function _renderCaptureInline(slot, capturePath) {
    slot.innerHTML = '<div class="empty-state" style="padding:12px">Loading capture…</div>';
    let data;
    try {
        const url = '/api/admin/scans/captures/file?p=' + encodeURIComponent(capturePath);
        data = await api(url).then(r => r.json());
    } catch (e) {
        slot.innerHTML = `<div class="empty-state" style="padding:12px;color:var(--err)">Failed to load capture JSON: ${escape(String(e))}</div>`;
        return;
    }

    // Build a structured summary so the operator doesn't have to scroll
    // through 700KB of network log to find what went wrong.
    const network = data.network || [];
    const console_ = data.console || [];
    const steps    = data.steps   || [];

    const statusCounts = {};
    for (const n of network) {
        const s = n.status || 0;
        statusCounts[s] = (statusCounts[s] || 0) + 1;
    }
    const statusStr = Object.entries(statusCounts)
        .sort((a, b) => a[0] - b[0])
        .map(([s, c]) => `${s}: ${c}`)
        .join(' · ');

    const non200 = network.filter(n => (n.status || 0) >= 400);
    const consoleErrs = console_.filter(c => c.type === 'error' || c.type === 'pageerror');

    // Summary blocks
    const summaryHtml = `
        <div class="capture-summary">
            <div class="capture-summary-row">
                <span class="capture-label">Outcome</span>
                <span class="pill ${pillClass(data.outcome)}">${escape(data.outcome || '?')}</span>
                <span class="mono" style="color:var(--text-dim)">${escape(data.message || '')}</span>
            </div>
            <div class="capture-summary-row">
                <span class="capture-label">Engine</span>
                <span class="mono">${escape(data.engine || '(script)')}</span>
                <span class="capture-label">Mode</span>
                <span class="mono">${data.sandbox_mode ? 'sandbox' : 'live'}</span>
            </div>
            <div class="capture-summary-row">
                <span class="capture-label">Final URL</span>
                ${data.final_url
                    ? `<a class="mono" href="${escape(data.final_url)}" target="_blank" rel="noopener" style="word-break:break-all">${escape(data.final_url)}</a>`
                    : '<span class="mono" style="color:var(--text-mute)">—</span>'}
            </div>
            <div class="capture-summary-row">
                <span class="capture-label">Timing</span>
                <span class="mono">${escape(data.started_at || '—')} → ${escape(data.finished_at || '—')}</span>
            </div>
            <div class="capture-summary-row">
                <span class="capture-label">Counts</span>
                <span class="mono">${steps.length} steps · ${network.length} responses (${statusStr}) · ${console_.length} console msgs (${consoleErrs.length} errors)</span>
            </div>
        </div>
    `;

    // Steps timeline
    const stepsHtml = steps.length
        ? `<details class="capture-section">
            <summary>Steps timeline (${steps.length})</summary>
            <pre class="purchase-detail__pre">${steps.map(s =>
                `[${escape(s.ts || '')}] ${escape(s.label || '')}` +
                (Object.keys(s).filter(k => k !== 'ts' && k !== 'label').length
                    ? ' ' + escape(JSON.stringify(Object.fromEntries(Object.entries(s).filter(([k]) => k !== 'ts' && k !== 'label'))))
                    : '')
            ).join('\n')}</pre>
        </details>`
        : '';

    // 4xx/5xx responses (most actionable)
    const errResponsesHtml = non200.length
        ? `<details class="capture-section" open>
            <summary>Non-200 responses (${non200.length})</summary>
            <pre class="purchase-detail__pre">${non200.map(n =>
                `HTTP ${n.status} ${n.method || ''} ${n.url}\n  content-type: ${n.content_type || ''}\n  body: ${escape((n.body || '').slice(0, 600))}`
            ).join('\n\n')}</pre>
        </details>`
        : '';

    // Console errors
    const consoleHtml = consoleErrs.length
        ? `<details class="capture-section" open>
            <summary>Console errors (${consoleErrs.length})</summary>
            <pre class="purchase-detail__pre">${consoleErrs.map(c =>
                `[${escape(c.ts || '')}] [${escape(c.type)}] ${escape(c.text || '')}`
            ).join('\n')}</pre>
        </details>`
        : '';

    // Final HTML preview if present
    const finalHtmlBlock = data.final_html
        ? `<details class="capture-section">
            <summary>Final page HTML (${data.final_html.length} chars)</summary>
            <pre class="purchase-detail__pre">${escape(data.final_html.slice(0, 8000))}${data.final_html.length > 8000 ? '\n…[truncated]' : ''}</pre>
        </details>`
        : '';

    // Full raw network log (collapsed by default - it's big)
    const networkHtml = network.length
        ? `<details class="capture-section">
            <summary>All network responses (${network.length}, including 200s)</summary>
            <pre class="purchase-detail__pre">${network.map(n =>
                `HTTP ${n.status} ${n.method || ''} ${n.url}${n.size ? ' [' + n.size + 'B]' : ''}`
            ).join('\n')}</pre>
        </details>`
        : '';

    // Raw JSON dump (collapsed)
    const rawJsonHtml = `<details class="capture-section">
        <summary>Raw capture JSON (${JSON.stringify(data).length.toLocaleString()} chars)</summary>
        <pre class="purchase-detail__pre">${escape(JSON.stringify(data, null, 2))}</pre>
    </details>`;

    slot.innerHTML = `
        ${summaryHtml}
        ${stepsHtml}
        ${errResponsesHtml}
        ${consoleHtml}
        ${finalHtmlBlock}
        ${networkHtml}
        ${rawJsonHtml}
    `;
}

function _purchaseSummary(p) {
    // Pick the most useful one-liner. SUCCESS → confirmation #; FAILED →
    // first line of the error; otherwise short status note.
    if (p.confirmation_number) return `Order #${p.confirmation_number}`;
    const err = p.error_message || p.error || p.detail || '';
    if (err) {
        const firstLine = String(err).split('\n')[0].slice(0, 240);
        return firstLine;
    }
    return '—';
}

function _purchaseDetailBody(p) {
    // Pretty-print every meaningful field with click-through links for the
    // capture JSON + screenshots so an operator can drill in without
    // copy-pasting paths.
    const rows = [];
    const push = (label, value) => {
        if (value === null || value === undefined || value === '') return;
        rows.push([label, value]);
    };
    push('Source',          p.source || 'legacy');
    push('Engine',          p.engine);
    push('Status',          p.status);
    // Dynamic buyer history: lifecycle chip + buttons (roster successes only)
    if ((p.source || 'roster') === 'roster') {
        const cell = orderLifecycleCell(p);
        if (cell !== '—') push('Order status', cell);
    }
    push('Mode',            p.mode);
    push('Started',         p.started_at);
    push('Completed',       p.completed_at);
    push('Elapsed',         p.elapsed_seconds != null ? `${Number(p.elapsed_seconds).toFixed(1)}s` : null);
    push('Org',             p.org_name || p.org_id);
    push('Customer',        p.customer_name);
    push('Customer ID',     p.customer_id || p.quickbeed_customer_id);
    push('Truck',           p.truck_name || p.truck || p.truck_title);
    push('Truck URL',       p.truck_url
        ? `<a class="mono" href="${escape(p.truck_url)}" target="_blank" rel="noopener" style="word-break:break-all">${escape(p.truck_url)}</a>`
        : null);
    push('Truck price',     p.truck_price != null ? `$${Number(p.truck_price).toLocaleString()}` : null);
    push('Order total',     p.order_total != null ? `$${Number(p.order_total).toLocaleString()}` : null);
    push('Confirmation #',  p.confirmation_number);
    push('Capture JSON',    p.capture_path
        ? `<a class="mono" target="_blank" rel="noopener" href="/api/admin/scans/captures/file?p=${encodeURIComponent(p.capture_path)}">view raw capture (network log, console errors, final HTML) ↗</a>`
        : null);
    push('Screenshot',      p.screenshot_path
        ? `<a class="mono" target="_blank" rel="noopener" href="/api/admin/screenshots/file?p=${encodeURIComponent(p.screenshot_path)}">view screenshot ↗</a>`
        : null);

    // Error: shown as a separate, full-width block — these can be long
    // multi-line tracebacks and squishing them into the table is what
    // made the Detail column unreadable to begin with.
    const errorBlock = (p.error_message || p.error)
        ? `<div class="purchase-detail__error">
             <div class="product-detail__label">Error</div>
             <pre class="purchase-detail__pre">${escape(p.error_message || p.error)}</pre>
           </div>`
        : '';

    const rowHtml = rows.map(([label, value]) =>
        `<dt>${escape(label)}</dt><dd>${value}</dd>`
    ).join('');

    // Slot for the AI-generated diagnosis (lazy-loaded on first expand).
    // Top of the panel so the operator sees the explanation before the
    // raw data. Only shown for attempts that have an id we can route to.
    const delId = p.attempt_id != null ? p.attempt_id : (p.id != null ? p.id : '');
    const diagnoseSlot = delId !== ''
        ? `<div class="purchase-detail__diagnose" data-diagnose-slot
                 data-diag-source="${escape(p.source || 'roster')}"
                 data-diag-id="${escape(String(delId))}">
             <div class="diagnose-head">
                 <span class="diagnose-label">AI diagnosis</span>
                 <button type="button" class="diagnose-refresh" title="Re-run the diagnosis (bypass cache)" hidden>↻</button>
             </div>
             <div class="diagnose-body"><em>—</em></div>
           </div>`
        : '';

    // Slot for the full capture JSON (lazy-loaded on row expand).
    const captureSlot = p.capture_path
        ? `<div class="purchase-detail__capture">
             <div class="product-detail__label">Capture (network, console, page state)</div>
             <div data-capture-slot></div>
           </div>`
        : '';

    return `<div class="purchase-detail__body">
        ${diagnoseSlot}
        <dl class="scan-detail__grid">${rowHtml}</dl>
        ${errorBlock}
        ${captureSlot}
    </div>`;
}

function isSuccess(p) {
    const s = (p.status || p.event || '').toLowerCase();
    return s.includes('success') || s.includes('purchased') || s === 'ok';
}
function isFail(p) {
    const s = (p.status || p.event || '').toLowerCase();
    return s.includes('fail') || s.includes('error');
}
function displayStatus(p) {
    if (p.status) return p.status;
    const e = p.event || '';
    if (e.endsWith('_success')) return 'SUCCESS';
    if (e.endsWith('_fail') || e.endsWith('_error')) return 'FAIL';
    return e.toUpperCase() || 'ATTEMPT';
}

// ---------------- Users ----------------

async function loadUsers() {
    if (CURRENT_USER.role !== 'super_admin') {
        $('#usersTable tbody').innerHTML =
            `<tr><td colspan="6"><div class="empty-state">Only super-admins can view the user roster.</div></td></tr>`;
        $('#userCount').textContent = '';
        return;
    }
    const r = await api('/api/admin/users').then(r => r.json());
    const users = r.data || [];
    $('#userCount').textContent = `${users.length} account${users.length === 1 ? '' : 's'}`;

    const tb = $('#usersTable tbody');
    tb.innerHTML = '';
    for (const u of users) {
        const canDelete = u.role !== 'super_admin' && u.id !== CURRENT_USER.id;
        const roleClass = u.role === 'super_admin' ? 'err' : 'ok';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="mono" data-label="ID">#${u.id}</td>
            <td data-label="Email">${escape(u.email)}</td>
            <td data-label="Role"><span class="pill role ${roleClass}">${escape(u.role.replace('_', ' '))}</span></td>
            <td class="mono" data-label="Created">${escape(u.created_at || '—')}</td>
            <td class="mono" data-label="Last login">${escape(u.last_login_at || '—')}</td>
            <td class="actions" data-label="">${canDelete ? `<button data-uid="${u.id}">remove</button>` : ''}</td>
        `;
        tb.appendChild(tr);
    }
    tb.querySelectorAll('button[data-uid]').forEach(b => b.addEventListener('click', async () => {
        const email = b.closest('tr').children[1].textContent;
        if (!confirm(`Remove user ${email}? This is irreversible.`)) return;
        const res = await api(`/api/admin/users/${b.dataset.uid}`, {method: 'DELETE'});
        const j = await res.json();
        if (!j.success) alert(j.error || 'failed');
        loadUsers();
    }));
}

$('#addUserBtn').addEventListener('click', () => $('#addUserModal').hidden = false);
$$('#addUserModal [data-close]').forEach(b => b.addEventListener('click', () => {
    $('#addUserModal').hidden = true;
    $('#addUserErr').textContent = '';
    $('#addUserModal form').reset();
}));
$('#addUserModal form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const res = await api('/api/admin/users', {
        method: 'POST',
        body: JSON.stringify({email: f.email.value, password: f.password.value}),
    });
    const j = await res.json();
    if (!j.success) { $('#addUserErr').textContent = j.error || 'failed'; return; }
    $('#addUserModal').hidden = true;
    f.reset();
    loadUsers();
});

// ---------------- Customers (QuickBeed mirror) ----------------

function applyToggleState(btn, on) {
    btn.classList.toggle('on', on);
    btn.classList.toggle('off', !on);
    btn.setAttribute('aria-checked', String(on));
    btn.title = on ? 'Autobuy ON — tap to pause' : 'Autobuy OFF — tap to enable';
}

// State for the customer roster (filter / search / paginate).
const CUST_STATE = {
    all: [],
    page: 0,
    perPage: 15,
    statusFilter: '',     // '', 'active', 'paused', 'onboarding', 'inactive'
    autobuyFilter: '',    // '', 'on', 'off', 'cool'
    query: '',            // free-text search
};

function _matchesCustFilters(c) {
    if (CUST_STATE.statusFilter) {
        const s = (c.status || '').toLowerCase();
        if (CUST_STATE.statusFilter === 'inactive') {
            if (s !== 'inactive' && s !== 'suspended') return false;
        } else if (s !== CUST_STATE.statusFilter) return false;
    }
    const onCool = c.cooldown_until && new Date(c.cooldown_until).getTime() > Date.now();
    if (CUST_STATE.autobuyFilter === 'on'   && !c.in_rotation) return false;
    if (CUST_STATE.autobuyFilter === 'off'  &&  c.in_rotation) return false;
    if (CUST_STATE.autobuyFilter === 'cool' && !onCool)        return false;
    if (CUST_STATE.query) {
        const q = CUST_STATE.query.toLowerCase();
        const hay = [
            c.organization_name, c.full_name, c.email, c.id, c.phone,
        ].filter(Boolean).join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
    }
    return true;
}

async function loadCustomers() {
    const r = await api('/api/admin/customers').then(r => r.json());
    const customers = r.data || [];
    const summary = r.summary || {};

    $('#custActive').textContent      = summary.active     || 0;
    $('#custOnboarding').textContent  = summary.onboarding || 0;
    $('#custPaused').textContent      = summary.paused     || 0;
    $('#custInactive').textContent    = (summary.inactive || 0) + (summary.suspended || 0);

    CUST_STATE.all = customers;
    renderCustomers();
}

function renderCustomers() {
    const customers = CUST_STATE.all;
    const filtered = customers.filter(_matchesCustFilters);
    const sub = (CUST_STATE.statusFilter || CUST_STATE.autobuyFilter || CUST_STATE.query)
        ? `${filtered.length} of ${customers.length} matching`
        : `${customers.length} record${customers.length === 1 ? '' : 's'} mirrored locally`;
    $('#custRosterSub').textContent = sub;

    const tb = $('#customersTable tbody');
    tb.innerHTML = '';
    if (!filtered.length) {
        const msg = customers.length
            ? 'No customers match the current filters.'
            : 'No customers synced yet. Set QuickBeed credentials in Settings, then click "Resync now".';
        tb.innerHTML = `<tr><td colspan="8"><div class="empty-state">${msg}</div></td></tr>`;
        $('#custPager').hidden = true;
        return;
    }
    const pages = Math.max(1, Math.ceil(filtered.length / CUST_STATE.perPage));
    CUST_STATE.page = Math.min(CUST_STATE.page, pages - 1);
    const start = CUST_STATE.page * CUST_STATE.perPage;
    const slice = filtered.slice(start, start + CUST_STATE.perPage);
    const isSuper = CURRENT_USER?.role === 'super_admin';
    // Delegate toggle clicks once — survives re-renders.
    if (!tb._autobuyDelegated) {
        tb._autobuyDelegated = true;
        // Use capture-phase so we run BEFORE the row's own click listener
        // (which would otherwise navigate to customer-detail).
        tb.addEventListener('click', async (ev) => {
            const btn = ev.target.closest('.autobuy-toggle');
            if (!btn) return;
            ev.stopPropagation();
            ev.preventDefault();
            if (btn.disabled) return;
            const customerId = btn.dataset.customerId;
            const wasOn = btn.classList.contains('on');
            const turningOn = !wasOn;
            // Optimistic visual flip for snappy UX; revert on error.
            applyToggleState(btn, turningOn);
            btn.disabled = true;
            btn.classList.add('saving');
            try {
                const res = await api(`/api/admin/customers/${encodeURIComponent(customerId)}/rotation`, {
                    method: 'PATCH',
                    body: JSON.stringify({in_rotation: turningOn ? 1 : 0}),
                });
                const j = await res.json();
                if (!j.success) {
                    applyToggleState(btn, wasOn);   // revert
                    alert(j.error || 'failed to update');
                }
            } catch (e) {
                applyToggleState(btn, wasOn);       // revert
                alert('Network error: ' + (e?.message || e));
            } finally {
                btn.disabled = false;
                btn.classList.remove('saving');
            }
        }, true);   // capture phase
    }

    for (const c of slice) {
        const tr = document.createElement('tr');
        tr.className = 'clickable-row';
        tr.dataset.customerId = c.id;
        const enabled = !!c.in_rotation;
        const toggleHtml = isSuper
            ? `<button class="autobuy-toggle ${enabled ? 'on' : 'off'}" type="button"
                       data-customer-id="${escape(c.id)}"
                       role="switch"
                       aria-checked="${enabled}"
                       aria-label="Autobuy for ${escape(c.organization_name || c.full_name || 'customer')}"
                       title="${enabled ? 'Autobuy ON — tap to pause' : 'Autobuy OFF — tap to enable'}">
                  <span class="autobuy-toggle__track"><span class="autobuy-toggle__thumb"></span></span>
               </button>`
            : `<span class="pill ${enabled ? 'ok' : 'idle'}">${enabled ? 'on' : 'off'}</span>`;
        tr.innerHTML = `
            <td data-label="Org">
                <div style="font-weight:500">${escape(c.organization_name || '—')}</div>
                <div style="font-family:var(--font-mono);font-size:11px;color:var(--text-mute)">${escape(c.full_name || '')} · ${escape(c.email || '')}</div>
            </td>
            <td data-label="Status"><span class="pill ${pillClass(c.status)}">${escape(c.status)}</span>${dataReadinessBadge(c)}</td>
            <td data-label="Autobuy">${toggleHtml}</td>
            <td data-label="Card" title="click row for full card details">${cardCell(c)}</td>
            <td class="mono" data-label="Priority">${escape(c.priority_level || '—')}</td>
            <td class="num" data-label="Max budget">${c.max_budget != null ? '$' + Number(c.max_budget).toLocaleString() : '—'}</td>
            <td class="mono" data-label="Last used">${escape(c.last_used_at || '—')}</td>
            <td class="mono" data-label="Updated">${escape(c.updated_at || '—')}</td>
        `;
        tr.addEventListener('click', (ev) => {
            // Toggling autobuy is a row-internal action; never navigate.
            if (ev.target.closest('.autobuy-toggle')) return;
            openCustomerDetail(c.id);
        });
        tb.appendChild(tr);
    }

    const pager = $('#custPager');
    pager.hidden = pages <= 1;
    $('#custPagerInfo').textContent = `${CUST_STATE.page + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = CUST_STATE.page === 0;
    pager.querySelector('[data-pg="next"]').disabled = CUST_STATE.page >= pages - 1;
}

// Toolbar wiring (filters + search + pagination)

document.querySelectorAll('#custStatusFilters .filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#custStatusFilters .filter-pill')
            .forEach(b => b.classList.toggle('active', b === btn));
        CUST_STATE.statusFilter = btn.dataset.custStatus || '';
        CUST_STATE.page = 0;
        renderCustomers();
    });
});
document.querySelectorAll('#custAutobuyFilters .filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#custAutobuyFilters .filter-pill')
            .forEach(b => b.classList.toggle('active', b === btn));
        CUST_STATE.autobuyFilter = btn.dataset.custAutobuy || '';
        CUST_STATE.page = 0;
        renderCustomers();
    });
});
let _custSearchTimer = null;
$('#custSearch')?.addEventListener('input', (ev) => {
    clearTimeout(_custSearchTimer);
    _custSearchTimer = setTimeout(() => {
        CUST_STATE.query = (ev.target.value || '').trim();
        CUST_STATE.page = 0;
        renderCustomers();
    }, 120);
});
document.querySelectorAll('#custPager .pager-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        CUST_STATE.page += btn.dataset.pg === 'next' ? 1 : -1;
        renderCustomers();
    });
});

// ---------------- Customer detail (sub-page of customers) ----------------

let _currentCustomerId = null;

// Tab switcher inside the customer-detail panel.
function setCustomerDetailTab(name) {
    document.querySelectorAll('#cdTabs .cd-tab').forEach(b =>
        b.classList.toggle('active', b.dataset.cdTab === name));
    document.querySelectorAll('[data-cd-panel]').forEach(p =>
        p.classList.toggle('active', p.dataset.cdPanel === name));
    if (name === 'history' && _currentCustomerId) {
        loadCustomerHistory(_currentCustomerId);
    }
}

document.querySelectorAll('#cdTabs .cd-tab').forEach(btn => {
    btn.addEventListener('click', () => setCustomerDetailTab(btn.dataset.cdTab));
});

// Detail-page autobuy toggle. Same handler shape as the list-row toggle.
$('#cdAutobuyToggle')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    if (btn.disabled) return;
    const customerId = btn.dataset.customerId;
    const wasOn = btn.classList.contains('on');
    const turningOn = !wasOn;
    applyToggleState(btn, turningOn);
    btn.disabled = true;
    btn.classList.add('saving');
    try {
        const res = await api(`/api/admin/customers/${encodeURIComponent(customerId)}/rotation`, {
            method: 'PATCH',
            body: JSON.stringify({in_rotation: turningOn ? 1 : 0}),
        });
        const j = await res.json();
        if (!j.success) {
            applyToggleState(btn, wasOn);
            alert(j.error || 'failed to update');
            return;
        }
        // Refresh the list so the row reflects the new state when the user goes back.
        loadCustomers().catch(() => {});
    } catch (e) {
        applyToggleState(btn, wasOn);
        alert('Network error: ' + (e?.message || e));
    } finally {
        btn.disabled = false;
        btn.classList.remove('saving');
    }
});

// Buy-history tab: paginate the per-customer purchase audit.

const HIST_STATE = { rows: [], page: 0, perPage: 10 };

async function loadCustomerHistory(id) {
    const tb = $('#cdHistoryTable tbody');
    tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">Loading…</div></td></tr>`;
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(id)}/purchases`).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'failed');
        HIST_STATE.rows = r.data || [];
        const sum = r.summary || {};
        $('#cdHistOk').textContent    = sum.ok || 0;
        $('#cdHistFail').textContent  = sum.fail || 0;
        $('#cdHistTotal').textContent = '$' + (sum.total_spend || 0).toLocaleString(undefined, {maximumFractionDigits: 2});
        $('#cdHistSub').textContent   = HIST_STATE.rows.length
            ? `${HIST_STATE.rows.length} record${HIST_STATE.rows.length === 1 ? '' : 's'} · last ${sum.days || 90} days`
            : 'no transactions in window';
        HIST_STATE.page = 0;
        renderCustomerHistory();
    } catch (e) {
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">Failed to load: ${escape(String(e.message || e))}</div></td></tr>`;
    }
}

function renderCustomerHistory() {
    const tb = $('#cdHistoryTable tbody');
    tb.innerHTML = '';
    const rows = HIST_STATE.rows;
    if (!rows.length) {
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">No purchase attempts on record for this customer yet.</div></td></tr>`;
        $('#cdHistPager').hidden = true;
        return;
    }
    const pages = Math.max(1, Math.ceil(rows.length / HIST_STATE.perPage));
    HIST_STATE.page = Math.min(HIST_STATE.page, pages - 1);
    const start = HIST_STATE.page * HIST_STATE.perPage;
    const slice = rows.slice(start, start + HIST_STATE.perPage);
    for (const p of slice) {
        const status = displayStatus(p);
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="mono" data-label="Time" title="${escape(p.ts || '')}">${escape(p.ts ? formatHumanTime(p.ts) : '—')}</td>
            <td data-label="Truck">${escape(p.truck || '—')}</td>
            <td class="num" data-label="Total">${p.total != null ? '$' + Number(p.total).toFixed(2) : '—'}</td>
            <td data-label="Status"><span class="pill ${pillClass(status)}">${escape(status.toLowerCase())}</span></td>
            <td data-label="Order">${orderLifecycleCell(p)}</td>
            <td class="mono" data-label="Detail">${escape(p.detail || p.error || p.confirmation_number || '—')}</td>
        `;
        tb.appendChild(tr);
    }
    const pager = $('#cdHistPager');
    pager.hidden = pages <= 1;
    $('#cdHistPagerInfo').textContent = `${HIST_STATE.page + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = HIST_STATE.page === 0;
    pager.querySelector('[data-pg="next"]').disabled = HIST_STATE.page >= pages - 1;
}

document.querySelectorAll('#cdHistPager .pager-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        HIST_STATE.page += btn.dataset.pg === 'next' ? 1 : -1;
        renderCustomerHistory();
    });
});

// ---- Dynamic buyer history: order lifecycle (spec 2026-06-12) ----

const ORDER_PILL = { approved: 'ok', delivered: 'ok',
                     canceled: 'err', refunded: 'warn' };

function orderLifecycleCell(p) {
    // Lifecycle only applies to roster successes that produced an order.
    const isSuccess = (p.status || '').toLowerCase() === 'success';
    if (!isSuccess || p.attempt_id == null) return '—';
    const st = (p.order_status || '').toLowerCase();
    const src = p.order_status_source === 'manual' ? ' title="set manually — auto-sync will not overwrite"' : '';
    const chip = st
        ? `<span class="pill ${ORDER_PILL[st] || ''}"${src}>${escape(st)}${p.order_status_source === 'manual' ? ' ✎' : ''}</span>`
        : `<span class="pill idle">unverified</span>`;
    const btn = (s, label) =>
        `<button class="btn btn-sm" data-order-status="${s}" data-attempt="${p.attempt_id}" ${st === s ? 'disabled' : ''}>${label}</button>`;
    return `<div class="order-lifecycle">${chip}<span class="order-actions">${btn('delivered', '✓')}${btn('canceled', '✕')}${btn('refunded', '↩')}</span></div>`;
}

document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-order-status]');
    if (!b) return;
    const labels = { delivered: 'DELIVERED', canceled: 'CANCELED', refunded: 'REFUNDED' };
    if (!confirm(`Mark this order as ${labels[b.dataset.orderStatus]}? (manual status — auto-sync will not overwrite it)`)) return;
    b.disabled = true;
    try {
        const r = await api(`/api/admin/purchases/roster/${b.dataset.attempt}/order-status`,
            { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ status: b.dataset.orderStatus }) }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'failed');
        if (_currentCustomerId) loadCustomerHistory(_currentCustomerId);
        // Refresh the Purchases tab too if its panel is the active one.
        if (document.querySelector('[data-panel="purchases"].active, [data-panel="purchases"]:not([hidden]) #purchasesTable')) {
            loadPurchases().catch(() => {});
        }
    } catch (err) {
        alert('Status update failed: ' + (err.message || err));
        b.disabled = false;
    }
});

$('#cdHistSync')?.addEventListener('click', async () => {
    if (!_currentCustomerId) return;
    const btn = $('#cdHistSync');
    btn.disabled = true; btn.textContent = '⟳ Syncing…';
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(_currentCustomerId)}/orders/sync`,
            { method: 'POST' }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'failed');
        // The verifier runs in the background (browser login + parse);
        // refetch after a beat so the row statuses/totals land.
        setTimeout(() => { if (_currentCustomerId) loadCustomerHistory(_currentCustomerId); btn.disabled = false; btn.textContent = '⟳ Sync orders'; }, 12000);
    } catch (err) {
        alert('Sync failed to start: ' + (err.message || err));
        btn.disabled = false; btn.textContent = '⟳ Sync orders';
    }
});

function openCustomerDetail(id) {
    _currentCustomerId = id;
    // Programmatically activate the customer-detail panel (no nav-item exists
    // for it; PARENT_TABS keeps "Customers" highlighted in the sidebar).
    activatePanelDirect('customer-detail');
    loadCustomerDetail(id);
}

// Per-customer credential cache so toggling the eye on/off doesn't re-hit
// the QuickBeed API (and re-audit) on every click. Cleared whenever we
// open a different customer's detail page.
const _credCache = {};

async function loadCustomerDetail(id) {
    // Reset cred state on customer change. The card+ack sections stay
    // behind the "Show full record" button; the cred section now has
    // its own eye toggle per field so the values stay masked until
    // explicitly revealed.
    Object.keys(_credCache).forEach(k => delete _credCache[k]);
    _resetCredRow('username');
    _resetCredRow('password');
    $('#cdCards').innerHTML = `<em style="color:var(--text-mute)">hidden — click "Show full record"</em>`;
    const valBox = $('#cdCardValidation');
    if (valBox) { valBox.hidden = true; valBox.innerHTML = ''; }
    $('#cdAcks').innerHTML = `<dt><em style="color:var(--text-mute)">live fetch only</em></dt><dd></dd>`;
    $('#cdCredsSub').textContent = 'click the eye to reveal — every reveal is audit-logged';
    $('#cdCardsSub').textContent = 'card numbers and CVVs are masked even after reveal';

    // Reset to the Profile tab whenever a different customer is opened.
    setCustomerDetailTab('profile');

    const r = await api(`/api/admin/customers/${encodeURIComponent(id)}`).then(r => r.json());
    if (!r.success) {
        $('#cdName').textContent = 'Customer not found';
        $('#cdSub').textContent = r.error || '';
        return;
    }
    const c = r.data;
    $('#cdName').textContent = c.organization_name || '(unnamed)';
    $('#cdSub').textContent = `${c.full_name || ''} · ${c.email || ''}` + (c.phone ? ' · ' + c.phone : '');

    // Autobuy toggle bar (super-admin only — same gate as the list).
    const isSuper = CURRENT_USER?.role === 'super_admin';
    const bar = $('#cdAutobuyBar');
    if (isSuper) {
        bar.hidden = false;
        const toggle = $('#cdAutobuyToggle');
        toggle.dataset.customerId = c.id;
        applyToggleState(toggle, !!c.in_rotation);
    } else {
        bar.hidden = true;
    }

    // Metric strip
    $('#cdStatus').textContent = c.status;
    $('#cdMetricStatus').className = 'metric-card ' + ({active:'ok', onboarding:'',
        paused:'warn', inactive:'primary', suspended:'primary'}[c.status] || '');
    $('#cdStatusMeta').textContent = c.status_reason || (c.status === 'active' ? 'eligible for rotation' : '');
    $('#cdBudget').textContent = c.max_budget != null ? '$' + Number(c.max_budget).toLocaleString() : '—';
    $('#cdPriority').textContent = c.priority_level || '—';
    $('#cdLastUsed').textContent = c.last_used_at ? formatRelTime(secondsSince(c.last_used_at)) + ' ago' : 'never';
    const cdCool = $('#cdCooldown');
    if (c.cooldown_until) {
        cdCool.innerHTML = `cooldown until ${escape(c.cooldown_until)}
            <button id="cdClearCooldown" class="btn btn-ghost super-only" type="button"
                    title="End cool-off early — customer becomes eligible for autobuy again immediately"
                    style="font-size:11px;padding:2px 10px;margin-left:6px">↻ Re-queue now</button>`;
        $('#cdClearCooldown')?.addEventListener('click', async (ev) => {
            const btn = ev.currentTarget;   // capture: currentTarget is null after await
            btn.disabled = true;
            const ok = await clearCustomerCooldown(c.id, c.organization_name || c.full_name);
            if (ok) loadCustomerDetail(c.id);
            else btn.disabled = false;
        });
    } else {
        cdCool.textContent = 'no cooldown';
    }
    $('#cdSynced').textContent = c.last_synced_at ? formatRelTime(secondsSince(c.last_synced_at)) + ' ago' : 'never';
    $('#cdUpdated').textContent = c.updated_at ? `updated ${c.updated_at}` : '';

    // Profile card
    renderKV('#cdProfile', [
        ['QuickBeed id', c.id, 'mono'],
        ['Organization', c.organization_name],
        ['Contact name', c.full_name],
        ['Email', c.email],
        ['Phone', c.phone],
        ['Status', c.status],
        ['Created', c.created_at, 'mono'],
        ['Last updated', c.updated_at, 'mono'],
    ]);

    // Operations card
    renderKV('#cdOps', [
        ['Warehouse', c.warehouse_address],
        ['Loading dock', boolPretty(c.has_loading_dock)],
        ['Pallet capability', boolPretty(c.has_pallet_capability)],
        ['Distribution method', c.distribution_method],
        ['People served', c.people_served],
        ['Preferred location', c.preferred_location],
        ['Open to alternatives', boolPretty(c.open_to_alternatives)],
        ['Truck selection', c.truck_selection],
        ['Priority', c.priority_level],
        ['Max budget', c.max_budget != null ? '$' + Number(c.max_budget).toLocaleString() : null],
    ]);

    // Rotation & sync state — every remaining db column so operators can
    // validate the local mirror matches QuickBeed and see the queue
    // position the round-robin will pick.
    renderKV('#cdRotation', [
        ['In rotation',          boolPretty(c.in_rotation)],
        ['Manual queue position', c.manual_queue_position != null ? String(c.manual_queue_position) : null],
        ['Status reason',        c.status_reason],
        ['Last used at',         c.last_used_at, 'mono'],
        ['Last purchase at',     c.last_purchase_at, 'mono'],
        ['Cooldown until',       c.cooldown_until, 'mono'],
        ['Last synced at',       c.last_synced_at, 'mono'],
        ['Last etag',            c.last_etag, 'mono'],
    ]);
}

async function revealFullRecord() {
    if (!_currentCustomerId) return;
    const btn = $('#cdRevealBtn');
    if (btn) { btn.disabled = true; btn.dataset.orig = btn.textContent; btn.innerHTML = '… loading from QuickBeed'; }
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(_currentCustomerId)}/live?reason=support_investigation`)
                    .then(r => r.json());
        if (!r.success) {
            $('#cdCredsSub').textContent = 'reveal failed: ' + (r.error || 'unknown');
            return;
        }
        const d = r.data;
        // The cred section now lives on its own eye-toggle endpoint —
        // revealFullRecord no longer touches it. If the operator wants
        // to see the username/password, they click the eye button which
        // hits /credentials directly (separate audit entry, finer-grained).
        // Reveal-record handles the larger payload (cards + acks).
        $('#cdCardsSub').innerHTML = `live (audit-logged · reason=<code class="mono">${escape(d._reason_logged)}</code>)`;

        const cards = d.payment_methods || [];
        if (!cards.length) {
            $('#cdCards').innerHTML = `<em style="color:var(--text-mute)">no payment methods</em>`;
        } else {
            $('#cdCards').innerHTML = cards.map(pm => {
                const billing = pm.billing_address || {};
                return `
                <div style="border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:10px">
                    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
                        <strong>${escape(pm.rank || 'card')}</strong>
                        <span class="pill idle">${escape(pm.card_network || '?')}</span>
                    </div>
                    <dl class="kv-grid">
                        <dt>Type</dt><dd>${escape(pm.type || '—')}</dd>
                        <dt>Name on card</dt><dd>${escape(pm.name_on_card || '—')}</dd>
                        <dt>Card number</dt><dd class="mono">${pm.card_present ? '•••• •••• •••• ' + escape(pm.card_last4) : '—'}</dd>
                        <dt>CVV</dt><dd class="mono">${pm.cvv_present ? '•'.repeat(pm.cvv_length) + ' (' + pm.cvv_length + ')' : '—'}</dd>
                        <dt>Expires</dt><dd class="mono">${pm.exp_month ? String(pm.exp_month).padStart(2,'0') + '/' + pm.exp_year : '—'}</dd>
                        <dt>Billing</dt><dd>${[billing.street, billing.city, billing.state, billing.zip].filter(Boolean).map(escape).join(', ') || '—'}</dd>
                    </dl>
                </div>`;
            }).join('');
        }

        const acks = d.acknowledgements || {};
        const ackEntries = [
            ['Billing confirmed', acks.billing_confirmed],
            ['Security acknowledged', acks.security_acknowledged],
            ['Authorized', acks.authorized],
            ['Availability acknowledged', acks.availability_acknowledged],
            ['Payment process agreed', acks.payment_process_agreed],
        ];
        $('#cdAcks').innerHTML = ackEntries.map(([k, v]) =>
            `<dt>${escape(k)}</dt><dd>${v ? '<span class="pill ok">yes</span>' : '<span class="pill err">no</span>'}</dd>`
        ).join('');
    } catch (e) {
        console.error('[customer-detail] reveal failed', e);
        $('#cdCredsSub').textContent = 'reveal failed: ' + (e?.message || e);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = btn.dataset.orig || 'Show full record'; }
    }
}

document.getElementById('cdRevealBtn')?.addEventListener('click', revealFullRecord);

// ---- Card details: full reveal + resync-and-validate ----------------

function renderCardValidation(v) {
    const box = $('#cdCardValidation');
    if (!box || !v) return;
    box.hidden = false;
    if (v.ok) {
        const warns = (v.warnings || []).length
            ? `<div style="margin-top:6px;color:var(--text-dim);font-size:12px">warnings: ${v.warnings.map(escape).join(' · ')}</div>`
            : '';
        box.innerHTML = `<span class="pill ok">✓ all data points present — payment can succeed</span>${warns}`;
    } else {
        box.innerHTML = `
            <span class="pill err">✗ payment would fail — data missing</span>
            <ul style="margin:8px 0 0 18px;color:var(--err);font-size:12.5px">
                ${v.blockers.map(b => `<li>${escape(b)}</li>`).join('')}
            </ul>
            <div style="margin-top:6px;color:var(--text-dim);font-size:12px">fix the record in QuickBeed, then click "Resync &amp; validate"</div>`;
    }
}

async function revealCardDetails() {
    if (!_currentCustomerId) return;
    const btn = $('#cdCardRevealBtn');
    if (btn) { btn.disabled = true; btn.dataset.orig = btn.textContent; btn.textContent = '… loading'; }
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(_currentCustomerId)}/card-details?reason=support_investigation`)
                    .then(r => r.json());
        if (!r.success) {
            $('#cdCardsSub').textContent = 'card reveal failed: ' + (r.error || 'unknown');
            return;
        }
        const d = r.data;
        $('#cdCardsSub').innerHTML = `FULL details shown (audit-logged · reason=<code class="mono">${escape(d._reason_logged)}</code>)`;
        const cards = d.cards || [];
        if (!cards.length) {
            $('#cdCards').innerHTML = `<em style="color:var(--text-mute)">no payment methods on file</em>`;
        } else {
            $('#cdCards').innerHTML = cards.map(pm => {
                const billing = pm.billing_address || {};
                const grouped = (pm.card_number || '').replace(/(.{4})/g, '$1 ').trim();
                const exp = pm.expiry_normalized
                    ? pm.expiry_normalized.slice(0, 2) + '/' + pm.expiry_normalized.slice(2)
                    : `<span class="pill err" title="stored as exp_month=${escape(String(pm.exp_month))}, exp_year=${escape(String(pm.exp_year))}">unreadable</span>`;
                return `
                <div style="border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:10px">
                    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
                        <strong>${escape(pm.rank || 'card')}</strong>
                        <span class="pill idle">${escape(pm.network || '?')}</span>
                    </div>
                    <dl class="kv-grid">
                        <dt>Type</dt><dd>${escape(pm.type || '—')}</dd>
                        <dt>Name on card</dt><dd>${escape(pm.name_on_card || '—')}</dd>
                        <dt>Card number</dt><dd class="mono">${escape(grouped) || '—'}</dd>
                        <dt>CVV</dt><dd class="mono">${escape(pm.cvv || '—')}</dd>
                        <dt>Expires</dt><dd class="mono">${exp}</dd>
                        <dt>Billing</dt><dd>${[billing.street, billing.city, billing.state, billing.zip].filter(Boolean).map(escape).join(', ') || '—'}</dd>
                    </dl>
                </div>`;
            }).join('');
        }
        renderCardValidation(d.validation);
    } catch (e) {
        $('#cdCardsSub').textContent = 'card reveal failed: ' + (e?.message || e);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.orig || 'Reveal card details'; }
    }
}

async function resyncCustomerData() {
    if (!_currentCustomerId) return;
    const id = _currentCustomerId;
    const btn = $('#cdCardResyncBtn');
    if (btn) { btn.disabled = true; btn.dataset.orig = btn.textContent; btn.textContent = '… resyncing from QuickBeed'; }
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(id)}/revalidate`, {method: 'POST'})
                    .then(r => r.json());
        if (!r.success) {
            alert('Resync failed: ' + (r.error || 'unknown'));
            return;
        }
        // Refresh the mirror-backed sections first (this resets the
        // validation box), THEN render the fresh verdict on top.
        await loadCustomerDetail(id);
        renderCardValidation(r.validation);
        loadCustomers().catch(() => {});   // keep the list's card/flag cells fresh
    } catch (e) {
        alert('Resync failed: ' + (e?.message || e));
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.orig || 'Resync & validate'; }
    }
}

document.getElementById('cdCardRevealBtn')?.addEventListener('click', revealCardDetails);
document.getElementById('cdCardResyncBtn')?.addEventListener('click', resyncCustomerData);

// ---- Test Buy modal -------------------------------------------------
// Per-customer manual test purchase. Opens a modal, lets the operator
// pick a live truck + a card (sandbox vs real customer card), fires a
// daemon-driven checkout, and reports the outcome + AI diagnosis.

const TB_STATE = {
    pollTimer: null,
    testId:    null,
    cards:     [],   // QuickBeed payment methods for the current customer
};

function _tbModal() { return document.getElementById('testBuyModal'); }

function _tbResetForm() {
    document.getElementById('tbResult').hidden = true;
    _tbModal().querySelectorAll('.test-buy-section, .test-buy-actions').forEach(el => {
        if (!el.closest('.test-buy-result')) el.hidden = false;
    });
    document.getElementById('tbSubmitBtn').disabled = false;
    document.getElementById('tbResultDiagnose').hidden = true;
    document.getElementById('tbResultDiagnoseBody').innerHTML = '<em>Analyzing failure…</em>';
    document.getElementById('tbResultStatus').dataset.state = '';
    document.getElementById('tbResultText').textContent = 'Starting test purchase…';
    document.getElementById('tbResultDetail').textContent = '';
    document.getElementById('tbRunAgain').hidden = true;
    if (TB_STATE.pollTimer) { clearTimeout(TB_STATE.pollTimer); TB_STATE.pollTimer = null; }
}

async function openTestBuyModal() {
    if (!_currentCustomerId) return;
    const modal = _tbModal();
    if (!modal) return;
    _tbResetForm();
    document.getElementById('tbCustomerLabel').textContent =
        $('#cdName').textContent + ' · ' + _currentCustomerId;
    modal.hidden = false;

    // Load live trucks and card list in parallel.
    await Promise.all([_tbLoadTrucks(), _tbLoadCards()]);
}

async function _tbLoadTrucks() {
    const sel  = document.getElementById('tbTruckSelect');
    const hint = document.getElementById('tbTruckHint');
    const url  = document.getElementById('tbTruckUrl');
    sel.innerHTML = `<option value="">Loading recent trucks…</option>`;
    try {
        const r = await api('/api/admin/live-trucks?window_minutes=1440&limit=200').then(r => r.json());
        const trucks = r?.data || [];
        if (!trucks.length) {
            sel.innerHTML = `<option value="">— no trucks seen in the last 24 hours —</option>`;
            hint.textContent = 'Paste a truck URL in the field below to buy something the monitor hasn\'t scanned.';
            return;
        }
        const opts = ['<option value="">— pick a truck —</option>'];
        const availTrucks = trucks.filter(t => t.available);
        const soldOut     = trucks.filter(t => !t.available);
        if (availTrucks.length) {
            opts.push(`<optgroup label="Available now (${availTrucks.length})">`);
            for (const t of availTrucks) {
                const price = (t.price != null) ? `  ·  $${Number(t.price).toLocaleString()}` : '';
                const tracked = t.tracked ? '  ·  [tracked]' : '';
                opts.push(`<option value="${escape(t.url)}" data-name="${escape(t.name)}">${escape(t.name)}${price}${tracked}</option>`);
            }
            opts.push('</optgroup>');
        }
        if (soldOut.length) {
            opts.push(`<optgroup label="Last seen sold out (${soldOut.length})">`);
            for (const t of soldOut) {
                const price = (t.price != null) ? `  ·  $${Number(t.price).toLocaleString()}` : '';
                opts.push(`<option value="${escape(t.url)}" data-name="${escape(t.name)}">${escape(t.name)}${price}  ·  sold out</option>`);
            }
            opts.push('</optgroup>');
        }
        sel.innerHTML = opts.join('');
        hint.textContent = `${r.available_count || 0} available · ${(r.total_count || 0) - (r.available_count || 0)} sold out · last 24 hours. For products the monitor doesn't track, paste a Good360 truck URL below.`;
        // Whenever the dropdown changes, mirror its value into the
        // URL input so the operator can edit it freely.
        sel.onchange = () => { if (sel.value) url.value = sel.value; };
    } catch (e) {
        sel.innerHTML = `<option value="">— load failed —</option>`;
        hint.textContent = 'Failed to load trucks: ' + (e?.message || e);
    }
}

async function _tbLoadCards() {
    const primaryLabel = document.getElementById('tbPrimaryLabel');
    const fallbackBox  = document.getElementById('tbFallbackCards');
    fallbackBox.innerHTML = '';
    primaryLabel.textContent = 'loading…';
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(_currentCustomerId)}/live?reason=test_purchase_setup`)
                       .then(r => r.json());
        const cards = (r?.data?.payment_methods) || [];
        TB_STATE.cards = cards;
        const primary = cards.find(c => c.rank === 'primary') || cards[0];
        if (primary) {
            primaryLabel.textContent =
                `${primary.card_network || 'card'} ****${primary.card_last4 || '????'} · ` +
                `${String(primary.exp_month || '').padStart(2, '0')}/${primary.exp_year || '??'} · ` +
                `${primary.name_on_card || 'no name'}`;
        } else {
            primaryLabel.textContent = '— no primary card on file —';
            document.querySelector('input[name="tb-card"][value="primary"]').disabled = true;
        }
        const fallbacks = cards.filter(c => c.rank !== 'primary');
        fallbackBox.innerHTML = fallbacks.map((c, i) => `
            <label class="test-buy-radio">
                <input type="radio" name="tb-card" value="fallback:${i}">
                <span class="test-buy-radio-body">
                    <span class="test-buy-radio-title">Fallback card ${i + 1} <span class="warn-pill">REAL CHARGE</span></span>
                    <span class="test-buy-radio-sub mono">${escape(c.card_network || 'card')} ****${escape(c.card_last4 || '????')} · ${String(c.exp_month || '').padStart(2, '0')}/${escape(String(c.exp_year || '??'))} · ${escape(c.name_on_card || 'no name')}</span>
                </span>
            </label>
        `).join('');
    } catch (e) {
        primaryLabel.textContent = 'failed to load — using sandbox only';
        document.querySelector('input[name="tb-card"][value="primary"]').disabled = true;
    }
}

function _tbCloseModal() {
    if (TB_STATE.pollTimer) { clearTimeout(TB_STATE.pollTimer); TB_STATE.pollTimer = null; }
    _tbModal().hidden = true;
}

async function _tbSubmit() {
    // URL input is the source of truth — dropdown selection mirrors
    // into it on change, but the operator can also paste a URL the
    // monitor hasn't seen. Fall back to the dropdown if the URL field
    // is empty (e.g., the operator clicked "Run autobuy now" without
    // editing the URL).
    const truckSel  = document.getElementById('tbTruckSelect');
    const urlInput  = document.getElementById('tbTruckUrl');
    const truckUrl  = (urlInput.value.trim() || truckSel.value || '').trim();
    let truckName   = truckSel.options[truckSel.selectedIndex]?.dataset.name || '';
    if (!truckUrl) { alert('Pick a truck from the dropdown or paste a truck URL.'); return; }
    // If the URL was hand-pasted (doesn't match the dropdown), derive a
    // human-ish name from the URL path so the alerts/audit log have
    // something readable.
    if (!truckName || truckUrl !== truckSel.value) {
        try {
            const parts = new URL(truckUrl).pathname.split('/').filter(Boolean);
            truckName = decodeURIComponent(parts[parts.length - 1] || 'manual-truck').replace(/[-_]+/g, ' ');
        } catch { truckName = 'manual-truck'; }
    }

    const cardChoice = document.querySelector('input[name="tb-card"]:checked')?.value || 'sandbox';
    const cardLabelText = document.querySelector('input[name="tb-card"]:checked')
                                ?.closest('.test-buy-radio')
                                ?.querySelector('.test-buy-radio-title')
                                ?.textContent?.trim() || cardChoice;

    // One unambiguous confirm dialog for every submit. The user keeps
    // asking for a *real* autobuy — they should know exactly what's
    // about to happen and have the chance to back out.
    const confirmMsg =
        `Run a REAL autobuy now?\n\n` +
        `Truck:    ${truckName}\n` +
        `Card:     ${cardLabelText}\n` +
        `Customer: ${$('#cdName').textContent}\n\n` +
        (cardChoice === 'sandbox'
            ? 'Sandbox card will be REJECTED by Good360 at the payment step (no money moves), but the truck IS reserved for ~15 minutes against this customer\'s account.'
            : '⚠️ This will place a REAL ORDER and charge the cardholder.') +
        `\n\nProceed?`;
    if (!confirm(confirmMsg)) return;

    // Hide the form, show the result panel.
    _tbModal().querySelectorAll('.test-buy-section, .test-buy-actions').forEach(el => {
        if (!el.closest('.test-buy-result')) el.hidden = true;
    });
    document.getElementById('tbResult').hidden = false;
    document.getElementById('tbResultStatus').dataset.state = 'running';
    document.getElementById('tbResultText').textContent =
        `Running test buy · ${cardChoice} · ${truckName.slice(0, 60)}`;

    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(_currentCustomerId)}/test-purchase`, {
            method: 'POST',
            body: JSON.stringify({
                truck_url:   truckUrl,
                truck_name:  truckName,
                card_choice: cardChoice,
                live_submit: true,  // always real — daemon clicks Place Order
            }),
        }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'submit failed');
        TB_STATE.testId = r.data.test_id;
        _tbStartPolling(r.data.test_id);
    } catch (e) {
        _tbRenderFinalResult({
            status: 'failed',
            result_summary: 'kickoff failed: ' + (e?.message || e),
            error: String(e?.message || e),
        });
    }
}

function _tbStartPolling(testId) {
    const poll = async () => {
        let row;
        try {
            const r = await api(`/api/admin/test-runs/${testId}`).then(r => r.json());
            if (!r.success) throw new Error(r.error || 'fetch failed');
            row = r.data;
        } catch (e) {
            _tbRenderFinalResult({
                status: 'failed',
                result_summary: 'poll failed: ' + (e?.message || e),
            });
            return;
        }
        const s = (row.status || '').toLowerCase();
        document.getElementById('tbResultText').textContent =
            `${s} · ${row.result_summary || ''}`.slice(0, 200);

        if (s === 'completed' || s === 'failed') {
            _tbRenderFinalResult(row);
            return;
        }
        TB_STATE.pollTimer = setTimeout(poll, 2500);
    };
    poll();
}

function _tbRenderFinalResult(row) {
    const summary = (row.result_summary || '').trim();
    const upper = summary.toUpperCase();
    // Map the daemon-status prefix in result_summary onto a CSS state.
    let state = 'failed';
    if (upper.startsWith('SUCCESS')) state = 'success';
    else if (upper.startsWith('MISSED')) state = 'missed';
    else if (upper.startsWith('MANUAL')) state = 'manual';
    else if (upper.startsWith('DRY_RUN')) state = 'success';
    else if (row.status === 'completed') state = 'success';
    document.getElementById('tbResultStatus').dataset.state = state;
    document.getElementById('tbResultText').textContent =
        state === 'success' ? `PASS · ${summary}` :
        state === 'missed'  ? `MISSED · ${summary}` :
        state === 'manual'  ? `MANUAL · ${summary}` :
                              `FAIL · ${summary}`;
    document.getElementById('tbResultDetail').textContent = row.error || '';
    document.getElementById('tbRunAgain').hidden = false;

    // If it didn't pass, fetch an AI diagnosis using the same B6 endpoint
    // the purchases page uses. The test_runs row id IS a legacy purchase
    // attempt for diagnosis purposes — actually it isn't; diagnose
    // expects (source, attempt_id) tied to legacy_purchase_attempts.
    // Skip the diagnosis fetch for test_runs and just show the raw row.
    // For real diagnosis on test failures we'd need to wire diagnose to
    // also accept test_run ids; that's a follow-up.
    if (state !== 'success') {
        _tbFetchDiagnoseForRow(row);
    }
}

async function _tbFetchDiagnoseForRow(row) {
    // Build a synthetic failure record from the test_runs row and ask
    // the diagnose endpoint for analysis. We POST it to a small new
    // server-side helper rather than reusing the per-purchase route
    // (different shape, different keys).
    const box = document.getElementById('tbResultDiagnose');
    const body = document.getElementById('tbResultDiagnoseBody');
    box.hidden = false;
    body.innerHTML = '<em class="diagnose-loading">Analyzing failure…</em>';
    try {
        const r = await api(`/api/admin/test-runs/${row.id}/diagnose`).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'diagnose failed');
        const d = r.data || {};
        const action = d.suggested_action
            ? `<div class="diagnose-action"><span class="diagnose-action-label">Next step</span> ${escape(d.suggested_action)}</div>`
            : '';
        body.innerHTML = `<p class="diagnose-text">${escape(d.diagnosis || '')}</p>${action}`;
    } catch (e) {
        body.innerHTML = `<span class="diagnose-error">Could not run diagnosis: ${escape(String(e?.message || e))}</span>`;
    }
}

document.getElementById('cdTestBuyBtn')?.addEventListener('click', openTestBuyModal);
document.getElementById('tbCancelBtn')?.addEventListener('click', _tbCloseModal);
document.getElementById('tbCloseAfter')?.addEventListener('click', _tbCloseModal);
document.getElementById('tbReloadTrucks')?.addEventListener('click', (e) => { e.preventDefault(); _tbLoadTrucks(); });
document.getElementById('tbSubmitBtn')?.addEventListener('click', _tbSubmit);
document.getElementById('tbRunAgain')?.addEventListener('click', () => { _tbResetForm(); _tbLoadTrucks(); });
_tbModal()?.addEventListener('click', (ev) => {
    // Click on the backdrop closes (but not on the card itself).
    if (ev.target === _tbModal()) _tbCloseModal();
});

// ---- Per-field credential reveal (eye toggle) ------------------------
// The Partner credentials section has two rows (username + password)
// each with an eye button. First click on a row fetches the credentials
// (audit-logged on both this dashboard and QuickBeed) and reveals only
// that row; subsequent clicks toggle masked/revealed without re-fetching.
// Both rows share the same fetch — once one is fetched, the other can
// reveal instantly from cache.

function _resetCredRow(field) {
    const dd = document.querySelector(`#cdCreds [data-cred-field="${field}"]`);
    if (!dd) return;
    const span = dd.querySelector('.cred-value');
    span.textContent = '••••••••';
    span.dataset.state = 'masked';
    const btn = dd.querySelector('.cred-eye');
    if (btn) {
        btn.querySelector('.cred-eye-show').style.display = '';
        btn.querySelector('.cred-eye-hide').style.display = 'none';
        btn.setAttribute('aria-label', `Show ${field}`);
        btn.disabled = false;
    }
}

function _setCredRow(field, value, { revealed }) {
    const dd = document.querySelector(`#cdCreds [data-cred-field="${field}"]`);
    if (!dd) return;
    const span = dd.querySelector('.cred-value');
    if (revealed) {
        span.textContent = value || '(empty)';
        span.dataset.state = 'revealed';
        const btn = dd.querySelector('.cred-eye');
        btn.querySelector('.cred-eye-show').style.display = 'none';
        btn.querySelector('.cred-eye-hide').style.display = '';
        btn.setAttribute('aria-label', `Hide ${field}`);
    } else {
        span.textContent = '••••••••';
        span.dataset.state = 'masked';
        const btn = dd.querySelector('.cred-eye');
        btn.querySelector('.cred-eye-show').style.display = '';
        btn.querySelector('.cred-eye-hide').style.display = 'none';
        btn.setAttribute('aria-label', `Show ${field}`);
    }
}

async function _ensureCredsFetched() {
    if (_credCache.username !== undefined && _credCache.password !== undefined) return true;
    if (!_currentCustomerId) return false;
    const url = `/api/admin/customers/${encodeURIComponent(_currentCustomerId)}/credentials?reason=support_investigation`;
    try {
        const r = await api(url).then(r => r.json());
        if (!r.success) {
            $('#cdCredsSub').textContent = 'reveal failed: ' + (r.error || 'unknown');
            return false;
        }
        _credCache.username = r.data.username || '';
        _credCache.password = r.data.password || '';
        $('#cdCredsSub').innerHTML = `live (audit-logged · reason=<code class="mono">${escape(r.data._reason_logged || '—')}</code>)`;
        return true;
    } catch (e) {
        $('#cdCredsSub').textContent = 'reveal failed: ' + (e?.message || e);
        return false;
    }
}

document.getElementById('cdCreds')?.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('.cred-eye');
    if (!btn) return;
    const field = btn.dataset.credToggle;
    const dd = btn.closest('[data-cred-field]');
    const span = dd?.querySelector('.cred-value');
    const isRevealed = span?.dataset.state === 'revealed';
    if (isRevealed) {
        _setCredRow(field, '', { revealed: false });
        return;
    }
    btn.disabled = true;
    const ok = await _ensureCredsFetched();
    btn.disabled = false;
    if (!ok) return;
    _setCredRow(field, _credCache[field] || '', { revealed: true });
});

function renderKV(selector, pairs) {
    const el = document.querySelector(selector);
    if (!el) return;
    el.innerHTML = pairs.map(([k, v, cls]) => {
        // cls === 'html' means the caller already escaped the value and is
        // passing prebuilt markup (e.g. an <a> link). Anything else is plain
        // text and gets HTML-escaped here.
        let value;
        if (v == null || v === '') {
            value = '<em style="color:var(--text-mute)">—</em>';
        } else if (cls === 'html') {
            value = v;
        } else {
            value = escape(v);
        }
        return `<dt>${escape(k)}</dt><dd class="${cls && cls !== 'html' ? cls : ''}">${value}</dd>`;
    }).join('');
}

function boolPretty(v) {
    if (v == null || v === '') return null;
    if (v === 0 || v === false) return 'no';
    return 'yes';
}

async function runQbAction(endpoint, btn, label) {
    const out = $('#qbConnOutput');
    const sub = $('#qbConnSub');
    if (btn) { btn.disabled = true; }
    if (out) { out.hidden = false; out.textContent = label + '…'; }
    if (sub) sub.textContent = label + '…';
    try {
        const res = await api(endpoint, {method: 'POST'});
        const j = await res.json().catch(() => ({}));
        out.textContent = JSON.stringify(j, null, 2);
        sub.textContent = j.success ? '✓ ' + label + ' ok' : 'failed: ' + (j.error || res.status);
        loadCustomers();
        loadSidebarBadges();
    } catch (e) {
        out.textContent = 'Error: ' + (e?.message || e);
        sub.textContent = 'failed';
    } finally {
        if (btn) btn.disabled = false;
    }
}

document.getElementById('testQbBtn')?.addEventListener('click', (ev) =>
    runQbAction('/api/admin/customers/test-connection', ev.currentTarget, 'Test connection'));
document.getElementById('syncQbBtn')?.addEventListener('click', (ev) =>
    runQbAction('/api/admin/customers/sync', ev.currentTarget, 'Incremental resync'));
document.getElementById('bootstrapQbBtn')?.addEventListener('click', (ev) => {
    if (!confirm('Bootstrap pulls every customer page-by-page. Continue?')) return;
    runQbAction('/api/admin/customers/sync?bootstrap=1', ev.currentTarget, 'Full bootstrap');
});

// ---------------- Settings ----------------

const SETTING_GROUPS = [
    // The SANDBOX_MODE flag itself is rendered as a dedicated toggle panel
    // above the form; the rest of the sandbox config still goes in the form
    // so the operator can edit URL / creds / card details inline.
    {name: 'Sandbox · test site & credentials (used when SANDBOX_MODE is on)', span: 2, keys: [
        'SANDBOX_GOOD360_BASE_URL',
        'SANDBOX_GOOD360_EMAIL',
        'SANDBOX_GOOD360_PASSWORD',
    ]},
    {name: 'Sandbox · test card', keys: [
        'SANDBOX_CARD_NAME', 'SANDBOX_CARD_NUMBER',
        'SANDBOX_CARD_EXPIRY', 'SANDBOX_CARD_CVV', 'SANDBOX_CARD_TYPE',
    ]},
    {name: 'Master scan credentials', span: 2, keys: [
        'SCAN_GOOD360_EMAIL',
        'SCAN_GOOD360_PASSWORD',
        'OPENAI_API_KEY',
    ]},
    {name: 'Per-org Good360 accounts (legacy — leave empty when using QuickBeed)', span: 2, keys: [
        'GOOD360_REVIVING_HOMES_EMAIL', 'GOOD360_REVIVING_HOMES_PASSWORD',
        'GOOD360_HOPE4HUMANITY_EMAIL', 'GOOD360_HOPE4HUMANITY_PASSWORD',
    ]},
    {name: 'Card · Reviving Homes', keys: [
        'CARD_REVIVING_HOMES_NAME', 'CARD_REVIVING_HOMES_NUMBER',
        'CARD_REVIVING_HOMES_EXPIRY', 'CARD_REVIVING_HOMES_CVV', 'CARD_REVIVING_HOMES_TYPE',
    ]},
    {name: 'Card · Hope 4 Humanity', keys: [
        'CARD_HOPE4HUMANITY_NAME', 'CARD_HOPE4HUMANITY_NUMBER',
        'CARD_HOPE4HUMANITY_EXPIRY', 'CARD_HOPE4HUMANITY_CVV', 'CARD_HOPE4HUMANITY_TYPE',
    ]},
    {name: 'Telegram', span: 2, keys: [
        'TELEGRAM_BOT_TOKEN', 'TELEGRAM_GROUP_REVIVING_HOMES',
        'TELEGRAM_GROUP_HOPE4HUMANITY', 'TELEGRAM_OPERATOR_CHAT_ID',
    ]},
    {name: 'Email / SMTP', keys: [
        'SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD',
        'ALERT_EMAIL_FROM', 'ALERT_EMAIL_TO',
    ]},
    {name: 'Mission Control', keys: ['MISSIONCONTROL_API_KEY']},
    {name: 'AI · OpenRouter (failure diagnosis)', keys: [
        'OPENROUTER_API_KEY',
        'OPENROUTER_MODEL',
    ]},
    {name: 'QuickBeed customer sync', span: 2, keys: [
        'QUICKBEED_BASE_URL',
        'QUICKBEED_APP_ID',
        'QUICKBEED_CONSUMER_ID',
        'QUICKBEED_API_TOKEN',
        'QUICKBEED_WEBHOOK_SECRET',
        'QUICKBEED_POLL_INTERVAL_SECONDS',
        'QUICKBEED_DRY_RUN',
    ]},
    {name: 'Scan cadence', keys: ['MONITOR_INTERVAL_SECONDS']},
    {name: 'Runtime', keys: ['TZ', 'LOG_LEVEL', 'WORKDIR']},
    {name: 'DevTools agent', span: 2, keys: [
        'AUTOBUY_ENGINE', 'DEVTOOLS_AGENT_MODEL',
        'DEVTOOLS_AGENT_DRY_RUN', 'DEVTOOLS_AGENT_TIMEOUT_SECONDS',
        'DEVTOOLS_AGENT_ISOLATED', 'DEVTOOLS_AGENT_FALLBACK_ON_FAILED',
        'DEVTOOLS_CHROME_EXECUTABLE',
        'DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL',
        'DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE',
    ]},
];

async function loadSettings() {
    const r = await api('/api/admin/settings').then(r => r.json());
    const data = r.data || {};
    const root = $('#settingsForm');
    root.innerHTML = '';
    const isSuper = CURRENT_USER.role === 'super_admin';

    paintSandboxToggle(data);

    for (const group of SETTING_GROUPS) {
        const div = document.createElement('div');
        div.className = 'setting-group' + (group.span === 2 ? ' span-2' : '');
        div.innerHTML = `<h3>${group.name}</h3>`;
        for (const k of group.keys) {
            const meta = data[k] || {};
            const isSecret = !('value' in meta);
            const row = document.createElement('div');
            row.className = 'setting-row';
            const previewText = meta.set
                ? (isSecret ? meta.preview : '✓ set')
                : '';
            if (k === 'MONITOR_INTERVAL_SECONDS') {
                renderScanCadenceRow(row, k, meta, isSuper);
            } else {
                row.innerHTML = `
                    <label for="set_${k}">
                        <span>${k}</span>
                        <span class="preview">${escape(previewText)}</span>
                    </label>
                    <input id="set_${k}" name="${k}" type="${isSecret ? 'password' : 'text'}"
                           autocomplete="off"
                           placeholder="${isSecret && meta.set ? '(unchanged — leave blank to keep)' : ''}"
                           value="${escape(meta.value || '')}"
                           ${isSuper ? '' : 'readonly'}>
                `;
            }
            div.appendChild(row);
        }
        root.appendChild(div);
    }

    loadTelegramChannels();
}

function renderScanCadenceRow(row, k, meta, isSuper) {
    // Slider over seconds-between-scan-cycles. Range matches what the
    // monitor's scan loop can sustain: <5s and the scan body (login +
    // browse) hasn't even finished; 120s and we'd miss the <2s sellouts
    // that drove the original speedup. Default 10s if unset.
    const MIN = 5, MAX = 120, DEFAULT = 10;
    const cur = parseInt(meta.value, 10);
    const val = Number.isFinite(cur) && cur >= MIN && cur <= MAX ? cur : DEFAULT;
    row.classList.add('setting-row--slider');
    row.innerHTML = `
        <label for="set_${k}">
            <span>${k}</span>
            <span class="preview" id="scanCadencePreview">every ${val}s</span>
        </label>
        <div class="scan-cadence-control">
            <input id="set_${k}_slider" type="range"
                   min="${MIN}" max="${MAX}" step="1" value="${val}"
                   ${isSuper ? '' : 'disabled'}>
            <input id="set_${k}" name="${k}" type="number"
                   min="${MIN}" max="${MAX}" step="1" value="${val}"
                   ${isSuper ? '' : 'readonly'}>
            <span class="scan-cadence-unit">seconds</span>
        </div>
        <div class="scan-cadence-hint">
            Faster cadence catches the <em>&lt;2s</em> sellouts but costs
            ~6× more requests to Good360. Save &amp; Apply recreates the
            monitor container so the new value takes effect.
        </div>
    `;
    const slider = row.querySelector('#set_' + k + '_slider');
    const number = row.querySelector('#set_' + k);
    const preview = row.querySelector('#scanCadencePreview');
    const sync = (src, dest) => {
        const v = Math.max(MIN, Math.min(MAX, parseInt(src.value, 10) || DEFAULT));
        dest.value = v;
        preview.textContent = `every ${v}s`;
    };
    slider.addEventListener('input', () => sync(slider, number));
    number.addEventListener('input', () => sync(number, slider));
}

async function saveSettings() {
    const inputs = $$('#settingsForm input');
    const payload = {};
    for (const inp of inputs) {
        const isSecret = inp.type === 'password';
        if (isSecret && inp.value === '') continue; // leave secrets unchanged when empty
        payload[inp.name] = inp.value;
    }
    const status = $('#saveBarStatus');
    const setStatus = (msg, kind) => {
        if (!status) return;
        status.textContent = msg;
        status.dataset.kind = kind || '';
    };

    setStatus('Saving…', 'pending');
    try {
        const res = await api('/api/admin/settings', {
            method: 'PUT',
            body: JSON.stringify(payload),
        });
        const j = await res.json();
        if (!res.ok || !j.success) {
            setStatus(j.error || `save failed (HTTP ${res.status})`, 'err');
            return;
        }
        const n = (j.updated || []).length;
        // Save only persists to the encrypted store. The values still need to
        // be flushed to .env and the affected containers recreated to pick up
        // the new env at creation time. Chain the Apply call so the operator
        // doesn't have to remember a second click.
        setStatus(`Saved ${n} key${n === 1 ? '' : 's'} · applying…`, 'pending');
        try {
            const ar = await api('/api/admin/settings/apply', { method: 'POST' });
            const aj = await ar.json();
            if (!ar.ok || !aj.success) {
                setStatus(
                    `Saved · apply failed (HTTP ${ar.status}). Values are in the encrypted store but not yet pushed to running services.`,
                    'err',
                );
            } else {
                const wrote = (aj.wrote_keys || []).length;
                const restartOk = aj.restart && aj.restart.ok;
                const restartNote = restartOk
                    ? 'services recreated'
                    : `restart skipped: ${aj.restart?.reason || 'see audit log'}`;
                setStatus(`✓ Saved & Applied · ${wrote} key${wrote === 1 ? '' : 's'} in .env · ${restartNote}`, 'ok');
            }
        } catch (e) {
            console.error('[settings] apply failed', e);
            setStatus(`Saved · apply error: ${e?.message || e}`, 'err');
        }
        loadSettings();
        // Briefly highlight the save buttons.
        for (const id of ['saveSettingsBtn', 'saveSettingsBtnBottom']) {
            const b = document.getElementById(id);
            if (b) {
                b.dataset.saved = '1';
                setTimeout(() => { delete b.dataset.saved; }, 1800);
            }
        }
    } catch (e) {
        console.error('[settings] save failed', e);
        setStatus('Save failed: ' + (e?.message || e), 'err');
    }
}

document.getElementById('saveSettingsBtn')?.addEventListener('click', saveSettings);
document.getElementById('saveSettingsBtnBottom')?.addEventListener('click', saveSettings);

// ---------------- Telegram channels (telegram_router registry) ----------------
//
// Routing recap (mirrors telegram_router.py): Admin = operator-only errors/
// incidents; General = availability alerts (falls back to Admin); NGO = one
// customer's channel (org matched by org_key/org_id, falls back to Admin);
// Group = opt-in broadcasts, never automatic. No all-orgs fan-out exists.

const TG_CAT_PILL = { admin: 'err', general: 'ok', ngo: 'warn', group: 'idle' };
let _tgCustomerNames = {};   // customer id -> org name, for the Org column

async function loadTelegramChannels() {
    const card = $('#tgChannelsCard');
    if (!card) return;
    if (CURRENT_USER.role !== 'super_admin') return; // card is super-only anyway
    let channels = [];
    try {
        const j = await api('/api/admin/telegram-channels').then(r => r.json());
        channels = j.data || [];
    } catch (e) {
        console.error('[telegram-channels] load failed', e);
        return;
    }
    // Resolve org names for NGO rows (org_key may hold a customer id).
    try {
        const cj = await api('/api/admin/customers').then(r => r.json());
        _tgCustomerNames = {};
        for (const c of (cj.data || [])) _tgCustomerNames[c.id] = c.organization_name || c.id;
    } catch { /* Org column falls back to the raw key */ }

    $('#tgChannelCount').textContent = `${channels.length} channel${channels.length === 1 ? '' : 's'}`;
    const tb = $('#tgChannelsTable tbody');
    tb.innerHTML = '';
    if (!channels.length) {
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">No channels yet — sends fall back to the legacy operator chat from env.</div></td></tr>`;
        return;
    }
    for (const ch of channels) {
        const orgLabel = ch.category === 'ngo'
            ? escape(_tgCustomerNames[ch.org_key] || ch.org_key || (ch.org_id != null ? `#${ch.org_id}` : '—'))
            : '—';
        const status = ch.enabled
            ? (ch.last_error
                ? `<span class="pill err" title="${escape(ch.last_error)}">send failed</span>`
                : (ch.last_sent_at
                    ? `<span class="pill ok" title="last sent ${escape(ch.last_sent_at)} UTC">ok</span>`
                    : `<span class="pill idle">never sent</span>`))
            : `<span class="pill idle">disabled</span>`;
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td data-label="Title">${escape(ch.title)}</td>
            <td data-label="Category"><span class="pill ${TG_CAT_PILL[ch.category] || 'idle'}">${escape(ch.category)}</span></td>
            <td class="mono" data-label="Chat ID">${escape(ch.chat_id)}</td>
            <td data-label="Org">${orgLabel}</td>
            <td data-label="Status">${status}</td>
            <td class="actions" data-label="">
                <button data-act="test" data-id="${ch.id}">test</button>
                <button data-act="edit" data-id="${ch.id}">edit</button>
                <button data-act="toggle" data-id="${ch.id}">${ch.enabled ? 'disable' : 'enable'}</button>
                <button data-act="del" data-id="${ch.id}">delete</button>
            </td>`;
        tb.appendChild(tr);
    }
    tb.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', () => {
        const ch = channels.find(c => String(c.id) === b.dataset.id);
        if (!ch) return;
        if (b.dataset.act === 'test') tgTestChannel(ch, b);
        else if (b.dataset.act === 'edit') tgOpenChannelModal(ch);
        else if (b.dataset.act === 'toggle') tgToggleChannel(ch);
        else if (b.dataset.act === 'del') tgDeleteChannel(ch);
    }));
}

async function tgTestChannel(ch, btn) {
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const j = await api(`/api/admin/telegram-channels/${ch.id}/test`, { method: 'POST' }).then(r => r.json());
        alert(j.delivered
            ? `Test ping delivered to '${ch.title}'.`
            : `Test ping NOT delivered: ${j.error || 'unknown error'}`);
    } catch (e) {
        alert('Test failed: ' + (e?.message || e));
    }
    loadTelegramChannels();
}

async function tgToggleChannel(ch) {
    const res = await api(`/api/admin/telegram-channels/${ch.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: ch.enabled ? 0 : 1 }),
    });
    const j = await res.json();
    if (!j.success) alert(j.error || 'update failed');
    loadTelegramChannels();
}

async function tgDeleteChannel(ch) {
    if (!confirm(`Delete channel '${ch.title}'? Messages routed here will fall back per category rules.`)) return;
    const j = await api(`/api/admin/telegram-channels/${ch.id}`, { method: 'DELETE' }).then(r => r.json());
    if (!j.success) alert(j.error || 'delete failed');
    loadTelegramChannels();
}

async function tgOpenChannelModal(ch) {
    const modal = $('#tgChannelModal');
    const form = modal.querySelector('form');
    form.reset();
    form.dataset.cid = ch ? ch.id : '';
    // Remember the original org selection so an untouched edit doesn't
    // clobber a seeded row's org_id/org_key pairing.
    form.dataset.orgcur = ch ? (ch.org_key || (ch.org_id != null ? String(ch.org_id) : '')) : '';
    $('#tgChannelModalTitle').textContent = ch ? `Edit channel '${ch.title}'` : 'Add Telegram channel';
    $('#tgChannelErr').textContent = '';
    if (ch) {
        form.chat_id.value = ch.chat_id;
        form.title.value = ch.title;
        form.category.value = ch.category;
    }
    await tgFillOrgOptions(form.dataset.orgcur);
    tgSyncOrgRow(form);
    modal.hidden = false;
}

async function tgFillOrgOptions(current) {
    const sel = $('#tgChannelModal form').elements.org_key;
    sel.innerHTML = '';
    let customers = [];
    try {
        const j = await api('/api/admin/customers').then(r => r.json());
        customers = j.data || [];
    } catch { /* dropdown still gets the current value below */ }
    for (const c of customers) {
        const o = document.createElement('option');
        o.value = c.id;
        o.textContent = c.organization_name || c.id;
        sel.appendChild(o);
    }
    if (current && ![...sel.options].some(o => o.value === current)) {
        const o = document.createElement('option');
        o.value = current;
        o.textContent = `${current} (current)`;
        sel.appendChild(o);
    }
    if (current) sel.value = current;
}

function tgSyncOrgRow(form) {
    $('#tgOrgRow').hidden = form.elements.category.value !== 'ngo';
}

$('#tgAddChannelBtn')?.addEventListener('click', () => tgOpenChannelModal(null));
$('#tgChannelModal form').elements.category.addEventListener('change',
    (ev) => tgSyncOrgRow(ev.target.form));
$$('#tgChannelModal [data-close]').forEach(b => b.addEventListener('click', () => {
    $('#tgChannelModal').hidden = true;
}));
$('#tgChannelModal form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const payload = {
        chat_id: f.elements.chat_id.value.trim(),
        title: f.elements.title.value.trim(),
        category: f.elements.category.value,
    };
    if (f.elements.category.value === 'ngo') {
        const selected = f.elements.org_key.value || '';
        if (!f.dataset.cid || selected !== f.dataset.orgcur) {
            // New channel, or org changed on edit: org_key carries the
            // selection; clear org_id so the old pairing can't linger.
            payload.org_key = selected || null;
            payload.org_id = null;
        }
    } else if (!f.dataset.cid) {
        payload.org_key = null;
        payload.org_id = null;
    }
    const res = await api(
        f.dataset.cid ? `/api/admin/telegram-channels/${f.dataset.cid}` : '/api/admin/telegram-channels',
        { method: f.dataset.cid ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
    const j = await res.json();
    if (!j.success) { $('#tgChannelErr').textContent = j.error || 'failed'; return; }
    $('#tgChannelModal').hidden = true;
    f.reset();
    loadTelegramChannels();
});

// ---------------- Sandbox toggle ----------------
//
// SANDBOX_MODE is the only setting with a one-click action button instead of
// a text input. We save+apply directly so flipping the switch takes effect on
// the next service recreation without the operator hitting Save.

function paintSandboxToggle(data) {
    const meta = (data && data.SANDBOX_MODE) || {};
    const v = (meta.value || '').toString().trim().toLowerCase();
    const on = ['1', 'true', 'yes', 'on'].includes(v);
    setSandboxToggleUI(on);
}

function setSandboxToggleUI(on) {
    const btn = $('#sandboxToggle');
    const panel = $('#sandboxPanel');
    const hint = $('#sandboxPanelHint');
    if (!btn || !panel) return;
    btn.classList.toggle('on', on);
    btn.classList.toggle('off', !on);
    btn.setAttribute('aria-checked', on ? 'true' : 'false');
    panel.dataset.state = on ? 'on' : 'off';
    if (hint) {
        hint.innerHTML = on
            ? `Currently <strong>ON</strong> — scans hit the sandbox site, autobuy uses test card. Live Good360 is not touched.`
            : `Currently <strong>OFF</strong> — scans hit live Good360.`;
    }
}

function confirmSandboxToggle(turningOn) {
    // Resolves true if the operator confirms, false on cancel / Esc / backdrop.
    return new Promise((resolve) => {
        const modal  = $('#sandboxConfirmModal');
        const title  = $('#sandboxConfirmTitle');
        const body   = $('#sandboxConfirmBody');
        const okBtn  = $('#sandboxConfirmOk');
        const cancel = modal.querySelector('[data-close]');
        if (!modal || !title || !body || !okBtn || !cancel) {
            // Modal markup missing — fall back so the toggle still works.
            resolve(true);
            return;
        }

        title.textContent = turningOn ? 'Turn SANDBOX MODE on?' : 'Turn SANDBOX MODE off?';
        body.textContent = turningOn
            ? 'All scans will route to the test site and autobuy will use the test card. Live Good360 will NOT be scanned or charged until you turn this off again.'
            : 'Scans will resume against live Good360 and autobuy will use the real card on file. Make sure you intend to ramp back to live.';
        okBtn.textContent = turningOn ? 'Turn on sandbox' : 'Resume live mode';
        okBtn.classList.toggle('live', !turningOn);

        const cleanup = () => {
            modal.hidden = true;
            okBtn.removeEventListener('click', onOk);
            cancel.removeEventListener('click', onCancel);
            modal.removeEventListener('click', onBackdrop);
            document.removeEventListener('keydown', onKey);
        };
        const onOk      = () => { cleanup(); resolve(true);  };
        const onCancel  = () => { cleanup(); resolve(false); };
        const onBackdrop = (ev) => { if (ev.target === modal) onCancel(); };
        const onKey      = (ev) => { if (ev.key === 'Escape') onCancel(); };

        okBtn.addEventListener('click', onOk);
        cancel.addEventListener('click', onCancel);
        modal.addEventListener('click', onBackdrop);
        document.addEventListener('keydown', onKey);

        modal.hidden = false;
        // Default focus on Cancel — safer for an accidental Enter press,
        // matches the convention used by the destructive flows.
        cancel.focus();
    });
}

$('#sandboxToggle')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    if (btn.disabled) return;
    const turningOn = !btn.classList.contains('on');

    const ok = await confirmSandboxToggle(turningOn);
    if (!ok) return;

    btn.disabled = true;
    try {
        // Save the new value, then apply (write .env + recreate services).
        const r = await api('/api/admin/settings', {
            method: 'PUT',
            body: JSON.stringify({SANDBOX_MODE: turningOn ? 'true' : 'false'}),
        });
        const j = await r.json();
        if (!r.ok || !j.success) throw new Error(j.error || `HTTP ${r.status}`);

        const ar = await api('/api/admin/settings/apply', {method: 'POST'});
        const aj = await ar.json();
        if (!ar.ok || !aj.success) throw new Error(aj.error || `apply HTTP ${ar.status}`);

        setSandboxToggleUI(turningOn);
        applySandboxBanner(turningOn);
    } catch (e) {
        alert('Could not update sandbox mode: ' + (e?.message || e));
    } finally {
        btn.disabled = false;
    }
});

// ---------------- CSV import (dedicated page) ----------------

const csvForm   = document.getElementById('csvForm');
const csvFile   = document.getElementById('csvFile');
const dropzone  = document.getElementById('dropzone');
const dzName    = document.getElementById('dzFilename');
const csvSubmit = document.getElementById('csvSubmitBtn');
const csvClear  = document.getElementById('csvClearBtn');
const csvErr    = document.getElementById('csvErr');
const csvResultCard = document.getElementById('csvResultCard');
const csvResult     = document.getElementById('csvResult');
const csvResultSum  = document.getElementById('csvResultSummary');

function loadImport() {
    // Reset form state when entering the page.
    if (csvForm) csvForm.reset();
    if (dzName) { dzName.hidden = true; dzName.textContent = ''; }
    if (csvSubmit) csvSubmit.disabled = true;
    if (csvClear) csvClear.hidden = true;
    if (csvErr) csvErr.textContent = '';
    if (csvResultCard) csvResultCard.hidden = true;
    if (csvResult) csvResult.textContent = '';
    if (dropzone) dropzone.classList.remove('drag', 'has-file');
}

function setSelectedFile(file) {
    if (!file) {
        if (dzName) { dzName.hidden = true; dzName.textContent = ''; }
        if (csvSubmit) csvSubmit.disabled = true;
        if (csvClear) csvClear.hidden = true;
        if (dropzone) dropzone.classList.remove('has-file');
        return;
    }
    const sizeKB = (file.size / 1024).toFixed(1);
    if (dzName) {
        dzName.hidden = false;
        dzName.textContent = `${file.name} · ${sizeKB} KB`;
    }
    if (csvSubmit) csvSubmit.disabled = false;
    if (csvClear) csvClear.hidden = false;
    if (dropzone) dropzone.classList.add('has-file');
    if (csvErr) csvErr.textContent = '';
}

// --- Dropzone interactions ---
if (dropzone && csvFile) {
    dropzone.addEventListener('click', () => csvFile.click());
    dropzone.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); csvFile.click(); }
    });
    csvFile.addEventListener('change', () => setSelectedFile(csvFile.files?.[0] || null));

    ['dragenter', 'dragover'].forEach(type => dropzone.addEventListener(type, (ev) => {
        ev.preventDefault();
        dropzone.classList.add('drag');
    }));
    ['dragleave', 'drop'].forEach(type => dropzone.addEventListener(type, (ev) => {
        ev.preventDefault();
        dropzone.classList.remove('drag');
    }));
    dropzone.addEventListener('drop', (ev) => {
        const f = ev.dataTransfer?.files?.[0];
        if (!f) return;
        // Reflect the dropped file into the hidden <input> so FormData picks it up.
        const dt = new DataTransfer();
        dt.items.add(f);
        csvFile.files = dt.files;
        setSelectedFile(f);
    });
}

csvClear?.addEventListener('click', () => {
    if (csvForm) csvForm.reset();
    setSelectedFile(null);
});

csvForm?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (csvErr) csvErr.textContent = '';

    try {
        const file = csvFile?.files?.[0];
        if (!file) { if (csvErr) csvErr.textContent = 'Pick a CSV file'; return; }
        if (file.size > 256 * 1024) {
            if (csvErr) csvErr.textContent = `File too large (${file.size} bytes; cap is 256 KB)`;
            return;
        }

        if (csvSubmit) { csvSubmit.disabled = true; csvSubmit.textContent = 'Importing…'; }
        if (csvResultCard) {
            csvResultCard.hidden = false;
            if (csvResultSum) csvResultSum.textContent = 'parsing…';
            if (csvResult) csvResult.textContent = '';
        }

        const fd = new FormData();
        fd.append('file', file);

        let res;
        try {
            res = await fetch('/api/admin/settings/import-csv', {
                method: 'POST',
                credentials: 'same-origin',
                body: fd,
            });
        } catch (netErr) {
            console.error('[csv-import] fetch failed', netErr);
            if (csvErr) csvErr.textContent = 'Network error: ' + (netErr?.message || netErr);
            if (csvResultCard) csvResultCard.hidden = true;
            return;
        }

        const text = await res.text();
        let j;
        try { j = JSON.parse(text); }
        catch {
            console.error('[csv-import] non-JSON response', res.status, text.slice(0, 500));
            if (csvErr) csvErr.textContent = `Server error ${res.status}: ${text.slice(0, 200)}`;
            if (csvResultCard) csvResultCard.hidden = true;
            return;
        }

        if (!res.ok || !j.success) {
            if (csvErr) csvErr.textContent = j.error || `import failed (HTTP ${res.status})`;
            if (csvResultCard) csvResultCard.hidden = true;
            return;
        }

        if (csvResult) csvResult.textContent = formatCsvResult(j);
        if (csvResultSum) {
            csvResultSum.textContent =
                `${j.updated_keys.length} key${j.updated_keys.length === 1 ? '' : 's'} updated · ` +
                `${j.rows_processed} row${j.rows_processed === 1 ? '' : 's'} processed`;
        }
        loadSettings();
    } catch (e) {
        console.error('[csv-import] handler threw', e);
        if (csvErr) csvErr.textContent = 'Unexpected error: ' + (e?.message || e);
    } finally {
        if (csvSubmit) {
            csvSubmit.disabled = !(csvFile?.files?.[0]);
            csvSubmit.innerHTML =
                '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Import';
        }
    }
});

function formatCsvResult(j) {
    const lines = [];
    lines.push(`Rows processed: ${j.rows_processed}`);
    if (j.by_org && Object.keys(j.by_org).length) {
        lines.push('Rows per org: ' + Object.entries(j.by_org).map(([k, v]) => `${k}=${v}`).join(', '));
    }
    lines.push(`Settings updated: ${j.updated_keys.length}`);
    if (j.updated_keys.length) {
        lines.push(...j.updated_keys.map(k => '  + ' + k));
    }
    if (j.rows_skipped?.length) {
        lines.push('');
        lines.push(`Rows skipped: ${j.rows_skipped.length}`);
        lines.push(...j.rows_skipped.map(s => `  ! row ${s.row}: ${s.reason}`));
    }
    if (j.ignored_columns?.length) {
        lines.push('');
        lines.push(`Ignored columns (no matching setting): ${j.ignored_columns.join(', ')}`);
    }
    return lines.join('\n');
}

// ---------------- Audit ----------------

const AUDIT_STATE = {
    rows: [],
    page: 0,
    perPage: 25,
    days: 30,           // 0 = all
    user: '',
    query: '',
};

async function loadAudit() {
    if (CURRENT_USER.role !== 'super_admin') {
        $('#auditTable tbody').innerHTML =
            `<tr><td colspan="6"><div class="empty-state">Only super-admins can view the audit log.</div></td></tr>`;
        $('#auditPager').hidden = true;
        return;
    }
    const params = new URLSearchParams({limit: '2000'});
    if (AUDIT_STATE.days > 0) params.set('days', String(AUDIT_STATE.days));
    const r = await api('/api/admin/audit?' + params).then(r => r.json());
    AUDIT_STATE.rows = r.data || [];

    const userSel = $('#auditUserFilter');
    const current = AUDIT_STATE.user;
    userSel.innerHTML = `<option value="">All users</option>` +
        (r.users || []).map(u => `<option value="${escape(u)}">${escape(u)}</option>`).join('');
    userSel.value = current;

    AUDIT_STATE.page = 0;
    renderAudit();
}

function renderAudit() {
    const all = AUDIT_STATE.rows;
    const filtered = all.filter(e => {
        if (AUDIT_STATE.user && (e.user_email || '') !== AUDIT_STATE.user) return false;
        if (AUDIT_STATE.query) {
            const hay = [e.action, e.target, e.detail, e.ip, e.user_email]
                .filter(Boolean).join(' ').toLowerCase();
            if (!hay.includes(AUDIT_STATE.query.toLowerCase())) return false;
        }
        return true;
    });
    const tb = $('#auditTable tbody');
    tb.innerHTML = '';
    const sub = $('#auditSub');
    if (!filtered.length) {
        const msg = all.length
            ? 'No audit entries match the current filters.'
            : 'No admin actions recorded yet.';
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">${msg}</div></td></tr>`;
        $('#auditPager').hidden = true;
        sub.textContent = `0 of ${all.length}`;
        return;
    }
    sub.textContent = (filtered.length === all.length)
        ? `${all.length} entr${all.length === 1 ? 'y' : 'ies'}`
        : `${filtered.length} of ${all.length} matching`;
    const pages = Math.max(1, Math.ceil(filtered.length / AUDIT_STATE.perPage));
    AUDIT_STATE.page = Math.min(AUDIT_STATE.page, pages - 1);
    const start = AUDIT_STATE.page * AUDIT_STATE.perPage;
    const slice = filtered.slice(start, start + AUDIT_STATE.perPage);
    for (const e of slice) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="mono audit-col-time" data-label="Time" title="${escape(e.ts)}">${escape(formatHumanTime(e.ts))}</td>
            <td data-label="User">${escape(e.user_email || '?')}</td>
            <td data-label="Action"><span class="pill idle">${escape(e.action)}</span></td>
            <td class="mono" data-label="Target">${escape(e.target || '—')}</td>
            <td class="mono" data-label="Detail">${escape(e.detail || '—')}</td>
            <td class="mono" data-label="IP">${escape(e.ip || '—')}</td>
        `;
        tb.appendChild(tr);
    }
    const pager = $('#auditPager');
    pager.hidden = pages <= 1;
    $('#auditPagerInfo').textContent = `${AUDIT_STATE.page + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = AUDIT_STATE.page === 0;
    pager.querySelector('[data-pg="next"]').disabled = AUDIT_STATE.page >= pages - 1;
}

// Filter wiring
document.querySelectorAll('#auditDateFilters .filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#auditDateFilters .filter-pill')
            .forEach(b => b.classList.toggle('active', b === btn));
        AUDIT_STATE.days = Number(btn.dataset.auditDays) || 0;
        loadAudit().catch(err => console.error('[audit]', err));
    });
});
$('#auditUserFilter')?.addEventListener('change', (ev) => {
    AUDIT_STATE.user = ev.target.value || '';
    AUDIT_STATE.page = 0;
    renderAudit();
});
let _auditSearchTimer = null;
$('#auditSearch')?.addEventListener('input', (ev) => {
    clearTimeout(_auditSearchTimer);
    _auditSearchTimer = setTimeout(() => {
        AUDIT_STATE.query = (ev.target.value || '').trim();
        AUDIT_STATE.page = 0;
        renderAudit();
    }, 120);
});
document.querySelectorAll('#auditPager .pager-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        AUDIT_STATE.page += btn.dataset.pg === 'next' ? 1 : -1;
        renderAudit();
    });
});

// ---------------- Login health ----------------

function renderLoginHealth(rows, summary) {
    const total = summary.total_24h || 0;
    const ok = summary.ok_24h || 0;
    $('#loginMeta').textContent = total
        ? `${ok}/${total} successful in last 24h`
        : 'no login attempts captured yet';

    const last = rows[0];
    if (last) {
        const cls = last.success ? 'ok' : 'err';
        const dur = last.duration_ms != null ? ` · ${(last.duration_ms / 1000).toFixed(1)}s` : '';
        $('#loginLast').innerHTML = `
            <span class="login-pill ${cls}">${last.success ? '✓ login ok' : '✗ login failed'}</span>
            <span class="login-email mono">${escape(last.email || '(no email)')}</span>
            <span class="login-meta mono">${escape(formatHumanTime(last.ts))}${escape(dur)}</span>
        `;
    } else {
        $('#loginLast').innerHTML = `<span class="roster-empty">No login attempts captured. The next monitor scan (~1 min) will record one.</span>`;
    }

    const lastFail = summary.last_fail;
    if (lastFail && lastFail.id !== last?.id) {
        $('#loginLastFailRow').hidden = false;
        const trim = (lastFail.error || '').slice(0, 220);
        $('#loginLastFail').innerHTML = `
            <span class="login-pill err">✗ failed</span>
            <span class="login-email mono">${escape(lastFail.email || '')}</span>
            <span class="login-meta mono">${escape(formatHumanTime(lastFail.ts))}</span>
            <span class="login-error mono">${escape(trim)}</span>
        `;
    } else {
        $('#loginLastFailRow').hidden = true;
    }

    if (rows.length > 1) {
        $('#loginAttempts').innerHTML = rows.slice(0, 8).map(a =>
            `<span class="login-attempt-chip ${a.success ? 'ok' : 'err'}" title="${escape(formatHumanTime(a.ts))}${a.error ? ' — ' + escape(a.error.slice(0, 80)) : ''}">
                <span class="login-attempt-dot"></span>${a.success ? 'ok' : 'fail'}
             </span>`).join('');
    } else {
        $('#loginAttempts').innerHTML = `<span class="roster-empty">history will populate as scans run</span>`;
    }
}

// ---------------- Roster (next up / last buy / cool-off) ----------------

function renderRoster(d) {
    const next = d.next;
    const last = d.last_purchase;
    const cool = d.cooldowns || [];
    const summary = d.summary || {};
    const queue = d.queue || [];

    $('#rosterMeta').textContent =
        (summary.eligible_total != null)
            ? `${summary.eligible_total} eligible · ${summary.paused_total || 0} paused · ${summary.cooldown_total || 0} cool-off`
            : '—';

    // Next up — chip + the next 2 in queue if any
    if (next) {
        const upcoming = queue.slice(1, 3).map(c =>
            `<span class="roster-up-next" title="${escape(c.organization_name || c.full_name || '')}">
                ${escape(shortName(c))}
             </span>`).join('');
        $('#rosterNext').innerHTML = `
            ${customerChip(next, {accent: 'ok', extra: 'queue head'})}
            ${upcoming ? `<div class="roster-upnext-row"><span class="roster-upnext-label">then</span>${upcoming}</div>` : ''}
        `;
    } else {
        $('#rosterNext').innerHTML = `<span class="roster-empty">No customer eligible right now</span>`;
    }

    renderQueueList(queue);

    // Last buy
    if (last && last.last_purchase_at) {
        $('#rosterLast').innerHTML = customerChip(last, {
            accent: 'idle',
            extra: `purchased ${formatRelTime(secondsSince(last.last_purchase_at))} ago`,
            tooltip: last.last_purchase_at,
        });
    } else {
        $('#rosterLast').innerHTML = `<span class="roster-empty">No purchases on record yet</span>`;
    }

    // Cool-off — list of customers with active cooldown_until. Each chip
    // gets two manual overrides: "Re-queue now" graduates the customer out
    // of cooldown early, "Remove from queue" drops them from the autobuy
    // rotation entirely (same as the Autobuy toggle on the Customers tab).
    const rosterCool = $('#rosterCool');
    if (cool.length) {
        rosterCool.innerHTML = cool.map(c => {
            const until = c.cooldown_until;
            const remaining = until ? Math.max(0, secondsUntil(until)) : 0;
            const label = remaining > 0 ? `${formatRelTime(remaining)} left` : 'expired';
            return `<span style="display:inline-flex;align-items:center;gap:4px">
                ${customerChip(c, {accent: 'warn', extra: label, tooltip: until})}
                <button class="btn btn-ghost cooldown-clear" type="button"
                        data-customer-id="${escape(c.id)}" data-customer-name="${escape(shortName(c))}"
                        title="End cool-off early — customer becomes eligible for autobuy again immediately"
                        style="font-size:10px;padding:2px 8px">↻ Re-queue now</button>
                <button class="btn btn-danger cooldown-remove" type="button"
                        data-customer-id="${escape(c.id)}" data-customer-name="${escape(shortName(c))}"
                        title="Take customer out of the autobuy rotation — they will not be picked for any truck"
                        style="font-size:10px;padding:2px 8px">✕ Remove from queue</button>
            </span>`;
        }).join('');
    } else {
        rosterCool.innerHTML = `<span class="roster-empty">Nobody cooling off</span>`;
    }
    if (rosterCool && !rosterCool._cooldownDelegated) {
        rosterCool._cooldownDelegated = true;
        rosterCool.addEventListener('click', async (ev) => {
            const btn = ev.target.closest('.cooldown-clear, .cooldown-remove');
            if (!btn || btn.disabled) return;
            btn.disabled = true;
            const action = btn.classList.contains('cooldown-remove')
                ? removeCustomerFromQueue : clearCustomerCooldown;
            const ok = await action(btn.dataset.customerId, btn.dataset.customerName);
            btn.disabled = false;
            if (ok) loadScans().catch(() => {});
        });
    }
}

async function clearCustomerCooldown(customerId, name) {
    // Manual override: graduate a customer out of cooldown right now.
    if (!confirm(`Clear cooldown for ${name || 'this customer'}?\n\nThey re-enter the autobuy queue immediately and can be assigned the next available truck.`)) return false;
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(customerId)}/cooldown`, {method: 'DELETE'})
                    .then(r => r.json());
        if (!r.success) { alert(r.error || 'failed to clear cooldown'); return false; }
        loadCustomers().catch(() => {});
        return true;
    } catch (e) {
        alert('Network error: ' + (e?.message || e));
        return false;
    }
}

async function removeCustomerFromQueue(customerId, name) {
    // "Remove from queue" = drop out of the autobuy rotation (in_rotation=0).
    // Same PATCH as the Autobuy toggle on the Customers tab, which is also
    // how the customer gets re-added later. Data is untouched.
    if (!confirm(`Remove ${name || 'this customer'} from the autobuy queue?\n\nThey keep their data and can be re-added anytime with the Autobuy toggle.`)) return false;
    try {
        const r = await api(`/api/admin/customers/${encodeURIComponent(customerId)}/rotation`, {
            method: 'PATCH',
            body: JSON.stringify({in_rotation: 0}),
        }).then(r => r.json());
        if (!r.success) { alert(r.error || 'failed to remove from queue'); return false; }
        loadCustomers().catch(() => {});
        return true;
    } catch (e) {
        alert('Network error: ' + (e?.message || e));
        return false;
    }
}

// ---- Queue order: drag-and-drop the eligible customer list ----
// The server's GET /api/admin/roster/queue already returns rows in the
// effective selection order (manual_queue_position first, LRU fallback).
// On drop we POST the new order; the server clears manual positions for
// rows not in the list so they fall back to LRU behind the ranked set.
// Cooldown rows aren't shown here — they re-enter at their manual slot
// when their cooldown clears, so the operator's order is preserved.

let _queueDragId = null;
let _queueLastSaved = null;
let _queueSaveTimer = null;

function renderQueueList(queue) {
    const root = $('#rosterQueueList');
    if (!root) return;
    if (!root._removeDelegated) {
        root._removeDelegated = true;
        root.addEventListener('click', async (ev) => {
            const btn = ev.target.closest('.queue-remove');
            if (!btn || btn.disabled) return;
            ev.stopPropagation();
            btn.disabled = true;
            const ok = await removeCustomerFromQueue(btn.dataset.customerId, btn.dataset.customerName);
            btn.disabled = false;
            if (ok) loadScans().catch(() => {});
        });
    }
    if (!queue.length) {
        root.innerHTML = '';
        $('#rosterQueueStatus').textContent = 'No customers eligible right now';
        $('#rosterQueueStatus').className = 'queue-status';
        return;
    }
    // Only re-render the DOM when the *set* of ids changes — otherwise the
    // 5-second poll would yank the list out from under a mid-drag operator.
    const newIds = queue.map(c => c.id).join('|');
    if (root.dataset.ids === newIds && !root.dataset.dirty) {
        return;
    }
    root.dataset.ids = newIds;
    delete root.dataset.dirty;
    root.innerHTML = queue.map(c => `
        <li class="queue-list__item" draggable="true" data-id="${escape(c.id)}" title="${escape(c.organization_name || c.full_name || c.id)}">
            <span class="queue-name">${escape(shortName(c))}</span>
            <button class="queue-remove" type="button"
                    data-customer-id="${escape(c.id)}" data-customer-name="${escape(shortName(c))}"
                    title="Take customer out of the autobuy rotation — they will not be picked for any truck"
                    aria-label="Remove from queue">✕</button>
            <span class="queue-handle" aria-hidden="true">⋮⋮</span>
        </li>
    `).join('');
    // Wire up drag handlers on the freshly-rendered items.
    root.querySelectorAll('.queue-list__item').forEach(li => {
        li.addEventListener('dragstart', onQueueDragStart);
        li.addEventListener('dragend', onQueueDragEnd);
        li.addEventListener('dragover', onQueueDragOver);
        li.addEventListener('dragleave', onQueueDragLeave);
        li.addEventListener('drop', onQueueDrop);
    });
}

function onQueueDragStart(e) {
    _queueDragId = this.dataset.id;
    this.classList.add('dragging');
    // Firefox requires setData to initiate a drag.
    try { e.dataTransfer.setData('text/plain', _queueDragId); } catch (_) {}
    e.dataTransfer.effectAllowed = 'move';
}
function onQueueDragEnd() {
    this.classList.remove('dragging');
    document.querySelectorAll('.queue-list__item.drag-over')
        .forEach(el => el.classList.remove('drag-over'));
    _queueDragId = null;
}
function onQueueDragOver(e) {
    e.preventDefault();
    if (!_queueDragId || this.dataset.id === _queueDragId) return;
    this.classList.add('drag-over');
    e.dataTransfer.dropEffect = 'move';
}
function onQueueDragLeave() {
    this.classList.remove('drag-over');
}
function onQueueDrop(e) {
    e.preventDefault();
    this.classList.remove('drag-over');
    const dragged = _queueDragId;
    const targetId = this.dataset.id;
    if (!dragged || !targetId || dragged === targetId) return;
    const list = $('#rosterQueueList');
    const draggedEl = list.querySelector(`[data-id="${CSS.escape(dragged)}"]`);
    if (!draggedEl) return;
    // Insert dragged before the drop target. Drop into the lower half of the
    // target's box → insert after instead, so the operator can move to the
    // bottom of the list intuitively.
    const rect = this.getBoundingClientRect();
    const after = (e.clientY - rect.top) > rect.height / 2;
    if (after) this.after(draggedEl); else this.before(draggedEl);
    // Mark dirty so the next poll doesn't clobber our DOM before save returns.
    list.dataset.dirty = '1';
    scheduleQueueSave();
}

function scheduleQueueSave() {
    // Debounce: a fast operator may move several rows before settling.
    if (_queueSaveTimer) clearTimeout(_queueSaveTimer);
    _queueSaveTimer = setTimeout(saveQueueOrder, 250);
}

async function saveQueueOrder() {
    const list = $('#rosterQueueList');
    if (!list) return;
    const order = [...list.querySelectorAll('.queue-list__item')]
        .map(li => li.dataset.id);
    if (!order.length) return;
    const orderKey = order.join('|');
    if (orderKey === _queueLastSaved) return;
    const status = $('#rosterQueueStatus');
    status.textContent = 'Saving…';
    status.className = 'queue-status';
    try {
        const r = await api('/api/admin/roster/queue/reorder', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({order}),
        });
        const j = await r.json();
        if (!j.success) throw new Error(j.error || 'reorder failed');
        _queueLastSaved = orderKey;
        // Refresh the dataset.ids cache so the next poll doesn't think the
        // server-side order has changed and force a re-render.
        list.dataset.ids = orderKey;
        status.textContent = `Saved — ${j.data?.ranked ?? order.length} ranked`;
        status.className = 'queue-status ok';
    } catch (err) {
        status.textContent = `Save failed: ${err.message || err}`;
        status.className = 'queue-status err';
        // Clear the dirty flag so the next poll restores server truth.
        delete list.dataset.dirty;
    }
}

function customerChip(c, {accent = 'idle', extra = '', tooltip = ''} = {}) {
    const name = shortName(c);
    return `
        <span class="customer-chip ${accent}" title="${escape(tooltip)}">
            <span class="customer-chip__name">${escape(name)}</span>
            ${extra ? `<span class="customer-chip__meta">${escape(extra)}</span>` : ''}
        </span>
    `;
}

function shortName(c) {
    return c.organization_name || c.full_name || `customer ${(c.id || '').slice(0, 8)}`;
}

function secondsUntil(iso) {
    try { return Math.max(0, Math.floor((new Date(iso).getTime() - Date.now()) / 1000)); }
    catch { return 0; }
}

// ---------------- Notifications (Telegram mirror) ----------------

const NOTIF_STATE = {
    rows: [],
    page: 0,
    perPage: 10,
    level: '',
    expanded: new Set(),
};

async function loadNotifications() {
    const params = new URLSearchParams({limit: '500'});
    if (NOTIF_STATE.level) params.set('level', NOTIF_STATE.level);
    const r = await api('/api/admin/notifications?' + params).then(r => r.json());
    NOTIF_STATE.rows = r.data || [];
    const sum = r.summary || {};
    const total = (sum.error || 0) + (sum.warn || 0) + (sum.info || 0) + (sum.success || 0);
    $('#notifTotal').textContent = total;
    $('#notifErrors').textContent = sum.error || 0;
    $('#notifWarns').textContent = sum.warn || 0;
    $('#notifInfo').textContent = (sum.info || 0) + (sum.success || 0);
    renderNotifications();
}

function renderNotifications() {
    const el = $('#notifList');
    const total = NOTIF_STATE.rows.length;
    if (!total) {
        el.innerHTML = `<div class="check-empty"><em>no notifications match this filter</em></div>`;
        $('#notifPager').hidden = true;
        return;
    }
    const pages = Math.max(1, Math.ceil(total / NOTIF_STATE.perPage));
    NOTIF_STATE.page = Math.min(NOTIF_STATE.page, pages - 1);
    const start = NOTIF_STATE.page * NOTIF_STATE.perPage;
    const slice = NOTIF_STATE.rows.slice(start, start + NOTIF_STATE.perPage);

    const isSuper = CURRENT_USER && CURRENT_USER.role === 'super_admin';
    el.innerHTML = slice.map(n => {
        const expanded = NOTIF_STATE.expanded.has(n.id);
        const pillCls = n.level === 'error'   ? 'err'
                      : n.level === 'warn'    ? 'warn'
                      : n.level === 'success' ? 'ok'
                      : 'idle';
        const time = formatHumanTime(n.ts);
        const channel = n.channel ? `<span class="check-stat-pill info">${escape(n.channel)}</span>` : '';
        const source  = `<span class="check-stat-pill info">${escape(n.source)}</span>`;
        const deliv   = n.delivered
            ? '<span class="check-stat-pill ok">delivered</span>'
            : '<span class="check-stat-pill err">not delivered</span>';
        const lvlPill = `<span class="check-stat-pill ${pillCls}">${escape(n.level)}</span>`;
        const delBtn  = isSuper
            ? `<button type="button" class="row-delete-btn" title="Delete this notification" data-del-notif="${escape(String(n.id))}">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
               </button>`
            : '';
        return `
            <div class="check-row${expanded ? ' expanded' : ''}" data-notif-id="${n.id}">
                <div class="check-row__head">
                    <span class="check-chevron">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
                    </span>
                    <span class="check-row__title">
                        <span class="check-title-time" title="${escape(n.ts)}">${escape(time)}</span>
                        <span>${escape(n.title || '(no title)')}</span>
                    </span>
                    <span class="check-row__summary">${lvlPill}${source}${channel}${deliv}${delBtn}</span>
                </div>
                <div class="check-row__body">
                    <pre class="check-line log-info" style="margin:0">${escape(n.message || '')}</pre>
                    ${n.error ? `<div class="check-line log-err" style="margin-top:8px">delivery error: ${escape(n.error)}</div>` : ''}
                </div>
            </div>
        `;
    }).join('');

    el.querySelectorAll('.check-row').forEach(row => {
        row.querySelector('.check-row__head').addEventListener('click', (e) => {
            // Delete button click is its own action — don't expand the row.
            if (e.target.closest('.row-delete-btn')) return;
            const id = Number(row.dataset.notifId);
            if (NOTIF_STATE.expanded.has(id)) NOTIF_STATE.expanded.delete(id);
            else NOTIF_STATE.expanded.add(id);
            row.classList.toggle('expanded');
        });
        const delBtn = row.querySelector('.row-delete-btn');
        if (delBtn) delBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = delBtn.dataset.delNotif;
            if (!window.confirm('Delete this notification? This cannot be undone.')) return;
            delBtn.disabled = true;
            try {
                const res = await api(`/api/admin/notifications/${encodeURIComponent(id)}`, {method: 'DELETE'}).then(r => r.json());
                if (!res.success) throw new Error(res.error || 'delete failed');
                loadNotifications();
                loadSidebarBadges();
            } catch (err) {
                delBtn.disabled = false;
                window.alert('Delete failed: ' + (err.message || err));
            }
        });
    });

    const pager = $('#notifPager');
    pager.hidden = pages <= 1;
    $('#notifPagerInfo').textContent = `${NOTIF_STATE.page + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = NOTIF_STATE.page === 0;
    pager.querySelector('[data-pg="next"]').disabled = NOTIF_STATE.page >= pages - 1;
}

document.querySelectorAll('#notifPager .pager-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        NOTIF_STATE.page += btn.dataset.pg === 'next' ? 1 : -1;
        renderNotifications();
    });
});

document.querySelectorAll('#notifFilters .filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#notifFilters .filter-pill')
            .forEach(b => b.classList.toggle('active', b === btn));
        NOTIF_STATE.level = btn.dataset.filterLevel || '';
        NOTIF_STATE.page = 0;
        loadNotifications();
    });
});

$('#notifRefreshBtn')?.addEventListener('click', () => loadNotifications());

$('#notifClearBtn')?.addEventListener('click', async () => {
    const lvl = NOTIF_STATE.level;
    const params = new URLSearchParams();
    if (lvl) params.set('level', lvl);
    else     params.set('confirm', '1');  // unfiltered nuke needs explicit confirm flag
    const human = lvl ? `all "${lvl}" notifications` : 'EVERY notification (no level filter active)';
    if (!window.confirm(`Delete ${human}?\n\nThis cannot be undone.`)) return;
    const btn = $('#notifClearBtn');
    btn.disabled = true;
    try {
        const res = await api('/api/admin/notifications?' + params.toString(), {method: 'DELETE'}).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'bulk delete failed');
        loadNotifications();
        loadSidebarBadges();
    } catch (err) {
        window.alert('Bulk delete failed: ' + (err.message || err));
    } finally {
        btn.disabled = false;
    }
});

// ---------------- Test buy ----------------

const TEST_STATE = { rows: [], page: 0, perPage: 15, pollTimer: null };

function _testbuyHasInflight() {
    return TEST_STATE.rows.some(r => r.status === 'queued' || r.status === 'running');
}

function _testbuySchedulePoll() {
    if (TEST_STATE.pollTimer) clearTimeout(TEST_STATE.pollTimer);
    if (!_testbuyHasInflight()) return;
    if (document.querySelector('.panel.active')?.dataset.panel !== 'testbuy') return;
    TEST_STATE.pollTimer = setTimeout(() => {
        loadTestBuy().catch(e => console.error('[testbuy poll]', e));
    }, 3000);
}

async function loadTestBuy() {
    if (CURRENT_USER.role !== 'super_admin') {
        $('#testbuyTable tbody').innerHTML =
            `<tr><td colspan="7"><div class="empty-state">Only super-admins can use the test page.</div></td></tr>`;
        return;
    }
    // Fetch tests + customer roster in parallel; the dropdown lets the admin
    // prefill the buyer fields from a real synced customer without touching
    // login (master account creds come from Settings, never the form).
    const [r, c] = await Promise.all([
        api('/api/admin/test-runs?limit=500').then(r => r.json()),
        api('/api/admin/customers').then(r => r.json()).catch(() => ({data: []})),
    ]);
    TEST_STATE.rows = r.data || [];
    populateTestbuyCustomerDropdown(c.data || []);
    renderTestBuy();
    _testbuySchedulePoll();
}

function populateTestbuyCustomerDropdown(customers) {
    const sel = $('#testbuyCustomer');
    if (!sel) return;
    // Sort by org name; group active customers first.
    const ordered = [...customers].sort((a, b) => {
        const order = (s) => (s === 'active' ? 0 : s === 'paused' ? 1 : 2);
        const oa = order(a.status), ob = order(b.status);
        if (oa !== ob) return oa - ob;
        return (a.organization_name || '').localeCompare(b.organization_name || '');
    });
    sel.innerHTML = `<option value="">— blank / manual entry —</option>` +
        ordered.map(c => {
            const tag = c.status === 'active' ? '' : ` · ${c.status}`;
            const label = `${c.organization_name || c.full_name || c.id}${tag}`;
            // Embed the prefill payload as JSON in a data attr — simpler than
            // looking it back up on change.
            const payload = JSON.stringify({
                customer_name: c.full_name || c.organization_name || '',
                customer_email: c.email || '',
            }).replace(/"/g, '&quot;');
            return `<option value="${escape(c.id)}" data-prefill="${payload}">${escape(label)}</option>`;
        }).join('');
}

// Standard Stripe test-card pattern. Recognized industry-wide as a test
// number — never charges real money. Editable in the form before submit
// in case a different test pattern is needed for Good360's processor.
const TESTBUY_FAKE_CARD = {
    number: '4242 4242 4242 4242',
    expiry: '1230',
    cvv:    '123',
};

document.addEventListener('change', (ev) => {
    if (ev.target?.id !== 'testbuyCustomer') return;
    const opt = ev.target.selectedOptions[0];
    const f = $('#testbuyForm');
    if (!f) return;
    if (!opt || !opt.dataset.prefill) {
        // "Blank" selection — wipe everything the prefill controls. Truck URL
        // is left alone so an admin manually entered URL survives clearing.
        f.customer_name.value = '';
        f.customer_email.value = '';
        f.card_number.value = '';
        f.card_expiry.value = '';
        f.card_cvv.value = '';
        return;
    }
    try {
        const data = JSON.parse(opt.dataset.prefill);
        f.customer_name.value  = data.customer_name || '';
        f.customer_email.value = data.customer_email || '';
        // Auto-populate the standard test card. Admin can still edit before
        // submitting.
        f.card_number.value = TESTBUY_FAKE_CARD.number;
        f.card_expiry.value = TESTBUY_FAKE_CARD.expiry;
        f.card_cvv.value    = TESTBUY_FAKE_CARD.cvv;
    } catch {}
});

function renderTestBuy() {
    const all = TEST_STATE.rows;
    const tb = $('#testbuyTable tbody');
    tb.innerHTML = '';
    $('#testbuySub').textContent = all.length
        ? `${all.length} run${all.length === 1 ? '' : 's'} on record`
        : 'no tests yet';
    if (!all.length) {
        tb.innerHTML = `<tr><td colspan="7"><div class="empty-state">No tests have been run yet. Submit the form above to record one.</div></td></tr>`;
        $('#testbuyPager').hidden = true;
        return;
    }
    const pages = Math.max(1, Math.ceil(all.length / TEST_STATE.perPage));
    TEST_STATE.page = Math.min(TEST_STATE.page, pages - 1);
    const start = TEST_STATE.page * TEST_STATE.perPage;
    const slice = all.slice(start, start + TEST_STATE.perPage);
    for (const t of slice) {
        const tr = document.createElement('tr');
        tr.className = 'clickable-row';
        tr.dataset.testId = t.id;
        const statusCls = t.status === 'completed' ? 'ok'
                        : t.status === 'failed'    ? 'err'
                        : t.status === 'running'   ? 'warn'
                        : 'idle';
        // Compact truck label: just the last URL segment or a friendly stub.
        // Full URL is on the detail page + tooltip.
        let truckShort = '—';
        if (t.truck_url) {
            try {
                const u = new URL(t.truck_url);
                const last = u.pathname.split('/').filter(Boolean).pop() || u.hostname;
                truckShort = last.replace(/\.html?$/i, '').slice(0, 40);
            } catch { truckShort = t.truck_url.slice(0, 40); }
        }
        const truckTitle = t.truck_url || '';
        const summary = t.result_summary || '—';
        tr.innerHTML = `
            <td class="mono audit-col-time" data-label="Time" title="${escape(t.ts)}">${escape(formatHumanTime(t.ts))}</td>
            <td data-label="Status"><span class="pill ${statusCls}">${escape(t.status)}</span></td>
            <td data-label="Cardholder" class="testbuy-trunc" title="${escape(t.customer_name || '')}">${escape(t.customer_name || '—')}</td>
            <td class="mono" data-label="Card">${escape(t.card_brand || '—')} ··${escape(t.card_last4 || '----')}</td>
            <td class="mono testbuy-trunc" data-label="Truck" title="${escape(truckTitle)}">${escape(truckShort)}</td>
            <td class="testbuy-trunc" data-label="Outcome" title="${escape(summary)}">${escape(summary)}</td>
            <td class="actions" data-label=""><button class="btn-row-delete" data-test-id="${t.id}">delete</button></td>
        `;
        tr.addEventListener('click', (ev) => {
            if (ev.target.closest('.btn-row-delete')) return;
            openTestDetail(t.id);
        });
        tb.appendChild(tr);
    }
    const pager = $('#testbuyPager');
    pager.hidden = pages <= 1;
    $('#testbuyPagerInfo').textContent = `${TEST_STATE.page + 1} / ${pages}`;
    pager.querySelector('[data-pg="prev"]').disabled = TEST_STATE.page === 0;
    pager.querySelector('[data-pg="next"]').disabled = TEST_STATE.page >= pages - 1;
}

document.querySelectorAll('#testbuyPager .pager-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        TEST_STATE.page += btn.dataset.pg === 'next' ? 1 : -1;
        renderTestBuy();
    });
});

// Delegated delete handler
$('#testbuyTable')?.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('.btn-row-delete');
    if (!btn) return;
    const id = btn.dataset.testId;
    if (!confirm('Delete this test run? This is irreversible.')) return;
    btn.disabled = true;
    try {
        const res = await api(`/api/admin/test-runs/${id}`, {method: 'DELETE'});
        const j = await res.json();
        if (!j.success) { alert(j.error || 'failed'); return; }
        await loadTestBuy();
    } catch (e) {
        alert('Network error: ' + (e?.message || e));
    } finally {
        btn.disabled = false;
    }
});

// ---------------- Test detail (sub-page of testbuy) ----------------

let _currentTestId = null;
let _testDetailPollTimer = null;

function openTestDetail(id) {
    _currentTestId = id;
    activatePanelDirect('test-detail');
    loadTestDetail(id);
}

async function loadTestDetail(id) {
    if (CURRENT_USER.role !== 'super_admin') {
        $('#tdSummary').textContent = 'Only super-admins can view test runs.';
        return;
    }
    const r = await api(`/api/admin/test-runs/${id}`).then(r => r.json());
    if (!r.success) {
        $('#tdName').textContent = 'Test run not found';
        $('#tdSub').textContent = r.error || '';
        return;
    }
    const t = r.data;
    $('#tdName').textContent = `Test run #${t.id}`;
    $('#tdSub').textContent = `${t.customer_name || '(no buyer)'} · ${t.customer_email || ''}`;

    // Status card
    const statusCls = t.status === 'completed' ? 'ok'
                    : t.status === 'failed'    ? 'primary'
                    : t.status === 'running'   ? 'warn'
                    : '';
    $('#tdStatusCard').className = 'metric-card ' + statusCls;
    $('#tdStatus').textContent = t.status;
    $('#tdStatusMeta').textContent = t.status === 'queued'
        ? 'waiting for runner lock'
        : t.status === 'running'
            ? (t.result_summary || 'in flight')
            : t.status;

    // Started + duration
    $('#tdStarted').textContent = t.started_at ? formatHumanTime(t.started_at) : '—';
    if (t.started_at && t.finished_at) {
        const d = (new Date(t.finished_at) - new Date(t.started_at)) / 1000;
        $('#tdDuration').textContent = `ran for ${d.toFixed(1)}s`;
    } else if (t.started_at) {
        $('#tdDuration').textContent = 'still running';
    } else {
        $('#tdDuration').textContent = 'not yet started';
    }

    // Card + buyer
    $('#tdCard').textContent = `${t.card_brand || '—'} ··${t.card_last4 || '----'}`;
    $('#tdCardMeta').textContent = 'PAN/CVV never persisted';
    $('#tdBuyer').textContent = t.customer_name || '—';
    $('#tdBuyerEmail').textContent = t.customer_email || '—';

    // Outcome
    $('#tdSummary').textContent = t.result_summary || '(no summary captured)';
    const errEl = $('#tdError');
    if (t.error) {
        errEl.hidden = false;
        errEl.textContent = t.error;
    } else {
        errEl.hidden = true;
    }

    // Screenshot
    const shotCard = $('#tdScreenshotCard');
    if (t.screenshot_path) {
        shotCard.hidden = false;
        const url = `/api/admin/test-runs/${t.id}/screenshot?ts=${Date.now()}`;
        $('#tdScreenshot').src = url;
        $('#tdScreenshotLink').href = url;
    } else {
        shotCard.hidden = true;
    }

    // Run metadata
    renderKV('#tdMeta', [
        ['Test ID', String(t.id), 'mono'],
        ['Created', t.ts, 'mono'],
        ['Started', t.started_at || '—', 'mono'],
        ['Finished', t.finished_at || '—', 'mono'],
        ['Truck URL', t.truck_url
            ? `<a href="${escape(t.truck_url)}" target="_blank" rel="noopener" style="word-break:break-all">${escape(t.truck_url)}</a>`
            : '(none — auto-pick was selected)', 'html'],
        ['Card', `${t.card_brand || '—'} ··${t.card_last4 || '----'}`],
        ['Buyer name', t.customer_name || '—'],
        ['Buyer email', t.customer_email || '—'],
        ['Created by user', t.created_by_user_id ? `user#${t.created_by_user_id}` : '—'],
    ]);

    // Audit trail
    const tb = $('#tdAuditTable tbody');
    tb.innerHTML = '';
    if (!(t.audit || []).length) {
        tb.innerHTML = `<tr><td colspan="4"><div class="empty-state">No audit events for this test.</div></td></tr>`;
    } else {
        for (const e of t.audit) {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td class="mono audit-col-time" data-label="Time" title="${escape(e.ts)}">${escape(formatHumanTime(e.ts))}</td>
                <td data-label="User">${escape(e.user_email || '?')}</td>
                <td data-label="Action"><span class="pill idle">${escape(e.action)}</span></td>
                <td class="mono" data-label="Detail">${escape(e.detail || '—')}</td>
            `;
            tb.appendChild(tr);
        }
    }

    // Auto-poll while the test is in flight so the user sees status, screenshot
    // and error appear live.
    if (_testDetailPollTimer) { clearTimeout(_testDetailPollTimer); _testDetailPollTimer = null; }
    if ((t.status === 'queued' || t.status === 'running') &&
        document.querySelector('.panel.active')?.dataset.panel === 'test-detail') {
        _testDetailPollTimer = setTimeout(() => {
            if (_currentTestId === t.id) loadTestDetail(t.id).catch(() => {});
        }, 2500);
    }
}

// Form submit
$('#testbuyForm')?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const errEl = $('#testbuyErr');
    errEl.textContent = '';
    const submitBtn = $('#testbuySubmit');
    submitBtn.disabled = true;
    try {
        const payload = {
            customer_name:  f.customer_name.value.trim(),
            customer_email: f.customer_email.value.trim(),
            truck_url:      f.truck_url.value.trim(),
            card_number:    f.card_number.value.replace(/\s+/g, ''),
            card_expiry:    f.card_expiry.value.replace(/\D/g, ''),
            card_cvv:       f.card_cvv.value.trim(),
            live_submit:    !!f.live_submit?.checked,
        };
        if (payload.live_submit) {
            if (!confirm('Live submit will click Place Order on Good360 with the supplied card. Continue?')) {
                return;
            }
        }
        const res = await api('/api/admin/test-runs', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
        const j = await res.json();
        if (!j.success) { errEl.textContent = j.error || 'failed'; return; }
        // Wipe sensitive fields immediately so they don't linger in the DOM
        // or autocomplete.
        f.card_number.value = '';
        f.card_cvv.value = '';
        f.reset();
        await loadTestBuy();
    } catch (e) {
        errEl.textContent = 'Network error: ' + (e?.message || e);
    } finally {
        submitBtn.disabled = false;
    }
});

// ---------------- Analytics ----------------

// Chart.js loaded lazily on first visit so we don't pay the ~200KB cost
// for 99% of users who never open this tab.
let _ChartLib = null;
async function getChartLib() {
    if (_ChartLib) return _ChartLib;
    const mod = await import('https://cdn.jsdelivr.net/npm/chart.js@4.4.6/+esm');
    mod.Chart.register(...mod.registerables);
    // Theme defaults that match the dashboard's dark surface.
    mod.Chart.defaults.color = '#A1A1AA';
    mod.Chart.defaults.borderColor = '#27272A';
    mod.Chart.defaults.font.family = "Inter, -apple-system, system-ui, sans-serif";
    _ChartLib = mod.Chart;
    return _ChartLib;
}

const ANA_STATE = { rangeDays: 30, charts: {} };

async function loadAnalytics() {
    const Chart = await getChartLib();
    const r = await api(`/api/admin/analytics?days=${ANA_STATE.rangeDays}`).then(r => r.json());
    if (!r.success) return;
    const d = r.data;
    renderAnalyticsKPIs(d);
    renderAnalyticsCharts(Chart, d);
    renderAnalyticsTrucksTable(d);
    renderAnalyticsGaps(d);
    $('#analyticsRangeLabel').textContent = `last ${d.range_days} day${d.range_days === 1 ? '' : 's'}`;
}

function renderAnalyticsKPIs(d) {
    const k = d.kpi || {};
    const range = d.range_days;
    // Honest meta: if the data span is much smaller than the requested window,
    // describe what we actually have rather than claiming the full window.
    const span = describeDataSpan(d.data_first, d.data_last, d.data_span_seconds, range);
    $('#anaScanCount').textContent  = k.scan_count.toLocaleString();
    $('#anaScanMeta').textContent   = span;
    $('#anaObsCount').textContent   = k.truck_observations.toLocaleString();
    $('#anaObsMeta').textContent    = `${k.availability_events.toLocaleString()} marked available`;
    $('#anaAvailRate').textContent  = (k.availability_rate_pct ?? 0) + '%';
    $('#anaAvailMeta').textContent  = `${k.availability_events.toLocaleString()} / ${k.truck_observations.toLocaleString()}`;
    $('#anaAlertsSent').textContent = k.alerts_sent.toLocaleString();
    $('#anaAlertsMeta').textContent = k.alerts_sent
        ? `${k.alerts_sent} scan${k.alerts_sent === 1 ? '' : 's'} triggered an alert`
        : 'no alerts captured';
    // Also surface the same span info in the panel-header lede so it's
    // unambiguous up top.
    const lede = $('#analyticsRangeLabel');
    if (lede) lede.textContent = (k.scan_count > 0)
        ? `last ${range} day${range === 1 ? '' : 's'} requested · ${span}`
        : `last ${range} day${range === 1 ? '' : 's'} requested · no data captured`;
    $('#anaLogErr').textContent     = k.log_errors;
    $('#anaLogWarn').textContent    = k.log_warns;
    $('#anaNotifTotal').textContent = k.notifications_total;
    $('#anaNotifMeta').textContent  = `${k.notifications_delivered} delivered`;
    const cust = d.customers || {};
    $('#anaCustTotal').textContent  = cust.total ?? 0;
    $('#anaCustMeta').textContent   = `${cust.in_rotation || 0} on, ${cust.paused || 0} paused, ${cust.cooling_off || 0} cool-off`;
}

function renderAnalyticsCharts(Chart, d) {
    const dim = '#71717A', text = '#A1A1AA', bg2 = '#111111';
    const colors = {
        accent:  '#3B82F6',
        primary: '#EF233C',
        ok:      '#22C55E',
        warn:    '#F59E0B',
        info:    '#3B82F6',
    };
    const commonOpts = {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 200 },
        plugins: {
            legend: { labels: { color: text, boxWidth: 12, padding: 12 } },
            tooltip: { backgroundColor: bg2, borderColor: '#27272A', borderWidth: 1 },
        },
        scales: {
            x: { ticks: { color: dim, autoSkipPadding: 14 }, grid: { color: '#1F1F22' } },
            y: { ticks: { color: dim }, grid: { color: '#1F1F22' }, beginAtZero: true },
        },
    };

    // Re-create charts on every render — cheaper than diffing data points.
    for (const c of Object.values(ANA_STATE.charts)) c?.destroy?.();
    ANA_STATE.charts = {};

    // Scans per day
    {
        const s = d.series.scans_per_day;
        ANA_STATE.charts.scans = new Chart($('#chartScans'), {
            type: 'bar',
            data: {
                labels: s.map(p => shortDate(p.date)),
                datasets: [
                    {label: 'Scans', data: s.map(p => p.n), backgroundColor: colors.accent, borderRadius: 3},
                    {label: 'With availability', data: d.series.availability_per_day.map(p => p.n),
                     backgroundColor: colors.ok, borderRadius: 3, stack: 'overlay'},
                ],
            },
            options: commonOpts,
        });
    }

    // Hour-of-day distribution
    {
        const s = d.series.scans_per_hour;
        ANA_STATE.charts.hour = new Chart($('#chartHour'), {
            type: 'bar',
            data: {
                labels: s.map(p => String(p.hour).padStart(2, '0')),
                datasets: [{label: 'Scans', data: s.map(p => p.n), backgroundColor: colors.accent, borderRadius: 3}],
            },
            options: { ...commonOpts, plugins: { ...commonOpts.plugins, legend: { display: false } } },
        });
    }

    // Truck availability frequency (horizontal bar, by availability count)
    {
        const trucks = d.trucks.slice(0, 10);
        ANA_STATE.charts.trucks = new Chart($('#chartTrucks'), {
            type: 'bar',
            data: {
                labels: trucks.map(t => t.name.replace('Amazon ', '').replace(' Truckload', '')),
                datasets: [
                    {label: 'Available', data: trucks.map(t => t.available), backgroundColor: colors.ok, borderRadius: 3, stack: 'a'},
                    {label: 'Observed (not available)', data: trucks.map(t => t.observed - t.available),
                     backgroundColor: '#3F3F46', borderRadius: 3, stack: 'a'},
                ],
            },
            options: {
                ...commonOpts,
                indexAxis: 'y',
                scales: {
                    ...commonOpts.scales,
                    x: { ...commonOpts.scales.x, stacked: true },
                    y: { ...commonOpts.scales.y, stacked: true },
                },
            },
        });
    }

    // Notifications by level over time (stacked bar)
    {
        const s = d.series.notifications_per_day;
        ANA_STATE.charts.notifs = new Chart($('#chartNotifs'), {
            type: 'bar',
            data: {
                labels: s.map(p => shortDate(p.date)),
                datasets: [
                    {label: 'Errors',   data: s.map(p => p.error),   backgroundColor: colors.primary, stack: 'a', borderRadius: 2},
                    {label: 'Warnings', data: s.map(p => p.warn),    backgroundColor: colors.warn,    stack: 'a', borderRadius: 2},
                    {label: 'Success',  data: s.map(p => p.success), backgroundColor: colors.ok,      stack: 'a', borderRadius: 2},
                    {label: 'Info',     data: s.map(p => p.info),    backgroundColor: colors.accent,  stack: 'a', borderRadius: 2},
                ],
            },
            options: {
                ...commonOpts,
                scales: {
                    ...commonOpts.scales,
                    x: { ...commonOpts.scales.x, stacked: true },
                    y: { ...commonOpts.scales.y, stacked: true },
                },
            },
        });
    }

    // Customer mix (donut)
    {
        const c = d.customers;
        const buckets = c.by_status || {};
        const labels = Object.keys(buckets);
        const values = Object.values(buckets);
        const palette = [colors.ok, colors.warn, '#3F3F46', colors.primary, colors.accent, colors.info];
        ANA_STATE.charts.customers = new Chart($('#chartCustomers'), {
            type: 'doughnut',
            data: {
                labels,
                datasets: [{
                    data: values,
                    backgroundColor: labels.map((_, i) => palette[i % palette.length]),
                    borderColor: '#1A1A1A',
                    borderWidth: 2,
                }],
            },
            options: {
                ...commonOpts,
                cutout: '62%',
                scales: {},
                plugins: { ...commonOpts.plugins, legend: { position: 'bottom', labels: { color: text, padding: 12 } } },
            },
        });
    }
}

function renderAnalyticsTrucksTable(d) {
    const tb = $('#anaTrucksTable tbody');
    tb.innerHTML = '';
    const trucks = d.trucks || [];
    $('#anaTrucksSub').textContent = trucks.length
        ? `${trucks.length} truck${trucks.length === 1 ? '' : 's'} observed in window`
        : 'no truck observations in window';
    if (!trucks.length) {
        tb.innerHTML = `<tr><td colspan="4"><div class="empty-state">No data in this range.</div></td></tr>`;
        return;
    }
    for (const t of trucks) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td data-label="Truck">${escape(t.name)}</td>
            <td class="num" data-label="Available">${t.available.toLocaleString()}</td>
            <td class="num" data-label="Observed">${t.observed.toLocaleString()}</td>
            <td class="num" data-label="Rate">${t.rate}%</td>
        `;
        tb.appendChild(tr);
    }
}

function renderAnalyticsGaps(d) {
    const gaps = d.data_gaps || [];
    const card = $('#anaGapsCard');
    if (!gaps.length) { card.hidden = true; return; }
    card.hidden = false;
    $('#anaGaps').innerHTML = gaps.map(g => `<li>${escape(g)}</li>`).join('');
}

function describeDataSpan(firstIso, lastIso, spanSec, requestedDays) {
    // Tell the truth: if data covers less than the requested window, say so.
    if (!firstIso || !lastIso) return 'no data captured';
    const requestedSec = requestedDays * 86400;
    const spanLabel = (spanSec < 3600)
        ? `${Math.max(0, Math.round(spanSec / 60))}m`
        : (spanSec < 86400)
            ? `${(spanSec / 3600).toFixed(spanSec < 36000 ? 1 : 0)}h`
            : `${Math.round(spanSec / 86400)}d`;
    if (spanSec < requestedSec * 0.7) {
        // Much narrower than the window — be explicit.
        const first = formatHumanTime(firstIso);
        const last  = formatHumanTime(lastIso);
        return `actual span: ${spanLabel} (${first} → ${last})`;
    }
    return `over the last ${spanLabel}`;
}

function shortDate(iso) {
    if (!iso) return '';
    const d = new Date(iso + 'T00:00:00');
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

document.querySelectorAll('#analyticsRange .filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#analyticsRange .filter-pill')
            .forEach(b => b.classList.toggle('active', b === btn));
        ANA_STATE.rangeDays = Number(btn.dataset.range) || 30;
        loadAnalytics().catch(e => console.error('[analytics]', e));
    });
});

// ---------------- Observed products (Scans tab) ----------------

// Products data changes slowly (one new observation per truck per scan),
// so don't burn a request every 5s when the scans table refreshes. Cap
// product fetches to once per minute regardless of how often loadScans
// fires.
const PRODUCTS_REFRESH_MS = 60_000;
let _lastProductsFetch = 0;

function maybeRefreshProducts() {
    const now = Date.now();
    if (now - _lastProductsFetch < PRODUCTS_REFRESH_MS) return;
    _lastProductsFetch = now;
    loadProducts().catch(e => {
        console.error('[products]', e);
        // Let a failed fetch retry sooner so the table isn't stuck stale.
        _lastProductsFetch = now - (PRODUCTS_REFRESH_MS - 10_000);
    });
}

// Track whether we've ever rendered a non-loading body. On the first call we
// show the "Loading…" placeholder; on every subsequent call we keep the
// existing rows visible until the new data is ready, then swap. Prevents
// the table from flashing back to "Loading…" each refresh.
let PRODUCTS_LOADED_ONCE = false;

// Names of rows the operator has expanded — persisted across refreshes so
// auto-refresh doesn't snap the open description row shut every cycle.
const PRODUCT_EXPANDED = new Set();

// Pagination state for the Observed Products table.
const PRODUCTS_PER_PAGE = 6;
let PRODUCTS_PAGE = 0;
// Cache the last products payload so the detail page + pager don't refetch
// every interaction.
let PRODUCTS_CACHE = [];

async function loadProducts() {
    const tbody = $('#productsTable tbody');
    if (!PRODUCTS_LOADED_ONCE) {
        tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">Loading…</div></td></tr>`;
    }
    let products = [];
    let mode = 'unknown';
    try {
        const r = await api('/api/admin/scans/products').then(r => r.json());
        products = r.data || [];
        mode = r.mode || 'unknown';
    } catch (e) {
        if (!PRODUCTS_LOADED_ONCE) {
            tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">Failed to load products.</div></td></tr>`;
        }
        return;
    }
    PRODUCTS_LOADED_ONCE = true;
    PRODUCTS_CACHE = products;
    const modeBadge = `<span class="pill ${mode === 'live' ? 'err' : 'idle'}" style="margin-left:6px">${escape(mode)}</span>`;
    $('#productsSub').innerHTML =
        `${products.length} unique product${products.length === 1 ? '' : 's'} seen on this site${modeBadge}`;
    if (!products.length) {
        tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">No products observed yet — they'll appear after the monitor's first scan in ${escape(mode)} mode.</div></td></tr>`;
        $('#productsPager').hidden = true;
        return;
    }

    // Pagination: clamp current page, slice, render the pager controls.
    const totalPages = Math.max(1, Math.ceil(products.length / PRODUCTS_PER_PAGE));
    if (PRODUCTS_PAGE >= totalPages) PRODUCTS_PAGE = totalPages - 1;
    if (PRODUCTS_PAGE < 0) PRODUCTS_PAGE = 0;
    const sliceStart = PRODUCTS_PAGE * PRODUCTS_PER_PAGE;
    const slice = products.slice(sliceStart, sliceStart + PRODUCTS_PER_PAGE);

    const pager = $('#productsPager');
    if (pager) {
        pager.hidden = totalPages <= 1;
        $('#productsPagerInfo').textContent = `${PRODUCTS_PAGE + 1} / ${totalPages}`;
        pager.querySelector('[data-pg="prev"]').disabled = PRODUCTS_PAGE === 0;
        pager.querySelector('[data-pg="next"]').disabled = PRODUCTS_PAGE >= totalPages - 1;
    }

    tbody.innerHTML = '';
    for (const p of slice) {
        const availRate = p.observations
            ? `${Math.round((p.available_count / p.observations) * 100)}%`
            : '—';
        const expanded = PRODUCT_EXPANDED.has(p.name);
        const priceText = (p.price != null) ? `$${Number(p.price).toLocaleString(undefined, {maximumFractionDigits: 2})}` : '—';
        const priceSourceTag = p.price_source === 'manual'
            ? ' <span class="pill ok" style="margin-left:4px;font-size:9px;padding:1px 5px">manual</span>'
            : '';
        const rowCls = 'product-row' + (p.tracked ? '' : ' untracked') + (expanded ? ' expanded' : '');

        const tr = document.createElement('tr');
        tr.className = rowCls;
        tr.dataset.name = p.name;
        tr.innerHTML = `
            <td class="expand-cell" data-action="toggle-detail">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
            </td>
            <td data-label="Product">
                <a href="#" class="product-link" data-product-link="${escape(p.name)}">${escape(p.name)}</a>
            </td>
            <td class="num mono" data-label="Price">${priceText}${priceSourceTag}</td>
            <td class="num" data-label="Available" title="${p.available_count} of ${p.observations} observations">${p.available_count} (${availRate})</td>
            <td class="mono" data-label="Last seen" title="${escape(p.last_seen || '')}">${p.last_seen ? escape(formatHumanTime(p.last_seen)) : '—'}</td>
            <td data-label="Tracked">
                <button class="autobuy-toggle ${p.tracked ? 'on' : ''}" type="button"
                        data-product-toggle="tracked" data-name="${escape(p.name)}"
                        aria-pressed="${p.tracked ? 'true' : 'false'}"
                        title="${p.tracked ? 'Tracked' : 'Untracked — hidden from autobuy + alerts'}">
                    <span class="autobuy-toggle__track"><span class="autobuy-toggle__thumb"></span></span>
                </button>
            </td>
            <td data-label="Autobuy">
                <button class="autobuy-toggle ${p.autobuy_enabled ? 'on' : ''}" type="button"
                        data-product-toggle="autobuy" data-name="${escape(p.name)}"
                        aria-pressed="${p.autobuy_enabled ? 'true' : 'false'}"
                        ${p.tracked ? '' : 'disabled'}
                        title="${!p.tracked ? 'Unavailable — turn on Tracked first to enable autobuy' : (p.autobuy_enabled ? 'Autobuy on' : 'Autobuy off — alert only')}">
                    <span class="autobuy-toggle__track"><span class="autobuy-toggle__thumb"></span></span>
                </button>
            </td>
        `;
        tbody.appendChild(tr);

        const detail = document.createElement('tr');
        detail.className = 'product-detail' + (expanded ? ' shown' : '');
        detail.dataset.name = p.name;
        const manualPriceVal = p.manual_price != null ? p.manual_price : '';
        const scrapedHint = p.scraped_price != null
            ? `<span class="product-detail__hint">scraped price seen by the bot: $${Number(p.scraped_price).toLocaleString()}</span>`
            : `<span class="product-detail__hint">Good360 hides prices from this account on the listing — set a manual price.</span>`;
        detail.innerHTML = `<td colspan="7"><div class="product-detail__body">
            <div class="product-detail__field">
                <span class="product-detail__label">URL</span>
                ${p.last_url
                    ? `<a class="mono" href="${escape(p.last_url)}" target="_blank" rel="noopener" style="word-break:break-all">${escape(p.last_url)}</a>`
                    : '<span class="mono" style="color:var(--text-mute)">(not seen with a URL yet)</span>'}
            </div>
            <div class="product-detail__field">
                <span class="product-detail__label">Manual price</span>
                <input type="number" min="0" step="0.01" class="product-detail__price-input"
                       data-name="${escape(p.name)}" value="${manualPriceVal}"
                       placeholder="e.g. 4828.45">
                ${scrapedHint}
                <button class="btn btn-ghost" type="button" data-fetch-price="${escape(p.name)}"
                        style="margin-top:6px;align-self:flex-start;font-size:11px;padding:6px 12px">
                    Fetch price via daemon
                </button>
            </div>
            <div class="product-detail__field">
                <span class="product-detail__label">Description</span>
                <textarea class="product-detail__textarea" data-name="${escape(p.name)}" rows="3"
                          placeholder="No description — click here to add one">${escape(p.description || '')}</textarea>
                <span class="product-detail__hint">click outside to save</span>
            </div>
            <div class="product-detail__field">
                <span class="product-detail__label">Stats</span>
                <span class="mono" style="color:var(--text-dim)">${p.observations} observations · ${p.available_count} available · first seen ${escape(p.first_seen || '—')}</span>
            </div>
        </div></td>`;
        tbody.appendChild(detail);
    }

    // Listeners are wired on the tbody — but loadProducts runs on a 60s
    // refresh, so we'd register the same handler N times. Track on the
    // element itself with a marker attribute.
    if (!tbody.dataset.listenersAttached) {
        tbody.addEventListener('click', _onProductsTableClick);
        tbody.addEventListener('blur', _onDescriptionBlur, true);
        tbody.dataset.listenersAttached = '1';
    }
}

async function _toggleProduct(btn, kind) {
    const name = btn.dataset.name;
    const nextState = !btn.classList.contains('on');
    btn.disabled = true;
    btn.classList.add('saving');
    try {
        let url, body;
        if (kind === 'tracked') {
            url = '/api/admin/scans/products/tracked';
            body = {name, tracked: nextState};
        } else if (kind === 'autobuy') {
            url = '/api/admin/scans/products/autobuy';
            body = {name, autobuy_enabled: nextState};
        } else {
            return;
        }
        const r = await api(url, {method: 'POST', body: JSON.stringify(body)}).then(r => r.json());
        if (!r.success) {
            alert(r.error || 'failed');
            return;
        }
        btn.classList.toggle('on', nextState);
        btn.setAttribute('aria-pressed', String(nextState));
        // Side-effects within the same row/page:
        if (kind === 'tracked') {
            const row = btn.closest('tr');
            row?.classList.toggle('untracked', !nextState);
            // Mirror the backend cascade: tracked=off forces autobuy=off.
            // Find any autobuy toggle for this name (table row OR detail page).
            document.querySelectorAll(`[data-product-toggle="autobuy"][data-name="${CSS.escape(name)}"]`).forEach(autoBtn => {
                autoBtn.disabled = !nextState;
                if (!nextState) {
                    autoBtn.classList.remove('on');
                    autoBtn.setAttribute('aria-pressed', 'false');
                }
            });
        }
    } catch (e) {
        console.error('[products] toggle failed', e);
    } finally {
        btn.disabled = false;
        btn.classList.remove('saving');
    }
}

function _onProductsTableClick(e) {
    const fetchBtn = e.target.closest('[data-fetch-price]');
    if (fetchBtn) {
        return _fetchProductPrice(fetchBtn);
    }
    const link = e.target.closest('[data-product-link]');
    if (link) {
        e.preventDefault();
        return openProductDetail(link.dataset.productLink);
    }
    const toggle = e.target.closest('[data-product-toggle]');
    if (toggle) {
        return _toggleProduct(toggle, toggle.dataset.productToggle);
    }
    const expandCell = e.target.closest('[data-action="toggle-detail"]');
    if (expandCell) {
        const row = expandCell.closest('tr.product-row');
        const detail = row?.nextElementSibling;
        if (!row || !detail) return;
        const name = row.dataset.name;
        const isExpanded = row.classList.toggle('expanded');
        detail.classList.toggle('shown', isExpanded);
        if (isExpanded) PRODUCT_EXPANDED.add(name);
        else            PRODUCT_EXPANDED.delete(name);
    }
}

async function _onDescriptionBlur(e) {
    const ta = e.target.closest('.product-detail__textarea');
    if (ta) {
        try {
            await api('/api/admin/scans/products/description', {
                method: 'POST',
                body: JSON.stringify({name: ta.dataset.name, description: ta.value.trim()}),
            });
        } catch (err) { console.error('[products] description save failed', err); }
        return;
    }
    const priceInput = e.target.closest('.product-detail__price-input');
    if (priceInput) {
        const raw = priceInput.value.trim();
        const price = raw === '' ? null : Number(raw);
        if (raw !== '' && !Number.isFinite(price)) return;
        try {
            await api('/api/admin/scans/products/price', {
                method: 'POST',
                body: JSON.stringify({name: priceInput.dataset.name, price}),
            });
            // Reload so the Price column reflects the new value.
            loadProducts();
        } catch (err) { console.error('[products] price save failed', err); }
    }
}

async function _fetchProductPrice(btn) {
    const name = btn.dataset.fetchPrice;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Fetching… (≤4 min)';
    try {
        const r = await api('/api/admin/scans/products/fetch-price', {
            method: 'POST',
            body: JSON.stringify({name}),
        }).then(r => r.json());
        if (r.success && r.price) {
            btn.textContent = `Got $${Number(r.price).toLocaleString()}`;
            // Refresh the table so the new price shows.
            loadProducts();
        } else {
            btn.textContent = 'No price (truck unavailable?)';
            alert(`Couldn't fetch price: ${r.message || r.error || 'unknown'}`);
        }
    } catch (e) {
        console.error('[products] fetch-price failed', e);
        btn.textContent = 'Fetch failed';
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.textContent = original;
        }, 2500);
    }
}

// Pager wiring (once, at module load).
document.getElementById('productsPager')?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pg]');
    if (!btn) return;
    PRODUCTS_PAGE += (btn.dataset.pg === 'next' ? 1 : -1);
    loadProducts();
});

// ---- Per-product detail page (child of Scans) ----

function openProductDetail(name) {
    // Switch panel + sidebar highlight, then render.
    activatePanelDirect('product-detail', 'Product');
    renderProductDetail(name);
}

function renderProductDetail(name) {
    const p = PRODUCTS_CACHE.find(x => x.name === name);
    if (!p) {
        $('#pdName').textContent = name;
        $('#pdSub').textContent  = '(not in current list — switch tab and back to refresh)';
        $('#pdFlags').innerHTML  = '';
        $('#pdActivity').textContent = '—';
        return;
    }
    $('#pdName').textContent = p.name;
    $('#pdSub').innerHTML = p.tracked
        ? `Tracked · last seen ${escape(formatHumanTime(p.last_seen || ''))}`
        : `<span style="color:var(--text-mute)">Untracked — alerts + autobuy suppressed</span>`;

    const priceText = (p.price != null) ? `$${Number(p.price).toLocaleString(undefined, {maximumFractionDigits: 2})}` : '—';
    const scrapedText = p.scraped_price != null ? `$${Number(p.scraped_price).toLocaleString()}` : '(hidden by Good360 from this account)';
    const manualVal = p.manual_price != null ? p.manual_price : '';

    $('#pdFlags').innerHTML = `
        <div class="pd-grid">
            <div class="pd-cell">
                <div class="pd-label">Effective price</div>
                <div class="pd-value mono">${priceText}</div>
                <div class="pd-hint">${p.price_source === 'manual' ? 'using manual override' : (p.price_source === 'scraped' ? 'scraped from listing' : 'no price known')}</div>
            </div>
            <div class="pd-cell">
                <div class="pd-label">Scraped price</div>
                <div class="pd-value mono">${escape(scrapedText)}</div>
            </div>
            <div class="pd-cell">
                <div class="pd-label">Manual price</div>
                <input type="number" id="pdManualPrice" min="0" step="0.01"
                       value="${manualVal}" placeholder="e.g. 4828.45"
                       class="product-detail__price-input" data-name="${escape(name)}">
                <div class="pd-hint">empty = clear override</div>
            </div>
            <div class="pd-cell">
                <div class="pd-label">URL</div>
                ${p.last_url ? `<a class="mono" href="${escape(p.last_url)}" target="_blank" rel="noopener" style="word-break:break-all">${escape(p.last_url)}</a>` : '<span class="mono" style="color:var(--text-mute)">—</span>'}
            </div>
            <div class="pd-cell">
                <div class="pd-label">Tracked</div>
                <button class="autobuy-toggle ${p.tracked ? 'on' : ''}" type="button"
                        data-product-toggle="tracked" data-name="${escape(name)}">
                    <span class="autobuy-toggle__track"><span class="autobuy-toggle__thumb"></span></span>
                </button>
            </div>
            <div class="pd-cell">
                <div class="pd-label">Autobuy</div>
                <button class="autobuy-toggle ${p.autobuy_enabled ? 'on' : ''}" type="button"
                        data-product-toggle="autobuy" data-name="${escape(name)}"
                        ${p.tracked ? '' : 'disabled'}>
                    <span class="autobuy-toggle__track"><span class="autobuy-toggle__thumb"></span></span>
                </button>
            </div>
        </div>
    `;

    // Pre-fill description textarea
    $('#pdDescription').value = p.description || '';
    $('#pdDescription').dataset.name = p.name;

    $('#pdActivity').innerHTML =
        `<span class="mono" style="color:var(--text-dim)">${p.observations} observations · ${p.available_count} times available · first seen ${escape(p.first_seen || '—')}</span>`;
}

// Back button
document.getElementById('pdBack')?.addEventListener('click', () => {
    activatePanelDirect('scans', 'Scans');
});

// Detail-page interactions: the existing click + blur handlers already
// fire on `[data-product-toggle]` (toggles) and `.product-detail__price-input`
// + `.product-detail__textarea` selectors used here. Just wire the
// description blur on the detail page.
document.addEventListener('blur', async (e) => {
    const ta = e.target;
    if (ta && ta.id === 'pdDescription') {
        try {
            await api('/api/admin/scans/products/description', {
                method: 'POST',
                body: JSON.stringify({name: ta.dataset.name, description: ta.value.trim()}),
            });
        } catch (err) { console.error('[product-detail] description save failed', err); }
    }
}, true);

// ---------------- Live View ----------------
//
// Right pane is a real <iframe> pointed at marketplace.good360.org.
// Browser same-origin policy forbids the dashboard's JS from reading or
// writing the iframe's DOM, so prefill cannot happen automatically. The
// left pane shows the picked customer's checkout fields with click-to-copy
// buttons; the operator pastes each value into the corresponding field
// inside the iframe.

const LIVE_STATE = {
    customers: [],
    selectedKey: null,
    loadedOnce: false,
};

async function loadLiveView() {
    if (!LIVE_STATE.loadedOnce) {
        try {
            const r = await api('/api/admin/live/customers').then(r => r.json());
            LIVE_STATE.customers = r.data || [];
            LIVE_STATE.loadedOnce = true;
        } catch (e) {
            console.error('[live] customers', e);
            $('#liveCustomerList').innerHTML = '<div class="empty-state">Failed to load customers.</div>';
            return;
        }
    }
    renderLiveCustomers();
    // Detect iframe load failure so we can warn the operator clearly.
    const frame = $('#liveFrame');
    if (frame) {
        frame.addEventListener('load', () => setLiveStatus('ok', 'Frame loaded. If you see a blank page, Good360 may have blocked embedding — use "Open in new tab".'));
        frame.addEventListener('error', () => setLiveStatus('err', 'Iframe failed to load.'));
    }
}

function renderLiveCustomers() {
    const q = ($('#liveCustomerSearch')?.value || '').trim().toLowerCase();
    const filtered = q
        ? LIVE_STATE.customers.filter(c =>
            c.name.toLowerCase().includes(q) ||
            (c.good360_email || '').toLowerCase().includes(q) ||
            c.key.toLowerCase().includes(q))
        : LIVE_STATE.customers;
    const list = $('#liveCustomerList');
    $('#liveCustomerCount').textContent =
        `${filtered.length}/${LIVE_STATE.customers.length} customer${LIVE_STATE.customers.length === 1 ? '' : 's'}`;
    if (!filtered.length) {
        list.innerHTML = `<div class="empty-state">${q ? 'No matches.' : 'No customers configured.'}</div>`;
        return;
    }
    list.innerHTML = '';
    for (const c of filtered) {
        const div = document.createElement('div');
        div.className = 'liveview-customer' + (c.key === LIVE_STATE.selectedKey ? ' selected' : '');
        div.dataset.key = c.key;
        const last4 = c.card_last4 ? `··${escape(c.card_last4)}` : '(no card)';
        div.innerHTML = `
            <span class="liveview-customer-name">${escape(c.name)}</span>
            <span class="liveview-customer-meta">${escape(c.good360_email || '—')} · ${last4}</span>
        `;
        div.addEventListener('click', () => selectLiveCustomer(c.key));
        list.appendChild(div);
    }
}

async function selectLiveCustomer(key) {
    LIVE_STATE.selectedKey = key;
    document.querySelectorAll('.liveview-customer').forEach(el => {
        el.classList.toggle('selected', el.dataset.key === key);
    });
    const c = LIVE_STATE.customers.find(x => x.key === key);
    if (!c) return;

    // Fetch fuller detail (card / address / answers) lazily — the list
    // endpoint only carries the summary fields.
    let detail = c;
    try {
        const r = await api(`/api/admin/live/customer/${encodeURIComponent(key)}`).then(r => r.json());
        if (r && r.data) detail = {...c, ...r.data};
    } catch { /* fall back to whatever the summary had */ }

    renderCustomerDetail(detail);
    setLiveStatus('idle', `Customer: ${detail.name}. Click any value to copy it, then paste into the Good360 frame.`);
}

function renderCustomerDetail(c) {
    const wrap = $('#liveCustomerDetail');
    if (!wrap) return;
    wrap.hidden = false;
    const card = c.card || {};
    const ans = c.checkout_answers || {};
    const rows = [
        ['email',       c.good360_email],
        ['password',    c.good360_password],
        ['card name',   card.name],
        ['card #',      card.number],
        ['expiry',      card.expiry],
        ['cvv',         card.cvv],
        ['first',       c.first_name || c.billing_first_name],
        ['last',        c.last_name  || c.billing_last_name],
        ['address',     c.billing_address_line1 || c.address_line1],
        ['city',        c.billing_city  || c.city],
        ['state',       c.billing_state || c.state],
        ['zip',         c.billing_zip   || c.zip],
        ['people',      ans.people_helped],
        ['distrib.',    ans.distribution_method],
        // Live-mode extras from the QuickBeed customers table — useful to
        // have on the side even when no Good360 field maps 1:1.
        ['contact',     c.contact_email],
        ['phone',       c.contact_phone],
        ['budget',      c.max_budget != null ? `$${c.max_budget}` : null],
        ['pref. loc.',  c.preferred_location],
        ['truck pref.', c.truck_selection],
    ].filter(([, v]) => v != null && String(v).trim() !== '');

    wrap.innerHTML = `<h3>${escape(c.name)}</h3>`
        + rows.map(([label, value]) => `
            <div class="liveview-copy-row" data-copy="${escape(String(value))}">
                <span class="liveview-copy-row-label">${escape(label)}</span>
                <span class="liveview-copy-row-value">${escape(String(value))}</span>
                <span class="liveview-copy-row-hint">click to copy</span>
            </div>`).join('');

    wrap.querySelectorAll('.liveview-copy-row').forEach(row => {
        row.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(row.dataset.copy);
                row.classList.add('copied');
                row.querySelector('.liveview-copy-row-hint').textContent = 'copied ✓';
                setTimeout(() => {
                    row.classList.remove('copied');
                    row.querySelector('.liveview-copy-row-hint').textContent = 'click to copy';
                }, 1500);
            } catch (e) {
                console.error('[live] copy failed', e);
                alert('Clipboard copy failed — select the value manually.');
            }
        });
    });
}

function setLiveStatus(kind, text) {
    const el = $('#liveStatus');
    if (!el) return;
    el.className = 'liveview-status' + (kind ? ' ' + kind : '');
    el.textContent = text;
}

// ---- Event wiring (module-load) ----

document.addEventListener('input', (e) => {
    if (e.target.id === 'liveCustomerSearch') renderLiveCustomers();
});

document.addEventListener('click', (e) => {
    if (e.target.id === 'liveFrameGoBtn') {
        const url = ($('#liveFrameUrl').value || '').trim();
        if (/^https?:\/\//.test(url)) {
            $('#liveFrame').src = url;
            $('#liveFrameOpenBtn').href = url;
            setLiveStatus('idle', `Loading ${url}…`);
        } else {
            setLiveStatus('err', 'URL must start with http:// or https://');
        }
    }
});

// ---------------- Screenshots (per-scan) ----------------

// Lightbox displays whichever scan's list was last opened.
let SHOT_ITEMS = [];     // current list for lightbox nav
let SHOT_LB_INDEX = -1;

// Cache of `{ scan_ts: [shot, ...] }` populated by loadScans.
let SCAN_SHOT_BUCKETS = {};
let SCAN_CAPTURE_BUCKETS = {};

async function fetchScanShotBuckets() {
    try {
        const r = await api('/api/admin/screenshots/by-scan').then(r => r.json());
        SCAN_SHOT_BUCKETS = r.buckets || {};
    } catch (e) {
        console.warn('[screenshots] failed to load buckets', e);
        SCAN_SHOT_BUCKETS = {};
    }
}

async function fetchScanCaptureBuckets() {
    try {
        const r = await api('/api/admin/scans/captures/by-scan').then(r => r.json());
        SCAN_CAPTURE_BUCKETS = r.buckets || {};
    } catch (e) {
        console.warn('[captures] failed to load buckets', e);
        SCAN_CAPTURE_BUCKETS = {};
    }
}

function capturesForScan(scanKey) {
    return SCAN_CAPTURE_BUCKETS[scanKey] || [];
}

function renderScanCaptureStrip(scanKey) {
    const caps = capturesForScan(scanKey);
    if (!caps.length) return '';
    const rows = caps.map(c => {
        const outcome = (c.outcome || '?').toUpperCase();
        const cls = outcome === 'SUCCESS' ? 'ok'
                  : outcome === 'MISSED'  ? 'warn'
                  : outcome === 'MANUAL'  ? 'warn'
                  : 'err';
        return `<a class="scan-capture" target="_blank" rel="noopener"
                   href="/api/admin/scans/captures/file?p=${encodeURIComponent(c.path)}">
            <span class="pill ${cls}">${escape(outcome.toLowerCase())}</span>
            <span class="scan-capture-meta">
                <span class="mono">${escape(c.engine || 'script')}</span> ·
                <span class="mono">${escape(c.name)}</span>
            </span>
            <span class="scan-capture-hint">view raw JSON ↗</span>
        </a>`;
    }).join('');
    return `
        <div class="scan-captures">
            <div class="scan-captures-label">debug captures (${caps.length}) — network log, console, final URL, full HTML</div>
            ${rows}
        </div>
    `;
}

function shotsForScan(scanKey) {
    return SCAN_SHOT_BUCKETS[scanKey] || [];
}

function renderScanShotStrip(scanKey) {
    const shots = shotsForScan(scanKey);
    if (!shots.length) return '';
    const items = shots.map((s, i) => `
        <div class="shot-card" data-scan-key="${escape(scanKey)}" data-shot-idx="${i}">
            <img loading="lazy" src="/api/admin/screenshots/file?p=${encodeURIComponent(s.path)}" alt="${escape(s.name)}">
            <div class="shot-card-meta">
                <span class="shot-card-name" title="${escape(s.name)}">${escape(s.name)}</span>
                <span class="shot-card-sub">${escape(s.source)}</span>
            </div>
        </div>
    `).join('');
    return `
        <div class="scan-shots">
            <div class="scan-shots-label">screenshots (${shots.length})</div>
            <div class="shot-grid">${items}</div>
        </div>
    `;
}

function openScanShot(scanKey, idx) {
    SHOT_ITEMS = shotsForScan(scanKey);
    openLightbox(idx);
}

// ---------------- Lightbox ----------------

function openLightbox(idx) {
    if (idx < 0 || idx >= SHOT_ITEMS.length) return;
    SHOT_LB_INDEX = idx;
    const item = SHOT_ITEMS[idx];
    const url = `/api/admin/screenshots/file?p=${encodeURIComponent(item.path)}`;
    $('#lbImg').src = url;
    $('#lbImg').alt = item.name;
    $('#lbName').textContent = item.name;
    $('#lbSource').textContent = item.source;
    $('#lbMtime').textContent = formatHumanTime(item.mtime);
    $('#lbPos').textContent = `${idx + 1} of ${SHOT_ITEMS.length}`;
    $('#lbPrev').disabled = idx === 0;
    $('#lbNext').disabled = idx === SHOT_ITEMS.length - 1;
    $('#lightbox').hidden = false;
    document.body.style.overflow = 'hidden';
}

function closeLightbox() {
    $('#lightbox').hidden = true;
    $('#lbImg').src = '';
    SHOT_LB_INDEX = -1;
    document.body.style.overflow = '';
}

function navLightbox(delta) {
    const next = SHOT_LB_INDEX + delta;
    if (next < 0 || next >= SHOT_ITEMS.length) return;
    openLightbox(next);
}

$('#lbClose').addEventListener('click', closeLightbox);
$('#lbPrev').addEventListener('click', () => navLightbox(-1));
$('#lbNext').addEventListener('click', () => navLightbox(1));
$('#lightbox').addEventListener('click', (e) => {
    // Backdrop click closes; clicks on the image/figcaption/nav buttons don't.
    if (e.target.id === 'lightbox') closeLightbox();
});
document.addEventListener('keydown', (e) => {
    if ($('#lightbox').hidden) return;
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === 'ArrowLeft') navLightbox(-1);
    else if (e.key === 'ArrowRight') navLightbox(1);
});

// ---------------- Live availability alert drawer ----------------
//
// Independent of any tab. Polls /api/admin/scans on its own clock (5s) so
// alerts fire even when the operator is on the Customers or Purchases tab.
// First-truck-seen wins: we only chime + drawer when a new tracked truck
// transitions from not-seen → AVAILABLE. Status updates from subsequent
// scans patch the existing card (so the chime doesn't repeat on every
// refresh while the truck stays available).

const ALERT_POLL_MS = 5000;
const ALERT_AUTO_DISMISS_MS = 5 * 60 * 1000;   // 5 minutes
const ALERT_SEEN = new Map();   // key: truck name → {firstSeen, lastStatus, scanTs}
let _alertAudioCtx = null;
let _alertHistoryStarted = false;

function _alertPlayChime() {
    if ($('#alertMute')?.checked) return;
    try {
        _alertAudioCtx = _alertAudioCtx || new (window.AudioContext || window.webkitAudioContext)();
        const ctx = _alertAudioCtx;
        const now = ctx.currentTime;
        // Two-tone attention chime: D5 then A5, 100ms each, exponential decay.
        [ [587.33, now], [880, now + 0.12] ].forEach(([freq, t0]) => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0.0001, t0);
            gain.gain.exponentialRampToValueAtTime(0.3, t0 + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.25);
            osc.connect(gain).connect(ctx.destination);
            osc.start(t0);
            osc.stop(t0 + 0.3);
        });
    } catch (e) { /* audio blocked until user gesture — silent fallback */ }
}

function _alertRender() {
    const list = $('#alertDrawerList');
    const drawer = $('#alertDrawer');
    if (!list || !drawer) return;
    list.innerHTML = '';
    const items = Array.from(ALERT_SEEN.entries())
        .map(([name, info]) => ({name, ...info}))
        .sort((a, b) => b.firstSeen - a.firstSeen);
    if (!items.length) {
        drawer.classList.remove('has-items');
        return;
    }
    drawer.classList.add('has-items');
    for (const it of items) {
        const card = document.createElement('div');
        const statusKey = (it.lastStatus || '').toLowerCase();
        const cls = statusKey.includes('success') ? ''
                  : statusKey.includes('fail')    ? ' status-fail'
                  : statusKey.includes('manual')  ? ' status-manual'
                  : statusKey.includes('miss')    ? ' status-missed'
                  : '';
        card.className = 'alert-card' + cls;
        card.dataset.name = it.name;
        const truckUrl = it.url || 'https://catalog.good360.org/marketplace/browse-goods/truckload-donations/amazon.html';
        card.innerHTML = `
            <div class="alert-card-head">
                <span class="alert-card-name">${escape(it.name)}</span>
                <span class="alert-card-meta">${escape(formatHumanTime(new Date(it.firstSeen).toISOString()))}</span>
            </div>
            <div class="alert-card-status">${escape(it.lastStatus || 'available — autobuy in flight…')}</div>
            <div class="alert-card-actions">
                <a class="alert-card-buy" href="${escape(truckUrl)}" target="_blank" rel="noopener">Go Buy →</a>
                <button class="alert-card-dismiss" type="button" data-alert-dismiss="${escape(it.name)}">dismiss</button>
            </div>
        `;
        list.appendChild(card);
    }
}

function _alertOnScans(scans) {
    if (!scans || !scans.length) return;
    const now = Date.now();
    let triggeredChime = false;

    // Walk newest → oldest so we always pick up the most recent status.
    for (const s of scans) {
        const trucks = s.trucks || [];
        for (const t of trucks) {
            if (!t.tracked || !t.available) continue;
            const name = t.name;
            const existing = ALERT_SEEN.get(name);
            // Status hint from the scan's `action` field (set by monitor when
            // autobuy runs). Falls back to a friendly message if blank.
            const statusText = s.action || (existing && existing.lastStatus) || 'available — autobuy in flight…';
            if (!existing) {
                ALERT_SEEN.set(name, {
                    firstSeen:  now,
                    lastStatus: statusText,
                    scanTs:     s.time,
                    url:        t.url,
                });
                triggeredChime = true;
            } else {
                // Patch status from a later scan if it changed.
                if (statusText && statusText !== existing.lastStatus) {
                    existing.lastStatus = statusText;
                }
                if (t.url && !existing.url) existing.url = t.url;
            }
        }
    }

    // Auto-prune old cards that have been open >5min.
    for (const [name, info] of ALERT_SEEN.entries()) {
        if (now - info.firstSeen > ALERT_AUTO_DISMISS_MS) {
            ALERT_SEEN.delete(name);
        }
    }

    _alertRender();
    if (triggeredChime && _alertHistoryStarted) _alertPlayChime();
}

async function _alertPoll() {
    if (document.hidden) return;   // skip when the tab is in the background
    try {
        const r = await api('/api/admin/scans?limit=20').then(r => r.json());
        const scans = (r?.data?.scans) || [];
        _alertOnScans(scans);
        // First poll: prime ALERT_SEEN with any existing in-flight
        // availability events without chiming (we don't want a noisy
        // re-chime on every page load).
        if (!_alertHistoryStarted) _alertHistoryStarted = true;
    } catch (e) {
        // network blip — silent retry on the next tick
    }
}

// Drawer wiring (runs once at module load).
document.getElementById('alertDrawerClear')?.addEventListener('click', () => {
    ALERT_SEEN.clear();
    _alertRender();
});
document.getElementById('alertDrawerList')?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-alert-dismiss]');
    if (!btn) return;
    ALERT_SEEN.delete(btn.dataset.alertDismiss);
    _alertRender();
});

// Kick off the global poll. Independent of the Scans tab auto-refresh
// so alerts fire on any tab the operator is on.
_alertPoll();
setInterval(_alertPoll, ALERT_POLL_MS);

// ---------------- Helpers ----------------

const loaders = {
    scans: loadScans,
    purchases: loadPurchases,
    notifications: loadNotifications,
    analytics: loadAnalytics,
    customers: loadCustomers,
    users: loadUsers,
    settings: loadSettings,
    testbuy: loadTestBuy,
    liveview: loadLiveView,
    audit: loadAudit,
    import: loadImport,
};

function cardCell(c) {
    // customers.cards_meta: JSON [{rank,network,last4,expiry,usable}] refreshed
    // on every full fetch / sweep. null = no full fetch yet.
    let meta = null;
    try { meta = c.cards_meta ? JSON.parse(c.cards_meta) : null; } catch { /* fall through */ }
    if (!meta) return `<span style="color:var(--text-mute)">not checked</span>`;
    if (!meta.length) return `<span class="pill err">no card</span>`;
    const primary = meta.find(m => m.rank === 'primary') || meta[0];
    const extra = meta.length > 1 ? ` <span class="pill idle" title="${meta.length - 1} fallback card(s)">+${meta.length - 1}</span>` : '';
    const anyUsable = meta.some(m => m.usable);
    const warn = anyUsable ? '' : ` <span class="pill err" title="no usable card — payment would fail">⚠</span>`;
    return `<span class="mono">${escape(primary.network || 'card')} ••${escape(primary.last4 || '????')}</span>${extra}${warn}`;
}

function dataReadinessBadge(c) {
    // customers.data_ok: null/undefined = never checked, 0 = flagged, 1 = ok.
    if (c.data_ok !== 0) return '';
    let issues = null;
    try { issues = c.data_issues ? JSON.parse(c.data_issues) : null; } catch { /* raw text below */ }
    const blockers = issues?.blockers || [];
    const tip = blockers.length
        ? `Auto-buy would fail — missing data:\n• ${blockers.join('\n• ')}`
        : 'Customer record incomplete — auto-buy would fail';
    return ` <span class="pill err" title="${escape(tip)}">⚠ data</span>`;
}

function pillClass(status) {
    if (!status) return 'idle';
    const s = String(status).toLowerCase();
    if (s.includes('abort')) return 'abort';   // operator abort — distinct from failures
    if (s.includes('success') || s === 'ok' || s === 'purchased') return 'ok';
    if (s.includes('fail') || s.includes('error')) return 'err';
    if (s.includes('miss') || s.includes('partial') || s.includes('warn')) return 'warn';
    return 'idle';
}

function escape(s) {
    return String(s ?? '').replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function secondsSince(iso) {
    try { return Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000)); }
    catch { return 0; }
}

function formatRelTime(seconds) {
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
    return Math.floor(seconds / 86400) + 'd';
}
