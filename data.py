"""
ORB backtest that reports its own measurement error.

Every trade is scored twice:

  optimistic   a bar spanning both levels is a WIN   (TP touched first)
  pessimistic  a bar spanning both levels is a LOSS  (stop touched first)

5-min OHLC cannot distinguish these. The true win rate lies between the two
bounds; the gap between them is the uncertainty of the instrument, and it is
only meaningful to draw a conclusion when that gap is smaller than the effect
being measured. AMBIG% reports how often the ambiguity actually bites.

Both entry arms share identical filters, stop placement, sizing and evaluation:

  require_retest=True   entry = confirmation candle close   (current strategy)
  require_retest=False  entry = breakout candle close       (momentum variant)

Monte Carlo uses replace=True. replace=False with size=len(r) is a permutation:
every run sums to the same total, so "probability of profit" is 100% by
construction and tells you nothing.
"""

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime, timezone, date
import math
import matplotlib.pyplot as plt
import numpy as np
from config import *

ACCOUNT_SIZE = 100000
RISK_PERCENT = 0.01
SYMBOLS = ["QQQ", "SPY", "IWM", "TQQQ"]

MAX_POSITION_PCT = 0.20     # position value cap, matches bot.py
MIN_DISTANCE = 0.03         # minimum entry-to-stop gap, matches bot.py

# STOP_MODE controls where the stop sits relative to the breakout candle.
#   "mid"    (breakout_high + breakout_low) / 2      - current
#   "extreme" breakout candle low (long) / high (short) - roughly 2x wider
# Wider stops produce fewer ambiguous bars, i.e. a narrower error bar.
STOP_MODE = "mid"

HOLDOUT_START = date(2024, 1, 1)   # untouched until the train period is decided

stock_client = StockHistoricalDataClient(api_key, secret_key)

request_params_five_mins = StockBarsRequest(
    symbol_or_symbols=SYMBOLS,
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=datetime(2017, 2, 2, tzinfo=timezone.utc),
    end=datetime(2026, 4, 4, tzinfo=timezone.utc),
)

request_params_daily = StockBarsRequest(
    symbol_or_symbols=SYMBOLS,
    timeframe=TimeFrame(1, TimeFrameUnit.Day),
    start=datetime(2017, 2, 2, tzinfo=timezone.utc),
    end=datetime(2026, 4, 4, tzinfo=timezone.utc),
)

daily_bars = stock_client.get_stock_bars(request_params_daily)
five_min_bars = stock_client.get_stock_bars(request_params_five_mins)


def _find_retest(bars_day, breakout_time, direction, OR_high, OR_low):
    """First candle after the breakout that pulls back to the broken level and
    still closes beyond it, within the 10:30 window. None if it never happens."""
    for j, candle in bars_day[bars_day.index > breakout_time].iterrows():
        if j.hour > 10 or (j.hour == 10 and j.minute > 30):
            return None
        if direction == 'long' and candle["low"] <= OR_high and candle["close"] > OR_high:
            return j
        if direction == 'short' and candle["high"] >= OR_low and candle["close"] < OR_low:
            return j
    return None


def _evaluate(trade_bars, direction, take_profit, stop):
    """Returns (pessimistic, optimistic, ambiguous).

    A bar that touches both levels is unresolvable from OHLC, so we report it
    both ways rather than picking one and calling it a measurement."""
    for _, c in trade_bars.iterrows():
        if direction == 'long':
            hit_stop = c["low"] <= stop
            hit_tp = c["high"] >= take_profit
        else:
            hit_stop = c["high"] >= stop
            hit_tp = c["low"] <= take_profit

        if hit_stop and hit_tp:
            return "Loss", "Win", True
        if hit_stop:
            return "Loss", "Loss", False
        if hit_tp:
            return "Win", "Win", False

    return "Open", "Open", False


