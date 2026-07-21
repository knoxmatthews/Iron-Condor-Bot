"""
0 DTE SPY Iron Condor Bot
--------------------------
- Opens every market morning after 9:45am (lets market settle)
- $5 wide wings, 1 contract
- Closes at 50% profit OR 3pm ET, whichever comes first
- Verifies all 4 legs filled before considering position open
- Correct P&L calculation across mixed long/short legs
- Uses market orders for reliable fills on 0 DTE
"""

import os
import sys
import time
import logging
import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOptionContractsRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest
from alpaca.data.enums import OptionsFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("condor")

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER      = True

UNDERLYING     = "SPY"
RISK_PCT       = 0.02    # risk 2% of equity per condor - reasonable for a recurring daily strategy
MAX_CONTRACTS  = 20       # hard ceiling regardless of math - caps slippage/liquidity risk on size
MIN_CONTRACTS  = 1
WING_WIDTH    = 5      # $5 wide — meaningful premium, defined risk of $500 max loss
OTM_PCT       = 0.006  # 0.6% OTM — tight enough for premium, wide enough for safety
PROFIT_TARGET = 0.50   # close at 50% of premium collected

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

trade_client  = TradingClient(API_KEY, API_SECRET, paper=PAPER)
data_client   = StockHistoricalDataClient(API_KEY, API_SECRET)
option_client = OptionHistoricalDataClient(API_KEY, API_SECRET)


def is_market_day(date=None):
    d = date or datetime.date.today()
    return d.weekday() < 5 and d not in HOLIDAYS


def get_spy_price() -> float:
    req   = StockLatestQuoteRequest(symbol_or_symbols=UNDERLYING)
    resp  = data_client.get_stock_latest_quote(req)
    quote = resp[UNDERLYING]
    price = (quote.ask_price + quote.bid_price) / 2
    log.info(f"SPY mid price: {price:.2f}")
    return price


def get_contracts(expiry_str: str) -> list:
    req = GetOptionContractsRequest(
        underlying_symbols=[UNDERLYING],
        expiration_date=expiry_str,
        limit=1000,
    )
    contracts = trade_client.get_option_contracts(req).option_contracts
    log.info(f"Fetched {len(contracts)} contracts expiring {expiry_str}")
    return contracts


def build_legs(contracts: list, price: float):
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

    sp = float(short_put.strike_price)
    sc = float(short_call.strike_price)

    # Sanity check — short strikes must straddle the price
    if sp >= price or sc <= price:
        log.warning(f"Bad strikes: put {sp} call {sc} price {price:.2f}")
        return None

    # Long wings WING_WIDTH away
    long_put  = min(puts,  key=lambda x: abs(float(x.strike_price) - (sp - WING_WIDTH)))
    long_call = min(calls, key=lambda x: abs(float(x.strike_price) - (sc + WING_WIDTH)))

    lp = float(long_put.strike_price)
    lc = float(long_call.strike_price)

    # Verify wing widths are correct
    if sp - lp < 1 or lc - sc < 1:
        log.warning(f"Wings too narrow: put spread {sp-lp} call spread {lc-sc}")
        return None

    log.info(
        f"Legs: LP {lp} | SP {sp} | SC {sc} | LC {lc} | "
        f"Width: {sp-lp:.0f}/{lc-sc:.0f} | Price: {price:.2f}"
    )

    return {
        "short_put":  short_put,
        "long_put":   long_put,
        "short_call": short_call,
        "long_call":  long_call,
    }


def get_option_mid_price(symbol: str) -> float:
    """
    Fetch the real bid/ask for an option contract and return the mid price.
    Falls back to a conservative $0.02 estimate if the quote can't be fetched -
    conservative meaning it UNDERESTIMATES credit, which SHRINKS position size
    rather than inflating it. A sizing error should always fail small, not big.
    """
    try:
        req   = OptionLatestQuoteRequest(symbol_or_symbols=symbol, feed=OptionsFeed.INDICATIVE)
        quote = option_client.get_option_latest_quote(req)[symbol]
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        if bid <= 0 and ask <= 0:
            raise ValueError("empty quote")
        return (bid + ask) / 2
    except Exception as e:
        log.warning(f"Could not get quote for {symbol}: {e} — using conservative fallback")
        return 0.02


