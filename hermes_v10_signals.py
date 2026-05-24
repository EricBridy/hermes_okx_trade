#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes v10 — Signal Generators v3: Break & Retest + Price Action

Complete rewrite of the signal layer. Replaces z-score mean reversion
with price-action-based strategies that have documented edge:

  BreakRetestSignal:
    The highest-probability price action continuation setup.
    1. Identify S/R levels (swing highs/lows over lookback period)
    2. Detect breakout (close beyond S/R)
    3. Wait for retest (price returns to the broken level)
    4. Confirm with candlestick pattern (pin bar / engulfing / strong body)
    5. Enter in breakout direction
    6. Invalidation: price closes back through the level → immediate exit

  FundingRateSignal:
    Kept from v2 but only fires in RANGE regime (ADX < 20).

  LiquidationSignal:
    Disabled (OKX public feed insufficient).

References:
- Break & Retest as highest-probability continuation:
    https://chartingwithkr.substack.com/p/charting-and-trading-p11-breakoutretest
- Pin Bar + S/R for reversal confirmation:
    https://www.tradingview.com/script/6jwIsZAE/
- 4-Phase pullback state machine (Sharpe 0.89, WR 55%):
    https://github.com/ilahuerta-IA/backtrader-pullback-window-xauusd
- ADX regime filter:
    https://coinrule.com/market-regime-detection
- "Entries aren't the problem, exits are":
    https://algotr.substack.com/p/your-entries-arent-the-problem-your
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from hermes_v10_brain import safe_float


# ----------------------------------------------------------------------
# Common helpers
# ----------------------------------------------------------------------


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> Optional[float]:
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return sum(trs[-period:]) / period if len(trs) >= period else None


def adx_di(highs: List[float], lows: List[float], closes: List[float],
           period: int = 14) -> Optional[Tuple[float, float, float]]:
    """Returns (adx, +DI, -DI) or None."""
    n = len(highs)
    if n < period * 2 + 1:
        return None
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm_list.append(up if up > down and up > 0 else 0.0)
        minus_dm_list.append(down if down > up and down > 0 else 0.0)
        tr_list.append(max(highs[i] - lows[i],
                           abs(highs[i] - closes[i - 1]),
                           abs(lows[i] - closes[i - 1])))
    atr_val = sum(tr_list[:period])
    plus_dm_s = sum(plus_dm_list[:period])
    minus_dm_s = sum(minus_dm_list[:period])
    dx_list = []
    for i in range(period, len(tr_list)):
        atr_val = atr_val - atr_val / period + tr_list[i]
        plus_dm_s = plus_dm_s - plus_dm_s / period + plus_dm_list[i]
        minus_dm_s = minus_dm_s - minus_dm_s / period + minus_dm_list[i]
        if atr_val == 0:
            continue
        pdi = 100.0 * plus_dm_s / atr_val
        mdi = 100.0 * minus_dm_s / atr_val
        di_sum = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / di_sum if di_sum else 0.0)
    if len(dx_list) < period:
        return None
    adx_val = sum(dx_list[:period]) / period
    for i in range(period, len(dx_list)):
        adx_val = (adx_val * (period - 1) + dx_list[i]) / period
    if atr_val == 0:
        return None
    return (adx_val, 100.0 * plus_dm_s / atr_val, 100.0 * minus_dm_s / atr_val)


def classify_regime(adx_value: float) -> str:
    if adx_value < 20:
        return "RANGE"
    elif adx_value >= 25:
        return "TREND"
    return "NEUTRAL"


# Module-level constants for import convenience
REGIME_RANGE = "RANGE"
REGIME_TREND = "TREND"
REGIME_NEUTRAL = "NEUTRAL"


# ----------------------------------------------------------------------
# Price Action helpers
# ----------------------------------------------------------------------


def find_swing_high(highs: List[float], lookback: int = 20) -> float:
    """Highest high over the last `lookback` bars (excluding current)."""
    if len(highs) < lookback + 1:
        return highs[-2] if len(highs) >= 2 else 0.0
    return max(highs[-(lookback + 1):-1])


def find_swing_low(lows: List[float], lookback: int = 20) -> float:
    """Lowest low over the last `lookback` bars (excluding current)."""
    if len(lows) < lookback + 1:
        return lows[-2] if len(lows) >= 2 else 0.0
    return min(lows[-(lookback + 1):-1])


