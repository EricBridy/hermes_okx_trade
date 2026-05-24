#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes v10 — execution layer

Position lifecycle: open -> monitor -> close -> settle.

Single settlement path `close_and_settle` ensures the brain sees every
trade outcome regardless of how it exited (TP, SL, trail, time, signal
decay, external).

REST calls are blocking, so we wrap them in `asyncio.to_thread` for use
inside the async main loop.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from hermes_v10_brain import StrategyBrain, TradeRecord, safe_float
from hermes_v10_okx import OKXRest

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

LEVERAGE = 6
HARD_TP_PCT = 0.010           # 1.0%（之前 1.8% 太高，大部分交易只动 0.3-0.5% 就 TIME_FLAT）
HARD_SL_PCT = 0.015           # 1.5% 保持不变
ATR_TP_MULT = 1.5             # 配套 TP 缩小
ATR_SL_MULT = 1.5             # 保持
TRAIL_ATR_MULT = 2.0
TRAIL_ACTIVATE_PCT = 0.007    # +0.7%（TP 是 1.0%，trail 在 70% 处启动）
TIME_STOP_LOSING_SEC = 600    # 10min 给信号时间发酵
TIME_STOP_FLAT_SEC = 1800
COOLDOWN_AFTER_CLOSE_SEC = 1800
MAX_CONCURRENT = 3            # 之前 2，资金允许并发 3 笔
MAX_TOTAL_EXPOSURE = 0.60     # 所有持仓 fraction 之和上限，防止过度叠加
MAX_SAME_DIRECTION = 2        # 同方向最多 2 笔（防止 3 笔全 LONG 同时爆亏）
OKX_TAKER_FEE = 0.0005
ROUND_TRIP_FEE = OKX_TAKER_FEE * 2

# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "hermes_v10_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "hermes_v10_trades.log")


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


def save_state(state: Dict[str, Any], positions: Dict[str, "Position"]) -> None:
    state["position_meta"] = {pid: p.to_meta() for pid, p in positions.items()}
    try:
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"⚠️ save_state: {e}")


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


