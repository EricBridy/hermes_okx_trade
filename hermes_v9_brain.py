#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes v9.0 - Strategy Brain
============================

In-process, self-iterating strategy core. Decoupled from any specific exchange.

Five components, all serializable to a single JSON file:

    1. FeatureExtractor   — 16-D numerical feature vector from market snapshot
    2. OnlineLogReg       — Online logistic regression (SGD + L2) over features
                            Output: p_win = sigmoid(w·x + b)
    3. KellySizer         — Rolling avg_win / avg_loss tracker -> fractional
                            Kelly fraction with safety cap
    4. RegimeDetector     — ATR-based market regime (RANGING / TRENDING /
                            VOLATILE / NORMAL) for context-conditional thresholds
    5. PerformanceTracker — Per-symbol cooldown + per-hour stats (cheap and
                            uncorrelated with the model's learning signal)

Design rationale
----------------
The previous `adaptive_engine` used 8 *independent* Beta-Bernoulli bandits, one
per factor. That formulation has zero credit-assignment power: every closed
trade had several factors active simultaneously, so the per-factor reward was
ambiguous, and in our live data we observed alpha=beta=2.0 across the board
after 2 trades — the signal never differentiated.

Replacing that with a single linear logistic-regression model gives us proper
joint credit assignment: the gradient step `w += eta * (y - p) * x` updates
*all* features simultaneously based on the prediction error, so each feature's
weight reflects its *marginal* contribution given the others. This is the
standard online-learning baseline used in ad CTR prediction, etc.

References
----------
- Funding rate as crowding contrarian:
    https://phemex.com/academy/what-is-funding-rate-in-crypto-futures
- Microprice / L2 imbalance as short-horizon predictor:
    https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html
- Chandelier / ATR exits for volatile assets:
    https://quantstrategy.io/blog/how-to-use-the-chandelier-exit-in-trading/
- Fractional Kelly for sizing under estimation noise:
    https://quantstrategy.io/blog/applying-the-kelly-criterion-to-trading-maximizing-growth
- Online logistic regression / SGD for trading signal weighting:
    https://blog.quantinsti.com/machine-learning-logistic-regression-python/

All content rephrased and synthesized for compliance with licensing.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Default state file (override via env var if needed)
# ---------------------------------------------------------------------------

DEFAULT_BRAIN_FILE = os.path.expanduser(
    os.environ.get("HERMES_BRAIN_FILE", "~/.hermes/scripts/hermes_v9_brain.json")
)


# ---------------------------------------------------------------------------
# 1. Feature schema
# ---------------------------------------------------------------------------
#
# A single fixed feature vector schema, used by both:
#   - FeatureExtractor.build()   -> dict[str, float]
#   - OnlineLogReg.predict() / .update()
#
# Order matters because it determines the index in the weight vector. Adding a
# new feature is safe (treated as zero historically); removing or reordering
# features will invalidate a saved model — bump FEATURE_VERSION when you do
# that and the brain will reset weights instead of loading garbage.

FEATURE_VERSION = 1

FEATURE_NAMES: Tuple[str, ...] = (
    # Funding-rate signal (contrarian on extreme readings)
    "fr_abs",         # |funding rate|, scaled to ~[0, 1]
    "fr_dir_align",   # +1 if FR<0 and we go long (or FR>0 and short), else -1, else 0

    # Open-interest dynamics
    "oi_delta_15m",   # (oi_now - oi_15m_ago) / oi_15m_ago, clipped to [-1, 1]
    "oi_price_dir",   # +1 price up & OI up (real new longs), -1 price down & OI up (real new shorts), etc.

    # Order-book microstructure
    "microprice_bias",   # (microprice - mid) / mid, scaled
    "l2_imbalance",      # (sum_bid_size - sum_ask_size) / (sum_bid_size + sum_ask_size)

    # Trade-flow imbalance from recent candles (proxy for OFI)
    "taker_buy_ratio",   # bullish taker volume / total taker volume in last N candles
    "vol_spike_1m",      # recent 1m volume / prior baseline

    # Multi-timeframe trend
    "ema_alignment",     # +1 fully bullish stack, -1 fully bearish, 0 mixed
    "ha_trend_15m",      # +1 / 0 / -1 from Heikin-Ashi 15m direction
    "rsi_weighted",      # weighted average of 1m/5m/15m RSI, normalised to [-1, 1]

    # Volatility / squeeze
    "atr_pct",           # ATR(14) / price, scaled
    "bb_squeeze",        # +1 if BB width < 1.5%, else 0

    # External alpha
    "chain_score",       # chain bonus / 8.0, in [0, 1]

    # Time of day, cyclical encoding (UTC+8)
    "hour_sin",
    "hour_cos",
)

NUM_FEATURES = len(FEATURE_NAMES)


def _safe_float(x: Any, default: float = 0.0) -> float:
    """Best-effort float cast that *never* raises and *never* returns NaN/Inf."""
    try:
        if x is None or x == "":
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _clip(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# FeatureExtractor
# ---------------------------------------------------------------------------

class FeatureExtractor:
    """
    Build a fixed-length feature vector from a heterogeneous market snapshot.

    The caller passes in a `snapshot` dict containing whatever raw fields are
    available; the extractor is fully tolerant to missing keys (they become
    zeros). This means new data sources can be added without breaking the
    contract with the model.

    Expected snapshot keys (all optional):

        funding_rate         float
        oi_now               float
        oi_15m_ago           float
        chg_5m               float           5-minute price change in percent
        microprice           float           size-weighted mid
        mid_price            float
        l2_bid_size          float           sum of bid sizes within depth window
        l2_ask_size          float           sum of ask sizes within depth window
        taker_buy_vol_5m     float
        taker_sell_vol_5m    float
        vol_1m_ratio         float           recent 1m vol / baseline
        ema12_5m / ema26_5m  float           for 5m alignment
        ema12_15m/ ema26_15m float           for 15m alignment (optional)
        ha_trend_15m         "UP"/"DOWN"/"FLAT"
        rsi_1m / rsi_5m / rsi_15m   float
        atr_pct              float
        bb_width             float           in percent
        chain_score          int             raw additive score
        direction            "LONG"/"SHORT"  (for direction-aware features)
    """

    @staticmethod
    def build(snapshot: Dict[str, Any]) -> Dict[str, float]:
        f: Dict[str, float] = {n: 0.0 for n in FEATURE_NAMES}

        direction = snapshot.get("direction", "LONG")
        d_sign = 1 if direction == "LONG" else -1

        # --- Funding rate ----------------------------------------------------
        fr = _safe_float(snapshot.get("funding_rate"))
        f["fr_abs"] = _clip(abs(fr) * 200.0, 0.0, 1.0)  # |fr|=0.5% -> 1.0
        # Contrarian alignment: extreme negative FR should favour longs
        if fr < 0 and direction == "LONG":
            f["fr_dir_align"] = 1.0
        elif fr > 0 and direction == "SHORT":
            f["fr_dir_align"] = 1.0
        elif fr < 0 and direction == "SHORT":
            f["fr_dir_align"] = -1.0
        elif fr > 0 and direction == "LONG":
            f["fr_dir_align"] = -1.0

        # --- Open interest ---------------------------------------------------
        oi_now = _safe_float(snapshot.get("oi_now"))
        oi_old = _safe_float(snapshot.get("oi_15m_ago"))
        if oi_old > 0:
            oi_delta = (oi_now - oi_old) / oi_old
            f["oi_delta_15m"] = _clip(oi_delta * 5.0, -1.0, 1.0)  # 20% delta saturates

        chg_5m = _safe_float(snapshot.get("chg_5m"))
        # Direction-aware "real money" flag: with our trade direction, do we see
        # price moving in our favour AND OI rising (i.e. new positions opening)?
        if oi_old > 0:
            oi_rising = (oi_now - oi_old) / oi_old > 0.005   # >0.5% rise
            price_with_us = (d_sign * chg_5m) > 0.1          # price up if LONG
            if oi_rising and price_with_us:
                f["oi_price_dir"] = 1.0
            elif oi_rising and not price_with_us:
                f["oi_price_dir"] = -1.0  # OI rising against us = new opposite positions

        # --- Microprice / L2 imbalance --------------------------------------
        microprice = _safe_float(snapshot.get("microprice"))
        mid = _safe_float(snapshot.get("mid_price"))
        if mid > 0 and microprice > 0:
            mp_bias = (microprice - mid) / mid
            # Direction-aware: positive bias is good for longs, bad for shorts
            f["microprice_bias"] = _clip(d_sign * mp_bias * 5000.0, -1.0, 1.0)

        bid_sz = _safe_float(snapshot.get("l2_bid_size"))
        ask_sz = _safe_float(snapshot.get("l2_ask_size"))
        if bid_sz + ask_sz > 0:
            imb = (bid_sz - ask_sz) / (bid_sz + ask_sz)
            f["l2_imbalance"] = _clip(d_sign * imb, -1.0, 1.0)

        # --- Trade flow ------------------------------------------------------
        tbv = _safe_float(snapshot.get("taker_buy_vol_5m"))
        tsv = _safe_float(snapshot.get("taker_sell_vol_5m"))
        if tbv + tsv > 0:
            ratio = tbv / (tbv + tsv)            # in [0, 1]
            # Direction-aware: longs want ratio > 0.5, shorts want < 0.5
            f["taker_buy_ratio"] = _clip(d_sign * (ratio - 0.5) * 2.0, -1.0, 1.0)

        vol_ratio = _safe_float(snapshot.get("vol_1m_ratio"), 1.0)
        f["vol_spike_1m"] = _clip((vol_ratio - 1.0) / 2.0, -0.5, 1.0)

        # --- EMA alignment ---------------------------------------------------
        ema12_5 = _safe_float(snapshot.get("ema12_5m"))
        ema26_5 = _safe_float(snapshot.get("ema26_5m"))
        ema12_15 = _safe_float(snapshot.get("ema12_15m"))
        ema26_15 = _safe_float(snapshot.get("ema26_15m"))
        align = 0.0
        if ema12_5 > 0 and ema26_5 > 0:
            align += 0.5 * d_sign * _sign(ema12_5 - ema26_5)
        if ema12_15 > 0 and ema26_15 > 0:
            align += 0.5 * d_sign * _sign(ema12_15 - ema26_15)
        f["ema_alignment"] = _clip(align, -1.0, 1.0)

        # --- HA 15m trend ----------------------------------------------------
        ha = snapshot.get("ha_trend_15m", "FLAT")
        if ha == "UP":
            f["ha_trend_15m"] = float(d_sign)
        elif ha == "DOWN":
            f["ha_trend_15m"] = float(-d_sign)

        # --- Weighted multi-TF RSI ------------------------------------------
        r1 = _safe_float(snapshot.get("rsi_1m"), 50.0)
        r5 = _safe_float(snapshot.get("rsi_5m"), 50.0)
        r15 = _safe_float(snapshot.get("rsi_15m"), 50.0)
        # Higher TF weighted more (per Wilder / multi-TF best practice)
        wrsi = 0.2 * r1 + 0.3 * r5 + 0.5 * r15
        # For LONG: ideal range is 40-65 (uptrend, not yet overbought).
        # For SHORT: ideal range is 35-60.
        # Encode as direction-aware "in zone" bonus
        if direction == "LONG":
            zone = 1.0 if 40 <= wrsi <= 65 else (0.3 if 30 <= wrsi < 40 or 65 < wrsi <= 70 else -0.5)
            if wrsi > 75:
                zone = -1.0  # strong overbought, dangerous for new longs
        else:
            zone = 1.0 if 35 <= wrsi <= 60 else (0.3 if 60 < wrsi <= 70 or 30 <= wrsi < 35 else -0.5)
            if wrsi < 25:
                zone = -1.0
        f["rsi_weighted"] = zone

        # --- Volatility ------------------------------------------------------
        atr = _safe_float(snapshot.get("atr_pct"), 1.0)
        f["atr_pct"] = _clip(atr / 3.0, 0.0, 1.0)  # 3% ATR saturates
        bb = _safe_float(snapshot.get("bb_width"), 3.0)
        f["bb_squeeze"] = 1.0 if bb < 1.5 else 0.0

        # --- Chain alpha -----------------------------------------------------
        cs = _safe_float(snapshot.get("chain_score"))
        f["chain_score"] = _clip(cs / 8.0, -0.5, 1.0)

        # --- Cyclic time of day (UTC+8) -------------------------------------
        utc8 = datetime.now(timezone(timedelta(hours=8)))
        hour_frac = (utc8.hour + utc8.minute / 60.0) / 24.0
        f["hour_sin"] = math.sin(2 * math.pi * hour_frac)
        f["hour_cos"] = math.cos(2 * math.pi * hour_frac)

        # Final guard: nothing should leak NaN
        for k, v in list(f.items()):
            if math.isnan(v) or math.isinf(v):
                f[k] = 0.0
        return f

    @staticmethod
    def to_vector(features: Dict[str, float]) -> List[float]:
        return [features.get(n, 0.0) for n in FEATURE_NAMES]


# ---------------------------------------------------------------------------
# 2. Online logistic regression (SGD + L2)
# ---------------------------------------------------------------------------

class OnlineLogReg:
    """
    Online (per-sample) logistic regression with L2 regularization.

    Update rule for one (x, y) sample with y in {0, 1}:

        p     = sigmoid(w . x + b)
        w    += eta * ((y - p) * x - lam * w)
        b    += eta * (y - p)

    Calibration via Platt-style temperature `temp` is supported but defaults
    to 1.0 (no scaling). The temperature is *not* learned — it is bumped
    upward when the rolling Brier score worsens, to "cool" overconfident
    predictions.
    """

    def __init__(
        self,
        n_features: int = NUM_FEATURES,
        eta: float = 0.05,
        lam: float = 1e-4,
        eta_decay: float = 0.9995,
        eta_min: float = 0.005,
    ):
        self.n_features = n_features
        self.eta = eta
        self.eta_min = eta_min
        self.eta_decay = eta_decay
        self.lam = lam
        self.w: List[float] = [0.0] * n_features
        self.b: float = 0.0
        self.temp: float = 1.0
        self.updates: int = 0
        # Rolling Brier score (mean squared error of probability) for calibration
        self.recent_brier: List[float] = []
        self.brier_window = 50

    @staticmethod
    def _sigmoid(z: float) -> float:
        # Numerically stable sigmoid
        if z >= 0:
            ez = math.exp(-z)
            return 1.0 / (1.0 + ez)
        ez = math.exp(z)
        return ez / (1.0 + ez)

    def _logit(self, x: Sequence[float]) -> float:
        z = self.b
        for wi, xi in zip(self.w, x):
            z += wi * xi
        return z / max(self.temp, 0.1)

    def predict_proba(self, x: Sequence[float]) -> float:
        return self._sigmoid(self._logit(x))

    def update(self, x: Sequence[float], y: int) -> Tuple[float, float]:
        """
        One SGD step. Returns (p_before_update, post_update_brier_contrib).
        """
        if len(x) != self.n_features:
            raise ValueError(f"feature length {len(x)} != model dim {self.n_features}")
        if y not in (0, 1):
            y = 1 if y > 0 else 0

        p = self.predict_proba(x)
        err = y - p

        # Apply L2 shrinkage and gradient step
        new_w = []
        for wi, xi in zip(self.w, x):
            wi_new = wi + self.eta * (err * xi - self.lam * wi)
            # Hard clip to prevent runaway weights
            wi_new = _clip(wi_new, -10.0, 10.0)
            new_w.append(wi_new)
        self.w = new_w
        self.b = _clip(self.b + self.eta * err, -10.0, 10.0)

        self.updates += 1

        # Decay learning rate slowly so old experience is not overwritten by
        # a single recent fluke
        if self.eta > self.eta_min:
            self.eta = max(self.eta_min, self.eta * self.eta_decay)

        brier = (p - y) ** 2
        self.recent_brier.append(brier)
        if len(self.recent_brier) > self.brier_window:
            self.recent_brier = self.recent_brier[-self.brier_window:]

        # Auto temperature tuning: if Brier > 0.30 (worse than always-50%),
        # cool predictions toward 0.5
        if len(self.recent_brier) >= 20:
            avg_brier = sum(self.recent_brier) / len(self.recent_brier)
            if avg_brier > 0.30 and self.temp < 5.0:
                self.temp *= 1.05
            elif avg_brier < 0.20 and self.temp > 1.0:
                self.temp *= 0.98

        return p, brier

    def avg_brier(self) -> float:
        if not self.recent_brier:
            return 0.25  # uninformed prior (always p=0.5 -> brier=0.25)
        return sum(self.recent_brier) / len(self.recent_brier)

    def feature_importance(self) -> List[Tuple[str, float]]:
        return sorted(
            zip(FEATURE_NAMES, self.w),
            key=lambda t: abs(t[1]),
            reverse=True,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": FEATURE_VERSION,
            "n_features": self.n_features,
            "eta": self.eta,
            "eta_min": self.eta_min,
            "eta_decay": self.eta_decay,
            "lam": self.lam,
            "w": list(self.w),
            "b": self.b,
            "temp": self.temp,
            "updates": self.updates,
            "recent_brier": list(self.recent_brier),
        }

    def from_dict(self, d: Dict[str, Any]) -> bool:
        """Returns True on successful load, False if schema mismatched."""
        if d.get("version") != FEATURE_VERSION:
            return False
        if d.get("n_features") != self.n_features:
            return False
        try:
            self.eta = float(d.get("eta", self.eta))
            self.eta_min = float(d.get("eta_min", self.eta_min))
            self.eta_decay = float(d.get("eta_decay", self.eta_decay))
            self.lam = float(d.get("lam", self.lam))
            w = d.get("w") or []
            if len(w) == self.n_features:
                self.w = [float(x) for x in w]
            self.b = float(d.get("b", 0.0))
            self.temp = max(0.1, float(d.get("temp", 1.0)))
            self.updates = int(d.get("updates", 0))
            self.recent_brier = [float(x) for x in d.get("recent_brier", [])]
            return True
        except (TypeError, ValueError):
            return False


# ---------------------------------------------------------------------------
# 3. Kelly position sizer
# ---------------------------------------------------------------------------

class KellySizer:
    """
    Tracks rolling avg_win and avg_loss (in *fractional* return terms,
    e.g. 0.025 for +2.5%) and computes a fractional Kelly stake.

    f_full     = (p * b - (1 - p)) / b      where b = avg_win / avg_loss
    f_used     = clip(kelly_frac * f_full, 0, max_risk_pct)

    `kelly_frac` defaults to 0.25 ("quarter Kelly") to handle estimation
    noise — full Kelly is theoretically growth-optimal but in practice
    leads to drawdowns that retail accounts can't tolerate.
    """

    def __init__(self, kelly_frac: float = 0.25, max_risk_pct: float = 0.40,
                 min_risk_pct: float = 0.10, decay: float = 0.95):
        self.kelly_frac = kelly_frac
        self.max_risk_pct = max_risk_pct
        self.min_risk_pct = min_risk_pct
        self.decay = decay
        # Initialize with mild priors so we don't divide by zero on day one
        self.avg_win = 0.025   # assume 2.5% win on average (=== TP)
        self.avg_loss = 0.015  # assume 1.5% loss on average (=== SL)
        self.win_count = 0.0
        self.loss_count = 0.0
        self.total_wins_pct = 0.0
        self.total_losses_pct = 0.0

    def record(self, pnl_pct: float) -> None:
        """Update rolling stats with a fractional return (e.g. 0.0123 = +1.23%)."""
        # Apply decay so old trades fade
        self.win_count *= self.decay
        self.loss_count *= self.decay
        self.total_wins_pct *= self.decay
        self.total_losses_pct *= self.decay
        if pnl_pct > 0:
            self.win_count += 1
            self.total_wins_pct += pnl_pct
        else:
            self.loss_count += 1
            self.total_losses_pct += abs(pnl_pct)
        # Recompute averages with priors
        self.avg_win = self.total_wins_pct / max(self.win_count, 1.0) if self.win_count > 0.5 else 0.025
        self.avg_loss = self.total_losses_pct / max(self.loss_count, 1.0) if self.loss_count > 0.5 else 0.015

    def fraction(self, p_win: float) -> float:
        """Compute the fractional-Kelly stake as a fraction of equity."""
        # Sanitise
        p = _clip(p_win, 0.01, 0.99)
        b = self.avg_win / max(self.avg_loss, 1e-6)
        f_full = (p * b - (1 - p)) / max(b, 1e-6)
        # Negative edge -> zero size
        if f_full <= 0:
            return 0.0
        f = self.kelly_frac * f_full
        f = _clip(f, 0.0, self.max_risk_pct)
        # Floor: if model has any positive edge, take at least the min so we
        # actually accumulate samples to learn from
        if f > 0:
            f = max(f, self.min_risk_pct)
        return f

    def expected_value(self, p_win: float, fees_pct: float = 0.001) -> float:
        """E[PnL] per unit of position: p*win - (1-p)*loss - fees."""
        return p_win * self.avg_win - (1 - p_win) * self.avg_loss - fees_pct

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kelly_frac": self.kelly_frac,
            "max_risk_pct": self.max_risk_pct,
            "min_risk_pct": self.min_risk_pct,
            "decay": self.decay,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "total_wins_pct": self.total_wins_pct,
            "total_losses_pct": self.total_losses_pct,
        }

    def from_dict(self, d: Dict[str, Any]) -> None:
        self.kelly_frac = float(d.get("kelly_frac", self.kelly_frac))
        self.max_risk_pct = float(d.get("max_risk_pct", self.max_risk_pct))
        self.min_risk_pct = float(d.get("min_risk_pct", self.min_risk_pct))
        self.decay = float(d.get("decay", self.decay))
        self.avg_win = float(d.get("avg_win", self.avg_win))
        self.avg_loss = float(d.get("avg_loss", self.avg_loss))
        self.win_count = float(d.get("win_count", 0.0))
        self.loss_count = float(d.get("loss_count", 0.0))
        self.total_wins_pct = float(d.get("total_wins_pct", 0.0))
        self.total_losses_pct = float(d.get("total_losses_pct", 0.0))


# ---------------------------------------------------------------------------
# 4. Regime detector
# ---------------------------------------------------------------------------

class RegimeDetector:
    """
    Lightweight ATR-based regime classifier. Same role as the v8.1 detector
    but with thresholds re-tuned for the actual altcoin range we observed.
    """

    HISTORY_SIZE = 60

    def __init__(self):
        self.atr_history: List[Tuple[float, float]] = []  # (ts, median_atr_pct)
        self.regime: str = "UNKNOWN"
        self.regime_since: float = time.time()
        self.confidence: float = 0.0

    def update(self, atr_pcts: Sequence[float]) -> None:
        if not atr_pcts:
            return
        sorted_ = sorted(atr_pcts)
        median = sorted_[len(sorted_) // 2]
        now = time.time()
        self.atr_history.append((now, median))
        if len(self.atr_history) > self.HISTORY_SIZE:
            self.atr_history = self.atr_history[-self.HISTORY_SIZE:]
        if len(self.atr_history) < 10:
            self.regime = "UNKNOWN"
            return

        recent = [x[1] for x in self.atr_history[-10:]]
        avg_recent = sum(recent) / len(recent)
        avg_all = sum(x[1] for x in self.atr_history) / len(self.atr_history)
        std = (sum((x - avg_recent) ** 2 for x in recent) / len(recent)) ** 0.5
        cv = std / max(avg_recent, 1e-6)

        old = self.regime
        if avg_recent < 0.25 and cv < 0.15:
            self.regime = "RANGING"
            self.confidence = _clip(0.6 * (0.25 - avg_recent) / 0.15
                                    + 0.4 * (0.15 - cv) / 0.10, 0.0, 0.95)
        elif avg_recent > 2.5:
            self.regime = "VOLATILE"
            self.confidence = _clip((avg_recent - 2.5) / 2.0, 0.0, 1.0)
        elif avg_recent > avg_all * 1.3:
            self.regime = "TRENDING"
            self.confidence = _clip((avg_recent / max(avg_all, 1e-6) - 1.0) / 0.5, 0.0, 1.0)
        else:
            self.regime = "NORMAL"
            self.confidence = 0.5

        if old != self.regime:
            self.regime_since = now

    def edge_threshold(self, base_edge: float) -> float:
        """
        Adjust the minimum expected-value gate by regime.
        Tighter (higher) in adverse regimes.
        """
        if self.regime == "RANGING":
            return base_edge * (1.0 + 0.5 * self.confidence)  # up to 1.5x harder
        if self.regime == "VOLATILE":
            return base_edge * 1.3
        if self.regime == "TRENDING":
            return base_edge * (1.0 - 0.3 * self.confidence)  # easier in trends
        return base_edge

    def status_str(self) -> str:
        dur_min = int((time.time() - self.regime_since) / 60)
        return f"{self.regime}({self.confidence:.0%},{dur_min}min)"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "atr_history": self.atr_history[-30:],
            "regime": self.regime,
            "regime_since": self.regime_since,
            "confidence": self.confidence,
        }

    def from_dict(self, d: Dict[str, Any]) -> None:
        self.atr_history = [(float(t), float(v)) for t, v in d.get("atr_history", [])]
        self.regime = d.get("regime", "UNKNOWN")
        self.regime_since = float(d.get("regime_since", time.time()))
        self.confidence = float(d.get("confidence", 0.0))


# ---------------------------------------------------------------------------
# 5. Performance tracker (cooldowns + per-symbol stats)
# ---------------------------------------------------------------------------

class PerformanceTracker:
    """
    Per-symbol consecutive-loss cooldown + per-hour win-rate stats.
    Smaller surface than the v8.1 tracker — most of the "what's working"
    learning has moved into the logistic regression weights.
    """

    COOLDOWN_LOSSES = 3
    COOLDOWN_SECONDS = 7200  # 2 hours

    def __init__(self):
        self.symbol_state: Dict[str, Dict[str, Any]] = {}
        self.hourly: Dict[int, Dict[str, float]] = {
            h: {"wins": 0.0, "losses": 0.0, "pnl": 0.0} for h in range(24)
        }

    def is_cold(self, symbol: str) -> bool:
        s = self.symbol_state.get(symbol)
        if not s:
            return False
        return time.time() < s.get("cold_until", 0.0)

    def record(self, symbol: str, pnl: float) -> None:
        s = self.symbol_state.setdefault(
            symbol, {"streak": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                     "cold_until": 0.0, "last_trade": 0.0}
        )
        if pnl > 0:
            s["streak"] = 0
            s["wins"] += 1
        else:
            s["streak"] += 1
            s["losses"] += 1
            if s["streak"] >= self.COOLDOWN_LOSSES:
                s["cold_until"] = time.time() + self.COOLDOWN_SECONDS
                s["streak"] = 0
        s["pnl"] += pnl
        s["last_trade"] = time.time()

        utc8 = datetime.now(timezone(timedelta(hours=8)))
        h = utc8.hour
        bucket = self.hourly.setdefault(h, {"wins": 0.0, "losses": 0.0, "pnl": 0.0})
        if pnl > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["pnl"] += pnl

    def status_str(self) -> str:
        cold = sum(1 for s in self.symbol_state.values()
                   if time.time() < s.get("cold_until", 0))
        utc8 = datetime.now(timezone(timedelta(hours=8)))
        h = utc8.hour
        bucket = self.hourly.get(h, {"wins": 0, "losses": 0, "pnl": 0})
        tot = bucket["wins"] + bucket["losses"]
        wr = (bucket["wins"] / tot * 100) if tot > 0 else 0
        return f"hour={h:02d}({wr:.0f}%/{int(tot)}笔) cold={cold}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol_state": self.symbol_state,
            "hourly": {str(k): v for k, v in self.hourly.items()},
        }

    def from_dict(self, d: Dict[str, Any]) -> None:
        self.symbol_state = d.get("symbol_state", {})
        # JSON has string keys for ints
        hourly = d.get("hourly", {})
        for k, v in hourly.items():
            try:
                self.hourly[int(k)] = v
            except (TypeError, ValueError):
                pass


