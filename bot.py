import logging
from datetime import datetime, timezone, timedelta
import pytz
from alpaca.trading.client import TradingClient
from alpaca.data.live import StockDataStream
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.requests import LimitOrderRequest
from config import *
import csv
import os

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

ET = pytz.timezone('America/New_York')
SYMBOLS = ["QQQ", "SPY", "IWM", "TQQQ"]
RISK_PERCENT = 0.01
SIMULATION_MODE = False

trading_client = TradingClient(api_key, secret_key, paper=True)
prev_day_bias = {}
last_reset_date = None


class SymbolState:
    def __init__(self, symbol):
        self.symbol = symbol
        self.bars = []
        self.or_high = None
        self.or_low = None
        self.direction = None
        self.breakout_confirmed = False
        self.breakout_candle = None
        self.retest_confirmed = False
        self.post_breakout_candle = None
        self.trade_taken = False

    def reset(self):
        self.bars = []
        self.or_high = None
        self.or_low = None
        self.direction = None
        self.breakout_confirmed = False
        self.breakout_candle = None
        self.retest_confirmed = False
        self.post_breakout_candle = None
        self.trade_taken = False


states = {symbol: SymbolState(symbol) for symbol in SYMBOLS}


def is_market_open():
    clock = trading_client.get_clock()
    return clock.is_open


def get_account_equity():
    account = trading_client.get_account()
    return float(account.equity)


def get_prev_day_bias(symbol):
    data_client = StockHistoricalDataClient(api_key, secret_key)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start,
        end=end,
        feed='iex'
    )

    bars = data_client.get_stock_bars(request)
    df = bars.df.loc[symbol].sort_index()

    if len(df) < 2:
        return None

    prev_day = df.iloc[-2]
    prev_close = float(prev_day["close"])

    logging.info(f"{symbol} prev close: {prev_close:.2f}")

    return {"prev_close": prev_close}


def _do_daily_reset():
    for s in states.values():
        s.reset()
    for sym in SYMBOLS:
        prev_day_bias[sym] = get_prev_day_bias(sym)
    logging.info("Daily reset complete")


def place_order(symbol, direction, entry, stop):
    try:
        equity = get_account_equity()
        risk_amount = equity * RISK_PERCENT
        risk_per_share = abs(entry - stop)

        if risk_per_share == 0:
            logging.warning(f"{symbol} risk_per_share is 0, skipping")
            return

        shares = min(int(risk_amount / risk_per_share), 1000)

        max_position_value = equity * 0.20
        max_shares_by_value = int(max_position_value / entry)
        shares = min(shares, max_shares_by_value)

        if shares == 0:
            logging.warning(f"{symbol} shares calculated as 0, skipping")
            return

        if direction == 'long':
            take_profit = round(entry + risk_per_share, 2)          # 1:1 RR
            take_profit = max(take_profit, round(entry + 0.02, 2))
            stop_price = round(stop, 2)
            side = OrderSide.BUY
        elif direction == 'short':
            take_profit = round(entry - risk_per_share, 2)          # 1:1 RR
            take_profit = min(take_profit, round(entry - 0.02, 2))
            stop_price = round(stop, 2)
            side = OrderSide.SELL

        order = trading_client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=shares,
                side=side,
                type='limit',
                limit_price=round(entry, 2),
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss={"stop_price": stop_price},
                take_profit={"limit_price": take_profit}
            )
        )

        log_trade(
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop=stop_price,
            take_profit=take_profit,
            shares=shares,
            order_id=order.id
        )

        logging.info(f"{symbol} Order placed - {direction.upper()} "
                     f"{shares} shares | Entry: ~{entry:.2f} "
                     f"Stop: {stop_price:.2f} TP: {take_profit:.2f} "
                     f"Risk: ${risk_amount:.2f}")
        return order

    except Exception as e:
        logging.error(f"{symbol} Order failed: {e}")


