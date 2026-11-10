"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { Activity, ArrowLeft, RefreshCw, ShieldCheck, Target, BookOpen, CalendarClock, AlertTriangle } from "lucide-react";

type AnyRecord = Record<string, any>;
type CalendarEvent = { id:number; title:string; currency:string; impact:string; event_time:number; blackout_before_minutes:number; blackout_after_minutes:number };
type JournalEntry = { id:number; symbol:string; direction:string; timeframe:string; entry_price:number; stop_price:number; target_price:number; risk_percent:number; status:string; r_multiple:number|null; notes:string; created_at:string };

async function api<T>(url:string, init?:RequestInit):Promise<T> {
  const response = await fetch(url, { cache:"no-store", ...init, headers:{ "Content-Type":"application/json", ...(init?.headers || {}) } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.error || "Request failed");
  return body as T;
}

const fmt = (value:any, digits=5) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
const dateTime = (value:number|string) => new Date(typeof value === "number" ? value * 1000 : value).toLocaleString();
function readLocal<T>(key:string,fallback:T):T {
  try { const raw=window.localStorage.getItem(key); return raw ? JSON.parse(raw) as T : fallback; } catch { return fallback; }
}
function writeLocal(key:string,value:unknown) {
  try { window.localStorage.setItem(key,JSON.stringify(value)); } catch { /* local persistence may be unavailable */ }
}
const labels:Record<string,string> = {
  liquidity_sweep:"Liquidity sweep / reclaim",
  directional_displacement:"Directional displacement",
  structure_break:"BOS in setup direction",
  higher_timeframe_alignment:"D1 / H4 / H1 alignment",
  fresh_quotes:"Fresh broker data",
  spread_filter:"Spread / ATR filter",
  preferred_session:"London / New York window",
  unmitigated_fvg:"Unmitigated FVG",
  ote_confluence:"OTE retracement confluence",
  no_high_impact_news_blackout:"Outside manual news blackout",
};

export default function ResearchPage() {
  const [authenticated,setAuthenticated] = useState(false);
  const [authChecked,setAuthChecked] = useState(false);
  const [symbol,setSymbol] = useState("EURUSD");
  const [report,setReport] = useState<AnyRecord|null>(null);
  const [events,setEvents] = useState<CalendarEvent[]>([]);
  const [journal,setJournal] = useState<JournalEntry[]>([]);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");
  const [riskResult,setRiskResult] = useState<AnyRecord|null>(null);
  const [riskError,setRiskError] = useState("");
  const [equity,setEquity] = useState("");
  const [riskPercent,setRiskPercent] = useState("0.5");
  const [stopPips,setStopPips] = useState("20");
  const [pipValue,setPipValue] = useState("");
  const [lotStep,setLotStep] = useState("0.01");
  const [backtest,setBacktest] = useState<AnyRecord|null>(null);
  const [backtestError,setBacktestError] = useState("");
  const [backtestBusy,setBacktestBusy] = useState(false);
  const [eventTitle,setEventTitle] = useState("");
  const [eventCurrency,setEventCurrency] = useState("USD");
  const [eventImpact,setEventImpact] = useState("high");
  const [eventTime,setEventTime] = useState("");
  const [eventBefore,setEventBefore] = useState("15");
  const [eventAfter,setEventAfter] = useState("15");
  const [calendarMessage,setCalendarMessage] = useState("");
  const [direction,setDirection] = useState("LONG");
  const [journalTf,setJournalTf] = useState("M15");
  const [entry,setEntry] = useState("");
  const [stop,setStop] = useState("");
  const [target,setTarget] = useState("");
  const [journalRisk,setJournalRisk] = useState("0.5");
  const [notes,setNotes] = useState("");
  const [journalMessage,setJournalMessage] = useState("");

  const refresh = useCallback(async () => {
    setBusy(true); setError("");
    const [researchResult,calendarResult,journalResult] = await Promise.allSettled([
      api<AnyRecord>("/api/institutional/" + encodeURIComponent(symbol)),
      api<{items:CalendarEvent[]}>("/api/calendar"),
      api<{items:JournalEntry[]}>("/api/journal"),
    ]);
    if (researchResult.status === "fulfilled") setReport(researchResult.value);
    else { setReport(null); setError("Live analysis is unavailable until the cloud worker and database are connected."); }
    if (calendarResult.status === "fulfilled") setEvents(calendarResult.value.items || []);
    else setEvents(readLocal<CalendarEvent[]>("swiftlabor:calendar:manual", []));
    if (journalResult.status === "fulfilled") setJournal(journalResult.value.items || []);
    else setJournal(readLocal<JournalEntry[]>("swiftlabor:journal:planned", []));
    setBusy(false);
  },[symbol]);

  useEffect(() => {
    let active = true;
    api<{authenticated:boolean}>("/api/auth/login").then(result => {
      if (active) { setAuthenticated(Boolean(result.authenticated)); setAuthChecked(true); }
    }).catch(() => { if (active) setAuthChecked(true); });
    return () => { active = false; };
  },[]);

  useEffect(() => {
    if (authenticated) void refresh();
  },[authenticated,refresh]);

  const calculateRisk = async () => {
    setRiskError(""); setRiskResult(null);
    const values=[Number(equity),Number(riskPercent),Number(stopPips),Number(pipValue),Number(lotStep)];
    if (!values.every(Number.isFinite) || values[0]<=0 || values[1]<=0 || values[1]>1 || values[2]<=0 || values[3]<=0 || values[4]<=0) {
      setRiskError("Enter valid positive inputs. Risk must be no more than 1%.");
      return;
    }
    const [account,risk,stopDistance,pip,step]=values;
    const riskAmount=account*risk/100;
    const rawLots=riskAmount/(stopDistance*pip);
    const lots=Math.floor(rawLots/step+1e-9)*step;
    setRiskResult({
      risk_amount:Math.round(riskAmount*100)/100, raw_lots:rawLots,
      rounded_lots:lots, below_broker_minimum_lot_step:lots<step,
      minimum_target_distance_pips:stopDistance*3.5,
      estimated_loss_at_stop:lots*stopDistance*pip,
    });
  };

  const runBacktest = async () => {
    setBacktestBusy(true); setBacktestError(""); setBacktest(null);
    try {
      const result=await api<AnyRecord>("/api/backtest/"+encodeURIComponent(symbol)+"?timeframe=M15");
      setBacktest(result);
    } catch(e) { setBacktestError(e instanceof Error ? e.message : "Backtest could not run"); }
    finally { setBacktestBusy(false); }
  };

  const addCalendarEvent = async (event:FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setCalendarMessage("");
    if (!eventTime || !Number.isFinite(Date.parse(eventTime))) { setCalendarMessage("Enter a valid event date and time."); return; }
    try {
      const payload={
        title:eventTitle, currency:eventCurrency, impact:eventImpact,
        event_time:Math.floor(Date.parse(eventTime)/1000),
        blackout_before_minutes:Number(eventBefore), blackout_after_minutes:Number(eventAfter),
      };
      try {
        const saved=await api<{item:CalendarEvent}>("/api/calendar",{method:"POST",body:JSON.stringify(payload)});
        setEvents(current=>[saved.item,...current].sort((a,b)=>a.event_time-b.event_time));
        setCalendarMessage("Event saved to the cloud calendar.");
      } catch {
        const localEvent:CalendarEvent={id:-Date.now(),...payload};
        const next=[localEvent,...readLocal<CalendarEvent[]>("swiftlabor:calendar:manual",[])].sort((a,b)=>a.event_time-b.event_time);
        writeLocal("swiftlabor:calendar:manual",next); setEvents(next);
        setCalendarMessage("Cloud worker unavailable: saved in this browser only.");
      }
      setEventTitle(""); setEventTime("");
      await refresh();
    } catch(e) { setCalendarMessage(e instanceof Error ? e.message : "Could not save event"); }
  };

  const addJournal = async (event:React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setJournalMessage("");
    try {
      const payload={
        symbol, direction, timeframe:journalTf, entry_price:Number(entry),
        stop_price:Number(stop), target_price:Number(target),
        risk_percent:Number(journalRisk), status:"planned", notes,
      };
      try {
        const saved=await api<{item:JournalEntry}>("/api/journal",{method:"POST",body:JSON.stringify(payload)});
        setJournal(current=>[saved.item,...current]);
        setJournalMessage("Research plan saved to the cloud journal.");
      } catch {
        const localEntry:JournalEntry={id:-Date.now(),...payload,r_multiple:null,created_at:new Date().toISOString()};
        const next=[localEntry,...readLocal<JournalEntry[]>("swiftlabor:journal:planned",[])];
        writeLocal("swiftlabor:journal:planned",next); setJournal(next);
        setJournalMessage("Cloud worker unavailable: plan saved in this browser only.");
      }
      setEntry(""); setStop(""); setTarget(""); setNotes("");
      await refresh();
    } catch(e) { setJournalMessage(e instanceof Error ? e.message : "Could not save journal entry"); }
  };

  if (!authChecked) return <main className="research-shell"><div className="research-empty">Checking secure workspace…</div></main>;
  if (!authenticated) return <main className="research-shell"><div className="research-empty"><ShieldCheck size={32}/><h1>Secure workspace required</h1><p>Sign in to the SwiftLabor dashboard first.</p><Link href="/">Go to dashboard sign in</Link></div></main>;

  const lower=report?.lower_timeframe || {};
  const bias=report?.higher_timeframe_bias || {};
  const range=lower.dealing_range || {};
  const fvg=lower.nearest_active_fvg;
  const ote=lower.ote;
  const session=report?.session || {};
  const blockers:string[]=report?.blockers || [];
  const decision=report?.decision || "Awaiting research snapshot";
  const ready=lower.status === "ready";
  const passing=Object.values(report?.checks || {}).filter(Boolean).length;
  const checksCount=Object.keys(report?.checks || {}).length;

  return (
    <main className="research-shell">
      <header className="research-header">
        <div className="research-brand"><span><Activity size={20}/></span><div><b>SwiftLabor.ai</b><small>INSTITUTIONAL RESEARCH SUITE</small></div></div>
        <nav><Link href="/"><ArrowLeft size={15}/> Dashboard</Link><button onClick={() => void refresh()} disabled={busy}><RefreshCw size={14} className={busy?"spin":""}/> Refresh research</button></nav>
      </header>

      <section className="research-content">
        <div className="research-title">
          <div><small>MARKET STRUCTURE · RISK · VALIDATION</small><h1>Institutional Research Desk</h1><p>Multi-timeframe broker-candle analysis. Research only — no broker execution.</p></div>
          <div className="symbol-picker"><label htmlFor="research-symbol">INSTRUMENT</label><input id="research-symbol" value={symbol} onChange={e=>setSymbol(e.target.value.toUpperCase().replace(/[^A-Z0-9._+-]/g,"").slice(0,32))} maxLength={32}/></div>
        </div>

        {error && <div className="research-alert"><AlertTriangle size={17}/><span>{error}. Cloud worker and database can be connected later; this screen will not fabricate prices or analysis.</span></div>}

        <div className="research-grid research-top-grid">
          <article className="research-card decision-card">
            <small>RESEARCH DECISION</small>
            <h2>{decision}</h2>
            <p>{report?.symbol || symbol} · {report?.data_mode || "No broker data"} · {report?.evaluated_at ? dateTime(report.evaluated_at) : "No timestamp"}</p>
            <div className="scoreline"><b>{report?.evidence_score ?? 0}<small>/100</small></b><span>Checklist completion<br/>not win probability</span></div>
            <div className="progress"><i style={{width:(report?.evidence_score ?? 0)+"%"}}/></div>
          </article>
          <article className="research-card">
            <small>HIGHER-TIMEFRAME BIAS</small>
            <h2 className="capitalize">{bias.bias || "Unavailable"}</h2>
            <p>Weighted D1 / H4 / H1 structure votes</p>
            <div className="vote-row"><span>D1</span><b>{report?.timeframes?.D1?.trend || "No feed"}</b><span>×3</span></div>
            <div className="vote-row"><span>H4</span><b>{report?.timeframes?.H4?.trend || "No feed"}</b><span>×2</span></div>
            <div className="vote-row"><span>H1</span><b>{report?.timeframes?.H1?.trend || "No feed"}</b><span>×1</span></div>
          </article>
          <article className="research-card">
            <small>SESSION CONTEXT</small>
            <h2>{report?.session?.labels?.join(" · ") || "Unavailable"}</h2>
            <p>London: {session.london_time ? new Date(session.london_time).toLocaleTimeString() : "—"}</p>
            <p>New York: {session.new_york_time ? new Date(session.new_york_time).toLocaleTimeString() : "—"}</p>
            <span className={"state-pill "+(session.in_preferred_window?"positive":"neutral")}>{report?.session?.in_preferred_window?"Preferred window":"Outside preferred window"}</span>
          </article>
        </div>

        <section className="research-card">
          <div className="research-section-head"><div><small>CONFLUENCE GATES</small><h2>Setup validation checklist</h2></div><span>{passing}/{checksCount} checks passed</span></div>
          <div className="check-grid">
            {Object.entries(report?.checks || {}).map(([key,value])=><div className="check-item" key={key}><span className={value?"check-ok":"check-no"}>{value?"✓":"—"}</span><span>{labels[key] || key}</span></div>)}
          </div>
          {blockers.length>0 && <p className="muted-line">Blocking / incomplete: {blockers.map((x:string)=>labels[x]||x).join(", ")}</p>}
        </section>

        <div className="research-grid research-analysis-grid">
          <article className="research-card">
            <small>MARKET STRUCTURE · {lower.timeframe || "M15"}</small><h2>Structure & dealing range</h2>
            <div className="data-pair"><span>Trend</span><b className="capitalize">{lower.trend || "Unavailable"}</b></div>
            <div className="data-pair"><span>BOS</span><b className="capitalize">{lower.bos || "Not detected"}</b></div>
            <div className="data-pair"><span>CHoCH</span><b className="capitalize">{lower.choch || "Not detected"}</b></div>
            <div className="data-pair"><span>Last closed</span><b>{lower.last_closed_candle_time?dateTime(lower.last_closed_candle_time):"—"}</b></div>
            <div className="data-pair"><span>ATR(14)</span><b>{fmt(lower.atr,6)}</b></div>
            <div className="data-pair"><span>Range high</span><b>{fmt(range.high,6)}</b></div>
            <div className="data-pair"><span>Equilibrium</span><b>{fmt(range.equilibrium,6)}</b></div>
            <div className="data-pair"><span>Range low</span><b>{fmt(range.low,6)}</b></div>
            <div className="data-pair"><span>Price location</span><b className="capitalize">{range.location || "—"}</b></div>
            <p className="muted-line">Swings are confirmed only after two later candles have closed; the forming candle is excluded.</p>
          </article>
          <article className="research-card">
            <small>LIQUIDITY & IMBALANCE</small><h2>Sweep, equal highs/lows & FVG</h2>
            <div className="data-pair"><span>Sweep</span><b>{lower.sweep ? lower.sweep.side+" @ "+fmt(lower.sweep.level,6) : "Not confirmed"}</b></div>
            <div className="data-pair"><span>Sweep wick extreme</span><b>{fmt(lower.sweep?.wick_extreme,6)}</b></div>
            <div className="data-pair"><span>Displacement</span><b>{lower.displacement?.qualified ? lower.displacement.side+" · "+lower.displacement.range_atr+" ATR" : "Not confirmed"}</b></div>
            <div className="data-pair"><span>FVG status</span><b>{fvg ? fvg.side+" · "+fvg.status : "No active FVG"}</b></div>
            <div className="data-pair"><span>FVG zone</span><b>{fvg ? fmt(fvg.low,6)+" – "+fmt(fvg.high,6) : "—"}</b></div>
            <div className="data-pair"><span>Equal highs pools</span><b>{lower.equal_highs?.length || 0}</b></div>
            <div className="data-pair"><span>Equal lows pools</span><b>{lower.equal_lows?.length || 0}</b></div>
            <p className="muted-line">These are broker-candle price references, not verified global stops or order-book liquidity.</p>
          </article>
          <article className="research-card">
            <small>DEALING RANGE & RETRACEMENT</small><h2>Premium / discount & OTE</h2>
            <div className="data-pair"><span>Price quadrant</span><b className="capitalize">{range.location || "—"}</b></div>
            <div className="data-pair"><span>OTE side</span><b className="capitalize">{ote?.side || "Unavailable"}</b></div>
            <div className="data-pair"><span>OTE zone</span><b>{ote ? fmt(ote.low,6)+" – "+fmt(ote.high,6) : "—"}</b></div>
            <div className="data-pair"><span>Current close in OTE</span><b>{ote ? (ote.in_zone?"Yes":"No") : "Not calculated"}</b></div>
            <div className="data-pair"><span>Spread / ATR</span><b>{report?.spread_to_atr!=null ? (report.spread_to_atr*100).toFixed(2)+"%" : "Unavailable"}</b></div>
            <p className="muted-line">OTE is calculated from the last confirmed swing leg. It is not a universally accepted entry rule.</p>
          </article>
        </div>

        <section className="research-card">
          <div className="research-section-head"><div><small>POSITION RISK</small><h2>Risk & position-size calculator</h2></div><span className="state-pill neutral">Max 1% per trade · minimum 3.5R</span></div>
          <div className="risk-inputs">
            <label>Account equity<input type="number" min="0" step="any" value={equity} onChange={e=>setEquity(e.target.value)}/></label>
            <label>Risk % (max 1%)<input type="number" min="0.01" max="1" step="0.01" value={riskPercent} onChange={e=>setRiskPercent(e.target.value)}/></label>
            <label>Stop distance (pips)<input type="number" min="0" step="any" value={stopPips} onChange={e=>setStopPips(e.target.value)}/></label>
            <label>Pip value / standard lot<input type="number" min="0" step="any" value={pipValue} onChange={e=>setPipValue(e.target.value)}/></label>
            <label>Broker lot step<input type="number" min="0" step="any" value={lotStep} onChange={e=>setLotStep(e.target.value)}/></label>
          </div>
          <div className="form-actions"><button onClick={()=>void calculateRisk()}><Target size={14}/> Calculate size</button><span>Enter the pip value in your account currency from the broker's contract specification. It is not guessed by the app.</span></div>
          {riskError && <p className="inline-error">{riskError}</p>}
          {riskResult && <div className="risk-result">
            <div><small>ROUNDED VOLUME</small><b>{fmt(riskResult.rounded_lots,2)} lots</b></div>
            <div><small>AMOUNT AT RISK</small><b>{fmt(riskResult.risk_amount,2)}</b></div>
            <div><small>ESTIMATED LOSS AT STOP</small><b>{fmt(riskResult.estimated_loss_at_stop,2)}</b></div>
            <div><small>MINIMUM TARGET DISTANCE</small><b>{fmt(riskResult.minimum_target_distance_pips,1)} pips</b></div>
          </div>}
          <p className="muted-line">Sizing excludes commission, slippage, swap, gaps and conversion changes. Validate symbol contract size, pip convention, lot step and minimum volume with Equiti before using a calculated volume.</p>
        </section>

        <section className="research-card">
          <div className="research-section-head"><div><small>HISTORICAL VALIDATION</small><h2>Backtest & walk-forward split</h2></div><button className="secondary-action" onClick={()=>void runBacktest()} disabled={backtestBusy}><Activity size={14}/>{backtestBusy?"Running…":"Run M15 backtest"}</button></div>
          <p className="muted-line">Rule set: sweep + displacement + BOS; next-bar-open entry; 3.5R target; stop-first on same-bar target/stop collision; one position at a time.</p>
          {backtestError && <p className="inline-error">{backtestError}</p>}
          {backtest && <><div className="backtest-grid">
            {(["in_sample","out_of_sample","full_sample"] as const).map(k=>{const m=backtest[k]||{};return <div className="backtest-card" key={k}><small>{k.replace("_"," ").toUpperCase()}</small><b>{m.trades??0} trades</b><div>Win rate: {m.win_rate_percent==null?"—":m.win_rate_percent+"%"}</div><div>Expectancy: {m.expectancy_r==null?"—":m.expectancy_r+"R"}</div><div>Profit factor: {m.profit_factor??"—"}</div><div>Max drawdown: {m.max_drawdown_r??"—"}R</div></div>;})}
          </div><p className="muted-line">{backtest.out_of_sample?.warning}</p></>}
          {!backtest && !backtestError && <p className="research-empty small-empty">Requires at least 120 completed historical broker candles for {symbol} M15.</p>}
        </section>

        <div className="research-grid research-analysis-grid">
          <section className="research-card">
            <div className="research-section-head"><div><small>NEWS RISK</small><h2>Manual economic calendar</h2></div><CalendarClock size={18}/></div>
            <p className="muted-line">Add verified high-impact releases manually. This does not fetch a live news calendar.</p>
            <form className="research-form" onSubmit={addCalendarEvent}>
              <label>Event title<input value={eventTitle} onChange={e=>setEventTitle(e.target.value)} required minLength={3} maxLength={120} placeholder="e.g. US CPI"/></label>
              <div className="form-two">
                <label>Currency<select value={eventCurrency} onChange={e=>setEventCurrency(e.target.value)}>{["USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD","ALL"].map(v=><option key={v}>{v}</option>)}</select></label>
                <label>Impact<select value={eventImpact} onChange={e=>setEventImpact(e.target.value)}><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></label>
              </div>
              <label>Event time (your local time)<input type="datetime-local" value={eventTime} onChange={e=>setEventTime(e.target.value)} required/></label>
              <div className="form-two"><label>Block before (min)<input type="number" min="0" max="240" value={eventBefore} onChange={e=>setEventBefore(e.target.value)}/></label><label>Block after (min)<input type="number" min="0" max="240" value={eventAfter} onChange={e=>setEventAfter(e.target.value)}/></label></div>
              <button type="submit"><CalendarClock size={14}/> Save event</button>
            </form>
            {calendarMessage && <p className="form-message">{calendarMessage}</p>}
            <div className="event-list">{events.slice(0,8).map(e=><div className="event-row" key={e.id}><span className={"impact "+e.impact}>{e.impact}</span><b>{e.currency} · {e.title}</b><time>{dateTime(e.event_time)}</time><small>±{e.blackout_before_minutes}/{e.blackout_after_minutes}m</small></div>)}{!events.length&&<p className="research-empty small-empty">No manual events recorded.</p>}</div>
          </section>

          <section className="research-card">
            <div className="research-section-head"><div><small>PROCESS DISCIPLINE</small><h2>Trade-plan journal</h2></div><BookOpen size={18}/></div>
            <p className="muted-line">Record a planned setup before execution. This journal does not place or manage orders.</p>
            <form className="research-form" onSubmit={addJournal}>
              <div className="form-two">
                <label>Direction<select value={direction} onChange={e=>setDirection(e.target.value)}><option>LONG</option><option>SHORT</option></select></label>
                <label>Timeframe<select value={journalTf} onChange={e=>setJournalTf(e.target.value)}>{["M5","M15","M30","H1","H4","D1"].map(v=><option key={v}>{v}</option>)}</select></label>
              </div>
              <div className="form-two"><label>Entry<input type="number" step="any" min="0" value={entry} onChange={e=>setEntry(e.target.value)} required/></label><label>Stop<input type="number" step="any" min="0" value={stop} onChange={e=>setStop(e.target.value)} required/></label></div>
              <div className="form-two"><label>Target<input type="number" step="any" min="0" value={target} onChange={e=>setTarget(e.target.value)} required/></label><label>Risk % (max 1)<input type="number" min="0.01" max="1" step="0.01" value={journalRisk} onChange={e=>setJournalRisk(e.target.value)} required/></label></div>
              <label>Setup notes<textarea value={notes} onChange={e=>setNotes(e.target.value)} rows={3} maxLength={2000} placeholder="HTF bias, sweep, BOS, FVG, session, invalidation…"/></label>
              <button type="submit"><BookOpen size={14}/> Save research plan</button>
            </form>
            {journalMessage && <p className="form-message">{journalMessage}</p>}
            <div className="journal-list">{journal.slice(0,8).map(item=><div className="journal-row" key={item.id}><b>{item.symbol} · {item.direction} · {item.timeframe}</b><span>Entry {fmt(item.entry_price,5)} / SL {fmt(item.stop_price,5)} / TP {fmt(item.target_price,5)}</span><small>{item.risk_percent}% risk · {item.status} · {dateTime(item.created_at)}</small>{item.notes&&<p>{item.notes}</p>}</div>)}{!journal.length&&<p className="research-empty small-empty">Your research plans will appear here.</p>}</div>
          </section>
        </div>

        <footer className="research-footer"><span><ShieldCheck size={14}/> Read-only research terminal</span><span>FX is OTC · no consolidated global FX order book · no guaranteed edge</span></footer>
      </section>
    </main>
  );
}
