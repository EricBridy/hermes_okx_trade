#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes v9.0 — OKX Adaptive Perpetual Strategy
=============================================

A from-scratch rewrite of the v8.x line. Built on top of the network research
documented in hermes_v9_brain.py.

Pipeline per scan tick (every SCAN_INTERVAL seconds):

   1. Refresh balance, positions, open algo orders.
   2. close_and_settle() any position that hit TP/SL or has been closed
      externally — *always* through one path so the brain sees every result.
   3. monitor_positions(): apply Chandelier-style trailing stop, hard SL,
      time-based stop, and "signal-decay" early exit.
   4. scan_dual_channel():
        - Channel A: extreme |FR| with full feature snapshot.
        - Channel B: trend-momentum breakout with full feature snapshot.
      Each candidate is run through brain.evaluate() — the *single* place
      where the trade-or-skip decision is made (p_win, EV, Kelly fraction).
   5. open_position() for highest-EV candidate per slot if the slot is empty,
      sized via the brain's Kelly fraction.
   6. brain.save() (throttled).

After every closed trade:
   - Real PnL is fetched via positions-history (preferred) or bills (fallback).
   - brain.record_trade() does the SGD step and persists everything.

The only file dependency is hermes_v9_brain.py and ~/.okx/config.toml.
"""

from __future__ import annotations

import base64
import hmac
import json
import math
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from hermes_v9_brain import (
    FEATURE_NAMES,
    FeatureExtractor,
    StrategyBrain,
    TradeRecord,
    _safe_float,
)

# =====================================================================
#  Configuration
# =====================================================================

LEVERAGE = 6
HARD_TP_PCT = 0.030          # Hard take-profit (also lifts trailing exit)
HARD_SL_PCT = 0.015          # Hard stop-loss (sized via ATR but capped here)
ATR_TP_MULT = 3.0            # Trail / TP placement = entry +/- ATR * mult
ATR_SL_MULT = 1.5
TRAIL_ATR_MULT = 2.5         # Chandelier exit: HighestHigh - mult*ATR
TRAIL_ACTIVATE_PCT = 0.008   # Start trailing after +0.8% favourable move
TIME_STOP_LOSING = 360       # 6 min: if losing >1%, give up
TIME_STOP_FLAT = 1500        # 25 min: if not making progress, exit
SCAN_INTERVAL = 30
SYMBOL_COOLDOWN_SEC = 1800   # 30 min after closing a symbol
SWAP_COOLDOWN_SEC = 1800     # 30 min between consecutive swaps in same slot
MAX_CONCURRENT = 2           # Two slots: one Channel-A, one Channel-B

# Channel A (extreme funding)
FR_TRIGGER = 0.0005          # |FR| > 0.05%
FR_MAX = 0.01                # ignore absurdly high (likely data glitch)
A_VOL_SPIKE_MIN = 1.2

# Channel B (momentum / trend)
B_MIN_5M_CHG = 0.6           # |chg_5m| >= 0.6%
B_MIN_VOL_SPIKE = 1.3

# Risk gates that the brain *can override downward* but not upward
MIN_RISK_PCT = 0.10          # never use less than 10% of equity if we open
MAX_RISK_PCT = 0.40          # never use more than 40%

# Universe filters
MIN_24H_VOL = 3_000_000
MIN_PRICE = 0.001
SCAN_TOP_N = 60
OKX_TAKER_FEE = 0.0005
ROUND_TRIP_FEE = OKX_TAKER_FEE * 2

# Settlement-window protection (UTC)
SETTLEMENT_HOURS_UTC = [0, 8, 16]
PRE_SETTLEMENT_BUFFER_MIN = 12

# Daily limit & loss safeguards
MAX_DAILY_TRADES = 25
MAX_CONSECUTIVE_LOSSES = 4
LOSS_PAUSE_SEC = 1800
BLACKLIST_LOSS_COUNT = 4
BLACKLIST_DURATION = 43200    # 12 hours

# Chain data
CHAIN_DATA_TTL = 60
CHAIN_API_BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"
CHAIN_API_TIMEOUT = 3

# Files
SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
STATE_FILE = os.path.join(SCRIPT_DIR, "hermes_v9_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "hermes_v9_trades.log")
CHAIN_CACHE_FILE = os.path.join(SCRIPT_DIR, "hermes_v9_chain_cache.json")
BRAIN_FILE = os.path.join(SCRIPT_DIR, "hermes_v9_brain.json")
TRADES_JSONL = os.path.join(SCRIPT_DIR, "hermes_v9_trades.jsonl")

# Skip list for known-bad instruments
SKIP_SYMS = {"RLS-USDT-SWAP", "BILL-USDT-SWAP"}

# =====================================================================
#  Globals
# =====================================================================

INSTRUMENTS_CACHE: Dict[str, Dict[str, Any]] = {}
CHAIN_CACHE = {
    "smart_money_buy": set(),
    "hot_topics": set(),
    "smart_money_inflow": set(),
    "token_metrics": {},
    "last_update": 0.0,
}
BLACKLIST: Dict[str, float] = {}
LOSS_STREAK: Dict[str, int] = {}
DAILY_TRADE_COUNT = {"date": "", "count": 0}
OI_HISTORY: Dict[str, List[Tuple[float, float]]] = {}  # symbol -> [(ts, oi)]

positions: Dict[str, Dict[str, Any]] = {}
positions_lock = threading.Lock()
swap_cooldown_map: Dict[str, float] = {}

brain = StrategyBrain(brain_file=BRAIN_FILE, trades_log=TRADES_JSONL)


# =====================================================================
#  Utilities
# =====================================================================

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def utc8_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def is_near_settlement() -> Tuple[bool, int]:
    now_utc = datetime.now(timezone.utc)
    for h in SETTLEMENT_HOURS_UTC:
        s = now_utc.replace(hour=h, minute=0, second=0, microsecond=0)
        diff = (s - now_utc).total_seconds()
        if diff <= 0:
            s += timedelta(days=1)
            diff = (s - now_utc).total_seconds()
        if 0 < diff <= PRE_SETTLEMENT_BUFFER_MIN * 60:
            return True, int(diff)
    return False, 0


# =====================================================================
#  HTTP layer (signed + unsigned)
# =====================================================================

def get_creds() -> Tuple[str, str, str]:
    cfg_path = os.path.expanduser("~/.okx/config.toml")
    api = sec = pw = ""
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            for raw in f:
                ln = raw.strip()
                if ln.startswith("api_key"):
                    api = ln.split("=", 1)[1].strip().strip('"').strip("'")
                elif ln.startswith("secret_key"):
                    sec = ln.split("=", 1)[1].strip().strip('"').strip("'")
                elif ln.startswith("passphrase"):
                    pw = ln.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:
        log(f"⚠️ creds load failed: {e}")
    return api, sec, pw


def _signed_request(method: str, path: str, body: str = "") -> Dict[str, Any]:
    try:
        import requests
    except ImportError:
        log("❌ requests not installed")
        return {"code": "-1", "msg": "no requests"}
    api, sec, pw = get_creds()
    for attempt in range(3):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        msg = ts + method + path + body
        sign = base64.b64encode(
            hmac.new(sec.encode(), msg.encode(), "sha256").digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": api,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": pw,
            "Content-Type": "application/json",
        }
        try:
            url = "https://www.okx.com" + path
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=10)
            else:
                r = requests.post(url, headers=headers, data=body, timeout=10)
            d = r.json()
            if d.get("code") == "50011":
                time.sleep(1.5 * (attempt + 1))
                continue
            return d
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return {"code": "-1", "msg": str(e)}
    return {"code": "-1", "msg": "max retries"}


def okx_get(path: str) -> Dict[str, Any]:
    return _signed_request("GET", path, "")


def okx_post(path: str, body: str) -> Dict[str, Any]:
    return _signed_request("POST", path, body)


def curl_json(url: str, timeout: int = 10, retries: int = 3) -> Dict[str, Any]:
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            d = json.loads(r.stdout)
            if d.get("code") == "50011":
                time.sleep(0.5 * (attempt + 1))
                continue
            return d
        except Exception:
            pass
    return {}


def curl_post_json(url: str, body: Dict[str, Any], timeout: int = 8,
                   retries: int = 2) -> Dict[str, Any]:
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-sS", "-X", "POST", "--max-time", str(timeout),
                 "-H", "Content-Type: application/json",
                 "-H", "Accept-Encoding: identity",
                 "-H", "User-Agent: hermes-v9 (python)",
                 "-d", json.dumps(body), url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            return json.loads(r.stdout)
        except Exception:
            pass
    return {}


# =====================================================================
#  Public market data
# =====================================================================

def load_instruments_cache() -> None:
    global INSTRUMENTS_CACHE
    d = curl_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP", 15)
    if d.get("data"):
        for inst in d["data"]:
            iid = inst["instId"]
            INSTRUMENTS_CACHE[iid] = {
                "ctVal": _safe_float(inst.get("ctVal"), 1.0),
                "lotSz": _safe_float(inst.get("lotSz"), 1.0),
                "minSz": _safe_float(inst.get("minSz"), 1.0),
                "maxLev": _safe_float(inst.get("lever"), 10.0),
                "tickSz": inst.get("tickSz", "0.00001"),
            }
        log(f"✅ instruments cached: {len(INSTRUMENTS_CACHE)}")


def get_funding_rates(syms: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}

    def fetch(sym: str) -> Tuple[str, Optional[float]]:
        d = curl_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}", 5)
        if d.get("code") == "0" and d.get("data"):
            return sym, _safe_float(d["data"][0].get("fundingRate"), None)
        return sym, None

    with ThreadPoolExecutor(max_workers=10) as ex:
        for sym, rate in ex.map(fetch, syms):
            if rate is not None:
                out[sym] = rate
    return out


def get_open_interest(sym: str) -> Optional[float]:
    """Return current OI in number of contracts; None if API failed."""
    d = curl_json(f"https://www.okx.com/api/v5/public/open-interest?instId={sym}", 5)
    if d.get("data"):
        return _safe_float(d["data"][0].get("oi"), None)
    return None


def get_orderbook(sym: str, depth: int = 20) -> Optional[Dict[str, Any]]:
    """
    Fetch L2 orderbook top-N levels and pre-compute microprice / imbalance.
    Returns dict with mid, microprice, l2_bid_size, l2_ask_size.
    """
    d = curl_json(f"https://www.okx.com/api/v5/market/books?instId={sym}&sz={depth}", 5)
    if not d.get("data"):
        return None
    book = d["data"][0]
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None
    try:
        bid_px = _safe_float(bids[0][0])
        ask_px = _safe_float(asks[0][0])
        bid_sz0 = _safe_float(bids[0][1])
        ask_sz0 = _safe_float(asks[0][1])
        if bid_px <= 0 or ask_px <= 0 or (bid_sz0 + ask_sz0) <= 0:
            return None
        # Microprice: size-weighted mid (bid weighted by ask size, vice versa)
        microprice = (bid_px * ask_sz0 + ask_px * bid_sz0) / (bid_sz0 + ask_sz0)
        mid = (bid_px + ask_px) / 2.0
        # Aggregate L1..LN sizes
        bid_total = sum(_safe_float(b[1]) for b in bids[:depth])
        ask_total = sum(_safe_float(a[1]) for a in asks[:depth])
        return {
            "mid_price": mid,
            "microprice": microprice,
            "l2_bid_size": bid_total,
            "l2_ask_size": ask_total,
            "best_bid": bid_px,
            "best_ask": ask_px,
        }
    except (IndexError, ValueError):
        return None


# =====================================================================
#  K-line analysis
# =====================================================================

def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0 for d in deltas[:period]]
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: List[float], lows: List[float], closes: List[float],
         period: int = 14) -> Optional[float]:
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def get_klines(sym: str) -> Dict[str, Any]:
    """Pull 1m/5m/15m candles and compute everything we feed into the brain."""
    out: Dict[str, Any] = {"sym": sym}
    try:
        c1 = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=1m&limit=30", 5)
        c5 = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=5m&limit=50", 5)
        c15 = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=15m&limit=20", 5)

        # ---- 1m -----------------------------------------------------------
        if c1.get("data") and len(c1["data"]) >= 16:
            cd = c1["data"][::-1]
            cls = [_safe_float(c[4]) for c in cd]
            vol = [_safe_float(c[5]) for c in cd]
            out["chg_5m"] = (cls[-1] - cls[-5]) / cls[-5] * 100 if cls[-5] > 0 else 0
            r1 = sum(vol[-3:]) / 3
            base = sum(vol[:-3]) / max(len(vol[:-3]), 1)
            out["vol_1m_ratio"] = r1 / max(base, 1e-9)
            out["rsi_1m"] = _rsi(cls, 14) or 50.0
            out["last_price"] = cls[-1]

        # ---- 5m -----------------------------------------------------------
        if c5.get("data") and len(c5["data"]) >= 28:
            cd = c5["data"][::-1]
            cls = [_safe_float(c[4]) for c in cd]
            highs = [_safe_float(c[2]) for c in cd]
            lows = [_safe_float(c[3]) for c in cd]
            vols = [_safe_float(c[5]) for c in cd]
            taker_buys = [_safe_float(c[8]) for c in cd] if len(cd[0]) >= 9 else None
            out["rsi_5m"] = _rsi(cls, 14) or 50.0
            out["ema12_5m"] = _ema(cls, 12) or 0.0
            out["ema26_5m"] = _ema(cls, 26) or 0.0
            atr14 = _atr(highs, lows, cls, 14)
            if atr14 is not None and cls[-1] > 0:
                out["atr_pct"] = atr14 / cls[-1] * 100
                out["atr_abs"] = atr14
            # Bollinger bands width over last 20
            if len(cls) >= 20:
                bb_c = cls[-20:]
                m = sum(bb_c) / 20
                std = (sum((c - m) ** 2 for c in bb_c) / 20) ** 0.5
                if m > 0:
                    out["bb_width"] = (4 * std) / m * 100
            # 5m taker buy ratio over last 6 bars (~30 min)
            recent_buys = sum(taker_buys[-6:]) if taker_buys else 0
            recent_total = sum(vols[-6:])
            if recent_total > 0 and recent_buys > 0:
                out["taker_buy_vol_5m"] = recent_buys
                out["taker_sell_vol_5m"] = max(recent_total - recent_buys, 0)
            else:
                # OKX tickers don't always include taker flow; fall back to
                # a price-action proxy.
                up_vol = sum(vols[i] for i in range(-6, 0) if cls[i] > cls[i - 1])
                dn_vol = sum(vols[i] for i in range(-6, 0) if cls[i] < cls[i - 1])
                out["taker_buy_vol_5m"] = up_vol
                out["taker_sell_vol_5m"] = dn_vol
            # Highest high / lowest low for chandelier exit
            out["hh_20"] = max(highs[-20:])
            out["ll_20"] = min(lows[-20:])

        # ---- 15m (Heikin-Ashi trend) -------------------------------------
        if c15.get("data") and len(c15["data"]) >= 5:
            cd = c15["data"][::-1]
            cls = [_safe_float(c[4]) for c in cd]
            ops = [_safe_float(c[1]) for c in cd]
            highs = [_safe_float(c[2]) for c in cd]
            lows = [_safe_float(c[3]) for c in cd]
            ha_open = []
            ha_close = []
            for i in range(len(cls)):
                hc = (ops[i] + highs[i] + lows[i] + cls[i]) / 4
                ha_close.append(hc)
                if i == 0:
                    ha_open.append((ops[i] + cls[i]) / 2)
                else:
                    ha_open.append((ha_open[-1] + ha_close[i - 1]) / 2)
            ups = sum(1 for i in range(-4, -1) if ha_close[i] > ha_open[i])
            dns = sum(1 for i in range(-4, -1) if ha_close[i] < ha_open[i])
            chg3 = (cls[-2] - cls[-5]) / cls[-5] * 100 if len(cls) >= 5 and cls[-5] > 0 else 0
            if ups >= 2 and chg3 > 0.3:
                out["ha_trend_15m"] = "UP"
            elif dns >= 2 and chg3 < -0.3:
                out["ha_trend_15m"] = "DOWN"
            else:
                out["ha_trend_15m"] = "FLAT"
            if len(cls) >= 15:
                out["rsi_15m"] = _rsi(cls, 14) or 50.0
            out["ema12_15m"] = _ema(cls, 12) or 0.0
            out["ema26_15m"] = _ema(cls, 14) or 0.0  # can't quite get 26 with 20 candles
    except Exception as e:
        log(f"  ⚠️ klines {sym}: {e}")
    return out


# =====================================================================
#  Chain data (binance web3) — kept from v8.x but cleaner
# =====================================================================

def fetch_smart_money_signals() -> set:
    out = set()
    for chain_id in ("CT_501", "56"):
        try:
            d = curl_post_json(
                CHAIN_API_BASE + "/buw/wallet/web/signal/smart-money/ai",
                {"chainId": chain_id, "page": 1, "pageSize": 50},
                timeout=CHAIN_API_TIMEOUT,
            )
            for sig in (d.get("data") or []):
                if sig.get("direction") == "buy" and sig.get("smartMoneyCount", 0) >= 2:
                    t = (sig.get("ticker") or "").upper()
                    if t:
                        out.add(t)
        except Exception:
            pass
    return out


def fetch_hot_topics() -> set:
    out = set()
    for chain_id in ("CT_501", "56"):
        try:
            d = curl_json(
                "https://web3.binance.com/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai"
                f"?chainId={chain_id}&rankType=20&sort=20&asc=false",
                CHAIN_API_TIMEOUT,
            )
            for topic in (d.get("data") or []):
                for t in topic.get("tokenList", [])[:5]:
                    sym = (t.get("symbol") or "").upper()
                    if sym:
                        out.add(sym)
        except Exception:
            pass
    return out


def fetch_smart_money_inflow() -> set:
    out = set()
    for chain_id in ("CT_501", "56"):
        try:
            d = curl_post_json(
                "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai",
                {"chainId": chain_id, "period": "4h", "tagType": 2},
                timeout=CHAIN_API_TIMEOUT,
            )
            for item in (d.get("data") or [])[:20]:
                sym = (item.get("tokenName") or "").upper()
                if sym:
                    out.add(sym)
        except Exception:
            pass
    return out


def fetch_token_metrics() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for chain_id in ("CT_501", "56"):
        try:
            d = curl_json(
                "https://web3.binance.com/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai"
                f"?chainId={chain_id}&rankType=10&sort=20&asc=false",
                CHAIN_API_TIMEOUT,
            )
            for topic in (d.get("data") or []):
                for t in topic.get("tokenList", []):
                    sym = (t.get("symbol") or "").upper()
                    if not sym:
                        continue
                    holders = int(t.get("holders") or 0)
                    if holders < 50:
                        continue
                    existing = out.get(sym, {})
                    if holders > existing.get("holders", 0):
                        out[sym] = {
                            "traders24h": int(t.get("uniqueTrader24h") or 0),
                            "trades24h": int(t.get("count24h") or 0),
                            "sm_pct": _safe_float(t.get("smartMoneyHoldingPercent")),
                            "insider_pct": _safe_float(t.get("insiderHoldingPercent")),
                            "holders": holders,
                            "kol": int(t.get("kolHolders") or 0),
                        }
        except Exception:
            pass
    return out


def update_chain_cache() -> None:
    now = time.time()
    if now - CHAIN_CACHE["last_update"] < CHAIN_DATA_TTL:
        return
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_sm = ex.submit(fetch_smart_money_signals)
        f_ht = ex.submit(fetch_hot_topics)
        f_in = ex.submit(fetch_smart_money_inflow)
        f_tm = ex.submit(fetch_token_metrics)
        try:
            CHAIN_CACHE["smart_money_buy"] = f_sm.result(timeout=CHAIN_API_TIMEOUT + 2)
            CHAIN_CACHE["hot_topics"] = f_ht.result(timeout=CHAIN_API_TIMEOUT + 2)
            CHAIN_CACHE["smart_money_inflow"] = f_in.result(timeout=CHAIN_API_TIMEOUT + 2)
            CHAIN_CACHE["token_metrics"] = f_tm.result(timeout=CHAIN_API_TIMEOUT + 2)
            CHAIN_CACHE["last_update"] = now
            save_chain_cache()
            log(f"  [chain] sm={len(CHAIN_CACHE['smart_money_buy'])} "
                f"hot={len(CHAIN_CACHE['hot_topics'])} "
                f"inf={len(CHAIN_CACHE['smart_money_inflow'])} "
                f"tm={len(CHAIN_CACHE['token_metrics'])}")
        except Exception as e:
            log(f"  [chain] timeout: {e}")
            CHAIN_CACHE["last_update"] = now


def save_chain_cache() -> None:
    try:
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        with open(CHAIN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "smart_money_buy": list(CHAIN_CACHE["smart_money_buy"]),
                "hot_topics": list(CHAIN_CACHE["hot_topics"]),
                "smart_money_inflow": list(CHAIN_CACHE["smart_money_inflow"]),
                "token_metrics": CHAIN_CACHE["token_metrics"],
                "last_update": CHAIN_CACHE["last_update"],
            }, f)
    except Exception:
        pass


def load_chain_cache() -> None:
    try:
        if not os.path.exists(CHAIN_CACHE_FILE):
            return
        with open(CHAIN_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        CHAIN_CACHE["smart_money_buy"] = set(d.get("smart_money_buy", []))
        CHAIN_CACHE["hot_topics"] = set(d.get("hot_topics", []))
        CHAIN_CACHE["smart_money_inflow"] = set(d.get("smart_money_inflow", []))
        CHAIN_CACHE["token_metrics"] = d.get("token_metrics", {})
        CHAIN_CACHE["last_update"] = d.get("last_update", 0)
        log(f"  [chain] restored sm={len(CHAIN_CACHE['smart_money_buy'])}")
    except Exception:
        pass


def chain_score_for(ticker: str) -> Tuple[int, List[str]]:
    sym = ticker.upper()
    score = 0
    tags: List[str] = []
    if sym in CHAIN_CACHE["smart_money_buy"]:
        score += 2
        tags.append("SM")
    if sym in CHAIN_CACHE["hot_topics"]:
        score += 2
        tags.append("HOT")
    if sym in CHAIN_CACHE["smart_money_inflow"]:
        score += 2
        tags.append("INF")
    metrics = CHAIN_CACHE["token_metrics"].get(sym)
    if metrics:
        traders = metrics.get("traders24h", 0)
        if traders > 2000:
            score += 2; tags.append(f"social{traders}")
        elif traders > 500:
            score += 1; tags.append(f"social{traders}")
        sm_pct = metrics.get("sm_pct", 0)
        if sm_pct > 3:
            score += 2; tags.append(f"whale{sm_pct:.1f}%")
        elif sm_pct > 1:
            score += 1; tags.append(f"whale{sm_pct:.1f}%")
        insider = metrics.get("insider_pct", 0)
        if insider > 5:
            score -= 2; tags.append(f"⚠insider{insider:.1f}%")
        elif insider > 2:
            score -= 1; tags.append(f"insider{insider:.1f}%")
        elif 0 < insider < 0.5:
            score += 1; tags.append("decentralised")
    return score, tags


# =====================================================================
#  State (file-backed)
# =====================================================================

def load_state() -> Dict[str, Any]:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "consecutive_losses": 0,
        "pause_until": None,
        "last_trade": {},
        "total_pnl": 0.0,
        "trade_count": 0,
        "position_meta": {},
    }


def save_state(state: Dict[str, Any]) -> None:
    try:
        meta = {}
        for pid, p in positions.items():
            meta[pid] = {
                "channel": p.get("channel", ""),
                "direction": p.get("direction"),
                "open_ts": p.get("open_ts"),
                "features": p.get("features", {}),
                "p_win": p.get("p_win", 0.5),
                "ev": p.get("ev", 0.0),
                "fraction": p.get("fraction", 0.0),
                "atr_abs": p.get("atr_abs", 0.0),
            }
        state["position_meta"] = meta
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"⚠️ save_state: {e}")


# =====================================================================
#  Account / position helpers
# =====================================================================

def get_balance() -> float:
    try:
        d = okx_get("/api/v5/account/balance?ccy=USDT")
        if d.get("data") and d["data"][0].get("details"):
            det = d["data"][0]["details"][0]
            return _safe_float(det.get("availBal") or det.get("eq"))
    except Exception as e:
        log(f"⚠️ balance: {e}")
    return 0.0


def get_position(inst_id: str) -> Optional[Dict[str, Any]]:
    d = okx_get(f"/api/v5/account/positions?instId={inst_id}")
    if d.get("data"):
        for p in d["data"]:
            if p.get("instId") == inst_id and _safe_float(p.get("pos")) != 0:
                return {
                    "pos": _safe_float(p["pos"]),
                    "avgPx": _safe_float(p["avgPx"]),
                    "upl": _safe_float(p.get("upl")),
                }
    return None


def get_all_positions_raw() -> List[Dict[str, Any]]:
    d = okx_get("/api/v5/account/positions")
    out = []
    if d.get("data"):
        for p in d["data"]:
            if _safe_float(p.get("pos")) != 0:
                out.append({
                    "instId": p["instId"],
                    "pos": _safe_float(p["pos"]),
                    "avgPx": _safe_float(p["avgPx"]),
                    "upl": _safe_float(p.get("upl")),
                })
    return out


def get_realized_pnl(inst_id: str, since_ts_ms: int,
                     retries: int = 4, sleep: float = 0.6) -> float:
    """
    Best-effort realized PnL since `since_ts_ms`.

    Order of preference:
      1. /api/v5/account/positions-history     (uTime, pnl)
      2. /api/v5/account/bills                 (subType 212/214 close pnl)
    """
    for attempt in range(retries):
        # Preferred: positions-history (most reliable post-close)
        d = okx_get(f"/api/v5/account/positions-history?instId={inst_id}&limit=10")
        if d.get("code") == "0" and d.get("data"):
            for row in d["data"]:
                u = int(_safe_float(row.get("uTime"), 0))
                if u >= since_ts_ms - 1000:  # 1s leeway
                    pnl = _safe_float(row.get("pnl"))
                    if pnl != 0:
                        return pnl
        # Fallback: bills
        d2 = okx_get(f"/api/v5/account/bills?instId={inst_id}&instType=SWAP&limit=20")
        if d2.get("code") == "0" and d2.get("data"):
            total = 0.0
            for b in d2["data"]:
                ts = int(_safe_float(b.get("ts"), 0))
                if ts >= since_ts_ms - 1000:
                    pnl = _safe_float(b.get("pnl"))
                    fee = _safe_float(b.get("fee"))
                    total += pnl + fee  # fee is already negative for taker
            if total != 0:
                return total
        time.sleep(sleep)
    return 0.0


def cancel_algos(inst_id: str, algo_ids: List[str]) -> None:
    if not algo_ids:
        return
    for aid in algo_ids:
        try:
            okx_post("/api/v5/trade/cancel-algos",
                     json.dumps([{"instId": inst_id, "algoId": aid}]))
        except Exception:
            pass


def close_market(inst_id: str, algo_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    if algo_ids:
        cancel_algos(inst_id, algo_ids)
    pos = get_position(inst_id)
    if not pos:
        return {"code": "0", "msg": "already closed"}
    side = "sell" if pos["pos"] > 0 else "buy"
    return okx_post("/api/v5/trade/order", json.dumps({
        "instId": inst_id, "tdMode": "cross",
        "side": side, "ordType": "market",
        "sz": str(int(abs(pos["pos"]))), "reduceOnly": True,
    }))


# =====================================================================
#  Daily limit & blacklist
# =====================================================================

def check_blacklist(sym: str) -> bool:
    now = time.time()
    if sym in BLACKLIST:
        if now < BLACKLIST[sym]:
            return True
        del BLACKLIST[sym]
    return False


def update_loss_streak(sym: str, pnl: float) -> None:
    if pnl > 0:
        LOSS_STREAK[sym] = 0
    else:
        LOSS_STREAK[sym] = LOSS_STREAK.get(sym, 0) + 1
        if LOSS_STREAK[sym] >= BLACKLIST_LOSS_COUNT:
            BLACKLIST[sym] = time.time() + BLACKLIST_DURATION
            log(f"  🚫 {sym} blacklisted for {BLACKLIST_DURATION//3600}h")
            LOSS_STREAK[sym] = 0


def check_daily_limit() -> bool:
    today = utc8_now().strftime("%Y-%m-%d")
    if DAILY_TRADE_COUNT["date"] != today:
        DAILY_TRADE_COUNT["date"] = today
        DAILY_TRADE_COUNT["count"] = 0
    return DAILY_TRADE_COUNT["count"] >= MAX_DAILY_TRADES


def incr_daily_trade() -> None:
    today = utc8_now().strftime("%Y-%m-%d")
    if DAILY_TRADE_COUNT["date"] != today:
        DAILY_TRADE_COUNT["date"] = today
        DAILY_TRADE_COUNT["count"] = 0
    DAILY_TRADE_COUNT["count"] += 1


# =====================================================================
#  Snapshot construction
# =====================================================================

def build_snapshot(sym: str, direction: str, fr: Optional[float],
                   klines: Dict[str, Any],
                   orderbook: Optional[Dict[str, Any]],
                   oi_now: Optional[float],
                   chain_score: int) -> Dict[str, Any]:
    """Compose everything the brain needs into a single snapshot dict."""
    snap: Dict[str, Any] = {"direction": direction}
    if fr is not None:
        snap["funding_rate"] = fr
    if oi_now is not None:
        snap["oi_now"] = oi_now
        # Look up historical OI ~15 min back
        hist = OI_HISTORY.get(sym, [])
        cutoff = time.time() - 900
        old_pt = next((o for ts, o in reversed(hist) if ts <= cutoff), None)
        if old_pt is not None:
            snap["oi_15m_ago"] = old_pt
    snap.update(klines)
    if orderbook:
        snap.update(orderbook)
    snap["chain_score"] = chain_score
    return snap


def update_oi_history(sym: str, oi: float) -> None:
    arr = OI_HISTORY.setdefault(sym, [])
    arr.append((time.time(), oi))
    # Keep last 30 points (~15 min @ 30s scan)
    if len(arr) > 30:
        OI_HISTORY[sym] = arr[-30:]


# =====================================================================
#  Order placement
# =====================================================================

def open_position(sym: str, direction: str, capital_usd: float,
                  evaluation: Dict[str, Any], snapshot: Dict[str, Any],
                  channel: str) -> Optional[Dict[str, Any]]:
    specs = INSTRUMENTS_CACHE.get(sym)
    if not specs:
        log(f"  ❌ {sym} no instrument spec")
        return None
    leverage = min(LEVERAGE, int(specs["maxLev"]))
    margin = capital_usd * 0.95
    notional_max = margin * leverage

    d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={sym}", 5)
    if not d.get("data"):
        return None
    last = _safe_float(d["data"][0].get("last"))
    if last <= 0:
        log(f"  ❌ {sym} bad last={d['data'][0].get('last')!r}")
        return None

    lots = int(notional_max / (specs["ctVal"] * last))
    if lots < 1:
        log(f"  ❌ {sym} lots<1 (capital=${capital_usd:.2f})")
        return None

    notional_actual = lots * specs["ctVal"] * last
    fee_cost = notional_actual * ROUND_TRIP_FEE
    if notional_actual * HARD_TP_PCT < fee_cost:
        log(f"  ❌ {sym} TP profit < fee, skip")
        return None

    # ATR-based TP/SL prices (clipped by hard limits)
    atr_abs = snapshot.get("atr_abs", 0)
    if atr_abs > 0:
        atr_tp_dist = min(atr_abs * ATR_TP_MULT, last * HARD_TP_PCT)
        atr_sl_dist = min(atr_abs * ATR_SL_MULT, last * HARD_SL_PCT)
    else:
        atr_tp_dist = last * HARD_TP_PCT
        atr_sl_dist = last * HARD_SL_PCT

    tick = _safe_float(specs.get("tickSz"), 1e-5)
    tick_dec = max(0, len(str(specs.get("tickSz", "0.00001")).rstrip("0").split(".")[-1]))

    def fmt(p: float) -> str:
        rounded = round(p / tick) * tick
        return f"{rounded:.{tick_dec}f}" if tick_dec > 0 else str(rounded)

    if direction == "LONG":
        tp = fmt(last + atr_tp_dist)
        sl = fmt(last - atr_sl_dist)
        side, close_side = "buy", "sell"
    else:
        tp = fmt(last - atr_tp_dist)
        sl = fmt(last + atr_sl_dist)
        side, close_side = "sell", "buy"

    # Place leverage + market open
    okx_post("/api/v5/account/set-leverage", json.dumps({
        "instId": sym, "lever": str(leverage), "mgnMode": "cross",
    }))
    open_resp = okx_post("/api/v5/trade/order", json.dumps({
        "instId": sym, "tdMode": "cross",
        "side": side, "ordType": "market", "sz": str(lots),
    }))
    if open_resp.get("code") != "0":
        log(f"  ❌ open failed {sym}: {open_resp.get('msg', '')}")
        return None

    time.sleep(0.5)
    pos_now = get_position(sym)
    if not pos_now:
        log(f"  ❌ {sym} fill confirm failed, retrying close")
        close_market(sym)
        return None

    avg = pos_now["avgPx"]
    sz = int(abs(pos_now["pos"]))

    # Place TP and SL algos in parallel
    algo_ids: List[str] = []
    placed: set = set()
    placed_lock = threading.Lock()

    def place_algo(label: str, px: str, trig_key: str):
        body = {
            "instId": sym, "tdMode": "cross",
            "side": close_side, "sz": str(sz),
            "ordType": "conditional",
            trig_key: px,
            "tpOrdPx" if label == "TP" else "slOrdPx": "-1",
            "reduceOnly": True,
        }
        r = okx_post("/api/v5/trade/order-algo", json.dumps(body))
        if r.get("code") == "0":
            with placed_lock:
                algo_ids.append(r["data"][0].get("algoId", ""))
                placed.add(label)
        else:
            log(f"  ❌ {label}@{px} failed: {r.get('msg', '')}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(place_algo, "TP", tp, "tpTriggerPx")
        ex.submit(place_algo, "SL", sl, "slTriggerPx")
    time.sleep(0.3)

    if "TP" not in placed or "SL" not in placed:
        log(f"  🚨 {sym} TP/SL incomplete -> emergency close")
        for _ in range(3):
            time.sleep(0.8)
            close_market(sym, algo_ids)
            if not get_position(sym):
                break
        return None

    log(f"  ✅ [{channel}] {direction} {sym} {sz}lots @ ${avg} "
        f"TP=${tp} SL=${sl} f={evaluation['fraction']:.0%} "
        f"p_win={evaluation['p_win']:.2%}")

    return {
        "instId": sym,
        "channel": channel,
        "direction": direction,
        "entry_price": avg,
        "sz": sz,
        "algo_ids": algo_ids,
        "open_ts": time.time(),
        "open_ts_ms": int(time.time() * 1000),
        "notional": notional_actual,
        "atr_abs": atr_abs,
        "highest_pnl_pct": 0.0,
        "lowest_pnl_pct": 0.0,
        "trail_active": False,
        "highest_price": avg if direction == "LONG" else 0,
        "lowest_price": avg if direction == "SHORT" else 1e18,
        "features": evaluation["features"],
        "p_win": evaluation["p_win"],
        "ev": evaluation["ev"],
        "fraction": evaluation["fraction"],
    }


# =====================================================================
#  Position monitoring
# =====================================================================

def monitor_position(inst_id: str, p: Dict[str, Any],
                     klines: Dict[str, Any]) -> Optional[str]:
    """
    Returns one of:
        None             -> still holding
        "TP" / "SL"      -> server-side algo did the job; we just need to settle
        "TRAIL"          -> chandelier hit
        "TIME_LOSS"      -> time stop while losing
        "TIME_FLAT"      -> time stop while flat
        "SIGNAL_DECAY"   -> signal flipped strongly against us
        "EXTERNAL"       -> position vanished from OKX (TP/SL filled or manual)
    """
    pos = get_position(inst_id)
    if not pos:
        return "EXTERNAL"

    last = klines.get("last_price")
    if last is None or last <= 0:
        return None

    entry = p["entry_price"]
    direction = p["direction"]
    if direction == "LONG":
        pnl_pct = (last - entry) / entry * 100
        if last > p["highest_price"]:
            p["highest_price"] = last
    else:
        pnl_pct = (entry - last) / entry * 100
        if last < p["lowest_price"] or p["lowest_price"] >= 1e17:
            p["lowest_price"] = last

    if pnl_pct > p["highest_pnl_pct"]:
        p["highest_pnl_pct"] = pnl_pct
    if pnl_pct < p["lowest_pnl_pct"]:
        p["lowest_pnl_pct"] = pnl_pct

    elapsed = time.time() - p["open_ts"]

    # Hard TP (server algo should hit; this is belt-and-braces)
    if pnl_pct >= HARD_TP_PCT * 100:
        log(f"  🎯 {inst_id} hit HARD_TP {pnl_pct:+.2f}%")
        close_market(inst_id, p.get("algo_ids"))
        return "TP"

    # Hard SL
    if pnl_pct <= -HARD_SL_PCT * 100:
        log(f"  🛑 {inst_id} hit HARD_SL {pnl_pct:+.2f}%")
        close_market(inst_id, p.get("algo_ids"))
        return "SL"

    # Chandelier-style trailing exit (active only after we've moved
    # favourably enough)
    if pnl_pct >= TRAIL_ACTIVATE_PCT * 100 or p["trail_active"]:
        if not p["trail_active"]:
            p["trail_active"] = True
            log(f"  🪝 {inst_id} trail active @ +{pnl_pct:.2f}%")
        # Compute the trailing level using ATR if available, otherwise %
        atr = p.get("atr_abs", 0)
        if atr > 0:
            trail_dist = atr * TRAIL_ATR_MULT
        else:
            trail_dist = entry * 0.005  # 0.5% fallback
        if direction == "LONG":
            stop = p["highest_price"] - trail_dist
            if last <= stop:
                log(f"  🪝 {inst_id} trail STOP @ {last:.6g} "
                    f"(peak {p['highest_price']:.6g}, dist={trail_dist:.6g})")
                close_market(inst_id, p.get("algo_ids"))
                return "TRAIL"
        else:
            stop = p["lowest_price"] + trail_dist
            if last >= stop:
                log(f"  🪝 {inst_id} trail STOP @ {last:.6g} "
                    f"(trough {p['lowest_price']:.6g}, dist={trail_dist:.6g})")
                close_market(inst_id, p.get("algo_ids"))
                return "TRAIL"

    # Signal decay: same direction & strong reversal in 5m
    chg5 = klines.get("chg_5m", 0)
    if elapsed > 120:  # give it 2 min to settle
        if direction == "LONG" and chg5 < -0.7 and pnl_pct < -0.3:
            log(f"  🩺 {inst_id} signal decay (5m{chg5:+.2f}%) pnl={pnl_pct:+.2f}%")
            close_market(inst_id, p.get("algo_ids"))
            return "SIGNAL_DECAY"
        if direction == "SHORT" and chg5 > 0.7 and pnl_pct < -0.3:
            log(f"  🩺 {inst_id} signal decay (5m{chg5:+.2f}%) pnl={pnl_pct:+.2f}%")
            close_market(inst_id, p.get("algo_ids"))
            return "SIGNAL_DECAY"

    # Time stops
    if elapsed > TIME_STOP_LOSING and pnl_pct < -1.0:
        log(f"  ⏰ {inst_id} TIME_LOSS {int(elapsed)}s pnl={pnl_pct:+.2f}%")
        close_market(inst_id, p.get("algo_ids"))
        return "TIME_LOSS"
    if elapsed > TIME_STOP_FLAT and abs(pnl_pct) < 0.3:
        log(f"  ⏰ {inst_id} TIME_FLAT {int(elapsed)}s pnl={pnl_pct:+.2f}%")
        close_market(inst_id, p.get("algo_ids"))
        return "TIME_FLAT"

    return None


# =====================================================================
#  THE single settlement path
# =====================================================================

def close_and_settle(inst_id: str, exit_reason: str, state: Dict[str, Any]) -> None:
    """
    Single source of truth for post-close bookkeeping. Every exit must go
    through here so the brain sees every outcome.
    """
    with positions_lock:
        p = positions.pop(inst_id, None)
    if p is None:
        return

    open_ts_ms = p.get("open_ts_ms", int(p.get("open_ts", time.time()) * 1000))
    pnl = get_realized_pnl(inst_id, open_ts_ms)
    if pnl == 0.0:
        # Fallback: estimate from notional
        last_d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", 5)
        last_px = _safe_float(last_d.get("data", [{}])[0].get("last")) if last_d.get("data") else 0
        if last_px > 0:
            entry = p["entry_price"]
            sign = 1 if p["direction"] == "LONG" else -1
            specs = INSTRUMENTS_CACHE.get(inst_id, {"ctVal": 1})
            notional_pnl = sign * (last_px - entry) * p["sz"] * specs["ctVal"]
            fee_est = -p["notional"] * ROUND_TRIP_FEE
            pnl = notional_pnl + fee_est
            log(f"  ⚠️ {inst_id} fallback pnl estimate: ${pnl:.4f}")

    # Compute fractional PnL relative to deployed margin
    margin_used = p.get("notional", 0) / max(LEVERAGE, 1)
    pnl_pct = pnl / margin_used if margin_used > 0 else 0.0

    log(f"  📊 {inst_id} CLOSED via {exit_reason} pnl=${pnl:+.4f} "
        f"({pnl_pct:+.2%}) hi/lo={p['highest_pnl_pct']:+.2f}%/{p['lowest_pnl_pct']:+.2f}%")

    # --- State updates ---
    if pnl > 0:
        state["consecutive_losses"] = 0
    else:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
            state["pause_until"] = until.isoformat()
            log(f"  ⏸️ {state['consecutive_losses']} consecutive losses, "
                f"pause for {LOSS_PAUSE_SEC//60} min")
    state["total_pnl"] = state.get("total_pnl", 0) + pnl
    state["trade_count"] = state.get("trade_count", 0) + 1
    state.setdefault("last_trade", {})[inst_id] = time.time()

    # --- Brain update ---
    last_px = 0.0
    try:
        d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", 5)
        if d.get("data"):
            last_px = _safe_float(d["data"][0].get("last"))
    except Exception:
        pass
    rec = TradeRecord(
        symbol=inst_id,
        direction=p["direction"],
        entry_price=p["entry_price"],
        exit_price=last_px,
        pnl_usd=pnl,
        pnl_pct=pnl_pct,
        open_ts=p["open_ts"],
        close_ts=time.time(),
        features=p.get("features", {}),
        p_win_predicted=p.get("p_win", 0.5),
        expected_value=p.get("ev", 0.0),
        fraction_used=p.get("fraction", 0.0),
        exit_reason=exit_reason,
        extra={
            "channel": p.get("channel"),
            "highest_pnl_pct": p.get("highest_pnl_pct"),
            "lowest_pnl_pct": p.get("lowest_pnl_pct"),
        },
    )
    diag = brain.record_trade(rec)
    log(f"  🧠 brain: p_pred={rec.p_win_predicted:.2%} actual={'W' if pnl>0 else 'L'} "
        f"brier={diag['brier']:.3f} avg={diag['avg_brier']:.3f} "
        f"b={diag['kelly_b']:.2f}")

    # Blacklist / streak
    update_loss_streak(inst_id, pnl)

    # Persist
    save_state(state)


# =====================================================================
#  Scanner
# =====================================================================

def scan_universe() -> Tuple[List[Dict[str, Any]], Dict[str, float],
                              Dict[str, Dict[str, Any]],
                              Dict[str, Optional[Dict[str, Any]]],
                              Dict[str, Optional[float]]]:
    """
    Returns:
        candidates_raw: list of dicts with sym, direction, fr, klines, ob, oi, snap
        fr_map: full FR map for monitoring positions
        kline_map: full kline map for monitoring positions
        ob_map: orderbooks
        oi_map: open interest
    """
    d = curl_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP", 15)
    if not d.get("data"):
        return [], {}, {}, {}, {}

    universe = []
    for x in d["data"]:
        if not x["instId"].endswith("USDT-SWAP"):
            continue
        last = _safe_float(x.get("last"))
        vol24 = _safe_float(x.get("volCcy24h"))
        if last < MIN_PRICE or vol24 < MIN_24H_VOL:
            continue
        if x["instId"] in SKIP_SYMS:
            continue
        universe.append({"instId": x["instId"], "last": last, "vol24h": vol24})
    universe.sort(key=lambda y: y["vol24h"], reverse=True)
    universe = universe[:SCAN_TOP_N]
    syms = [u["instId"] for u in universe]

    fr_map = get_funding_rates(syms)

    # Pull klines for top 30 + any held position
    target_syms = list({*syms[:30], *(p for p in positions if p in syms)})
    kline_map: Dict[str, Dict[str, Any]] = {}
    ob_map: Dict[str, Optional[Dict[str, Any]]] = {}
    oi_map: Dict[str, Optional[float]] = {}

    with ThreadPoolExecutor(max_workers=12) as ex:
        future_to_sym = {ex.submit(get_klines, s): s for s in target_syms}
        future_ob = {ex.submit(get_orderbook, s, 20): s for s in target_syms}
        future_oi = {ex.submit(get_open_interest, s): s for s in target_syms}

        for f in as_completed(future_to_sym):
            sym = future_to_sym[f]
            try:
                kline_map[sym] = f.result() or {"sym": sym}
            except Exception:
                kline_map[sym] = {"sym": sym}
        for f in as_completed(future_ob):
            sym = future_ob[f]
            try:
                ob_map[sym] = f.result()
            except Exception:
                ob_map[sym] = None
        for f in as_completed(future_oi):
            sym = future_oi[f]
            try:
                oi_map[sym] = f.result()
                if oi_map[sym] is not None:
                    update_oi_history(sym, oi_map[sym])
            except Exception:
                oi_map[sym] = None

    update_chain_cache()

    # Build candidate list (each direction is a separate candidate)
    candidates: List[Dict[str, Any]] = []
    for u in universe:
        sym = u["instId"]
        if check_blacklist(sym):
            continue
        ticker = sym.replace("-USDT-SWAP", "")
        fr = fr_map.get(sym)
        kl = kline_map.get(sym, {})
        ob = ob_map.get(sym)
        oi = oi_map.get(sym)
        chain_score, chain_tags = chain_score_for(ticker)

        # Channel A: extreme funding (always evaluate both directions)
        if fr is not None and FR_TRIGGER <= abs(fr) <= FR_MAX:
            vol_ratio = kl.get("vol_1m_ratio", 0)
            if vol_ratio >= A_VOL_SPIKE_MIN:
                # Negative FR -> shorts crowded -> contrarian long
                # Positive FR -> longs crowded   -> contrarian short
                if fr < 0:
                    direction = "LONG"
                else:
                    direction = "SHORT"
                snap = build_snapshot(sym, direction, fr, kl, ob, oi, chain_score)
                candidates.append({
                    "sym": sym, "direction": direction, "channel": "A",
                    "fr": fr, "snapshot": snap, "chain_tags": chain_tags,
                })

        # Channel B: trend / momentum
        chg5 = kl.get("chg_5m", 0)
        vol_ratio = kl.get("vol_1m_ratio", 0)
        if abs(chg5) >= B_MIN_5M_CHG and vol_ratio >= B_MIN_VOL_SPIKE:
            direction = "LONG" if chg5 > 0 else "SHORT"
            ha = kl.get("ha_trend_15m", "FLAT")
            # Soft hard-gate: 15m HA must not be against us (FLAT ok)
            if direction == "LONG" and ha == "DOWN":
                continue
            if direction == "SHORT" and ha == "UP":
                continue
            snap = build_snapshot(sym, direction, fr, kl, ob, oi, chain_score)
            candidates.append({
                "sym": sym, "direction": direction, "channel": "B",
                "fr": fr, "snapshot": snap, "chain_tags": chain_tags,
            })

    return candidates, fr_map, kline_map, ob_map, oi_map


# =====================================================================
#  Pre-open guard
# =====================================================================

def can_open(sym: str, state: Dict[str, Any]) -> Tuple[bool, str]:
    if check_blacklist(sym):
        return False, "blacklisted"
    if check_daily_limit():
        return False, "daily limit"
    if brain.is_symbol_cold(sym):
        return False, "cold"
    last = state.get("last_trade", {}).get(sym, 0)
    if time.time() - last < SYMBOL_COOLDOWN_SEC:
        return False, f"cooldown {int(SYMBOL_COOLDOWN_SEC - (time.time() - last))}s"
    near, secs = is_near_settlement()
    if near:
        return False, f"near settlement ({secs//60}m)"
    return True, ""


# =====================================================================
#  Main loop
# =====================================================================

def restore_positions(state: Dict[str, Any]) -> None:
    log("📥 restoring open positions...")
    pending = okx_get("/api/v5/trade/orders-algo-pending?ordType=conditional")
    algo_map: Dict[str, List[str]] = {}
    if pending.get("data"):
        for a in pending["data"]:
            algo_map.setdefault(a.get("instId", ""), []).append(a.get("algoId", ""))
    existing = get_all_positions_raw()
    saved_meta = state.get("position_meta", {})

    for raw in existing:
        iid = raw["instId"]
        meta = saved_meta.get(iid, {})
        direction = "LONG" if raw["pos"] > 0 else "SHORT"
        positions[iid] = {
            "instId": iid,
            "channel": meta.get("channel", "?"),
            "direction": direction,
            "entry_price": raw["avgPx"],
            "sz": int(abs(raw["pos"])),
            "algo_ids": algo_map.get(iid, []),
            "open_ts": _safe_float(meta.get("open_ts"), time.time()),
            "open_ts_ms": int(_safe_float(meta.get("open_ts"), time.time()) * 1000),
            "notional": abs(raw["pos"]) * INSTRUMENTS_CACHE.get(iid, {}).get("ctVal", 1) * raw["avgPx"],
            "atr_abs": _safe_float(meta.get("atr_abs"), 0),
            "highest_pnl_pct": 0.0,
            "lowest_pnl_pct": 0.0,
            "trail_active": False,
            "highest_price": raw["avgPx"] if direction == "LONG" else 0,
            "lowest_price": raw["avgPx"] if direction == "SHORT" else 1e18,
            "features": meta.get("features", {}),
            "p_win": _safe_float(meta.get("p_win"), 0.5),
            "ev": _safe_float(meta.get("ev"), 0.0),
            "fraction": _safe_float(meta.get("fraction"), 0.0),
        }
        log(f"  📥 {iid} {direction} {int(abs(raw['pos']))}lots @ ${raw['avgPx']} "
            f"algos={len(algo_map.get(iid, []))}")

    if not positions:
        log("  no open positions")

    # Cancel any algos that don't belong to a tracked position
    held = set(positions.keys())
    for iid, algos in algo_map.items():
        if iid not in held:
            for aid in algos:
                okx_post("/api/v5/trade/cancel-algos",
                         json.dumps([{"instId": iid, "algoId": aid}]))
                log(f"  🧹 stale algo {iid}/{aid}")


def main() -> None:
    log("=" * 60)
    log("🔥 Hermes v9.0 - OKX adaptive perpetual strategy")
    log(f"   leverage={LEVERAGE}x  hard TP={HARD_TP_PCT*100}%  hard SL={HARD_SL_PCT*100}%")
    log(f"   ATR-based exits: TP={ATR_TP_MULT}*ATR  SL={ATR_SL_MULT}*ATR  "
        f"trail={TRAIL_ATR_MULT}*ATR")
    log(f"   universe top {SCAN_TOP_N}, min24hVol=${MIN_24H_VOL/1e6:.1f}M")
    log(f"   brain: {NUM_FEATURES_NOTE} features, online SGD + fractional Kelly")
    log("=" * 60)

    load_instruments_cache()
    load_chain_cache()
    state = load_state()
    restore_positions(state)
    scan_count = 0

    while True:
        try:
            # ----- Pause check -----
            if state.get("pause_until"):
                until = datetime.fromisoformat(state["pause_until"])
                if datetime.now() < until:
                    rem = int((until - datetime.now()).total_seconds())
                    log(f"⏸️ pause {rem}s")
                    time.sleep(min(30, rem))
                    continue
                state["pause_until"] = None
                state["consecutive_losses"] = 0
                save_state(state)

            scan_count += 1
            balance = get_balance()
            log(f"📡 scan#{scan_count} bal=${balance:.2f} "
                f"slots={len(positions)}/{MAX_CONCURRENT}")

            # ----- 1. Universe scan -----
            candidates, fr_map, kline_map, ob_map, oi_map = scan_universe()

            # ----- 2. Update regime (uses ATR distribution) -----
            atrs = [k.get("atr_pct") for k in kline_map.values()
                    if isinstance(k.get("atr_pct"), (int, float))]
            brain.update_regime(atrs)

            if scan_count % 10 == 0:
                log(f"  {brain.status_str()}")

            # ----- 3. Monitor & exit existing positions -----
            for iid in list(positions.keys()):
                kl = kline_map.get(iid, {})
                if not kl.get("last_price"):
                    # Fall back to a fresh ticker; never use stale klines for exits
                    d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={iid}", 5)
                    if d.get("data"):
                        kl = dict(kl)
                        kl["last_price"] = _safe_float(d["data"][0].get("last"))
                try:
                    reason = monitor_position(iid, positions[iid], kl)
                except Exception as e:
                    log(f"  ⚠️ monitor {iid}: {e}")
                    reason = None
                if reason:
                    close_and_settle(iid, reason, state)

            # ----- 4. Settlement window check -----
            near, secs = is_near_settlement()
            if near:
                log(f"  ⏳ near funding settlement ({secs//60}m), no new opens")
                time.sleep(SCAN_INTERVAL)
                continue

            # ----- 5. Evaluate candidates through the brain -----
            evaluated: List[Dict[str, Any]] = []
            for c in candidates:
                ev = brain.evaluate(c["snapshot"])
                if ev["passes"]:
                    evaluated.append({**c, **ev})

            evaluated.sort(key=lambda e: e["ev"], reverse=True)

            if evaluated:
                log(f"  🎯 {len(evaluated)} candidates pass the brain")
                for e in evaluated[:5]:
                    log(f"     {e['sym']:18s} {e['channel']} {e['direction']:5s} "
                        f"p={e['p_win']:.2%} ev={e['ev']:+.4f} f={e['fraction']:.2%} "
                        f"tags=[{','.join(e['chain_tags'])}]")

            # ----- 6. Open positions -----
            slots_used = {p["channel"] for p in positions.values()}
            for e in evaluated:
                if len(positions) >= MAX_CONCURRENT:
                    break
                if e["channel"] in slots_used:
                    continue
                if e["sym"] in positions:
                    continue
                ok, why = can_open(e["sym"], state)
                if not ok:
                    log(f"     skip {e['sym']}: {why}")
                    continue
                cap = balance * e["fraction"]
                if cap < 1.0:
                    log(f"     skip {e['sym']}: cap ${cap:.2f} too small")
                    continue
                pos = open_position(e["sym"], e["direction"], cap, e,
                                    e["snapshot"], e["channel"])
                if pos:
                    with positions_lock:
                        positions[e["sym"]] = pos
                    slots_used.add(e["channel"])
                    incr_daily_trade()
                    state.setdefault("last_trade", {})[e["sym"]] = time.time()
                    save_state(state)
                    balance = get_balance()  # refresh for next slot

            # ----- 7. Periodic brain save -----
            brain.save()

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 interrupted, closing all positions...")
            for iid in list(positions.keys()):
                close_market(iid, positions[iid].get("algo_ids"))
                close_and_settle(iid, "MANUAL_STOP", state)
            brain.save(force=True)
            break
        except Exception as e:
            log(f"⚠️ main loop error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)


# Helper for logging the feature count
NUM_FEATURES_NOTE = len(FEATURE_NAMES)


if __name__ == "__main__":
    main()