def is_pin_bar(open_p: float, high: float, low: float, close: float,
               direction: str) -> bool:
    """
    Pin bar detection: long wick in the rejection direction, small body.
    For bullish pin bar (direction="LONG"): long lower wick, close near high.
    For bearish pin bar (direction="SHORT"): long upper wick, close near low.
    """
    body = abs(close - open_p)
    full_range = high - low
    if full_range <= 0:
        return False
    if direction == "LONG":
        lower_wick = min(open_p, close) - low
        return lower_wick > body * 2 and lower_wick > full_range * 0.6
    else:
        upper_wick = high - max(open_p, close)
        return upper_wick > body * 2 and upper_wick > full_range * 0.6


def is_engulfing(prev_open: float, prev_close: float,
                 curr_open: float, curr_close: float,
                 direction: str) -> bool:
    """
    Engulfing pattern: current candle's body completely engulfs previous.
    """
    prev_body = abs(prev_close - prev_open)
    curr_body = abs(curr_close - curr_open)
    if curr_body <= prev_body:
        return False
    if direction == "LONG":
        # Bullish engulfing: prev was bearish, current is bullish and engulfs
        return (prev_close < prev_open and curr_close > curr_open and
                curr_close > prev_open and curr_open <= prev_close)
    else:
        # Bearish engulfing
        return (prev_close > prev_open and curr_close < curr_open and
                curr_close < prev_open and curr_open >= prev_close)


def is_strong_body(open_p: float, high: float, low: float, close: float,
                   direction: str, min_body_ratio: float = 0.6) -> bool:
    """Strong directional candle: body > 60% of total range."""
    body = abs(close - open_p)
    full_range = high - low
    if full_range <= 0:
        return False
    if body / full_range < min_body_ratio:
        return False
    if direction == "LONG":
        return close > open_p
    return close < open_p


# ----------------------------------------------------------------------
# Break & Retest Signal
# ----------------------------------------------------------------------


@dataclass
class BreakRetestCandidate:
    inst_id: str
    direction: str          # "LONG" (broke resistance, retested) or "SHORT"
    level: float            # the S/R level that was broken
    breakout_bar_idx: int   # how many bars ago the breakout happened
    retest_distance_pct: float  # how close price is to the level (%)
    confirmation: str       # "pin_bar" / "engulfing" / "strong_body"
    atr_pct: float
    adx: float
    regime: str
    timestamp: float


