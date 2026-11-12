import os
import time
import unittest

os.environ["TESTING"] = "true"
os.environ["BRIDGE_TOKEN"] = "unit-test-bridge-token-with-32-chars"
os.environ["DASHBOARD_API_TOKEN"] = "unit-test-dashboard-token-with-32chars"

from fastapi.testclient import TestClient
import main


def bars(n=50):
    now = int(time.time())
    base = now - (n * 900)
    rows = []
    for i in range(n):
        price = 1.0800 + ((i % 5) - 2) * 0.00001
        rows.append({
            "time": base + i * 900,
            "open": price,
            "high": price + 0.0002,
            "low": price - 0.0002,
            "close": price + 0.00001,
            "tick_volume": 10,
        })
    return rows


def payload(symbol="EURUSD.a", quote_age=0):
    return {
        "symbol": symbol,
        "timeframe": "M15",
        "asset_class": "forex",
        "description": "Euro vs US Dollar",
        "broker_server": "Equiti-Demo",
        "quote_time": int(time.time()) - quote_age,
        "bid": 1.08,
        "ask": 1.0801,
        "digits": 5,
        "point": 0.00001,
        "spread_points": 10,
        "bars": bars(),
    }


class FakeStore:
    def __init__(self):
        self.snapshot = None

    async def rate_limit(self, *args, **kwargs):
        return True

    async def upsert_snapshot(self, tenant_id, data):
        self.snapshot = (tenant_id, data)

    async def status(self, tenant_id):
        return {"streams": 1 if self.snapshot else 0, "symbols": 1 if self.snapshot else 0,
                "last_received": None, "last_quote": None}

    async def universe(self, tenant_id):
        if not self.snapshot:
            return []
        tenant, item = self.snapshot
        return [{
            "symbol": item.symbol, "asset_class": item.asset_class,
            "description": item.description, "timeframes": [item.timeframe],
            "last_price": item.bid, "bid": item.bid, "ask": item.ask,
            "digits": item.digits, "point": item.point,
            "spread_points": item.spread_points, "quote_age_seconds": 0,
            "quote_fresh": True, "broker_server": item.broker_server,
            "received_at": None,
        }]

    async def market(self, tenant_id, symbol, timeframe, limit=300):
        if not self.snapshot or self.snapshot[1].symbol != symbol:
            return None
        tenant, item = self.snapshot
        age = max(0, int(time.time()) - item.quote_time)
        return {
            "symbol": item.symbol, "timeframe": item.timeframe,
            "asset_class": item.asset_class, "broker_server": item.broker_server,
            "data_mode": "broker", "bars": [bar.model_dump() for bar in item.bars],
            "updated_at": None, "quote_time": None, "quote_age_seconds": age,
            "receive_age_seconds": 0, "quote_fresh": age <= main.MAX_QUOTE_AGE,
            "bid": item.bid, "ask": item.ask, "digits": item.digits,
            "point": item.point, "spread_points": item.spread_points,
            "last_bar_at": item.bars[-1].time,
        }

    async def signals(self, tenant_id):
        return []


class ApiTests(unittest.TestCase):
    def setUp(self):
        main.app.state.store = FakeStore()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.state.store = None

    def bridge_headers(self):
        return {"X-Bridge-Token": os.environ["BRIDGE_TOKEN"]}

    def dashboard_headers(self):
        return {"X-Dashboard-Token": os.environ["DASHBOARD_API_TOKEN"]}

    def test_bridge_token_required(self):
        response = self.client.post("/ingest/mt5", json=payload())
        self.assertEqual(response.status_code, 401)

    def test_invalid_dashboard_token_rejected(self):
        response = self.client.get("/api/status", headers={"X-Dashboard-Token": "wrong"})
        self.assertEqual(response.status_code, 401)

    def test_read_only_ingestion_and_dashboard_universe(self):
        response = self.client.post("/ingest/mt5", headers=self.bridge_headers(), json=payload())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertFalse(response.json()["orders_enabled"])

        market = self.client.get("/api/market/EURUSD.a?timeframe=M15", headers=self.dashboard_headers())
        self.assertEqual(market.status_code, 200)
        self.assertEqual(market.json()["data_mode"], "broker")
        self.assertEqual(market.json()["broker_server"], "Equiti-Demo")
        self.assertEqual(len(market.json()["bars"]), 50)

        universe = self.client.get("/api/universe", headers=self.dashboard_headers())
        self.assertEqual(universe.status_code, 200)
        self.assertEqual(universe.json()["items"][0]["symbol"], "EURUSD.a")
        self.assertEqual(universe.json()["items"][0]["timeframes"], ["M15"])

    def test_stale_quotes_are_not_served_as_live(self):
        response = self.client.post("/ingest/mt5", headers=self.bridge_headers(), json=payload(quote_age=main.MAX_QUOTE_AGE + 100))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["quote_fresh"])
        market = self.client.get("/api/market/EURUSD.a?timeframe=M15", headers=self.dashboard_headers()).json()
        self.assertEqual(market["data_mode"], "stale")
        self.assertEqual(market["bars"], [])

    def test_out_of_order_candles_rejected(self):
        data = payload()
        data["bars"][-1]["time"] = data["bars"][-2]["time"] - 1
        response = self.client.post("/ingest/mt5", headers=self.bridge_headers(), json=data)
        self.assertEqual(response.status_code, 422)

    def test_quote_spread_must_match_bid_ask(self):
        data = payload()
        data["spread_points"] = 3
        response = self.client.post("/ingest/mt5", headers=self.bridge_headers(), json=data)
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

