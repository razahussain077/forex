// SwiftLabor.ai MT5 broker feed bridge — READ ONLY. No order functions exist in this EA.
#property strict
#property version "0.3.0"
input string InpWorkerUrl = "https://YOUR-WORKER.onrender.com";
input string InpBridgeToken = "PASTE_PRIVATE_BRIDGE_TOKEN_HERE";
input string InpSymbols = ""; // Blank = every symbol currently visible in Market Watch.
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_M15;
input int InpBars = 180;
input int InpTimerSeconds = 15;
input int InpTimeoutMs = 8000;
input int InpMaxQuoteAgeSeconds = 300;
input int InpMaxSymbolsPerInstance = 25;

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
   if(copied < 35)
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

   long quoteTime = (long)tick.time;
   if(quoteTime <= 0 || (long)TimeCurrent() - quoteTime > InpMaxQuoteAgeSeconds)
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
      payload += "\"time\":" + IntegerToString((long)rates[i].time) + ",";
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
   int code = WebRequest("POST", InpWorkerUrl + "/ingest/mt5", headers, InpTimeoutMs, data, response, responseHeaders);
   if(code < 200 || code >= 300)
      Print("SwiftLabor bridge HTTP ", code, " for ", symbol, ": ", CharArrayToString(response));
}

int OnInit()
{
   if(InpWorkerUrl == "" || StringFind(InpWorkerUrl, "YOUR-WORKER") >= 0 ||
      InpBridgeToken == "" || InpBridgeToken == "PASTE_PRIVATE_BRIDGE_TOKEN_HERE")
      return INIT_PARAMETERS_INCORRECT;
   EventSetTimer((int)MathMax(5, InpTimerSeconds));
   Print("SwiftLabor read-only bridge started. Server=", AccountInfoString(ACCOUNT_SERVER),
         " timeframe=", TfName(InpTimeframe), ". No trade execution is enabled.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   if(InpSymbols == "")
   {
      int total = SymbolsTotal(true);
      int limit = (int)MathMax(1, InpMaxSymbolsPerInstance);
      if(total > limit)
         Print("SwiftLabor: Market Watch has ", total, " symbols; capped at ", limit,
               ". Set InpSymbols to an explicit approved list.");
      for(int i = 0; i < total && i < limit; i++)
         SendSymbol(SymbolName(i, true));
   }
   else
   {
      string symbols[];
      int total = StringSplit(InpSymbols, ',', symbols);
      int limit = (int)MathMax(1, InpMaxSymbolsPerInstance);
      if(total > limit)
      {
         Print("SwiftLabor: explicit symbol list exceeds InpMaxSymbolsPerInstance; excess symbols skipped.");
         total = limit;
      }
      for(int i = 0; i < total; i++)
         SendSymbol(symbols[i]);
   }
}

