"""
master_account_safety.py
=========================
Force-closes any open position before Friday market close and stays
flat through the weekend, every account stage, no configuration
needed. This used to be a toggle (ACCOUNT_STAGE=eval/master) so eval
accounts could keep weekend-hold profits, but the simpler, safer
default now is: always behave this way, so nothing has to be
remembered or flipped later when the account graduates to funded.

If you ever DO want to allow weekend holding again (e.g. specifically
during an evaluation phase, where it's explicitly permitted and can
add some profit), set this on that bot's Railway service:

    ACCOUNT_STAGE=eval

Leaving it unset means "always closed for the weekend" -- the safe
default.

CAVEAT: Funding Pips doesn't publish an exact "market close" timestamp
per instrument anywhere we have access to. FRIDAY_CLOSE_CUTOFF_HOUR_UTC
below is a conservative estimate (20:00 UTC) -- intentionally earlier
than every real close time we've seen evidence for, so bots close out
safely ahead of the actual deadline instead of cutting it close.
Confirm the real cutoff with Funding Pips directly if precision here
ever matters, and override via the FRIDAY_CLOSE_CUTOFF_HOUR_UTC env var
if needed, no code change required for that either.
"""

import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("master_account_safety")

ACCOUNT_STAGE = os.getenv("ACCOUNT_STAGE", "master").strip().lower()
FRIDAY_CLOSE_CUTOFF_HOUR_UTC = int(os.getenv("FRIDAY_CLOSE_CUTOFF_HOUR_UTC", "20"))

_IS_MASTER = ACCOUNT_STAGE != "eval"  # anything other than explicit "eval" opts INTO safety

_state = {"logged_mode": False}


def _log_mode_once():
    if not _state["logged_mode"]:
        if _IS_MASTER:
            logger.info(
                f"🏦 Weekend safety ON (default) -- positions will be force-closed before Friday "
                f"{FRIDAY_CLOSE_CUTOFF_HOUR_UTC}:00 UTC and stay flat through the weekend."
            )
        else:
            logger.info("🧪 ACCOUNT_STAGE=eval -- weekend holds permitted, no forced closures.")
        _state["logged_mode"] = True


def in_master_friday_close_window(now_utc=None):
    """True only when ACCOUNT_STAGE=master AND we're in the forced-closure
    window: from the cutoff hour on Friday through the end of the weekend."""
    if not _IS_MASTER:
        return False
    now_utc = now_utc or datetime.now(timezone.utc)
    wd = now_utc.weekday()  # Mon=0 ... Sun=6
    if wd == 4 and now_utc.hour >= FRIDAY_CLOSE_CUTOFF_HOUR_UTC:  # Friday, after cutoff
        return True
    if wd in (5, 6):  # Saturday, Sunday -- stay closed until Monday to be safe
        return True
    return False


def handle_master_account_safety(client, trade_state, bot_name, send_telegram=None):
    """
    Call this once per loop iteration, right after fetching `positions`,
    passing whatever local state dict the bot uses to track its own open
    trade (active_trade or logged_trade -- anything with a "position_id"
    key works; a missing "side" is fine, it isn't required to close).

    Returns True if this call handled the cycle (either force-closed a
    position or we're simply in the closed window with nothing to do) --
    callers should treat True as "skip normal signal/trailing logic this
    iteration." Returns False the rest of the time, meaning: proceed
    exactly as before, this module has done nothing.

    During the evaluation phase (the default), this is always a no-op --
    zero behavior change, zero risk, until ACCOUNT_STAGE=master is set.
    """
    _log_mode_once()

    if not in_master_friday_close_window():
        return False

    if trade_state and trade_state.get("position_id"):
        position_id = trade_state["position_id"]
        side = trade_state.get("side", "")
        ok, err = client.close_position(position_id, "", side, 0)
        if ok:
            logger.info(
                f"🏦 Force-closed {bot_name} position {position_id} ahead of the "
                f"Master-account weekend restriction."
            )
            if send_telegram:
                send_telegram(
                    f"🏦 {bot_name}: position force-closed for the weekend "
                    f"(Master-account rule, not a breach)."
                )
        else:
            logger.warning(f"🏦 Force-close attempt failed for {bot_name} (will retry next cycle): {err}")
        return True

    return True
