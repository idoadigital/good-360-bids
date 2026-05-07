# E-Comsetter Good360 API Documentation
**Version:** 1.1  
**Last Updated:** 2026-03-26  
**For:** External Frontend Developer

---

## 🔐 Authentication - READ THIS FIRST

### The URL is PUBLIC - No Login Required!

**Tell your developer:**
> *"Just use the URL directly - there's no login page. For API calls that control the system, include the API key in the header."*

### How It Works

| What | Authentication | Notes |
|------|----------------|--------|
| **The URL itself** | ✅ PUBLIC | Anyone with URL can access |
| **Viewing status/data** | ✅ PUBLIC | GET requests work without API key |
| **Controlling system** | 🔑 API Key | POST actions need API key |

### Quick Example for Developer

```javascript
// READ data - No API key needed!
fetch('https://missioncontrol.quicklybid.com/api/status')
  .then(res => res.json())
  .then(data => console.log(data));

// CONTROL system - API key required
fetch('https://missioncontrol.quicklybid.com/api/pause', {
  method: 'POST',
  headers: {
    'X-API-Key': '<MISSIONCONTROL_API_KEY>',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({ reason: 'Maintenance' })
});
```

### API Key Details

| Setting | Value |
|---------|-------|
| **Header Name** | `X-API-Key` |
| **API Key Value** | `<MISSIONCONTROL_API_KEY>` |

### Which Endpoints Need API Key?

| Endpoint | Method | Needs API Key? |
|----------|--------|----------------|
| `/api/health` | GET | ❌ No (public health check) |
| `/api/status` | GET | ❌ No (read-only) |
| `/api/trucks` | GET | ❌ No (read-only) |
| `/api/logs` | GET | ❌ No (read-only) |
| `/api/alerts` | GET | ❌ No (read-only) |
| `/api/transactions` | GET | ❌ No (read-only) |
| `/api/cooldown` | GET | ❌ No (read-only) |
| `/api/config` | GET | ❌ No (read-only) |
| `/api/config` | PUT | ✅ Yes (modifies config) |
| `/api/pause` | POST | ✅ Yes (controls system) |
| `/api/resume` | POST | ✅ Yes (controls system) |
| `/api/test` | POST | ✅ Yes (triggers action) |
| `/api/force-buy` | POST | ✅ Yes (triggers purchase) |

> 💡 **Summary:** GET = read = no key. POST/PUT = control = needs key.

---

## 🎯 Overview

This API controls the Good360 truckload monitoring and auto-purchase system.

**Architecture:**
## 🎯 Overview

This API controls the Good360 truckload monitoring and auto-purchase system.

**Architecture:**
```
┌─────────────────────────────────────────────────────────────────┐
│                    CLOUD (Frontend)                              │
│  Your React/Vue/HTML dashboard                                   │
│  ↓ sends HTTP requests ↓                                         │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              https://missioncontrol.quicklybid.com                       │
│              (Cloudflare Tunnel - Permanent URL)                 │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    LOCAL (Agent Zero Server)                     │
│  - Python Flask/FastAPI server running on port 5000             │
│  - Controls monitoring, auto-buy, alerts, configuration         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🔐 Authentication

All API requests require an API key in the header:

```http
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Example:**
```bash
curl -H "X-API-Key: <MISSIONCONTROL_API_KEY>" https://missioncontrol.quicklybid.com/api/status
```

---

## 📍 Base URL

```
https://missioncontrol.quicklybid.com
```

All endpoints are relative to this base URL.

---

## 📡 API Endpoints

### 1. GET /api/status
**Purpose:** Get complete system health check

**Request:**
```http
GET /api/status
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "system": {
      "status": "healthy",
      "uptime_hours": 48.5,
      "last_restart": "2026-03-24T10:00:00-04:00"
    },
    "monitor": {
      "running": true,
      "pid": 675,
      "last_scan": "2026-03-26T00:50:00-04:00",
      "scans_today": 450,
      "trucks_found_today": 3,
      "interval_minutes": 1
    },
    "autobuy": {
      "enabled": true,
      "paused": false,
      "cooldown_active": false,
      "cooldown_until": null,
      "single_purchase_lock": false,
      "max_price": 6400
    },
    "telegram_bot": {
      "running": true,
      "pid": 347,
      "connected": true
    },
    "watchdog": {
      "running": true,
      "last_check": "2026-03-26T00:45:00-04:00",
      "alerts_sent_today": 0
    },
    "cron": {
      "running": true,
      "schedule": "*/1 6-23 * * 1-5"
    }
  }
}
```

---

### 2. GET /api/trucks
**Purpose:** Get current truck availability and history

**Query Parameters:**
- `limit` (optional): Number of trucks to return (default: 20)
- `status` (optional): Filter by status (detected/purchased/missed)
- `category` (optional): Filter by category (unsorted/variety/houseware/softlines)

