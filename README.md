# ORB trading strategy — build, validation, and why it was discontinued

A rule-based intraday Opening Range Breakout strategy in Python, with a live paper-trading bot, a replay harness, and a pre-registered validation experiment.

**The strategy was discontinued.** A head-to-head test against a control arm revealed two biases in my own earlier backtest. Correcting them removed the apparent edge entirely. This repo is kept as a record of the methodology and of how the result was overturned.

## The strategy

Trades QQQ, SPY, IWM and TQQQ on 5-minute bars through the Alpaca API.

1. The 09:30–09:35 bar defines the opening range (OR high and OR low)
2. Skip the day if the range is under 0.10% wide, or if the open gapped more than 0.7% from the previous close
3. Wait for a 5-minute close beyond the OR — that's the breakout
4. Wait for a pullback to the broken level that still closes beyond it — the retest
5. Enter at the close of the next confirming candle, before 10:30
6. Stop at the breakout candle's midpoint, take profit at 1:1, risking 1% of equity per trade

## What went wrong, and how it was found

### Bias 1 — lookahead in the entry price

The original backtest entered at the **retest candle's close**. But the signal isn't complete until the *confirmation* candle closes, which is five minutes later. In live trading that entry price is already stale by a full bar.

Correcting it to enter at the confirmation candle close:

| | Trades | Win rate | Total |
|---|---|---|---|
| Original (lookahead) | 2,427 | 72.2% | — |
| Corrected entry | 2,155 | 53.1% | +133R |

A 19-point drop, entirely from a five-minute timing error.

### Bias 2 — ambiguous bars scored in my favour

When a single 5-minute bar's range spans **both** the take profit and the stop, OHLC data cannot tell you which was touched first. The original code checked take profit first, so every one of these coinflips was silently awarded as a win.

With a stop placed half a breakout candle away, this happens often. Scoring stop-first instead:

| Scoring rule | Win rate | Total |
|---|---|---|
| TP checked first (optimistic bound) | 53.1% | +133R |
| Stop checked first (pessimistic bound) | 43.9% | −263R |

### The conclusion: the data can't answer the question

These two figures are **bounds, not estimates**. The true result sits somewhere between them, and 5-minute OHLC cannot narrow it further.

The gap between the bounds is about 9 percentage points. The edge being tested — a 1:1 risk-reward strategy needs better than 50% to be profitable — is around 3 points. **The measurement uncertainty is roughly three times larger than the effect.**

The median stop distance is $0.33 per share. At that tightness, resolving the outcome would need tick data or at minimum 1-minute bars. Continuing to optimise parameters against a signal the data cannot resolve would have been fitting noise.

The strategy was discontinued rather than tuned further.

### Supporting checks

**Live paper trading** to July 2026: roughly 28 trades at 42.9%, sitting at the pessimistic bound of the corrected backtest rather than the optimistic one.

**Removing the retest requirement** (entering at the breakout close instead) was decisively worse: 39.5% win rate, −1061R, z = −14.93, and worse in train, holdout and every individual symbol. The retest filter does help — just not enough to reach breakeven.

**Monte Carlo methodology fix.** The original bootstrap used `replace=False` with `size=len(r)`, which is a permutation — every run sums to the same total, so "probability of profit" came out at 100% by construction. Corrected to sample with replacement.

## Methodology notes

The validation in `validation.py` was written to avoid repeating the same mistakes:

- **Pre-registered.** Both arms and the comparison were specified before running. No variants added after seeing output.
- **Train/holdout split** at 2024-01-01, with the holdout looked at once.
- **Both arms share everything** except the entry rule: same filters, same stop placement, same sizing, same outcome evaluation.
- **Self-flagged bias.** The "never retested vs retested later" split is noted in the code as biased by construction, since trades resolving before a retest could occur are tagged "never retested" automatically. Reported as suggestive only.

## Repository layout

```
bot.py              Live paper-trading bot — streams bars, detects setups, places bracket orders
replay.py           Replays a single historical day through bot.py's logic for testing
validation.py       Pre-registered head-to-head experiment, bootstrap, equity curves
config.py           API keys (not tracked)
requirements.txt
```

## Setup

```bash
git clone https://github.com/RG1579/quant-alg.git
cd quant-alg
pip install -r requirements.txt
```

Create `config.py` with your Alpaca paper-trading credentials:

```python
api_key = "..."
secret_key = "..."
```

Then:

```bash
python bot.py           # live paper trading
python replay.py        # replay one historical day
python validation.py    # run the validation experiment
```

## Known issues

- `place_order` passes `limit_price` to `MarketOrderRequest`, left over from switching from limit to market orders. The field does not exist on that class and should be removed.
- `replay.py` still computes a `bullish` flag that nothing consumes, left from a removed directional filter.
- Slippage is not measured. `bot.py` logs the intended entry but not the actual fill. Given a median stop of $0.33, a few cents of slippage is a meaningful fraction of risk per trade and would be worth quantifying before any revival.

## Disclaimer

For educational purposes only. Not financial advice. The strategy in this repository was tested and found not to have a measurable edge.