class BreakRetestSignal:
    """
    Break & Retest: the highest-probability price action continuation setup.

    State machine per symbol:
      IDLE       → detect breakout → WAITING_RETEST
      WAITING_RETEST → price returns to level → CHECK_CONFIRMATION
      CHECK_CONFIRMATION → candle pattern confirms → EMIT candidate
      Any state → too many bars pass → reset to IDLE

    Parameters:
      lookback: bars to compute swing high/low (S/R level)
      max_retest_bars: max bars to wait for retest after breakout
      retest_tolerance_pct: how close price must get to the level (%)
      min_breakout_pct: minimum breakout distance to confirm (% beyond level)
    """

    def __init__(
        self,
        on_candidate: Callable[[BreakRetestCandidate], Awaitable[None]],
        lookback: int = 20,
        max_retest_bars: int = 10,
        retest_tolerance_pct: float = 0.3,
        min_breakout_pct: float = 0.5,
        min_atr_pct: float = 0.4,
        max_atr_pct: float = 5.0,
    ) -> None:
        self.on_candidate = on_candidate
        self.lookback = lookback
        self.max_retest_bars = max_retest_bars
        self.retest_tolerance_pct = retest_tolerance_pct
        self.min_breakout_pct = min_breakout_pct
        self.min_atr_pct = min_atr_pct
        self.max_atr_pct = max_atr_pct
        # Per-symbol state machine
        self._states: Dict[str, Dict[str, Any]] = {}
        self._last_emit: Dict[str, float] = {}

    async def evaluate(self, inst_id: str, candles_30m: List[List[str]]
                       ) -> Optional[BreakRetestCandidate]:
        """
        Called every 5 minutes with the latest 30M candles (newest-first from OKX).
        """
        if len(candles_30m) < self.lookback + 5:
            return None
        rows = list(reversed(candles_30m))  # oldest first
        try:
            opens = [float(r[1]) for r in rows]
            highs = [float(r[2]) for r in rows]
            lows = [float(r[3]) for r in rows]
            closes = [float(r[4]) for r in rows]
        except (ValueError, IndexError):
            return None

        # ATR filter
        atr14 = atr(highs, lows, closes, 14)
        if atr14 is None or closes[-1] <= 0:
            return None
        atr_pct = atr14 / closes[-1] * 100.0
        if atr_pct < self.min_atr_pct or atr_pct > self.max_atr_pct:
            return None

        # ADX for regime (informational, B&R works in both trend and range)
        adx_result = adx_di(highs, lows, closes, 14)
        adx_val = adx_result[0] if adx_result else 20.0
        regime = classify_regime(adx_val)

        # Cooldown
        now = time.time()
        if now - self._last_emit.get(inst_id, 0) < 1800.0:
            return None

        # --- State machine ---
        state = self._states.get(inst_id, {"phase": "IDLE"})
        n = len(closes)

        if state["phase"] == "IDLE":
            # Look for a breakout in the most recent 3 bars
            # S/R levels computed from bars BEFORE the breakout window
            resistance = find_swing_high(highs[:-3], self.lookback)
            support = find_swing_low(lows[:-3], self.lookback)

            # Check last 3 bars for breakout above resistance
            for i in range(n - 3, n):
                if closes[i] > resistance * (1 + self.min_breakout_pct / 100.0):
                    state = {
                        "phase": "WAITING_RETEST",
                        "direction": "LONG",
                        "level": resistance,
                        "breakout_bar": i,
                        "bars_waited": 0,
                    }
                    break
                elif closes[i] < support * (1 - self.min_breakout_pct / 100.0):
                    state = {
                        "phase": "WAITING_RETEST",
                        "direction": "SHORT",
                        "level": support,
                        "breakout_bar": i,
                        "bars_waited": 0,
                    }
                    break

        elif state["phase"] == "WAITING_RETEST":
            state["bars_waited"] += 1
            if state["bars_waited"] > self.max_retest_bars:
                state = {"phase": "IDLE"}  # Timeout, no retest
            else:
                level = state["level"]
                direction = state["direction"]
                last_close = closes[-1]
                last_low = lows[-1]
                last_high = highs[-1]

                # Check if price has retested the level
                distance_pct = abs(last_close - level) / level * 100.0
                touched = False
                if direction == "LONG":
                    # For bullish B&R: price should dip back DOWN toward resistance
                    # (which is now support)
                    touched = last_low <= level * (1 + self.retest_tolerance_pct / 100.0)
                else:
                    # For bearish B&R: price should bounce back UP toward support
                    # (which is now resistance)
                    touched = last_high >= level * (1 - self.retest_tolerance_pct / 100.0)

                if touched:
                    # Check confirmation candle
                    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
                    po, pc = opens[-2], closes[-2]
                    confirmation = None

                    if is_pin_bar(o, h, l, c, direction):
                        confirmation = "pin_bar"
                    elif is_engulfing(po, pc, o, c, direction):
                        confirmation = "engulfing"
                    elif is_strong_body(o, h, l, c, direction):
                        confirmation = "strong_body"

                    if confirmation:
                        # EMIT!
                        self._last_emit[inst_id] = now
                        state = {"phase": "IDLE"}
                        self._states[inst_id] = state
                        cand = BreakRetestCandidate(
                            inst_id=inst_id,
                            direction=direction,
                            level=level,
                            breakout_bar_idx=state.get("bars_waited", 0),
                            retest_distance_pct=distance_pct,
                            confirmation=confirmation,
                            atr_pct=atr_pct,
                            adx=adx_val,
                            regime=regime,
                            timestamp=now,
                        )
                        await self.on_candidate(cand)
                        return cand

        self._states[inst_id] = state
        return None


# ----------------------------------------------------------------------
# Funding-rate contrarian (RANGE regime only)
# ----------------------------------------------------------------------


@dataclass
class FundingRateCandidate:
    inst_id: str
    direction: str
    fr: float
    fr_abs_z: float
    timestamp: float


