"""Multi-timeframe explainable FX research and conservative backtesting.

This module analyses observable broker candles only. It does not infer hidden orders,
dealer inventory, global FX volume, or guaranteed institutional positioning.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

RISK_LIMIT_PCT = 1.0
MIN_REWARD_RISK = 3.5


def _valid_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for bar in bars:
        try:
            values = [float(bar[k]) for k in ("open", "high", "low", "close")]
            ts = int(bar["time"])
            o, h, l, c = values
            if ts <= 0 or not all(isfinite(v) and v > 0 for v in values):
                continue
            if h < max(o, c, l) or l > min(o, c, h):
                continue
            clean.append({**bar, "time": ts, "open": o, "high": h, "low": l, "close": c})
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    clean.sort(key=lambda item: item["time"])
    deduped = {bar["time"]: bar for bar in clean}
    return [deduped[key] for key in sorted(deduped)]


def atr_value(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    tr: list[float] = []
    for i in range(len(bars) - period, len(bars)):
        bar, prev = bars[i], float(bars[i - 1]["close"])
        tr.append(max(bar["high"] - bar["low"], abs(bar["high"] - prev), abs(bar["low"] - prev)))
    return sum(tr) / len(tr)


def _confirmed_pivots(bars: list[dict[str, Any]], left: int = 2, right: int = 2) -> tuple[list[dict], list[dict]]:
    """Expose a pivot only after the configured number of later candles have closed."""
    highs: list[dict] = []
    lows: list[dict] = []
    for i in range(left, len(bars) - right):
        high, low = bars[i]["high"], bars[i]["low"]
        left_highs = [bars[j]["high"] for j in range(i - left, i)]
        right_highs = [bars[j]["high"] for j in range(i + 1, i + right + 1)]
        left_lows = [bars[j]["low"] for j in range(i - left, i)]
        right_lows = [bars[j]["low"] for j in range(i + 1, i + right + 1)]
        if high > max(left_highs) and high >= max(right_highs):
            highs.append({"index": i, "time": bars[i]["time"], "price": high})
        if low < min(left_lows) and low <= min(right_lows):
            lows.append({"index": i, "time": bars[i]["time"], "price": low})
    return highs, lows


def _equal_pools(pivots: list[dict], tolerance: float, kind: str) -> list[dict]:
    pools: list[dict] = []
    if tolerance <= 0:
        return pools
    for i, first in enumerate(pivots):
        for second in pivots[i + 1:]:
            if second["index"] - first["index"] < 3:
                continue
            if abs(first["price"] - second["price"]) <= tolerance:
                pools.append({
                    "kind": kind,
                    "price": round((first["price"] + second["price"]) / 2, 10),
                    "members": 2,
                    "first_time": first["time"],
                    "last_time": second["time"],
                    "tolerance": tolerance,
                })
    pools.sort(key=lambda item: item["last_time"], reverse=True)
    return pools[:3]


def _fvg_zones(bars: list[dict[str, Any]], atr: float) -> list[dict]:
    zones: list[dict] = []
    start = max(2, len(bars) - 35)
    for i in range(start, len(bars)):
        first, third = bars[i - 2], bars[i]
        if third["low"] > first["high"]:
            low, high, side = first["high"], third["low"], "bullish"
            future = bars[i + 1:]
            touched = any(item["low"] <= high for item in future)
            filled = any(item["low"] <= low for item in future)
        elif third["high"] < first["low"]:
            low, high, side = third["high"], first["low"], "bearish"
            future = bars[i + 1:]
            touched = any(item["high"] >= low for item in future)
            filled = any(item["high"] >= high for item in future)
        else:
            continue
        if atr and high - low < atr * 0.04:
            continue
        zones.append({
            "side": side, "low": round(low, 10), "high": round(high, 10),
            "mid": round((low + high) / 2, 10),
            "formed_at": third["time"],
            "status": "filled" if filled else ("mitigated" if touched else "unmitigated"),
        })
    return zones[-5:]


def _session_context(timestamp: int) -> dict[str, Any]:
    utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    london = utc.astimezone(ZoneInfo("Europe/London"))
    new_york = utc.astimezone(ZoneInfo("America/New_York"))
    london_kz = london.weekday() < 5 and 7 <= london.hour < 10
    new_york_kz = new_york.weekday() < 5 and 8 <= new_york.hour < 11
    asia = utc.weekday() < 5 and 0 <= utc.hour < 7
    labels = []
    if asia:
        labels.append("Asia")
    if london_kz:
        labels.append("London AM")
    if new_york_kz:
        labels.append("New York AM")
    if london_kz and new_york_kz:
        labels.append("London/New York overlap")
    return {
        "utc_time": utc.isoformat(),
        "london_time": london.isoformat(),
        "new_york_time": new_york.isoformat(),
        "labels": labels or ["Outside configured killzones"],
        "in_london_killzone": london_kz,
        "in_new_york_killzone": new_york_kz,
        "in_preferred_window": london_kz or new_york_kz,
        "note": "Session windows are research defaults; daylight-saving changes use IANA time zones.",
    }


def _analyse_timeframe(raw_bars: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    bars = _valid_bars(raw_bars)
    empty = {
        "timeframe": timeframe, "status": "insufficient_data", "bars": len(bars),
        "trend": "unknown", "structure": "unavailable", "bos": None, "choch": None,
        "sweep": None, "displacement": None, "atr": None, "dealing_range": None,
        "fvg_zones": [], "equal_highs": [], "equal_lows": [], "ote": None,
    }
    if len(bars) < 45:
        return empty
    last = bars[-1]
    close, atr = last["close"], atr_value(bars)
    if atr <= 0:
        return {**empty, "status": "invalid_volatility_baseline"}
    highs, lows = _confirmed_pivots(bars)
    if len(highs) < 2 or len(lows) < 2:
        return {**empty, "status": "insufficient_confirmed_swings", "bars": len(bars), "atr": atr}
    h1, h2, l1, l2 = highs[-2], highs[-1], lows[-2], lows[-1]
    higher_high, higher_low = h2["price"] > h1["price"], l2["price"] > l1["price"]
    lower_high, lower_low = h2["price"] < h1["price"], l2["price"] < l1["price"]
    trend = "bullish" if higher_high and higher_low else ("bearish" if lower_high and lower_low else "range/transition")
    bos_up, bos_down = close > h2["price"], close < l2["price"]
    bos = "bullish" if bos_up else ("bearish" if bos_down else None)
    choch = "bullish" if trend == "bearish" and bos_up else ("bearish" if trend == "bullish" and bos_down else None)

    prior_highs = [p for p in highs if len(bars) - 35 <= p["index"] < len(bars) - 2]
    prior_lows = [p for p in lows if len(bars) - 35 <= p["index"] < len(bars) - 2]
    swept_high = max(prior_highs, key=lambda p: p["index"]) if prior_highs else None
    swept_low = max(prior_lows, key=lambda p: p["index"]) if prior_lows else None
    bullish_sweep = bool(swept_low and last["low"] < swept_low["price"] and close > swept_low["price"])
    bearish_sweep = bool(swept_high and last["high"] > swept_high["price"] and close < swept_high["price"])
    sweep = None
    if bullish_sweep != bearish_sweep:
        ref = swept_low if bullish_sweep else swept_high
        sweep = {
            "side": "bullish" if bullish_sweep else "bearish",
            "level": ref["price"], "wick_extreme": last["low"] if bullish_sweep else last["high"],
            "bar_time": last["time"], "reference_time": ref["time"],
            "description": "Broker-candle sweep and close back through a confirmed swing; not proof of resting orders.",
        }

    candle_range = max(last["high"] - last["low"], 1e-12)
    body = abs(last["close"] - last["open"])
    displacement_side = "bullish" if last["close"] > last["open"] else ("bearish" if last["close"] < last["open"] else None)
    displacement = {
        "side": displacement_side,
        "range_atr": round(candle_range / atr, 3),
        "body_ratio": round(body / candle_range, 3),
        "qualified": bool(displacement_side and candle_range >= 1.2 * atr and body / candle_range >= 0.60),
    }

    range_bars = bars[-51:-1]
    range_high = max(b["high"] for b in range_bars) if range_bars else last["high"]
    range_low = min(b["low"] for b in range_bars) if range_bars else last["low"]
    midpoint = (range_high + range_low) / 2
    location = "premium" if close > midpoint else ("discount" if close < midpoint else "equilibrium")

    ote = None
    if trend == "bullish" and l2["index"] < h2["index"]:
        leg_low, leg_high = l2["price"], h2["price"]
        width = leg_high - leg_low
        if width > 0:
            zone_low, zone_high = leg_high - width * 0.79, leg_high - width * 0.62
            ote = {"side": "bullish", "low": round(zone_low, 10), "high": round(zone_high, 10),
                   "in_zone": zone_low <= close <= zone_high, "basis": "last confirmed swing leg"}
    elif trend == "bearish" and h2["index"] < l2["index"]:
        leg_high, leg_low = h2["price"], l2["price"]
        width = leg_high - leg_low
        if width > 0:
            zone_low, zone_high = leg_low + width * 0.62, leg_low + width * 0.79
            ote = {"side": "bearish", "low": round(zone_low, 10), "high": round(zone_high, 10),
                   "in_zone": zone_low <= close <= zone_high, "basis": "last confirmed swing leg"}

    zones = _fvg_zones(bars, atr)
    active_zones = [z for z in zones if z["status"] != "filled"]
    tolerance = atr * 0.10
    return {
        "timeframe": timeframe, "status": "ready", "bars": len(bars),
        "last_closed_candle_time": last["time"], "close": close, "atr": round(atr, 10),
        "trend": trend,
        "structure": {
            "last_two_swing_highs": [h1, h2], "last_two_swing_lows": [l1, l2],
            "higher_high": higher_high, "higher_low": higher_low,
            "lower_high": lower_high, "lower_low": lower_low,
        },
        "bos": bos, "choch": choch, "sweep": sweep, "displacement": displacement,
        "dealing_range": {
            "high": round(range_high, 10), "low": round(range_low, 10),
            "equilibrium": round(midpoint, 10), "location": location, "lookback": len(range_bars),
        },
        "ote": ote, "fvg_zones": zones,
        "nearest_active_fvg": active_zones[-1] if active_zones else None,
        "equal_highs": _equal_pools(highs[-12:], tolerance, "buy-side"),
        "equal_lows": _equal_pools(lows[-12:], tolerance, "sell-side"),
        "liquidity_note": "Equal highs/lows and swing extremes are visible price references, not confirmed stop orders.",
    }


def _weighted_htf_bias(contexts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    weights = {"D1": 3, "H4": 2, "H1": 1}
    points = {"bullish": 0, "bearish": 0}
    used: list[str] = []
    for timeframe, weight in weights.items():
        context = contexts.get(timeframe)
        if not context or context.get("status") != "ready":
            continue
        trend = context.get("trend")
        if trend in points:
            points[trend] += weight
            used.append(timeframe)
    if len(used) < 2:
        bias = "insufficient_data"
    elif points["bullish"] > points["bearish"]:
        bias = "bullish"
    elif points["bearish"] > points["bullish"]:
        bias = "bearish"
    else:
        bias = "mixed"
    return {"bias": bias, "weighted_votes": points, "timeframes_used": used, "weights": weights,
            "note": "D1/H4/H1 structure votes; transparent heuristic, not a predictive model."}


def analyse_institutional(
    bars_by_timeframe: dict[str, list[dict[str, Any]]],
    symbol: str,
    spread_points: float | None,
    point: float | None,
    feed_freshness: dict[str, bool] | None = None,
    news_events: list[dict[str, Any]] | None = None,
    now_timestamp: int | None = None,
) -> dict[str, Any]:
    contexts = {
        tf: _analyse_timeframe(bars_by_timeframe.get(tf, []), tf)
        for tf in ("D1", "H4", "H1", "M30", "M15", "M5")
        if bars_by_timeframe.get(tf)
    }
    bias = _weighted_htf_bias(contexts)
    ltf = next((contexts[tf] for tf in ("M15", "M5", "M30")
                 if tf in contexts and contexts[tf].get("status") == "ready"), None)
    if not ltf:
        ltf = next((contexts[tf] for tf in ("M15", "M5", "M30") if tf in contexts), None)
    if not ltf:
        ltf = {"status": "no_lower_timeframe"}
    freshness = feed_freshness or {}
    stale_frames = [tf for tf, fresh in freshness.items() if not fresh]
    spread_ratio = None
    if ltf.get("status") == "ready" and spread_points is not None and point and point > 0:
        spread_ratio = (float(spread_points) * float(point)) / float(ltf["atr"])

    timestamp = now_timestamp or int(datetime.now(timezone.utc).timestamp())
    session = _session_context(timestamp)
    active_news = []
    normalized_symbol = "".join(char for char in symbol.upper() if char.isalpha())
    relevant_currencies = {normalized_symbol[:3], normalized_symbol[3:6]} if len(normalized_symbol) >= 6 else set()
    if "DXY" in normalized_symbol:
        relevant_currencies = {"USD"}
    for event in news_events or []:
        try:
            event_time = int(event["event_time"] if "event_time" in event else event["timestamp"])
            before = max(0, int(event.get("blackout_before_minutes", 15)))
            after = max(0, int(event.get("blackout_after_minutes", 15)))
            currency = str(event.get("currency", "ALL")).upper()
            impact = str(event.get("impact", "high")).lower()
            relevant = currency == "ALL" or not relevant_currencies or currency in relevant_currencies
            if impact == "high" and relevant and event_time - before * 60 <= timestamp <= event_time + after * 60:
                active_news.append({
                    "title": str(event.get("title", "High-impact event"))[:100],
                    "currency": currency[:8], "impact": impact, "timestamp": event_time,
                })
        except (KeyError, TypeError, ValueError):
            continue

    direction = (ltf.get("sweep") or {}).get("side")
    displacement = ltf.get("displacement") or {}
    bos = ltf.get("bos")
    checks = {
        "liquidity_sweep": bool(direction),
        "directional_displacement": bool(displacement.get("qualified") and displacement.get("side") == direction),
        "structure_break": bos == direction,
        "higher_timeframe_alignment": bias.get("bias") == direction,
        "fresh_quotes": not stale_frames and bool(freshness),
        "spread_filter": spread_ratio is not None and spread_ratio <= 0.15,
        "preferred_session": session["in_preferred_window"],
        "unmitigated_fvg": bool((ltf.get("nearest_active_fvg") or {}).get("status") == "unmitigated"),
        "ote_confluence": bool((ltf.get("ote") or {}).get("side") == direction and (ltf.get("ote") or {}).get("in_zone")),
        "no_high_impact_news_blackout": not active_news,
    }
    blockers = [key for key, passed in checks.items() if not passed]
    hard_blocks = [key for key in blockers if key in {
        "fresh_quotes", "spread_filter", "no_high_impact_news_blackout",
        "higher_timeframe_alignment", "liquidity_sweep", "directional_displacement", "structure_break",
    }]
    if ltf.get("status") != "ready":
        decision = "DATA INSUFFICIENT"
    elif hard_blocks:
        decision = "STAND ASIDE"
    elif blockers:
        decision = "CONFLUENCE INCOMPLETE"
    else:
        decision = "WATCH " + str(direction).upper()
    passed_count = sum(1 for passed in checks.values() if passed)
    return {
        "symbol": symbol, "data_mode": "broker_candles_only", "decision": decision,
        "direction": direction, "evidence_score": round(100 * passed_count / len(checks)),
        "score_type": "confluence checklist completion; not win probability",
        "risk_framework": {"max_risk_percent": RISK_LIMIT_PCT, "minimum_reward_risk": MIN_REWARD_RISK},
        "higher_timeframe_bias": bias, "lower_timeframe": ltf, "timeframes": contexts,
        "session": session, "spread_to_atr": round(spread_ratio, 4) if spread_ratio is not None else None,
        "active_news_blackouts": active_news, "checks": checks, "blockers": blockers,
        "stale_timeframes": stale_frames,
        "limitations": [
            "FX is OTC; this feed does not reveal the complete global interbank order book, dealer inventory or every participant's stops.",
            "A candle sweep is a price-action observation, not proof that stop orders rested at that level.",
            "FVG, OTE and liquidity-pool labels are rule-based chart interpretations requiring out-of-sample validation.",
            "No trade is sent. This report is research context, not a guaranteed signal or financial advice.",
        ],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def calculate_position_size(
    account_equity: float,
    risk_percent: float,
    stop_distance_pips: float,
    pip_value_per_standard_lot: float,
    lot_step: float = 0.01,
) -> dict[str, float]:
    values = (account_equity, risk_percent, stop_distance_pips, pip_value_per_standard_lot, lot_step)
    if not all(isfinite(float(v)) for v in values):
        raise ValueError("All risk inputs must be finite numbers")
    if account_equity <= 0 or stop_distance_pips <= 0 or pip_value_per_standard_lot <= 0 or lot_step <= 0:
        raise ValueError("Equity, stop distance, pip value and lot step must be greater than zero")
    if not 0 < risk_percent <= RISK_LIMIT_PCT:
        raise ValueError("Risk must be greater than zero and no more than 1% per trade")
    risk_amount = account_equity * risk_percent / 100
    raw_lots = risk_amount / (stop_distance_pips * pip_value_per_standard_lot)
    lots = int(raw_lots / lot_step) * lot_step
    return {
        "risk_amount": round(risk_amount, 2), "raw_lots": round(raw_lots, 6),
        "rounded_lots": round(lots, 4), "below_broker_minimum_lot_step": lots < lot_step,
        "risk_percent": risk_percent,
        "stop_distance_pips": stop_distance_pips,
        "pip_value_per_standard_lot": pip_value_per_standard_lot,
        "minimum_target_distance_pips": round(stop_distance_pips * MIN_REWARD_RISK, 2),
        "minimum_reward_risk": MIN_REWARD_RISK,
        "estimated_loss_at_stop": round(lots * stop_distance_pips * pip_value_per_standard_lot, 2),
    }


def backtest_sweep_continuation(
    raw_bars: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
    spread_price: float = 0.0,
    slippage_price: float = 0.0,
    target_r: float = MIN_REWARD_RISK,
    max_holding_bars: int = 48,
) -> dict[str, Any]:
    """Next-bar-open single-position simulation; stop wins any same-candle collision."""
    bars = _valid_bars(raw_bars)
    if target_r < MIN_REWARD_RISK:
        raise ValueError("Backtest target must be at least 3.5R")
    if spread_price < 0 or slippage_price < 0 or max_holding_bars < 1:
        raise ValueError("Costs must be non-negative and holding window positive")
    trades: list[dict[str, Any]] = []
    i = 40
    while i < len(bars) - 1:
        context = _analyse_timeframe(bars[:i + 1], timeframe)
        sweep = context.get("sweep")
        displacement = context.get("displacement") or {}
        direction = (sweep or {}).get("side")
        if context.get("status") != "ready" or not direction:
            i += 1
            continue
        if not displacement.get("qualified") or displacement.get("side") != direction or context.get("bos") != direction:
            i += 1
            continue
        next_bar = bars[i + 1]
        is_long = direction == "bullish"
        entry = next_bar["open"]
        stop = float(sweep["wick_extreme"])
        risk = entry - stop if is_long else stop - entry
        if risk <= 0:
            i += 1
            continue
        target = entry + target_r * risk if is_long else entry - target_r * risk
        exit_index = min(i + max_holding_bars, len(bars) - 1)
        exit_price, outcome = bars[exit_index]["close"], "time_exit"
        for j in range(i + 1, exit_index + 1):
            bar = bars[j]
            stop_hit = bar["low"] <= stop if is_long else bar["high"] >= stop
            target_hit = bar["high"] >= target if is_long else bar["low"] <= target
            if stop_hit:
                exit_price, exit_index, outcome = stop, j, "loss"
                break
            if target_hit:
                exit_price, exit_index, outcome = target, j, "win"
                break
        gross_r = ((exit_price - entry) if is_long else (entry - exit_price)) / risk
        net_r = gross_r - (spread_price + slippage_price) / risk
        trades.append({
            "direction": direction, "entry_time": next_bar["time"], "exit_time": bars[exit_index]["time"],
            "entry": round(entry, 10), "stop": round(stop, 10), "target": round(target, 10),
            "exit": round(exit_price, 10), "outcome": outcome,
            "gross_r": round(gross_r, 3), "net_r": round(net_r, 3),
        })
        i = exit_index + 1

    wins = [t["net_r"] for t in trades if t["net_r"] > 0]
    losses = [t["net_r"] for t in trades if t["net_r"] < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    equity = peak = max_drawdown = 0.0
    for trade in trades:
        equity += trade["net_r"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    count = len(trades)
    return {
        "symbol": symbol, "timeframe": timeframe, "bars": len(bars), "trades": count,
        "wins": len(wins), "losses": len(losses),
        "win_rate_percent": round(100 * len(wins) / count, 2) if count else None,
        "expectancy_r": round(sum(t["net_r"] for t in trades) / count, 3) if count else None,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else (None if not wins else "infinite"),
        "max_drawdown_r": round(max_drawdown, 3),
        "average_win_r": round(sum(wins) / len(wins), 3) if wins else None,
        "average_loss_r": round(sum(losses) / len(losses), 3) if losses else None,
        "target_r": target_r, "spread_price": spread_price, "slippage_price": slippage_price,
        "execution_model": "next bar open; stop first on same-bar collision; one position at a time",
        "trades_detail": trades[-100:],
        "warning": "Fixed-rule historical simulation only. The later 30% is a chronological holdout from the same feed, not independent validation. It excludes commissions, swaps, latency, liquidity gaps and parameter-selection bias. Test on unseen data and realistic broker costs before relying on it.",
    }