def run_strategy(bars_day, daily_df, require_retest):
    today = bars_day.index[0].date()
    prev_day_data = daily_df[daily_df.index.date < today]

    if len(prev_day_data) == 0:
        return None

    prev_close = prev_day_data.iloc[-1]["close"]

    open_bar = bars_day.between_time("09:30", "09:30")
    if len(open_bar) == 0:
        return None

    OR_high = open_bar.iloc[0]["high"]
    OR_low = open_bar.iloc[0]["low"]

    if (OR_high - OR_low) / OR_low * 100 < 0.10:
        return None

    today_open = open_bar.iloc[0]["open"]
    if abs(today_open - prev_close) / prev_close * 100 > 0.7:
        return None

    # --- Breakout ---
    breakout_time = None
    direction = None
    for i, candle in bars_day.between_time("09:35", "16:00").iterrows():
        if candle["close"] > OR_high:
            breakout_time, direction = i, 'long'
            break
        if candle["close"] < OR_low:
            breakout_time, direction = i, 'short'
            break

    if breakout_time is None:
        return None

    breakout_candle = bars_day.loc[breakout_time]

    if STOP_MODE == "mid":
        stop = (breakout_candle["high"] + breakout_candle["low"]) / 2
    elif STOP_MODE == "extreme":
        stop = breakout_candle["low"] if direction == 'long' else breakout_candle["high"]
    else:
        raise ValueError(f"unknown STOP_MODE {STOP_MODE!r}")

    # Tag the retest regardless of arm, so the no-retest arm can be split on it.
    retest = _find_retest(bars_day, breakout_time, direction, OR_high, OR_low)

    # --- Entry ---
    if require_retest:
        if retest is None:
            return None
        trade_bars = bars_day[bars_day.index > retest]
        if len(trade_bars) == 0:
            return None
        confirmation_candle = trade_bars.iloc[0]
        if direction == 'long' and confirmation_candle["close"] <= OR_high:
            return None
        if direction == 'short' and confirmation_candle["close"] >= OR_low:
            return None
        entry = confirmation_candle["close"]
        eval_bars = trade_bars.iloc[1:]          # skip the confirmation candle
    else:
        entry = breakout_candle["close"]
        eval_bars = bars_day[bars_day.index > breakout_time]
        if len(eval_bars) == 0:
            return None

    # --- Stop side / distance guard ---
    if direction == 'long':
        if stop >= entry or (entry - stop) < MIN_DISTANCE:
            return None
        risk_per_share = entry - stop
        take_profit = entry + risk_per_share
    else:
        if stop <= entry or (stop - entry) < MIN_DISTANCE:
            return None
        risk_per_share = stop - entry
        take_profit = entry - risk_per_share

    # --- Sizing ---
    shares = math.floor((ACCOUNT_SIZE * RISK_PERCENT) / risk_per_share)
    shares = min(shares, 1000, int((ACCOUNT_SIZE * MAX_POSITION_PCT) / entry))
    if shares == 0:
        return None

    pess, opt, ambig = _evaluate(eval_bars, direction, take_profit, stop)
    if pess == "Open":
        return None

    return {
        "direction": direction,
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "take_profit": round(take_profit, 4),
        "risk_per_share": round(risk_per_share, 4),
        "shares": shares,
        "retested": bool(retest is not None),
        "pess": pess,
        "opt": opt,
        "ambiguous": bool(ambig),
    }


def collect(require_retest):
    rows = []
    for symbol in SYMBOLS:
        df_daily = daily_bars.df.loc[symbol].sort_index().tz_convert("America/New_York")
        df_five = five_min_bars.df.loc[symbol].sort_index().tz_convert("America/New_York")
        bars_market = df_five.between_time("09:30", "16:00")

        for day_date, group in bars_market.groupby(bars_market.index.date):
            r = run_strategy(group, df_daily, require_retest)
            if r is None:
                continue
            r["date"] = day_date
            r["symbol"] = symbol
            rows.append(r)

    df = pd.DataFrame(rows)
    if len(df) == 0:
        return df

    df["retested"] = df["retested"].astype(bool)
    df["ambiguous"] = df["ambiguous"].astype(bool)
    df["r_pess"] = np.where(df["pess"] == "Win", 1, -1)
    df["r_opt"] = np.where(df["opt"] == "Win", 1, -1)
    return df.sort_values("date").reset_index(drop=True)


def summarise(df, label):
    n = len(df)
    if n == 0:
        print(f"{label:24} no trades")
        return

    wr_p = (df["pess"] == "Win").mean() * 100
    wr_o = (df["opt"] == "Win").mean() * 100
    z_p = (wr_p / 100 - 0.5) / (0.25 / n) ** 0.5
    z_o = (wr_o / 100 - 0.5) / (0.25 / n) ** 0.5

    print(f"{label:24} {n:5} | WR {wr_p:5.1f}% - {wr_o:5.1f}% "
          f"| R {df['r_pess'].sum():+6.0f} - {df['r_opt'].sum():+6.0f} "
          f"| z {z_p:+6.2f} - {z_o:+6.2f} "
          f"| ambig {df['ambiguous'].mean() * 100:4.1f}%")


