"""
SPY Iron Condor Bot - 3 DTE Weekly Strategy
--------------------------------------------
Opens Wednesday morning, closes Friday or at 50% profit.
Targets SPY options expiring the same week (Friday).
Runs via GitHub Actions on schedule.
"""

import os
import sys
import datetime
import logging

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOptionContractsRequest,
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

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER      = True

UNDERLYING    = "SPY"
QTY           = 1
WING_WIDTH    = 3      # $3 wide spreads - good balance of premium vs risk
PROFIT_TARGET = 0.50   # close at 50% of max profit

# US market holidays 2026 - skip these
MARKET_HOLIDAYS_2026 = {
    datetime.date(2026, 1, 1),   # New Year's Day
    datetime.date(2026, 1, 19),  # MLK Day
    datetime.date(2026, 2, 16),  # Presidents Day
    datetime.date(2026, 4, 3),   # Good Friday
    datetime.date(2026, 5, 25),  # Memorial Day
    datetime.date(2026, 7, 3),   # July 4th observed
    datetime.date(2026, 7, 4),   # July 4th
    datetime.date(2026, 9, 7),   # Labor Day
    datetime.date(2026, 11, 26), # Thanksgiving
    datetime.date(2026, 11, 27), # Black Friday
    datetime.date(2026, 12, 25), # Christmas
}

trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
data_client    = StockHistoricalDataClient(API_KEY, API_SECRET)


def is_market_day(date: datetime.date) -> bool:
    return date.weekday() < 5 and date not in MARKET_HOLIDAYS_2026


def get_spy_price() -> float:
    req   = StockLatestQuoteRequest(symbol_or_symbols=UNDERLYING)
    resp  = data_client.get_stock_latest_quote(req)
    quote = resp[UNDERLYING]
    price = (quote.ask_price + quote.bid_price) / 2
    log.info(f"SPY mid price: {price:.2f}")
    return price


def get_this_friday() -> datetime.date:
    """Get the Friday of the current week. If today IS Friday, use today."""
    today      = datetime.date.today()
    days_ahead = (4 - today.weekday()) % 7
    friday     = today + datetime.timedelta(days=days_ahead)
    # If Friday is a holiday, use Thursday
    if friday in MARKET_HOLIDAYS_2026:
        friday = friday - datetime.timedelta(days=1)
    return friday


def get_option_contracts(expiry: datetime.date) -> list:
    req = GetOptionContractsRequest(
        underlying_symbols=[UNDERLYING],
        expiration_date=expiry.strftime("%Y-%m-%d"),
        limit=500,
    )
    contracts = trading_client.get_option_contracts(req).option_contracts
    log.info(f"Fetched {len(contracts)} contracts for expiry {expiry}")
    return contracts


def select_legs(contracts: list, price: float):
    """
    Build iron condor legs:
    - Short put ~5 delta (about 2-3% OTM)
    - Long put WING_WIDTH strikes below short put
    - Short call ~5 delta (about 2-3% OTM)  
    - Long call WING_WIDTH strikes above short call
    """
    puts  = sorted([c for c in contracts if c.type == ContractType.PUT],
                   key=lambda x: float(x.strike_price))
    calls = sorted([c for c in contracts if c.type == ContractType.CALL],
                   key=lambda x: float(x.strike_price))

    if not puts or not calls:
        log.warning("No puts or calls found.")
        return None, None, None, None

    # Target ~2.5% OTM for short strikes
    short_put_target  = price * 0.975
    short_call_target = price * 1.025

    # Find closest strikes to targets
    short_put  = min(puts,  key=lambda x: abs(float(x.strike_price) - short_put_target))
    short_call = min(calls, key=lambda x: abs(float(x.strike_price) - short_call_target))

    short_put_strike  = float(short_put.strike_price)
    short_call_strike = float(short_call.strike_price)

    # Long wings WING_WIDTH dollars away
    long_put_target  = short_put_strike  - WING_WIDTH
    long_call_target = short_call_strike + WING_WIDTH

    long_put  = min(puts,  key=lambda x: abs(float(x.strike_price) - long_put_target))
    long_call = min(calls, key=lambda x: abs(float(x.strike_price) - long_call_target))

    log.info(
        f"Legs — Long Put: {long_put.strike_price} | Short Put: {short_put.strike_price} | "
        f"Short Call: {short_call.strike_price} | Long Call: {long_call.strike_price}"
    )
    return short_put, long_put, short_call, long_call


