"""Reference FX data ingestion and synchronized cross-asset analytics.

OANDA candles use midpoint OHLC plus optional bid/ask closes. Massive forex bars
are quote-derived aggregates (not consolidated traded volume). DXY is sourced only from
the explicitly configured broker MT5 symbol. Missing/stale inputs fail closed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger("swiftlabor.reference_data")

M5_SECONDS = 300
DEFAULT_PAIRS = ("EUR_USD", "GBP_USD")
MAX_CANDLES = min(5000, max(100, int(os.getenv("REFERENCE_CANDLE_COUNT", "2500"))))
POLL_SECONDS = max(30, int(os.getenv("REFERENCE_POLL_SECONDS", "60")))
MAX_AGE_SECONDS = max(600, int(os.getenv("MAX_REFERENCE_AGE_SECONDS", "900")))


@dataclass(frozen=True)
class ReferenceCandle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    bid_close: float | None = None
    ask_close: float | None = None
    quote_count: int | None = None
    price_basis: str = "mid"


def _parse_time(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _valid_candle(candle: ReferenceCandle) -> bool:
    values = (candle.open, candle.high, candle.low, candle.close)
    return (
        candle.timestamp > 0
        and all(math.isfinite(v) and v > 0 for v in values)
        and candle.high >= max(candle.open, candle.close, candle.low)
        and candle.low <= min(candle.open, candle.close, candle.high)
    )


def _oanda_settings() -> tuple[str, str] | None:
    token = os.getenv("OANDA_TOKEN", "").strip()
    if not token:
        return None
    environment = os.getenv("OANDA_ENV", "practice").strip().lower()
    if environment not in {"practice", "live"}:
        raise RuntimeError("OANDA_ENV must be 'practice' or 'live'")
    base = (
        "https://api-fxpractice.oanda.com"
        if environment == "practice"
        else "https://api-fxtrade.oanda.com"
    )
    return token, os.getenv("OANDA_BASE_URL", base).rstrip("/")


async def fetch_oanda_pair(
    client: httpx.AsyncClient, instrument: str
) -> list[ReferenceCandle]:
    settings = _oanda_settings()
    if not settings:
        raise RuntimeError("OANDA_TOKEN is not configured")
    token, base_url = settings
    response = await client.get(
        f"{base_url}/v3/instruments/{instrument}/candles",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={"granularity": "M5", "count": MAX_CANDLES, "price": "MBA", "smooth": "false"},
        timeout=httpx.Timeout(15.0, connect=5.0),
    )
    response.raise_for_status()
    return parse_oanda_payload(response.json())


def parse_oanda_payload(payload: dict[str, Any]) -> list[ReferenceCandle]:
    output: list[ReferenceCandle] = []
    for raw in payload.get("candles", []):
        if not raw.get("complete"):
            continue
        mid = raw.get("mid") or {}
        bid = raw.get("bid") or {}
        ask = raw.get("ask") or {}
        try:
            candle = ReferenceCandle(
                timestamp=_parse_time(raw["time"]),
                open=float(mid["o"]), high=float(mid["h"]),
                low=float(mid["l"]), close=float(mid["c"]),
                bid_close=float(bid["c"]) if bid.get("c") is not None else None,
                ask_close=float(ask["c"]) if ask.get("c") is not None else None,
                price_basis="mid",
            )
        except (KeyError, TypeError, ValueError):
            continue
        if _valid_candle(candle):
            output.append(candle)
    return sorted(output, key=lambda item: item.timestamp)


async def fetch_massive_pair(
    client: httpx.AsyncClient, pair: str
) -> list[ReferenceCandle]:
    """Fetch 5-minute quote-derived bid bars from Massive (formerly Polygon)."""
    api_key = (os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("MASSIVE_API_KEY is not configured")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=8)
    ticker = "C:" + pair.replace("_", "")
    base_url = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
    url = (
        f"{base_url}/v2/aggs/ticker/{ticker}/range/5/minute/"
        f"{int(start.timestamp() * 1000)}/{int(end.timestamp() * 1000)}"
    )
    response = await client.get(
        url, params={"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": api_key},
        timeout=httpx.Timeout(20.0, connect=5.0),
    )
    response.raise_for_status()
    return parse_massive_payload(response.json())


def parse_massive_payload(payload: dict[str, Any], now_ts: int | None = None) -> list[ReferenceCandle]:
    now_value = now_ts or int(datetime.now(timezone.utc).timestamp())
    output: list[ReferenceCandle] = []
    for raw in payload.get("results", []):
        try:
            timestamp = int(raw["t"]) // 1000
            candle = ReferenceCandle(
                timestamp=timestamp,
                open=float(raw["o"]), high=float(raw["h"]),
                low=float(raw["l"]), close=float(raw["c"]),
                quote_count=int(raw["n"]) if raw.get("n") is not None else None,
                price_basis="quote_aggregate",
            )
        except (KeyError, TypeError, ValueError):
            continue
        # The REST aggregate endpoint may include a currently updating bar.
        if timestamp + M5_SECONDS <= now_value and _valid_candle(candle):
            output.append(candle)
    return sorted(output, key=lambda item: item.timestamp)


async def init_reference_schema(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reference_candles (
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                candle_time BIGINT NOT NULL,
                open DOUBLE PRECISION NOT NULL,
                high DOUBLE PRECISION NOT NULL,
                low DOUBLE PRECISION NOT NULL,
                close DOUBLE PRECISION NOT NULL,
                bid_close DOUBLE PRECISION,
                ask_close DOUBLE PRECISION,
                quote_count BIGINT,
                price_basis TEXT NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (provider, symbol, timeframe, candle_time)
            );
            CREATE INDEX IF NOT EXISTS reference_candles_lookup_idx
                ON reference_candles (provider, symbol, timeframe, candle_time DESC);
            CREATE TABLE IF NOT EXISTS reference_feed_status (
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                configured BOOLEAN NOT NULL DEFAULT FALSE,
                last_success_at TIMESTAMPTZ,
                last_candle_time BIGINT,
                last_error TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (provider, symbol, timeframe)
            );
        """)


