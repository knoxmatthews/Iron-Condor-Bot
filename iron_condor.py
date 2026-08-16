#!/usr/bin/env python3
"""
Iron Condor / Directional Credit Spread Bot
SPY, 0DTE, Alpaca Paper Trading

STRATEGY
--------
1. Pull SPY's 5-minute bars since today's open and compute ADX(14) with
   +DI/-DI to gauge how strongly the market is trending right now.
2. NOT strongly trending  -> sell a full iron condor (put spread + call spread).
3. Strongly trending UP   -> sell only a put credit spread (bullish, defined risk).
4. Strongly trending DOWN -> sell only a call credit spread (bearish, defined risk).
5. If either side of a condor can't actually be built (no contract near the
   target delta, or the credit is too thin to be worth the risk), the bot
   drops that side and sells the other side alone instead of forcing a condor.
6. Position size is derived from a per-trade risk budget (% of account
   equity), aimed at a $100/day target on a ~$5k-10k account. See the sizing
   note at the bottom of this file before you run it live.

Run this once, near/after market open, via cron / GitHub Actions / Railway --
same pattern as your other bots. All orders are 0DTE so there's no separate
close-out logic needed beyond expiration; add a stop-loss check yourself if
you want to bail early on a blown-through spread (noted below).

ENV VARS REQUIRED
------------------
ALPACA_API_KEY
ALPACA_API_SECRET
"""

import os
import sys
import math
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, ContractType

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest, OptionChainRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ============================================================
# CONFIG -- tune these
# ============================================================
API_KEY = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]

SYMBOL = "SPY"
WING_WIDTH = 2.0                  # $ width of each spread (matches your existing condor)
TARGET_DAILY_PROFIT = 100.0       # $ goal per day -- informational, see note at bottom
ACCOUNT_EQUITY_FALLBACK = 7500.0  # used only if the account-equity API call fails
RISK_PER_TRADE_PCT = 0.05         # % of equity risked on today's trade(s) -- see sizing note
MIN_CREDIT_PER_CONTRACT = 0.15    # don't sell a spread collecting less than this ($/share)
SHORT_LEG_DELTA_TARGET = 0.16     # ~16-delta short strikes, standard condor delta
DELTA_TOLERANCE = 0.08            # how far from target delta a strike can be and still count

ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25          # ADX >= this = "strongly trending" -> single spread, not condor
BARS_TIMEFRAME = TimeFrame(5, TimeFrameUnit.Minute)

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
stock_data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
option_data_client = OptionHistoricalDataClient(API_KEY, API_SECRET)


# ============================================================
# TREND DETECTION (Wilder's ADX / +DI / -DI, pure python)
# ============================================================
def get_bars_since_open():
    now = datetime.now(timezone.utc)
    market_open_utc = now.replace(hour=13, minute=30, second=0, microsecond=0)  # 9:30am ET
    req = StockBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=BARS_TIMEFRAME,
        start=market_open_utc,
    )
    bars = stock_data_client.get_stock_bars(req)
    return list(bars[SYMBOL])


def compute_adx(bars, period=ADX_PERIOD):
    """Returns (adx, plus_di, minus_di) or (None, None, None) if not enough bars yet."""
    if len(bars) < period + 1:
        return None, None, None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        ))

    def wilder_smooth(values, period):
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    tr_s = wilder_smooth(trs, period)
    pdm_s = wilder_smooth(plus_dm, period)
    mdm_s = wilder_smooth(minus_dm, period)

    plus_di = [100 * (p / t) if t else 0.0 for p, t in zip(pdm_s, tr_s)]
    minus_di = [100 * (m / t) if t else 0.0 for m, t in zip(mdm_s, tr_s)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) else 0.0 for p, m in zip(plus_di, minus_di)]

    if len(dx) < period:
        return None, plus_di[-1], minus_di[-1]

    adx = sum(dx[:period]) / period
    for d in dx[period:]:
        adx = (adx * (period - 1) + d) / period

    return adx, plus_di[-1], minus_di[-1]


