#!/usr/bin/env python3
"""
OKX Perpetual Swap Backtester v1.0
===================================
通用回测框架 — 不绑定特定策略版本
支持: 多品种 | 多时间框架 | 资金费率 | 手续费 | 止盈止损 | 追踪止损

用法:
  # 下载数据
  python3 okx_backtest.py download --symbols BTC,ETH,SOL --days 30

  # 运行回测 (内置策略)
  python3 okx_backtest.py backtest --strategy momentum --days 7

  # 运行回测 (自定义策略)
  python3 okx_backtest.py backtest --strategy my_strategy.py --days 7

  # 生成报告
  python3 okx_backtest.py report --output report.html
"""

import os
import sys
import json
import time
import math
import hashlib
import argparse
import statistics
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

# ============================================================
# CONFIG
# ============================================================
BASE_URL = "https://www.okx.com"
DATA_DIR = os.path.expanduser("~/.hermes/scripts/backtest_data")
REPORT_DIR = os.path.expanduser("~/.hermes/scripts/backtest_reports")

DEFAULT_CONFIG = {
    # 资金
    "initial_balance": 20.0,
    "leverage": 6,
    "position_pct": 0.40,          # 每笔仓位占可用资金比例

    # 止盈止损
    "tp_pct": 0.03,                # 3%
    "sl_pct": 0.015,               # 1.5%
    "trail_activate_pct": 0.015,   # 浮盈1.5%激活追踪
    "trail_distance_pct": 0.008,   # 距离0.8%

    # 时间止损
    "time_stop_seconds": 480,      # 8分钟
    "time_stop_breakeven_pct": 0.005,  # 浮盈>0.5%保本追踪

    # 手续费
    "taker_fee": 0.0005,           # 0.05% 单边
    "maker_fee": 0.0002,           # 0.02% 单边

    # 风控
    "max_positions": 2,
    "cooldown_seconds": 2400,      # 平仓后冷却40分钟
    "max_consecutive_loss": 3,     # 连亏暂停
    "cooldown_after_consec_loss": 3600,

    # 过滤
    "min_24h_vol": 50000,
    "min_price": 0.01,
}


# ============================================================
# HTTP (urllib, no deps)
# ============================================================
import urllib.request
import urllib.error
import ssl

_ssl_ctx = ssl.create_default_context()

