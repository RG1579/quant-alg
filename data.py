import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime, timezone
import math
import matplotlib.pyplot as plt
import numpy as np
from config import *

ACCOUNT_SIZE = 100000
RISK_PERCENT = 0.01
SYMBOLS = ["QQQ", "SPY", "IWM", "TQQQ"]

stock_client = StockHistoricalDataClient(api_key, secret_key)

request_params_five_mins = StockBarsRequest(
    symbol_or_symbols=SYMBOLS,
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=datetime(2017, 1, 4, tzinfo=timezone.utc),    
    end=datetime(2026, 1, 5, tzinfo=timezone.utc),
)

request_params_daily = StockBarsRequest(
    symbol_or_symbols=SYMBOLS,
    timeframe=TimeFrame(1, TimeFrameUnit.Day),
    start=datetime(2021, 1, 4, tzinfo=timezone.utc),    
    end=datetime(2026, 1, 5, tzinfo=timezone.utc),
)

daily_bars = stock_client.get_stock_bars(request_params_daily)
five_min_bars = stock_client.get_stock_bars(request_params_five_mins)

def run_strategy(bars_day, daily_df):

    today = bars_day.index[0].date()
    prev_day_data = daily_df[daily_df.index.date < today]

    if len(prev_day_data) == 0:
        return {"result": "No setup"}

    prev_close = prev_day_data.iloc[-1]["close"]
    prev_open = prev_day_data.iloc[-1]["open"]

    open_bar = bars_day.between_time("09:30", "09:30")
    high_low_df = open_bar[["high", "low"]]
    OR_high = high_low_df.iloc[0]["high"]
    OR_low = high_low_df.iloc[0]["low"]
    OR_range = OR_high - OR_low

    OR_range_pct = OR_range / OR_low * 100

    if OR_range_pct < 0.10:
        return {"result": "No setup"}
    
    today_open = open_bar.iloc[0]["open"]
    gap_pct = abs(today_open - prev_close) / prev_close * 100

    if gap_pct > 0.7:
        return {"result": "No setup"}
    
    prev_day_bullish = prev_close > prev_open

    next_bars = bars_day.iloc[1:]

    for i, candle in next_bars.iterrows():
        if candle["close"] > OR_high:
            breakout_time = i
            direction = 'long' 
            break
        elif candle["close"] < OR_low:
            breakout_time = i 
            direction = 'short'
            break
        else:
            result = "No setup"

    if 'breakout_time' not in locals():
        return {"result": "No setup"}
    
    if direction == "long" and not prev_day_bullish:
        return {"result": "No setup"}
    if direction == "short" and prev_day_bullish:
        return {"result": "No setup"}
            
    post_breakout = bars_day[bars_day.index > breakout_time]

    for j, candle in post_breakout.iterrows():
        if j.hour > 10 or (j.hour == 10 and j.minute > 30):
            break
        if candle["low"] <= OR_high and candle["close"] > OR_high:
            retest = j
            break
        elif candle["high"] >= OR_low and candle["close"] < OR_low:
            retest = j
            break
        else:
            result = "No setup"

    if 'retest' not in locals():
        return {"result": "No setup"}

    retest_candle = bars_day.loc[retest]
    trade_bars = bars_day[bars_day.index > retest]
    post_retest_close = trade_bars.iloc[0]["close"]

    entry = retest_candle["close"]
        
    if direction == "long" and post_retest_close > OR_high:
        stop = retest_candle["low"]
        risk_per_share = abs(entry - stop)
        if risk_per_share == 0:
            return {"result": "No setup"}
        take_profit = entry + risk_per_share*2
    elif direction == "short" and post_retest_close < OR_low:
        stop = retest_candle["high"]
        risk_per_share = abs(entry - stop)
        if risk_per_share == 0:
            return {"result": "No setup"}
        take_profit = entry - risk_per_share*2
    else:
        return {"result": "No setup"}

    risk_amount = ACCOUNT_SIZE * RISK_PERCENT
    shares = min(math.floor(risk_amount / risk_per_share), 1000)

    confirmation_time = trade_bars.index[0]
    trade_bars = trade_bars[trade_bars.index > confirmation_time]

    if direction == 'short':
        for l, candle in trade_bars.iterrows():
            if candle["low"] <= take_profit:
                result = "Win"
                break
            elif candle["high"] >= stop:
                result = "Loss"
                break
        else:
            result = "Open"
    elif direction == 'long':
        for k, candle in trade_bars.iterrows():
            if candle["high"] >= take_profit:
                result = "Win"
                break
            elif candle["low"] <= stop:
                result = "Loss"
                break
        else:
            result = "Open"

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "take_profit": take_profit,
        "shares": shares,
        "result": result
    }

