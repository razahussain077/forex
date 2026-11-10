import math
import os
import unittest
from unittest.mock import patch
from reference_data import (
    ReferenceCandle,
    _correlation_pair,
    _valid_candle,
    parse_massive_payload,
    parse_oanda_payload,
    selected_provider,
)


class ProviderSelectionTests(unittest.TestCase):
    def test_auto_uses_mt5_when_all_exact_symbols_are_configured(self):
        env = {
            "REFERENCE_PROVIDER": "auto",
            "OANDA_TOKEN": "",
            "MASSIVE_API_KEY": "",
            "POLYGON_API_KEY": "",
            "EURUSD_MT5_SYMBOL": "EURUSD.a",
            "GBPUSD_MT5_SYMBOL": "GBPUSD.a",
            "DXY_MT5_SYMBOL": "USDX.a",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(selected_provider(), "mt5")

    def test_auto_stays_unavailable_when_mt5_symbol_set_is_incomplete(self):
        env = {
            "REFERENCE_PROVIDER": "auto",
            "OANDA_TOKEN": "",
            "MASSIVE_API_KEY": "",
            "POLYGON_API_KEY": "",
            "EURUSD_MT5_SYMBOL": "EURUSD.a",
            "GBPUSD_MT5_SYMBOL": "GBPUSD.a",
            "DXY_MT5_SYMBOL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(selected_provider(), "none")

    def test_explicit_mt5_mode_does_not_require_external_api_key(self):
        with patch.dict(os.environ, {"REFERENCE_PROVIDER": "mt5"}, clear=False):
            self.assertEqual(selected_provider(), "mt5")


class CandleValidationTests(unittest.TestCase):
    def test_rejects_invalid_ohlc(self):
        candle = ReferenceCandle(300, 1.1, 1.0, 0.9, 1.05)
        self.assertFalse(_valid_candle(candle))

    def test_accepts_valid_ohlc(self):
        candle = ReferenceCandle(300, 1.0, 1.2, 0.9, 1.1)
        self.assertTrue(_valid_candle(candle))

    def test_oanda_skips_incomplete_and_preserves_bid_ask(self):
        payload = {"candles": [
            {
                "time": "2026-11-10T00:00:00Z",
                "complete": True,
                "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
                "bid": {"c": "1.1499"},
                "ask": {"c": "1.1501"},
            },
            {
                "time": "2026-11-10T00:05:00Z",
                "complete": False,
                "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
            },
        ]}
        candles = parse_oanda_payload(payload)
        self.assertEqual(len(candles), 1)
        self.assertAlmostEqual(candles[0].bid_close, 1.1499)
        self.assertAlmostEqual(candles[0].ask_close, 1.1501)
        self.assertEqual(candles[0].price_basis, "mid")

    def test_massive_excludes_current_unfinished_aggregate(self):
        payload = {"results": [
            {"t": 300000, "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "n": 4},
            {"t": 600000, "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.2, "n": 5},
            {"t": 900000, "o": 1.2, "h": 1.4, "l": 1.1, "c": 1.3, "n": 6},
        ]}
        candles = parse_massive_payload(payload, now_ts=1199)
        self.assertEqual([c.timestamp for c in candles], [300, 600])
        self.assertEqual(candles[0].price_basis, "quote_aggregate")


class CorrelationTests(unittest.TestCase):
    def test_inverse_log_returns_produce_negative_one(self):
        increments = [0.001, 0.002, -0.001, 0.003, 0.0005, -0.002, 0.0015]
        cumulative = [0.0]
        for increment in increments:
            cumulative.append(cumulative[-1] + increment)
        a = {i * 300: math.exp(value) for i, value in enumerate(cumulative)}
        b = {i * 300: math.exp(-value) for i, value in enumerate(cumulative)}
        coefficient, samples = _correlation_pair(a, b, 300, 6)
        self.assertAlmostEqual(coefficient, -1.0, places=8)
        self.assertEqual(samples, 7)

    def test_missing_intervals_are_not_forward_filled(self):
        times = [0, 300, 900, 1200, 1500]
        increments = [0.001, 0.002, -0.001, 0.003]
        cumulative = [0.0]
        for increment in increments:
            cumulative.append(cumulative[-1] + increment)
        a = {t: math.exp(cumulative[i]) for i, t in enumerate(times)}
        b = {t: math.exp(-cumulative[i]) for i, t in enumerate(times)}
        coefficient, samples = _correlation_pair(a, b, 300, 3)
        self.assertAlmostEqual(coefficient, -1.0, places=8)
        self.assertEqual(samples, 3)

    def test_zero_variance_returns_no_correlation(self):
        a = {i * 300: 1.1 for i in range(8)}
        b = {i * 300: 1.2 for i in range(8)}
        coefficient, samples = _correlation_pair(a, b, 300, 3)
        self.assertIsNone(coefficient)
        self.assertEqual(samples, 7)

    def test_insufficient_observations_return_none(self):
        a = {i * 300: 1.1 + i * 0.001 for i in range(4)}
        b = {i * 300: 1.2 - i * 0.001 for i in range(4)}
        coefficient, samples = _correlation_pair(a, b, 300, 20)
        self.assertIsNone(coefficient)
        self.assertEqual(samples, 3)


if __name__ == "__main__":
    unittest.main()

