"""
SPY Iron Condor Bot - Alpaca Paper Trading
------------------------------------------
Runs once via GitHub Actions on a schedule.
Handles both OPEN (morning) and CLOSE (afternoon) logic in one script.
"""

import os
import sys
import datetime
import logging
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOptionContractsRequest,
    ClosePositionRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")
PAPER      = True

UNDERLYING   = "SPY"
QTY          = 1
STRIKE_WIDTH = 2

trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
data_client    = StockHistoricalDataClient(API_KEY, API_SECRET)


def get_spy_price() -> float:
    request  = StockLatestQuoteRequest(symbol_or_symbols=UNDERLYING)
    response = data_client.get_stock_latest_quote(request)
    quote    = response[UNDERLYING]
    price    = (quote.ask_price + quote.bid_price) / 2
    log.info(f"SPY mid price: {price:.2f}")
    return price


def get_open_option_positions() -> list:
    positions = trading_client.get_all_positions()
    return [p for p in positions if p.asset_class == AssetClass.US_OPTION and UNDERLYING in p.symbol]


def is_already_invested() -> bool:
    open_opts = get_open_option_positions()
    if open_opts:
        log.info(f"Already have {len(open_opts)} open option position(s). Skipping open.")
        return True
    return False


def get_option_contracts(expiry_date: str) -> list:
    request = GetOptionContractsRequest(
        underlying_symbols=[UNDERLYING],
        expiration_date=expiry_date,
        limit=200,
    )
    contracts = trading_client.get_option_contracts(request).option_contracts
    log.info(f"Fetched {len(contracts)} contracts for expiry {expiry_date}")
    return contracts


def find_nearest_friday() -> str:
    today      = datetime.date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    expiry = today + datetime.timedelta(days=days_ahead)
    return expiry.strftime("%Y-%m-%d")


def select_legs(contracts: list, price: float):
    puts  = sorted([c for c in contracts if c.type == ContractType.PUT],  key=lambda x: float(x.strike_price))
    calls = sorted([c for c in contracts if c.type == ContractType.CALL], key=lambda x: float(x.strike_price))

    short_put  = next((p for p in reversed(puts)  if float(p.strike_price) < price), None)
    short_call = next((c for c in calls            if float(c.strike_price) > price), None)

    if not short_put or not short_call:
        log.warning("Could not find short put or short call.")
        return None, None, None, None

    long_put_candidates  = [p for p in puts  if float(p.strike_price) < float(short_put.strike_price)]
    long_call_candidates = [c for c in calls if float(c.strike_price) > float(short_call.strike_price)]

    if not long_put_candidates or not long_call_candidates:
        log.warning("Could not find long put or long call wings.")
        return None, None, None, None

    long_put  = long_put_candidates[max(0, len(long_put_candidates) - STRIKE_WIDTH)]
    long_call = long_call_candidates[min(STRIKE_WIDTH - 1, len(long_call_candidates) - 1)]

    log.info(
        f"Legs — Long Put: {long_put.strike_price} | Short Put: {short_put.strike_price} | "
        f"Short Call: {short_call.strike_price} | Long Call: {long_call.strike_price}"
    )
    return short_put, long_put, short_call, long_call


def place_order(symbol: str, side: OrderSide):
    order = trading_client.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=QTY,
        side=side,
        time_in_force=TimeInForce.DAY,
    ))
    log.info(f"Order placed: {side.value} {QTY}x {symbol} — ID: {order.id}")
    return order


def open_iron_condor():
    log.info("── OPEN IRON CONDOR ──────────────────────────────────────")
    if is_already_invested():
        return
    try:
        price = get_spy_price()
    except Exception as e:
        log.error(f"Price fetch failed: {e}")
        return
    expiry = find_nearest_friday()
    log.info(f"Target expiry: {expiry}")
    try:
        contracts = get_option_contracts(expiry)
    except Exception as e:
        log.error(f"Contract fetch failed: {e}")
        return
    if len(contracts) < 10:
        log.warning("Not enough contracts available. Skipping.")
        return
    short_put, long_put, short_call, long_call = select_legs(contracts, price)
    if not all([short_put, long_put, short_call, long_call]):
        log.warning("Could not build Iron Condor legs. Skipping.")
        return
    try:
        place_order(short_put.symbol,  OrderSide.SELL)
        place_order(long_put.symbol,   OrderSide.BUY)
        place_order(short_call.symbol, OrderSide.SELL)
        place_order(long_call.symbol,  OrderSide.BUY)
        log.info(f"✅ Iron Condor opened on {UNDERLYING} @ {price:.2f}")
    except Exception as e:
        log.error(f"Order placement failed: {e}")


def close_iron_condor():
    log.info("── CLOSE IRON CONDOR ─────────────────────────────────────")
    open_opts = get_open_option_positions()
    if not open_opts:
        log.info("No open option positions to close.")
        return
    for pos in open_opts:
        try:
            trading_client.close_position(pos.symbol)
            log.info(f"✅ Closed position: {pos.symbol}")
        except Exception as e:
            log.error(f"Failed to close {pos.symbol}: {e}")


if __name__ == "__main__":
    if datetime.date.today().weekday() >= 5:
        log.info("Weekend — skipping.")
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "open"
    log.info(f"Iron Condor Bot | Mode: {mode.upper()} | {'PAPER' if PAPER else '⚠️  LIVE'}")
    log.info("=" * 60)

    if mode == "open":
        open_iron_condor()
    elif mode == "close":
        close_iron_condor()
    else:
        log.error(f"Unknown mode: {mode}. Use 'open' or 'close'.")
        sys.exit(1)
