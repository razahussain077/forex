"""Run on the Windows host where the client's Equiti MT5 terminal is installed.

Lists exact symbol names for EUR/USD, GBP/USD and a USD Index/DXY instrument.
This script only reads symbol metadata; it never sends or modifies orders.
"""
import re

try:
    import MetaTrader5 as mt5
except ImportError as exc:
    raise SystemExit(
        "MetaTrader5 Python package is missing. On the Windows MT5 host run: "
        "py -m pip install MetaTrader5"
    ) from exc

if not mt5.initialize():
    raise SystemExit(f"MT5 initialization failed: {mt5.last_error()}")

def classify(symbol):
    raw = f"{symbol.name} {symbol.description} {symbol.path}".upper()
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    if "EURUSD" in compact:
        return "EURUSD_MT5_SYMBOL"
    if "GBPUSD" in compact:
        return "GBPUSD_MT5_SYMBOL"
    if any(term in compact for term in ("DXY", "USDINDEX", "DOLLARINDEX")):
        return "DXY_MT5_SYMBOL"
    return None

try:
    symbols = mt5.symbols_get()
    if symbols is None:
        raise SystemExit(f"MT5 symbols_get failed: {mt5.last_error()}")

    matches = []
    for item in symbols:
        variable = classify(item)
        if variable:
            matches.append((variable, item))

    if not matches:
        print("No matching EURUSD, GBPUSD or DXY-like symbols found.")
        print("Use MT5 Market Watch or ask Equiti for the exact symbol names.")
    else:
        for variable, item in matches:
            print({
                "environment_variable": variable,
                "exact_symbol_name": item.name,
                "description": item.description,
                "path": item.path,
                "visible": item.visible,
                "digits": item.digits,
                "point": item.point,
                "trade_mode": item.trade_mode,
            })
finally:
    mt5.shutdown()
