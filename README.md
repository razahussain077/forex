# SwiftLabor.ai — Broker Market Intelligence (production deployment template)

A client-specific, read-only MT5 market research terminal:

- Next.js dashboard hosted on Vercel.
- FastAPI ingestion and deterministic candle-analysis worker.
- PostgreSQL persistence for streams, candles, signal snapshots and cached AI commentary.
- Read-only MetaTrader 5 Expert Advisor bridge.
- Password-protected dashboard session; server-side worker proxy keeps API tokens out of browser code.

## Deployment state — read this first

This repository contains the dashboard, MT5 bridge, FastAPI worker, PostgreSQL blueprint and reference-correlation code. **The Vercel project has not yet been verified as connected to this repository; the worker/database deployment, environment configuration, Equiti MT5 installation and end-to-end live feed have also not been verified.** Do not describe the service as live or 24/7 until each connection check below passes.

## Architecture

Client's Equiti MT5 Desktop / Windows VPS
→ read-only SwiftLaborBridge EA
→ HTTPS FastAPI worker (private token)
→ PostgreSQL
→ server-side dashboard proxy (separate dashboard token)
→ password-protected Vercel dashboard.

Secrets remain server-side. The browser receives a signed, HttpOnly, Secure, SameSite=Strict session cookie and never receives the worker token. The bridge token and dashboard token are independent.

## Production deployment

### 1. Create the Render services

1. Open Render and create a Blueprint from GitHub repository `razahussain077/forex`, branch `main`.
2. Review the paid resource choices in `render.yaml` before confirming. The Blueprint creates an always-on worker and managed PostgreSQL database; these resources incur recurring charges. Adjust plans only after reviewing current Render pricing and your expected load.
3. Render generates `BRIDGE_TOKEN`. Provide a new random token of at least 32 characters for `DASHBOARD_API_TOKEN`. Keep both secrets private and different.
4. Wait for the worker deployment and database provisioning to finish. The worker uses the Render private database connection string; it creates the required tables/indexes at startup.
5. Confirm `https://YOUR-WORKER.onrender.com/health` returns `status: ok` and `/ready` returns `status: ready`.

Optional: add `AI_GATEWAY_API_KEY` and `AI_GATEWAY_MODEL` in Render only after choosing a model/provider, checking provider pricing and confirming your Vercel AI Gateway access. Leave both unset to keep AI commentary disabled. The rules engine runs independently.

### 2. Configure Vercel

In the Vercel project connected to `razahussain077/forex` → Settings → Environment Variables, add the following **server-only** Production variables:

| Variable | Value |
|---|---|
| `WORKER_API_URL` | Render worker HTTPS base URL, without trailing slash |
| `DASHBOARD_API_TOKEN` | Exact same secret as Render's `DASHBOARD_API_TOKEN` |
| `DASHBOARD_PASSWORD` | Unique, long workspace password (minimum 12 characters; use 16+ recommended) |
| `SESSION_SECRET` | Cryptographically random secret, at least 32 characters |
| `APP_ORIGIN` | `https://YOUR-VERCEL-DOMAIN` |

Do not use a `NEXT_PUBLIC_` prefix for any of these values. The worker URL and API token must not be exposed to browser JavaScript. Redeploy Vercel after saving the variables.

The dashboard login uses an eight-hour signed HttpOnly session. A Vercel Firewall rule is configured to deny more than 10 POST requests per IP in 60 seconds to `/api/auth/login`. The login password is not an API token and must not be reused elsewhere.

### 3. Connect the client's Equiti MT5 terminal

1. The client logs in to their own Equiti MT5 Desktop terminal or dedicated Windows VPS. Never request the account password, investor password, OTP or remote access credentials.
2. In MT5 select File → Open Data Folder → MQL5 → Experts.
3. Copy `bridge/SwiftLaborBridge.mq5` into the Experts folder and open it in MetaEditor.
4. Compile with F7. Compilation must show zero errors. This repository's EA has not yet been compiled by MetaEditor on the client's terminal.
5. In MT5 → Tools → Options → Expert Advisors, allow WebRequest and add the exact worker HTTPS base URL.
6. Attach the EA to a chart. Set `InpWorkerUrl`, `InpBridgeToken` and an approved short `InpSymbols` list using the exact Market Watch symbol names, including suffixes. The EA caps symbols per instance and skips quotes older than its configured maximum age.
7. Start with one symbol and M15. Check the Experts tab for HTTP 2xx responses. Add symbols/timeframes gradually; the bridge sends the current bar as well as history, while the analysis engine evaluates completed candles only.

