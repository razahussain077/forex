# SwiftLabor.ai — Broker Market Intelligence (production deployment template)

A client-specific, read-only MT5 market research terminal:

- Next.js dashboard hosted on Vercel.
- FastAPI ingestion and deterministic candle-analysis worker.
- PostgreSQL persistence for streams, candles, signal snapshots and cached AI commentary.
- Read-only MetaTrader 5 Expert Advisor bridge.
- Public read-only dashboard proxy; the server-side worker token stays out of browser code. Per-user sign-in is not currently enabled.

## Deployment architecture — Vercel Services + Neon (Render not required)

The production target is one Vercel project with two services:

- **Frontend:** existing Next.js dashboard at the project root.
- **Backend:** the existing FastAPI market API in `worker/`, routed internally from the Next.js proxy and publicly only for the token-protected MT5 ingestion path.
- **Database:** Neon Serverless PostgreSQL connected to the Vercel project using a pooled connection string.
- **Market-data source:** the client's Equiti MT5 Desktop terminal on the current Windows PC. A Windows VPS can replace it later for always-on collection.

The dashboard login session, dashboard API token and MT5 bridge token are separate credentials. Tokens remain server-side except the bridge token entered directly into the local MT5 EA settings. Never put secrets in browser variables, chat or GitHub.

### Serverless execution model

Vercel Functions are request-driven, so this deployment does not depend on a perpetual Python background loop. Each authenticated MT5 ingestion request persists its broker snapshot and evaluates the newest completed candle for that symbol/timeframe. Repeated updates for the same candle do not create duplicate signal records. The continuous background scan/reference polling loops remain available only for a persistent non-serverless host. Automatic external provider polling is not enabled in Vercel mode; a provider sync, if later configured, is request-triggered.

### Connect Neon

1. Open the Neon integration from Vercel Marketplace and connect it to the `swiftlabor-forex` project.
2. Select the **Free** plan for the first integration stage. Review current quotas, region, autosuspend and usage limits in Neon before creating the database; upgrade only after actual usage is measured.
3. Select a region near the Vercel service; Singapore is the initial candidate, but compare actual latency.
4. Connect the resource to Production, Preview and Development. The integration should supply `DATABASE_URL`. Use Neon's **pooled** connection string (host includes `-pooler`) for runtime traffic.
5. Keep the generated database credential private. The FastAPI startup creates the required tables and indexes using idempotent SQL.

Neon's connection-pooling guidance: https://neon.com/docs/connect/connection-pooling

### Configure Vercel Services

The root `vercel.json` declares two services. The backend is internal by default; only the MT5 ingestion path and safe health endpoint are publicly rewritten to it. The Next.js frontend receives the backend's internal URL through a service binding as `WORKER_API_URL`.

**Plan requirement:** The connected Vercel team is currently on Hobby. Vercel's Hobby terms restrict the plan to personal/non-commercial use; SwiftLabor client/business deployment requires a suitable commercial plan. Vercel currently lists Pro at $20/month before taxes, with included usage credits and usage-based charges beyond allowances. Do not promote this Services migration to production until the account owner has reviewed and accepted the plan and any usage costs.

1. In Vercel Project Settings, change the Framework Preset to **Services**.
2. Keep the Git repository connected and deploy the updated `main` branch.
3. Add server-only environment variables to Production, Preview and Development:
   - `DATABASE_SSL=true`
   - `DASHBOARD_API_TOKEN` — random secret, at least 32 characters.
   - `BRIDGE_TOKEN` — a different random secret, at least 32 characters.
   - `TENANT_ID=equiti-client`
   - `EURUSD_MT5_SYMBOL=EURUSD.sd`
   - `GBPUSD_MT5_SYMBOL=GBPUSD.sd`
