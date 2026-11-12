// SwiftLabor.ai MT5 broker feed bridge — READ ONLY. No order functions exist in this EA.
#property strict
#property version "1.001"
input string InpWorkerUrl = "https://swiftlabor-forex-api.vercel.app";
input string InpBridgeToken = "PASTE_PRIVATE_BRIDGE_TOKEN_HERE";
// Empty = scan all symbols selected in MT5 Market Watch. Or enter a comma-separated allowlist.
input string InpSymbols = "";
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_M15;
input int InpBars = 180;
input int InpTimerSeconds = 15;
input int InpTimeoutMs = 8000;
input int InpMaxQuoteAgeSeconds = 300;
// Symbols processed per timer cycle. Larger lists are scanned in round-robin batches.
input int InpMaxSymbolsPerInstance = 25;

int g_marketWatchCursor = 0;
int g_explicitCursor = 0;
int g_lastTransportError = 0;
int g_lastHttpError = 0;
bool g_marketWatchBatchLogged = false;
bool g_explicitBatchLogged = false;
string g_successLoggedSymbols[];

string TfName(ENUM_TIMEFRAMES tf)
{
   switch(tf)
   {
      case PERIOD_M1: return "M1";
      case PERIOD_M5: return "M5";
      case PERIOD_M15: return "M15";
      case PERIOD_M30: return "M30";
      case PERIOD_H1: return "H1";
      case PERIOD_H4: return "H4";
      case PERIOD_D1: return "D1";
      case PERIOD_W1: return "W1";
   }
   return "M15";
}

// Convert broker-server candle/tick timestamps to UTC before sending to the API.
datetime BrokerToUtc(datetime brokerTime)
{
   int brokerOffsetSeconds = (int)(TimeCurrent() - TimeGMT());
   return brokerTime - brokerOffsetSeconds;
}

string AssetClass(string symbol)
{
   string path = SymbolInfoString(symbol, SYMBOL_PATH);
   string lower = path + " " + symbol;
   StringToLower(lower);
   if(StringFind(lower, "crypto") >= 0 || StringFind(lower, "bitcoin") >= 0 || StringFind(lower, "ethereum") >= 0)
      return "crypto";
   if(StringFind(lower, "metal") >= 0 || StringFind(lower, "gold") >= 0 || StringFind(lower, "silver") >= 0 ||
      StringFind(lower, "xau") >= 0 || StringFind(lower, "xag") >= 0)
      return "metal";
   if(StringFind(lower, "index") >= 0 || StringFind(lower, "indices") >= 0)
      return "index";
   if(StringFind(lower, "stock") >= 0 || StringFind(lower, "share") >= 0 || StringFind(lower, "equity") >= 0)
      return "share";
   if(StringFind(lower, "commodity") >= 0 || StringFind(lower, "energy") >= 0 || StringFind(lower, "oil") >= 0)
      return "commodity";
   if(StringFind(lower, "forex") >= 0 || StringFind(lower, "fx") >= 0)
      return "forex";
   return "other";
}

string JsonEscape(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", "");
   StringReplace(value, "\n", " ");
   return value;
}

string Payload(string symbol)
{
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied = CopyRates(symbol, InpTimeframe, 0, InpBars, rates);
   // The API requires at least 40 bars; wait until MT5 has enough history.
   if(copied < 40)
   {
      Print("SwiftLabor: waiting for history: ", symbol, " / ", TfName(InpTimeframe), " bars=", copied);
      return "";
   }

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return "";
   if(tick.bid <= 0 || tick.ask < tick.bid || point <= 0)
      return "";

   long quoteTime = (long)BrokerToUtc(tick.time);
   if(quoteTime <= 0 || (long)TimeGMT() - quoteTime > InpMaxQuoteAgeSeconds)
   {
      Print("SwiftLabor: stale broker quote skipped: ", symbol);
      return "";
   }
   double spreadPoints = (tick.ask - tick.bid) / point;

   string broker = JsonEscape(AccountInfoString(ACCOUNT_SERVER));
   string description = JsonEscape(SymbolInfoString(symbol, SYMBOL_DESCRIPTION));
   string payload = "{";
   payload += "\"symbol\":\"" + JsonEscape(symbol) + "\",";
   payload += "\"timeframe\":\"" + TfName(InpTimeframe) + "\",";
   payload += "\"asset_class\":\"" + AssetClass(symbol) + "\",";
   payload += "\"description\":\"" + description + "\",";
   payload += "\"broker_server\":\"" + broker + "\",";
   payload += "\"quote_time\":" + IntegerToString(quoteTime) + ",";
   payload += "\"bid\":" + DoubleToString(tick.bid, digits) + ",";
   payload += "\"ask\":" + DoubleToString(tick.ask, digits) + ",";
   payload += "\"digits\":" + IntegerToString(digits) + ",";
   payload += "\"point\":" + DoubleToString(point, 12) + ",";
   payload += "\"spread_points\":" + DoubleToString(spreadPoints, 2) + ",";
   payload += "\"bars\":[";

   // MT5 series index 0 is the currently forming candle. Emit oldest -> newest.
   for(int i = copied - 1; i >= 0; i--)
   {
      if(i != copied - 1)
         payload += ",";
      payload += "{";
      payload += "\"time\":" + IntegerToString((long)BrokerToUtc(rates[i].time)) + ",";
      payload += "\"open\":" + DoubleToString(rates[i].open, digits) + ",";
      payload += "\"high\":" + DoubleToString(rates[i].high, digits) + ",";
      payload += "\"low\":" + DoubleToString(rates[i].low, digits) + ",";
      payload += "\"close\":" + DoubleToString(rates[i].close, digits) + ",";
      payload += "\"tick_volume\":" + IntegerToString((long)rates[i].tick_volume);
      payload += "}";
   }
   payload += "]}";
   return payload;
}