Do not paste the bridge token into chat or commit it to GitHub. It belongs only in Render's private environment and the client's MT5 Expert inputs.

### 4. Verify the full path

After signing into the dashboard, verify:

- Worker health: `GET /health` returns ok; `GET /ready` confirms database access.
- Dashboard status: the UI changes from awaiting MT5 to broker connected only after authenticated fresh snapshots arrive.
- Market universe: symbols and suffixes match the client's actual MT5 Market Watch.
- Market chart: bid, ask, spread, quote time and candles are consistent with the same Equiti MT5 terminal at the same time.
- Signal feed: assessments are tied to completed candles and disappear from the API once the quote becomes stale.
- Restart test: restart the worker, wait for MT5 republishing, then confirm stored candle history remains available and fresh-stream status recovers.

Browser dashboard endpoints are protected by a login session. Worker APIs require a dashboard token; MT5 ingestion requires a separate bridge token. PostgreSQL applies tenant-scoped request counters and feed/signal queries are tenant-filtered. For more than one client, create unique token-to-tenant mappings with `DASHBOARD_TOKENS_JSON` and `BRIDGE_TOKENS_JSON`; do not share client tokens.

## Cross-asset reference data and correlation pipeline

The worker can ingest EUR/USD and GBP/USD M5 reference candles from OANDA v20 or Massive (formerly Polygon). Set `REFERENCE_PROVIDER=auto` to prefer OANDA when `OANDA_TOKEN` exists, otherwise Massive when `MASSIVE_API_KEY` exists. The worker polls on a configurable interval and persists normalized completed candles in PostgreSQL. Provider failures are recorded and do not create placeholder prices.

### Configure the reference feed

Set these server-side variables in Render (never in the browser or a public `NEXT_PUBLIC_*` variable):

| Variable | Purpose |
|---|---|
| `REFERENCE_PROVIDER` | `auto`, `oanda` or `massive` |
| `OANDA_ENV` | `practice` or `live` |
| `OANDA_TOKEN` | OANDA personal access token |
| `MASSIVE_API_KEY` / `POLYGON_API_KEY` | Optional Massive (formerly Polygon) forex API key; either variable name is accepted |
| `REFERENCE_CANDLE_COUNT` | 100–5000, default 2500 |
| `REFERENCE_POLL_SECONDS` | Poll interval; default 60 seconds |
| `MAX_REFERENCE_AGE_SECONDS` | Maximum reference-bar age; default 900 seconds |
| `DXY_MT5_SYMBOL` | Exact DXY/index symbol from the broker's Market Watch |

OANDA is the preferred first integration. Its candles include a midpoint stream and separate bid/ask close components when requested. Massive (formerly Polygon) forex aggregates are built from bid/ask quote data, not a consolidated tape of executed FX trades; its Basic tier is rate-limited and should not be assumed to provide a real-time streaming entitlement. Review the entitlement attached to your account before using it for live monitoring.

### Verify the exact Equiti DXY symbol

On the Windows host running the client's MT5 terminal, install the MetaTrader5 Python package and run:

```powershell
py -m pip install MetaTrader5
py tools/inspect_mt5_symbols.py
```

The script only lists symbol metadata. Use the exact returned Market Watch symbol for `DXY_MT5_SYMBOL`; if no matching symbol exists, ask Equiti for the correct index instrument. Do not guess a symbol or construct synthetic DXY from EUR/USD.

### DXY broker connection