def http_get(url, timeout=15, retries=3):
    """HTTP GET with retries, returns parsed JSON."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "OKXBacktest/1.0",
                "Accept": "application/json"
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                return {"code": "-1", "msg": str(e), "data": []}
            time.sleep(1 * (attempt + 1))
    return {"code": "-1", "msg": "max retries", "data": []}


# ============================================================
# DATA DOWNLOAD
# ============================================================
def get_all_swap_symbols():
    """获取所有USDT永续合约"""
    d = http_get(f"{BASE_URL}/api/v5/market/tickers?instType=SWAP")
    symbols = []
    if d.get("data"):
        for x in d["data"]:
            if x["instId"].endswith("USDT-SWAP"):
                vol24h = float(x.get("volCcy24h", "0"))
                last = float(x.get("last", "0"))
                if vol24h > 0 and last > 0:
                    symbols.append({
                        "instId": x["instId"],
                        "vol24h": vol24h,
                        "last": last,
                    })
    symbols.sort(key=lambda s: s["vol24h"], reverse=True)
    return symbols


def fetch_candles(inst_id, bar, limit=300, after=None):
    """拉取K线数据"""
    url = f"{BASE_URL}/api/v5/market/history-candles?instId={inst_id}&bar={bar}&limit={limit}"
    if after:
        url += f"&after={after}"
    d = http_get(url, timeout=15)
    if d.get("code") == "0" and d.get("data"):
        return d["data"]
    return []


def fetch_funding_rates(inst_id, limit=100, after=None):
    """拉取资金费率历史"""
    url = f"{BASE_URL}/api/v5/public/funding-rate-history?instId={inst_id}&limit={limit}"
    if after:
        url += f"&after={after}"
    d = http_get(url, timeout=15)
    if d.get("code") == "0" and d.get("data"):
        return d["data"]
    return []


def download_candles(inst_id, bar, days, bar_seconds):
    """下载指定时间范围的K线"""
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - days * 86400 * 1000

    all_candles = []
    cursor = None

    while True:
        candles = fetch_candles(inst_id, bar, limit=300, after=cursor)
        if not candles:
            break

        for c in candles:
            ts = int(c[0])
            if ts >= start_ts:
                all_candles.append(c)
            else:
                all_candles.append(c)
                cursor = c[0]
                return sorted(all_candles, key=lambda x: int(x[0]))

        if len(candles) < 300:
            break
        cursor = candles[-1][0]
        time.sleep(0.1)  # rate limit

    return sorted(all_candles, key=lambda x: int(x[0]))


def download_all(symbol_list, days=7):
    """下载所有数据"""
    os.makedirs(DATA_DIR, exist_ok=True)

    timeframes = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "1H": 3600,
    }

    all_data = {}
    for inst_id in symbol_list:
        all_data[inst_id] = {}
        for bar, bar_sec in timeframes.items():
            cache_key = f"{inst_id}_{bar}_{days}d"
            cache_file = os.path.join(DATA_DIR, f"{cache_key}.json")

            if os.path.exists(cache_file):
                with open(cache_file) as f:
                    all_data[inst_id][bar] = json.load(f)
                print(f"  📦 {inst_id} {bar}: 缓存 ({len(all_data[inst_id][bar])} 根)")
                continue

            print(f"  📡 下载 {inst_id} {bar} ({days}天)...", end=" ", flush=True)
            candles = download_candles(inst_id, bar, days, bar_sec)
            print(f"{len(candles)} 根")

            with open(cache_file, "w") as f:
                json.dump(candles, f)
            all_data[inst_id][bar] = candles
            time.sleep(0.15)

        # 下载资金费率
        fr_cache = os.path.join(DATA_DIR, f"{inst_id}_funding_{days}d.json")
        if os.path.exists(fr_cache):
            with open(fr_cache) as f:
                all_data[inst_id]["funding"] = json.load(f)
            print(f"  📦 {inst_id} funding: 缓存 ({len(all_data[inst_id]['funding'])} 条)")
        else:
            print(f"  📡 下载 {inst_id} 资金费率...", end=" ", flush=True)
            frs = []
            cursor = None
            while True:
                batch = fetch_funding_rates(inst_id, limit=100, after=cursor)
                if not batch:
                    break
                frs.extend(batch)
                if len(batch) < 100:
                    break
                cursor = batch[-1].get("fundingTime")
                time.sleep(0.1)
            with open(fr_cache, "w") as f:
                json.dump(frs, f)
            all_data[inst_id]["funding"] = frs
            print(f"{len(frs)} 条")

    return all_data


# ============================================================
# TECHNICAL INDICATORS
# ============================================================
def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_adx(highs, lows, closes, period=14):
    """计算ADX"""
    if len(highs) < period * 2 + 1:
        return None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)

    # Smoothed
    atr = sum(trs[:period]) / period
    pdm_s = sum(plus_dm[:period]) / period
    mdm_s = sum(minus_dm[:period]) / period
    dxs = []
    for i in range(period, len(trs)):
        atr = atr * (period-1)/period + trs[i]/period
        pdm_s = pdm_s * (period-1)/period + plus_dm[i]/period
        mdm_s = mdm_s * (period-1)/period + minus_dm[i]/period
        if atr == 0:
            continue
        pdi = 100 * pdm_s / atr
        mdi = 100 * mdm_s / atr
        if pdi + mdi == 0:
            dxs.append(0)
        else:
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))

    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for i in range(period, len(dxs)):
        adx = (adx * (period-1) + dxs[i]) / period
    return adx


def calc_bb_width(closes, period=20):
    """布林带宽度 %"""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    std = (sum((c - sma)**2 for c in window) / period) ** 0.5
    if sma == 0:
        return None
    return (2 * std * 2) / sma * 100


def calc_atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = atr * (period-1)/period + trs[i]/period
    return atr


def calc_stoch_k(highs, lows, closes, period=14):
    if len(highs) < period:
        return 50
    h = max(highs[-period:])
    l = min(lows[-period:])
    if h == l:
        return 50
    return (closes[-1] - l) / (h - l) * 100


def calc_roc(closes, period=10):
    if len(closes) < period + 1 or closes[-period-1] == 0:
        return None
    return (closes[-1] - closes[-period-1]) / closes[-period-1] * 100


# ============================================================
# DATA HELPERS
# ============================================================
def candles_to_arrays(candles):
    """candle数据 -> arrays"""
    timestamps = [int(c[0]) for c in candles]
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    vols = [float(c[5]) for c in candles]
    return timestamps, opens, highs, lows, closes, vols


def find_funding_at(funding_list, ts_ms):
    """找最近的资金费率"""
    best = None
    best_diff = float('inf')
    for fr in funding_list:
        ft = int(fr.get("fundingTime", 0))
        diff = abs(ts_ms - ft)
        if diff < best_diff:
            best_diff = diff
            best = float(fr.get("fundingRate", 0))
    return best if best is not None else 0


# ============================================================
# BUILT-IN STRATEGIES
# ============================================================

class BaseStrategy:
    """策略基类 — 所有策略继承此类"""

    def __init__(self, config=None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.name = "base"

    def init(self, data):
        """策略初始化，data是所有品种的K线数据"""
        pass

    def on_bar(self, ctx):
        """
        每根K线触发。
        ctx 包含:
          - symbol: 品种
          - bar_index: 当前K线索引
          - bars: 到当前为止的所有K线
          - timestamps, opens, highs, lows, closes, vols
          - funding: 资金费率列表
          - balance: 当前余额
          - positions: 当前持仓列表
          - signals: 当前活跃信号列表

        返回: list of Signal 或 []
          Signal(symbol, direction, score, reason)
        """
        return []


class Signal:
    """交易信号"""
    def __init__(self, symbol, direction, score=0, reason=""):
        self.symbol = symbol
        self.direction = direction  # "LONG" or "SHORT"
        self.score = score
        self.reason = reason


class MomentumStrategy(BaseStrategy):
    """
    动量顺势策略 (类似 v7.7 通道B)
    适用: 任意品种, 5m级别
    """
    def __init__(self, config=None):
        super().__init__(config)
        self.name = "momentum"
        self.score_threshold = 8
        self.require_15m_align = True

    def on_bar(self, ctx):
        signals = []
        c = ctx["closes"]
        v = ctx["vols"]
        h = ctx["highs"]
        lo = ctx["lows"]
        t = ctx["timestamps"]

        if len(c) < 50 or len(v) < 50:
            return []

        # 指标
        rsi = calc_rsi(c)
        adx = calc_adx(h, lo, c)
        bb = calc_bb_width(c)
        roc = calc_roc(c)

        # 5m涨跌幅
        chg_5m = (c[-1] - c[-5]) / c[-5] * 100 if c[-5] != 0 else 0

        # 1m放量
        vol_recent = sum(v[-3:]) / 3
        vol_history = sum(v[:-3]) / max(len(v[:-3]), 1)
        vol_ratio = vol_recent / max(vol_history, 0.001)

        # 趋势方向
        if chg_5m > 0.1:
            direction = "LONG"
        elif chg_5m < -0.1:
            direction = "SHORT"
        else:
            return []

        # 过滤
        if adx is not None and adx >= 40:
            return []
        if bb is not None and bb < 1.5:
            return []
        if roc is not None:
            if direction == "SHORT" and roc > 0:
                return []
            if direction == "LONG" and roc < 0:
                return []
        if rsi > 70 and direction == "LONG":
            return []
        if rsi < 30 and direction == "SHORT":
            return []

        # 评分
        score = 0
        abs_chg = abs(chg_5m)
        if abs_chg > 2: score += 3
        elif abs_chg > 1: score += 2
        elif abs_chg > 0.5: score += 1

        if vol_ratio > 3: score += 3
        elif vol_ratio > 2: score += 2
        elif vol_ratio > 1.5: score += 1

        # 连涨/连跌
        ups = sum(1 for i in range(1, min(30, len(c))) if c[-i] > c[-i-1])
        downs = sum(1 for i in range(1, min(30, len(c))) if c[-i] < c[-i-1])
        if ups > 12 or downs > 12:
            score += 2
        elif ups > 10 or downs > 10:
            score += 1

        # FR方向一致
        fr = find_funding_at(ctx["funding"], t[-1])
        if fr is not None:
            if (fr < 0 and chg_5m > 0) or (fr > 0 and chg_5m < 0):
                score += 1

        if score >= self.score_threshold:
            signals.append(Signal(ctx["symbol"], direction, score, f"chg5m={chg_5m:.2f}% vol={vol_ratio:.1f}x rsi={rsi:.0f}"))

        return signals


class FundingReversalStrategy(BaseStrategy):
    """
    极端资金费率反转策略 (类似 v7.7 通道A)
    FR极端时反向开仓
    """
    def __init__(self, config=None):
        super().__init__(config)
        self.name = "funding_reversal"
        self.fr_min = 0.0005   # |FR| >= 0.05%
        self.fr_max = 0.05     # |FR| <= 5%

    def on_bar(self, ctx):
        signals = []
        c = ctx["closes"]
        v = ctx["vols"]
        h = ctx["highs"]
        lo = ctx["lows"]
        t = ctx["timestamps"]

        if len(c) < 50:
            return []

        fr = find_funding_at(ctx["funding"], t[-1])
        if fr is None:
            return []

        abs_fr = abs(fr)
        if abs_fr < self.fr_min or abs_fr > self.fr_max:
            return []

        # 技术指标
        rsi = calc_rsi(c)
        adx = calc_adx(h, lo, c)
        bb = calc_bb_width(c)
        chg_5m = (c[-1] - c[-5]) / c[-5] * 100 if c[-5] != 0 else 0

        vol_recent = sum(v[-3:]) / 3
        vol_history = sum(v[:-3]) / max(len(v[:-3]), 1)
        vol_ratio = vol_recent / max(vol_history, 0.001)

        if vol_ratio < 1.5:
            return []
        if adx is not None and adx >= 40:
            return []
        if bb is not None and bb < 1.5:
            return []

        if fr < 0 and (chg_5m > 0.1):
            if rsi > 70:
                return []
            signals.append(Signal(ctx["symbol"], "LONG", abs_fr * 1000, f"FR={fr:.6f} chg5m={chg_5m:.2f}%"))

        elif fr > 0 and chg_5m < -0.1:
            if rsi < 30:
                return []
            roc = calc_roc(c)
            if roc is not None and roc > 0:
                return []
            signals.append(Signal(ctx["symbol"], "SHORT", abs_fr * 1000, f"FR={fr:.6f} chg5m={chg_5m:.2f}%"))

        return signals


class DualChannelStrategy(BaseStrategy):
    """
    双通道策略 = 动量 + 资金费率反转
    组合使用两个子策略
    """
    def __init__(self, config=None):
        super().__init__(config)
        self.name = "dual_channel"
        self.momentum = MomentumStrategy(config)
        self.funding = FundingReversalStrategy(config)

    def on_bar(self, ctx):
        signals = []
        signals.extend(self.momentum.on_bar(ctx))
        signals.extend(self.funding.on_bar(ctx))
        return signals


# 策略注册表
STRATEGIES = {
    "momentum": MomentumStrategy,
    "funding_reversal": FundingReversalStrategy,
    "dual_channel": DualChannelStrategy,
}


# ============================================================
# BACKTEST ENGINE
# ============================================================
class Position:
    """持仓记录"""
    def __init__(self, symbol, direction, entry_price, size, notional, channel, score, open_time, fr_cost=0):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.size = size
        self.notional = notional
        self.channel = channel
        self.score = score
        self.open_time = open_time
        self.fr_cost = fr_cost  # 开仓时的FR成本

        # 动态追踪
        self.tp_price = 0
        self.sl_price = 0
        self.highest_pnl_pct = 0
        self.trail_activated = False
        self.close_time = 0
        self.close_price = 0
        self.close_reason = ""
        self.pnl = 0
        self.fee_paid = 0
        self.funding_paid = 0
        self.total_cost = 0

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "close_price": self.close_price,
            "size": self.size,
            "notional": self.notional,
            "channel": self.channel,
            "score": self.score,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "close_reason": self.close_reason,
            "pnl": self.pnl,
            "fee_paid": self.fee_paid,
            "funding_paid": self.funding_paid,
            "total_cost": self.total_cost,
            "net_pnl": self.pnl - self.total_cost,
            "duration_seconds": self.close_time - self.open_time if self.close_time else 0,
        }


class BacktestEngine:
    """回测引擎"""

    def __init__(self, strategy, config=None):
        self.strategy = strategy
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.balance = self.config["initial_balance"]
        self.initial_balance = self.config["initial_balance"]
        self.positions = []        # 活跃持仓
        self.closed_trades = []    # 已平仓
        self.equity_curve = []     # 权益曲线
        self.cooldown_until = {}   # {symbol: timestamp}
        self.consec_loss = 0       # 连亏计数

    def _fee(self, notional, is_taker=True):
        rate = self.config["taker_fee"] if is_taker else self.config["maker_fee"]
        return notional * rate

    def _calc_position_size(self, balance, price, symbol):
        """计算仓位大小 (张数)"""
        ct_val = 0.001  # 默认合约面值
        # 从 symbol 名推断
        major = ["BTC", "ETH"]
        base = symbol.split("-")[0]
        if base in major:
            ct_val = 0.01
        elif base in ["SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "MATIC"]:
            ct_val = 1
        elif base in ["PEPE", "WIF", "BONK", "FLOKI", "SHIB"]:
            ct_val = 1000
        else:
            ct_val = 1

        leverage = self.config["leverage"]
        margin = balance * self.config["position_pct"]
        notional_max = margin * leverage
        lots = int(notional_max / (ct_val * price))
        notional_actual = lots * ct_val * price
        return lots, notional_actual, ct_val

    def open_trade(self, signal, timestamp, price, fr_rate=0):
        """开仓"""
        if len(self.positions) >= self.config["max_positions"]:
            return None

        # 检查冷却
        if signal.symbol in self.cooldown_until:
            if timestamp < self.cooldown_until[signal.symbol]:
                return None

        # 检查连亏暂停
        if self.consec_loss >= self.config["max_consecutive_loss"]:
            # 找最近一笔的时间
            if self.closed_trades:
                last_close = max(t.close_time for t in self.closed_trades)
                if timestamp - last_close < self.config["cooldown_after_consec_loss"]:
                    return None
            self.consec_loss = 0  # 重置

        lots, notional, ct_val = self._calc_position_size(self.balance, price, signal.symbol)
        if lots < 1 or notional < 1:
            return None

        # 手续费 (开仓 taker)
        open_fee = self._fee(notional, is_taker=True)

        # TP/SL
        if signal.direction == "LONG":
            tp = price * (1 + self.config["tp_pct"])
            sl = price * (1 - self.config["sl_pct"])
        else:
            tp = price * (1 - self.config["tp_pct"])
            sl = price * (1 + self.config["sl_pct"])

        pos = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=price,
            size=lots,
            notional=notional,
            channel=signal.channel if hasattr(signal, "channel") else signal.reason[:10],
            score=signal.score,
            open_time=timestamp,
            fr_cost=fr_rate,
        )
        pos.tp_price = tp
        pos.sl_price = sl
        pos.fee_paid = open_fee

        self.balance -= open_fee
        self.positions.append(pos)
        return pos

    def update_positions(self, timestamp, high, low, close, fr_rate=0):
        """更新持仓，检查止盈止损"""
        to_close = []
        for pos in self.positions:
            if pos.direction == "LONG":
                # 浮盈 %
                pnl_pct = (close - pos.entry_price) / pos.entry_price
            else:
                pnl_pct = (pos.entry_price - close) / pos.entry_price

            # 更新最高浮盈
            if pnl_pct > pos.highest_pnl_pct:
                pos.highest_pnl_pct = pnl_pct

            # === 止盈检查 ===
            if pos.direction == "LONG" and high >= pos.tp_price:
                pos.close_price = pos.tp_price
                pos.close_time = timestamp
                pos.close_reason = "TP"
                to_close.append(pos)
                continue
            if pos.direction == "SHORT" and low <= pos.tp_price:
                pos.close_price = pos.tp_price
                pos.close_time = timestamp
                pos.close_reason = "TP"
                to_close.append(pos)
                continue

            # === 止损检查 ===
            if pos.direction == "LONG" and low <= pos.sl_price:
                pos.close_price = pos.sl_price
                pos.close_time = timestamp
                pos.close_reason = "SL"
                to_close.append(pos)
                continue
            if pos.direction == "SHORT" and high >= pos.sl_price:
                pos.close_price = pos.sl_price
                pos.close_time = timestamp
                pos.close_reason = "SL"
                to_close.append(pos)
                continue

            # === 追踪止损 ===
            if pos.highest_pnl_pct >= self.config["trail_activate_pct"]:
                if not pos.trail_activated:
                    pos.trail_activated = True
                if pos.direction == "LONG":
                    trail_sl = close * (1 - self.config["trail_distance_pct"])
                    if trail_sl > pos.sl_price:
                        pos.sl_price = trail_sl
                else:
                    trail_sl = close * (1 + self.config["trail_distance_pct"])
                    if trail_sl < pos.sl_price:
                        pos.sl_price = trail_sl

            # === 时间止损 ===
            elapsed = timestamp - pos.open_time
            if elapsed >= self.config["time_stop_seconds"]:
                if pnl_pct > self.config["time_stop_breakeven_pct"]:
                    # 保本追踪
                    pos.sl_price = pos.entry_price * 1.001 if pos.direction == "LONG" else pos.entry_price * 0.999
                elif elapsed >= self.config["time_stop_seconds"] * 2:
                    # 双倍时间强制平仓
                    pos.close_price = close
                    pos.close_time = timestamp
                    pos.close_reason = "TIMEOUT"
                    to_close.append(pos)
                    continue

            # === 资金费率成本 ===
            if fr_rate != 0:
                fr_cost = abs(pos.notional) * abs(fr_rate)
                pos.funding_paid += fr_cost

        # 平仓
        for pos in to_close:
            self._close_position(pos)
        return to_close

    def _close_position(self, pos):
        """结算持仓"""
        if pos.direction == "LONG":
            pnl = (pos.close_price - pos.entry_price) / pos.entry_price * pos.notional
        else:
            pnl = (pos.entry_price - pos.close_price) / pos.entry_price * pos.notional

        close_fee = self._fee(pos.notional, is_taker=True)
        pos.pnl = pnl
        pos.fee_paid += close_fee
        pos.total_cost = pos.fee_paid + pos.funding_paid
        pos.net_pnl = pnl - pos.total_cost

        self.balance += pnl - close_fee
        self.balance -= pos.funding_paid  # 扣资金费率

        if pos in self.positions:
            self.positions.remove(pos)
        self.closed_trades.append(pos)

        # 连亏计数
        if pos.net_pnl < 0:
            self.consec_loss += 1
            if self.consec_loss >= self.config["max_consecutive_loss"]:
                # 设置冷却
                for p in self.positions:
                    self.cooldown_until[p.symbol] = pos.close_time + self.config["cooldown_after_consec_loss"]
        else:
            self.consec_loss = 0

        # 设置品种冷却
        self.cooldown_until[pos.symbol] = pos.close_time + self.config["cooldown_seconds"]

    def run(self, all_data):
        """运行回测"""
        print(f"\n🚀 回测启动: {self.strategy.name}")
        print(f"   余额: ${self.initial_balance:.2f} | 杠杆: {self.config['leverage']}x")
        print(f"   TP: {self.config['tp_pct']*100:.1f}% | SL: {self.config['sl_pct']*100:.1f}%")
        print(f"   追踪: 激活{self.config['trail_activate_pct']*100:.1f}% 距离{self.config['trail_distance_pct']*100:.1f}%")
        print()

        # 构建时间线: 合并所有品种的5m K线
        timeline = {}  # {timestamp_ms: [(symbol, candle_index, ...)]}
        for symbol, data in all_data.items():
            candles = data.get("5m", [])
            if not candles:
                continue
            ts, opens, highs, lows, closes, vols = candles_to_arrays(candles)
            for i in range(50, len(candles)):  # 跳过前50根(指标预热)
                t = ts[i]
                if t not in timeline:
                    timeline[t] = []
                timeline[t].append({
                    "symbol": symbol,
                    "bar_index": i,
                    "ts": ts, "opens": opens, "highs": highs,
                    "lows": lows, "closes": closes, "vols": vols,
                })

        sorted_times = sorted(timeline.keys())
        total_bars = len(sorted_times)
        print(f"📊 时间线: {total_bars} 根5m K线, {len(all_data)} 品种")

        for bar_num, ts in enumerate(sorted_times):
            for item in timeline[ts]:
                symbol = item["symbol"]
                i = item["bar_index"]
                c = item["closes"]
                v = item["vols"]
                h = item["highs"]
                lo = item["lows"]
                t = item["ts"]
                data = all_data[symbol]

                # 更新已有持仓
                fr = find_funding_at(data.get("funding", []), ts)
                self.update_positions(ts, h[i], lo[i], c[i], fr)

                # 构建 ctx
                ctx = {
                    "symbol": symbol,
                    "bar_index": i,
                    "timestamps": t[:i+1],
                    "opens": item["opens"][:i+1],
                    "highs": h[:i+1],
                    "lows": lo[:i+1],
                    "closes": c[:i+1],
                    "vols": v[:i+1],
                    "funding": data.get("funding", []),
                    "balance": self.balance,
                    "positions": self.positions,
                }

                # 策略信号
                signals = self.strategy.on_bar(ctx)
                for sig in signals:
                    if len(self.positions) >= self.config["max_positions"]:
                        break
                    # 检查是否已持有该品种
                    if any(p.symbol == sig.symbol for p in self.positions):
                        continue
                    self.open_trade(sig, ts, c[i], fr)

            # 记录权益
            unrealized = 0
            for pos in self.positions:
                if pos.direction == "LONG":
                    unrealized += (c[pos.symbol][-1] - pos.entry_price) / pos.entry_price * pos.notional
                else:
                    unrealized += (pos.entry_price - c[pos.symbol][-1]) / pos.entry_price * pos.notional
            self.equity_curve.append({
                "timestamp": ts,
                "balance": self.balance,
                "unrealized": unrealized,
                "equity": self.balance + unrealized,
            })

            # 进度
            if bar_num % 500 == 0 and bar_num > 0:
                pct = bar_num / total_bars * 100
                print(f"   ⏳ {pct:.0f}% ({bar_num}/{total_bars}) 余额:${self.balance:.2f} 交易:{len(self.closed_trades)}")

        # 强制平仓
        if self.positions:
            print(f"\n⚠️ 还有 {len(self.positions)} 个持仓，强制平仓")
            last_ts = sorted_times[-1] if sorted_times else 0
            for pos in list(self.positions):
                pos.close_price = pos.entry_price  # 简化：按入场价平
                if pos.symbol in all_data:
                    c = all_data[pos.symbol].get("5m", [])
                    if c:
                        pos.close_price = float(c[-1][4])
                pos.close_time = last_ts
                pos.close_reason = "FORCE_CLOSE"
                self._close_position(pos)

        self._print_report()
        return self.closed_trades, self.equity_curve

    def _print_report(self):
        """打印回测报告"""
        trades = self.closed_trades
        if not trades:
            print("\n📭 无交易")
            return

        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        total_pnl = sum(t.net_pnl for t in trades)
        total_fee = sum(t.fee_paid for t in trades)
        total_funding = sum(t.funding_paid for t in trades)

        print("\n" + "="*60)
        print(f"📊 回测报告 — {self.strategy.name}")
        print("="*60)
        print(f"  初始余额:  ${self.initial_balance:.2f}")
        print(f"  最终余额:  ${self.balance:.2f}")
        print(f"  总净利润:  ${total_pnl:.4f} ({total_pnl/self.initial_balance*100:.2f}%)")
        print(f"  总手续费:  ${total_fee:.4f}")
        print(f"  总资金费率: ${total_funding:.4f}")
        print(f"  总交易数:  {len(trades)}")
        print(f"  胜率:      {len(wins)/len(trades)*100:.1f}% ({len(wins)}W/{len(losses)}L)")

        if wins:
            avg_win = sum(t.net_pnl for t in wins) / len(wins)
            print(f"  平均盈利:  ${avg_win:.4f}")
        if losses:
            avg_loss = sum(t.net_pnl for t in losses) / len(losses)
            print(f"  平均亏损:  ${avg_loss:.4f}")

        if losses and wins:
            profit_factor = abs(sum(t.net_pnl for t in wins) / sum(t.net_pnl for t in losses))
            print(f"  盈亏比:    {profit_factor:.2f}")

        # 最大回撤
        if self.equity_curve:
            peak = 0
            max_dd = 0
            max_dd_pct = 0
            for eq in self.equity_curve:
                if eq["equity"] > peak:
                    peak = eq["equity"]
                dd = peak - eq["equity"]
                dd_pct = dd / peak * 100 if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
                    max_dd_pct = dd_pct
            print(f"  最大回撤:  ${max_dd:.4f} ({max_dd_pct:.2f}%)")

        # 品种统计
        sym_stats = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0})
        for t in trades:
            sym_stats[t.symbol]["trades"] += 1
            sym_stats[t.symbol]["pnl"] += t.net_pnl
            if t.net_pnl > 0:
                sym_stats[t.symbol]["wins"] += 1

        print(f"\n  {'品种':<20} {'交易':>4} {'胜率':>6} {'净利润':>10}")
        print("  " + "-"*44)
        for sym, st in sorted(sym_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = st["wins"]/st["trades"]*100 if st["trades"] > 0 else 0
            print(f"  {sym:<20} {st['trades']:>4} {wr:>5.0f}% ${st['pnl']:>9.4f}")

        # 平仓原因统计
        reasons = defaultdict(int)
        for t in trades:
            reasons[t.close_reason] += 1
        print(f"\n  平仓原因:")
        for reason, count in sorted(reasons.items(), key=lambda x: x[1], reverse=True):
            print(f"    {reason}: {count}")

        print("="*60)

    def generate_html_report(self, output_path):
        """生成HTML报告"""
        trades = self.closed_trades
        if not trades:
            print("无交易数据")
            return

        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        total_pnl = sum(t.net_pnl for t in trades)
        total_fee = sum(t.fee_paid for t in trades)
        total_funding = sum(t.funding_paid for t in trades)

        # 权益曲线数据
        eq_data = json.dumps([{
            "t": eq["timestamp"],
            "e": round(eq["equity"], 4)
        } for eq in self.equity_curve[::10]])  # 采样

        # 交易表
        trade_rows = ""
        for t in sorted(trades, key=lambda x: x["open_time"] if isinstance(x, dict) else x.open_time):
            d = t.to_dict() if hasattr(t, "to_dict") else t
            net = d.get("net_pnl", 0)
            color = "#4caf50" if net > 0 else "#f44336"
            trade_rows += f"""<tr>
