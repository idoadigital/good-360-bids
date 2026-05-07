// E-Comsetter Mission Control - Dashboard JavaScript

const API_BASE = '';
// API key is supplied at login and kept in sessionStorage (cleared on tab close).
// Never ship a key in source. If missing, prompt the operator.
const API_KEY = sessionStorage.getItem('mc_api_key') || (() => {
    const k = prompt('Mission Control API key:');
    if (k) sessionStorage.setItem('mc_api_key', k);
    return k || '';
})();
const REFRESH_INTERVAL = 30000; // 30 seconds

let currentLogFilter = 'all';

// ============================================
// API Functions
// ============================================

const api = {
    headers: {
        'X-API-Key': API_KEY,
        'Content-Type': 'application/json'
    },

    async get(endpoint) {
        try {
            const res = await fetch(`${API_BASE}${endpoint}`, { headers: this.headers });
            if (res.status === 401) {
                showToast('Authentication failed', 'error');
                return null;
            }
            return await res.json();
        } catch (err) {
            console.error(`API GET ${endpoint} failed:`, err);
            updateConnectionStatus(false);
            return null;
        }
    },

    async post(endpoint, data = {}) {
        try {
            const res = await fetch(`${API_BASE}${endpoint}`, {
                method: 'POST',
                headers: this.headers,
                body: JSON.stringify(data)
            });
            return await res.json();
        } catch (err) {
            console.error(`API POST ${endpoint} failed:`, err);
            return null;
        }
    },

    async put(endpoint, data = {}) {
        try {
            const res = await fetch(`${API_BASE}${endpoint}`, {
                method: 'PUT',
                headers: this.headers,
                body: JSON.stringify(data)
            });
            return await res.json();
        } catch (err) {
            console.error(`API PUT ${endpoint} failed:`, err);
            return null;
        }
    }
};

// ============================================
// Dashboard Functions
// ============================================

async function refreshAll() {
    showToast('Refreshing...', 'info');
    await Promise.all([
        loadStatus(),
        loadAlerts(),
        loadTransactions(),
        loadLogs()
    ]);
    document.getElementById('lastUpdate').textContent = formatTime(new Date());
    updateConnectionStatus(true);
}

async function loadStatus() {
    const data = await api.get('/api/status');
    if (!data || !data.success) return;
    
    const status = data.data;
    
    // System Status
    const systemEl = document.getElementById('systemStatus');
    systemEl.textContent = status.system.status === 'healthy' ? 'Healthy' : 'Degraded';
    systemEl.className = 'card-value ' + (status.system.status === 'healthy' ? 'healthy' : 'degraded');
    
    // Monitor
    const monitorEl = document.getElementById('monitorStatus');
    monitorEl.textContent = status.monitor.running ? 'Running' : 'Stopped';
    monitorEl.className = 'card-value ' + (status.monitor.running ? 'active' : 'down');
    document.getElementById('lastScan').textContent = `Last scan: ${status.monitor.last_scan || 'Never'}`;
    
    // Auto-Buy
    const autobuyEl = document.getElementById('autobuyStatus');
    if (status.autobuy.paused) {
        autobuyEl.textContent = 'Paused';
        autobuyEl.className = 'card-value paused';
        document.getElementById('cooldownStatus').textContent = 'Manually paused';
    } else if (status.autobuy.cooldown_active) {
        autobuyEl.textContent = 'Cooldown';
        autobuyEl.className = 'card-value paused';
        document.getElementById('cooldownStatus').textContent = `Until: ${status.autobuy.cooldown_until || 'next Wednesday'}`;
    } else {
        autobuyEl.textContent = 'Active';
        autobuyEl.className = 'card-value active';
        document.getElementById('cooldownStatus').textContent = `Max: $${status.autobuy.max_price}`;
    }
    
    // Trucks Today
    document.getElementById('trucksToday').textContent = status.stats.trucks_today;
    document.getElementById('totalScans').textContent = `${status.stats.total_scans} total scans`;
    
    // Update button states
    updateButtonStates(status.autobuy.paused);
}

async function loadAlerts() {
    const data = await api.get('/api/alerts?limit=20');
    if (!data || !data.success) return;
    
    const alerts = data.data.alerts;
    document.getElementById('alertCount').textContent = alerts.length;
    
    const container = document.getElementById('alertsList');
    
    if (alerts.length === 0) {
        container.innerHTML = '<div class="empty">No recent alerts</div>';
        return;
    }
    
    container.innerHTML = alerts.reverse().map(alert => {
        const icon = getAlertIcon(alert.type);
        return `
            <div class="alert-item">
                <div class="alert-icon ${alert.type}">${icon}</div>
                <div class="alert-content">
                    <div class="alert-message">${alert.message}</div>
                    <div class="alert-time">${formatTime(alert.timestamp)}</div>
                </div>
            </div>
        `;
    }).join('');
}