DXY is not synthesized. It must be an actual index instrument exposed by the client's Equiti MT5 Market Watch, with its exact symbol and quote basis verified. The existing bridge sends one timeframe per EA instance. Run a separate bridge instance with `InpTimeframe=PERIOD_M5` and `InpSymbols=<exact-DXY-symbol>` so the worker receives synchronized M5 DXY bars. Existing M15/H1/H4 instances can continue for chart analysis.

### Correlation API and dashboard

Authenticated dashboard route: `GET /api/correlation` (through the Vercel session proxy). It returns feed status, ages, common synchronized M5 observations, and rolling Pearson correlations for M5, M15 and H1. Each timeframe exposes 20- and 60-return windows. Missing intervals are not forward-filled; time buckets are emitted only when all constituent M5 bars exist. Values are returned as `null` when the series are stale or sample counts are insufficient.

The dashboard's **Macro & SMT** workspace shows the DXY and FX feed state, pairwise correlations, and data-quality caveats. Correlation is a statistical relationship, not causation and not SMT divergence by itself. EUR/USD is a major component of DXY, so EUR/USD–DXY inverse correlation is partly mechanical. Relative swing analysis must remain a separate calculation stage.

### Tests

Run `python -m unittest discover -s tests -v` from `worker/`. Tests cover OHLC validation, incomplete-bar filtering, bid/ask preservation, inverse return correlation, zero variance and missing time intervals.

## Analysis and AI boundaries

The deterministic rules engine looks for:
- 20-bar sweep-and-reclaim price-extreme candidates.
- ATR-relative directional displacement.
- A short-term structure break.
- Three-candle imbalance candidates.
- Broker spread relative to ATR.

The final forming MT5 candle is excluded from signal calculation. Scores are explainable evidence weights, not calibrated probabilities, expected returns or proof of profitability. No entries, stop-losses, targets, position sizes or orders are generated.

The AI commentary route is optional and on-demand. It receives only a bounded recent broker snapshot and the deterministic assessment, returns a validated structured explanation, is cached per completed candle and is rate-limited. It is instructed not to issue trades or claim access to hidden institutional orders. A model key and exact model ID must be configured before this feature is available.

Forex/CFD is OTC. This system does not have the global FX order book, dealer inventory or all participants' stop orders. Chart highs/lows are observable broker-candle reference points only.

## Reliability, security and operations

- Backend is fail-closed without PostgreSQL and long random API tokens.
- Data is persisted in PostgreSQL; signal snapshots are updated per completed candle. Repeated MT5 polls update the current candle and only new bars instead of rewriting the full candle history on every poll.
- Stale quotes are excluded using both MT5 quote timestamp and worker receipt timestamp; repeated upload of an old quote does not make it fresh.
- Bridge payload size, candle count, symbol names and timeframe are bounded/validated.
- Read-only EA contains no order send/modify/close calls.
- Worker health and readiness endpoints are separate.
- PostgreSQL is a paid Render instance with an internal-only IP allowlist and storage autoscaling; Render continuously backs up paid Postgres with PITR. Recovery window varies by workspace (Hobby 3 days, Pro+ 7 days). Trigger and test separate logical exports for long-term disaster recovery. High availability standby requires a Pro workspace and a qualifying Pro database plan, and bills a second database instance; it is not enabled by this default Blueprint.
- CI runs Python unit/API tests and TypeScript/build checks on pushes and pull requests.
- A Vercel Firewall login limit is active (10 POST requests per IP per 60 seconds). Configure Render/Vercel operational alerts, run periodic database recovery/export drills, keep dependencies updated and document a secret-rotation process before real client production use.
- MT5 must remain logged in and running on an always-on host. A paid backend does not keep the client's terminal alive.
- Backtest and forward-test the strategy independently across instruments, timeframes and market regimes before relying on it. A green deployment or successful API test is not evidence of trading profitability.

## Local development and tests

Worker:
```bash
cd worker
python -m pip install -r requirements.txt
TESTING=true BRIDGE_TOKEN=test-bridge-token-with-at-least-32 DASHBOARD_API_TOKEN=test-dashboard-token-with-32chars python -m unittest discover -s tests -v
```

Frontend:
```bash
npm install
npm run typecheck
npm run build
```

Use `.env.example` as a variable-name reference. Never commit real credentials.