def decide_mode():
    """Returns one of: 'condor', 'bull_spread' (sell put spread), 'bear_spread' (sell call spread)."""
    bars = get_bars_since_open()
    adx, plus_di, minus_di = compute_adx(bars)

    if adx is None:
        print("Not enough bars yet for ADX -- defaulting to condor.")
        return "condor"

    print(f"ADX={adx:.1f}  +DI={plus_di:.1f}  -DI={minus_di:.1f}")

    if adx >= ADX_TREND_THRESHOLD:
        return "bull_spread" if plus_di > minus_di else "bear_spread"
    return "condor"


# ============================================================
# OPTION CHAIN / STRIKE SELECTION
# ============================================================
def get_underlying_price():
    req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL)
    trade = stock_data_client.get_stock_latest_trade(req)
    return trade[SYMBOL].price


def todays_expiration_str():
    return datetime.now().strftime("%Y-%m-%d")


def build_credit_spread(side, current_price):
    """
    side: 'put' (sell put spread, bullish) or 'call' (sell call spread, bearish)
    Returns dict {short_symbol, long_symbol, credit} or None if no valid spread
    can be built (bad chain, no strike near target delta, credit too thin).
    """
    contract_type = ContractType.PUT if side == "put" else ContractType.CALL
    expiration = todays_expiration_str()

    try:
        chain = option_data_client.get_option_chain(
            OptionChainRequest(
                underlying_symbol=SYMBOL,
                expiration_date=expiration,
                type=contract_type,
            )
        )
    except Exception as e:
        print(f"Chain fetch failed for {side}: {e}")
        return None

    # Only OTM contracts, with usable delta + quote data
    candidates = []
    for symbol, snap in chain.items():
        if snap.greeks is None or snap.latest_quote is None:
            continue
        delta = abs(snap.greeks.delta)
        strike = float(symbol[-8:]) / 1000  # OCC symbol: last 8 digits = strike * 1000
        is_otm = strike < current_price if side == "put" else strike > current_price
        if is_otm:
            candidates.append((symbol, strike, delta, snap.latest_quote))

    if not candidates:
        print(f"No OTM {side} contracts with quote/greek data.")
        return None

    # Short leg: closest delta to target, within tolerance
    short_symbol, short_strike, short_delta, short_quote = min(
        candidates, key=lambda c: abs(c[2] - SHORT_LEG_DELTA_TARGET)
    )
    if abs(short_delta - SHORT_LEG_DELTA_TARGET) > DELTA_TOLERANCE:
        print(f"No {side} strike close enough to {SHORT_LEG_DELTA_TARGET} delta "
              f"(closest was {short_delta:.2f}).")
        return None

    # Long leg: WING_WIDTH further out
    target_long_strike = short_strike - WING_WIDTH if side == "put" else short_strike + WING_WIDTH
    long_matches = [c for c in candidates if math.isclose(c[1], target_long_strike, abs_tol=0.01)]
    if not long_matches:
        print(f"No {side} contract at the {WING_WIDTH}-wide long strike ({target_long_strike}).")
        return None
    long_symbol, long_strike, long_delta, long_quote = long_matches[0]

    short_mid = (short_quote.bid_price + short_quote.ask_price) / 2
    long_mid = (long_quote.bid_price + long_quote.ask_price) / 2
    credit = round(short_mid - long_mid, 2)

    if credit < MIN_CREDIT_PER_CONTRACT:
        print(f"{side} spread credit too thin (${credit:.2f} < ${MIN_CREDIT_PER_CONTRACT}).")
        return None

    return {"short_symbol": short_symbol, "long_symbol": long_symbol, "credit": credit}


# ============================================================
# SIZING
# ============================================================
def get_account_equity():
    try:
        return float(trading_client.get_account().equity)
    except Exception as e:
        print(f"Couldn't fetch account equity ({e}), using fallback ${ACCOUNT_EQUITY_FALLBACK}.")
        return ACCOUNT_EQUITY_FALLBACK


def calc_contracts(total_credit_per_contract, equity):
    """Max loss per contract for a single spread or a condor (only one side can
    breach at expiration, so wing width minus TOTAL credit collected is the
    right number either way)."""
    max_risk_per_contract = (WING_WIDTH - total_credit_per_contract) * 100
    if max_risk_per_contract <= 0:
        return 0
    risk_budget = equity * RISK_PER_TRADE_PCT
    return max(1, math.floor(risk_budget / max_risk_per_contract))


