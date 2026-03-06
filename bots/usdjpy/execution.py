"""
execution.py - Submits trades to OANDA demo account via v20 REST API
"""

import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades_ep
import oandapyV20.endpoints.positions as positions_ep
from oandapyV20.contrib.requests import MarketOrderRequest, TakeProfitDetails, StopLossDetails
from oandapyV20.exceptions import V20Error

from config import OANDA_ACCESS_TOKEN, OANDA_ACCOUNT_ID, OANDA_ENVIRONMENT, TRADE_CONFIG, ASSET_CONFIG


# Initialize OANDA client
client = oandapyV20.API(
    access_token=OANDA_ACCESS_TOKEN,
    environment=OANDA_ENVIRONMENT  # "practice" for demo, "live" for real
)


def get_open_trades() -> list:
    """Returns list of currently open trades."""
    try:
        r = trades_ep.OpenTrades(OANDA_ACCOUNT_ID)
        rv = client.request(r)
        return rv.get("trades", [])
    except V20Error as e:
        print(f"[EXEC] Error fetching trades: {e}")
        return []


def has_open_position(instrument: str) -> bool:
    """Check if we already have an open trade for this instrument."""
    open_trades = get_open_trades()
    return any(t.get("instrument") == instrument for t in open_trades)


def submit_order(signal: dict) -> dict:
    """
    Submit a market order to OANDA demo account with TP and SL attached.
    Units are positive for buy, negative for sell.
    """
    instrument = ASSET_CONFIG["oanda_instrument"]

    if not signal.get("trade"):
        return {"status": "skipped", "reason": signal.get("reason")}

    # Don't stack positions
    if has_open_position(instrument):
        return {"status": "skipped", "reason": "Position already open"}

    # OANDA uses units (not notional) — use a fixed small unit size for demo
    units = TRADE_CONFIG["oanda_units"]
    if signal["action"] == "sell":
        units = -abs(units)
    else:
        units = abs(units)

    tp_price = str(round(signal["take_profit"], 2))
    sl_price = str(round(signal["stop_loss"], 2))

    try:
        mkt_order = MarketOrderRequest(
            instrument=instrument,
            units=units,
            takeProfitOnFill=TakeProfitDetails(price=tp_price).data,
            stopLossOnFill=StopLossDetails(price=sl_price).data,
        )

        r = orders.OrderCreate(OANDA_ACCOUNT_ID, data=mkt_order.data)
        rv = client.request(r)

        order_fill = rv.get("orderFillTransaction", {})
        return {
            "status":      "submitted",
            "order_id":    order_fill.get("id", "?"),
            "instrument":  instrument,
            "side":        signal["action"],
            "units":       units,
            "fill_price":  order_fill.get("price", "?"),
            "take_profit": tp_price,
            "stop_loss":   sl_price,
        }

    except V20Error as e:
        print(f"[EXEC] OANDA error: {e}")
        return {
            "status": "error",
            "error":  str(e),
        }


def close_all_positions(instrument: str) -> dict:
    """Close all open positions for an instrument."""
    try:
        data = {"longUnits": "ALL", "shortUnits": "ALL"}
        r = positions_ep.PositionClose(OANDA_ACCOUNT_ID, instrument=instrument, data=data)
        client.request(r)
        return {"status": "closed", "instrument": instrument}
    except V20Error as e:
        return {"status": "error", "error": str(e)}
