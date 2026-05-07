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
    customers: 'Customers',
    'customer-detail': 'Customer',
    users: 'Users',
    settings: 'Settings',
    audit: 'Audit',
    import: 'Import CSV',
};

// Sub-pages reachable from a parent panel — keep the parent highlighted in
// the sidebar while the child is active (e.g. Import is a child of Settings).
const PARENT_TABS = { import: 'settings', 'customer-detail': 'customers' };

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

    loadScans();
    loadSidebarBadges();
    loadSystemStatus();
    setInterval(loadSystemStatus, 30_000);
})();

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
}

// ---------------- Scans ----------------

async function loadScans() {
    // Pull both the scan summary and the live log tail in parallel.
    const [r1, r2] = await Promise.all([
        api('/api/admin/scans?limit=200').then(r => r.json()),
        api('/api/admin/scans/log-tail?n=200').then(r => r.json()).catch(() => ({data: {lines: []}})),
    ]);
    const data = r1.data || {};
    const scans = (data.scans || []);
    const hb = data.heartbeat;
    const services = data.services || [];
    const tail = (r2.data || {});

    // ---- Metric strip ----
    if (hb && hb.last_scan) {
        const ago = Math.max(0, Math.floor((Date.now() - new Date(hb.last_scan).getTime()) / 1000));
        $('#metricLastScan').textContent = formatRelTime(ago);
        $('#metricLastScanMeta').textContent = new Date(hb.last_scan).toLocaleString();
        $('#hbStatus').textContent = `heartbeat: ${formatRelTime(ago)} ago`;
    } else if (scans.length) {
        const t = scans[scans.length - 1].time;
        $('#metricLastScan').textContent = t ? formatRelTime(secondsSince(t)) : '—';
        $('#metricLastScanMeta').textContent = t || 'no timestamp';
        $('#hbStatus').textContent = 'heartbeat: no file';
    } else {
        $('#metricLastScan').textContent = '—';
        $('#metricLastScanMeta').textContent = 'no scans yet';
        $('#hbStatus').textContent = 'heartbeat: no data';
    }
    $('#metricScanCount').textContent = scans.length;

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

    // ---- Live log tail ----
    const tailEl = $('#logTail');
    const lines = tail.lines || [];
    if (!lines.length) {
        tailEl.innerHTML = tail.log_present === false
            ? `<em style="color:var(--text-mute)">cron log file not yet written. Start the monitor service: <code style="font-family:var(--font-mono)">docker compose up -d monitor</code></em>`
            : `<em style="color:var(--text-mute)">cron log empty</em>`;
    } else {
        tailEl.innerHTML = lines.map(l => {
            const cls = l.severity === 'error' ? 'log-err' :
                        l.severity === 'warn'  ? 'log-warn' :
                        l.severity === 'ok'    ? 'log-ok'   : 'log-info';
            return `<div class="${cls}">${escape(l.line)}</div>`;
        }).join('');
        tailEl.scrollTop = tailEl.scrollHeight;
    }
    $('#tailMeta').textContent = tail.total
        ? `${tail.total} lines · ${tail.error_count || 0} err · ${tail.warn_count || 0} warn · ${tail.ok_count || 0} ok`
        : 'no log activity';

    // ---- Detected trucks table ----
    const tb = $('#scansTable tbody');
    tb.innerHTML = '';
    if (!scans.length) {
        tb.innerHTML = `<tr><td colspan="5"><div class="empty-state">No scans yet — start the monitor service to begin collecting telemetry.</div></td></tr>`;
        return;
    }
    for (const s of [...scans].reverse()) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="mono">${escape(s.time || '')}</td>
            <td>${escape(s.org_id || '')}</td>
            <td>${
                s.login_ok === false ? '<span class="pill err">fail</span>' :
                s.login_ok === true  ? '<span class="pill ok">ok</span>'   : '<span class="pill idle">—</span>'
            }</td>
            <td>${escape(s.title || '')}</td>
            <td><span class="pill ${pillClass(s.status)}">${escape(s.status || 'scan')}</span></td>
        `;
        tb.appendChild(tr);
    }
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
            <td class="mono">${escape(p.ts || '')}</td>
            <td>${escape(p.org_id || '')}</td>
            <td>${escape(p.truck || '')}</td>
            <td class="num">${p.total != null ? '$' + Number(p.total).toFixed(2) : ''}</td>
            <td><span class="pill ${pillClass(status)}">${escape(status.toLowerCase())}</span></td>
            <td class="mono">${escape(p.detail || p.error || p.confirmation_number || '')}</td>
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
            <td class="mono">#${u.id}</td>
            <td>${escape(u.email)}</td>
            <td><span class="pill role ${roleClass}">${escape(u.role.replace('_', ' '))}</span></td>
            <td class="mono">${escape(u.created_at || '')}</td>
            <td class="mono">${escape(u.last_login_at || '—')}</td>
            <td class="actions">${canDelete ? `<button data-uid="${u.id}">remove</button>` : ''}</td>
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

async function loadCustomers() {
    const r = await api('/api/admin/customers').then(r => r.json());
    const customers = r.data || [];
    const summary = r.summary || {};

    $('#custActive').textContent      = summary.active     || 0;
    $('#custOnboarding').textContent  = summary.onboarding || 0;
    $('#custPaused').textContent      = summary.paused     || 0;
    $('#custInactive').textContent    = (summary.inactive || 0) + (summary.suspended || 0);
    $('#custRosterSub').textContent   = `${customers.length} record${customers.length === 1 ? '' : 's'} mirrored locally`;

    const tb = $('#customersTable tbody');
    tb.innerHTML = '';
    if (!customers.length) {
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">No customers synced yet. Set QuickBeed credentials in Settings, then click "Resync now".</div></td></tr>`;
        return;
    }
    for (const c of customers) {
        const tr = document.createElement('tr');
        tr.className = 'clickable-row';
        tr.dataset.customerId = c.id;
        tr.innerHTML = `
            <td>
                <div style="font-weight:500">${escape(c.organization_name || '—')}</div>
                <div style="font-family:var(--font-mono);font-size:11px;color:var(--text-mute)">${escape(c.full_name || '')} · ${escape(c.email || '')}</div>
            </td>
            <td><span class="pill ${pillClass(c.status)}">${escape(c.status)}</span></td>
            <td class="mono">${escape(c.priority_level || '—')}</td>
            <td class="num">${c.max_budget != null ? '$' + Number(c.max_budget).toLocaleString() : '—'}</td>
            <td class="mono">${escape(c.last_used_at || '—')}</td>
            <td class="mono">${escape(c.updated_at || '')}</td>
        `;
        tr.addEventListener('click', () => openCustomerDetail(c.id));
        tb.appendChild(tr);
    }
}

