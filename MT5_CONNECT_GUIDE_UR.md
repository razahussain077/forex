# SwiftLabor.ai — Equiti MT5 Production Connection Guide (Roman Urdu)

## Pehle deployment status samajh lein

Dashboard ka frontend pehle Vercel par deploy ho chuka tha. Is repository update mein production architecture tayyar ki gayi hai, magar Render worker, PostgreSQL, Vercel ke naye secrets aur client ke MT5 terminal ka end-to-end connection abhi verify nahi hua. Jab tak neeche ke tamam checks pass na hon, client ko live/24-7 service ka claim na dein.

## 1. Render backend aur database

1. Render.com par New → Blueprint select karein.
2. GitHub repo `razahussain077/razatrades`, branch `main` choose karein.
3. `render.yaml` mein paid always-on worker aur managed PostgreSQL database define hain. Create karne se pehle Render ki current pricing aur recurring charges check karein.
4. Render `BRIDGE_TOKEN` khud generate karega. `DASHBOARD_API_TOKEN` ke liye apna alag random secret (kam az kam 32 characters) enter karein.
5. Dono tokens ko alag aur private rakhein. Dashboard token Render aur Vercel dono mein same hoga; bridge token sirf Render aur MT5 EA mein use hoga.
6. Deploy complete hone ke baad `https://YOUR-WORKER.onrender.com/health` par `status: ok` aur `/ready` par `status: ready` check karein.

## 2. Vercel dashboard secure karein

Vercel project `swiftlabor-forex-intelligence-app` → Settings → Environment Variables mein Production ke liye ye server-only variables add karein:

| Variable | Kya value deni hai |
|---|---|
| `WORKER_API_URL` | Render worker ka HTTPS URL, trailing slash ke baghair |
| `DASHBOARD_API_TOKEN` | Render mein dala gaya bilkul wahi dashboard token |
| `DASHBOARD_PASSWORD` | Client ke workspace ka unique password, kam az kam 12 characters; 16+ behtar |
| `SESSION_SECRET` | Naya random secret, kam az kam 32 characters |
| `APP_ORIGIN` | `https://swiftlabor-forex-intelligence-app.vercel.app` |

Kisi bhi secret ka naam `NEXT_PUBLIC_` se shuru na karein. Is se secret browser code mein expose ho sakta hai. Variables save kar ke Vercel ko redeploy karein.

Dashboard login 8-hour signed HttpOnly session cookie use karta hai. Vercel Firewall par login endpoint ke liye per-IP rule configured hai: 60 seconds mein 10 se zyada POST requests deny hoti hain.

## 3. Client ke Equiti MT5 mein bridge install karein

1. Client apne Equiti MT5 terminal mein khud login kare. Account password, investor password, OTP ya remote access details hum kabhi na maangein.
2. MT5 → File → Open Data Folder → MQL5 → Experts open karein.
3. Repo ki `bridge/SwiftLaborBridge.mq5` file Experts folder mein copy karein.
4. MetaEditor (F4) mein file open kar ke F7 compile karein. Zero compile errors confirm karein. Client ke terminal par MetaEditor compilation abhi hamari taraf se verify nahi hui.
5. MT5 → Tools → Options → Expert Advisors mein Allow WebRequest enable karein aur Render worker ka exact HTTPS URL add karein.
6. EA ko chart par attach karein. Inputs mein `InpWorkerUrl`, `InpBridgeToken` aur approved short `InpSymbols` list set karein. Symbol names Market Watch se exact copy karein, suffix ke saath.
7. Pehle aik symbol aur M15 se start karein. Experts tab mein HTTP 2xx response verify karein. Phir symbols/timeframes dheere dheere add karein.

EA read-only hai: trade open, modify ya close nahi karta. EA stale quote ko skip karta hai aur symbols per instance cap karta hai. Bridge token ko chat ya GitHub mein paste na karein.

## 4. Dashboard ki verification

Login ke baad ye confirm karein:

- Worker `/health` aur database readiness `/ready` pass hon.
- Dashboard mein live broker streams ka count zero se zyada ho.
- Market Watch ke exact Equiti symbol names aur suffixes show hon.
- Bid, ask, spread, quote timestamp aur candles client ke MT5 se match karein.
- Freshness expiry par purani quotes aur stale signals dashboard API se remove hon.
- Worker restart ke baad database mein purani candle history rahe aur MT5 dobara snapshots bhejne par feed recover ho.

## Important limits

- MT5 terminal aur client ka Windows PC/VPS on rehna lazmi hai. Backend paid ho tab bhi band MT5 se data nahi ayega.
- PostgreSQL paid plan hai, database sirf internal Render network se accessible hai aur storage autoscaling enabled hai. Render paid Postgres ke liye PITR backups deta hai; recovery window Hobby workspace par 3 din aur Pro+ par 7 din hoti hai. Long-term backup ke liye alag logical exports aur restore test rakhain. HA standby ke liye Pro workspace aur qualifying Pro database plan chahiye, aur doosra database instance bill hota hai; default Blueprint mein HA off hai.
- PostgreSQL data persist karta hai; worker RAM cache par depend nahi karta. MT5 ke har poll par poori 200-candle history dobara write nahi hoti; pehli dafa history save hoti hai, us ke baad current/new candles update hoti hain.
- AI commentary optional hai. Render par AI Gateway API key aur exact model ID configure kiye baghair disabled rahegi. Rules engine phir bhi kaam karega.
- Score evidence weight hai, profit probability nahi. Yeh setup BUY/SELL instruction, entry, stop, target ya position size generate nahi karta.
- Forex/CFD OTC hai. System global FX order book, dealer inventory ya hidden stop orders nahi dekh sakta. Chart ke highs/lows broker candles ke reference points hain.
- CI tests aur successful deployment trading strategy ki profitability prove nahi karte. Live trade se pehle independent backtest aur forward test zaroori hain.