results = []

for symbol in SYMBOLS:
    df_daily = daily_bars.df.loc[symbol].sort_index().tz_convert("America/New_York")
    df_five_mins = five_min_bars.df.loc[symbol].sort_index()
    df_ny = df_five_mins.tz_convert("America/New_York")
    bars_day = df_ny.between_time("09:30", "19:00")

    for date, group in bars_day.groupby(bars_day.index.date):
        result = run_strategy(group, df_daily)
        result["date"] = date
        result["symbol"] = symbol
        results.append(result)

results_df = pd.DataFrame(results)
print(results_df)

trades = results_df[results_df["result"].isin(["Win", "Loss"])]

total_trades = len(trades)
wins = len(trades[trades["result"] == "Win"])
losses = len(trades[trades["result"] == "Loss"])
win_rate = wins / total_trades * 100
total_r = (wins * 2) - (losses * 1)
avg_r = total_r / total_trades

print(f"\n--- Backtest Results (All Symbols) ---")
print(f"Total trades: {total_trades}")
print(f"Wins: {wins} | Losses: {losses}")
print(f"Win rate: {win_rate:.1f}%")
print(f"Total R: {total_r:.1f}R")
print(f"Average R per trade: {avg_r:.2f}R")

print(f"\n--- Results By Symbol ---")
for symbol in SYMBOLS:
    sym_trades = trades[trades["symbol"] == symbol]
    sym_wins = len(sym_trades[sym_trades["result"] == "Win"])
    sym_losses = len(sym_trades[sym_trades["result"] == "Loss"])
    sym_total = len(sym_trades)
    sym_wr = sym_wins / sym_total * 100 if sym_total > 0 else 0
    sym_r = (sym_wins * 2) - sym_losses
    print(f"{symbol}: {sym_total} trades | {sym_wr:.1f}% WR | {sym_r:.1f}R")

trades = trades.copy()
trades["r"] = trades["result"].apply(lambda x: 2 if x == "Win" else -1)
trades["cumulative_r"] = trades["r"].cumsum()

plt.figure(figsize=(12, 6))
plt.plot(trades["date"], trades["cumulative_r"])
plt.axhline(y=0, color='r', linestyle='--', alpha=0.5)
plt.title("Equity Curve - Cumulative R (All Symbols)")
plt.xlabel("Date")
plt.ylabel("Cumulative R")
plt.grid(True, alpha=0.3)
plt.tight_layout()

import numpy as np

def monte_carlo_simulation(trades_df, n_simulations=10000):
    r_values = trades_df["r"].values
    n_trades = len(r_values)
    
    simulation_results = []
    max_drawdowns = []
    all_curves = []  # store curves for plotting
    
    for _ in range(n_simulations):
        shuffled = np.random.choice(r_values, size=n_trades, replace=False)
        cumulative = np.cumsum(shuffled)
        
        peak = np.maximum.accumulate(cumulative)
        drawdown = peak - cumulative
        max_drawdowns.append(np.max(drawdown))
        simulation_results.append(cumulative[-1])
        all_curves.append(cumulative)  # save the curve
    
    simulation_results = np.array(simulation_results)
    max_drawdowns = np.array(max_drawdowns)
    
    print(f"\n--- Monte Carlo Results ({n_simulations:,} simulations) ---")
    print(f"Median final R: {np.median(simulation_results):.1f}R")
    print(f"Best case (95th percentile): {np.percentile(simulation_results, 95):.1f}R")
    print(f"Worst case (5th percentile): {np.percentile(simulation_results, 5):.1f}R")
    print(f"Probability of profit: {(simulation_results > 0).mean() * 100:.1f}%")
    print(f"\nMax Drawdown:")
    print(f"  Median: {np.median(max_drawdowns):.1f}R")
    print(f"  Worst case (95th percentile): {np.percentile(max_drawdowns, 95):.1f}R")
    
    plt.figure(figsize=(12, 6))
    for curve in all_curves[:200]:  # plot first 200 from same simulations
        plt.plot(curve, alpha=0.05, color='blue', linewidth=0.5)
    
    plt.axhline(y=0, color='r', linestyle='--', alpha=0.5)
    plt.axhline(y=np.median(simulation_results), color='g', 
                linestyle='--', alpha=0.8, label=f'Median: {np.median(simulation_results):.1f}R')
    plt.title("Monte Carlo Simulation - 200 Random Trade Orderings")
    plt.xlabel("Trade Number")
    plt.ylabel("Cumulative R")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.show()

monte_carlo_simulation(trades)