4. Do not manually set `WORKER_API_URL`; Vercel injects it through the service binding. None of these values may use a `NEXT_PUBLIC_*` prefix.
5. After Neon and secrets are connected, redeploy and verify the login, backend `/health`, database readiness and authenticated `/api/status`.

### Connect Equiti MT5

1. Keep the MT5 terminal logged into the verified Equiti Standard demo server `EquitiBrokerageSC-Demo`.
2. The verified Standard account symbols are `EURUSD.sd` and `GBPUSD.sd`. DXY remains unconfigured because the tested `USDINDEXZ6` contract returned no live bid/ask tick.
3. In MT5 select File → Open Data Folder → MQL5 → Experts. Copy `bridge/SwiftLaborBridge.mq5`, open MetaEditor and compile with F7. Confirm zero errors.
4. In MT5 → Tools → Options → Expert Advisors, enable WebRequest and allow `https://swiftlabor-forex-api.vercel.app`.
5. Attach the EA to a chart. The Vercel endpoint and approved FX symbols are preconfigured. Enter the private `BRIDGE_TOKEN` from Vercel's environment settings into the EA's `InpBridgeToken` input. Never share this token or commit it to GitHub.
6. Start with M15. Add separate EA instances for H1, H4, D1 and M5 only after the first stream is confirmed. Each timeframe uses the same two verified symbols. M5 supports pairwise EUR/USD–GBP/USD correlation; DXY correlation remains unavailable.
7. Confirm HTTP 2xx responses in MT5's Experts tab and compare bid/ask, quote timestamp and candles against the same Equiti terminal.

The EA is read-only and contains no order placement or modification functions. Use the demo account for integration testing. Market data reaches the dashboard only while the MT5 terminal and EA are running; a Windows VPS can be added later for always-on operation.

### Deployment truth

The existing Vercel dashboard remains deployed, but the full broker-to-dashboard path is not live until the Services deployment succeeds, Neon is connected, secrets are configured, the EA is compiled and attached, and fresh broker snapshots appear in the authenticated dashboard.

## Cross-asset reference data and correlation pipeline

The worker can ingest EUR/USD and GBP/USD M5 reference candles from OANDA v20 or Massive (formerly Polygon). Set `REFERENCE_PROVIDER=auto` to prefer OANDA when `OANDA_TOKEN` exists, otherwise Massive when `MASSIVE_API_KEY` exists. The worker polls on a configurable interval and persists normalized completed candles in PostgreSQL. Provider failures are recorded and do not create placeholder prices.

### Configure the reference feed

Set these server-side variables in Vercel (never in the browser or a public `NEXT_PUBLIC_*` variable):

| Variable | Purpose |
|---|---|
| `REFERENCE_PROVIDER` | `auto`, `oanda` or `massive` |
| `OANDA_ENV` | `practice` or `live` |
| `OANDA_TOKEN` | OANDA personal access token |
| `MASSIVE_API_KEY` / `POLYGON_API_KEY` | Optional Massive (formerly Polygon) forex API key; either variable name is accepted |
| `REFERENCE_CANDLE_COUNT` | 100–5000, default 2500 |
| `REFERENCE_POLL_SECONDS` | Poll interval; default 60 seconds |
| `MAX_REFERENCE_AGE_SECONDS` | Maximum reference-bar age; default 900 seconds |
| `EURUSD_MT5_SYMBOL`, `GBPUSD_MT5_SYMBOL`, `DXY_MT5_SYMBOL` | Exact EUR/USD, GBP/USD and DXY/index symbols from the broker's Market Watch |

For a credential-free first-stage setup, set all three exact MT5 symbol variables above and leave external API keys blank. With `REFERENCE_PROVIDER=auto`, the worker selects MT5-native mode when all three symbols are configured. The bridge must stream those three instruments on M5; the correlation engine reads only completed candles and verifies each symbol's quote and worker-receipt freshness. All three prices then come from the same Equiti broker feed, so this is broker-specific correlation, not independent venue confirmation.