# ---------------------------------------------------------------------------
# Strategy Brain — composition of the five components
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    pnl_usd: float
    pnl_pct: float
    open_ts: float
    close_ts: float
    features: Dict[str, float]
    p_win_predicted: float
    expected_value: float
    fraction_used: float
    exit_reason: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl_usd": self.pnl_usd,
            "pnl_pct": self.pnl_pct,
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "features": self.features,
            "p_win_predicted": self.p_win_predicted,
            "expected_value": self.expected_value,
            "fraction_used": self.fraction_used,
            "exit_reason": self.exit_reason,
            "extra": self.extra,
        }


class StrategyBrain:
    """
    Top-level facade. The trading loop interacts only with this class.
    """

    def __init__(self, brain_file: str = DEFAULT_BRAIN_FILE,
                 trades_log: Optional[str] = None,
                 min_edge: float = 0.001):
        self.brain_file = brain_file
        self.trades_log = trades_log or os.path.join(
            os.path.dirname(brain_file), "hermes_v9_trades.jsonl"
        )
        self.min_edge = min_edge

        self.feature_extractor = FeatureExtractor()
        self.model = OnlineLogReg()
        self.kelly = KellySizer()
        self.regime = RegimeDetector()
        self.tracker = PerformanceTracker()
        self.last_save = 0.0
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if os.path.exists(self.brain_file):
                with open(self.brain_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not self.model.from_dict(data.get("model", {})):
                    # Schema bump: keep regime/tracker but reset model
                    self.model = OnlineLogReg()
                self.kelly.from_dict(data.get("kelly", {}))
                self.regime.from_dict(data.get("regime", {}))
                self.tracker.from_dict(data.get("tracker", {}))
        except Exception:
            # Corrupt state file -> safer to keep defaults than crash
            pass

    def save(self, force: bool = False) -> None:
        # Throttle writes — at most once every 30s unless forced
        if not force and time.time() - self.last_save < 30 and not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.brain_file), exist_ok=True)
            data = {
                "version": FEATURE_VERSION,
                "saved_at": time.time(),
                "model": self.model.to_dict(),
                "kelly": self.kelly.to_dict(),
                "regime": self.regime.to_dict(),
                "tracker": self.tracker.to_dict(),
            }
            tmp = self.brain_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.brain_file)
            self.last_save = time.time()
            self._dirty = False
        except Exception:
            pass

    def _append_trade_log(self, rec: TradeRecord) -> None:
        try:
            os.makedirs(os.path.dirname(self.trades_log), exist_ok=True)
            with open(self.trades_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Decision API (called from trading loop)
    # ------------------------------------------------------------------
    def update_regime(self, atr_pcts: Sequence[float]) -> None:
        self.regime.update(atr_pcts)
        self._dirty = True

    def is_symbol_cold(self, symbol: str) -> bool:
        return self.tracker.is_cold(symbol)

    def evaluate(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Score a candidate. Returns a dict with the decision artefacts.

        Inputs:
            snapshot: dict with raw market features (see FeatureExtractor)

        Returns:
            {
              "features": Dict[str, float],
              "vector":   List[float],
              "p_win":    float,
              "ev":       float,        # expected fractional return per unit
              "fraction": float,        # fraction of equity to deploy
              "passes":   bool,         # ev > regime-adjusted edge AND fraction > 0
              "reason":   str,          # short why-text
            }
        """
        features = self.feature_extractor.build(snapshot)
        x = self.feature_extractor.to_vector(features)
        p_win = self.model.predict_proba(x)
        ev = self.kelly.expected_value(p_win)
        fraction = self.kelly.fraction(p_win)
        edge_required = self.regime.edge_threshold(self.min_edge)
        passes = ev > edge_required and fraction > 0
        reason = (f"p_win={p_win:.2%} ev={ev:+.4f} need>{edge_required:+.4f} "
                  f"f={fraction:.2%} reg={self.regime.regime}")
        return {
            "features": features,
            "vector": x,
            "p_win": p_win,
            "ev": ev,
            "fraction": fraction,
            "edge_required": edge_required,
            "passes": passes,
            "reason": reason,
        }

    def record_trade(self, rec: TradeRecord) -> Dict[str, Any]:
        """
        Called once per closed trade. Performs the full learning step:
          1) Append full record to JSONL trade log
          2) Online SGD update of the logistic-regression model
          3) Update Kelly rolling stats (using fractional return)
          4) Update PerformanceTracker (per-symbol cooldown, per-hour stats)

        Returns a small dict with diagnostic info for logging.
        """
        # 1. Persist raw record (always, regardless of model state)
        self._append_trade_log(rec)

        # 2. Convert feature dict back to vector (in canonical order)
        x = self.feature_extractor.to_vector(rec.features)
        y = 1 if rec.pnl_usd > 0 else 0
        p_before, brier = self.model.update(x, y)

        # 3. Kelly rolling stats — use fractional return
        self.kelly.record(rec.pnl_pct)

        # 4. Tracker
        self.tracker.record(rec.symbol, rec.pnl_usd)

        self._dirty = True
        self.save()  # throttled

        return {
            "p_before": p_before,
            "brier": brier,
            "avg_brier": self.model.avg_brier(),
            "model_temp": self.model.temp,
            "kelly_b": self.kelly.avg_win / max(self.kelly.avg_loss, 1e-6),
        }

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status_str(self) -> str:
        topk = self.model.feature_importance()[:4]
        feat_str = " ".join(f"{n[:8]}={w:+.2f}" for n, w in topk)
        return (
            f"[brain] reg={self.regime.status_str()} | "
            f"kelly b={self.kelly.avg_win/max(self.kelly.avg_loss,1e-6):.2f} "
            f"p+={self.kelly.win_count:.1f}/{self.kelly.loss_count:.1f} | "
            f"model n={self.model.updates} brier={self.model.avg_brier():.3f} "
            f"T={self.model.temp:.2f} | top: {feat_str} | "
            f"{self.tracker.status_str()}"
        )


__all__ = [
    "FEATURE_VERSION",
    "FEATURE_NAMES",
    "FeatureExtractor",
    "OnlineLogReg",
    "KellySizer",
    "RegimeDetector",
    "PerformanceTracker",
    "StrategyBrain",
    "TradeRecord",
]