**Request:**
```http
GET /api/trucks?limit=10&status=detected
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "current_availability": [
      {
        "id": "truck_001",
        "title": "Amazon New Unsorted Truckload - Maysville, KY",
        "category": "amazon_new_unsorted",
        "price": 3200.00,
        "location": "Maysville, KY",
        "url": "https://shop.good360.org/products/amazon-new-unsorted-maysville",
        "status": "available",
        "detected_at": "2026-03-26T00:48:00-04:00",
        "auto_buy_target": true,
        "estimated_total": 3850.00
      }
    ],
    "recent_history": [
      {
        "id": "truck_002",
        "title": "Amazon Assorted Houseware Truckload - Maysville, KY",
        "category": "amazon_houseware",
        "price": 2800.00,
        "status": "purchased",
        "detected_at": "2026-03-25T14:30:00-04:00",
        "purchased_at": "2026-03-25T14:30:45-04:00",
        "purchase_result": "success",
        "confirmation_number": "ORD-12345"
      },
      {
        "id": "truck_003",
        "title": "Amazon Variety Truckload - Maysville, KY",
        "category": "amazon_variety",
        "price": 3500.00,
        "status": "missed",
        "detected_at": "2026-03-25T10:15:00-04:00",
        "missed_reason": "sold_out_before_checkout"
      }
    ],
    "stats": {
      "total_detected_today": 5,
      "total_purchased_today": 1,
      "total_missed_today": 1,
      "total_alerted_today": 3
    }
  }
}
```

---

### 3. GET /api/logs
**Purpose:** Get recent activity logs

**Query Parameters:**
- `limit` (optional): Number of log entries (default: 50)
- `level` (optional): Filter by level (info/warning/error/critical)

**Request:**
```http
GET /api/logs?limit=20
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "logs": [
      {
        "timestamp": "2026-03-26T00:50:00-04:00",
        "level": "info",
        "source": "monitor",
        "message": "Scan complete: No trucks available"
      },
      {
        "timestamp": "2026-03-26T00:49:00-04:00",
        "level": "info",
        "source": "monitor",
        "message": "Scan complete: No trucks available"
      },
      {
        "timestamp": "2026-03-26T00:48:00-04:00",
        "level": "warning",
        "source": "autobuy",
        "message": "Cooldown active until 2026-04-01 (Wednesday)"
      }
    ],
    "total_logs": 1250,
    "showing": 20
  }
}
```

---

### 4. POST /api/pause
**Purpose:** Pause auto-buy (alerts continue)

**Request:**
```http
POST /api/pause
X-API-Key: <MISSIONCONTROL_API_KEY>
Content-Type: application/json

{
  "reason": "Manual pause for maintenance"
}
```

**Response:**
```json
{
  "success": true,
  "data": {
    "paused": true,
    "paused_at": "2026-03-26T00:52:00-04:00",
    "paused_by": "api",
    "reason": "Manual pause for maintenance",
    "message": "Auto-buy paused. Alerts will continue."
  }
}
```

---

### 5. POST /api/resume
**Purpose:** Resume auto-buy

**Request:**
```http
POST /api/resume
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "paused": false,
    "resumed_at": "2026-03-26T00:55:00-04:00",
    "message": "Auto-buy resumed. Ready to purchase trucks."
  }
}
```

---

### 6. POST /api/test
**Purpose:** Trigger a manual test scan

**Request:**
```http
POST /api/test
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "test_id": "test_20260326_005500",
    "status": "running",
    "message": "Manual scan triggered. Results will be sent via Telegram/Email.",
    "check_results_url": "/api/test/test_20260326_005500"
  }
}
```

---

### 7. GET /api/cooldown
**Purpose:** Get current cooldown status

**Request:**
```http
GET /api/cooldown
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "cooldown_active": true,
    "org_name": "Hope 4 Humanity",
    "last_purchase": "2026-03-25T18:07:00-04:00",
    "cooldown_until": "2026-04-01T00:00:00-04:00",
    "cooldown_type": "calendar_week",
    "next_allowed_day": "Wednesday",
    "days_remaining": 6,
    "can_purchase": false
  }
}
```

---

### 8. GET /api/config
**Purpose:** Get current configuration

