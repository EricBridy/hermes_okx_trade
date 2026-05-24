#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes v10 — Strategy Brain
============================

Self-iterating learning core for the v10 trading strategy. Pure Python,
no exchange dependencies, fully unit-testable.

Signal types that the brain receives at evaluation time:
    LIQ : liquidation-cascade reversal
    FR  : extreme funding-rate contrarian (experimental)
    MR  : 30-minute mean reversion
    BR  : break & retest continuation

Each candidate trade is described by a fixed feature vector. A single
unified online logistic regression (SGD + L2) learns p_win across all
signal types — the one-hot signal-type features let the model discover
which family is working and which one is not.

Why one model not three:
    Three small datasets with imbalanced flow (LIQ events are rare,
    FR happens 3x/day, MR signals fire daily) would each take months
    to converge separately. A unified model learns shared structure
    (atr_pct, time-of-day, microprice) jointly, then specialises via
    the signal-type indicators.

References (rephrased for compliance):
- Online logistic regression / SGD for trading signal weighting:
    https://blog.quantinsti.com/machine-learning-logistic-regression-python/
- Fractional Kelly under estimation noise:
    https://quantstrategy.io/blog/applying-the-kelly-criterion-to-trading-maximizing-growth
- Microprice as the strongest LOB single-feature predictor:
    https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html
- Liquidation cascade reversal mechanism:
    https://hummingbot.org/blog/coding-a-liquidation-sniper-v2-strategy-controller/
- Funding rate as crowding contrarian:
    https://phemex.com/academy/what-is-funding-rate-in-crypto-futures
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------

FEATURE_VERSION = 2

FEATURE_NAMES: Tuple[str, ...] = (
    # 0-2  : market context (always populated)
    "atr_pct",          # 5m ATR / price, scaled to ~[0, 1]
    "hour_sin",         # cyclic time of day (UTC+8)
    "hour_cos",
    # 3-6  : signal type one-hot (mutually exclusive)
    "is_liq",
    "is_fr",
    "is_mr",
    "is_br",
    # 7    : trade direction (sign-aware downstream features)
    "dir_long",         # +1 for long, -1 for short
    # 8-10 : LIQ-only features (zero when is_liq=0)
    "liq_size_z",       # 60s cumulative liq size, z-score vs 24h baseline
    "liq_imbalance",    # direction-aware: positive when reversal favours us
    "price_drop_atr",   # how far has price moved during cascade, in ATRs
    # 11   : FR-only feature (zero when is_fr=0)
    "fr_abs_z",         # |FR| z-score vs symbol's |FR| history
    # 12-13: MR-only features (zero when is_mr=0)
    "mr_zscore",        # direction-aware: +ve when entering against deviation
    "bb_position",      # price's location in 30M Bollinger band, [-1, 1]
    # 14-18: BR-only features (zero when is_br=0)
    "br_breakout_age",  # recentness of breakout, scaled to [0, 1]
    "br_retest_tight",  # closeness of retest to the broken level, [0, 1]
    "br_pin_bar",
    "br_engulfing",
    "br_strong_body",
    # 19-21: microstructure (always populated)
    "microprice_bias",  # direction-aware
    "l2_imbalance",     # direction-aware
    "taker_buy_ratio",  # direction-aware
)
NUM_FEATURES = len(FEATURE_NAMES)

DEFAULT_BRAIN_FILE = os.environ.get(
    "HERMES_V10_BRAIN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes_v11_brain.json"),
)

# ----------------------------------------------------------------------
# Numeric helpers (defensive)
# ----------------------------------------------------------------------


def safe_float(x: Any, default: float = 0.0) -> float:
    """Tolerant float cast — never raises, never returns NaN/Inf."""
    try:
        if x is None or x == "":
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


# ----------------------------------------------------------------------
# Feature extractor
# ----------------------------------------------------------------------


