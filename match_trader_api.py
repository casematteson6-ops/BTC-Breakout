"""
Match-Trader API Client for FundingPips
========================================
Endpoints confirmed by live browser network inspection on 2026-06-25.

Authentication flow:
1. POST /mtr-core-edge/v2/login
   Body: {"email": "...", "password": "...", "partnerId": 1}
   → Returns auth token in response + sets session cookies

2. All trading endpoints use: /mtr-api/{systemUUID}/...
3. Candle data uses: /market-data-api/{systemUUID}/candles

Your System UUID: beedbea9-c757-46ad-b93b-a52ba2c3d648
"""

import os
import time
import requests
import pandas as pd
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Environment Variables ──────────────────────────────────────────────────────
RAW_URL = os.getenv("MT_PLATFORM_URL", "https://mtr-platform.fundingpips.com")
MT_PLATFORM_URL = RAW_URL.split("/app/")[0].rstrip("/")

MT_EMAIL      = os.getenv("MT_EMAIL", "casematteson6@gmail.com")
MT_PASSWORD   = os.getenv("MT_PASSWORD", "")
MT_BROKER_ID  = os.getenv("MT_BROKER_ID", "FundingPips")
MT_ACCOUNT_ID = os.getenv("MT_ACCOUNT_ID", "2009271")

# System UUID discovered from live browser session — fixed for this account
SYSTEM_UUID = "beedbea9-c757-46ad-b93b-a52ba2c3d648"


