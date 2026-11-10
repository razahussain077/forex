"""Explainable broker-candle market structure research. Scores are evidence weights, not probabilities."""
from statistics import median
from typing import Any

def atr_value(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    ranges = []
    for i in range(len(bars) - period, len(bars)):
        bar, previous_close = bars[i], float(bars[i - 1]["close"])
        ranges.append(max(
            float(bar["high"]) - float(bar["low"]),
            abs(float(bar["high"]) - previous_close),
            abs(float(bar["low"]) - previous_close),
        ))
    return sum(ranges) / len(ranges)

def analyze_market(
    bars: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
    spread_points: float | None = None,
    point: float | None = None,
) -> dict[str, Any]:
    result = {
        "symbol": symbol, "timeframe": timeframe, "side": "NO TRADE",
        "score": 0, "reason": "No qualifying multi-factor setup is present.",
        "evidence": [], "invalidation": None, "atr": None,
        "spread_points": spread_points, "spread_to_atr": None,
        "engine": "swiftlabor-rules-v0.3",
    }
    if len(bars) < 35:
        result["reason"] = "Insufficient completed candles; at least 35 are required."
        return result

    last = bars[-1]
    prior_20 = bars[-21:-1]
    close, open_, high, low = (float(last[name]) for name in ("close", "open", "high", "low"))
    prior_high = max(float(bar["high"]) for bar in prior_20)
    prior_low = min(float(bar["low"]) for bar in prior_20)
    atr = atr_value(bars)
    result["atr"] = atr or None
    if atr <= 0:
        result["reason"] = "Volatility baseline is unavailable; no setup is published."
        return result

    if spread_points is not None and point is not None and point > 0:
        spread_price = spread_points * point
        spread_ratio = spread_price / atr
        result["spread_to_atr"] = round(spread_ratio, 4)
        if spread_ratio > 0.20:
            result["reason"] = "Broker spread is elevated relative to ATR; setup filtered out."
            result["evidence"] = ["Spread filter blocked the setup (spread exceeds 20% of ATR)."]
            return result

    bullish_sweep = low < prior_low and close > prior_low
    bearish_sweep = high > prior_high and close < prior_high
    if bullish_sweep == bearish_sweep:
        result["reason"] = "No single-sided 20-bar liquidity sweep and reclaim is present."
        result["evidence"] = ["No qualifying buy-side or sell-side sweep candidate."]
        return result

    direction = "BUY" if bullish_sweep else "SELL"
    micro_range = bars[-9:-1]
    micro_high = max(float(bar["high"]) for bar in micro_range)
    micro_low = min(float(bar["low"]) for bar in micro_range)
    structure_break = close > micro_high if direction == "BUY" else close < micro_low

    body = abs(close - open_)
    recent_bodies = [abs(float(bar["close"]) - float(bar["open"])) for bar in bars[-12:-1]]
    baseline_body = median(recent_bodies) if recent_bodies else 0.0
    directional_candle = close > open_ if direction == "BUY" else close < open_
    displacement = directional_candle and body >= max(atr * 0.65, baseline_body * 1.5)

    imbalance = low > float(bars[-3]["high"]) if direction == "BUY" else high < float(bars[-3]["low"])
    evidence = [
        ("Sell-side" if direction == "BUY" else "Buy-side")
        + " liquidity sweep candidate: price traded beyond the prior 20-bar extreme and closed back inside."
    ]
    score = 35
    if displacement:
        score += 25
        evidence.append("Directional displacement: candle body expanded relative to ATR and recent candle bodies.")
    if structure_break:
        score += 25
        evidence.append("Short-term structure break: close cleared the prior eight-candle range.")
    if imbalance:
        score += 15
        evidence.append("Three-candle imbalance candidate detected; this is not proof of institutional orders.")
    result.update({
        "score": score,
        "evidence": evidence,
        "invalidation": low if direction == "BUY" else high,
        "setup_direction": direction,
    })
    if not displacement or not structure_break:
        result["reason"] = "Sweep candidate found, but displacement and structure confirmation are incomplete. Stand aside."
        return result
    result.update({
        "side": f"WATCH {direction}",
        "reason": "Sweep, directional displacement and short-term structure break align. Research watch only; wait for a separately defined retest/trigger.",
    })
    return result