**Request:**
```http
GET /api/config
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "org": {
      "name": "Hope 4 Humanity",
      "email": "berneitha@hope4humanity.us",
      "warehouse": "1025 Progress Circle, Lawrenceville, GA 30043"
    },
    "autobuy": {
      "max_price": 6400,
      "targets": [
        {
          "category": "amazon_new_unsorted",
          "label": "Amazon New Unsorted Truckload",
          "enabled": true
        },
        {
          "category": "amazon_variety",
          "label": "Amazon Variety Truckload",
          "enabled": true
        },
        {
          "category": "amazon_houseware",
          "label": "Amazon Assorted Houseware Truckload",
          "enabled": true
        }
      ],
      "excluded": [
        {
          "category": "softlines",
          "label": "Amazon Assorted Softlines",
          "enabled": false
        }
      ]
    },
    "schedule": {
      "monitor_interval_minutes": 1,
      "business_hours_start": "06:00",
      "business_hours_end": "23:00",
      "timezone": "America/New_York",
      "days": "Monday-Friday"
    },
    "alerts": {
      "telegram_enabled": true,
      "email_enabled": true,
      "email_recipients": ["berneitha@hope4humanity.us", "sdibao@gmail.com"]
    }
  }
}
```

---

### 9. PUT /api/config
**Purpose:** Update configuration

**Request:**
```http
PUT /api/config
X-API-Key: <MISSIONCONTROL_API_KEY>
Content-Type: application/json

{
  "autobuy": {
    "max_price": 7000
  },
  "targets": [
    {
      "category": "amazon_new_unsorted",
      "enabled": true
    },
    {
      "category": "amazon_variety",
      "enabled": false
    }
  ]
}
```

**Response:**
```json
{
  "success": true,
  "data": {
    "message": "Configuration updated successfully",
    "changes": [
      "max_price: 6400 → 7000",
      "amazon_variety: enabled → disabled"
    ]
  }
}
```

---

### 10. GET /api/alerts
**Purpose:** Get recent alerts and notifications

**Query Parameters:**
- `limit` (optional): Number of alerts (default: 20)
- `type` (optional): Filter by type (success/failed/missed/system)

**Request:**
```http
GET /api/alerts?limit=10&type=success
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "alerts": [
      {
        "id": "alert_001",
        "type": "success",
        "timestamp": "2026-03-25T18:07:00-04:00",
        "title": "✅ AUTO-BUY COMPLETE",
        "message": "Amazon Assorted Houseware Truckload purchased!",
        "details": {
          "truck": "Amazon Assorted Houseware Truckload - Maysville, KY",
          "total": 3850.00,
          "confirmation": "ORD-12345"
        },
        "channels": ["telegram", "email"]
      }
    ],
    "stats": {
      "total_alerts_today": 12,
      "success": 1,
      "missed": 2,
      "failed": 0,
      "system": 9
    }
  }
}
```

---

### 11. GET /api/transactions
**Purpose:** Get purchase transaction history

**Query Parameters:**
- `limit` (optional): Number of transactions (default: 20)
- `status` (optional): Filter by status (success/failed/missed)
- `from_date` (optional): Start date (ISO format)
- `to_date` (optional): End date (ISO format)

**Request:**
```http
GET /api/transactions?limit=10
X-API-Key: <MISSIONCONTROL_API_KEY>
```

**Response:**
```json
{
  "success": true,
  "data": {
    "transactions": [
      {
        "id": "txn_001",
        "truck_title": "Amazon Assorted Houseware Truckload - Maysville, KY",
        "truck_price": 2800.00,
        "admin_fee": 1050.00,
        "shipping_fee": 0.00,
        "total": 3850.00,
        "status": "success",
        "started_at": "2026-03-25T18:07:10-04:00",
        "completed_at": "2026-03-25T18:07:45-04:00",
        "duration_seconds": 35,
        "confirmation_number": "ORD-12345",
        "payment_method": "Visa ending 7421",
        "cooldown_until": "2026-04-01"
      }
    ],
    "summary": {
      "total_transactions": 8,
      "successful": 6,
      "missed": 1,
      "failed": 1,
      "total_spent": 23100.00,
      "avg_transaction_time": 42
    }
  }
}
```

---

### 12. POST /api/force-buy
**Purpose:** Manually trigger purchase for a specific truck (requires truck URL)

**Request:**
```http
POST /api/force-buy
X-API-Key: <MISSIONCONTROL_API_KEY>
Content-Type: application/json

{
  "truck_url": "https://shop.good360.org/products/amazon-new-unsorted-maysville",
  "confirm": true
}
```

**Response:**
```json
{
  "success": true,
  "data": {
    "purchase_id": "force_20260326_010000",
    "status": "initiated",
    "message": "Manual purchase initiated. Monitor Telegram for updates.",
    "check_status_url": "/api/transactions/force_20260326_010000"
  }
}
```

---

### 13. GET /api/health
**Purpose:** Simple health check endpoint (no auth required)

**Request:**
```http
GET /api/health
```

**Response:**
```json
{
  "status": "ok",
  "timestamp": "2026-03-26T00:52:00-04:00",
  "service": "good360-api"
}
```

---

## 🔴 Error Responses