@dataclass
class Position:
    inst_id: str
    signal_type: str           # LIQ / FR / MR
    direction: str             # LONG / SHORT
    entry_price: float
    sz: int                    # number of contracts
    notional: float
    atr_abs: float
    algo_ids: List[str] = field(default_factory=list)
    open_ts: float = field(default_factory=time.time)
    open_ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    highest_pnl_pct: float = 0.0
    lowest_pnl_pct: float = 0.0
    trail_active: bool = False
    highest_price: float = 0.0
    lowest_price: float = 1e18
    features: Dict[str, float] = field(default_factory=dict)
    p_win: float = 0.5
    ev: float = 0.0
    fraction: float = 0.0

    def to_meta(self) -> Dict[str, Any]:
        return {
            "signal_type": self.signal_type,
            "direction": self.direction,
            "open_ts": self.open_ts,
            "atr_abs": self.atr_abs,
            "p_win": self.p_win,
            "ev": self.ev,
            "fraction": self.fraction,
            "features": self.features,
        }


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """
    All order placement, monitoring, and post-close settlement.
    Methods that touch REST are async wrappers around thread-pool calls.
    """

    def __init__(self, rest: OKXRest, brain: StrategyBrain,
                 instruments: Dict[str, Dict[str, Any]],
                 state: Dict[str, Any]):
        self.rest = rest
        self.brain = brain
        self.instruments = instruments
        self.state = state
        self.positions: Dict[str, Position] = {}
        self._lock = asyncio.Lock()

    # ----- helpers around REST -----

    async def get_balance(self) -> float:
        return await asyncio.to_thread(self.rest.balance_usdt)

    async def get_position_remote(self, inst_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.rest.position, inst_id)

    async def get_realized_pnl(self, inst_id: str, since_ts_ms: int,
                               retries: int = 4, sleep: float = 0.7) -> float:
        for _ in range(retries):
            rows = await asyncio.to_thread(self.rest.positions_history, inst_id, 5)
            for r in rows:
                u = int(safe_float(r.get("uTime"), 0))
                if u >= since_ts_ms - 1000:
                    pnl = safe_float(r.get("pnl"))
                    if pnl != 0:
                        return pnl
            await asyncio.sleep(sleep)
        return 0.0

    # ----- price formatting -----

    def _fmt_price(self, inst_id: str, p: float) -> str:
        spec = self.instruments.get(inst_id, {})
        tick_str = spec.get("tickSz", "0.00001")
        tick = safe_float(tick_str, 1e-5)
        if "." in tick_str:
            decs = len(tick_str.rstrip("0").split(".")[-1])
        else:
            decs = 0
        rounded = round(p / tick) * tick
        return f"{rounded:.{decs}f}" if decs > 0 else str(int(rounded))

    # ----- open -----

    async def open_position(self, candidate: Dict[str, Any],
                            evaluation: Dict[str, Any], capital_usd: float
                            ) -> Optional[Position]:
        inst_id = candidate["instId"]
        spec = self.instruments.get(inst_id)
        if not spec:
            log(f"  ❌ {inst_id} no instrument spec")
            return None
        leverage = min(LEVERAGE, int(spec["maxLev"]))
        margin = capital_usd * 0.95
        notional_max = margin * leverage

        # Look up current price via REST ticker (single hit, not from kline)
        d = await asyncio.to_thread(
            self.rest.get, f"/api/v5/market/ticker?instId={inst_id}"
        )
        rows = d.get("data") or []
        if not rows:
            log(f"  ❌ {inst_id} ticker fetch failed")
            return None
        last = safe_float(rows[0].get("last"))
        if last <= 0:
            log(f"  ❌ {inst_id} bad last={rows[0].get('last')!r}")
            return None
        ct_val = safe_float(spec.get("ctVal"), 1.0)
        lots = int(notional_max / max(ct_val * last, 1e-9))
        if lots < 1:
            log(f"  ❌ {inst_id} lots<1 (capital=${capital_usd:.2f})")
            return None
        notional_actual = lots * ct_val * last
        fee_cost = notional_actual * ROUND_TRIP_FEE
        if notional_actual * HARD_TP_PCT < fee_cost * 1.5:
            log(f"  ❌ {inst_id} TP unprofitable after fees, skip")
            return None
        atr_abs = safe_float(candidate.get("atr_abs"))
        if atr_abs > 0:
            tp_dist = min(atr_abs * ATR_TP_MULT, last * HARD_TP_PCT)
            sl_dist = min(atr_abs * ATR_SL_MULT, last * HARD_SL_PCT)
        else:
            tp_dist = last * HARD_TP_PCT
            sl_dist = last * HARD_SL_PCT
        direction = candidate["direction"]
        if direction == "LONG":
            tp = self._fmt_price(inst_id, last + tp_dist)
            sl = self._fmt_price(inst_id, last - sl_dist)
            side, close_side = "buy", "sell"
        else:
            tp = self._fmt_price(inst_id, last - tp_dist)
            sl = self._fmt_price(inst_id, last + sl_dist)
            side, close_side = "sell", "buy"

        # Set leverage
        await asyncio.to_thread(self.rest.set_leverage, inst_id, leverage)
        # Market order
        r = await asyncio.to_thread(
            self.rest.order_market, inst_id, side, lots, False
        )
        if r.get("code") != "0":
            log(f"  ❌ open failed {inst_id}: {r.get('msg', '')}")
            return None
        await asyncio.sleep(0.5)
        pos_remote = await self.get_position_remote(inst_id)
        if not pos_remote:
            log(f"  ❌ {inst_id} fill confirm failed; emergency close")
            await asyncio.to_thread(self.rest.order_market, inst_id, close_side,
                                    lots, True)
            return None
        avg = safe_float(pos_remote.get("avgPx"))
        sz = int(abs(safe_float(pos_remote.get("pos"))))
        # Place TP+SL as a single OCO algo
        algo_ids: List[str] = []
        algo_resp = await asyncio.to_thread(
            self.rest.order_algo_tp_sl, inst_id, close_side, sz, tp, sl
        )
        if algo_resp.get("code") == "0":
            for d in algo_resp.get("data", []):
                if d.get("algoId"):
                    algo_ids.append(d["algoId"])
        if not algo_ids:
            log(f"  🚨 {inst_id} OCO algo failed: {algo_resp.get('msg', '')} → closing")
            for _ in range(3):
                await asyncio.sleep(0.8)
                await asyncio.to_thread(
                    self.rest.order_market, inst_id, close_side, sz, True
                )
                if not await self.get_position_remote(inst_id):
                    break
            return None
        log(f"  ✅ [{candidate.get('signal_type')}] {direction} {inst_id} "
            f"{sz}lots @ ${avg} TP=${tp} SL=${sl} "
            f"f={evaluation['fraction']:.0%} p={evaluation['p_win']:.2%}")
        pos = Position(
            inst_id=inst_id, signal_type=candidate.get("signal_type", "?"),
            direction=direction, entry_price=avg, sz=sz,
            notional=notional_actual, atr_abs=atr_abs,
            algo_ids=algo_ids,
            highest_price=avg if direction == "LONG" else 0.0,
            lowest_price=avg if direction == "SHORT" else 1e18,
            features=evaluation["features"], p_win=evaluation["p_win"],
            ev=evaluation["ev"], fraction=evaluation["fraction"],
        )
        async with self._lock:
            self.positions[inst_id] = pos
        save_state(self.state, self.positions)
        return pos

    # ----- monitor -----

    async def monitor_position(self, p: Position,
                               last_price: float) -> Optional[str]:
        """
        Returns one of: None / TP / SL / TRAIL / TIME_LOSS / TIME_FLAT /
                       SIGNAL_DECAY / EXTERNAL
        """
        # External-close detection
        remote = await self.get_position_remote(p.inst_id)
        if not remote:
            return "EXTERNAL"
        if last_price <= 0:
            return None
        entry = p.entry_price
        if p.direction == "LONG":
            pct = (last_price - entry) / entry * 100
            if last_price > p.highest_price:
                p.highest_price = last_price
        else:
            pct = (entry - last_price) / entry * 100
            if last_price < p.lowest_price:
                p.lowest_price = last_price
        if pct > p.highest_pnl_pct:
            p.highest_pnl_pct = pct
        if pct < p.lowest_pnl_pct:
            p.lowest_pnl_pct = pct
        elapsed = time.time() - p.open_ts

        # Hard TP
        if pct >= HARD_TP_PCT * 100:
            log(f"  🎯 {p.inst_id} HARD_TP {pct:+.2f}%")
            await self._cancel_and_close(p)
            return "TP"
        # Hard SL
        if pct <= -HARD_SL_PCT * 100:
            log(f"  🛑 {p.inst_id} HARD_SL {pct:+.2f}%")
            await self._cancel_and_close(p)
            return "SL"
        # Chandelier-like trailing
        if pct >= TRAIL_ACTIVATE_PCT * 100 or p.trail_active:
            if not p.trail_active:
                p.trail_active = True
                log(f"  🪝 {p.inst_id} trail active @ {pct:+.2f}%")
            atr = p.atr_abs if p.atr_abs > 0 else entry * 0.005
            trail_dist = atr * TRAIL_ATR_MULT
            if p.direction == "LONG":
                stop = p.highest_price - trail_dist
                if last_price <= stop:
                    log(f"  🪝 {p.inst_id} TRAIL stop @ {last_price:.6g}")
                    await self._cancel_and_close(p)
                    return "TRAIL"
            else:
                stop = p.lowest_price + trail_dist
                if last_price >= stop:
                    log(f"  🪝 {p.inst_id} TRAIL stop @ {last_price:.6g}")
                    await self._cancel_and_close(p)
                    return "TRAIL"
        # Time stops
        if elapsed > TIME_STOP_LOSING_SEC and pct < -1.0:
            log(f"  ⏰ {p.inst_id} TIME_LOSS {int(elapsed)}s pnl={pct:+.2f}%")
            await self._cancel_and_close(p)
            return "TIME_LOSS"
        if elapsed > TIME_STOP_FLAT_SEC and abs(pct) < 0.3:
            log(f"  ⏰ {p.inst_id} TIME_FLAT {int(elapsed)}s pnl={pct:+.2f}%")
            await self._cancel_and_close(p)
            return "TIME_FLAT"
        return None

    async def _cancel_and_close(self, p: Position) -> None:
        for aid in p.algo_ids:
            try:
                await asyncio.to_thread(self.rest.cancel_algo, p.inst_id, aid)
            except Exception:
                pass
        # Close at market with reduceOnly
        close_side = "sell" if p.direction == "LONG" else "buy"
        for _ in range(3):
            r = await asyncio.to_thread(
                self.rest.order_market, p.inst_id, close_side, p.sz, True
            )
            await asyncio.sleep(0.6)
            if not await self.get_position_remote(p.inst_id):
                return
            if r.get("code") == "0":
                continue

    # ----- settle (the ONE settlement path) -----

    async def close_and_settle(self, inst_id: str, exit_reason: str) -> None:
        async with self._lock:
            p = self.positions.pop(inst_id, None)
        if p is None:
            return
        # Try to fetch real PnL from positions-history; fall back gracefully
        pnl = await self.get_realized_pnl(inst_id, p.open_ts_ms)
        if pnl == 0.0:
            # estimate from last ticker price
            d = await asyncio.to_thread(
                self.rest.get, f"/api/v5/market/ticker?instId={inst_id}"
            )
            rows = d.get("data") or []
            last_px = safe_float(rows[0].get("last")) if rows else 0
            if last_px > 0:
                sgn = 1 if p.direction == "LONG" else -1
                ct_val = safe_float(self.instruments.get(inst_id, {}).get("ctVal"), 1.0)
                gross = sgn * (last_px - p.entry_price) * p.sz * ct_val
                fees = -p.notional * ROUND_TRIP_FEE
                pnl = gross + fees
                log(f"  ⚠️ {inst_id} fallback pnl estimate ${pnl:+.4f}")
        margin = p.notional / max(LEVERAGE, 1)
        pnl_pct = pnl / margin if margin > 0 else 0.0
        log(f"  📊 {inst_id} CLOSED via {exit_reason} pnl=${pnl:+.4f} "
            f"({pnl_pct:+.2%}) hi/lo={p.highest_pnl_pct:+.2f}%/{p.lowest_pnl_pct:+.2f}%")
        # Update state
        if pnl > 0:
            self.state["consecutive_losses"] = 0
        else:
            self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
        self.state["total_pnl"] = self.state.get("total_pnl", 0.0) + pnl
        self.state["trade_count"] = self.state.get("trade_count", 0) + 1
        self.state.setdefault("last_trade", {})[inst_id] = time.time()
        # Tell the brain
        d = await asyncio.to_thread(
            self.rest.get, f"/api/v5/market/ticker?instId={inst_id}"
        )
        rows = d.get("data") or []
        last_px = safe_float(rows[0].get("last")) if rows else 0
        rec = TradeRecord(
            symbol=inst_id, signal_type=p.signal_type,
            direction=p.direction, entry_price=p.entry_price,
            exit_price=last_px, pnl_usd=pnl, pnl_pct=pnl_pct,
            open_ts=p.open_ts, close_ts=time.time(),
            features=p.features, p_win_predicted=p.p_win,
            expected_value=p.ev, fraction_used=p.fraction,
            exit_reason=exit_reason,
            extra={
                "highest_pnl_pct": p.highest_pnl_pct,
                "lowest_pnl_pct": p.lowest_pnl_pct,
            },
        )
        diag = self.brain.record_trade(rec)
        log(f"  🧠 brain p_pred={rec.p_win_predicted:.2%} actual={'W' if pnl>0 else 'L'} "
            f"brier={diag['brier']:.3f} avg={diag['avg_brier']:.3f}")
        save_state(self.state, self.positions)


__all__ = [
    "Executor", "Position", "log",
    "load_state", "save_state",
    "LEVERAGE", "MAX_CONCURRENT", "MAX_TOTAL_EXPOSURE", "MAX_SAME_DIRECTION",
    "COOLDOWN_AFTER_CLOSE_SEC",
    "HARD_TP_PCT", "HARD_SL_PCT", "TRAIL_ACTIVATE_PCT",
    "STATE_FILE", "LOG_FILE", "SCRIPT_DIR",
]