# ============================================================
# ORDER SUBMISSION
# ============================================================
def submit_spread(short_symbol, long_symbol, qty, credit):
    legs = [
        OptionLegRequest(symbol=short_symbol, side=OrderSide.SELL, ratio_qty=1),
        OptionLegRequest(symbol=long_symbol, side=OrderSide.BUY, ratio_qty=1),
    ]
    order = LimitOrderRequest(
        qty=qty,
        limit_price=credit,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        legs=legs,
    )
    result = trading_client.submit_order(order)
    print(f"Submitted: {short_symbol}/{long_symbol} x{qty} @ ${credit} credit -> order {result.id}")
    return result


def submit_condor(put_spread, call_spread, qty):
    legs = [
        OptionLegRequest(symbol=put_spread["short_symbol"], side=OrderSide.SELL, ratio_qty=1),
        OptionLegRequest(symbol=put_spread["long_symbol"], side=OrderSide.BUY, ratio_qty=1),
        OptionLegRequest(symbol=call_spread["short_symbol"], side=OrderSide.SELL, ratio_qty=1),
        OptionLegRequest(symbol=call_spread["long_symbol"], side=OrderSide.BUY, ratio_qty=1),
    ]
    total_credit = round(put_spread["credit"] + call_spread["credit"], 2)
    order = LimitOrderRequest(
        qty=qty,
        limit_price=total_credit,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        legs=legs,
    )
    result = trading_client.submit_order(order)
    print(f"Submitted condor x{qty} @ ${total_credit} total credit -> order {result.id}")
    return result


# ============================================================
# MAIN
# ============================================================
def main():
    mode = decide_mode()
    current_price = get_underlying_price()
    equity = get_account_equity()
    print(f"Mode: {mode}  |  SPY: {current_price}  |  Equity: ${equity:.2f}")

    if mode == "bull_spread":
        spread = build_credit_spread("put", current_price)
        if not spread:
            print("Bullish trend but no viable put spread found. Standing down for today.")
            return
        qty = calc_contracts(spread["credit"], equity)
        submit_spread(spread["short_symbol"], spread["long_symbol"], qty, spread["credit"])
        return

    if mode == "bear_spread":
        spread = build_credit_spread("call", current_price)
        if not spread:
            print("Bearish trend but no viable call spread found. Standing down for today.")
            return
        qty = calc_contracts(spread["credit"], equity)
        submit_spread(spread["short_symbol"], spread["long_symbol"], qty, spread["credit"])
        return

    # mode == "condor": try both sides, fall back to whichever side actually works
    put_spread = build_credit_spread("put", current_price)
    call_spread = build_credit_spread("call", current_price)

    if put_spread and call_spread:
        qty = calc_contracts(put_spread["credit"] + call_spread["credit"], equity)
        submit_condor(put_spread, call_spread, qty)
    elif put_spread:
        print("Call side of the condor wasn't buildable -- selling the put spread alone.")
        qty = calc_contracts(put_spread["credit"], equity)
        submit_spread(put_spread["short_symbol"], put_spread["long_symbol"], qty, put_spread["credit"])
    elif call_spread:
        print("Put side of the condor wasn't buildable -- selling the call spread alone.")
        qty = calc_contracts(call_spread["credit"], equity)
        submit_spread(call_spread["short_symbol"], call_spread["long_symbol"], qty, call_spread["credit"])
    else:
        print("Neither side of the condor was buildable today. Standing down.")


if __name__ == "__main__":
    main()


# ============================================================
# SIZING NOTE -- read before you scale this up
# ============================================================
# $100/day on a $5k-10k account is 1-2% of the account, EVERY day. RISK_PER_TRADE_PCT
# is set to 5% here, which is what it takes to get contract counts large enough to hit
# $100 on a good day -- but it means a handful of max-loss trend days (the exact
# condition that triggers "bull_spread"/"bear_spread" mode) can erase a week or more
# of $100 wins. There's no sizing knob that removes that tradeoff; it's the cost of
# targeting that daily $ number on that account size. Worth watching win rate and max
# drawdown in paper before pushing RISK_PER_TRADE_PCT higher.
