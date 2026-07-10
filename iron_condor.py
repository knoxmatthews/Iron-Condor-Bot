"""
0 DTE Iron Condor Bot - SPY
Uses limit orders to ensure defined risk spread margin treatment.
Opens every market day morning, closes at 50% profit or 3pm ET.
"""

import os
import sys
import logging
import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    GetOptionContractsRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("condor")

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER      = True

UNDERLYING    = "SPY"
QTY           = 1
WING_WIDTH    = 2      # $2 wide spreads = $200 max risk per spread
OTM_PCT       = 0.005  # 0.5% OTM short strikes
PROFIT_TARGET = 0.50   # close at 50% profit

ET = ZoneInfo("America/New_York")

HOLIDAYS = {
    datetime.date(2026, 1, 1),
    datetime.date(2026, 1, 19),
    datetime.date(2026, 2, 16),
    datetime.date(2026, 4, 3),
    datetime.date(2026, 5, 25),
    datetime.date(2026, 7, 3),
    datetime.date(2026, 7, 4),
    datetime.date(2026, 9, 7),
    datetime.date(2026, 11, 26),
    datetime.date(2026, 12, 25),
}

trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)


def is_market_day(date=None):
    d = date or datetime.date.today()
    return d.weekday() < 5 and d not in HOLIDAYS


def get_price() -> float:
    req   = StockLatestQuoteRequest(symbol_or_symbols=UNDERLYING)
    resp  = data_client.get_stock_latest_quote(req)
    quote = resp[UNDERLYING]
    price = (quote.ask_price + quote.bid_price) / 2
    log.info(f"{UNDERLYING} mid price: {price:.2f}")
    return price


def get_contracts(expiry_str):
    req = GetOptionContractsRequest(
        underlying_symbols=[UNDERLYING],
        expiration_date=expiry_str,
        limit=1000,
    )
    contracts = trade_client.get_option_contracts(req).option_contracts
    log.info(f"Fetched {len(contracts)} contracts expiring {expiry_str}")
    return contracts


def build_legs(contracts, price):
    puts  = sorted([c for c in contracts if c.type == ContractType.PUT],
                   key=lambda x: float(x.strike_price))
    calls = sorted([c for c in contracts if c.type == ContractType.CALL],
                   key=lambda x: float(x.strike_price))

    if not puts or not calls:
        log.warning("No puts or calls found.")
        return None

    short_put_target  = price * (1 - OTM_PCT)
    short_call_target = price * (1 + OTM_PCT)

    short_put  = min(puts,  key=lambda x: abs(float(x.strike_price) - short_put_target))
    short_call = min(calls, key=lambda x: abs(float(x.strike_price) - short_call_target))

    sp_strike = float(short_put.strike_price)
    sc_strike = float(short_call.strike_price)

    long_put  = min(puts,  key=lambda x: abs(float(x.strike_price) - (sp_strike - WING_WIDTH)))
    long_call = min(calls, key=lambda x: abs(float(x.strike_price) - (sc_strike + WING_WIDTH)))

    log.info(
        f"Legs: Long Put {long_put.strike_price} | Short Put {short_put.strike_price} | "
        f"Short Call {short_call.strike_price} | Long Call {long_call.strike_price}"
    )

    return {
        "short_put":  short_put,
        "long_put":   long_put,
        "short_call": short_call,
        "long_call":  long_call,
    }


def get_mid_price(contract):
    """Get mid price of an option contract."""
    try:
        bid = float(contract.close_price or 0.05)
        return max(bid, 0.01)
    except:
        return 0.05


def place_spread_orders(legs):
    """
    Place legs as individual limit orders.
    Short legs first to establish the spread margin requirement.
    """
    orders = [
        (legs["short_put"].symbol,  OrderSide.SELL, get_mid_price(legs["short_put"])),
        (legs["long_put"].symbol,   OrderSide.BUY,  get_mid_price(legs["long_put"])),
        (legs["short_call"].symbol, OrderSide.SELL, get_mid_price(legs["short_call"])),
        (legs["long_call"].symbol,  OrderSide.BUY,  get_mid_price(legs["long_call"])),
    ]

    for symbol, side, price in orders:
        limit_price = round(price * (0.95 if side == OrderSide.BUY else 1.05), 2)
        limit_price = max(limit_price, 0.01)
        try:
            o = trade_client.submit_order(LimitOrderRequest(
                symbol        = symbol,
                qty           = QTY,
                side          = side,
                time_in_force = TimeInForce.DAY,
                limit_price   = limit_price,
            ))
            log.info(f"✅ {side.value.upper()} {symbol} limit=${limit_price:.2f} | id={o.id}")
        except Exception as e:
            log.error(f"❌ Order failed {symbol}: {e}")


def get_open_positions():
    positions = trade_client.get_all_positions()
    return [p for p in positions if p.asset_class == AssetClass.US_OPTION
            and UNDERLYING in p.symbol]


def calc_pnl(positions) -> float:
    total_cost    = sum(abs(float(p.cost_basis)) for p in positions)
    total_current = sum(float(p.market_value) for p in positions)
    if total_cost == 0:
        return 0
    return (total_current - total_cost) / total_cost


def open_condor():
    log.info("━━ OPEN 0 DTE IRON CONDOR ━━")

    existing = get_open_positions()
    if existing:
        log.info(f"Already have {len(existing)} open positions. Skipping.")
        return

    today  = datetime.date.today()
    expiry = today.strftime("%Y-%m-%d")

    try:
        price = get_price()
    except Exception as e:
        log.error(f"Price fetch failed: {e}")
        return

    try:
        contracts = get_contracts(expiry)
    except Exception as e:
        log.error(f"Contract fetch failed: {e}")
        return

    if len(contracts) < 20:
        log.warning(f"Only {len(contracts)} contracts — not enough.")
        return

    legs = build_legs(contracts, price)
    if not legs:
        log.warning("Could not build legs.")
        return

    place_spread_orders(legs)
    log.info(f"✅ Iron Condor orders submitted | {UNDERLYING} @ {price:.2f} | Expiry {expiry}")


def close_condor(force=False):
    log.info("━━ CLOSE 0 DTE IRON CONDOR ━━")

    positions = get_open_positions()
    if not positions:
        log.info("No open option positions.")
        return

    pnl    = calc_pnl(positions)
    now_et = datetime.datetime.now(ET)
    past_3pm = now_et.hour >= 15

    log.info(f"P&L: {pnl*100:.1f}% | Force: {force} | Past 3pm: {past_3pm}")

    if force or past_3pm or pnl >= PROFIT_TARGET:
        reason = "forced" if force else ("3pm cutoff" if past_3pm else f"{pnl*100:.0f}% profit target")
        log.info(f"Closing — {reason}")
        for pos in positions:
            try:
                trade_client.close_position(pos.symbol)
                log.info(f"✅ Closed {pos.symbol}")
            except Exception as e:
                log.error(f"Failed to close {pos.symbol}: {e}")
    else:
        log.info(f"Holding — {pnl*100:.1f}% not at 50% target yet.")


if __name__ == "__main__":
    if not is_market_day():
        log.info(f"{datetime.date.today()} is not a market day. Skipping.")
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "open"
    log.info(f"0 DTE Condor Bot | Mode: {mode.upper()} | {'PAPER' if PAPER else '⚠️ LIVE'}")
    log.info("=" * 60)

    if mode == "open":
        open_condor()
    elif mode == "close":
        close_condor()
    elif mode == "force-close":
        close_condor(force=True)
    else:
        log.error(f"Unknown mode: {mode}")
        sys.exit(1)
