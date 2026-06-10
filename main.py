from AlgorithmImports import *

class GoldBaselineIronCondor(QCAlgorithm):

    def Initialize(self):
        self.SetStartDate(2024, 1, 1)
        self.SetEndDate(2024, 6, 1)
        self.SetCash(100000)

        self.underlying = self.AddEquity("SPY", Resolution.Minute).Symbol

        option = self.AddOption("SPY", Resolution.Minute)
        self.option_symbol = option.Symbol

        option.SetFilter(lambda u: u.IncludeWeeklys()
                                   .Strikes(-12, 12)
                                   .Expiration(0, 1))

        self.last_trade_date = None

        self.SetWarmUp(20, Resolution.Daily)


    def OnData(self, slice):

        if self.IsWarmingUp:
            return

        if self.option_symbol not in slice.OptionChains:
            return

        if self.Portfolio.Invested:
            return

        if self.last_trade_date == self.Time.date():
            return

        chain = slice.OptionChains[self.option_symbol]
        contracts = list(chain)

        if len(contracts) < 10:
            return

        price = self.Securities[self.underlying].Price

        expiries = sorted(set([c.Expiry for c in contracts]))
        if not expiries:
            return

        expiry = expiries[0]
        contracts = [c for c in contracts if c.Expiry == expiry]

        puts = [c for c in contracts if c.Right == OptionRight.Put]
        calls = [c for c in contracts if c.Right == OptionRight.Call]

        if len(puts) < 2 or len(calls) < 2:
            return

        puts = sorted(puts, key=lambda x: x.Strike)
        calls = sorted(calls, key=lambda x: x.Strike)

        # SHORT STRIKES (closest available OTM)
        short_put = next((p for p in reversed(puts) if p.Strike < price), None)
        short_call = next((c for c in calls if c.Strike > price), None)

        if short_put is None or short_call is None:
            return

        # LONG WINGS (further OTM protection)
        long_put_candidates = [p for p in puts if p.Strike < short_put.Strike]
        long_call_candidates = [c for c in calls if c.Strike > short_call.Strike]

        if not long_put_candidates or not long_call_candidates:
            return

        long_put = long_put_candidates[0]
        long_call = long_call_candidates[-1]

        # FINAL SAFETY CHECK
        legs = [short_put, long_put, short_call, long_call]
        for leg in legs:
            if leg is None or leg.Symbol is None:
                return

        qty = 1

        # EXECUTE IRON CONDOR
        self.MarketOrder(short_put.Symbol, -qty)
        self.MarketOrder(long_put.Symbol, qty)

        self.MarketOrder(short_call.Symbol, -qty)
        self.MarketOrder(long_call.Symbol, qty)

        self.last_trade_date = self.Time.date()

        self.Debug(f"IC OPENED @ {price}")