async def save_reference_candles(
    pool: Any, provider: str, symbol: str, candles: list[ReferenceCandle]
) -> None:
    if not candles:
        raise RuntimeError("Provider returned no completed candles")
    rows = [
        (provider, symbol, "M5", c.timestamp, c.open, c.high, c.low, c.close,
         c.bid_close, c.ask_close, c.quote_count, c.price_basis)
        for c in candles
    ]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany("""
                INSERT INTO reference_candles (
                    provider,symbol,timeframe,candle_time,open,high,low,close,
                    bid_close,ask_close,quote_count,price_basis
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (provider,symbol,timeframe,candle_time) DO UPDATE SET
                    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                    close=EXCLUDED.close, bid_close=EXCLUDED.bid_close,
                    ask_close=EXCLUDED.ask_close, quote_count=EXCLUDED.quote_count,
                    price_basis=EXCLUDED.price_basis, received_at=NOW()
            """, rows)
            await conn.execute("""
                INSERT INTO reference_feed_status
                    (provider,symbol,timeframe,configured,last_success_at,last_candle_time,last_error,updated_at)
                VALUES ($1,$2,'M5',TRUE,NOW(),$3,NULL,NOW())
                ON CONFLICT (provider,symbol,timeframe) DO UPDATE SET
                    configured=TRUE,last_success_at=NOW(),
                    last_candle_time=EXCLUDED.last_candle_time,last_error=NULL,updated_at=NOW()
            """, provider, symbol, max(c.timestamp for c in candles))