def bootstrap(df, label, n_sim=10000):
    if len(df) == 0:
        return
    print(f"\n--- Bootstrap: {label} ({n_sim:,} runs, replace=True) ---")
    for col, name in (("r_pess", "pessimistic"), ("r_opt", "optimistic")):
        r = df[col].values
        ends, dds = [], []
        for _ in range(n_sim):
            c = np.cumsum(np.random.choice(r, size=len(r), replace=True))
            ends.append(c[-1])
            dds.append(np.max(np.maximum.accumulate(c) - c))
        ends, dds = np.array(ends), np.array(dds)
        print(f"  {name:12} final R  5th {np.percentile(ends,5):+6.0f} | "
              f"median {np.median(ends):+6.0f} | 95th {np.percentile(ends,95):+6.0f} "
              f"| P(profit) {(ends > 0).mean() * 100:5.1f}% "
              f"| maxDD median {np.median(dds):4.0f}R")


# ---------------------------------------------------------------------------

print(f"STOP_MODE = {STOP_MODE!r}")
print("WR / R / z are reported as  pessimistic - optimistic  bounds.")
print("The truth is somewhere between. AMBIG% is how often the bar couldn't say.")

retest_df = collect(require_retest=True)
noretest_df = collect(require_retest=False)

for name, df in (("RETEST (current)", retest_df), ("NO RETEST", noretest_df)):
    if len(df) == 0:
        continue
    train = df[df["date"] < HOLDOUT_START]
    hold = df[df["date"] >= HOLDOUT_START]

    print(f"\n=== {name} ===")
    summarise(train, "  TRAIN 2017-2023")
    summarise(hold, "  HOLDOUT 2024-2026")
    summarise(df, "  ALL")

    print("  by symbol (train):")
    for sym in SYMBOLS:
        summarise(train[train["symbol"] == sym], f"    {sym}")

    rps = df["risk_per_share"]
    print(f"  risk/share: median ${rps.median():.3f} | mean ${rps.mean():.3f} "
          f"| under $0.26: {(rps < 0.26).mean() * 100:.0f}%")

    amb = df[df["ambiguous"]]
    print(f"  ambiguous trades: {len(amb)} ({len(amb) / len(df) * 100:.1f}%) "
          f"- these alone account for the whole WR spread")

# The Reddit claim: breakouts that never pull back outperform the ones that do.
if len(noretest_df):
    print("\n=== Does a later retest predict anything? (no-retest arm, train only) ===")
    tnr = noretest_df[noretest_df["date"] < HOLDOUT_START]
    summarise(tnr[~tnr["retested"]], "  never retested")
    summarise(tnr[tnr["retested"]], "  retested later")
    print("  NOTE: trades resolving before a retest could occur are tagged 'never")
    print("  retested' by construction. Suggestive only.")

    bootstrap(noretest_df[noretest_df["date"] < HOLDOUT_START], "NO RETEST, train")

if len(retest_df):
    bootstrap(retest_df[retest_df["date"] < HOLDOUT_START], "RETEST, train")

for name, df in (("Retest", retest_df), ("No retest", noretest_df)):
    if len(df) == 0:
        continue
    plt.figure(figsize=(12, 5))
    plt.plot(df["date"], df["r_opt"].cumsum(), label="optimistic (TP first)", alpha=0.8)
    plt.plot(df["date"], df["r_pess"].cumsum(), label="pessimistic (stop first)", alpha=0.8)
    plt.fill_between(df["date"], df["r_pess"].cumsum(), df["r_opt"].cumsum(),
                     alpha=0.15, label="what the data can't resolve")
    plt.axvline(x=HOLDOUT_START, color='orange', linestyle='--', alpha=0.7, label='holdout starts')
    plt.axhline(y=0, color='r', linestyle='--', alpha=0.5)
    plt.title(f"{name} entry, STOP_MODE={STOP_MODE} - true curve lies inside the band")
    plt.xlabel("Date")
    plt.ylabel("Cumulative R")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()

plt.show()