void SendSymbol(string symbol)
{
   StringTrimLeft(symbol);
   StringTrimRight(symbol);
   if(symbol == "" || !SymbolSelect(symbol, true))
      return;
   string body = Payload(symbol);
   if(body == "")
      return;

   char data[];
   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(ArraySize(data) > 0)
      ArrayResize(data, ArraySize(data) - 1);

   char response[];
   string responseHeaders;
   string headers = "Content-Type: application/json\r\nX-Bridge-Token: " + InpBridgeToken + "\r\n";
   ResetLastError();
   int code = WebRequest("POST", InpWorkerUrl + "/ingest/mt5", headers, InpTimeoutMs, data, response, responseHeaders);
   if(code == -1)
   {
      int error = GetLastError();
      if(error != g_lastTransportError)
      {
         PrintFormat("SwiftLabor: WebRequest transport failure. Error=%d Symbol=%s Timeframe=%s ProgramType=%d StrategyTester=%s",
                     error, symbol, TfName(InpTimeframe),
                     (int)MQLInfoInteger(MQL_PROGRAM_TYPE),
                     MQLInfoInteger(MQL_TESTER) ? "yes" : "no");
         if(error == 4014)
            Print("SwiftLabor: 4014 means this function is not allowed in the current program context. Confirm this file is running as a live Expert Advisor, not an indicator or Strategy Tester.");
      }
      g_lastTransportError = error;
      return;
   }

   g_lastTransportError = 0;
   if(code < 200 || code >= 300)
   {
      if(code != g_lastHttpError)
         Print("SwiftLabor bridge HTTP ", code, " for ", symbol, ": ", CharArrayToString(response));
      g_lastHttpError = code;
      return;
   }

   g_lastHttpError = 0;
   bool alreadyLogged = false;
   for(int i = 0; i < ArraySize(g_successLoggedSymbols); i++)
   {
      if(g_successLoggedSymbols[i] == symbol)
      {
         alreadyLogged = true;
         break;
      }
   }
   if(!alreadyLogged)
   {
      int size = ArraySize(g_successLoggedSymbols);
      ArrayResize(g_successLoggedSymbols, size + 1);
      g_successLoggedSymbols[size] = symbol;
      PrintFormat("SwiftLabor: ingest accepted for %s %s (HTTP %d).",
                  symbol, TfName(InpTimeframe), code);
   }
}

int OnInit()
{
   if(InpWorkerUrl == "" || StringFind(InpWorkerUrl, "YOUR-WORKER") >= 0 ||
      InpBridgeToken == "" || InpBridgeToken == "PASTE_PRIVATE_BRIDGE_TOKEN_HERE")
      return INIT_PARAMETERS_INCORRECT;
   EventSetTimer((int)MathMax(5, InpTimerSeconds));
   PrintFormat("SwiftLabor read-only bridge started. Server=%s timeframe=%s mode=%s program_type=%d tester=%s. No trade execution is enabled.",
               AccountInfoString(ACCOUNT_SERVER), TfName(InpTimeframe),
               InpSymbols == "" ? "MARKET_WATCH" : "EXPLICIT_SYMBOLS",
               (int)MQLInfoInteger(MQL_PROGRAM_TYPE),
               MQLInfoInteger(MQL_TESTER) ? "yes" : "no");
   if(InpSymbols == "")
      Print("SwiftLabor: empty InpSymbols means scan selected Market Watch symbols in round-robin batches. Use Market Watch -> Show All to expand the list.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void ScanMarketWatchBatch()
{
   int total = SymbolsTotal(true);
   if(total <= 0)
   {
      if(!g_marketWatchBatchLogged)
      {
         Print("SwiftLabor: Market Watch is empty. Use Market Watch -> Show All or add broker symbols.");
         g_marketWatchBatchLogged = true;
      }
      return;
   }

   int limit = (int)MathMax(1, InpMaxSymbolsPerInstance);
   int count = (int)MathMin(total, limit);
   if(total > limit && !g_marketWatchBatchLogged)
   {
      PrintFormat("SwiftLabor: Market Watch contains %d symbols; scanning %d per timer cycle in round-robin batches.",
                  total, limit);
      g_marketWatchBatchLogged = true;
   }

   int start = g_marketWatchCursor % total;
   for(int offset = 0; offset < count; offset++)
   {
      int index = (start + offset) % total;
      SendSymbol(SymbolName(index, true));
   }
   g_marketWatchCursor = (start + count) % total;
}

void ScanExplicitSymbolsBatch()
{
   string symbols[];
   int total = StringSplit(InpSymbols, ',', symbols);
   if(total <= 0)
      return;

   int limit = (int)MathMax(1, InpMaxSymbolsPerInstance);
   int count = (int)MathMin(total, limit);
   if(total > limit && !g_explicitBatchLogged)
   {
      PrintFormat("SwiftLabor: explicit list contains %d symbols; scanning %d per timer cycle in round-robin batches.",
                  total, limit);
      g_explicitBatchLogged = true;
   }

   int start = g_explicitCursor % total;
   for(int offset = 0; offset < count; offset++)
   {
      int index = (start + offset) % total;
      SendSymbol(symbols[index]);
   }
   g_explicitCursor = (start + count) % total;
}

void OnTimer()
{
   if(InpSymbols == "")
      ScanMarketWatchBatch();
   else
      ScanExplicitSymbolsBatch();
}