def process_bar(symbol, bar):
    global last_reset_date

    bar_et_time = bar.timestamp.astimezone(ET)

    # Daily reset on first 9:30 bar of each new trading day
    bar_date = bar_et_time.date()
    if bar_date != last_reset_date:
        if bar_et_time.hour == 9 and bar_et_time.minute == 30:
            last_reset_date = bar_date
            _do_daily_reset()

    state = states[symbol]

    if bar_et_time.hour < 9 or (bar_et_time.hour == 9 and bar_et_time.minute < 30):
        return
    if bar_et_time.hour >= 16:
        return
    if state.trade_taken:
        return

    logging.info(f"{symbol} bar: {bar_et_time.strftime('%H:%M')} "
                 f"O:{bar.open:.2f} H:{bar.high:.2f} "
                 f"L:{bar.low:.2f} C:{bar.close:.2f}")

    # Capture opening range
    if bar_et_time.hour == 9 and bar_et_time.minute == 30:
        state.or_high = bar.high
        state.or_low = bar.low

        OR_range = state.or_high - state.or_low
        OR_range_pct = OR_range / state.or_low * 100
        if OR_range_pct < 0.10:
            logging.info(f"{symbol} OR range too tight ({OR_range_pct:.2f}%), skipping day")
            state.trade_taken = True
            return

        bias_data = prev_day_bias.get(symbol)
        if bias_data:
            gap_pct = abs(bar.open - bias_data["prev_close"]) / bias_data["prev_close"] * 100
            if gap_pct > 0.7:
                logging.info(f"{symbol} Gap too large ({gap_pct:.2f}%), skipping day")
                state.trade_taken = True
                return

        state.bars.append(bar)
        logging.info(f"{symbol} Opening Range: {state.or_low:.2f} - {state.or_high:.2f}")
        return

    if state.or_high is None:
        return

    state.bars.append(bar)

    # Breakout detection
    if not state.breakout_confirmed:
        if bar.close > state.or_high:
            state.direction = 'long'
            state.breakout_confirmed = True
            state.breakout_candle = bar
            logging.info(f"{symbol} Breakout LONG at {bar_et_time.strftime('%H:%M')}, close: {bar.close:.2f}")
        elif bar.close < state.or_low:
            state.direction = 'short'
            state.breakout_confirmed = True
            state.breakout_candle = bar
            logging.info(f"{symbol} Breakout SHORT at {bar_et_time.strftime('%H:%M')}, close: {bar.close:.2f}")
        return

    # Time filter
    if bar_et_time.hour > 10 or (bar_et_time.hour == 10 and bar_et_time.minute > 30):
        logging.info(f"{symbol} Retest window expired")
        state.trade_taken = True
        return

    # Retest detection
    if not state.retest_confirmed:
        if state.direction == 'long':
            if bar.low <= state.or_high and bar.close > state.or_high:
                state.retest_confirmed = True
                state.post_breakout_candle = bar
                logging.info(f"{symbol} Retest LONG confirmed at {bar_et_time.strftime('%H:%M')}")
        elif state.direction == 'short':
            if bar.high >= state.or_low and bar.close < state.or_low:
                state.retest_confirmed = True
                state.post_breakout_candle = bar
                logging.info(f"{symbol} Retest SHORT confirmed at {bar_et_time.strftime('%H:%M')}")
        return

    # Confirmation candle + order
    if state.retest_confirmed and not state.trade_taken:
        breakout_mid = (state.breakout_candle.high + state.breakout_candle.low) / 2
        entry = state.post_breakout_candle.close
        stop = breakout_mid

        if state.direction == 'long' and bar.close > state.or_high:
            if stop >= entry:
                logging.warning(f"{symbol} Invalid setup - stop {stop:.2f} >= entry {entry:.2f}, skipping")
                state.trade_taken = True
                return
            logging.info(f"{symbol} Confirmation candle LONG - placing order")
            if SIMULATION_MODE:
                tp = round(entry + abs(entry - stop), 2)
                logging.info(f"{symbol} SIMULATION - would place LONG | "
                             f"Entry: ~{entry:.2f} Stop: {stop:.2f} TP: {tp:.2f}")
            else:
                place_order(symbol, 'long', entry, stop)

        elif state.direction == 'short' and bar.close < state.or_low:
            if stop <= entry:
                logging.warning(f"{symbol} Invalid setup - stop {stop:.2f} <= entry {entry:.2f}, skipping")
                state.trade_taken = True
                return
            logging.info(f"{symbol} Confirmation candle SHORT - placing order")
            if SIMULATION_MODE:
                tp = round(entry - abs(entry - stop), 2)
                logging.info(f"{symbol} SIMULATION - would place SHORT | "
                             f"Entry: ~{entry:.2f} Stop: {stop:.2f} TP: {tp:.2f}")
            else:
                place_order(symbol, 'short', entry, stop)

        state.trade_taken = True


async def bar_handler(bar):
    if bar.symbol in states:
        process_bar(bar.symbol, bar)


def run_stream():
    stream = StockDataStream(api_key, secret_key)
    stream.subscribe_bars(bar_handler, *SYMBOLS)
    logging.info(f"Streaming bars for {SYMBOLS}")
    stream.run()


def log_trade(symbol, direction, entry, stop, take_profit, shares, order_id):
    log_file = 'trades.log.csv'
    file_exists = os.path.exists(log_file)

    with open(log_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'date', 'time', 'symbol', 'direction', 'entry',
            'stop', 'take_profit', 'shares', 'risk_amount',
            'potential_profit', 'order_id'
        ])

        if not file_exists:
            writer.writeheader()

        now = datetime.now(ET)
        risk_amount = abs(entry - stop) * shares
        potential_profit = abs(take_profit - entry) * shares

        writer.writerow({
            'date': now.strftime('%Y-%m-%d'),
            'time': now.strftime('%H:%M:%S'),
            'symbol': symbol,
            'direction': direction,
            'entry': round(entry, 2),
            'stop': round(stop, 2),
            'take_profit': round(take_profit, 2),
            'shares': shares,
            'risk_amount': round(risk_amount, 2),
            'potential_profit': round(potential_profit, 2),
            'order_id': order_id
        })

    logging.info(f"{symbol} Trade logged to {log_file}")


if __name__ == "__main__":
    logging.info("Bot started")
    logging.info(f"Market open: {is_market_open()}")
    logging.info(f"Account equity: ${get_account_equity():,.2f}")

    for symbol, state in states.items():
        logging.info(f"Initialized state for {symbol}")

    for symbol in SYMBOLS:
        prev_day_bias[symbol] = get_prev_day_bias(symbol)

    for state in states.values():
        state.reset()

    logging.info("Waiting for market data...")
    run_stream()