All errors follow this format:

```json
{
  "success": false,
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Invalid or missing API key",
    "details": null
  }
}
```

**Error Codes:**
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `UNAUTHORIZED` | 401 | Invalid or missing API key |
| `NOT_FOUND` | 404 | Endpoint not found |
| `VALIDATION_ERROR` | 400 | Invalid request parameters |
| `SYSTEM_ERROR` | 500 | Internal server error |
| `COOLDOWN_ACTIVE` | 423 | Cannot purchase - cooldown active |
| `PURCHASE_LOCKED` | 423 | Another purchase in progress |

---

## 📱 Example: Frontend Integration

### React Example
```javascript
const API_BASE = 'https://missioncontrol.quicklybid.com';
const API_KEY = '<MISSIONCONTROL_API_KEY>';

const api = {
  headers: {
    'X-API-Key': API_KEY,
    'Content-Type': 'application/json'
  },

  async getStatus() {
    const res = await fetch(`${API_BASE}/api/status`, { headers: this.headers });
    return res.json();
  },

  async getTrucks(limit = 20) {
    const res = await fetch(`${API_BASE}/api/trucks?limit=${limit}`, { headers: this.headers });
    return res.json();
  },

  async pauseAutoBuy(reason) {
    const res = await fetch(`${API_BASE}/api/pause`, {
      method: 'POST',
      headers: this.headers,
      body: JSON.stringify({ reason })
    });
    return res.json();
  },

  async resumeAutoBuy() {
    const res = await fetch(`${API_BASE}/api/resume`, {
      method: 'POST',
      headers: this.headers
    });
    return res.json();
  },

  async getLogs(limit = 50) {
    const res = await fetch(`${API_BASE}/api/logs?limit=${limit}`, { headers: this.headers });
    return res.json();
  },

  async getTransactions(limit = 20) {
    const res = await fetch(`${API_BASE}/api/transactions?limit=${limit}`, { headers: this.headers });
    return res.json();
  }
};

// Usage in component
function Dashboard() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    api.getStatus().then(data => setStatus(data.data));
    const interval = setInterval(() => {
      api.getStatus().then(data => setStatus(data.data));
    }, 30000); // Refresh every 30 seconds
    return () => clearInterval(interval);
  }, []);

  return (
    <div>
      <h1>Good360 Mission Control</h1>
      <p>System: {status?.system?.status}</p>
      <p>Monitor: {status?.monitor?.running ? '✅ Running' : '❌ Stopped'}</p>
      <p>Auto-buy: {status?.autobuy?.paused ? '⏸️ Paused' : '✅ Active'}</p>
    </div>
  );
}
```

---

## 📊 Dashboard UI Requirements

The frontend should display:

### Main Dashboard
1. **System Status Card**
   - Overall health (green/yellow/red)
   - Monitor running status
   - Last scan time
   - Uptime

2. **Auto-Buy Status Card**
   - Enabled/Paused toggle
   - Cooldown status with countdown
   - Single-purchase lock status
   - Max price limit

3. **Truck Availability Card**
   - Current available trucks
   - Target trucks (unsorted/variety/houseware)
   - Excluded trucks (softlines)

4. **Quick Actions**
   - Pause/Resume button
   - Test Scan button
   - Refresh button

### History Page
1. **Transaction History Table**
   - Date/Time
   - Truck name
   - Total cost
   - Status (Success/Failed/Missed)
   - Confirmation number

2. **Alert History**
   - Timestamp
   - Type
   - Message
   - Channels (Telegram/Email)

### Settings Page
1. **Configuration**
   - Max price limit
   - Target categories toggles
   - Alert preferences

---

## 🔄 Real-Time Updates (Optional)

For live updates, the frontend can poll `/api/status` every 30 seconds or use WebSocket (if implemented).

---

## 🚀 Getting Started for Frontend Developer

1. **Test the API:**
   ```bash
   curl https://missioncontrol.quicklybid.com/api/health
   ```

2. **Test authenticated endpoint:**
   ```bash
   curl -H "X-API-Key: <MISSIONCONTROL_API_KEY>" https://missioncontrol.quicklybid.com/api/status
   ```

3. **Build your frontend** using the endpoints above

4. **Deploy** to Vercel, Netlify, or any hosting

5. **Connect** - All requests go to `https://missioncontrol.quicklybid.com`

---

## 📋 Checklist for Frontend Developer

- [ ] Test `/api/health` (no auth required)
- [ ] Test `/api/status` with API key
- [ ] Build status dashboard with real-time refresh
- [ ] Implement pause/resume controls
- [ ] Build transaction history view
- [ ] Add alert notification display
- [ ] Mobile responsive design
- [ ] Deploy and test with live URL

---

*This is a living document. Contact Agent Zero for questions or updates.*