async def set_feed_status(
    pool: Any, provider: str, symbol: str, configured: bool, error: str | None
) -> None:
    safe_error = (error or "")[:500] or None
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO reference_feed_status
                (provider,symbol,timeframe,configured,last_error,updated_at)
            VALUES ($1,$2,'M5',$3,$4,NOW())
            ON CONFLICT (provider,symbol,timeframe) DO UPDATE SET
                configured=EXCLUDED.configured,last_error=EXCLUDED.last_error,updated_at=NOW()
        """, provider, symbol, configured, safe_error)


def selected_provider() -> str:
    requested = os.getenv("REFERENCE_PROVIDER", "auto").strip().lower()
    if requested not in {"auto", "oanda", "massive", "mt5"}:
        raise RuntimeError("REFERENCE_PROVIDER must be auto, oanda, massive or mt5")
    if requested == "auto":
        if os.getenv("OANDA_TOKEN"):
            return "oanda"
        if os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY"):
            return "massive"
        if all(os.getenv(key, "").strip() for key in ("EURUSD_MT5_SYMBOL", "GBPUSD_MT5_SYMBOL", "DXY_MT5_SYMBOL")):
            return "mt5"
        return "none"
    return requested


async def sync_reference_once(pool: Any) -> dict[str, Any]:
    provider = selected_provider()
    if provider == "none":
        raise RuntimeError("Set OANDA_TOKEN, MASSIVE_API_KEY or POLYGON_API_KEY before syncing reference data")

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        for pair in DEFAULT_PAIRS:
            try:
                candles = (
                    await fetch_oanda_pair(client, pair)
                    if provider == "oanda"
                    else await fetch_massive_pair(client, pair)
                )
                await save_reference_candles(pool, provider, pair, candles)
                results.append({
                    "instrument": pair,
                    "status": "ok",
                    "candles_written": len(candles),
                    "last_candle_time": max(c.timestamp for c in candles),
                })
            except Exception as exc:
                log.warning("%s %s manual/background sync failed: %s", provider, pair, str(exc)[:300])
                await set_feed_status(pool, provider, pair, True, str(exc))
                results.append({"instrument": pair, "status": "error", "error": str(exc)[:300]})

    if not any(item["status"] == "ok" for item in results):
        raise RuntimeError("No reference instruments synced successfully")
    return {
        "provider": provider,
        "results": results,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


async def reference_ingestion_loop(pool: Any) -> None:
    """Poll the selected external provider. Broker MT5 remains the DXY source."""
    while True:
        try:
            provider = selected_provider()
            if provider == "none":
                for pair in DEFAULT_PAIRS:
                    await set_feed_status(
                        pool, "oanda", pair, False,
                        "Set OANDA_TOKEN / MASSIVE_API_KEY or configure all three MT5 symbols"
                    )
            elif provider != "mt5":
                await sync_reference_once(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reference ingestion cycle failed; loop will retry")
        await asyncio.sleep(POLL_SECONDS)


def _pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    vx, vy = sum(v * v for v in dx), sum(v * v for v in dy)
    if vx <= 0 or vy <= 0:
        return None
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(dx, dy)) / math.sqrt(vx * vy)))


def _bucket_series(m5: dict[int, float], minutes: int) -> dict[int, float]:
    bars_per_bucket = minutes // 5
    seconds = minutes * 60
    buckets: dict[int, dict[int, float]] = {}
    for timestamp, close in m5.items():
        start = (timestamp // seconds) * seconds
        buckets.setdefault(start, {})[timestamp] = close
    output: dict[int, float] = {}
    for start, values in buckets.items():
        expected = [start + i * M5_SECONDS for i in range(bars_per_bucket)]
        if all(ts in values for ts in expected):
            output[start] = values[expected[-1]]
    return output


def _correlation_pair(
    a: dict[int, float], b: dict[int, float], interval_seconds: int, window: int
) -> tuple[float | None, int]:
    common = sorted(set(a).intersection(b))
    returns_a: list[float] = []
    returns_b: list[float] = []
    for previous, current in zip(common, common[1:]):
        if current - previous != interval_seconds:
            continue
        if a[previous] <= 0 or a[current] <= 0 or b[previous] <= 0 or b[current] <= 0:
            continue
        returns_a.append(math.log(a[current] / a[previous]))
        returns_b.append(math.log(b[current] / b[previous]))
    if len(returns_a) < window:
        return None, len(returns_a)
    return _pearson(returns_a[-window:], returns_b[-window:]), len(returns_a)


def _frame_metrics(series: dict[str, dict[int, float]], minutes: int) -> dict[str, Any]:
    interval = minutes * 60
    current = {name: values if minutes == 5 else _bucket_series(values, minutes)
               for name, values in series.items()}
    pairs = {
        "EURUSD_vs_DXY": ("EUR_USD", "DXY"),
        "GBPUSD_vs_DXY": ("GBP_USD", "DXY"),
        "EURUSD_vs_GBPUSD": ("EUR_USD", "GBP_USD"),
    }
    result: dict[str, Any] = {"timeframe": f"M{minutes}" if minutes < 60 else "H1", "pairs": {}}
    for label, (left, right) in pairs.items():
        values: dict[str, Any] = {}
        for window in (20, 60):
            coefficient, samples = _correlation_pair(current[left], current[right], interval, window)
            values[str(window)] = round(coefficient, 4) if coefficient is not None else None
            values[f"samples_{window}"] = samples
        result["pairs"][label] = values
    return result


async def correlation_snapshot(pool: Any, tenant_id: str) -> dict[str, Any]:
    """Build correlations from either an external FX reference provider + MT5 DXY,
    or all three exact instruments streamed by the same Equiti MT5 terminal.
    Only completed M5 bars are compared; each MT5 feed must also have fresh quotes.
    """
    provider = selected_provider()
    symbols = {
        "EUR_USD": os.getenv("EURUSD_MT5_SYMBOL", "").strip(),
        "GBP_USD": os.getenv("GBPUSD_MT5_SYMBOL", "").strip(),
        "DXY": os.getenv("DXY_MT5_SYMBOL", "").strip(),
    }
    now = int(datetime.now(timezone.utc).timestamp())
    max_quote_age = max(60, int(os.getenv("MAX_QUOTE_AGE_SECONDS", "300")))
    stale_after = max(30, int(os.getenv("STALE_AFTER_SECONDS", "180")))

    series: dict[str, dict[int, float]] = {"EUR_USD": {}, "GBP_USD": {}, "DXY": {}}
    feeds: dict[str, dict[str, Any]] = {}
    external_status_rows: list[Any] = []

    async with pool.acquire() as conn:
        if provider == "mt5":
            # MT5 stores forming bars too. Exclude the current M5 bucket so
            # correlations use fully completed, time-aligned observations only.
            for key, symbol in symbols.items():
                rows = []
                stream = None
                if symbol:
                    rows = await conn.fetch("""
                        SELECT candle_time,close
                        FROM market_candles
                        WHERE tenant_id=$1 AND symbol=$2 AND timeframe='M5'
                          AND candle_time < (EXTRACT(EPOCH FROM NOW())::BIGINT / 300) * 300
                        ORDER BY candle_time DESC LIMIT 2500
                    """, tenant_id, symbol)
                    stream = await conn.fetchrow("""
                        SELECT quote_time,
                               EXTRACT(EPOCH FROM received_at)::BIGINT AS received_at_epoch
                        FROM market_streams
                        WHERE tenant_id=$1 AND symbol=$2 AND timeframe='M5'
                    """, tenant_id, symbol)
                for row in rows:
                    series[key][int(row["candle_time"])] = float(row["close"])
                latest = max(series[key]) if series[key] else None
                candle_age = max(0, now - latest) if latest else None
                quote_age = max(0, now - int(stream["quote_time"])) if stream else None
                receive_age = max(0, now - int(stream["received_at_epoch"])) if stream else None
                quote_fresh = bool(
                    stream
                    and quote_age is not None and quote_age <= max_quote_age
                    and receive_age is not None and receive_age <= stale_after
                )
                feeds[key] = {
                    "provider": "Equiti MT5",
                    "symbol": symbol or None,
                    "configured": bool(symbol),
                    "candles": len(series[key]),
                    "last_candle_time": latest,
                    "age_seconds": candle_age,
                    "quote_age_seconds": quote_age,
                    "receive_age_seconds": receive_age,
                    "fresh": bool(candle_age is not None and candle_age <= MAX_AGE_SECONDS and quote_fresh),
                }
        else:
            reference_rows = []
            if provider in {"oanda", "massive"}:
                reference_rows = await conn.fetch("""
                    SELECT symbol,candle_time,close,price_basis
                    FROM reference_candles
                    WHERE provider=$1 AND timeframe='M5'
                      AND symbol IN ('EUR_USD','GBP_USD')
                    ORDER BY candle_time DESC LIMIT 10000
                """, provider)
                external_status_rows = await conn.fetch("""
                    SELECT provider,symbol,configured,last_success_at,last_candle_time,last_error
                    FROM reference_feed_status
                    WHERE timeframe='M5' AND provider=$1
                    ORDER BY provider,symbol
                """, provider)
            for row in reference_rows:
                key = row["symbol"]
                if key in ("EUR_USD", "GBP_USD"):
                    series[key][int(row["candle_time"])] = float(row["close"])

            dxy_symbol = symbols["DXY"]
            dxy_stream = None
            dxy_rows = []
            if dxy_symbol:
                dxy_rows = await conn.fetch("""
                    SELECT candle_time,close
                    FROM market_candles
                    WHERE tenant_id=$1 AND symbol=$2 AND timeframe='M5'
                      AND candle_time < (EXTRACT(EPOCH FROM NOW())::BIGINT / 300) * 300
                    ORDER BY candle_time DESC LIMIT 2500
                """, tenant_id, dxy_symbol)
                dxy_stream = await conn.fetchrow("""
                    SELECT quote_time,
                           EXTRACT(EPOCH FROM received_at)::BIGINT AS received_at_epoch
                    FROM market_streams
                    WHERE tenant_id=$1 AND symbol=$2 AND timeframe='M5'
                """, tenant_id, dxy_symbol)
            for row in dxy_rows:
                series["DXY"][int(row["candle_time"])] = float(row["close"])

            for key in ("EUR_USD", "GBP_USD"):
                latest = max(series[key]) if series[key] else None
                candle_age = max(0, now - latest) if latest else None
                configured = bool(os.getenv("OANDA_TOKEN")) if provider == "oanda" else bool(os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY"))
                feeds[key] = {
                    "provider": provider,
                    "configured": configured,
                    "candles": len(series[key]),
                    "last_candle_time": latest,
                    "age_seconds": candle_age,
                    "fresh": bool(configured and candle_age is not None and candle_age <= MAX_AGE_SECONDS),
                }
            latest = max(series["DXY"]) if series["DXY"] else None
            candle_age = max(0, now - latest) if latest else None
            quote_age = max(0, now - int(dxy_stream["quote_time"])) if dxy_stream else None
            receive_age = max(0, now - int(dxy_stream["received_at_epoch"])) if dxy_stream else None
            dxy_quote_fresh = bool(
                dxy_stream
                and quote_age is not None and quote_age <= max_quote_age
                and receive_age is not None and receive_age <= stale_after
            )
            feeds["DXY"] = {
                "provider": "Equiti MT5",
                "symbol": dxy_symbol or None,
                "configured": bool(dxy_symbol),
                "candles": len(series["DXY"]),
                "last_candle_time": latest,
                "age_seconds": candle_age,
                "quote_age_seconds": quote_age,
                "receive_age_seconds": receive_age,
                "fresh": bool(dxy_symbol and candle_age is not None and candle_age <= MAX_AGE_SECONDS and dxy_quote_fresh),
            }

    for row in external_status_rows:
        key = row["symbol"]
        if key in feeds:
            feeds[key].update({
                "configured": bool(row["configured"]),
                "last_success_at": row["last_success_at"].isoformat() if row["last_success_at"] else None,
                "last_error": row["last_error"],
            })

    common = set(series["EUR_USD"]).intersection(series["GBP_USD"], series["DXY"])
    data_ready = (
        provider != "none"
        and all(feeds[key]["configured"] and feeds[key]["fresh"] for key in feeds)
        and len(common) >= 80
    )

    warnings = [
        "Only completed, synchronized M5 candles are compared; missing intervals are not forward-filled.",
        "EUR/USD is a major component of DXY, so EUR/USD–DXY inverse correlation is partly mechanical and is not independent confirmation.",
        "Correlation describes synchronized returns; it does not itself establish SMT divergence or predict direction.",
    ]
    if provider == "mt5":
        warnings.append("All three series are sourced from the same Equiti MT5 terminal; this is broker-specific data, not an independent venue comparison.")
        for key, symbol in symbols.items():
            if not symbol:
                warnings.append(f"{key} is missing its exact MT5 symbol environment setting.")
    else:
        warnings.append("Massive/Polygon forex bars are quote-derived aggregates, not consolidated executed trade volume.")
        if not symbols["DXY"]:
            warnings.append("DXY_MT5_SYMBOL is not configured. No synthetic DXY series will be substituted.")
    if provider == "none":
        warnings.append("Configure OANDA/Massive reference credentials or all three exact Equiti MT5 M5 symbols.")

    metrics = None
    if data_ready:
        metrics = {
            "M5": _frame_metrics(series, 5),
            "M15": _frame_metrics(series, 15),
            "H1": _frame_metrics(series, 60),
        }
    return {
        "status": "ready" if data_ready else "unavailable",
        "provider": provider,
        "dxy_symbol": symbols["DXY"] or None,
        "feeds": feeds,
        "common_m5_bars": len(common),
        "correlations": metrics,
        "warnings": warnings,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "minimum_age_seconds": MAX_AGE_SECONDS,
    }