<td>{datetime.fromtimestamp(d['open_time']/1000 if d['open_time'] > 1e10 else d['open_time']).strftime('%m-%d %H:%M')}</td>
<td>{d['symbol']}</td>
<td style="color:{'green' if d['direction']=='LONG' else 'red'}">{d['direction']}</td>
<td>{d['entry_price']:.6f}</td>
<td>{d['close_price']:.6f}</td>
<td>${net:.4f}</td>
<td>{d['close_reason']}</td>
<td>{int(d.get('duration_seconds',0))}s</td>
</tr>"""

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>OKX Backtest Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }}
h1 {{ color: #00d4ff; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }}
.stat-card {{ background: #16213e; padding: 15px; border-radius: 8px; border-left: 4px solid #00d4ff; }}
.stat-card h3 {{ margin: 0; color: #888; font-size: 12px; text-transform: uppercase; }}
.stat-card .value {{ font-size: 24px; font-weight: bold; margin-top: 5px; }}
.positive {{ color: #4caf50; }}
.negative {{ color: #f44336; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
th {{ background: #16213e; padding: 10px; text-align: left; font-size: 12px; color: #888; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #333; font-size: 13px; }}
tr:hover {{ background: #16213e; }}
#chart {{ width: 100%; height: 300px; background: #16213e; border-radius: 8px; margin: 20px 0; padding: 10px; }}
</style>
</head>
<body>
<h1>📊 OKX Backtest Report</h1>
<p>策略: {self.strategy.name} | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="stat-grid">
  <div class="stat-card"><h3>初始余额</h3><div class="value">${self.initial_balance:.2f}</div></div>
  <div class="stat-card"><h3>最终余额</h3><div class="value">${self.balance:.2f}</div></div>
  <div class="stat-card"><h3>净利润</h3><div class="value {'positive' if total_pnl>0 else 'negative'}">${total_pnl:.4f}</div></div>
  <div class="stat-card"><h3>总交易</h3><div class="value">{len(trades)}</div></div>
  <div class="stat-card"><h3>胜率</h3><div class="value">{len(wins)/len(trades)*100:.1f}%</div></div>
  <div class="stat-card"><h3>手续费</h3><div class="value">${total_fee:.4f}</div></div>
  <div class="stat-card"><h3>资金费率</h3><div class="value">${total_funding:.4f}</div></div>
</div>

<canvas id="chart"></canvas>

<h2>交易记录</h2>
<table>
<tr><th>时间</th><th>品种</th><th>方向</th><th>入场价</th><th>出场价</th><th>净利润</th><th>原因</th><th>持仓时间</th></tr>
{trade_rows}
</table>

<script>
const data = {eq_data};
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');
canvas.width = canvas.offsetWidth;
canvas.height = 300;
const W = canvas.width, H = canvas.height;
const vals = data.map(d => d.e);
const mn = Math.min(...vals), mx = Math.max(...vals);
const range = mx - mn || 1;
ctx.strokeStyle = vals[vals.length-1] >= vals[0] ? '#4caf50' : '#f44336';
ctx.lineWidth = 2;
ctx.beginPath();
vals.forEach((v, i) => {{
  const x = i / (vals.length - 1) * W;
  const y = H - 20 - (v - mn) / range * (H - 40);
  i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
}});
ctx.stroke();
</script>
</body>
</html>"""

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(html)
        print(f"\n📄 HTML报告: {output_path}")


