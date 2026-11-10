"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Activity, AlertTriangle, BrainCircuit, CandlestickChart, Clock3, Gauge,
  LayoutDashboard, Radio, ScanSearch, Search, Settings2, ShieldCheck,
  Target, Waves, RefreshCw, ChevronRight, LogOut, GitCompareArrows
} from "lucide-react";
import {
  CandlestickSeries, ColorType, CrosshairMode, createChart, type Time
} from "lightweight-charts";

type Bar = { time: number; open: number; high: number; low: number; close: number; tick_volume?: number };
type MarketItem = {
  symbol: string; asset_class: string; description: string; timeframes: string[];
  last_price: number; bid?: number; ask?: number; spread_points?: number; digits?: number;
  quote_age_seconds: number; quote_fresh: boolean; broker_server?: string | null;
};
type Signal = {
  symbol: string; timeframe: string; side: string; score: number; reason: string;
  evidence: string[]; invalidation?: number | null; evaluated_at?: string;
  quote_age_seconds?: number; spread_to_atr?: number | null;
};
type Status = {
  process?: string; data_mode?: string; broker_market_streams?: number; broker_symbols?: number;
  fresh_quotes?: number; symbols?: string[]; last_feed_at?: string | null;
  last_scan_at?: string | null; scan_interval_seconds?: number; execution_mode?: string;
  orders_enabled?: boolean; engine?: string; ai_commentary?: boolean;
};
type Commentary = {
  summary: string; observations: string[]; risks: string[];
  invalidation_context: string; limitations: string;
};
type CorrelationSnapshot = {
  status: "ready" | "unavailable";
  provider: string;
  dxy_symbol: string | null;
  common_m5_bars: number;
  minimum_age_seconds: number;
  updated_at: string;
  feeds: Record<string, {
    provider: string; symbol?: string | null; configured: boolean;
    candles?: number; age_seconds?: number | null; fresh?: boolean;
    last_success_at?: string | null; last_error?: string | null;
  }>;
  correlations?: Record<string, {
    timeframe: string;
    pairs: Record<string, Record<string, number | null>>;
  }>;
  warnings: string[];
};

const FRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const NAV = [
  ["Overview", LayoutDashboard],
  ["Macro & SMT", GitCompareArrows],
  ["AI analyst", BrainCircuit],
] as const;
const money = (value: number | null | undefined, digits = 5) =>
  typeof value === "number" && Number.isFinite(value) ? value.toFixed(Math.max(0, Math.min(10, digits))) : "—";
const timeLabel = (value?: string | null) => value ? new Date(value).toLocaleTimeString() : "—";
const ageLabel = (age?: number) => typeof age === "number" ? (age < 60 ? age + "s ago" : Math.floor(age / 60) + "m ago") : "—";