class FeatureExtractor:
    """
    Build a fixed-length feature vector from a heterogeneous candidate dict.
    Highly tolerant of missing fields (they become zeros).

    Expected candidate keys:
        signal_type      "LIQ" / "FR" / "MR" / "BR"      required
        direction        "LONG" / "SHORT"                required
        atr_pct          5m ATR percent                  optional
        # LIQ:
        liq_size_z       float
        liq_long_usd     float (longs liquidated in last 60s)
        liq_short_usd    float (shorts liquidated in last 60s)
        price_drop_atr   float (signed: + when price dropped)
        # FR:
        fr_abs_z         float
        fr                float (signed)
        # MR:
        mr_zscore        float (signed: + above mean)
        bb_position      float in [-1, 1]
        # BR:
        breakout_bar_idx int (bars since breakout)
        retest_distance_pct float
        confirmation     str ("pin_bar" / "engulfing" / "strong_body")
        # microstructure:
        microprice       float
        mid_price        float
        l2_bid_size      float
        l2_ask_size      float
        taker_buy_5m     float
        taker_sell_5m    float
    """

    @staticmethod
    def build(c: Dict[str, Any]) -> Dict[str, float]:
        f: Dict[str, float] = {n: 0.0 for n in FEATURE_NAMES}

        sig = (c.get("signal_type") or "").upper()
        direction = (c.get("direction") or "LONG").upper()
        d = 1 if direction == "LONG" else -1
        f["dir_long"] = float(d)
        f["is_liq"] = 1.0 if sig == "LIQ" else 0.0
        f["is_fr"] = 1.0 if sig == "FR" else 0.0
        f["is_mr"] = 1.0 if sig == "MR" else 0.0
        f["is_br"] = 1.0 if sig == "BR" else 0.0

        # ----- Common: ATR + cyclic time -----
        atr = safe_float(c.get("atr_pct"))
        f["atr_pct"] = clip(atr / 3.0, 0.0, 1.0)  # 3% saturates

        utc8 = datetime.now(timezone(timedelta(hours=8)))
        h = (utc8.hour + utc8.minute / 60.0) / 24.0
        f["hour_sin"] = math.sin(2 * math.pi * h)
        f["hour_cos"] = math.cos(2 * math.pi * h)

        # ----- LIQ features -----
        if sig == "LIQ":
            f["liq_size_z"] = clip(safe_float(c.get("liq_size_z")), -3.0, 5.0) / 5.0
            longs = safe_float(c.get("liq_long_usd"))
            shorts = safe_float(c.get("liq_short_usd"))
            tot = longs + shorts
            if tot > 0:
                # In a long-liquidation cascade, longs > shorts, price drops.
                # We want to LONG (bounce back). So liq_imbalance is + when
                # the cascade direction matches the *fade* direction we take.
                imb = (longs - shorts) / tot  # +1 = pure long-liq cascade
                # If we are LONG (fading the cascade), positive imb is good
                # If we are SHORT (fading short-squeeze), negative imb is good
                f["liq_imbalance"] = clip(d * imb, -1.0, 1.0)
            drop = safe_float(c.get("price_drop_atr"))
            # If we fade a long-liq cascade (we LONG), price has dropped =
            # positive drop value. We want d * (-drop) > 0 when fading.
            # If we LONG (d=+1) after a -3 ATR drop, the "extremity" is +3.
            f["price_drop_atr"] = clip(-d * drop / 3.0, -1.0, 1.0)

        # ----- FR feature -----
        if sig == "FR":
            f["fr_abs_z"] = clip(safe_float(c.get("fr_abs_z")), -1.0, 5.0) / 5.0

        # ----- MR features -----
        if sig == "MR":
            zs = safe_float(c.get("mr_zscore"))
            # MR fades extremes: if z=+2 (price above mean), we SHORT (d=-1).
            # We want d * (-z) > 0 in good setups.
            f["mr_zscore"] = clip(-d * zs / 3.0, -1.0, 1.0)
            bbp = safe_float(c.get("bb_position"))
            f["bb_position"] = clip(-d * bbp, -1.0, 1.0)

        # ----- BR features -----
        if sig == "BR":
            age = safe_float(c.get("breakout_bar_idx"))
            f["br_breakout_age"] = clip(1.0 - age / 10.0, 0.0, 1.0)

            retest = safe_float(c.get("retest_distance_pct"))
            f["br_retest_tight"] = clip(1.0 - retest / 1.0, 0.0, 1.0)

            conf = (c.get("confirmation") or "").lower()
            f["br_pin_bar"] = 1.0 if conf == "pin_bar" else 0.0
            f["br_engulfing"] = 1.0 if conf == "engulfing" else 0.0
            f["br_strong_body"] = 1.0 if conf == "strong_body" else 0.0

        # ----- Common: microstructure -----
        mp = safe_float(c.get("microprice"))
        mid = safe_float(c.get("mid_price"))
        if mp > 0 and mid > 0:
            mp_bias = (mp - mid) / mid
            # Positive bias = buy pressure. For LONG (d=+1) this is good.
            f["microprice_bias"] = clip(d * mp_bias * 5000.0, -1.0, 1.0)

        bid = safe_float(c.get("l2_bid_size"))
        ask = safe_float(c.get("l2_ask_size"))
        if bid + ask > 0:
            imb = (bid - ask) / (bid + ask)
            f["l2_imbalance"] = clip(d * imb, -1.0, 1.0)

        tbv = safe_float(c.get("taker_buy_5m"))
        tsv = safe_float(c.get("taker_sell_5m"))
        if tbv + tsv > 0:
            ratio = tbv / (tbv + tsv) - 0.5
            f["taker_buy_ratio"] = clip(d * ratio * 2.0, -1.0, 1.0)

        # Final NaN guard
        for k, v in list(f.items()):
            if math.isnan(v) or math.isinf(v):
                f[k] = 0.0
        return f

    @staticmethod
    def to_vector(features: Dict[str, float]) -> List[float]:
        return [features.get(n, 0.0) for n in FEATURE_NAMES]