# ============================================================
# CUSTOM STRATEGY LOADER
# ============================================================
def load_custom_strategy(path):
    """加载自定义策略文件"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("custom_strategy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if hasattr(mod, "Strategy"):
        return mod.Strategy()
    elif hasattr(mod, "MyStrategy"):
        return mod.MyStrategy()
    else:
        raise ValueError(f"策略文件 {path} 必须定义 Strategy 或 MyStrategy 类")


# ============================================================
# CLI
# ============================================================
def cmd_download(args):
    """下载数据"""
    symbols = [s.strip().upper() + "-USDT-SWAP" for s in args.symbols.split(",")]
    print(f"📥 下载 {len(symbols)} 个品种 {args.days}天 数据...")
    data = download_all(symbols, days=args.days)
    print(f"✅ 下载完成")

    # 列出可用品种
    all_syms = get_all_swap_symbols()
    print(f"\n📋 所有可用 USDT-SWAP 合约: {len(all_syms)} 个")
    if args.list:
        for s in all_syms[:50]:
            print(f"  {s['instId']:<25} 24h vol: ${s['vol24h']:>12,.0f}")


def cmd_backtest(args):
    """运行回测"""
    # 确定策略
    if os.path.exists(args.strategy):
        strategy = load_custom_strategy(args.strategy)
    elif args.strategy in STRATEGIES:
        strategy = STRATEGIES[args.strategy]()
    else:
        print(f"❌ 未知策略: {args.strategy}")
        print(f"   可用: {', '.join(STRATEGIES.keys())}")
        return

    # 确定品种
    if args.symbols:
        symbols = [s.strip().upper() + "-USDT-SWAP" for s in args.symbols.split(",")]
    else:
        # 自动选 top 10
        all_syms = get_all_swap_symbols()
        symbols = [s["instId"] for s in all_syms[:10]]
        print(f"📋 自动选择 Top 10 品种:")
        for s in symbols:
            print(f"   {s}")

    # 下载数据
    print(f"\n📥 准备数据 ({args.days}天)...")
    data = download_all(symbols, days=args.days)

    # 运行
    config = {}
    if args.balance:
        config["initial_balance"] = args.balance
    if args.leverage:
        config["leverage"] = args.leverage
    if args.tp:
        config["tp_pct"] = args.tp / 100
    if args.sl:
        config["sl_pct"] = args.sl / 100

    strategy.config.update(config)
    engine = BacktestEngine(strategy, config)
    trades, equity = engine.run(data)

    # 保存报告
    os.makedirs(REPORT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(REPORT_DIR, f"report_{strategy.name}_{timestamp}.html")
    engine.generate_html_report(report_path)

    # 保存交易记录 JSON
    json_path = os.path.join(REPORT_DIR, f"trades_{strategy.name}_{timestamp}.json")
    with open(json_path, "w") as f:
        json.dump([t.to_dict() for t in trades], f, indent=2)
    print(f"📄 交易记录: {json_path}")


def cmd_list_symbols(args):
    """列出所有可用合约"""
    all_syms = get_all_swap_symbols()
    print(f"📋 所有 USDT-SWAP 合约: {len(all_syms)} 个\n")
    print(f"{'品种':<25} {'24h成交额':>15} {'价格':>12}")
    print("-" * 55)
    for s in all_syms[:args.limit]:
        print(f"{s['instId']:<25} ${s['vol24h']:>13,.0f} {s['last']:>12.4f}")


def main():
    parser = argparse.ArgumentParser(description="OKX 永续合约回测框架")
    sub = parser.add_subparsers(dest="command")

    # download
    p_dl = sub.add_parser("download", help="下载历史数据")
    p_dl.add_argument("--symbols", default="BTC,ETH,SOL,DOGE,XRP,PEPE,WIF,AVAX,LINK,BONK",
                       help="品种列表,逗号分隔")
    p_dl.add_argument("--days", type=int, default=7, help="天数")
    p_dl.add_argument("--list", action="store_true", help="列出所有可用合约")

    # backtest
    p_bt = sub.add_parser("backtest", help="运行回测")
    p_bt.add_argument("--strategy", default="momentum",
                       help="策略名称或.py文件路径")
    p_bt.add_argument("--symbols", default="",
                       help="品种列表 (空=自动Top10)")
    p_bt.add_argument("--days", type=int, default=7, help="回测天数")
    p_bt.add_argument("--balance", type=float, help="初始余额")
    p_bt.add_argument("--leverage", type=int, help="杠杆倍数")
    p_bt.add_argument("--tp", type=float, help="止盈%")
    p_bt.add_argument("--sl", type=float, help="止损%")

    # list
    p_ls = sub.add_parser("list", help="列出可用合约")
    p_ls.add_argument("--limit", type=int, default=30, help="显示数量")

    args = parser.parse_args()
    if args.command == "download":
        cmd_download(args)
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "list":
        cmd_list_symbols(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