export default function Home() {
  const [section, setSection] = useState<string>("Overview");
  const [symbol, setSymbol] = useState<string>("EURUSD");
  const [frame, setFrame] = useState<string>("M15");
  const [status, setStatus] = useState<Status | null>(null);
  const [universe, setUniverse] = useState<MarketItem[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [macro, setMacro] = useState<CorrelationSnapshot | null>(null);
  const [syncingReferences, setSyncingReferences] = useState(false);
  const [referenceSyncMessage, setReferenceSyncMessage] = useState("");
  const [search, setSearch] = useState("");
  const [chartBars, setChartBars] = useState(false);
  const [chartMeta, setChartMeta] = useState<{source:string;updated:string;quoteAge?:number;bid?:number;ask?:number;spread?:number;digits?:number}>({source:"Waiting for broker feed",updated:"No broker candles received"});
  const [loading, setLoading] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [authReady, setAuthReady] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);
  const [commentary, setCommentary] = useState<Commentary | null>(null);
  const [commentaryLoading, setCommentaryLoading] = useState(false);
  const [commentaryError, setCommentaryError] = useState("");
  const chartEl = useRef<HTMLDivElement>(null);

  const selected = universe.find(item => item.symbol === symbol);
  const activeSignal = signals.find(item => item.symbol === symbol && item.timeframe === frame);
  const filtered = useMemo(() => universe.filter(item =>
    (item.symbol + " " + item.description + " " + item.asset_class).toLowerCase().includes(search.toLowerCase())
  ), [universe, search]);
  const streamCount = status?.broker_market_streams || 0;
  const feedConnected = streamCount > 0;
  const workerOnline = status?.process === "online";

  useEffect(() => {
    let stopped = false;
    fetch("/api/auth/login", { method: "GET", cache: "no-store" })
      .then(response => response.ok ? response.json() : { authenticated: false })
      .then(data => { if (!stopped) { setAuthenticated(Boolean(data.authenticated)); setAuthReady(true); } })
      .catch(() => { if (!stopped) setAuthReady(true); });
    return () => { stopped = true; };
  }, []);

  useEffect(() => {
    if (!authenticated) return;
    let stopped = false;
    const refresh = async () => {
      try {
        const [s, u, a, m] = await Promise.all([
          fetch("/api/status", { cache: "no-store" }),
          fetch("/api/universe", { cache: "no-store" }),
          fetch("/api/signals?data_mode=broker", { cache: "no-store" }),
          fetch("/api/correlation", { cache: "no-store" }),
        ]);
        if ([s, u, a, m].some(response => response.status === 401)) {
          setAuthenticated(false);
          return;
        }
        if (!s.ok || !u.ok || !a.ok) throw new Error("Worker API returned an error");
        const [nextStatus, nextUniverse, nextSignals] = await Promise.all([s.json(), u.json(), a.json()]);
        const nextMacro = m.ok ? await m.json() as CorrelationSnapshot : null;
        if (stopped) return;
        setStatus(nextStatus);
        setUniverse(nextUniverse.items || []);
        setSignals(nextSignals.items || []);
        setMacro(nextMacro);
        if ((nextUniverse.items || []).length) {
          setSymbol(current => current && nextUniverse.items.some((item: MarketItem) => item.symbol === current)
            ? current : nextUniverse.items[0].symbol);
        }
      } catch {
        if (!stopped) {
          setStatus({ process: "offline", data_mode: "disconnected", broker_market_streams: 0 });
          setUniverse([]);
          setSignals([]);
          setMacro(null);
        }
      }
    };
    void refresh();
    const timer = setInterval(refresh, 15000);
    return () => { stopped = true; clearInterval(timer); };
  }, [authenticated, refreshNonce]);

  useEffect(() => {
    if (!authenticated || !chartEl.current) return;
    let stopped = false;
    const chart = createChart(chartEl.current, {
      width: chartEl.current.clientWidth,
      height: 365,
      layout: { background: { type: ColorType.Solid, color: "#ffffff" }, textColor: "#718096", fontSize: 11 },
      grid: { vertLines: { color: "#f0f3f8" }, horzLines: { color: "#f0f3f8" } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#e8edf4" },
      timeScale: { borderColor: "#e8edf4", timeVisible: true },
      handleScroll: true, handleScale: true,
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#15966b", downColor: "#d95868", borderVisible: false,
      wickUpColor: "#15966b", wickDownColor: "#d95868",
    });
    let highLine: ReturnType<typeof candles.createPriceLine> | null = null;
    let lowLine: ReturnType<typeof candles.createPriceLine> | null = null;

    const refreshChart = async () => {
      if (!symbol) {
        candles.setData([]);
        setChartBars(false);
        setChartMeta({source:"Waiting for broker feed",updated:"Select a broker instrument"});
        return;
      }
      setLoading(true);
      try {
        const response = await fetch("/api/market/" + encodeURIComponent(symbol) + "?timeframe=" + frame, { cache: "no-store" });
        if (response.status === 401) { setAuthenticated(false); return; }
        if (!response.ok) throw new Error("No data");
        const data = await response.json();
        if (stopped) return;
        const bars: Bar[] = data.data_mode === "broker" && Array.isArray(data.bars) ? data.bars : [];
        candles.setData(bars.map(bar => ({...bar, time: bar.time as Time})));
        if (highLine) candles.removePriceLine(highLine);
        if (lowLine) candles.removePriceLine(lowLine);
        highLine = null; lowLine = null;
        if (bars.length) {
          const recent = bars.slice(-25);
          highLine = candles.createPriceLine({
            price: Math.max(...recent.map(bar => bar.high)), color: "#d69a36",
            lineWidth: 1, lineStyle: 2, title: "20-bar high candidate", axisLabelVisible: true,
          });
          lowLine = candles.createPriceLine({
            price: Math.min(...recent.map(bar => bar.low)), color: "#8873c7",
            lineWidth: 1, lineStyle: 2, title: "20-bar low candidate", axisLabelVisible: true,
          });
          chart.timeScale().fitContent();
        }
        setChartBars(bars.length > 0);
        setChartMeta({
          source: bars.length ? "Equiti MT5 broker candles" : "No broker data for this symbol/timeframe",
          updated: data.updated_at ? timeLabel(data.updated_at) : "Awaiting MT5 bridge",
          quoteAge: data.quote_age_seconds, bid: data.bid, ask: data.ask,
          spread: data.spread_points, digits: data.digits,
        });
      } catch {
        if (!stopped) {
          candles.setData([]);
          setChartBars(false);
          setChartMeta({source:"Broker feed unavailable",updated:"Check worker and MT5 connection"});
        }
      } finally {
        if (!stopped) setLoading(false);
      }
    };
    void refreshChart();
    const timer = setInterval(refreshChart, 15000);
    const resize = new ResizeObserver(entries => {
      if (entries[0]) chart.applyOptions({ width: entries[0].contentRect.width });
    });
    resize.observe(chartEl.current);
    return () => { stopped = true; clearInterval(timer); resize.disconnect(); chart.remove(); };
  }, [authenticated, symbol, frame, refreshNonce, section]);

  const changeSection = (name: string) => setSection(name);
  const heading = section === "Overview" ? "Market overview" : section;
  const brokerLabel = feedConnected ? "Equiti broker feed connected" : workerOnline ? "Worker online · awaiting MT5" : "Worker unavailable";

  const syncReferenceData = async () => {
    setSyncingReferences(true);
    setReferenceSyncMessage("");
    try {
      const response = await fetch("/api/reference/sync", { method: "POST" });
      const data = await response.json();
      if (response.status === 401) { setAuthenticated(false); return; }
      if (!response.ok) throw new Error(data.detail || data.error || "Reference sync failed");
      const successful = (data.results || []).filter((item: {status: string}) => item.status === "ok").length;
      setReferenceSyncMessage(successful + " reference instrument(s) synchronized from " + data.provider + ".");
      setRefreshNonce(value => value + 1);
    } catch (error) {
      setReferenceSyncMessage(error instanceof Error ? error.message : "Reference sync failed");
    } finally {
      setSyncingReferences(false);
    }
  };

  const runCommentary = async () => {
    setCommentaryLoading(true);
    setCommentaryError("");
    setCommentary(null);
    try {
      const response = await fetch("/api/ai/commentary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, timeframe: frame }),
      });
      const data = await response.json();
      if (response.status === 401) { setAuthenticated(false); return; }
      if (!response.ok) throw new Error(data.detail || data.error || "AI commentary is unavailable");
      setCommentary(data.commentary as Commentary);
    } catch (error) {
      setCommentaryError(error instanceof Error ? error.message : "Unable to generate commentary");
    } finally {
      setCommentaryLoading(false);
    }
  };

  const submitLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoginLoading(true);
    setLoginError("");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Sign-in failed");
      setPassword("");
      setAuthenticated(true);
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : "Unable to sign in");
    } finally {
      setLoginLoading(false);
    }
  };

  const logout = async () => {
    await fetch("/api/auth/login", { method: "DELETE" });
    setAuthenticated(false);
    setStatus(null);
    setUniverse([]);
    setSignals([]);
  };

  if (!authReady) {
    return <main style={{minHeight:"100vh",display:"grid",placeItems:"center",background:"#f5f7fb",color:"#526174",fontFamily:"Arial,sans-serif"}}>Loading secure workspace…</main>;
  }

  if (!authenticated) {
    return (
      <main style={{minHeight:"100vh",display:"grid",placeItems:"center",padding:24,background:"linear-gradient(135deg,#f7f9fd,#edf3ff)",fontFamily:"Arial,sans-serif",color:"#17243a"}}>
        <form onSubmit={submitLogin} style={{width:"min(100%,420px)",background:"#fff",border:"1px solid #e5ebf4",borderRadius:18,padding:34,boxShadow:"0 18px 55px rgba(22,48,88,.09)"}}>
          <div style={{display:"flex",alignItems:"center",gap:12,marginBottom:32}}>
            <div style={{width:42,height:42,borderRadius:12,background:"#064fc4",color:"#fff",display:"grid",placeItems:"center"}}><Activity size={22}/></div>
            <div><b style={{fontSize:19}}>SwiftLabor<span style={{color:"#0755ce"}}>.ai</span></b><div style={{fontSize:10,letterSpacing:1.5,color:"#718096",marginTop:4}}>PRIVATE MARKET INTELLIGENCE</div></div>
          </div>
          <div style={{fontSize:11,letterSpacing:1.4,color:"#63748b",fontWeight:700}}>SECURE CLIENT WORKSPACE</div>
          <h1 style={{fontSize:28,margin:"9px 0 8px"}}>Sign in</h1>
          <p style={{fontSize:14,lineHeight:1.6,color:"#6b7a90",marginBottom:24}}>Enter your workspace password to access broker market research.</p>
          <label htmlFor="workspace-password" style={{display:"block",fontSize:12,fontWeight:700,marginBottom:8}}>Workspace password</label>
          <input id="workspace-password" type="password" value={password} autoComplete="current-password" onChange={event => setPassword(event.target.value)} required maxLength={256} style={{width:"100%",boxSizing:"border-box",padding:"13px 14px",border:"1px solid #d8e0eb",borderRadius:9,fontSize:15,outlineColor:"#0755ce"}}/>
          {loginError && <p role="alert" style={{fontSize:13,color:"#b42318",margin:"12px 0 0"}}>{loginError}</p>}
          <button type="submit" disabled={loginLoading || !password} style={{width:"100%",marginTop:18,padding:14,border:0,borderRadius:9,background:"#0755ce",color:"#fff",fontWeight:700,cursor:"pointer",opacity:loginLoading || !password ? .6 : 1}}>{loginLoading ? "Signing in…" : "Access workspace"}</button>
          <p style={{fontSize:11,color:"#8995a6",lineHeight:1.5,marginTop:22}}>Broker credentials are never requested here. This workspace is read-only and does not place trades.</p>
        </form>
      </main>
    );
  }

  return (
    <main className="app">
      <aside className="side">
        <div className="brand"><i><Activity /></i><div><b>SwiftLabor<span>.ai</span></b><small>MARKET INTELLIGENCE</small></div></div>
        <label>WORKSPACE</label>
        <div className="desk"><b>Institutional desk</b><small>MT5-first market research</small></div>
        <label>RESEARCH</label>
        {NAV.map(([name, Icon]) => (
          <button className={"nav " + (section === name ? "active" : "")} key={name} onClick={() => changeSection(name)}>
            <Icon size={16}/>{name}{name === "AI analyst" && <em>OPTIONAL AI</em>}
          </button>
        ))}
        <a className="nav" href="/research"><ScanSearch size={16}/>Institutional Research <em>NEW</em></a>
        <div className="sidebottom">
          <div className="agent">
            <small><i className={feedConnected ? "green" : ""}/> DATA ENGINE</small>
            <b>{brokerLabel}</b>
            <p>{feedConnected ? streamCount + " live timeframe streams · read-only" : "No live quotes are invented or substituted."}</p>
          </div>
          <div className="profile">SL <span>Research workspace</span></div>
        </div>
      </aside>

      <section className="main">
        <header><span>Workspace <b>/ {section}</b></span>
          <div className={"status " + (feedConnected ? "live" : "demo")}><i/>{brokerLabel}</div>
          <button onClick={logout} title="Sign out" style={{display:"flex",alignItems:"center",gap:7,border:"1px solid #e2e8f0",background:"#fff",borderRadius:8,padding:"8px 11px",color:"#5a6a80",fontSize:12,cursor:"pointer"}}><LogOut size={14}/> Sign out</button>
        </header>
        <div className="content">
          <div className="heading">
            <div><small>SWIFTLABOR INSTITUTIONAL INTELLIGENCE</small><h1>{heading}</h1>
              <p>Broker data first. Explainable structure analysis. No synthetic market quotes.</p></div>
            <button onClick={() => { setLoading(true); setRefreshNonce(value => value + 1); }}> <RefreshCw size={13}/> Refresh view</button>
          </div>

          <div className={"notice " + (feedConnected ? "notice-live" : "")}>
            {feedConnected ? <Radio size={17}/> : <AlertTriangle size={17}/>}
            <div><b>{feedConnected ? "Real Equiti MT5 data stream detected" : workerOnline ? "Analysis service is ready — MT5 not connected" : "Live data is not connected yet"}</b>
              <p>{feedConnected
                ? "Charts and signals below are sourced from the connected broker terminal. Confirm quote freshness before using any research."
                : "The dashboard intentionally shows no made-up prices or candles. Connect the client's MT5 terminal to populate live broker data."}</p>
            </div>
            <button onClick={() => changeSection("Settings")}>Connection setup <ChevronRight size={13}/></button>
          </div>

          <div className="stats">
            <Metric icon={<CandlestickChart/>} title="BROKER INSTRUMENTS" value={String(status?.broker_symbols || 0).padStart(2,"0")} note="Discovered from MT5 Market Watch"/>
            <Metric icon={<Radio/>} title="ACTIVE DATA STREAMS" value={String(streamCount).padStart(2,"0")} note="Symbol + timeframe feeds"/>
            <Metric icon={<Gauge/>} title="ANALYSIS CYCLE" value={workerOnline ? (status?.scan_interval_seconds || 15) + "s" : "—"} note="Worker scan interval"/>
            <Metric icon={<ShieldCheck/>} title="EXECUTION MODE" value="Read-only" note="No broker orders or account actions"/>
          </div>

          {section === "Macro & SMT" ? (
            <>
              <div className="stats">
                <Metric icon={<Activity/>} title="EUR/USD REFERENCE" value={macro?.feeds?.EUR_USD?.fresh ? "Fresh" : "Unavailable"} note={macro?.feeds?.EUR_USD?.provider || "OANDA / Massive not configured"}/>
                <Metric icon={<Activity/>} title="GBP/USD REFERENCE" value={macro?.feeds?.GBP_USD?.fresh ? "Fresh" : "Unavailable"} note={macro?.feeds?.GBP_USD?.provider || "OANDA / Massive not configured"}/>
                <Metric icon={<Radio/>} title="DXY BROKER FEED" value={macro?.feeds?.DXY?.fresh ? "Fresh" : "Unavailable"} note={macro?.dxy_symbol || "Exact MT5 index symbol not configured"}/>
                <Metric icon={<Gauge/>} title="COMMON M5 BARS" value={String(macro?.common_m5_bars || 0)} note="Synchronized observations across all three feeds"/>
              </div>
              <section className="panel setup">
                <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",gap:12,flexWrap:"wrap"}}>
                  <div><small>CROSS-ASSET QUANTITATIVE MONITOR</small>
                    <h2>Rolling return correlation</h2></div>
                  <button onClick={() => void syncReferenceData()} disabled={syncingReferences || macro?.provider === "none" || macro?.provider === "mt5"}
                    style={{border:0,borderRadius:8,padding:"10px 13px",background:syncingReferences || macro?.provider === "none" || macro?.provider === "mt5" ? "#aab7c9" : "#0755ce",color:"#fff",fontWeight:700,cursor:syncingReferences || macro?.provider === "none" || macro?.provider === "mt5" ? "not-allowed" : "pointer"}}>
                    {syncingReferences ? "Syncing reference data…" : macro?.provider === "mt5" ? "MT5 candles sync automatically" : "Sync reference data"}
                  </button>
                </div>
                {referenceSyncMessage && <p role="status" style={{fontSize:12,color:referenceSyncMessage.includes("failed") ? "#b42318" : "#15835f"}}>{referenceSyncMessage}</p>}
                <p>Calculated from synchronized log returns. Missing intervals are not filled with synthetic prices. Correlation is context, not an entry signal or proof of SMT divergence.</p>
                {macro?.correlations ? ["M5", "M15", "H1"].map(period => {
                  const frameData = macro.correlations?.[period];
                  if (!frameData) return null;
                  const labels: [string, string][] = [
                    ["EURUSD_vs_DXY", "EUR/USD vs DXY"],
                    ["GBPUSD_vs_DXY", "GBP/USD vs DXY"],
                    ["EURUSD_vs_GBPUSD", "EUR/USD vs GBP/USD"],
                  ];
                  const fmt = (value: number | null | undefined) => typeof value === "number" ? value.toFixed(3) : "Insufficient data";
                  return <div key={period} style={{marginTop:20}}>
                    <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",marginBottom:9}}>
                      <b style={{fontSize:13}}>{period} correlation</b>
                      <span style={{fontSize:11,color:"#7a8798"}}>20 / 60 return windows</span>
                    </div>
                    <table><thead><tr><th>RELATIONSHIP</th><th>20 OBS.</th><th>60 OBS.</th><th>SAMPLES</th></tr></thead>
                      <tbody>{labels.map(([key,label]) => {
                        const values = frameData.pairs[key] || {};
                        return <tr key={key}>
                          <td><b>{label}</b></td>
                          <td>{fmt(values["20"])}</td><td>{fmt(values["60"])}</td>
                          <td>{String(values["samples_60"] ?? 0)}</td>
                        </tr>;
                      })}</tbody>
                    </table>
                  </div>;
                }) : <div className="empty"><GitCompareArrows size={24}/><b>Correlation unavailable</b><p>All three feeds need fresh, synchronized M5 candles and sufficient observations. No synthetic DXY or placeholder correlations are shown.</p></div>}
              </section>
              <section className="panel setup">
                <small>REFERENCE FEED HEALTH</small><h2>Data provenance & freshness</h2>
                <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(210px,1fr))",gap:12,marginTop:16}}>
                  {(["EUR_USD", "GBP_USD", "DXY"] as const).map(key => {
                    const feed = macro?.feeds?.[key];
                    return <div key={key} style={{border:"1px solid #e5eaf1",borderRadius:10,padding:14}}>
                      <b>{key.replace("_", "/")}</b>
                      <p style={{fontSize:12,color:"#66758a",margin:"7px 0"}}>{feed?.provider || "Not configured"} · {feed?.candles ?? 0} bars</p>
                      <strong style={{fontSize:13,color:feed?.fresh ? "#15835f" : "#9b6b28"}}>{feed?.fresh ? "Fresh" : "Unavailable / stale"}</strong>
                      <p style={{fontSize:11,color:"#8793a4",marginTop:6}}>Age: {typeof feed?.age_seconds === "number" ? feed.age_seconds + "s" : "—"}</p>
                      {feed?.last_error && <p style={{fontSize:11,color:"#b42318"}}>{feed.last_error}</p>}
                    </div>;
                  })}
                </div>
                {macro?.warnings?.map((warning, index) => <p key={index} style={{fontSize:12,lineHeight:1.6,color:"#718096",marginTop:10}}>• {warning}</p>)}
                <p style={{fontSize:11,color:"#8894a5",marginTop:14}}>Updated: {macro?.updated_at ? timeLabel(macro.updated_at) : "—"} · Maximum accepted age: {macro?.minimum_age_seconds ?? 900}s</p>
              </section>
              <section className="panel setup">
                <small>SMT INTERPRETATION BOUNDARY</small><h2>Correlation is not SMT</h2>
                <p>This page reports return correlation and data health. A separate swing-matching engine must detect one pair taking a corresponding swing high/low while the other fails. EUR/USD is a major DXY component, so that pairwise inverse correlation is partly mechanical.</p>
                <p>DXY must be a verified Equiti MT5 index symbol. Run a separate bridge instance on M5 for that exact symbol; this screen will remain unavailable until it receives fresh candles.</p>
              </section>
            </>
          ) : section === "Settings" ? (
            <section className="panel setup">
              <small>CONNECTION STATUS</small><h2>Equiti MT5 bridge</h2>
              <p>Backend: Server-side authenticated connection (worker URL is private)</p>
              <p>Worker: {workerOnline ? "Online" : "Not reachable"} · MT5 streams: {streamCount}</p>
              <p>Broker server: {selected?.broker_server || "Will be identified from the connected MT5 terminal"}</p>
              <p>Bridge is read-only. MT5 login passwords are never required by SwiftLabor.</p>
              <ol><li>Deploy the worker and create a private bridge token in its environment.</li><li>Install the SwiftLaborBridge EA in the client's MT5 terminal.</li><li>Allow the worker HTTPS domain in MT5 WebRequest settings.</li><li>Attach the EA to charts using M15, H1 and H4 timeframe instances for market analysis.</li><li>For MT5-native correlation, add a separate M5 bridge instance and set InpSymbols to all three exact verified EUR/USD, GBP/USD and DXY Market Watch symbols.</li></ol><p><b>Correlation feed:</b> You can use OANDA/Massive reference candles, or set EURUSD_MT5_SYMBOL, GBPUSD_MT5_SYMBOL and DXY_MT5_SYMBOL to the exact Equiti Market Watch names. In MT5-native mode, attach one M5 bridge instance with all three symbols; candles sync automatically. Never put provider keys in the browser.</p>
            </section>
          ) : section === "AI analyst" ? (
            <section className="panel setup">
              <small><BrainCircuit size={13}/> EXPLAINABLE MARKET ANALYSIS</small><h2>Rules engine status</h2>
              <p>{status?.engine || "Analysis worker is not reporting an active engine."}</p>
              <p>This build evaluates completed broker candles for a 20-bar sweep-and-reclaim candidate, directional displacement, short-term structure break, imbalance candidate, and spread relative to ATR.</p>
              <p>{status?.ai_commentary ? "On-demand AI commentary is enabled when requested. The deterministic signal engine remains authoritative." : "Generative AI commentary is not configured. This screen reports the deterministic rules engine only."}</p>
              <div style={{display:"flex",alignItems:"center",gap:12,flexWrap:"wrap",margin:"18px 0"}}>
                <button onClick={() => void runCommentary()} disabled={!status?.ai_commentary || !feedConnected || commentaryLoading} style={{border:0,borderRadius:8,padding:"11px 15px",background:!status?.ai_commentary || !feedConnected ? "#aab7c9" : "#0755ce",color:"#fff",fontWeight:700,cursor:!status?.ai_commentary || !feedConnected ? "not-allowed" : "pointer"}}>
                  {commentaryLoading ? "Reviewing broker snapshot…" : "Generate AI commentary"}
                </button>
                <span style={{fontSize:12,color:"#718096"}}>{symbol} · {frame} · broker data only</span>
              </div>
              {commentaryError && <p role="alert" style={{color:"#b42318"}}>{commentaryError}</p>}
              {commentary && <div style={{border:"1px solid #e2e9f2",borderRadius:12,padding:18,marginTop:14,background:"#fbfcfe"}}>
                <small style={{fontWeight:700,letterSpacing:1,color:"#64748b"}}>AI RESEARCH COMMENTARY · NOT A TRADE INSTRUCTION</small>
                <p style={{fontSize:15,lineHeight:1.7,color:"#28384d"}}>{commentary.summary}</p>
                <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(220px,1fr))",gap:14,marginTop:12}}>
                  <div><b style={{fontSize:12}}>OBSERVATIONS</b>{commentary.observations.map((item,i)=><p key={i} style={{fontSize:13,lineHeight:1.55,color:"#5d6b7f"}}>• {item}</p>)}</div>
                  <div><b style={{fontSize:12}}>RISKS</b>{commentary.risks.map((item,i)=><p key={i} style={{fontSize:13,lineHeight:1.55,color:"#5d6b7f"}}>• {item}</p>)}</div>
                </div>
                <p style={{fontSize:13,lineHeight:1.55,color:"#5d6b7f"}}><b>Invalidation context:</b> {commentary.invalidation_context || "Not defined by this analysis."}</p>
                <p style={{fontSize:12,lineHeight:1.55,color:"#8591a2"}}><b>Limitations:</b> {commentary.limitations}</p>
              </div>}
              <p>Scores represent rule evidence weights, not win probabilities, certainty, or expected returns.</p>
            </section>
          ) : section === "Risk & journal" ? (
            <section className="panel setup"><small>RISK & JOURNAL</small><h2>Read-only research mode</h2>
              <p>Trade execution is disabled. This bridge does not import private account history, balances, open positions or credentials.</p>
              <p>Risk calculations and journaling should be enabled only after the client explicitly authorizes those additional account-data scopes.</p>
            </section>
          ) : (
            <>
              <div className="grid">
                <section className="panel chartpanel">
                  <div className="charthead"><div><small>SELECTED BROKER INSTRUMENT</small><h2>{symbol || "Awaiting MT5 symbol"} <em>{selected?.asset_class?.toUpperCase() || "MARKET"}</em></h2>
                    <p>{selected?.description || "Instrument and exact broker suffix will populate from MT5 Market Watch"}</p></div>
                    <div className="price"><small>{chartMeta.source.toUpperCase()}</small><b>{money(selected?.last_price, selected?.digits ?? 5)}</b></div>
                  </div>
                  <div className="toolbar">{FRAMES.map(f => <button className={frame === f ? "chosen" : ""} key={f} onClick={() => setFrame(f)}>{f}</button>)}
                    <span><Waves size={13}/> PRICE ACTION</span></div>
                  <div className="chartwrap"><div className="chart" ref={chartEl}/>{!chartBars && <div className="chartplaceholder"><Radio size={25}/><b>{loading ? "Checking broker feed…" : "Waiting for Equiti MT5 candles"}</b><p>Select an instrument from the live Market Watch list once the bridge is connected.</p></div>}</div>
                  <div className="chartfoot"><span><i className={chartBars ? "green" : ""}/>{chartMeta.source}</span><span>{chartMeta.updated}</span><span>Quote age: {ageLabel(chartMeta.quoteAge)}</span></div>
                  <div className="marketmeta"><span>Bid <b>{money(chartMeta.bid, chartMeta.digits ?? 5)}</b></span><span>Ask <b>{money(chartMeta.ask, chartMeta.digits ?? 5)}</b></span><span>Spread <b>{chartMeta.spread ?? "—"} pts</b></span><span>Last feed <b>{timeLabel(status?.last_feed_at)}</b></span></div>
                  <div className="credit">Chart engine: <a href="https://www.tradingview.com/lightweight-charts/" target="_blank" rel="noreferrer">TradingView Lightweight Charts™</a></div>
                </section>

                <section className="panel analyst">
                  <small className="kicker"><BrainCircuit size={13}/> SWIFTLABOR MARKET ANALYST</small><h2>Setup assessment</h2>
                  <div className="agentstatus"><BrainCircuit/><div><b>{activeSignal ? activeSignal.side : "NO TRADE"}</b>
                    <small>{activeSignal ? symbol + " · " + frame + " · broker data" : "No validated broker-data setup"}</small></div></div>
                  <hr/><small className="kicker">LATEST ASSESSMENT</small>
                  <div className="assessment"><b>{activeSignal ? activeSignal.side : "STAND ASIDE"}</b>
                    <p>{activeSignal?.reason || "A research signal appears only after a fresh MT5 candle stream and the required price-action conditions are met."}</p></div>
                  <div className="row"><span><Waves size={14}/> Liquidity sweep candidate</span><b>{activeSignal?.evidence?.some(v => v.toLowerCase().includes("liquidity sweep")) ? "Detected" : "Not confirmed"}</b></div>
                  <div className="row"><span><Activity size={14}/> Displacement</span><b>{activeSignal?.evidence?.some(v => v.toLowerCase().includes("displacement")) ? "Detected" : "Not confirmed"}</b></div>
                  <div className="row"><span><Target size={14}/> Structure break</span><b>{activeSignal?.evidence?.some(v => v.toLowerCase().includes("structure break")) ? "Detected" : "Not confirmed"}</b></div>
                  <div className="row"><span><Gauge size={14}/> Evidence score</span><b>{activeSignal ? activeSignal.score + "/100" : "—"}</b></div>
                  <div className="note">Heuristic evidence score only — not a probability of profit. No entry orders are sent.</div>
                </section>
              </div>

              <div className="bottom">
                <section className="panel watch">
                  <div className="subhead"><div><small>LIVE MARKET WATCH</small><h2>Broker instruments</h2></div>
                    <div className="search"><Search size={13}/><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search symbols"/></div>
                  </div>
                  {filtered.length ? <table><thead><tr><th>INSTRUMENT</th><th>LAST</th><th>SPREAD</th><th>QUOTE AGE</th></tr></thead>
                    <tbody>{filtered.map(item => <tr key={item.symbol} onClick={() => setSymbol(item.symbol)} className={symbol === item.symbol ? "selected" : ""}>
                      <td><b>{item.symbol}</b><small>{item.description || item.asset_class}</small></td>
                      <td>{money(item.last_price, item.digits ?? 5)}</td><td>{item.spread_points ?? "—"} pts</td>
                      <td><i className={"tag " + (item.quote_fresh ? "fresh" : "")}>{ageLabel(item.quote_age_seconds)}</i></td>
                    </tr>)}</tbody></table> : <div className="empty"><Radio size={23}/><b>No MT5 instruments received</b><p>Once the client terminal sends its Market Watch snapshots, all available Equiti symbols will appear here, including suffixes.</p></div>}
                </section>
                <section className="panel signals">
                  <div className="subhead"><div><small>BROKER-DATA SETUPS</small><h2>Signal watch</h2></div><span>{signals.length} assessments</span></div>
                  {signals.length ? signals.slice(0,8).map(item => <div className="signal" key={item.symbol + item.timeframe} onClick={() => {setSymbol(item.symbol);setFrame(item.timeframe);}}>
                    <b>{item.symbol} · {item.timeframe}</b><span>{item.side} — {item.reason}</span><strong>{item.score}</strong>
                  </div>) : <div className="empty"><Target size={23}/><b>No broker signals yet</b><p>The engine will publish only when live completed candles support a qualifying setup.</p></div>}
                  <div className="note">A signal is a research watch, not an instruction to place a trade.</div>
                </section>
              </div>
              {section === "Liquidity map" && <section className="panel setup extra"><small>LIQUIDITY MAP · {symbol}</small><h2>Observable price extremes</h2><p>The chart plots recent 25-bar highs/lows as broker-candle reference levels. These are not global liquidity pools, dealer books, hidden orders, or guaranteed stop clusters.</p></section>}
              {section === "Signal center" && <section className="panel setup extra"><small>SIGNAL CENTER</small><h2>Rule-based broker assessments</h2><p>Only fresh broker streams are eligible. Signals expire when their feed becomes stale.</p></section>}
            </>
          )}
          <footer>© 2026 SwiftLabor.ai · Equiti MT5 bridge · Research-only mode · No trade execution</footer>
        </div>
      </section>
    </main>
  );
}

function Metric({icon,title,value,note}:{icon:ReactNode;title:string;value:string;note:string}) {
  return <div className="metric"><div><small>{title}</small><i>{icon}</i></div><b>{value}</b><span>{note}</span></div>;
}