OANDA is the preferred first integration. Its candles include a midpoint stream and separate bid/ask close components when requested. Massive (formerly Polygon) forex aggregates are built from bid/ask quote data, not a consolidated tape of executed FX trades; its Basic tier is rate-limited and should not be assumed to provide a real-time streaming entitlement. Review the entitlement attached to your account before using it for live monitoring.

### Verify the exact Equiti DXY symbol

On the Windows host running the client's MT5 terminal, install the MetaTrader5 Python package and run:

```powershell
py -m pip install MetaTrader5
py tools/inspect_mt5_symbols.py
```

The script only lists symbol metadata. Use the exact returned Market Watch symbol for `DXY_MT5_SYMBOL`; if no matching symbol exists, ask Equiti for the correct index instrument. Do not guess a symbol or construct synthetic DXY from EUR/USD.

### DXY broker connection

DXY is not synthesized. It must be an actual index instrument exposed by the client's Equiti MT5 Market Watch, with its exact symbol and quote basis verified. The existing bridge sends one timeframe per EA instance. For MT5-native correlation, run one separate bridge instance with `InpTimeframe=PERIOD_M5` and `InpSymbols=<exact-EURUSD>,<exact-GBPUSD>,<exact-DXY>` so all three market streams reach the worker together. Existing M15/H1/H4 instances can continue for chart analysis.

### Correlation API and dashboard

Authenticated dashboard route: `GET /api/correlation` (through the Vercel session proxy). It returns feed status, ages, common synchronized M5 observations, and rolling Pearson correlations for M5, M15 and H1. Each timeframe exposes 20- and 60-return windows. Missing intervals are not forward-filled; time buckets are emitted only when all constituent M5 bars exist. Values are returned as `null` when the series are stale or sample counts are insufficient.

The dashboard's **Macro & SMT** workspace shows the DXY and FX feed state, pairwise correlations, and data-quality caveats. Correlation is a statistical relationship, not causation and not SMT divergence by itself. EUR/USD is a major component of DXY, so EUR/USD–DXY inverse correlation is partly mechanical. Relative swing analysis must remain a separate calculation stage.

### Tests

Run `python -m unittest discover -s tests -v` from `worker/`. Tests cover OHLC validation, incomplete-bar filtering, bid/ask preservation, inverse return correlation, zero variance and missing time intervals.

## Institutional research suite

The public **Institutional Research** view adds a deterministic, read-only analysis layer alongside the existing broker dashboard:

- Confirmed swing highs/lows (two closed candles to confirm), HH/HL and LH/LL structure, BOS and CHoCH candidates.
- Broker-candle sweep/reclaim observations, ATR-normalized displacement, equal-high/equal-low clusters and five most recent fair-value-gap candidates with mitigation state.
- Recent dealing-range premium/discount/equilibrium, retracement OTE zones from confirmed swing legs, and weighted D1/H4/H1 directional context.
- London and New York session windows using IANA time zones, broker spread/ATR filtering, quote freshness checks and manually entered high-impact news blackout windows.
- Risk calculator that caps configured trade risk at 1% and requires a 3.5R minimum target distance. Pip value, account currency, symbol contract size and volume step must be verified against the broker's contract specification; the app does not guess them.
- A browser-local manual trade-plan journal. Entries stay in this browser; the public proxy does not expose tenant journal storage or writes.
- A chronological M15 institutional backtest that calls the same `analyse_institutional` decision function as the live research report at each historical M15 close. It applies completed D1/H4/H1 candles only after their close, a next-M15-open entry model, one position at a time and stop-first same-candle collision handling.

For a complete multi-timeframe report, stream the same instrument on **D1, H4, H1, M15 and M5**. Use separate EA instances because the bridge sends one timeframe per instance. The Research Desk displays each feed's presence, quote age and receipt age. Higher-timeframe bias uses only ready structures backed by fresh quotes and needs at least two HTF votes; the selected lower timeframe must also be fresh. Stale or missing inputs block a WATCH decision; the UI does not substitute synthetic prices.

