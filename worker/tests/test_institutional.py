import unittest
from unittest.mock import patch

from institutional import (
    analyse_institutional,
    backtest_sweep_continuation,
    calculate_position_size,
)


def flat_bars(count=120, step=900):
    out = []
    for i in range(count):
        base = 1.10000 + (i % 7) * 0.000002
        out.append({
            "time": 1_700_000_000 + i * step,
            "open": base,
            "high": base + 0.00010,
            "low": base - 0.00010,
            "close": base + 0.000002,
        })
    return out


class InstitutionalResearchTests(unittest.TestCase):
    def test_research_fails_closed_without_bars(self):
        result = analyse_institutional({}, "EURUSD", 1.0, 0.00001, {})
        self.assertEqual(result["decision"], "DATA INSUFFICIENT")
        self.assertEqual(result["higher_timeframe_bias"]["bias"], "insufficient_data")
        self.assertFalse(result["checks"]["fresh_quotes"])


    def test_freshness_requires_two_fresh_htfs_and_prefers_fresh_ltf(self):
        def ready_context(raw_bars, timeframe):
            trend = "bearish" if timeframe == "H1" else "bullish"
            return {
                "timeframe": timeframe,
                "status": "ready",
                "trend": trend,
                "sweep": {"side": "bullish", "wick_extreme": 1.0},
                "displacement": {"qualified": True, "side": "bullish"},
                "bos": "bullish",
                "atr": 0.001,
                "nearest_active_fvg": {"status": "unmitigated"},
                "ote": {"side": "bullish", "in_zone": True},
            }

        bars = {tf: [{}] for tf in ("D1", "H4", "H1", "M15", "M5")}
        freshness = {
            "D1": True, "H4": True, "H1": False,
            "M15": False, "M5": True,
        }
        with patch("institutional._analyse_timeframe", side_effect=ready_context):
            result = analyse_institutional(
                bars, "EURUSD", 1.0, 0.00001, freshness, now_timestamp=1_700_000_000
            )

        self.assertTrue(result["checks"]["fresh_quotes"])
        self.assertEqual(result["higher_timeframe_bias"]["bias"], "bullish")
        self.assertEqual(result["higher_timeframe_bias"]["timeframes_used"], ["D1", "H4"])
        self.assertEqual(result["lower_timeframe"]["timeframe"], "M5")
        self.assertEqual(result["feed_health"]["stale_timeframes"], ["H1", "M15"])

    def test_one_fresh_htf_blocks_market_watch_readiness(self):
        def ready_context(raw_bars, timeframe):
            trend = "bullish"
            return {
                "timeframe": timeframe,
                "status": "ready",
                "trend": trend,
                "sweep": {"side": "bullish", "wick_extreme": 1.0},
                "displacement": {"qualified": True, "side": "bullish"},
                "bos": "bullish",
                "atr": 0.001,
                "nearest_active_fvg": {"status": "unmitigated"},
                "ote": {"side": "bullish", "in_zone": True},
            }

        bars = {tf: [{}] for tf in ("D1", "H4", "H1", "M15")}
        freshness = {"D1": True, "H4": False, "H1": False, "M15": True}
        with patch("institutional._analyse_timeframe", side_effect=ready_context):
            result = analyse_institutional(
                bars, "EURUSD", 1.0, 0.00001, freshness, now_timestamp=1_700_000_000
            )

        self.assertFalse(result["checks"]["fresh_quotes"])
        self.assertEqual(result["higher_timeframe_bias"]["bias"], "insufficient_data")
        self.assertIn("fresh_quotes", result["blockers"])

    def test_missing_h1_is_reported_but_two_fresh_htfs_remain_usable(self):
        def ready_context(raw_bars, timeframe):
            return {
                "timeframe": timeframe,
                "status": "ready",
                "trend": "bullish",
                "sweep": {"side": "bullish", "wick_extreme": 1.0},
                "displacement": {"qualified": True, "side": "bullish"},
                "bos": "bullish",
                "atr": 0.001,
                "nearest_active_fvg": {"status": "unmitigated"},
                "ote": {"side": "bullish", "in_zone": True},
            }

        bars = {tf: [{}] for tf in ("D1", "H4", "M15")}
        freshness = {"D1": True, "H4": True, "M15": True}
        with patch("institutional._analyse_timeframe", side_effect=ready_context):
            result = analyse_institutional(
                bars, "EURUSD", 1.0, 0.00001, freshness, now_timestamp=1_700_000_000
            )

        self.assertTrue(result["checks"]["fresh_quotes"])
        self.assertIn("H1", result["missing_timeframes"])
        self.assertEqual(result["higher_timeframe_bias"]["timeframes_used"], ["D1", "H4"])

    def test_risk_calculator_caps_risk_at_one_percent(self):
        result = calculate_position_size(10000, 1.0, 30, 10, 0.01)
        self.assertEqual(result["risk_amount"], 100.0)
        self.assertEqual(result["minimum_target_distance_pips"], 105.0)
        self.assertLessEqual(result["estimated_loss_at_stop"], 100.0)

    def test_risk_calculator_rejects_risk_above_one_percent(self):
        with self.assertRaises(ValueError):
            calculate_position_size(10000, 1.01, 30, 10)

    def test_backtest_requires_at_least_three_point_five_r(self):
        with self.assertRaises(ValueError):
            backtest_sweep_continuation(flat_bars(), "EURUSD", "M15", target_r=2.0)

    def test_flat_market_does_not_generate_backtest_trades(self):
        result = backtest_sweep_continuation(flat_bars(), "EURUSD", "M15")
        self.assertEqual(result["trades"], 0)
        self.assertIsNone(result["win_rate_percent"])
        self.assertIsNone(result["expectancy_r"])

    def test_backtest_rejects_negative_costs(self):
        with self.assertRaises(ValueError):
            backtest_sweep_continuation(flat_bars(), "EURUSD", "M15", spread_price=-0.1)

    def test_news_blackout_blocks_a_watch_setup(self):
        timestamp = 1_700_000_000
        result = analyse_institutional(
            {}, "EURUSD", 1, 0.00001, {},
            news_events=[{
                "title": "High impact event",
                "currency": "USD",
                "timestamp": timestamp,
                "blackout_before_minutes": 30,
                "blackout_after_minutes": 30,
            }],
            now_timestamp=timestamp,
        )
        self.assertEqual(len(result["active_news_blackouts"]), 1)
        self.assertFalse(result["checks"]["no_high_impact_news_blackout"])


if __name__ == "__main__":
    unittest.main()
