from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from math import isfinite
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from analysis import analyze_market
from institutional import analyse_institutional, backtest_institutional_confluence, backtest_sweep_continuation, calculate_position_size
from reference_data import init_reference_schema, reference_ingestion_loop, correlation_snapshot, sync_reference_once, selected_provider

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("swiftlabor.worker")

SERVICE_VERSION = "1.1.0"
SCAN_INTERVAL = max(5, int(os.getenv("SCAN_INTERVAL_SECONDS", "15")))
STALE_AFTER = max(30, int(os.getenv("STALE_AFTER_SECONDS", "180")))
MAX_QUOTE_AGE = max(60, int(os.getenv("MAX_QUOTE_AGE_SECONDS", "300")))
MAX_BARS = min(500, max(50, int(os.getenv("MAX_BARS_PER_STREAM", "200"))))
RETENTION_DAYS = min(365, max(30, int(os.getenv("RETENTION_DAYS", "90"))))
MAX_BODY_BYTES = 1_000_000
FRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"}
ASSET_CLASSES = {"forex", "metal", "commodity", "index", "share", "crypto", "future", "other"}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9._+\\-]{2,32}$")
AI_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"

LAST_SCAN: float | None = None
LAST_CLEANUP_CHECK: float = 0.0
SCAN_TASK: asyncio.Task | None = None
REFERENCE_TASK: asyncio.Task | None = None
AI_SEMAPHORE = asyncio.Semaphore(2)


def now_ts() -> int:
    return int(time.time())


def iso(value: int | float | None) -> str | None:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value else None


def decode_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def constant_time_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (TypeError, UnicodeError):
        return False


def bridge_tenant(token: str | None) -> str | None:
    candidates: list[tuple[str, str]] = []
    configured = os.getenv("BRIDGE_TOKENS_JSON", "").strip()
    if configured:
        try:
            parsed = json.loads(configured)
            if isinstance(parsed, dict):
                candidates.extend((str(secret), str(tenant)) for secret, tenant in parsed.items())
        except json.JSONDecodeError:
            log.error("BRIDGE_TOKENS_JSON is invalid JSON")
            return None
    single = os.getenv("BRIDGE_TOKEN", "")
    if single:
        candidates.append((single, os.getenv("TENANT_ID", "equiti-client")))
    matched: str | None = None
    for secret, tenant in candidates:
        if constant_time_equal(token, secret):
            matched = tenant
    return matched


def dashboard_tenant(token: str | None) -> str | None:
    candidates: list[tuple[str, str]] = []
    configured = os.getenv("DASHBOARD_TOKENS_JSON", "").strip()
    if configured:
        try:
            parsed = json.loads(configured)
            if isinstance(parsed, dict):
                candidates.extend((str(secret), str(tenant)) for secret, tenant in parsed.items())
        except json.JSONDecodeError:
            log.error("DASHBOARD_TOKENS_JSON is invalid JSON")
            return None
    single = os.getenv("DASHBOARD_API_TOKEN", "")
    if single:
        candidates.append((single, os.getenv("TENANT_ID", "equiti-client")))
    matched: str | None = None
    for secret, tenant in candidates:
        if constant_time_equal(token, secret):
            matched = tenant
    return matched


class Candle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: int = Field(gt=0)
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    tick_volume: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_ohlc(self) -> "Candle":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the largest candle price")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the smallest candle price")
        return self


class IngestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=2, max_length=32)
    timeframe: str
    bars: list[Candle] = Field(min_length=40, max_length=500)
    asset_class: str = "other"
    description: str = Field(default="", max_length=120)
    broker_server: str = Field(min_length=1, max_length=100)
    quote_time: int = Field(gt=0)
    bid: float = Field(gt=0, allow_inf_nan=False)
    ask: float = Field(gt=0, allow_inf_nan=False)
    digits: int = Field(ge=0, le=12)
    point: float = Field(gt=0, allow_inf_nan=False)
    spread_points: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        value = value.strip()
        if not SYMBOL_RE.fullmatch(value):
            raise ValueError("symbol contains unsupported characters")
        return value

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in FRAMES:
            raise ValueError("unsupported timeframe")
        return value

    @field_validator("asset_class")
    @classmethod
    def validate_asset_class(cls, value: str) -> str:
        value = value.strip().lower()
        return value if value in ASSET_CLASSES else "other"

    @field_validator("broker_server")
    @classmethod
    def normalize_server(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("broker_server is required")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "IngestPayload":
        times = [bar.time for bar in self.bars]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("bars must be strictly ordered with unique timestamps")
        if self.ask < self.bid:
            raise ValueError("ask cannot be below bid")
        if self.bars[-1].time > now_ts() + 120:
            raise ValueError("latest candle timestamp is too far in the future")
        if self.quote_time > now_ts() + 120:
            raise ValueError("quote timestamp is too far in the future")
        # Reject a malformed spread rather than silently trusting SYMBOL_SPREAD.
        calculated = (self.ask - self.bid) / self.point
        if abs(calculated - self.spread_points) > max(3.0, calculated * 0.25):
            raise ValueError("spread_points does not agree with bid/ask and point size")
        return self


class CommentaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=2, max_length=32)
    timeframe: str

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        value = value.strip()
        if not SYMBOL_RE.fullmatch(value):
            raise ValueError("invalid symbol")
        return value

    @field_validator("timeframe")
    @classmethod
    def valid_timeframe(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in FRAMES:
            raise ValueError("unsupported timeframe")
        return value




class RiskCalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_equity: float = Field(gt=0, allow_inf_nan=False)
    risk_percent: float = Field(gt=0, le=1, allow_inf_nan=False)
    stop_distance_pips: float = Field(gt=0, allow_inf_nan=False)
    pip_value_per_standard_lot: float = Field(gt=0, allow_inf_nan=False)
    lot_step: float = Field(default=0.01, gt=0, le=1, allow_inf_nan=False)


class CalendarEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=120)
    currency: str = Field(default="ALL", min_length=3, max_length=8)
    impact: str = Field(default="high")
    event_time: int = Field(gt=0)
    blackout_before_minutes: int = Field(default=15, ge=0, le=240)
    blackout_after_minutes: int = Field(default=15, ge=0, le=240)

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if value != "ALL" and not re.fullmatch(r"[A-Z]{3,8}", value):
            raise ValueError("currency must be ALL or a 3-8 letter currency code")
        return value

    @field_validator("impact")
    @classmethod
    def valid_impact(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"high", "medium", "low"}:
            raise ValueError("impact must be high, medium or low")
        return value


class JournalEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=2, max_length=32)
    direction: str
    timeframe: str
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    stop_price: float = Field(gt=0, allow_inf_nan=False)
    target_price: float = Field(gt=0, allow_inf_nan=False)
    risk_percent: float = Field(gt=0, le=1, allow_inf_nan=False)
    status: str = Field(default="planned")
    r_multiple: float | None = Field(default=None, ge=-50, le=50, allow_inf_nan=False)
    notes: str = Field(default="", max_length=2000)

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        value = value.strip()
        if not SYMBOL_RE.fullmatch(value):
            raise ValueError("invalid symbol")
        return value

    @field_validator("direction")
    @classmethod
    def valid_direction(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        return value

    @field_validator("timeframe")
    @classmethod
    def valid_journal_timeframe(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in FRAMES:
            raise ValueError("unsupported timeframe")
        return value

    @field_validator("status")
    @classmethod
    def valid_journal_status(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"planned", "open", "closed", "win", "loss", "breakeven", "cancelled"}:
            raise ValueError("unsupported journal status")
        return value


class CommentaryResult(BaseModel):
    summary: str = Field(min_length=1, max_length=1200)
    observations: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=6)
    invalidation_context: str = Field(default="", max_length=500)
    limitations: str = Field(min_length=1, max_length=500)


class Store:
    """PostgreSQL-backed storage. Production boot fails closed without DATABASE_URL."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        ssl_required = os.getenv("DATABASE_SSL", "false").lower() == "true"
        serverless = os.getenv("VERCEL") == "1" or os.getenv("SERVERLESS_MODE", "false").lower() == "true"
        configured_max = max(1, min(10, int(os.getenv("DB_POOL_MAX", "3" if serverless else "6"))))
        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=0 if serverless else 1,
            max_size=min(3, configured_max) if serverless else configured_max,
            command_timeout=12,
            ssl=True if ssl_required else None,
            statement_cache_size=0 if serverless else 100,
            server_settings={"application_name": "swiftlabor-vercel-api" if serverless else "swiftlabor-market-worker"},
        )
        await self.initialize()

    def require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("database pool is not ready")
        return self.pool

    async def initialize(self) -> None:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(741902, 1)")
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS market_streams (
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    broker_server TEXT NOT NULL,
                    quote_time BIGINT NOT NULL,
                    bid DOUBLE PRECISION NOT NULL,
                    ask DOUBLE PRECISION NOT NULL,
                    digits SMALLINT NOT NULL,
                    point DOUBLE PRECISION NOT NULL,
                    spread_points DOUBLE PRECISION NOT NULL,
                    last_bar_time BIGINT NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, symbol, timeframe)
                );
                CREATE INDEX IF NOT EXISTS market_streams_fresh_idx
                    ON market_streams (tenant_id, received_at DESC, quote_time DESC);
                CREATE TABLE IF NOT EXISTS market_candles (
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_time BIGINT NOT NULL,
                    open DOUBLE PRECISION NOT NULL,
                    high DOUBLE PRECISION NOT NULL,
                    low DOUBLE PRECISION NOT NULL,
                    close DOUBLE PRECISION NOT NULL,
                    tick_volume DOUBLE PRECISION,
                    PRIMARY KEY (tenant_id, symbol, timeframe, candle_time)
                );
                CREATE INDEX IF NOT EXISTS market_candles_history_idx
                    ON market_candles (tenant_id, symbol, timeframe, candle_time DESC);
                CREATE TABLE IF NOT EXISTS market_signals (
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    anchor_candle_time BIGINT NOT NULL,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, symbol, timeframe)
                );
                CREATE TABLE IF NOT EXISTS api_rate_limits (
                    tenant_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    bucket BIGINT NOT NULL,
                    hits INTEGER NOT NULL,
                    PRIMARY KEY (tenant_id, scope)
                );
                CREATE TABLE IF NOT EXISTS ai_commentary (
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    anchor_candle_time BIGINT NOT NULL,
                    model TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, symbol, timeframe, anchor_candle_time)
                );
                CREATE TABLE IF NOT EXISTS maintenance_state (
                    job TEXT PRIMARY KEY,
                    last_run TIMESTAMPTZ
                );
                INSERT INTO maintenance_state (job, last_run)
                VALUES ('retention_cleanup', NULL)
                ON CONFLICT (job) DO NOTHING;

                CREATE TABLE IF NOT EXISTS manual_calendar_events (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    impact TEXT NOT NULL,
                    event_time BIGINT NOT NULL,
                    blackout_before_minutes INTEGER NOT NULL DEFAULT 15,
                    blackout_after_minutes INTEGER NOT NULL DEFAULT 15,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS manual_calendar_events_time_idx
                    ON manual_calendar_events (tenant_id, event_time);
                CREATE TABLE IF NOT EXISTS trade_journal (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    stop_price DOUBLE PRECISION NOT NULL,
                    target_price DOUBLE PRECISION NOT NULL,
                    risk_percent DOUBLE PRECISION NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    r_multiple DOUBLE PRECISION,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS trade_journal_tenant_time_idx
                    ON trade_journal (tenant_id, created_at DESC);
                """)

    async def ping(self) -> bool:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1

    async def upsert_snapshot(self, tenant_id: str, payload: IngestPayload) -> None:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                stream_result = await conn.execute("""
                    INSERT INTO market_streams (
                        tenant_id, symbol, timeframe, asset_class, description,
                        broker_server, quote_time, bid, ask, digits, point,
                        spread_points, last_bar_time, received_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NOW())
                    ON CONFLICT (tenant_id, symbol, timeframe) DO UPDATE SET
                        asset_class=EXCLUDED.asset_class,
                        description=EXCLUDED.description,
                        broker_server=EXCLUDED.broker_server,
                        quote_time=EXCLUDED.quote_time,
                        bid=EXCLUDED.bid, ask=EXCLUDED.ask,
                        digits=EXCLUDED.digits, point=EXCLUDED.point,
                        spread_points=EXCLUDED.spread_points,
                        last_bar_time=GREATEST(market_streams.last_bar_time, EXCLUDED.last_bar_time),
                        received_at=NOW()
                    WHERE EXCLUDED.quote_time >= market_streams.quote_time
                """, tenant_id, payload.symbol, payload.timeframe, payload.asset_class,
                    payload.description, payload.broker_server, payload.quote_time,
                    payload.bid, payload.ask, payload.digits, payload.point,
                    payload.spread_points, payload.bars[-1].time)
                # Ignore delayed snapshots instead of allowing them to roll back a fresh quote.
                if stream_result.endswith(" 0"):
                    return

                latest_saved_bar = await conn.fetchval("""
                    SELECT MAX(candle_time) FROM market_candles
                    WHERE tenant_id=$1 AND symbol=$2 AND timeframe=$3
                """, tenant_id, payload.symbol, payload.timeframe)
                incoming_bars = payload.bars[-MAX_BARS:]
                # First connection persists history; routine updates only touch the current
                # forming candle and any newly completed candle, not the full history on every poll.
                bars_to_write = [
                    bar for bar in incoming_bars
                    if latest_saved_bar is None or bar.time >= latest_saved_bar
                ]
                if bars_to_write:
                    await conn.executemany("""
                        INSERT INTO market_candles (
                            tenant_id, symbol, timeframe, candle_time,
                            open, high, low, close, tick_volume
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                        ON CONFLICT (tenant_id, symbol, timeframe, candle_time) DO UPDATE SET
                            open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                            close=EXCLUDED.close, tick_volume=EXCLUDED.tick_volume
                    """, [
                        (tenant_id, payload.symbol, payload.timeframe, bar.time,
                         bar.open, bar.high, bar.low, bar.close, bar.tick_volume)
                        for bar in bars_to_write
                    ])

    async def active_tenants(self) -> list[str]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT tenant_id FROM market_streams")
            return [row["tenant_id"] for row in rows]

    async def rate_limit(self, tenant_id: str, scope: str, limit: int, window_seconds: int = 60) -> bool:
        pool = self.require_pool()
        bucket = now_ts() // window_seconds
        async with pool.acquire() as conn:
            count = await conn.fetchval("""
                INSERT INTO api_rate_limits (tenant_id, scope, bucket, hits)
                VALUES ($1,$2,$3,1)
                ON CONFLICT (tenant_id, scope) DO UPDATE SET
                    hits = CASE WHEN api_rate_limits.bucket = EXCLUDED.bucket
                                THEN api_rate_limits.hits + 1 ELSE 1 END,
                    bucket = EXCLUDED.bucket
                RETURNING hits
            """, tenant_id, scope, bucket)
        return count <= limit

    async def active_streams(self, tenant_id: str) -> list[dict[str, Any]]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT tenant_id, symbol, timeframe, asset_class, description,
                       broker_server, quote_time, bid, ask, digits, point,
                       spread_points, last_bar_time,
                       EXTRACT(EPOCH FROM received_at)::BIGINT AS received_at
                FROM market_streams
                WHERE tenant_id=$1
                  AND received_at > NOW() - ($2::INT * INTERVAL '1 second')
                  AND quote_time >= EXTRACT(EPOCH FROM NOW())::BIGINT - $3
                ORDER BY symbol, timeframe
            """, tenant_id, STALE_AFTER, MAX_QUOTE_AGE)
            return [dict(row) for row in rows]

    async def recent_candles(self, tenant_id: str, symbol: str, timeframe: str, limit: int = 220) -> list[dict[str, Any]]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT candle_time AS time, open, high, low, close, tick_volume
                FROM market_candles
                WHERE tenant_id=$1 AND symbol=$2 AND timeframe=$3
                ORDER BY candle_time DESC LIMIT $4
            """, tenant_id, symbol, timeframe, min(500, max(40, limit)))
            return [dict(row) for row in reversed(rows)]

    async def upsert_signal(self, tenant_id: str, symbol: str, timeframe: str, anchor: int, payload: dict[str, Any]) -> None:
        pool = self.require_pool()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO market_signals (tenant_id,symbol,timeframe,anchor_candle_time,payload,updated_at)
                VALUES ($1,$2,$3,$4,$5::jsonb,NOW())
                ON CONFLICT (tenant_id,symbol,timeframe) DO UPDATE SET
                    anchor_candle_time=EXCLUDED.anchor_candle_time,
                    payload=EXCLUDED.payload, updated_at=NOW()
                WHERE market_signals.anchor_candle_time < EXCLUDED.anchor_candle_time
            """, tenant_id, symbol, timeframe, anchor, encoded)

    async def existing_signal_anchor(self, tenant_id: str, symbol: str, timeframe: str) -> int | None:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT anchor_candle_time FROM market_signals
                WHERE tenant_id=$1 AND symbol=$2 AND timeframe=$3
            """, tenant_id, symbol, timeframe)

    async def market(self, tenant_id: str, symbol: str, timeframe: str, limit: int = 300) -> dict[str, Any] | None:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            stream = await conn.fetchrow("""
                SELECT *, EXTRACT(EPOCH FROM received_at)::BIGINT AS received_at_epoch
                FROM market_streams
                WHERE tenant_id=$1 AND symbol=$2 AND timeframe=$3
            """, tenant_id, symbol, timeframe)
        if not stream:
            return None
        stream_data = dict(stream)
        bars = await self.recent_candles(tenant_id, symbol, timeframe, limit)
        quote_age = max(0, now_ts() - int(stream_data["quote_time"]))
        receive_age = max(0, now_ts() - int(stream_data["received_at_epoch"]))
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "asset_class": stream_data["asset_class"],
            "description": stream_data["description"],
            "broker_server": stream_data["broker_server"],
            "data_mode": "broker",
            "bars": bars,
            "updated_at": iso(stream_data["received_at_epoch"]),
            "quote_time": iso(stream_data["quote_time"]),
            "quote_age_seconds": quote_age,
            "receive_age_seconds": receive_age,
            "quote_fresh": quote_age <= MAX_QUOTE_AGE and receive_age <= STALE_AFTER,
            "bid": stream_data["bid"],
            "ask": stream_data["ask"],
            "digits": stream_data["digits"],
            "point": stream_data["point"],
            "spread_points": stream_data["spread_points"],
            "last_bar_at": stream_data["last_bar_time"],
        }

    async def universe(self, tenant_id: str) -> list[dict[str, Any]]:
        streams = await self.active_streams(tenant_id)
        latest: dict[str, dict[str, Any]] = {}
        frames_by_symbol: dict[str, set[str]] = {}
        for row in streams:
            symbol = row["symbol"]
            frames_by_symbol.setdefault(symbol, set()).add(row["timeframe"])
            previous = latest.get(symbol)
            if previous is None or row["received_at"] > previous["received_at"]:
                latest[symbol] = row
        result = []
        for row in latest.values():
            result.append({
                "symbol": row["symbol"], "asset_class": row["asset_class"],
                "description": row["description"],
                "timeframes": sorted(frames_by_symbol.get(row["symbol"], set())),
                "last_price": row["bid"], "bid": row["bid"], "ask": row["ask"],
                "digits": row["digits"], "point": row["point"],
                "spread_points": row["spread_points"],
                "quote_age_seconds": max(0, now_ts() - row["quote_time"]),
                "quote_fresh": True, "broker_server": row["broker_server"],
                "received_at": iso(row["received_at"]),
            })
        result.sort(key=lambda item: (item["asset_class"], item["symbol"]))
        return result

    async def signals(self, tenant_id: str) -> list[dict[str, Any]]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT s.payload
                FROM market_signals s
                JOIN market_streams m
                  ON m.tenant_id=s.tenant_id AND m.symbol=s.symbol AND m.timeframe=s.timeframe
                WHERE s.tenant_id=$1
                  AND m.received_at > NOW() - ($2::INT * INTERVAL '1 second')
                  AND m.quote_time >= EXTRACT(EPOCH FROM NOW())::BIGINT - $3
                ORDER BY s.updated_at DESC
            """, tenant_id, STALE_AFTER, MAX_QUOTE_AGE)
        return [decode_json(row["payload"]) for row in rows]

    async def status(self, tenant_id: str) -> dict[str, Any]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT COUNT(*)::INT AS streams,
                       COUNT(DISTINCT symbol)::INT AS symbols,
                       MAX(received_at) AS last_received,
                       MAX(quote_time) AS last_quote,
                       (SELECT MAX(updated_at) FROM market_signals WHERE tenant_id=$1) AS last_scan
                FROM market_streams
                WHERE tenant_id=$1
                  AND received_at > NOW() - ($2::INT * INTERVAL '1 second')
                  AND quote_time >= EXTRACT(EPOCH FROM NOW())::BIGINT - $3
            """, tenant_id, STALE_AFTER, MAX_QUOTE_AGE)
        return dict(row)

    async def get_cached_commentary(self, tenant_id: str, symbol: str, timeframe: str, anchor: int) -> dict[str, Any] | None:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT model, payload, created_at
                FROM ai_commentary
                WHERE tenant_id=$1 AND symbol=$2 AND timeframe=$3 AND anchor_candle_time=$4
            """, tenant_id, symbol, timeframe, anchor)
        if not row:
            return None
        return {"model": row["model"], "commentary": decode_json(row["payload"]), "created_at": row["created_at"].isoformat()}

    async def save_commentary(self, tenant_id: str, symbol: str, timeframe: str, anchor: int, model: str, payload: dict[str, Any]) -> None:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO ai_commentary (tenant_id,symbol,timeframe,anchor_candle_time,model,payload)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                ON CONFLICT (tenant_id,symbol,timeframe,anchor_candle_time) DO NOTHING
            """, tenant_id, symbol, timeframe, anchor, model, json.dumps(payload, ensure_ascii=False))


    async def calendar_events(self, tenant_id: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, title, currency, impact, event_time,
                       blackout_before_minutes, blackout_after_minutes
                FROM manual_calendar_events
                WHERE tenant_id=$1 AND event_time BETWEEN $2 AND $3
                ORDER BY event_time ASC LIMIT 300
            """, tenant_id, start_ts, end_ts)
        return [dict(row) for row in rows]

    async def add_calendar_event(self, tenant_id: str, event: CalendarEventRequest) -> dict[str, Any]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO manual_calendar_events
                    (tenant_id,title,currency,impact,event_time,blackout_before_minutes,blackout_after_minutes)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                RETURNING id,title,currency,impact,event_time,blackout_before_minutes,blackout_after_minutes
            """, tenant_id, event.title, event.currency, event.impact, event.event_time,
                event.blackout_before_minutes, event.blackout_after_minutes)
        return dict(row)

    async def add_journal_entry(self, tenant_id: str, entry: JournalEntryRequest) -> dict[str, Any]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO trade_journal
                    (tenant_id,symbol,direction,timeframe,entry_price,stop_price,target_price,
                     risk_percent,status,r_multiple,notes)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                RETURNING id,symbol,direction,timeframe,entry_price,stop_price,target_price,
                          risk_percent,status,r_multiple,notes,created_at
            """, tenant_id, entry.symbol, entry.direction, entry.timeframe, entry.entry_price,
                entry.stop_price, entry.target_price, entry.risk_percent, entry.status,
                entry.r_multiple, entry.notes)
        return dict(row)

    async def journal_entries(self, tenant_id: str) -> list[dict[str, Any]]:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id,symbol,direction,timeframe,entry_price,stop_price,target_price,
                       risk_percent,status,r_multiple,notes,created_at
                FROM trade_journal WHERE tenant_id=$1
                ORDER BY created_at DESC LIMIT 100
            """, tenant_id)
        return [dict(row) for row in rows]

    async def cleanup(self) -> None:
        pool = self.require_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM market_candles
                WHERE candle_time < EXTRACT(EPOCH FROM NOW())::BIGINT - ($1::INT * 86400)
            """, RETENTION_DAYS)
            await conn.execute("DELETE FROM api_rate_limits WHERE bucket < $1", now_ts() // 60 - 120)
            await conn.execute("""
                DELETE FROM ai_commentary
                WHERE created_at < NOW() - INTERVAL '90 days'
            """)

    async def cleanup_if_due(self, interval_seconds: int = 3600) -> bool:
        """Run retention cleanup at most once per interval across concurrent Vercel instances."""
        pool = self.require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # A transaction-scoped advisory lock serializes cleanup across cold starts/regions.
                acquired = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1::BIGINT)",
                    724801337,
                )
                if not acquired:
                    return False
                last_run = await conn.fetchval(
                    "SELECT last_run FROM maintenance_state WHERE job='retention_cleanup' FOR UPDATE"
                )
                if last_run and (datetime.now(timezone.utc) - last_run).total_seconds() < interval_seconds:
                    return False
                await conn.execute("""
                    DELETE FROM market_candles
                    WHERE candle_time < EXTRACT(EPOCH FROM NOW())::BIGINT - ($1::INT * 86400)
                """, RETENTION_DAYS)
                await conn.execute(
                    "DELETE FROM api_rate_limits WHERE bucket < $1",
                    now_ts() // 60 - 120,
                )
                await conn.execute("""
                    DELETE FROM ai_commentary
                    WHERE created_at < NOW() - INTERVAL '90 days'
                """)
                await conn.execute("""
                    INSERT INTO maintenance_state (job, last_run)
                    VALUES ('retention_cleanup', NOW())
                    ON CONFLICT (job) DO UPDATE SET last_run=EXCLUDED.last_run
                """)
                return True

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None


async def require_dashboard(request: Request) -> str:
    tenant = dashboard_tenant(request.headers.get("X-Dashboard-Token"))
    if not tenant:
        raise HTTPException(status_code=401, detail="Dashboard authentication required")
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Market data service is not ready")
    if not await store.rate_limit(tenant, "dashboard-api", 240, 60):
        raise HTTPException(status_code=429, detail="Dashboard request limit exceeded")
    return tenant


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length:
            try:
                if int(length) > self.max_bytes:
                    response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def analyze_stream_once(
    store: Store, tenant_id: str, symbol: str, timeframe: str
) -> dict[str, Any] | None:
    """Analyse the latest completed broker candle for one stream.

    In Vercel, this is invoked from the MT5 ingestion request instead of relying
    on a continuously-running background task.
    """
    global LAST_SCAN
    market = await store.market(tenant_id, symbol, timeframe, MAX_BARS)
    if not market:
        return None
    bars = market.get("bars", [])
    # MT5 sends the still-forming candle last. It is never used for analysis.
    completed = bars[:-1]
    if len(completed) < 35:
        return None
    anchor = int(completed[-1]["time"])
    prior_anchor = await store.existing_signal_anchor(tenant_id, symbol, timeframe)
    if prior_anchor == anchor:
        return None
    result = analyze_market(
        completed,
        symbol,
        timeframe,
        spread_points=float(market["spread_points"]),
        point=float(market["point"]),
    )
    result.update({
        "symbol": symbol,
        "timeframe": timeframe,
        "asset_class": market["asset_class"],
        "broker_server": market["broker_server"],
        "data_mode": "broker",
        "quote_age_seconds": market["quote_age_seconds"],
        "evaluated_at": iso(now_ts()),
        "anchor_candle_time": anchor,
    })
    await store.upsert_signal(tenant_id, symbol, timeframe, anchor, result)
    LAST_SCAN = time.time()
    return result


async def run_analysis(store: Store) -> None:
    """Optional persistent-worker loop retained for non-serverless deployments."""
    global LAST_SCAN
    while True:
        try:
            for tenant_id in await store.active_tenants():
                streams = await store.active_streams(tenant_id)
                for stream in streams:
                    await analyze_stream_once(
                        store, tenant_id, stream["symbol"], stream["timeframe"]
                    )
            LAST_SCAN = time.time()
            if not getattr(run_analysis, "_last_cleanup", 0) or time.time() - run_analysis._last_cleanup > 3600:
                await store.cleanup()
                run_analysis._last_cleanup = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Market analysis cycle failed")
        await asyncio.sleep(SCAN_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global SCAN_TASK, REFERENCE_TASK
    if os.getenv("TESTING") == "true":
        yield
        return
    serverless = os.getenv("VERCEL") == "1" or os.getenv("SERVERLESS_MODE", "false").lower() == "true"
    required = ["DATABASE_URL"]
    missing = [name for name in required if not os.getenv(name)]
    if not os.getenv("DASHBOARD_API_TOKEN") and not os.getenv("DASHBOARD_TOKENS_JSON"):
        missing.append("DASHBOARD_API_TOKEN or DASHBOARD_TOKENS_JSON")
    if not os.getenv("BRIDGE_TOKEN") and not os.getenv("BRIDGE_TOKENS_JSON"):
        missing.append("BRIDGE_TOKEN or BRIDGE_TOKENS_JSON")
    if missing:
        raise RuntimeError("Missing required production environment variables: " + ", ".join(missing))
    dashboard_secrets = [os.getenv("DASHBOARD_API_TOKEN", "")]
    bridge_secrets = [os.getenv("BRIDGE_TOKEN", "")]
    for variable, target in [("DASHBOARD_TOKENS_JSON", dashboard_secrets), ("BRIDGE_TOKENS_JSON", bridge_secrets)]:
        if os.getenv(variable):
            try:
                parsed = json.loads(os.environ[variable])
                if not isinstance(parsed, dict):
                    raise ValueError("must be a JSON object")
                target.extend(str(secret) for secret in parsed.keys())
            except (json.JSONDecodeError, ValueError):
                raise RuntimeError(variable + " must be a JSON object")
    if any(secret and len(secret) < 32 for secret in dashboard_secrets + bridge_secrets):
        raise RuntimeError("All configured dashboard and bridge tokens must contain at least 32 characters")
    dsn = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    store = Store(dsn)
    await store.connect()
    await init_reference_schema(store.require_pool())
    app.state.store = store
    app.state.serverless_mode = serverless
    if not serverless:
        SCAN_TASK = asyncio.create_task(run_analysis(store), name="market-analysis-loop")
        REFERENCE_TASK = asyncio.create_task(reference_ingestion_loop(store.require_pool()), name="reference-data-ingestion")
    log.info(
        "SwiftLabor API %s started; storage ready; serverless_mode=%s",
        SERVICE_VERSION, serverless,
    )
    try:
        yield
    finally:
        for task in (SCAN_TASK, REFERENCE_TASK):
            if task:
                task.cancel()
        for task in (SCAN_TASK, REFERENCE_TASK):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await store.close()


app = FastAPI(
    title="SwiftLabor.ai Market Intelligence API",
    version=SERVICE_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(BodyLimitMiddleware)


def get_store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Market data service is not ready")
    return store


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/")
async def root():
    return {"service": "SwiftLabor.ai Market Intelligence API", "version": SERVICE_VERSION, "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "swiftlabor-market-worker", "version": SERVICE_VERSION, "timestamp": iso(now_ts())}


@app.get("/ready")
async def ready(request: Request):
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="database is not ready")
    try:
        if not await store.ping():
            raise HTTPException(status_code=503, detail="database is not ready")
    except HTTPException:
        raise
    except Exception:
        log.exception("Readiness check failed")
        raise HTTPException(status_code=503, detail="database connection failed")
    return {"status": "ready", "storage": "postgresql", "version": SERVICE_VERSION}


@app.post("/ingest/mt5")
async def ingest_mt5(
    payload: IngestPayload,
    request: Request,
    x_bridge_token: str | None = Header(default=None),
):
    global LAST_CLEANUP_CHECK
    tenant_id = bridge_tenant(x_bridge_token)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Invalid bridge token")
    store = get_store(request)
    if not await store.rate_limit(tenant_id, f"ingest:{payload.symbol}:{payload.timeframe}", 10, 60):
        raise HTTPException(status_code=429, detail="Bridge update rate limit exceeded")
    try:
        await store.upsert_snapshot(tenant_id, payload)
        # Vercel Functions are request-driven; do not depend on a perpetual scan task.
        if getattr(request.app.state, "serverless_mode", False):
            await analyze_stream_once(store, tenant_id, payload.symbol, payload.timeframe)
            # Throttle the maintenance check locally; the database advisory lock makes
            # the hourly cleanup safe across concurrent/cold-started function instances.
            if time.monotonic() - LAST_CLEANUP_CHECK >= 60:
                LAST_CLEANUP_CHECK = time.monotonic()
                try:
                    await store.cleanup_if_due()
                except Exception:
                    log.exception("Serverless retention cleanup check failed")
    except Exception:
        log.exception("Broker snapshot persistence/analysis failed tenant=%s symbol=%s timeframe=%s", tenant_id, payload.symbol, payload.timeframe)
        raise HTTPException(status_code=503, detail="Market snapshot could not be persisted or analysed")
    quote_age = max(0, now_ts() - payload.quote_time)
    return {
        "accepted": True,
        "tenant_id": tenant_id,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "bars_received": len(payload.bars),
        "quote_age_seconds": quote_age,
        "quote_fresh": quote_age <= MAX_QUOTE_AGE,
        "data_mode": "broker",
        "execution_mode": "read_only",
        "orders_enabled": False,
    }


@app.get("/api/status")
async def api_status(request: Request):
    tenant_id = await require_dashboard(request)
    store = get_store(request)
    data = await store.status(tenant_id)
    return {
        "process": "online",
        "storage": "postgresql",
        "data_mode": "broker" if data["streams"] else "disconnected",
        "broker_market_streams": data["streams"],
        "broker_symbols": data["symbols"],
        "fresh_quotes": data["streams"],
        "last_feed_at": data["last_received"].isoformat() if data["last_received"] else None,
        "last_quote_at": iso(data["last_quote"]),
        "last_scan_at": data["last_scan"].isoformat() if data.get("last_scan") else iso(LAST_SCAN),
        "scan_mode": "on_ingest" if getattr(request.app.state, "serverless_mode", False) else "background_loop",
        "scan_interval_seconds": None if getattr(request.app.state, "serverless_mode", False) else SCAN_INTERVAL,
        "max_quote_age_seconds": MAX_QUOTE_AGE,
        "retention_days": RETENTION_DAYS,
        "execution_mode": "read_only",
        "orders_enabled": False,
        "engine": "swiftlabor-rules-v1",
        "ai_commentary": bool(os.getenv("AI_GATEWAY_API_KEY") and os.getenv("AI_GATEWAY_MODEL")),
    }


@app.post("/api/reference/sync")
async def api_reference_sync(request: Request):
    tenant_id = await require_dashboard(request)
    store = get_store(request)
    if not await store.rate_limit(tenant_id, "reference-manual-sync", 3, 60):
        raise HTTPException(status_code=429, detail="Reference sync rate limit exceeded")
    provider = selected_provider()
    if provider == "none":
        raise HTTPException(status_code=503, detail="Configure OANDA/Massive credentials or all three Equiti MT5 symbols first")
    if provider == "mt5":
        raise HTTPException(status_code=409, detail="MT5 reference candles are ingested automatically by the bridge; no manual provider sync is needed")
    try:
        result = await sync_reference_once(store.require_pool())
        return {"status": "ok", **result}
    except Exception as exc:
        log.exception("Manual reference data sync failed tenant=%s", tenant_id)
        raise HTTPException(status_code=502, detail="Reference data sync failed; inspect worker logs for provider details") from exc


@app.get("/api/correlation")
async def api_correlation(request: Request):
    tenant_id = await require_dashboard(request)
    try:
        result = await correlation_snapshot(get_store(request).require_pool(), tenant_id)
        return result
    except Exception:
        log.exception("Cross-asset correlation snapshot failed tenant=%s", tenant_id)
        raise HTTPException(status_code=503, detail="Cross-asset analytics are temporarily unavailable")




@app.get("/api/institutional/{symbol}")
async def api_institutional(symbol: str, request: Request):
    tenant_id = await require_dashboard(request)
    symbol = symbol.strip()
    if not SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    store = get_store(request)
    timeframes = ("D1", "H4", "H1", "M15", "M5")
    bars_by_timeframe: dict[str, list[dict[str, Any]]] = {}
    freshness: dict[str, bool] = {}
    stream_candidates: dict[str, dict[str, Any]] = {}
    for timeframe in timeframes:
        market = await store.market(tenant_id, symbol, timeframe, 300)
        if not market:
            continue
        freshness[timeframe] = bool(market.get("quote_fresh"))
        stream_candidates[timeframe] = market
        # MT5 publishes the last, still-forming bar; analysis uses completed candles only.
        bars_by_timeframe[timeframe] = market.get("bars", [])[:-1]
    # Keep quote/spread metadata aligned with a fresh lower-timeframe stream.
    selected_stream = next((
        stream_candidates[tf] for tf in ("M15", "M5")
        if tf in stream_candidates and stream_candidates[tf].get("quote_fresh")
    ), None)
    if selected_stream is None:
        selected_stream = next((
            stream_candidates[tf] for tf in ("M15", "M5") if tf in stream_candidates
        ), None)
    now = now_ts()
    events = await store.calendar_events(tenant_id, now - 4 * 3600, now + 48 * 3600)
    report = analyse_institutional(
        bars_by_timeframe,
        symbol,
        float(selected_stream["spread_points"]) if selected_stream else None,
        float(selected_stream["point"]) if selected_stream else None,
        freshness,
        events,
        now,
    )
    report["quote"] = ({
        "bid": selected_stream["bid"], "ask": selected_stream["ask"],
        "spread_points": selected_stream["spread_points"],
        "quote_age_seconds": selected_stream["quote_age_seconds"],
        "receive_age_seconds": selected_stream["receive_age_seconds"],
        "quote_fresh": selected_stream["quote_fresh"],
        "broker_server": selected_stream["broker_server"],
    } if selected_stream else None)
    report["calendar_source"] = "manually entered events; no automatic calendar provider configured"
    report["feed_health"]["feeds"] = {
        tf: {
            "present": tf in stream_candidates,
            "quote_fresh": bool(stream_candidates.get(tf, {}).get("quote_fresh")),
            "quote_age_seconds": stream_candidates.get(tf, {}).get("quote_age_seconds"),
            "receive_age_seconds": stream_candidates.get(tf, {}).get("receive_age_seconds"),
            "last_bar_at": stream_candidates.get(tf, {}).get("last_bar_at"),
            "broker_server": stream_candidates.get(tf, {}).get("broker_server"),
        }
        for tf in timeframes
    }
    return report


@app.get("/api/backtest/{symbol}")
async def api_backtest(
    symbol: str,
    request: Request,
    timeframe: str = "M15",
    spread_price: float | None = None,
    slippage_price: float | None = None,
):
    tenant_id = await require_dashboard(request)
    symbol = symbol.strip()
    timeframe = timeframe.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol) or timeframe not in FRAMES:
        raise HTTPException(status_code=422, detail="Invalid symbol or timeframe")
    if (
        (spread_price is not None and (not isfinite(spread_price) or spread_price < 0))
        or (slippage_price is not None and (not isfinite(slippage_price) or slippage_price < 0))
    ):
        raise HTTPException(status_code=422, detail="Backtest costs must be finite and non-negative")

    store = get_store(request)
    if not await store.rate_limit(tenant_id, f"backtest:{symbol}:{timeframe}", 2, 60):
        raise HTTPException(status_code=429, detail="Backtest rate limit exceeded; try again in a minute")
    if timeframe == "M15":
        # The primary institutional simulation evaluates the exact same live
        # decision function at each historical M15 close with completed HTF bars.
        frames = ("M15", "H1", "H4", "D1")
        markets = {
            tf: await store.market(tenant_id, symbol, tf, 500 if tf == "M15" else 300)
            for tf in frames
        }
        m15_market = markets["M15"]
        if not m15_market or not m15_market.get("quote_fresh"):
            raise HTTPException(status_code=409, detail="A fresh M15 broker quote is required")
        bars_by_timeframe = {
            tf: (market.get("bars", [])[:-1] if market else [])
            for tf, market in markets.items()
        }
        if len(bars_by_timeframe["M15"]) < 120:
            raise HTTPException(status_code=409, detail="At least 120 completed M15 broker candles are required")
        missing_history = [
            tf for tf in ("D1", "H4", "H1") if len(bars_by_timeframe[tf]) < 45
        ]
        if missing_history:
            raise HTTPException(
                status_code=409,
                detail="At least 45 completed candles are required for D1, H4 and H1 history; insufficient: "
                + ", ".join(missing_history),
            )

        live_spread_price = float(m15_market["spread_points"]) * float(m15_market["point"])
        effective_spread_price = live_spread_price if spread_price is None else spread_price
        effective_slippage_price = (
            max(float(m15_market["point"]), effective_spread_price * 0.10)
            if slippage_price is None else slippage_price
        )
        now = now_ts()
        events = await store.calendar_events(tenant_id, now - 365 * 86400, now + 365 * 86400)
        m15_bars = bars_by_timeframe["M15"]
        split = int(len(m15_bars) * 0.70)
        warmup = max(0, split - 45)
        common = {
            "symbol": symbol,
            "spread_price": effective_spread_price,
            "slippage_price": effective_slippage_price,
            "news_events": events,
        }
        in_sample_bars = {**bars_by_timeframe, "M15": m15_bars[:split]}
        out_sample_bars = {**bars_by_timeframe, "M15": m15_bars[warmup:]}
        full_sample = backtest_institutional_confluence(bars_by_timeframe, **common)
        in_sample = backtest_institutional_confluence(
            in_sample_bars,
            **common,
            end_trading_before=int(m15_bars[split]["time"]) if split < len(m15_bars) else None,
        )
        out_sample = backtest_institutional_confluence(
            out_sample_bars,
            **common,
            start_trading_at=int(m15_bars[split]["time"]) if split < len(m15_bars) else None,
        )
        return {
            "status": "ok",
            "split": "chronological 70% in-sample / 30% holdout with 45 M15 warm-up candles",
            "data_mode": "broker",
            "quote_age_seconds": m15_market["quote_age_seconds"],
            "completed_bars": len(m15_bars),
            "current_feed_health": {
                tf: {
                    "quote_fresh": bool(markets[tf] and markets[tf].get("quote_fresh")),
                    "quote_age_seconds": markets[tf].get("quote_age_seconds") if markets[tf] else None,
                }
                for tf in frames
            },
            "cost_assumptions": {
                "spread_price": effective_spread_price,
                "spread_source": "current live M15 broker quote" if spread_price is None else "caller supplied",
                "slippage_price": effective_slippage_price,
                "slippage_source": "10% of current spread or one point minimum" if slippage_price is None else "caller supplied",
                "commission_and_swap_included": False,
            },
            "strategy_scope": full_sample["strategy_scope"],
            "in_sample": in_sample,
            "out_of_sample": out_sample,
            "full_sample": full_sample,
        }

    # Other timeframe requests retain the simpler single-timeframe diagnostic.
    market = await store.market(tenant_id, symbol, timeframe, 500)
    if not market or not market.get("quote_fresh"):
        raise HTTPException(status_code=409, detail="Fresh broker history is required")
    live_spread_price = float(market["spread_points"]) * float(market["point"])
    effective_spread_price = live_spread_price if spread_price is None else spread_price
    effective_slippage_price = (
        max(float(market["point"]), effective_spread_price * 0.10)
        if slippage_price is None else slippage_price
    )
    bars = market.get("bars", [])[:-1]
    if len(bars) < 120:
        raise HTTPException(status_code=409, detail="At least 120 completed broker candles are required")
    split = int(len(bars) * 0.70)
    warmup = max(0, split - 40)
    common = {
        "symbol": symbol, "timeframe": timeframe,
        "spread_price": effective_spread_price, "slippage_price": effective_slippage_price,
    }
    in_sample = backtest_sweep_continuation(bars[:split], **common)
    out_sample = backtest_sweep_continuation(bars[warmup:], **common)
    full_sample = backtest_sweep_continuation(bars, **common)
    return {
        "status": "ok", "split": "chronological 70% in-sample / 30% out-of-sample with 40-bar warm-up overlap",
        "data_mode": "broker", "quote_age_seconds": market["quote_age_seconds"],
        "completed_bars": len(bars),
        "cost_assumptions": {
            "spread_price": effective_spread_price,
            "spread_source": "current live broker quote" if spread_price is None else "caller supplied",
            "slippage_price": effective_slippage_price,
            "slippage_source": "10% of spread or one point minimum" if slippage_price is None else "caller supplied",
            "commission_and_swap_included": False,
        },
        "strategy_scope": "Single-timeframe diagnostic: sweep + displacement + BOS. It is not the institutional multi-timeframe strategy used by the primary M15 backtest.",
        "in_sample": in_sample, "out_of_sample": out_sample, "full_sample": full_sample,
    }


@app.post("/api/risk/calculate")
async def api_risk_calculate(payload: RiskCalculationRequest, request: Request):
    await require_dashboard(request)
    try:
        return calculate_position_size(
            payload.account_equity, payload.risk_percent, payload.stop_distance_pips,
            payload.pip_value_per_standard_lot, payload.lot_step,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/calendar")
async def api_calendar(request: Request):
    tenant_id = await require_dashboard(request)
    now = now_ts()
    events = await get_store(request).calendar_events(tenant_id, now - 24 * 3600, now + 30 * 86400)
    return {"items": events, "source": "manual", "automatic_provider": False}


@app.post("/api/calendar")
async def api_add_calendar_event(payload: CalendarEventRequest, request: Request):
    tenant_id = await require_dashboard(request)
    if not await get_store(request).rate_limit(tenant_id, "calendar-add", 20, 60):
        raise HTTPException(status_code=429, detail="Calendar event rate limit exceeded")
    event = await get_store(request).add_calendar_event(tenant_id, payload)
    return {"item": event, "source": "manual"}


@app.get("/api/journal")
async def api_journal(request: Request):
    tenant_id = await require_dashboard(request)
    items = await get_store(request).journal_entries(tenant_id)
    return {"items": items, "count": len(items), "storage": "postgresql"}


@app.post("/api/journal")
async def api_add_journal_entry(payload: JournalEntryRequest, request: Request):
    tenant_id = await require_dashboard(request)
    if not await get_store(request).rate_limit(tenant_id, "journal-add", 30, 60):
        raise HTTPException(status_code=429, detail="Journal write rate limit exceeded")
    is_long = payload.direction == "LONG"
    if (is_long and not payload.stop_price < payload.entry_price < payload.target_price) or (
        not is_long and not payload.target_price < payload.entry_price < payload.stop_price
    ):
        raise HTTPException(status_code=422, detail="Prices must be ordered correctly for the selected direction")
    entry = await get_store(request).add_journal_entry(tenant_id, payload)
    return {"item": entry}



@app.get("/api/universe")
async def api_universe(request: Request):
    tenant_id = await require_dashboard(request)
    items = await get_store(request).universe(tenant_id)
    return {"items": items, "count": len(items), "data_mode": "broker" if items else "disconnected"}


@app.get("/api/market/{symbol}")
async def api_market(symbol: str, request: Request, timeframe: str = "M15"):
    tenant_id = await require_dashboard(request)
    symbol = symbol.strip()
    timeframe = timeframe.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol) or timeframe not in FRAMES:
        raise HTTPException(status_code=422, detail="Invalid symbol or timeframe")
    item = await get_store(request).market(tenant_id, symbol, timeframe)
    if not item:
        return {"symbol": symbol, "timeframe": timeframe, "data_mode": "disconnected", "bars": [], "updated_at": None}
    if not item["quote_fresh"]:
        item["data_mode"] = "stale"
        item["bars"] = []
    return item


@app.get("/api/signals")
async def api_signals(request: Request):
    tenant_id = await require_dashboard(request)
    items = await get_store(request).signals(tenant_id)
    items.sort(key=lambda item: (item.get("score", 0), item.get("evaluated_at") or ""), reverse=True)
    return {"items": items, "count": len(items), "data_mode": "broker" if items else "disconnected"}


@app.post("/api/ai/commentary")
async def ai_commentary(payload: CommentaryRequest, request: Request):
    tenant_id = await require_dashboard(request)
    api_key = os.getenv("AI_GATEWAY_API_KEY", "")
    model = os.getenv("AI_GATEWAY_MODEL", "")
    if not api_key or not model:
        raise HTTPException(status_code=503, detail="AI commentary is not configured")
    store = get_store(request)
    if not await store.rate_limit(tenant_id, f"ai-commentary:{payload.symbol}:{payload.timeframe}", 3, 60):
        raise HTTPException(status_code=429, detail="AI commentary rate limit exceeded")
    market = await store.market(tenant_id, payload.symbol, payload.timeframe, 160)
    if not market or not market.get("quote_fresh"):
        raise HTTPException(status_code=409, detail="A fresh broker quote is required for AI commentary")
    completed = market["bars"][:-1]
    if len(completed) < 35:
        raise HTTPException(status_code=409, detail="Not enough completed broker candles")
    anchor = int(completed[-1]["time"])
    cached = await store.get_cached_commentary(tenant_id, payload.symbol, payload.timeframe, anchor)
    if cached:
        return {**cached, "cached": True, "symbol": payload.symbol, "timeframe": payload.timeframe}
    signal = next(
        (item for item in await store.signals(tenant_id)
         if item.get("symbol") == payload.symbol and item.get("timeframe") == payload.timeframe),
        None,
    )
    context = {
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "broker_server": market["broker_server"],
        "quote_age_seconds": market["quote_age_seconds"],
        "bid": market["bid"],
        "ask": market["ask"],
        "spread_points": market["spread_points"],
        "digits": market["digits"],
        "rule_engine_assessment": signal or {"side": "NO TRADE", "reason": "No stored assessment"},
        "recent_completed_candles": completed[-40:],
    }
    system_prompt = (
        "You are a cautious market-research analyst. Explain ONLY the supplied broker-candle evidence. "
        "Do not issue a BUY/SELL recommendation, entry, target, stop, position size, or profit claim. "
        "Do not claim access to order books, dealer inventory, news, or hidden stop orders. "
        "The deterministic rule-engine assessment is authoritative; do not change it. "
        "Identify uncertainty and data limitations. Return a single JSON object with exactly these keys: "
        "summary (string), observations (array of strings), risks (array of strings), "
        "invalidation_context (string), limitations (string)."
    )
    user_prompt = "Analyse this live broker snapshot for research context only:\n" + json.dumps(context, separators=(",", ":"))
    async with AI_SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0)) as client:
                response = await client.post(
                    AI_URL,
                    headers={
                        "Authorization": "Bearer " + api_key,
                        "Content-Type": "application/json",
                        "X-Vercel-AI-App-Name": "SwiftLabor Market Intelligence",
                    },
                    json={
                        "model": model,
                        "temperature": 0.15,
                        "max_tokens": 600,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
            if response.status_code >= 400:
                log.error("AI Gateway returned HTTP %s", response.status_code)
                raise HTTPException(status_code=502, detail="AI commentary provider returned an error")
            raw = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(raw)
            result = CommentaryResult.model_validate(parsed).model_dump()
        except HTTPException:
            raise
        except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError):
            log.exception("AI commentary generation failed")
            raise HTTPException(status_code=502, detail="AI commentary could not be generated")
    await store.save_commentary(tenant_id, payload.symbol, payload.timeframe, anchor, model, result)
    return {
        "model": model, "commentary": result, "cached": False,
        "symbol": payload.symbol, "timeframe": payload.timeframe,
        "anchor_candle_time": anchor,
    }


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return JSONResponse(status_code=422, content={"detail": "Invalid request payload"})


if os.getenv("TESTING") != "true":
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        log.exception("Unhandled API exception path=%s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