# ----------------------------------------------------------------------
# Online logistic regression (SGD + L2)
# ----------------------------------------------------------------------


class OnlineLogReg:
    """
    Bernoulli logistic regression updated one sample at a time.
    Numerically stable sigmoid, weights clipped to prevent runaways,
    auto-temperature based on rolling Brier score.
    """

    def __init__(
        self,
        n_features: int = NUM_FEATURES,
        eta: float = 0.05,
        eta_min: float = 0.005,
        eta_decay: float = 0.9995,
        lam: float = 1e-4,
    ) -> None:
        self.n_features = n_features
        self.eta = eta
        self.eta_min = eta_min
        self.eta_decay = eta_decay
        self.lam = lam
        self.w: List[float] = [0.0] * n_features
        self.b: float = 0.0
        self.temp: float = 1.0
        self.updates: int = 0
        self.recent_brier: List[float] = []
        self.brier_window = 50

    @staticmethod
    def _sigmoid(z: float) -> float:
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
        if len(x) != self.n_features:
            raise ValueError(f"feature length {len(x)} != model {self.n_features}")
        y = 1 if y > 0 else 0
        p = self.predict_proba(x)
        err = y - p
        new_w = []
        for wi, xi in zip(self.w, x):
            wi_new = wi + self.eta * (err * xi - self.lam * wi)
            new_w.append(clip(wi_new, -10.0, 10.0))
        self.w = new_w
        self.b = clip(self.b + self.eta * err, -10.0, 10.0)
        self.updates += 1
        if self.eta > self.eta_min:
            self.eta = max(self.eta_min, self.eta * self.eta_decay)
        brier = (p - y) ** 2
        self.recent_brier.append(brier)
        if len(self.recent_brier) > self.brier_window:
            self.recent_brier = self.recent_brier[-self.brier_window:]
        if len(self.recent_brier) >= 20:
            avg = sum(self.recent_brier) / len(self.recent_brier)
            if avg > 0.30 and self.temp < 5.0:
                self.temp *= 1.05
            elif avg < 0.20 and self.temp > 1.0:
                self.temp *= 0.98
        return p, brier

    def avg_brier(self) -> float:
        return sum(self.recent_brier) / len(self.recent_brier) if self.recent_brier else 0.25

    def feature_importance(self) -> List[Tuple[str, float]]:
        return sorted(zip(FEATURE_NAMES, self.w), key=lambda t: abs(t[1]), reverse=True)

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
        if d.get("version") != FEATURE_VERSION or d.get("n_features") != self.n_features:
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


# ----------------------------------------------------------------------
# Fractional Kelly sizer
# ----------------------------------------------------------------------


