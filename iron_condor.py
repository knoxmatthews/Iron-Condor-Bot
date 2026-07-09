"""
0 DTE Iron Condor Bot - SPX/SPY
--------------------------------
Opens a same-day expiry iron condor every market morning.
Collects theta premium and closes at 50% profit or 3pm ET.
Uses SPX first, falls back to SPY if SPX unavailable.
Alpaca PAPER only. GitHub Actions triggers open + close.
"""

import os
import sys
import logging
import datetime
from zoneinfo import ZoneInfo

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
log = logging.getLogger("condor")

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER      = True

QTY          = 5
WING_WIDTH   = 5      # $5 wide wings on SPX, $2 on SPY
OTM_PCT      = 0.005  # 0.5% OTM for short strikes - tight for max premium
PROFIT_TARGET = 0.50  # close at 50% of max profit
CLOSE_TIME   = (15, 0)  # force close at 3pm ET no matter what

ET = ZoneInfo("America/New_York")

# US market holidays 2026
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


def get_price(symbol) -> float:
    req   = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    resp  = data_client.get_stock_latest_quote(req)
    quote = resp[symbol]
    price = (quote.ask_price + quote.bid_price) / 2
    log.info(f"{symbol} mid price: {price:.2f}")
    return price


def get_contracts(underlying, expiry_str):
    req = GetOptionContractsRequest(
        underlying_symbols=[underlying],
        expiration_date=expiry_str,
        limit=1000,
    )
    contracts = trade_client.get_option_contracts(req).option_contracts
    log.info(f"Fetched {len(contracts)} contracts for {underlying} expiring {expiry_str}")
    return contracts


def build_legs(contracts, price, wing_width):
    puts  = sorted([c for c in contracts if c.type == ContractType.PUT],
                   key=lambda x: float(x.strike_price))
    calls = sorted([c for c in contracts if c.type == ContractType.CALL],
                   key=lambda x: float(x.strike_price))

    if not puts or not calls:
        log.warning("No puts or calls found.")
        return None

    # Short strikes just OTM
    short_put_target  = price * (1 - OTM_PCT)
    short_call_target = price * (1 + OTM_PCT)

    short_put  = min(puts,  key=lambda x: abs(float(x.strike_price) - short_put_target))
    short_call = min(calls, key=lambda x: abs(float(x.strike_price) - short_call_target))

    sp_strike = float(short_put.strike_price)
    sc_strike = float(short_call.strike_price)

    # Long wings further OTM
    long_put  = min(puts,  key=lambda x: abs(float(x.strike_price) - (sp_strike - wing_width)))
    long_call = min(calls, key=lambda x: abs(float(x.strike_price) - (sc_strike + wing_width)))

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


def place_order(symbol, side):
    o = trade_client.submit_order(MarketOrderRequest(
        symbol        = symbol,
        qty           = QTY,
        side          = side,
        time_in_force = TimeInForce.DAY,
    ))
    log.info(f"✅ {side.value.upper()} {symbol} | id={o.id}")
    return o


def get_open_positions():
    positions = trade_client.get_all_positions()
    return [p for p in positions if p.asset_class == AssetClass.US_OPTION]


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
        log.info(f"Already have {len(existing)} open option positions. Skipping open.")
        return

    today = datetime.date.today()
    expiry = today.strftime("%Y-%m-%d")

    # Try SPX first, fall back to SPY
    for underlying, wing in [("SPX", 10), ("SPY", 2)]:
        log.info(f"Trying {underlying}...")
        try:
            price = get_price(underlying)
        except Exception as e:
            log.warning(f"Could not get {underlying} price: {e}")
            continue

        try:
            contracts = get_contracts(underlying, expiry)
        except Exception as e:
            log.warning(f"Could not get {underlying} contracts: {e}")
            continue

        if len(contracts) < 20:
            log.warning(f"Not enough {underlying} contracts ({len(contracts)}). Trying next.")
            continue

        legs = build_legs(contracts, price, wing)
        if not legs:
            log.warning(f"Could not build {underlying} legs. Trying next.")
            continue

        # Place all 4 legs
        try:
            place_order(legs["short_put"].symbol,  OrderSide.SELL)
            place_order(legs["long_put"].symbol,   OrderSide.BUY)
            place_order(legs["short_call"].symbol, OrderSide.SELL)
            place_order(legs["long_call"].symbol,  OrderSide.BUY)
            log.info(f"✅ 0 DTE Iron Condor opened on {underlying} @ {price:.2f} | Expiry: {expiry}")
            return
        except Exception as e:
            log.error(f"Order placement failed for {underlying}: {e}")
            continue

    log.error("Could not open condor on SPX or SPY. Both failed.")


def close_condor(force=False):
    log.info("━━ CLOSE 0 DTE IRON CONDOR ━━")

    positions = get_open_positions()
    if not positions:
        log.info("No open option positions.")
        return

    pnl = calc_pnl(positions)
    now_et = datetime.datetime.now(ET)
    past_close_time = now_et.hour > CLOSE_TIME[0] or (now_et.hour == CLOSE_TIME[0] and now_et.minute >= CLOSE_TIME[1])

    log.info(f"Current P&L: {pnl*100:.1f}% | Force: {force} | Past 3pm: {past_close_time}")

    should_close = force or past_close_time or pnl >= PROFIT_TARGET

    if should_close:
        reason = "forced" if force else ("3pm cutoff" if past_close_time else f"{pnl*100:.0f}% profit target")
        log.info(f"Closing — reason: {reason}")
        for pos in positions:
            try:
                trade_client.close_position(pos.symbol)
                log.info(f"✅ Closed {pos.symbol}")
            except Exception as e:
                log.error(f"Failed to close {pos.symbol}: {e}")
    else:
        log.info(f"Holding — P&L {pnl*100:.1f}% hasn't hit 50% yet and before 3pm.")


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
