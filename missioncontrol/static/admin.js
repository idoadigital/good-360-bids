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
    audit: 'Audit',
    import: 'Import CSV',
};

// Sub-pages reachable from a parent panel — keep the parent highlighted in
// the sidebar while the child is active (e.g. Import is a child of Settings).
const PARENT_TABS = {
    import: 'settings',
    'customer-detail': 'customers',
    'test-detail': 'testbuy',
};

$$('.tab').forEach(t => t.addEventListener('click', () => {
    const name = t.dataset.tab;
    const parent = PARENT_TABS[name] || name;
    // Active state is a sidebar concept; only mutate sidebar items.
    $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.tab === parent));
    $$('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
    $('#crumbCurrent').textContent = TAB_TITLES[name] || name;
    loaders[name] && loaders[name]();
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

// ---------------- Sidebar badges ----------------

async function loadSidebarBadges() {
    try {
        const s = await api('/api/admin/scans?limit=1000').then(r => r.json());
        $('#scansBadge').textContent = (s.data?.scans || []).length;
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
    // Pull scan summary, log tail, roster queue, and login telemetry in parallel.
    const [r1, r2, r3, r4] = await Promise.all([
        api('/api/admin/scans?limit=200').then(r => r.json()),
        api('/api/admin/scans/log-tail?n=200').then(r => r.json()).catch(() => ({data: {lines: []}})),
        api('/api/admin/roster/queue').then(r => r.json()).catch(() => ({data: {}})),
        api('/api/admin/login-attempts?limit=10').then(r => r.json()).catch(() => ({data: [], summary: {}})),
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
                const delta = Math.max(15, secondsSince(prev) - secondsSince(hbIso));
                // EWMA so we converge on a stable interval rather than chase noise.
                SCAN_STATE.intervalSec = SCAN_STATE.intervalSec
                    ? Math.round(SCAN_STATE.intervalSec * 0.6 + delta * 0.4)
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
        dt.innerHTML = `<td colspan="6"><dl class="scan-detail__grid">
            <dt>captured</dt><dd>${escape(s.time || '—')}</dd>
            <dt>org</dt><dd>${escape(s.org_id || '—')}</dd>
            <dt>truck</dt><dd>${escape(s.title || '—')}</dd>
            <dt>status</dt><dd>${escape(s.status || '—')}</dd>
            <dt>login</dt><dd>${s.login_ok === true ? 'ok' : s.login_ok === false ? 'fail' : 'unknown'}</dd>
            <dt>price</dt><dd>${s.price != null ? '$' + Number(s.price).toLocaleString() : '—'}</dd>
            <dt>url</dt><dd>${s.url ? `<a href="${escape(s.url)}" target="_blank" rel="noopener">${escape(s.url)}</a>` : '—'}</dd>
        </dl></td>`;
        tb.appendChild(dt);

        tr.addEventListener('click', () => {
            const k = tr.dataset.scanKey;
            if (SCAN_STATE.expandedScans.has(k)) SCAN_STATE.expandedScans.delete(k);
            else SCAN_STATE.expandedScans.add(k);
            tr.classList.toggle('expanded');
            dt.classList.toggle('shown');
        });
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

async function loadPurchases() {
    const r = await api('/api/admin/purchases?limit=500').then(r => r.json());
    const purchases = r.data || [];

    const successes = purchases.filter(p => isSuccess(p));
    const failures  = purchases.filter(p => isFail(p));
    const total = successes.reduce((acc, p) => acc + (Number(p.total) || 0), 0);

    $('#purchOk').textContent = successes.length;
    $('#purchFail').textContent = failures.length;
    $('#purchTotal').textContent = '$' + total.toLocaleString(undefined, {maximumFractionDigits: 2});

    const tb = $('#purchasesTable tbody');
    tb.innerHTML = '';
    if (!purchases.length) {
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">No purchase attempts logged yet.</div></td></tr>`;
        return;
    }
    for (const p of purchases) {
        const status = displayStatus(p);
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="mono" data-label="Time" title="${escape(p.ts || '')}">${escape(p.ts ? formatHumanTime(p.ts) : '—')}</td>
            <td data-label="Org">${escape(p.org_id || '—')}</td>
            <td data-label="Truck">${escape(p.truck || '—')}</td>
            <td class="num" data-label="Total">${p.total != null ? '$' + Number(p.total).toFixed(2) : '—'}</td>
            <td data-label="Status"><span class="pill ${pillClass(status)}">${escape(status.toLowerCase())}</span></td>
            <td class="mono" data-label="Detail">${escape(p.detail || p.error || p.confirmation_number || '—')}</td>
        `;
        tb.appendChild(tr);
    }
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
        tb.innerHTML = `<tr><td colspan="7"><div class="empty-state">${msg}</div></td></tr>`;
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
            <td data-label="Status"><span class="pill ${pillClass(c.status)}">${escape(c.status)}</span></td>
            <td data-label="Autobuy">${toggleHtml}</td>
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
    }
});

// Buy-history tab: paginate the per-customer purchase audit.

const HIST_STATE = { rows: [], page: 0, perPage: 10 };

async function loadCustomerHistory(id) {
    const tb = $('#cdHistoryTable tbody');
    tb.innerHTML = `<tr><td colspan="5"><div class="empty-state">Loading…</div></td></tr>`;
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
        tb.innerHTML = `<tr><td colspan="5"><div class="empty-state">Failed to load: ${escape(String(e.message || e))}</div></td></tr>`;
    }
}

function renderCustomerHistory() {
    const tb = $('#cdHistoryTable tbody');
    tb.innerHTML = '';
    const rows = HIST_STATE.rows;
    if (!rows.length) {
        tb.innerHTML = `<tr><td colspan="5"><div class="empty-state">No purchase attempts on record for this customer yet.</div></td></tr>`;
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

function openCustomerDetail(id) {
    _currentCustomerId = id;
    // Programmatically activate the customer-detail panel (no nav-item exists
    // for it; PARENT_TABS keeps "Customers" highlighted in the sidebar).
    $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.tab === 'customers'));
    $$('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === 'customer-detail'));
    $('#crumbCurrent').textContent = TAB_TITLES['customer-detail'];
    loadCustomerDetail(id);
}

async function loadCustomerDetail(id) {
    // Reset the masked sections in case we navigate from another customer.
    $('#cdCreds').innerHTML = `
        <dt>username</dt><dd class="mono"><em style="color:var(--text-mute)">hidden — click "Show full record"</em></dd>
        <dt>password</dt><dd class="mono"><em style="color:var(--text-mute)">hidden</em></dd>`;
    $('#cdCards').innerHTML = `<em style="color:var(--text-mute)">hidden — click "Show full record"</em>`;
    $('#cdAcks').innerHTML = `<dt><em style="color:var(--text-mute)">live fetch only</em></dt><dd></dd>`;
    $('#cdCredsSub').textContent = 'click "Show full record" to reveal';
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
    $('#cdCooldown').textContent = c.cooldown_until ? `cooldown until ${c.cooldown_until}` : 'no cooldown';
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
        const pc = d.partner_credentials || {};
        $('#cdCredsSub').innerHTML = `live (audit-logged · reason=<code class="mono">${escape(d._reason_logged)}</code>)`;
        $('#cdCreds').innerHTML = `
            <dt>username</dt><dd class="mono">${escape(pc.username || '—')}</dd>
            <dt>password</dt><dd class="mono">${pc.password_present
                ? `<span style="color:var(--text-mute)">${'•'.repeat(pc.password_length || 0)}</span> <span style="color:var(--text-mute);font-size:11px">(${pc.password_length} chars)</span>`
                : '<em style="color:var(--text-mute)">not set</em>'}</dd>`;

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
    {name: 'QuickBeed customer sync', span: 2, keys: [
        'QUICKBEED_BASE_URL',
        'QUICKBEED_APP_ID',
        'QUICKBEED_CONSUMER_ID',
        'QUICKBEED_API_TOKEN',
        'QUICKBEED_WEBHOOK_SECRET',
        'QUICKBEED_POLL_INTERVAL_SECONDS',
        'QUICKBEED_DRY_RUN',
    ]},
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
            div.appendChild(row);
        }
        root.appendChild(div);
    }
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

    // Cool-off — list of customers with active cooldown_until
    if (cool.length) {
        $('#rosterCool').innerHTML = cool.map(c => {
            const until = c.cooldown_until;
            const remaining = until ? Math.max(0, secondsUntil(until)) : 0;
            const label = remaining > 0 ? `${formatRelTime(remaining)} left` : 'expired';
            return customerChip(c, {accent: 'warn', extra: label, tooltip: until});
        }).join('');
    } else {
        $('#rosterCool').innerHTML = `<span class="roster-empty">Nobody cooling off</span>`;
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
                    <span class="check-row__summary">${lvlPill}${source}${channel}${deliv}</span>
                </div>
                <div class="check-row__body">
                    <pre class="check-line log-info" style="margin:0">${escape(n.message || '')}</pre>
                    ${n.error ? `<div class="check-line log-err" style="margin-top:8px">delivery error: ${escape(n.error)}</div>` : ''}
                </div>
            </div>
        `;
    }).join('');

    el.querySelectorAll('.check-row').forEach(row => {
        row.querySelector('.check-row__head').addEventListener('click', () => {
            const id = Number(row.dataset.notifId);
            if (NOTIF_STATE.expanded.has(id)) NOTIF_STATE.expanded.delete(id);
            else NOTIF_STATE.expanded.add(id);
            row.classList.toggle('expanded');
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
    $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.tab === 'testbuy'));
    $$('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === 'test-detail'));
    $('#crumbCurrent').textContent = TAB_TITLES['test-detail'];
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
    audit: loadAudit,
    import: loadImport,
};

function pillClass(status) {
    if (!status) return 'idle';
    const s = String(status).toLowerCase();
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
