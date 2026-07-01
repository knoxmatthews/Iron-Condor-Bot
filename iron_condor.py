"""
SPY Iron Condor Bot - Alpaca Paper Trading
------------------------------------------
Designed to run once via GitHub Actions on a schedule.
GitHub handles the daily timing — this script just runs and exits.

Requirements:
    pip install alpaca-py python-dotenv
"""

import os
import datetime
import logging
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")
PAPER      = True          # Set to False for live trading (be careful!)

UNDERLYING   = "SPY"
QTY          = 1
STRIKE_WIDTH = 2

# ── Client ────────────────────────────────────────────────────────────────────
client = TradingClient(API_KEY, API_SECRET, paper=PAPER)


def get_spy_price() -> float:
    snapshot = client.get_stock_latest_quote(UNDERLYING)
    price = (snapshot.ask_price + snapshot.bid_price) / 2
    log.info(f"SPY mid price: {price:.2f}")
    return price


def is_already_invested() -> bool:
    positions = client.get_all_positions()
    for pos in positions:
        if pos.asset_class == AssetClass.US_OPTION and UNDERLYING in pos.symbol:
            log.info("Already invested. Skipping.")
            return True
    return False


def get_option_contracts(expiry_date: str) -> list:
    request = GetOptionContractsRequest(
        underlying_symbols=[UNDERLYING],
        expiration_date=expiry_date,
        limit=200,
    )
    contracts = client.get_option_contracts(request).option_contracts
    log.info(f"Fetched {len(contracts)} contracts for expiry {expiry_date}")
    return contracts


def find_nearest_expiry() -> str:
    today = datetime.date.today()
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
        return None, None, None, None

    long_put_candidates  = [p for p in puts  if float(p.strike_price) < float(short_put.strike_price)]
    long_call_candidates = [c for c in calls if float(c.strike_price) > float(short_call.strike_price)]

    if not long_put_candidates or not long_call_candidates:
        return None, None, None, None

    long_put  = long_put_candidates[max(0, len(long_put_candidates) - STRIKE_WIDTH)]
    long_call = long_call_candidates[min(STRIKE_WIDTH - 1, len(long_call_candidates) - 1)]

    log.info(
        f"Legs — Long Put: {long_put.strike_price} | Short Put: {short_put.strike_price} | "
        f"Short Call: {short_call.strike_price} | Long Call: {long_call.strike_price}"
    )
    return short_put, long_put, short_call, long_call


def place_order(symbol: str, side: OrderSide):
    order = client.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=QTY,
        side=side,
        time_in_force=TimeInForce.DAY,
    ))
    log.info(f"Order: {side.value} {QTY}x {symbol} — ID: {order.id}")
    return order


def run_iron_condor():
    log.info("=" * 60)
    log.info(f"Iron Condor Bot | Mode: {'PAPER' if PAPER else '⚠️  LIVE'}")

    if datetime.date.today().weekday() >= 5:
        log.info("Weekend — skipping.")
        return

    if is_already_invested():
        return

    try:
        price = get_spy_price()
    except Exception as e:
        log.error(f"Price fetch failed: {e}")
        return

    expiry = find_nearest_expiry()
    log.info(f"Target expiry: {expiry}")

    try:
        contracts = get_option_contracts(expiry)
    except Exception as e:
        log.error(f"Contract fetch failed: {e}")
        return

    if len(contracts) < 10:
        log.warning("Not enough contracts. Skipping.")
        return

    short_put, long_put, short_call, long_call = select_legs(contracts, price)
    if not all([short_put, long_put, short_call, long_call]):
        log.warning("Could not build Iron Condor. Skipping.")
        return

    try:
        place_order(short_put.symbol,  OrderSide.SELL)
        place_order(long_put.symbol,   OrderSide.BUY)
        place_order(short_call.symbol, OrderSide.SELL)
        place_order(long_call.symbol,  OrderSide.BUY)
        log.info(f"✅ Iron Condor opened on SPY @ {price:.2f}")
    except Exception as e:
        log.error(f"Order failed: {e}")


if __name__ == "__main__":
    run_iron_condor()