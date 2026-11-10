"""Run on the Windows host where the client's Equiti MT5 terminal is installed.

This script only enumerates symbol metadata. It does not connect to an account
for trading and does not place or modify orders.
"""
try:
    import MetaTrader5 as mt5
except ImportError as exc:
    raise SystemExit(
        "MetaTrader5 Python package is missing. On the Windows MT5 host run: "
        "py -m pip install MetaTrader5"
    ) from exc

if not mt5.initialize():
    raise SystemExit(f"MT5 initialization failed: {mt5.last_error()}")

try:
    symbols = mt5.symbols_get()
    if symbols is None:
        raise SystemExit(f"MT5 symbols_get failed: {mt5.last_error()}")

    matches = [
        symbol for symbol in symbols
        if any(term in f"{symbol.name} {symbol.description} {symbol.path}".upper()
               for term in ("DXY", "USD INDEX", "DOLLAR INDEX", "US DOLLAR INDEX"))
    ]

    if not matches:
        print("No DXY-like instrument found.")
        print("Inspect MT5 Market Watch or ask Equiti for the exact USD Index symbol.")
        print("Do not substitute a synthetic or guessed symbol.")
    else:
        for item in matches:
            print({
                "name": item.name,
                "description": item.description,
                "path": item.path,
                "visible": item.visible,
                "digits": item.digits,
                "point": item.point,
                "trade_mode": item.trade_mode,
            })
finally:
    mt5.shutdown()

