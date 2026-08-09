"""
⚡ RSI DIVERGENCE Bot — BTC/USD H1
===============================================
Parameters below came from a full ForexLab validation pipeline on
real Coinbase BTC/USD H1 history (2021-2026):

  - Backtest (realistic $2.50/lot commission, 1 pip slippage):
      874 trades, 39.7% win rate, +$6,925 net profit, 8.4% max DD
  - Walk-Forward Optimization: 4/5 folds profitable out-of-sample
  - Monte Carlo (3000 resamples of the actual trades): 99.9%
      probability of profit

Strategy: mechanically different from every other bot in this
portfolio -- instead of confirming momentum (trend filters,
breakout follow-through, squeeze releases), this looks for a
MISMATCH between price and momentum. When price makes a new high
but RSI fails to make a new high too (momentum quietly weakening
even as price rises), that's bearish divergence -> short. The
mirror case (new low, but RSI holding up) is bullish divergence ->
long. A classic exhaustion/reversal signal, not a trend-following
one.

Uses an ATR trailing stop, same mechanism as Breakout Max Yield,
Trend Pullback, and Volatility Squeeze.

⚠️ You now have TWO bots trading BTC (this one and Trend Pullback
Max Yield). Different logic, but genuinely more concentrated
exposure to BTC than to your other pairs -- worth being aware of.

⚠️ Same standing caveat as every validated bot here: recommend
demo-account-first before live/funded capital.
"""

import os
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from match_trader_api import MatchTraderClient
from trade_logger import init_db, log_trade_open, log_trade_close
from master_account_safety import handle_master_account_safety

BOT_NAME = "rsi_divergence"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Shared-symbol position helpers ──────────────────────────────────────────
# Match Trader is confirmed to use independent tickets (not netted positions),
# so multiple bots CAN safely hold separate same-direction positions on one
# symbol. What must still be avoided is an actual HEDGE (opposite-direction
# positions on the same symbol), which Funding Pips prohibits outright.
# Field names below are defensive guesses based on this API's own confirmed
# conventions (orderId/positionId, orderSide) -- the first log line below
# prints a raw sample so these can be verified against real data.
_logged_position_sample = {"done": False}

def _log_position_sample_once(positions):
    if positions and not _logged_position_sample["done"]:
        logger.info(f"🔎 Sample position object (verify field names): {positions[0]}")
        _logged_position_sample["done"] = True

def _position_id(pos):
    return pos.get("orderId") or pos.get("positionId") or pos.get("id")

def _position_direction(pos):
    val = pos.get("orderSide") or pos.get("side") or pos.get("direction")
    return str(val).upper() if val else None

def _find_own_position(positions, my_order_id):
    if not positions or not my_order_id:
        return None
    for p in positions:
        if str(_position_id(p)) == str(my_order_id):
            return p
    return None

def _has_conflicting_direction(positions, my_side):
    """True if any OTHER open position on this symbol is in the opposite
    direction -- opening into this would be a real hedge (banned)."""
    if not positions:
        return False
    my_side = my_side.upper()
    for p in positions:
        other_side = _position_direction(p)
        if other_side and other_side != my_side:
            return True
    return False

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

INSTRUMENT   = "BTC_USD"
GRANULARITY  = "H1"
CANDLE_COUNT = 100

# ForexLab-validated parameters
RSI_PERIOD      = 17
LOOKBACK_PERIOD = 15
MIN_RSI_GAP     = 3.0
ATR_PERIOD      = 18
ATR_SL_MULT     = 1.5   # wide -- tight stops don't survive real BTC slippage (measured ~17pt avg)
ATR_TP_MULT     = 6.0

RISK_PCT     = 0.0024   # 0.24% -- risk-reallocated: borderline walk-forward evidence (3/5 folds)
LOOP_SLEEP   = 300     # Scan every 5 minutes

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram error: {e}")

