import unittest
from analysis import analyze_market

def flat_bars(n=60):
    bars=[]
    for i in range(n):
        price=1.1000+((i%5)-2)*0.00001
        bars.append({"time":1700000000+i*900,"open":price,"high":price+0.0002,"low":price-0.0002,"close":price+0.00001})
    return bars

def bullish_sweep():
    bars=[]
    for i in range(59):
        price=1.1000+((i%3)-1)*0.00001
        bars.append({"time":1700000000+i*900,"open":price,"high":1.1002,"low":1.0998,"close":1.1000})
    bars.append({"time":1700000000+59*900,"open":1.0995,"high":1.1015,"low":1.0990,"close":1.1010})
    return bars

class RulesTests(unittest.TestCase):
    def test_short_history_is_no_trade(self):
        self.assertEqual(analyze_market(flat_bars(20),"EURUSD","M15")["side"],"NO TRADE")
    def test_flat_market_is_no_trade(self):
        self.assertEqual(analyze_market(flat_bars(),"EURUSD","M15")["side"],"NO TRADE")
    def test_confirmed_bullish_setup_is_watch_only(self):
        result=analyze_market(bullish_sweep(),"EURUSD","M15")
        self.assertEqual(result["side"],"WATCH BUY")
        self.assertGreaterEqual(result["score"],70)
        self.assertNotIn("order",result)
    def test_high_spread_blocks_a_candidate(self):
        result=analyze_market(bullish_sweep(),"EURUSD","M15",spread_points=100,point=0.0001)
        self.assertEqual(result["side"],"NO TRADE")
        self.assertIn("spread",result["reason"].lower())

if __name__ == "__main__":
    unittest.main()

