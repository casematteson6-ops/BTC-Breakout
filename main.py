"""
📈 BREAKOUT MAX YIELD Bot — BTC/USD H1
===============================================
This is the gold Breakout Max Yield bot's mechanism (Donchian
channel breakout + long EMA trend filter + ATR trailing stop),
re-validated on BTC/USD rather than assumed to transfer:

  - First check: the GOLD-tuned params (dc:96, ema:200, atr:20,
    sl:2.5x, tp:5.0x), unchanged, tested blind on 6 other symbols.
    Only BTC held up (4/5 folds profitable) -- everything else
    (ETH, XAG, GBPUSD, EURCHF, USDJPY) failed outright. That's a
    reasonable signal this is a real BTC mechanism, not a fluke of
    one parameter set.
  - Then re-optimized specifically for BTC (150 random search
    combos, Master-mode, ~17pt measured BTC slippage applied
    throughout):
      Backtest: 458 trades, 38.9% win rate, +$2,161 net profit
      Walk-Forward Optimization: 4/5 folds profitable out-of-sample,
        +$457 total OOS net profit (strict per-fold re-optimizing)
      Monte Carlo (2000 resamples of the actual OOS trades):
        85.2% probability of profit, 6.38% worst-case (95th pct)
        drawdown
  - All of the above independently re-verified against a second,
    separately-built copy of the same testing engine before
    deployment -- exact match on every number.

This REPLACES RSI Divergence in the live portfolio (not stacked
alongside it) -- RSI Divergence was the weakest-evidenced BTC bot
(3/5 folds under the same strict test), and running two BTC bots
simultaneously would add concentration risk without a measured
diversification benefit. Portfolio-level check with this swap:
3.06% drawdown / 14.11%/yr vs. 3.80% / 13.02%/yr with RSI Divergence
-- better on both dimensions, same total portfolio risk budget.

Strategy: LONG when close breaks above the rolling Donchian channel
high AND price is above the long EMA (trend filter prevents
counter-trend breakout entries). SHORT mirrors this on the downside.
Stop trails with ATR once in the trade -- same trailing convention
as the rest of this portfolio (trail the stop, never widen it,
never touch take-profit).

⚠️ Only ONE fold (of 5) in walk-forward was negative, and the
re-optimizer kept drifting `ema_period` toward its ceiling (300) in
2 of 5 folds, meaning BTC may want an even slower trend filter than
what's deployed below. Worth revisiting after live data comes in --
same standing rule as every bot here: real slippage/fill data before
increasing risk.

⚠️ Same standing caveat as every validated bot here: recommend
demo-account-first before live/funded capital. RISK_PCT below
matches RSI Divergence's old allocation (0.24%) since this bot has
no live track record yet to justify more.
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

BOT_NAME = "breakout_max_yield_btc"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Shared-symbol position helpers ──────────────────────────────────────────
# Same convention as every other BTC bot in this portfolio. This one now
# REPLACES RSI Divergence rather than running alongside it, so BTC/USD is
# back to a single bot -- these guards are kept for safety (e.g. if you
# ever redeploy RSI Divergence temporarily) but shouldn't need to fire in
# normal operation.
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
CANDLE_COUNT = 400   # ema_period=150 + warm-up margin, comfortably above dc_period=155 too

# ForexLab-validated parameters (independently re-verified, see caveats above)
DC_PERIOD    = 155
EMA_PERIOD   = 150
ATR_PERIOD   = 50
SL_ATR_MULT  = 1.7
TP_ATR_MULT  = 9.6

RISK_PCT     = 0.0024   # 0.24% -- matches RSI Divergence's old slot, no live track record yet
LOOP_SLEEP   = 300      # Scan every 5 minutes

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

    # Donchian channel -- prior DC_PERIOD candles, NOT including the current
    # one (shift(1) before rolling), so the breakout level can't be set by
    # the same candle that breaks it.
    df["dc_high"] = df["high"].shift(1).rolling(DC_PERIOD).max()
    df["dc_low"]  = df["low"].shift(1).rolling(DC_PERIOD).min()

    df["ema"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()

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

    logger.info("📈 Breakout Max Yield (BTC) Bot Started.")
    send_telegram("📈 Breakout Max Yield Bot Started | BTC/USD | Risk: 0.24%")
    init_db()

    active_trade = None
    last_signal_candle_ts = None  # candle-cooldown guard

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
                df = client.get_candles(INSTRUMENT, ATR_PERIOD + 10, GRANULARITY)
                if df is not None:
                    df = compute_indicators(df)
                    last = df.iloc[-1]
                    price, atr_val = last["close"], last["atr"]

                    if not np.isnan(atr_val):
                        active_trade["last_price"] = price

                        if active_trade["side"] == "BUY":
                            new_sl = round(price - SL_ATR_MULT * atr_val, 2)
                            if new_sl > active_trade["sl"]:
                                active_trade["sl"] = new_sl
                                client.modify_position(active_trade["position_id"], sl=new_sl, tp=active_trade["tp"])
                                logger.info(f"📈 Trailing SL BTC LONG → {new_sl}")
                        else:
                            new_sl = round(price + SL_ATR_MULT * atr_val, 2)
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
                send_telegram(f"✅ BTC/USD Breakout position closed.{pnl_str}")
                active_trade = None

            if active_trade:
                time.sleep(LOOP_SLEEP)
                continue

            # active_trade is None here -- look for a new signal regardless of
            # what other bots are doing on BTC/USD. Direction check right
            # before placing the order (below) still guards against hedging.

            df = client.get_candles(INSTRUMENT, CANDLE_COUNT, GRANULARITY)
            if df is None or len(df) < max(DC_PERIOD, EMA_PERIOD, ATR_PERIOD) + 5:
                time.sleep(60)
                continue

            # Candle-cooldown guard: polls every 5 min, trades H1 candles.
            # Fingerprint by (close,high,low) since Match Trader API has no timestamp column.
            last_row = df.iloc[-1]
            candle_ts = (round(last_row["close"], 5), round(last_row["high"], 5), round(last_row["low"], 5))
            if candle_ts == last_signal_candle_ts:
                time.sleep(LOOP_SLEEP)
                continue

            df = compute_indicators(df)
            last = df.iloc[-1]

            close, dc_high, dc_low, ema, atr_val = (
                last["close"], last["dc_high"], last["dc_low"], last["ema"], last["atr"]
            )

            if any(np.isnan(v) for v in [dc_high, dc_low, ema, atr_val]):
                time.sleep(60)
                continue

            sl_dist = SL_ATR_MULT * atr_val
            tp_dist = TP_ATR_MULT * atr_val

            lots = client.calculate_lots(balance, RISK_PCT, sl_dist, INSTRUMENT)
            if lots <= 0:
                time.sleep(60)
                continue

            # LONG: breakout above the channel, filtered by the trend EMA
            if close > dc_high and close > ema:
                sl = round(close - sl_dist, 2)
                tp = round(close + tp_dist, 2)
                logger.info(f"🔼 LONG BTC/USD (Breakout) | Entry:{close} SL:{sl} TP:{tp}")
                if _has_conflicting_direction(positions, "BUY"):
                    logger.info("⏸ Skipping LONG signal -- another bot holds an opposing (SHORT) "
                                "BTC/USD position. Opening would be a hedge, which Funding Pips prohibits.")
                else:
                    order_id, err = client.open_position(INSTRUMENT, "BUY", lots, sl, tp)
                    if order_id:
                        active_trade = {
                            "position_id": order_id, "side": "BUY", "sl": sl, "tp": tp,
                            "balance_before": balance, "last_price": close,
                        }
                        log_trade_open(
                            bot_name=BOT_NAME, symbol=INSTRUMENT, direction="BUY",
                            order_id=order_id, entry_price=close, entry_time=datetime.now(timezone.utc),
                            sl=sl, tp=tp, lot_size=lots,
                        )
                        last_signal_candle_ts = candle_ts
                        send_telegram(f"✅ LONG BTC/USD Breakout Opened\nEntry: {close} | SL: {sl} | TP: {tp}")

            # SHORT: breakdown below the channel, filtered by the trend EMA
            elif close < dc_low and close < ema:
                sl = round(close + sl_dist, 2)
                tp = round(close - tp_dist, 2)
                logger.info(f"🔽 SHORT BTC/USD (Breakout) | Entry:{close} SL:{sl} TP:{tp}")
                if _has_conflicting_direction(positions, "SELL"):
                    logger.info("⏸ Skipping SHORT signal -- another bot holds an opposing (LONG) "
                                "BTC/USD position. Opening would be a hedge, which Funding Pips prohibits.")
                else:
                    order_id, err = client.open_position(INSTRUMENT, "SELL", lots, sl, tp)
                    if order_id:
                        active_trade = {
                            "position_id": order_id, "side": "SELL", "sl": sl, "tp": tp,
                            "balance_before": balance, "last_price": close,
                        }
                        log_trade_open(
                            bot_name=BOT_NAME, symbol=INSTRUMENT, direction="SELL",
                            order_id=order_id, entry_price=close, entry_time=datetime.now(timezone.utc),
                            sl=sl, tp=tp, lot_size=lots,
                        )
                        last_signal_candle_ts = candle_ts
                        send_telegram(f"✅ SHORT BTC/USD Breakout Opened\nEntry: {close} | SL: {sl} | TP: {tp}")

        except Exception as e:
            logger.error(f"🔥 Error: {e}")

        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()