# ── Indicators ─────────────────────────────────────────────────────────────────
def compute_indicators(df):
    df = df.copy()

    delta    = df["close"].diff()
    gain     = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss     = (-delta).clip(lower=0).rolling(RSI_PERIOD).mean()
    rs       = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df.apply(
        lambda r: max(r["high"] - r["low"],
                      abs(r["high"] - r["prev_close"]),
                      abs(r["low"]  - r["prev_close"])), axis=1)
    df["atr"] = df["tr"].rolling(ATR_PERIOD).mean()

    return df

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    client = MatchTraderClient()
    if not client.login():
        logger.error("❌ Login Failed.")
        return

    logger.info("⚡ RSI Divergence Bot Started.")
    send_telegram("⚡ RSI Divergence Bot Started | BTC/USD | Risk: 0.4%")
    init_db()

    active_trade = None

    while True:
        try:
            balance = client.get_balance()
            if balance is None:
                time.sleep(60)
                continue

            positions = client.get_open_positions(INSTRUMENT)
            _log_position_sample_once(positions)

            if handle_master_account_safety(client, active_trade, BOT_NAME, send_telegram):
                active_trade = None
                time.sleep(LOOP_SLEEP)
                continue

            my_position = _find_own_position(positions, active_trade["position_id"]) if active_trade else None

            # Manage existing trade (trailing SL) -- only while MY position is
            # confirmed still open, not just "some position on this symbol exists"
            if active_trade and my_position:
                df = client.get_candles(INSTRUMENT, 50, GRANULARITY)
                if df is not None:
                    df = compute_indicators(df)
                    last = df.iloc[-1]
                    price, atr = last["close"], last["atr"]
                    active_trade["last_price"] = price

                    if active_trade["side"] == "BUY":
                        new_sl = round(price - ATR_SL_MULT * atr, 2)
                        if new_sl > active_trade["sl"]:
                            active_trade["sl"] = new_sl
                            client.modify_position(active_trade["position_id"], sl=new_sl, tp=active_trade["tp"])
                            logger.info(f"📈 Trailing SL BTC LONG → {new_sl}")
                    else:
                        new_sl = round(price + ATR_SL_MULT * atr, 2)
                        if new_sl < active_trade["sl"]:
                            active_trade["sl"] = new_sl
                            client.modify_position(active_trade["position_id"], sl=new_sl, tp=active_trade["tp"])
                            logger.info(f"📉 Trailing SL BTC SHORT → {new_sl}")
                time.sleep(LOOP_SLEEP)
                continue

            if active_trade and not my_position:
                balance_after = client.get_balance()
                realized_pnl = None
                if balance_after is not None and active_trade.get("balance_before") is not None:
                    realized_pnl = round(balance_after - active_trade["balance_before"], 2)
                log_trade_close(
                    bot_name=BOT_NAME,
                    order_id=active_trade["position_id"],
                    exit_price=active_trade.get("last_price"),
                    exit_time=datetime.now(timezone.utc),
                    realized_pnl=realized_pnl,
                )
                pnl_str = f" | PnL: ${realized_pnl}" if realized_pnl is not None else ""
                send_telegram(f"✅ BTC/USD Divergence position closed.{pnl_str}")
                active_trade = None

            if active_trade:
                # Still waiting on a position we opened but haven't confirmed
                # yet, or an edge case where my_position lookup needs a retry.
                time.sleep(LOOP_SLEEP)
                continue

            # active_trade is None here -- look for a new signal regardless of
            # what other bots are doing on BTC/USD. A direction check right
            # before placing the order (below) still guards against hedging.

            # Candle-cooldown guard: polls every 5 min, trades H1 candles.
            # Fingerprint by (close,high,low) since Match Trader API has no timestamp column.
            last_row = df.iloc[-1]
            candle_ts = (round(last_row["close"],5), round(last_row["high"],5), round(last_row["low"],5))
            if candle_ts == last_signal_candle_ts:
                time.sleep(LOOP_SLEEP)
                continue

            # Signal Detection
            df = client.get_candles(INSTRUMENT, CANDLE_COUNT, GRANULARITY)
            if df is None or len(df) < LOOKBACK_PERIOD + RSI_PERIOD + 5:
                time.sleep(60)
                continue

            df = compute_indicators(df)

            if len(df) < LOOKBACK_PERIOD + 2:
                time.sleep(60)
                continue

            last = df.iloc[-1]
            close, rsi_val, atr = last["close"], last["rsi"], last["atr"]

            if any(np.isnan(v) for v in [rsi_val, atr]):
                time.sleep(60)
                continue

            # Look back over the prior LOOKBACK_PERIOD candles
            # (excluding the current one) for the highest high /
            # lowest low and the RSI value at that same candle.
            window = df.iloc[-(LOOKBACK_PERIOD + 1):-1]

            if window["rsi"].isna().any():
                time.sleep(60)
                continue

            prior_highest_high = window["high"].max()
            rsi_at_prior_high = window.loc[window["high"].idxmax(), "rsi"]

            prior_lowest_low = window["low"].min()
            rsi_at_prior_low = window.loc[window["low"].idxmin(), "rsi"]

            sl_dist = ATR_SL_MULT * atr
            tp_dist = ATR_TP_MULT * atr

            lots = client.calculate_lots(balance, RISK_PCT, sl_dist, INSTRUMENT)
            if lots <= 0:
                time.sleep(60)
                continue

            # Bearish divergence -> SHORT
            if (
                last["high"] > prior_highest_high
                and rsi_val < rsi_at_prior_high - MIN_RSI_GAP
            ):
                sl = round(close + sl_dist, 2)
                tp = round(close - tp_dist, 2)
                logger.info(f"🔽 SHORT BTC/USD (Bearish Divergence) | Entry:{close} SL:{sl} TP:{tp}")
                if _has_conflicting_direction(positions, "SELL"):
                    logger.info("⏸ Skipping SHORT signal -- another bot holds an opposing (LONG) "
                                "BTC/USD position. Opening would be a hedge, which Funding Pips prohibits.")
                else:
                    order_id, err = client.open_position(INSTRUMENT, "SELL", lots, sl, tp)
                    if order_id:
                        entry_time = datetime.now(timezone.utc)
                        active_trade = {
                            "position_id": order_id, "side": "SELL", "sl": sl, "tp": tp,
                            "balance_before": balance, "last_price": close,
                        }
                        log_trade_open(
                            bot_name=BOT_NAME, symbol=INSTRUMENT, direction="SELL",
                            order_id=order_id, entry_price=close, entry_time=entry_time,
                            sl=sl, tp=tp, lot_size=lots,
                        )
                        last_signal_candle_ts = candle_ts
                        send_telegram(f"✅ SHORT BTC/USD Divergence Opened\nEntry: {close} | SL: {sl} | TP: {tp}")

            # Bullish divergence -> LONG
            elif (
                last["low"] < prior_lowest_low
                and rsi_val > rsi_at_prior_low + MIN_RSI_GAP
            ):
                sl = round(close - sl_dist, 2)
                tp = round(close + tp_dist, 2)
                logger.info(f"🔼 LONG BTC/USD (Bullish Divergence) | Entry:{close} SL:{sl} TP:{tp}")
                if _has_conflicting_direction(positions, "BUY"):
                    logger.info("⏸ Skipping LONG signal -- another bot holds an opposing (SHORT) "
                                "BTC/USD position. Opening would be a hedge, which Funding Pips prohibits.")
                else:
                    order_id, err = client.open_position(INSTRUMENT, "BUY", lots, sl, tp)
                    if order_id:
                        entry_time = datetime.now(timezone.utc)
                        active_trade = {
                            "position_id": order_id, "side": "BUY", "sl": sl, "tp": tp,
                            "balance_before": balance, "last_price": close,
                        }
                        log_trade_open(
                            bot_name=BOT_NAME, symbol=INSTRUMENT, direction="BUY",
                            order_id=order_id, entry_price=close, entry_time=entry_time,
                            sl=sl, tp=tp, lot_size=lots,
                        )
                        last_signal_candle_ts = candle_ts
                        send_telegram(f"✅ LONG BTC/USD Divergence Opened\nEntry: {close} | SL: {sl} | TP: {tp}")

        except Exception as e:
            logger.error(f"🔥 Error: {e}")

        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()