### Research endpoints

Public dashboard proxy allowlist (GET only):

- GET /api/status, /api/universe, /api/signals and /api/correlation.
- GET /api/market/{symbol}?timeframe=M15 — broker candles and quote snapshot.
- GET /api/institutional/{symbol} — multi-timeframe structure, liquidity references, FVG/OTE, sessions, spread and available manual calendar filters.
- GET /api/backtest/{symbol}?timeframe=M15 — institutional M15 backtest; requires at least 120 completed M15 candles and 45 completed candles on each D1/H4/H1 stream. Other requested timeframes retain a single-timeframe diagnostic.

All POST routes are blocked at the public proxy. Calendar and journal entries are stored in browser-local storage only; the worker's tenant-scoped calendar/journal routes are not exposed publicly. The on-screen risk calculator runs locally in the browser.

Backtests use the current M15 broker spread and an estimated slippage allowance of 10% of that spread (minimum one point) uniformly across history because historical per-bar bid/ask spreads are not stored. The simulation applies the live institutional decision function, including D1/H4/H1 structure bias, M15 sweep/displacement/BOS, spread/ATR, London/New York session, manually entered high-impact news blackouts, FVG and OTE gates. It assumes historical HTF candles were available after their close; it cannot reconstruct historical quote freshness. Commission, swap, gaps, latency and market impact are excluded. The later 30% is a chronological holdout from the same broker history, not independent unseen-market validation. Scores are checklist completion, **not** win probabilities. Do not treat a backtest result as proof of an edge; validate on unseen data and actual broker execution costs.

## Analysis and AI boundaries

The deterministic live rules engine looks for:
- 20-bar sweep-and-reclaim price-extreme candidates.
- ATR-relative directional displacement.
- A short-term structure break.
- Three-candle imbalance candidates.
- Broker spread relative to ATR.

The final forming MT5 candle is excluded from signal calculation. Scores are explainable evidence weights, not calibrated probabilities, expected returns or proof of profitability. The live analysis engine does not generate entries, stop-losses, targets or orders; the separate browser risk calculator only estimates position size from user-supplied contract inputs.

The private worker AI commentary route is optional and on-demand, but it is not exposed through the public read-only proxy. It receives only a bounded recent broker snapshot and deterministic assessment, returns a validated structured explanation, is cached per completed candle and is rate-limited. It is instructed not to issue trades or claim access to hidden institutional orders.

Forex/CFD is OTC. This system does not have the global FX order book, dealer inventory or all participants' stop orders. Chart highs/lows are observable broker-candle reference points only.

## Reliability, security and operations

- Backend is fail-closed without PostgreSQL and long random API tokens.
- Data is persisted in PostgreSQL; signal snapshots are updated per completed candle. Repeated MT5 polls update the current candle and only new bars instead of rewriting the full candle history on every poll.
- Stale quotes are excluded using both MT5 quote timestamp and worker receipt timestamp; repeated upload of an old quote does not make it fresh.
- Bridge payload size, candle count, symbol names and timeframe are bounded/validated.
- Read-only EA contains no order send/modify/close calls.
- Worker health and readiness endpoints are separate.
- The target deployment stores market data in Neon PostgreSQL using a pooled connection. Confirm Neon branch, backup/restore and retention settings in the connected Neon project before production use; do not assume a database connection alone completes a disaster-recovery plan.
- CI runs Python unit/API tests and TypeScript/build checks on pushes and pull requests.
- Before client production use, validate authentication/authorization for every public route, protect the ingestion endpoint with the bridge token and rate limits, configure Vercel operational alerts, test database exports/restores, keep dependencies updated and document secret rotation. The current public dashboard proxy is GET-only; do not assume that protects direct worker routes unless they are separately checked.
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