def get_open_option_positions() -> list:
    positions = trading_client.get_all_positions()
    return [p for p in positions if p.asset_class == AssetClass.US_OPTION
            and UNDERLYING in p.symbol]


def calc_pnl_pct(positions: list) -> float:
    """Calculate total P&L percentage across all condor legs."""
    total_cost    = sum(float(p.cost_basis) for p in positions)
    total_current = sum(float(p.market_value) for p in positions)
    if total_cost == 0:
        return 0
    return (total_current - total_cost) / abs(total_cost)


def open_iron_condor():
    log.info("── OPEN IRON CONDOR ──────────────────────────────────────")

    # Only open on Wednesday (weekday 2)
    today = datetime.date.today()
    if today.weekday() != 2:
        log.info(f"Today is {today.strftime('%A')} — iron condor opens on Wednesday only.")
        return

    existing = get_open_option_positions()
    if existing:
        log.info(f"Already have {len(existing)} open option positions. Skipping.")
        return

    try:
        price = get_spy_price()
    except Exception as e:
        log.error(f"Price fetch failed: {e}")
        return

    friday = get_this_friday()
    log.info(f"Target expiry: {friday} (this Friday, 3 DTE)")

    try:
        contracts = get_option_contracts(friday)
    except Exception as e:
        log.error(f"Contract fetch failed: {e}")
        return

    if len(contracts) < 20:
        log.warning(f"Only {len(contracts)} contracts found — not enough. Skipping.")
        return

    short_put, long_put, short_call, long_call = select_legs(contracts, price)
    if not all([short_put, long_put, short_call, long_call]):
        log.warning("Could not build iron condor legs. Skipping.")
        return

    try:
        for symbol, side in [
            (short_put.symbol,  OrderSide.SELL),
            (long_put.symbol,   OrderSide.BUY),
            (short_call.symbol, OrderSide.SELL),
            (long_call.symbol,  OrderSide.BUY),
        ]:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=QTY,
                side=side,
                time_in_force=TimeInForce.DAY,
            ))
            log.info(f"✅ {side.value.upper()} {symbol} | id={order.id}")

        log.info(f"✅ Iron Condor opened on {UNDERLYING} expiring {friday} | SPY @ {price:.2f}")
    except Exception as e:
        log.error(f"Order failed: {e}")


def close_iron_condor(force: bool = False):
    log.info("── CLOSE IRON CONDOR ─────────────────────────────────────")

    positions = get_open_option_positions()
    if not positions:
        log.info("No open option positions to close.")
        return

    pnl_pct = calc_pnl_pct(positions)
    log.info(f"Current P&L: {pnl_pct*100:.1f}%")

    today = datetime.date.today()
    is_friday = today.weekday() == 4

    if force or is_friday or pnl_pct >= PROFIT_TARGET:
        reason = "forced" if force else ("Friday expiry" if is_friday else f"{pnl_pct*100:.0f}% profit target hit")
        log.info(f"Closing condor — reason: {reason}")
        for pos in positions:
            try:
                trading_client.close_position(pos.symbol)
                log.info(f"✅ Closed {pos.symbol}")
            except Exception as e:
                log.error(f"Failed to close {pos.symbol}: {e}")
    else:
        log.info(f"Holding — P&L {pnl_pct*100:.1f}% hasn't hit 50% target yet.")


if __name__ == "__main__":
    today = datetime.date.today()

    if not is_market_day(today):
        log.info(f"{today} is not a market day — skipping.")
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "open"
    log.info(f"Iron Condor Bot | Mode: {mode.upper()} | {'PAPER' if PAPER else '⚠️  LIVE'}")
    log.info("=" * 60)

    if mode == "open":
        open_iron_condor()
    elif mode == "close":
        close_iron_condor()
    elif mode == "force-close":
        close_iron_condor(force=True)
    else:
        log.error(f"Unknown mode: {mode}")
        sys.exit(1)