class KellySizer:
    """
    Pseudo-count prior version. Tracks avg_win / avg_loss as the running
    mean of (prior + observed) returns, which means a single outlier early
    trade cannot move the Kelly ratio off course.

    Returns are in MARGIN-LEVEL fractional terms (i.e. PnL / margin), so
    they are already multiplied by leverage. A 2.5% TP at 6x leverage is
    a 0.15 win; a 1.2% SL at 6x leverage is a 0.072 loss. The prior is
    set in this same unit.

    Default prior: avg_win = 0.07, avg_loss = 0.04, strength = 5
        b_prior = 0.07 / 0.04 = 1.75   (positive edge at p=0.5)

    Even after 1 outlier loss of 0.0736:
        loss_count    = decay*5 + 1     ≈ 5.75
        total_losses  = decay*0.20 + 0.0736 ≈ 0.264
        avg_loss      = 0.264 / 5.75    ≈ 0.046
        b             = 0.07 / 0.046    ≈ 1.52      (still trades)
    """

    def __init__(
        self,
        kelly_frac: float = 0.25,
        max_risk_pct: float = 0.40,
        min_risk_pct: float = 0.10,
        decay: float = 0.95,
        prior_avg_win: float = 0.10,    # margin-level (after leverage), 期望平均赢面 10%
        prior_avg_loss: float = 0.05,   # margin-level，期望平均亏面 5%
        prior_strength: float = 5.0,    # equivalent to 5 prior samples each side
    ) -> None:
        self.kelly_frac = kelly_frac
        self.max_risk_pct = max_risk_pct
        self.min_risk_pct = min_risk_pct
        self.decay = decay
        self.prior_avg_win = prior_avg_win
        self.prior_avg_loss = prior_avg_loss
        self.prior_strength = prior_strength
        # Initialise running counters with the prior already baked in
        self.win_count = prior_strength
        self.loss_count = prior_strength
        self.total_wins_pct = prior_avg_win * prior_strength
        self.total_losses_pct = prior_avg_loss * prior_strength
        self.avg_win = prior_avg_win
        self.avg_loss = prior_avg_loss

    def record(self, pnl_pct: float) -> None:
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
        # Pseudo-counts mean we always have >= 0.1 effective samples
        self.avg_win = self.total_wins_pct / max(self.win_count, 0.1)
        self.avg_loss = self.total_losses_pct / max(self.loss_count, 0.1)

    def fraction(self, p_win: float) -> float:
        p = clip(p_win, 0.01, 0.99)
        b = self.avg_win / max(self.avg_loss, 1e-6)
        f_full = (p * b - (1 - p)) / max(b, 1e-6)
        if f_full <= 0:
            return 0.0
        f = clip(self.kelly_frac * f_full, 0.0, self.max_risk_pct)
        return max(f, self.min_risk_pct) if f > 0 else 0.0

    def expected_value(self, p_win: float, fee_pct: float = 0.001) -> float:
        return p_win * self.avg_win - (1 - p_win) * self.avg_loss - fee_pct

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kelly_frac": self.kelly_frac,
            "max_risk_pct": self.max_risk_pct,
            "min_risk_pct": self.min_risk_pct,
            "decay": self.decay,
            "prior_avg_win": self.prior_avg_win,
            "prior_avg_loss": self.prior_avg_loss,
            "prior_strength": self.prior_strength,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "total_wins_pct": self.total_wins_pct,
            "total_losses_pct": self.total_losses_pct,
        }

    def from_dict(self, d: Dict[str, Any]) -> None:
        # Detect old-format save (no prior_strength key) and migrate cleanly:
        # rebuild counters from saved (avg_win, avg_loss) blended with the
        # new prior, treating the saved values as if they came from
        # prior_strength historical samples.
        is_old_format = "prior_strength" not in d
        for k in ("kelly_frac", "max_risk_pct", "min_risk_pct", "decay",
                 "prior_avg_win", "prior_avg_loss", "prior_strength"):
            if k in d:
                try:
                    setattr(self, k, float(d[k]))
                except (TypeError, ValueError):
                    pass
        if is_old_format:
            # Re-seed counters with the new prior; discard any old
            # accumulated state because units are wrong (see class docstring).
            self.win_count = self.prior_strength
            self.loss_count = self.prior_strength
            self.total_wins_pct = self.prior_avg_win * self.prior_strength
            self.total_losses_pct = self.prior_avg_loss * self.prior_strength
            self.avg_win = self.prior_avg_win
            self.avg_loss = self.prior_avg_loss
        else:
            for k in ("avg_win", "avg_loss", "win_count", "loss_count",
                     "total_wins_pct", "total_losses_pct"):
                if k in d:
                    try:
                        setattr(self, k, float(d[k]))
                    except (TypeError, ValueError):
                        pass