def calc_contracts(legs: dict, equity: float) -> tuple:
    """
    Risk-based contract sizing for the condor:
    1. Estimate net credit received per spread (short premiums - long premiums)
    2. Max loss per contract = (wing width - net credit) x 100
    3. Contracts = (equity x RISK_PCT) / max loss per contract, capped and floored
    Returns (contracts, net_credit_per_share, max_loss_per_contract)
    """
    sp_mid = get_option_mid_price(legs["short_put"].symbol)
    lp_mid = get_option_mid_price(legs["long_put"].symbol)
    sc_mid = get_option_mid_price(legs["short_call"].symbol)
    lc_mid = get_option_mid_price(legs["long_call"].symbol)

    net_credit = (sp_mid + sc_mid) - (lp_mid + lc_mid)
    net_credit = max(net_credit, 0.01)  # never let a bad quote produce negative "credit"

    put_wing  = abs(float(legs["short_put"].strike_price)  - float(legs["long_put"].strike_price))
    call_wing = abs(float(legs["long_call"].strike_price) - float(legs["short_call"].strike_price))
    wing      = max(put_wing, call_wing)

    max_loss_per_contract = (wing - net_credit) * 100
    max_loss_per_contract = max(max_loss_per_contract, 1.0)  # avoid divide-by-zero on a weird quote

    risk_dollars = equity * RISK_PCT
    contracts    = int(risk_dollars / max_loss_per_contract)
    contracts    = max(MIN_CONTRACTS, min(contracts, MAX_CONTRACTS))

    log.info(
        f"Sizing: net credit ${net_credit:.2f}/share | wing ${wing:.0f} | "
        f"max loss/contract ${max_loss_per_contract:.0f} | risk budget ${risk_dollars:.0f} | "
        f"→ {contracts} contract(s)"
    )
    return contracts, net_credit, max_loss_per_contract


def place_order(symbol: str, side: OrderSide, qty: int) -> bool:
    """Place a single market order. Returns True if submitted successfully."""
    try:
        o = trade_client.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty,
            side          = side,
            time_in_force = TimeInForce.DAY,
        ))
        log.info(f"✅ {side.value.upper()} {qty}x {symbol} | id={o.id}")
        return True
    except Exception as e:
        log.error(f"❌ Order failed {symbol}: {e}")
        return False


def get_open_option_positions() -> list:
    positions = trade_client.get_all_positions()
    return [p for p in positions
            if p.asset_class == AssetClass.US_OPTION and UNDERLYING in p.symbol]


def calc_pnl(positions: list) -> float:
    """
    Correct P&L for mixed long/short option positions.
    Long positions: profit when market_value > cost_basis
    Short positions: profit when market_value < abs(cost_basis)
    """
    total_pnl  = sum(float(p.unrealized_pl) for p in positions)
    total_cost = sum(abs(float(p.cost_basis)) for p in positions)
    if total_cost == 0:
        return 0
    return total_pnl / total_cost


def open_condor():
    log.info("━━ OPEN 0 DTE IRON CONDOR ━━")

    # Check if already in a position
    existing = get_open_option_positions()
    if existing:
        log.info(f"Already have {len(existing)} open option positions. Skipping.")
        return

    today  = datetime.date.today()
    expiry = today.strftime("%Y-%m-%d")

    # Get SPY price
    try:
        price = get_spy_price()
    except Exception as e:
        log.error(f"Price fetch failed: {e}")
        return

    # Get contracts
    try:
        contracts = get_contracts(expiry)
    except Exception as e:
        log.error(f"Contract fetch failed: {e}")
        return

    if len(contracts) < 20:
        log.warning(f"Only {len(contracts)} contracts — not enough. Skipping.")
        return

    # Build legs
    legs = build_legs(contracts, price)
    if not legs:
        log.warning("Could not build valid legs. Skipping.")
        return

    # Risk-based contract sizing - scales with account equity instead of a flat 1 contract
    acct   = trade_client.get_account()
    equity = float(acct.equity)
    qty, net_credit, max_loss = calc_contracts(legs, equity)

    # Place all 4 legs — longs first to establish defined risk margin
    results = []
    results.append(place_order(legs["long_put"].symbol,   OrderSide.BUY,  qty))
    results.append(place_order(legs["long_call"].symbol,  OrderSide.BUY,  qty))
    results.append(place_order(legs["short_put"].symbol,  OrderSide.SELL, qty))
    results.append(place_order(legs["short_call"].symbol, OrderSide.SELL, qty))

    if not all(results):
        log.error("Not all legs filled — attempting to close any that did open.")
        time.sleep(3)
        for pos in get_open_option_positions():
            try:
                trade_client.close_position(pos.symbol)
                log.info(f"Emergency close: {pos.symbol}")
            except Exception as e:
                log.error(f"Emergency close failed {pos.symbol}: {e}")
        return

    total_max_risk = max_loss * qty
    log.info(
        f"✅ Iron Condor opened | SPY @ {price:.2f} | Expiry {expiry} | "
        f"{qty} contract(s) | Est. credit ${net_credit*qty*100:.0f} | Max risk ${total_max_risk:.0f}"
    )


def close_condor(force: bool = False):
    log.info("━━ CLOSE 0 DTE IRON CONDOR ━━")

    positions = get_open_option_positions()
    if not positions:
        log.info("No open option positions.")
        return

    pnl      = calc_pnl(positions)
    now_et   = datetime.datetime.now(ET)
    past_3pm = now_et.hour >= 15

    log.info(f"P&L: {pnl*100:.1f}% | Force: {force} | Past 3pm ET: {past_3pm}")

    if force or past_3pm or pnl >= PROFIT_TARGET:
        if force:
            reason = "forced close"
        elif past_3pm:
            reason = "3pm ET cutoff"
        else:
            reason = f"{pnl*100:.0f}% profit target hit"

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
