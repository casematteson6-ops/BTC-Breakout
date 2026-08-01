"""
trade_logger.py
================
Shared trade-logging module for all ForexLab bot repos.

Writes to a single Postgres database (Railway) shared across every bot
service and ForexLab itself, via the DATABASE_URL env var Railway injects
automatically when you attach a Postgres service to each bot's environment.

Design goals:
  - Never crash the bot. Logging is observability, not the trading path.
    Every function catches its own exceptions, logs a warning, and returns
    None on failure so the calling bot's while-loop just keeps going.
  - Degrade gracefully. If DATABASE_URL isn't set yet (e.g. mid-rollout,
    only 1 of 6 bots wired up so far), every function becomes a silent
    no-op after one warning, instead of raising on import.
  - Cheap to call. Opens a short-lived connection per call rather than
    holding one open across a bot's multi-second sleep loop, since Railway
    Postgres free/hobby tiers can drop idle connections.

Usage in a bot:
    from trade_logger import init_db, log_trade_open, log_trade_close

    init_db()  # once, at startup

    # right after a confirmed fill:
    log_trade_open(
        bot_name="rsi_divergence", symbol="BTCUSD", direction="BUY",
        order_id=order_id, entry_price=close, entry_time=datetime.now(timezone.utc),
        sl=sl, tp=tp, lot_size=lots,
    )

    # right after you detect the position is gone:
    log_trade_close(
        bot_name="rsi_divergence", order_id=active_trade["position_id"],
        exit_price=last_known_price, exit_time=datetime.now(timezone.utc),
        realized_pnl=balance_after - active_trade["balance_before"],
    )
"""

import os
import logging

logger = logging.getLogger("trade_logger")

DATABASE_URL = os.environ.get("DATABASE_URL")
_warned_no_db = False

try:
    import psycopg2
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            SERIAL PRIMARY KEY,
    bot_name      TEXT        NOT NULL,
    symbol        TEXT        NOT NULL,
    direction     TEXT        NOT NULL,
    order_id      TEXT        NOT NULL,
    entry_price   DOUBLE PRECISION,
    entry_time    TIMESTAMPTZ NOT NULL,
    sl            DOUBLE PRECISION,
    tp            DOUBLE PRECISION,
    lot_size      DOUBLE PRECISION,
    exit_price    DOUBLE PRECISION,
    exit_time     TIMESTAMPTZ,
    realized_pnl  DOUBLE PRECISION,
    status        TEXT        NOT NULL DEFAULT 'open',
    source        TEXT        NOT NULL DEFAULT 'live',   -- 'live' (bot-logged) or 'manual' (backfilled)
    notes         TEXT,
    UNIQUE (bot_name, order_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_bot_status ON trades (bot_name, status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades (entry_time);
"""


def _ready():
    """Returns True if we can actually talk to a DB. Warns once if not."""
    global _warned_no_db
    if not _PSYCOPG2_AVAILABLE:
        if not _warned_no_db:
            logger.warning("trade_logger: psycopg2 not installed -- trade logging disabled. "
                            "Add psycopg2-binary to requirements.txt to enable it.")
            _warned_no_db = True
        return False
    if not DATABASE_URL:
        if not _warned_no_db:
            logger.warning("trade_logger: DATABASE_URL not set -- trade logging disabled for this bot.")
            _warned_no_db = True
        return False
    return True


def _connect():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def init_db():
    """Idempotent. Call once at bot startup. Safe to call from every bot --
    whichever one starts first creates the table, the rest just no-op."""
    if not _ready():
        return
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)
            conn.commit()
        logger.info("trade_logger: DB ready.")
    except Exception as e:
        logger.warning(f"trade_logger: init_db failed ({e}) -- continuing without logging.")


def log_trade_open(bot_name, symbol, direction, order_id, entry_price,
                    entry_time, sl, tp, lot_size):
    """Insert a new open trade row. Returns the new row id, or None on
    failure (bot should NOT treat None as a reason to stop trading)."""
    if not _ready():
        return None
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades
                        (bot_name, symbol, direction, order_id, entry_price,
                         entry_time, sl, tp, lot_size, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
                    ON CONFLICT (bot_name, order_id) DO NOTHING
                    RETURNING id
                    """,
                    (bot_name, symbol, direction, str(order_id), entry_price,
                     entry_time, sl, tp, lot_size),
                )
                row = cur.fetchone()
            conn.commit()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"trade_logger: log_trade_open failed ({e}) -- trade not logged, bot continues.")
        return None


def log_manual_trade(bot_name, symbol, direction, realized_pnl, trade_date,
                      entry_price=None, exit_price=None, notes=None):
    """For backfilling trades placed before logging was wired up (e.g. this
    week's trades from the Funding Pips / Match Trader history). Inserts an
    already-closed row directly. Generates its own synthetic order_id so it
    can't collide with a real Match Trader order_id.
    Returns the new row id, or None on failure."""
    if not _ready():
        return None
    import uuid
    synthetic_order_id = f"manual-{uuid.uuid4().hex[:12]}"
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades
                        (bot_name, symbol, direction, order_id, entry_price,
                         entry_time, exit_price, exit_time, realized_pnl,
                         status, source, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'closed', 'manual', %s)
                    RETURNING id
                    """,
                    (bot_name, symbol, direction, synthetic_order_id, entry_price,
                     trade_date, exit_price, trade_date, realized_pnl, notes),
                )
                row = cur.fetchone()
            conn.commit()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"trade_logger: log_manual_trade failed ({e}).")
        return None


def get_trades(bot_name=None, since=None):
    """Returns a list of dict rows from the trades table, most recent first.
    Optionally filter by bot_name and/or a `since` datetime (compares against
    entry_time). Returns [] on failure or if not configured -- callers should
    treat an empty list as 'nothing to show', not necessarily an error."""
    if not _ready():
        return []
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                query = "SELECT id, bot_name, symbol, direction, order_id, entry_price, " \
                        "entry_time, exit_price, exit_time, realized_pnl, status, source, notes " \
                        "FROM trades WHERE 1=1"
                params = []
                if bot_name:
                    query += " AND bot_name = %s"
                    params.append(bot_name)
                if since:
                    query += " AND entry_time >= %s"
                    params.append(since)
                query += " ORDER BY entry_time DESC"
                cur.execute(query, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows
    except Exception as e:
        logger.warning(f"trade_logger: get_trades failed ({e}).")
        return []


def log_trade_close(bot_name, order_id, exit_price, exit_time, realized_pnl):
    """Mark a trade closed by (bot_name, order_id). exit_price may be None
    if you don't have a reliable last-known price -- realized_pnl (from a
    balance delta) is the authoritative field and should always be passed
    when available."""
    if not _ready():
        return None
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE trades
                       SET exit_price   = %s,
                           exit_time    = %s,
                           realized_pnl = %s,
                           status       = 'closed'
                     WHERE bot_name = %s AND order_id = %s AND status = 'open'
                    """,
                    (exit_price, exit_time, realized_pnl, bot_name, str(order_id)),
                )
                updated = cur.rowcount
            conn.commit()
        if updated == 0:
            logger.warning(f"trade_logger: log_trade_close found no open row for "
                            f"bot={bot_name} order_id={order_id} -- nothing updated.")
        return updated
    except Exception as e:
        logger.warning(f"trade_logger: log_trade_close failed ({e}) -- close not logged, bot continues.")
        return None