# ----------------------------------------------------------------------
# Per-symbol cool-down + per-signal-type performance
# ----------------------------------------------------------------------


class PerformanceTracker:
    COOLDOWN_LOSSES = 3
    COOLDOWN_SECONDS = 7200

    def __init__(self) -> None:
        self.symbol_state: Dict[str, Dict[str, Any]] = {}
        self.by_signal: Dict[str, Dict[str, float]] = {
            s: {"wins": 0.0, "losses": 0.0, "pnl": 0.0}
            for s in ("LIQ", "FR", "MR", "BR")
        }

    def is_cold(self, symbol: str) -> bool:
        s = self.symbol_state.get(symbol)
        return bool(s and time.time() < s.get("cold_until", 0.0))

    def record(self, symbol: str, signal_type: str, pnl: float) -> None:
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
        bucket = self.by_signal.setdefault(
            signal_type, {"wins": 0.0, "losses": 0.0, "pnl": 0.0}
        )
        if pnl > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["pnl"] += pnl

    def signal_winrate(self, signal_type: str) -> Tuple[int, float]:
        b = self.by_signal.get(signal_type, {"wins": 0, "losses": 0})
        w = b.get("wins", 0)
        l = b.get("losses", 0)
        n = int(w + l)
        return n, (w / n if n else 0.0)

    def status_str(self) -> str:
        parts = []
        for sig in ("BR", "MR", "FR", "LIQ"):
            n, wr = self.signal_winrate(sig)
            parts.append(f"{sig}={int(self.by_signal[sig]['wins'])}/{n}({wr*100:.0f}%)")
        cold = sum(1 for s in self.symbol_state.values()
                   if time.time() < s.get("cold_until", 0))
        return " ".join(parts) + f" cold={cold}"

    def to_dict(self) -> Dict[str, Any]:
        return {"symbol_state": self.symbol_state, "by_signal": self.by_signal}

    def from_dict(self, d: Dict[str, Any]) -> None:
        self.symbol_state = d.get("symbol_state", {})
        for s in ("LIQ", "FR", "MR", "BR"):
            if s in d.get("by_signal", {}):
                self.by_signal[s] = d["by_signal"][s]


# ----------------------------------------------------------------------
# TradeRecord (immutable post-trade snapshot)
# ----------------------------------------------------------------------


@dataclass
class TradeRecord:
    symbol: str
    signal_type: str               # "LIQ" / "FR" / "MR" / "BR"
    direction: str                 # "LONG" / "SHORT"
    entry_price: float
    exit_price: float
    pnl_usd: float
    pnl_pct: float                 # fraction relative to margin
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
            "symbol": self.symbol, "signal_type": self.signal_type,
            "direction": self.direction, "entry_price": self.entry_price,
            "exit_price": self.exit_price, "pnl_usd": self.pnl_usd,
            "pnl_pct": self.pnl_pct, "open_ts": self.open_ts,
            "close_ts": self.close_ts, "features": self.features,
            "p_win_predicted": self.p_win_predicted,
            "expected_value": self.expected_value,
            "fraction_used": self.fraction_used,
            "exit_reason": self.exit_reason, "extra": self.extra,
        }


# ----------------------------------------------------------------------
# StrategyBrain — top-level facade for the trading loop
# ----------------------------------------------------------------------