async function loadTransactions() {
    const data = await api.get('/api/transactions?limit=10');
    if (!data || !data.success) return;
    
    const transactions = data.data.transactions;
    const container = document.getElementById('transactionsList');
    
    if (transactions.length === 0) {
        container.innerHTML = '<div class="empty">No recent transactions</div>';
        return;
    }
    
    container.innerHTML = transactions.reverse().map(tx => {
        return `
            <div class="transaction-item">
                <div class="transaction-info">
                    <div class="transaction-title">${tx.truck_title || 'Unknown Truck'}</div>
                    <div class="transaction-time">${formatTime(tx.timestamp)}</div>
                </div>
                <span class="transaction-status ${tx.status}">${tx.status}</span>
            </div>
        `;
    }).join('');
}

async function loadLogs() {
    const data = await api.get('/api/logs?limit=50');
    if (!data || !data.success) return;
    
    let logs = data.data.logs;
    
    // Apply filter
    if (currentLogFilter !== 'all') {
        logs = logs.filter(log => log.level === currentLogFilter);
    }
    
    const container = document.getElementById('logsList');
    
    if (logs.length === 0) {
        container.innerHTML = '<div class="empty">No logs match filter</div>';
        return;
    }
    
    container.innerHTML = logs.reverse().map(log => {
        return `
            <div class="log-item">
                <span class="log-time">${formatTime(log.timestamp)}</span>
                <span class="log-level ${log.level}">${log.level}</span>
                <span class="log-message">${log.message}</span>
            </div>
        `;
    }).join('');
}

function filterLogs(level) {
    currentLogFilter = level;
    
    // Update button states
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    event.target.classList.add('active');
    
    loadLogs();
}

// ============================================
// Control Functions
// ============================================

async function pauseAutoBuy() {
    const reason = prompt('Reason for pause (optional):') || 'Manual pause via dashboard';
    const data = await api.post('/api/pause', { reason });
    if (data && data.success) {
        showToast('Auto-buy paused successfully', 'success');
        loadStatus();
    } else {
        showToast('Failed to pause auto-buy', 'error');
    }
}

async function resumeAutoBuy() {
    const data = await api.post('/api/resume');
    if (data && data.success) {
        showToast('Auto-buy resumed!', 'success');
        loadStatus();
    } else {
        showToast('Failed to resume auto-buy', 'error');
    }
}

async function triggerTest() {
    if (!confirm('Trigger a manual scan now?')) return;
    
    showToast('Triggering test scan...', 'info');
    const data = await api.post('/api/test');
    if (data && data.success) {
        showToast('Test scan triggered! Check alerts for results.', 'success');
    } else {
        showToast('Failed to trigger test', 'error');
    }
}

function updateButtonStates(paused) {
    const resumeBtn = document.getElementById('resumeBtn');
    const pauseBtn = document.getElementById('pauseBtn');
    
    if (paused) {
        resumeBtn.style.display = 'inline-flex';
        pauseBtn.style.display = 'none';
    } else {
        resumeBtn.style.display = 'none';
        pauseBtn.style.display = 'inline-flex';
    }
}

// ============================================
// Utility Functions
// ============================================

function formatTime(timestamp) {
    if (!timestamp) return '--:--';
    try {
        const date = new Date(timestamp);
        return date.toLocaleTimeString('en-US', {
            hour: '2-digit',
            minute: '2-digit',
            hour12: true
        });
    } catch {
        return timestamp;
    }
}

function getAlertIcon(type) {
    switch (type) {
        case 'success': return '<i class="fas fa-check"></i>';
        case 'missed': return '<i class="fas fa-exclamation"></i>';
        case 'failed': return '<i class="fas fa-times"></i>';
        default: return '<i class="fas fa-info"></i>';
    }
}

function updateConnectionStatus(connected) {
    const statusEl = document.getElementById('connectionStatus');
    const dot = statusEl.querySelector('.dot');
    const text = statusEl.querySelector('span:last-child');
    
    if (connected) {
        dot.className = 'dot online';
        text.textContent = 'Connected';
    } else {
        dot.className = 'dot offline';
        text.textContent = 'Disconnected';
    }
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<span>${message}</span>`;
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============================================
// Initialize
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    console.log('🚀 E-Comsetter Mission Control Loading...');
    refreshAll();
    
    // Auto-refresh
    setInterval(refreshAll, REFRESH_INTERVAL);
});
