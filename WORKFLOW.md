# Three-Environment Workflow

```
feature/* branch ──▶ staging branch ──▶ main branch
feature env          staging env        production
feature.podhost.club staging.podhost.club podhost.club
```

| | PRODUCTION | STAGING | FEATURE |
|---|---|---|---|
| Branch | `main` | `staging` | any `feature/*` |
| Checkout | `/root/good-360-bids` | `/root/good-360-bids-staging` | `/root/good-360-bids-feature` |
| Compose project | `good-360-bids` | `good-360-bids-staging` | `good-360-bids-feature` |
| Compose file | `docker-compose.yml` | `docker-compose.staging.yml` | `docker-compose.feature.yml` |
| Image tag | `good360-monitor:local` | `:staging` | `:feature` |
| URL | https://podhost.club | https://staging.podhost.club | https://feature.podhost.club |
| Auto-buy | **ENABLED** | disabled (`ENABLE_AUTO_BUY=false`) | disabled |
| Good360 scanning | **ENABLED** | disabled (`ENABLE_URL_SCANNING=false`) | disabled |
| Telegram bot | runs | not deployed (getUpdates token conflict) | not deployed |
| Databases | own `workdir`/`roster_data` volumes | own (empty at first) | own (empty at first) |
| Local ports (127.0.0.1) | daemon 5002, intake 5000 | daemon 15002, intake 15000 | daemon 25002, intake 25000 |

Each environment is a separate Docker Compose project with its own containers,
volumes (= its own SQLite databases), image tag, and `.env`. Nothing is shared
except the host and the `edge` network described below.

## Day-to-day flow

1. **Develop** — branch off `main`:
   ```bash
   cd /root/good-360-bids-feature
   git checkout -b feature/my-change origin/main
   # ...edit, commit...
   scripts/deploy-feature.sh feature/my-change
   ```
   Review at https://feature.podhost.club.

2. **Stage** — merge the feature branch into `staging` and deploy:
   ```bash
   git checkout staging && git merge feature/my-change && git push origin staging
   cd /root/good-360-bids-staging && scripts/deploy-staging.sh
   ```
   Review at https://staging.podhost.club.

3. **Ship** — merge `staging` into `main` and deploy production:
   ```bash
   git checkout main && git merge staging && git push origin main
   cd /root/good-360-bids && scripts/deploy-prod.sh   # operator-run only
   ```

## Feature flags

`ENABLE_AUTO_BUY` and `ENABLE_URL_SCANNING` are read from the process
environment only (`feature_flags.py`). They are deliberately **not** in
`settings_bootstrap._KEYS_TO_LOAD`, so nothing in the dashboard settings DB
can ever re-enable them. Unset = enabled (production needs no .env change).

`ENABLE_AUTO_BUY=false` blocks, independently at each layer:
- `good360_monitor.py` — `is_autobuy_active()` returns False (alert-only mode)
  and `run_autobuy()` hard-aborts with status `BLOCKED`.
- `good360_daemon.py` — POST `/checkout`, `/test_checkout`,
  `/live/prepare_checkout`, `/live/place_order`, `/live/fetch_price` return
  403. (`/health`, `/live/navigate`, `/live/screenshot` still work.)
- `good360_autobuy.py` — exits immediately when run.
- `good360_roster/good360_autobuy_v2.py` — `attempt_purchase()` returns a
  failed CheckoutResult before doing anything, including test mode.

`ENABLE_URL_SCANNING=false` makes the monitor's scan loop idle without making
any Good360 requests. It still writes its heartbeat (status
`scanning_disabled`) so the container healthcheck stays green and the
watchdog stays quiet.

Testing a checkout flow in non-prod: set `SANDBOX_MODE=true` **and**
explicitly flip `ENABLE_AUTO_BUY=true` in that env's `.env`, then recreate the
containers. Never put real cards or real purchase credentials in a non-prod
`.env`.

## Non-prod .env rules

Each checkout has its own gitignored `.env`. Staging/feature must set:

```
ENABLE_AUTO_BUY=false
ENABLE_URL_SCANNING=false
COMPOSE_PROJECT_NAME=good-360-bids-staging          # or -feature
DASHBOARD_PROJECT_DIR=/root/good-360-bids-staging   # or -feature
MISSIONCONTROL_URL=http://missioncontrol-staging:5001            # or -feature
MISSIONCONTROL_INTERNAL_URL=http://missioncontrol-staging:5001   # or -feature
DASHBOARD_MASTER_KEY=<fresh 32-byte base64url key — do NOT reuse prod's>
MISSIONCONTROL_API_KEY=<fresh random key>
TELEGRAM_BOT_TOKEN=            # blank: no alerts, no poller conflict
```
…and blank all real card numbers / purchase credentials. Generate a master
key with:
```bash
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

`COMPOSE_PROJECT_NAME` + `DASHBOARD_PROJECT_DIR` also scope the dashboard's
Apply/Logs/Status docker operations to that environment's own stack — a
staging dashboard cannot recreate prod containers.

## Fresh environment bootstrap

The dashboard schema auto-creates on first boot. First visit to the
environment's URL redirects to `/register`; the first account created becomes
`super_admin` (password ≥ 12 chars). Databases start empty — customers,
settings, and tracked products are per-environment.

## Routing / TLS

Prod's caddy (ports 80/443) is the single edge for all three domains. It
reaches the non-prod dashboards over the external `edge` docker network:

```bash
docker network create edge                                # once
docker network connect edge good-360-bids-caddy-1        # once per caddy recreation*
```
\* `docker-compose.yml` now declares the `edge` network on the caddy service,
so after the next prod deploy the attachment is automatic and the manual
`network connect` is no longer needed.

Caddy auto-issues Let's Encrypt certs for `staging.` / `feature.` once their
DNS records exist (A `178.105.38.22`, AAAA `2a01:4f8:1c18:ef93::1`, or CNAME
to `podhost.club`). After editing `caddy/Caddyfile` in the PROD checkout,
apply with a zero-downtime reload:
```bash
docker exec good-360-bids-caddy-1 caddy reload --config /etc/caddy/Caddyfile
```

The non-prod dashboard services are named `missioncontrol-staging` /
`missioncontrol-feature` (not `missioncontrol`) because compose registers the
service name as a DNS alias on every attached network — a second
`missioncontrol` alias on `edge` could make prod caddy route podhost.club
traffic to a non-prod container.
