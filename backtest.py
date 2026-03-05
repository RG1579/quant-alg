import pandas as pd
from datetime import datetime, timezone, date, timedelta
import pytz
import logging
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from config import *

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

ET = pytz.timezone('America/New_York')
SYMBOLS = ["QQQ", "SPY", "IWM", "TQQQ"]

SIM_DATE = date(2026, 3, 5)

stock_client = StockHistoricalDataClient(api_key, secret_key)

# Fetch prev day bias relative to sim date 
def get_prev_day_bias_sim(symbol, reference_date):
    end = datetime.combine(reference_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    start = end - timedelta(days=5)
    
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start,
        end=end,
        feed='iex'
    )
    
    bars = stock_client.get_stock_bars(request)
    df = bars.df.loc[symbol].sort_index()
    
    if len(df) < 2:
        return None
    
    prev_day = df.iloc[-1]  # last available day before sim date
    bullish = prev_day["close"] > prev_day["open"]
    prev_close = float(prev_day["close"])
    
    logging.info(f"{symbol} prev day bias: {'BULLISH' if bullish else 'BEARISH'} "
                 f"(O:{prev_day['open']:.2f} C:{prev_close:.2f})")
    
    return {
        "bullish": bullish,
        "prev_close": prev_close
    }

# Mock bar object to match live stream format
class MockBar:
    def __init__(self, row, symbol):
        self.symbol = symbol
        self.timestamp = row.name
        self.open = row["open"]
        self.high = row["high"]
        self.low = row["low"]
        self.close = row["close"]
        self.volume = row["volume"]

# Fetch sim day bars
request = StockBarsRequest(
    symbol_or_symbols=SYMBOLS,
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=datetime.combine(SIM_DATE, datetime.min.time()).replace(tzinfo=timezone.utc),
    end=datetime.combine(SIM_DATE, datetime.min.time()).replace(tzinfo=timezone.utc) + timedelta(days=1),
    feed='iex'
)

bars = stock_client.get_stock_bars(request)

# Import bot components
import bot
from bot import SymbolState, process_bar

bot.states = {symbol: SymbolState(symbol) for symbol in SYMBOLS}
bot.prev_day_bias = {}
bot.last_reset_date = None

for symbol in SYMBOLS:
    bot.prev_day_bias[symbol] = get_prev_day_bias_sim(symbol, SIM_DATE)

# Run sim
for symbol in SYMBOLS:
    logging.info(f"\n--- Simulating {symbol} on {SIM_DATE} ---")
    
    try:
        df = bars.df.loc[symbol].sort_index().tz_convert(ET)
        df_day = df.between_time("09:30", "16:00")
        
        for timestamp, row in df_day.iterrows():
            mock_bar = MockBar(row, symbol)
            process_bar(symbol, mock_bar)
    except KeyError:
        logging.warning(f"{symbol} no data for {SIM_DATE}")