class MatchTraderClient:
    """
    Authenticated client for the Match-Trader Platform API (FundingPips).
    Endpoints confirmed via live browser network inspection.
    """

    def __init__(self):
        self.platform_url  = MT_PLATFORM_URL
        self.email         = MT_EMAIL
        self.password      = MT_PASSWORD
        self.account_id    = MT_ACCOUNT_ID
        self.system_uuid   = SYSTEM_UUID

        self.session       = requests.Session()
        self.auth_token    = None
        self.trading_token = None
        self.token_expiry  = datetime.min

    # ── Authentication ─────────────────────────────────────────────────────────

    def login(self):
        """
        Login using email + password.
        Confirmed endpoint: POST /mtr-core-edge/v2/login

        FIX: Reset the session on every login so stale cookies from a previous
        session don't interfere with the new auth token.
        """
        # ✅ FIX 1: Always create a fresh session so old cookies are wiped.
        self.session = requests.Session()
        self.auth_token    = None
        self.trading_token = None

        url = f"{self.platform_url}/mtr-core-edge/v2/login"
        payload = {
            "email":     self.email,
            "password":  self.password,
            "partnerId": 1
        }
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "Origin":       self.platform_url,
            "Referer":      f"{self.platform_url}/login",
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # 1. Extract main auth token
            self.auth_token = (
                data.get("token") or
                data.get("authToken") or
                data.get("accessToken") or
                ""
            )

            # 2. Extract specific trading account token (required for /mtr-api/ endpoints)
            accounts = data.get("tradingAccounts", [])
            for acct in accounts:
                if str(acct.get("tradingAccountId", "")) == str(self.account_id):
                    self.trading_token = acct.get("tradingApiToken")

                    system = acct.get("system") or {}
                    uuid   = system.get("uuid", "")
                    if uuid:
                        self.system_uuid = uuid
                    break

            # Fallback: check top-level
            if not self.trading_token:
                self.trading_token = data.get("tradingApiToken")

            # ✅ FIX 2: Log whether we actually got a trading token so failures
            #           are visible immediately rather than silently 401-ing.
            if not self.trading_token:
                logger.error(
                    "❌ Login succeeded but NO trading token found for account "
                    f"{self.account_id}. Available accounts in response: "
                    f"{[str(a.get('tradingAccountId')) for a in accounts]}"
                )
                logger.debug(f"Full login response: {data}")
                return False

            self.token_expiry = datetime.now() + timedelta(minutes=12)
            logger.info(f"✅ Login OK | Account: {self.account_id} | UUID: {self.system_uuid}")
            return True

        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Login HTTP error: {e.response.status_code} — {e.response.text[:300]}")
            return False
        except Exception as e:
            logger.error(f"❌ Login error: {e}")
            return False

    def ensure_auth(self):
        if (
            self.auth_token is None
            or self.trading_token is None
            or datetime.now() >= self.token_expiry
        ):
            return self.login()
        return True

    def _headers(self, is_trading=True):
        h = {
            "Accept":                    "application/json, text/plain, */*",
            "Content-Type":              "application/json",
            "Origin":                    self.platform_url,
            "Referer":                   f"{self.platform_url}/app/trade",
            "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "browser-request-send-time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.utcnow().microsecond // 1000:03d}Z",
            "cache-policy-expiration":   "skip-initial",
            "user-system-protection-type": "5",
        }
        # ✅ Only send auth-trading-api — no Authorization: Bearer (browser doesn't send it)
        token = self.trading_token if is_trading else self.auth_token
        if token:
            h["auth-trading-api"] = token
        return h

    # ── Core Request ───────────────────────────────────────────────────────────

    def request(self, method, path, retries=5, delay=15, **kwargs):
        """
        FIX: On a 401/403, wipe tokens AND rebuild headers after re-login
        before the next attempt. The old code called login() but then the
        `continue` jumped back to a stale ensure_auth() that saw fresh-looking
        tokens and skipped re-logging, so headers were never rebuilt with the
        new token.
        """
        is_trading = "/mtr-api/" in path or "/market-data-api/" in path

        for attempt in range(1, retries + 1):
            # ✅ FIX 3: ensure_auth() is called once here; after a 401 we call
            #           login() explicitly below and then build fresh headers
            #           on the very next line — no stale token risk.
            if not self.ensure_auth():
                logger.error("❌ Cannot authenticate. Aborting request.")
                return None

            url  = f"{self.platform_url}{path}"
            # ✅ FIX 4: Build headers AFTER ensure_auth so they always reflect
            #           the current token, including after a re-login.
            headers = self._headers(is_trading)

            try:
                resp = self.session.request(
                    method, url, headers=headers, timeout=30, **kwargs
                )

                if resp.status_code in (401, 403):
                    logger.warning(
                        f"{resp.status_code} on attempt {attempt}/{retries} "
                        f"for {path} — re-logging in..."
                    )
                    # ✅ FIX 5: Force a full re-login (which also resets the
                    #           session) so the next loop iteration starts clean.
                    self.auth_token    = None
                    self.trading_token = None
                    self.token_expiry  = datetime.min
                    if attempt < retries:
                        time.sleep(2)  # brief pause before re-login
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Timeout attempt {attempt}/{retries}. Retry in {delay}s...")
            except requests.exceptions.HTTPError as e:
                logger.warning(
                    f"⚠️ HTTP {e.response.status_code} attempt {attempt}/{retries}: "
                    f"{e.response.text[:200]}"
                )
            except Exception as e:
                logger.warning(f"⚠️ Error attempt {attempt}/{retries}: {e}")

            if attempt < retries:
                time.sleep(delay)

        logger.error(f"❌ {method} {path} failed after {retries} attempts.")
        return None

    # ── Trading Helpers ────────────────────────────────────────────────────────

    def get_balance(self):
        """Returns account balance as float, or None."""
        data = self.request("GET", f"/mtr-api/{self.system_uuid}/balance")
        if data is None:
            return None
        try:
            return float(data.get("balance", 0))
        except Exception as e:
            logger.error(f"Balance parse error: {e} | {data}")
            return None

    def get_open_positions(self, symbol=None):
        """
        Returns list of open positions (optionally filtered by symbol).
        Returns None on API failure.
        """
        data = self.request("GET", f"/mtr-api/{self.system_uuid}/open-positions")
        if data is None:
            return None
        positions = data.get("positions", []) if isinstance(data, dict) else data
        if symbol:
            mt_sym    = symbol.replace("_", "").upper()
            positions = [p for p in positions if p.get("symbol", "").upper() == mt_sym]
        return positions

    def open_position(self, symbol, side, lots, sl_price, tp_price):
        """
        Opens a market position.
        Returns (order_id, error_message)
        """
        mt_sym  = symbol.replace("_", "").upper()
        payload = {
            "instrument":      mt_sym,
            "orderSide":       side.upper(),
            "volume":          round(lots, 2),
            "slPrice":         round(sl_price, 5) if sl_price else 0,
            "tpPrice":         round(tp_price, 5) if tp_price else 0,
            "isMobile":        False,
            "source":          "Quick trade",
            "trailingDistance": 0,
        }
        data = self.request("POST", f"/mtr-api/{self.system_uuid}/position/open", json=payload)
        if data is None:
            return None, "API call failed after retries"

        status   = data.get("status", "")
        error    = data.get("errorMessage", "")
        order_id = data.get("orderId", "") or data.get("positionId", "")

        if status == "OK" or order_id:
            return order_id or "filled", None
        else:
            reason = error or data.get("nativeCode", str(data))
            return None, reason

    def close_position(self, position_id, symbol, open_side, volume):
        """Closes a specific position by ID using DELETE /position/{id}."""
        data = self.request(
            "DELETE", f"/mtr-api/{self.system_uuid}/position/{position_id}"
        )
        if data is None:
            return False, "API call failed after retries"

        status = data.get("status", "") if isinstance(data, dict) else ""
        error  = data.get("errorMessage", "") if isinstance(data, dict) else ""

        if status == "OK" or data == {}:
            return True, None
        else:
            reason = error or str(data)
            return False, reason

    def get_candles(self, symbol, count, granularity="H1"):
        """
        Fetches OHLC candle data as a pandas DataFrame.
        Uses confirmed endpoint: /market-data-api/{uuid}/candles
        granularity: 'M1','M5','M15','M30','H1','H4','D1'
        """
        mt_sym = symbol.replace("_", "").upper()
        path   = (
            f"/market-data-api/{self.system_uuid}/candles"
            f"?symbol={mt_sym}&interval={granularity}&candleSide=BID&amount={count}"
        )
        data = self.request("GET", path)

        if data is None:
            return None

        candles = data.get("candles", []) if isinstance(data, dict) else data
        if not candles:
            logger.warning(f"No candles returned for {symbol}")
            return None

        try:
            rows = []
            for c in candles:
                rows.append({
                    "open":  float(c.get("open",  c.get("o", 0))),
                    "high":  float(c.get("high",  c.get("h", 0))),
                    "low":   float(c.get("low",   c.get("l", 0))),
                    "close": float(c.get("close", c.get("c", 0))),
                })
            df = pd.DataFrame(rows)
            df.dropna(inplace=True)
            df.reset_index(drop=True, inplace=True)
            # Drop the last (incomplete) candle
            if len(df) > 1:
                df = df.iloc[:-1]
            return df
        except Exception as e:
            logger.error(f"Candle parse error for {symbol}: {e}")
            return None

    # ── Position Sizing ────────────────────────────────────────────────────────

    @staticmethod
    def calculate_lots(balance, risk_pct, sl_distance_price, symbol=""):
        """
        Calculate lot size for Match-Trader.

        Standard FX: 1 lot = 100,000 units.
        Gold/Silver (XAU/XAG): 1 lot = 100 oz.
        Crypto (BTC/ETH): 1 lot = 1 coin.
        Using the forex contract size for Gold or crypto makes
        every trade round down to the 0.01 minimum lot floor
        regardless of intended risk, since the correct lot size is
        orders of magnitude smaller in notional terms. This was
        silently happening before this fix: every non-forex trade
        was landing on the 0.01 floor instead of a real risk-based
        size, decoupling actual risk taken from both account
        balance and the intended risk_pct.

        risk_pct: e.g. 0.01 for 1%
        sl_distance_price: distance from entry to SL in price
        Returns lot size (float, minimum 0.01).
        """
        if sl_distance_price <= 0:
            return 0.01

        normalized_symbol = symbol.replace("_", "").replace("/", "").upper()

        if normalized_symbol.startswith("XAU") or normalized_symbol.startswith("XAG"):
            contract_size = 100      # 100 oz per standard lot (Gold/Silver)
        elif normalized_symbol.startswith("BTC") or normalized_symbol.startswith("ETH"):
            contract_size = 1        # 1 coin per standard lot (crypto)
        else:
            contract_size = 100000   # 100,000 units per standard forex lot

        risk_amount = balance * risk_pct
        lots        = risk_amount / (sl_distance_price * contract_size)
        lots        = max(0.01, round(lots, 2))
        return lots