class StrategyBrain:
    """
    The trading engine talks only to this class. Composes:
        FeatureExtractor   — vector building
        OnlineLogReg       — p_win prediction
        KellySizer         — capital fraction
        PerformanceTracker — cool-downs + per-signal stats
    """

    def __init__(
        self,
        brain_file: str = DEFAULT_BRAIN_FILE,
        trades_jsonl: Optional[str] = None,
        min_edge: float = 0.0008,
    ) -> None:
        self.brain_file = brain_file
        self.trades_jsonl = trades_jsonl or os.path.join(
            os.path.dirname(brain_file), "hermes_v11_trades.jsonl"
        )
        self.min_edge = min_edge
        self.fe = FeatureExtractor()
        self.model = OnlineLogReg()
        self.kelly = KellySizer()
        self.tracker = PerformanceTracker()
        self.last_save = 0.0
        self._dirty = False
        self._load()

    def _reset_runtime_state(self) -> None:
        self.model = OnlineLogReg()
        self.kelly = KellySizer()
        self.tracker = PerformanceTracker()

    # -------- persistence --------
    def _load(self) -> None:
        try:
            if os.path.exists(self.brain_file):
                with open(self.brain_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("version") != FEATURE_VERSION:
                    self._reset_runtime_state()
                    return
                if not self.model.from_dict(d.get("model", {})):
                    self._reset_runtime_state()
                    return
                self.kelly.from_dict(d.get("kelly", {}))
                self.tracker.from_dict(d.get("tracker", {}))
        except Exception:
            self._reset_runtime_state()

    def save(self, force: bool = False) -> None:
        if not force and time.time() - self.last_save < 30 and not self._dirty:
            return
        try:
            brain_dir = os.path.dirname(self.brain_file)
            if brain_dir:
                os.makedirs(brain_dir, exist_ok=True)
            data = {
                "version": FEATURE_VERSION,
                "saved_at": time.time(),
                "model": self.model.to_dict(),
                "kelly": self.kelly.to_dict(),
                "tracker": self.tracker.to_dict(),
            }
            tmp = self.brain_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, self.brain_file)
            except Exception:
                with open(self.brain_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            self.last_save = time.time()
            self._dirty = False
        except Exception:
            pass

    def _append_trade(self, rec: TradeRecord) -> None:
        try:
            os.makedirs(os.path.dirname(self.trades_jsonl), exist_ok=True)
            with open(self.trades_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass

    # -------- decision API --------
    def is_symbol_cold(self, symbol: str) -> bool:
        return self.tracker.is_cold(symbol)

    def evaluate(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """
        Score a candidate trade. Returns dict with all decision artefacts.
        """
        feats = self.fe.build(candidate)
        x = self.fe.to_vector(feats)
        p = self.model.predict_proba(x)
        ev = self.kelly.expected_value(p)
        fraction = self.kelly.fraction(p) if ev > self.min_edge else 0.0
        passes = ev > self.min_edge and fraction > 0
        return {
            "features": feats,
            "vector": x,
            "p_win": p,
            "ev": ev,
            "fraction": fraction,
            "edge_required": self.min_edge,
            "passes": passes,
            "reason": (f"p={p:.2%} ev={ev:+.4f} need>{self.min_edge:+.4f} "
                       f"f={fraction:.0%}"),
        }

    def record_trade(self, rec: TradeRecord) -> Dict[str, Any]:
        """Apply learning step + persist."""
        self._append_trade(rec)
        x = self.fe.to_vector(rec.features)
        y = 1 if rec.pnl_usd > 0 else 0
        p_before, brier = self.model.update(x, y)
        self.kelly.record(rec.pnl_pct)
        self.tracker.record(rec.symbol, rec.signal_type, rec.pnl_usd)
        self._dirty = True
        self.save()
        return {
            "p_before": p_before,
            "brier": brier,
            "avg_brier": self.model.avg_brier(),
            "model_temp": self.model.temp,
            "kelly_b": self.kelly.avg_win / max(self.kelly.avg_loss, 1e-6),
        }

    def status_str(self) -> str:
        topk = self.model.feature_importance()[:4]
        feat = " ".join(f"{n[:8]}={w:+.2f}" for n, w in topk)
        return (
            f"[brain] kelly b={self.kelly.avg_win/max(self.kelly.avg_loss,1e-6):.2f} "
            f"win/loss={self.kelly.win_count:.0f}/{self.kelly.loss_count:.0f} | "
            f"model n={self.model.updates} brier={self.model.avg_brier():.3f} "
            f"T={self.model.temp:.2f} | top: {feat} | {self.tracker.status_str()}"
        )


__all__ = [
    "FEATURE_VERSION",
    "FEATURE_NAMES",
    "NUM_FEATURES",
    "FeatureExtractor",
    "OnlineLogReg",
    "KellySizer",
    "PerformanceTracker",
    "TradeRecord",
    "StrategyBrain",
    "safe_float",
    "clip",
    "sign",
]