// ---------------- Customer detail (sub-page of customers) ----------------

let _currentCustomerId = null;

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

    const r = await api(`/api/admin/customers/${encodeURIComponent(id)}`).then(r => r.json());
    if (!r.success) {
        $('#cdName').textContent = 'Customer not found';
        $('#cdSub').textContent = r.error || '';
        return;
    }
    const c = r.data;
    $('#cdName').textContent = c.organization_name || '(unnamed)';
    $('#cdSub').textContent = `${c.full_name || ''} · ${c.email || ''}` + (c.phone ? ' · ' + c.phone : '');

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
        const value = (v == null || v === '') ? '<em style="color:var(--text-mute)">—</em>' : escape(v);
        return `<dt>${escape(k)}</dt><dd class="${cls || ''}">${value}</dd>`;
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
        setStatus(`✓ Saved · ${n} key${n === 1 ? '' : 's'} updated · encrypted at rest`, 'ok');
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

async function loadAudit() {
    if (CURRENT_USER.role !== 'super_admin') {
        $('#auditTable tbody').innerHTML =
            `<tr><td colspan="6"><div class="empty-state">Only super-admins can view the audit log.</div></td></tr>`;
        return;
    }
    const r = await api('/api/admin/audit?limit=200').then(r => r.json());
    const tb = $('#auditTable tbody');
    tb.innerHTML = '';
    const rows = r.data || [];
    if (!rows.length) {
        tb.innerHTML = `<tr><td colspan="6"><div class="empty-state">No admin actions recorded yet.</div></td></tr>`;
        return;
    }
    for (const e of rows) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="mono">${escape(e.ts)}</td>
            <td>${escape(e.user_email || '?')}</td>
            <td><span class="pill idle">${escape(e.action)}</span></td>
            <td class="mono">${escape(e.target || '')}</td>
            <td class="mono">${escape(e.detail || '')}</td>
            <td class="mono">${escape(e.ip || '')}</td>
        `;
        tb.appendChild(tr);
    }
}

// ---------------- Helpers ----------------

const loaders = {
    scans: loadScans,
    purchases: loadPurchases,
    customers: loadCustomers,
    users: loadUsers,
    settings: loadSettings,
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