class FundingRateSignal:
    """Only fires in RANGE regime (ADX < 20)."""

    def __init__(
        self,
        on_candidate: Callable[[FundingRateCandidate], Awaitable[None]],
        extreme_threshold: float = 0.0008,
        max_threshold: float = 0.01,
        mean_reversion_min_z: float = 2.0,
        history_size: int = 24,
    ) -> None:
        self.on_candidate = on_candidate
        self.extreme_threshold = extreme_threshold
        self.max_threshold = max_threshold
        self.mean_reversion_min_z = mean_reversion_min_z
        self.history_size = history_size
        self._history: Dict[str, Deque[float]] = {}
        self._last_emit: Dict[str, float] = {}

    def update(self, inst_id: str, fr: float,
               regime: str = "RANGE") -> Optional[FundingRateCandidate]:
        if fr is None or regime != "RANGE":
            return None
        afr = abs(fr)
        hist = self._history.setdefault(inst_id, deque(maxlen=self.history_size))
        hist.append(afr)
        if len(hist) < 6:
            return None
        prior = list(hist)[:-1]
        m = sum(prior) / len(prior)
        var = sum((v - m) ** 2 for v in prior) / len(prior)
        sd = math.sqrt(max(var, 1e-18))
        z = (afr - m) / max(sd, 1e-12)
        if afr < self.extreme_threshold or afr > self.max_threshold:
            return None
        if z < self.mean_reversion_min_z:
            return None
        now = time.time()
        if now - self._last_emit.get(inst_id, 0) < 600.0:
            return None
        self._last_emit[inst_id] = now
        return FundingRateCandidate(
            inst_id=inst_id,
            direction="LONG" if fr < 0 else "SHORT",
            fr=fr, fr_abs_z=z, timestamp=now,
        )


# ----------------------------------------------------------------------
# Liquidation (disabled)
# ----------------------------------------------------------------------


@dataclass
class LiquidationCandidate:
    inst_id: str
    direction: str
    liq_long_usd: float
    liq_short_usd: float
    liq_size_z: float
    price_drop_atr: float
    timestamp: float
    raw_event: Dict[str, Any]


class LiquidationSignal:
    """Disabled."""
    def __init__(self, on_candidate: Callable, instruments: Optional[Dict] = None,
                 **kwargs) -> None:
        self.on_candidate = on_candidate
        self.instruments = instruments or {}

    def subscribe_args(self) -> List[Dict[str, Any]]:
        return []

    async def handle_ws_message(self, msg, price_lookup, atr_lookup) -> None:
        return


# Kept for backward compat with brain features
@dataclass
class MeanReversionCandidate:
    inst_id: str
    direction: str
    mr_zscore: float
    bb_position: float
    atr_pct: float
    adx: float = 0.0
    regime: str = ""
    timestamp: float = 0.0


class MeanReversionSignal:
    """Disabled — replaced by BreakRetestSignal."""
    def __init__(self, on_candidate: Callable, **kwargs) -> None:
        self.on_candidate = on_candidate
        self.z_threshold = 99.0  # effectively disabled

    async def evaluate(self, inst_id: str, candles_30m: List[List[str]]) -> None:
        return None  # No-op


# Kept for backward compat
class TrendFollowSignal:
    """Disabled — replaced by BreakRetestSignal."""
    def __init__(self, on_candidate: Callable, **kwargs) -> None:
        self.on_candidate = on_candidate

    async def evaluate(self, inst_id: str, candles_30m: List[List[str]]) -> None:
        return None


# Kept for backward compat
@dataclass
class TrendFollowCandidate:
    inst_id: str
    direction: str
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    atr_pct: float = 0.0
    pullback_pct: float = 0.0
    regime: str = ""
    timestamp: float = 0.0


__all__ = [
    "LiquidationSignal", "LiquidationCandidate",
    "FundingRateSignal", "FundingRateCandidate",
    "MeanReversionSignal", "MeanReversionCandidate",
    "TrendFollowSignal", "TrendFollowCandidate",
    "BreakRetestSignal", "BreakRetestCandidate",
    "ema", "atr", "adx_di",
    "classify_regime", "REGIME_RANGE", "REGIME_TREND", "REGIME_NEUTRAL",
]
