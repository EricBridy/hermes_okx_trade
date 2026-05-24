#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes v10 — main async entry point

Architecture:

  ┌──────────────────────────────────────────────────────────────┐
  │ Public WebSocket  (long-lived, auto-reconnect)               │
  │   subscribed to:                                             │
  │     - liquidation-orders (instType=SWAP)  → LiquidationSignal │
  │     - tickers (per-symbol watchlist)      → price feed       │
  └──────────────────────────────────────────────────────────────┘
                          │
                          ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ Periodic tasks                                               │
  │   - every 60s : refresh funding rates    → FundingRateSignal │
  │   - every 5min: scan top universe by 30M  → MeanReversionSig │
  │   - every 30s : monitor positions, time stops                │
  │   - every 60s : universe + ATR refresh                       │
  └──────────────────────────────────────────────────────────────┘
                          │
                          ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ When any signal emits a candidate:                           │
  │   1. Filter (cool-down, daily limit, settlement window)      │
  │   2. brain.evaluate(candidate)                               │
  │   3. If passes → Executor.open_position(...)                 │
  │   4. Position lifecycle managed by monitor loop              │
  │   5. close_and_settle(...) calls brain.record_trade(...)     │
  └──────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from hermes_v10_brain import StrategyBrain, safe_float
from hermes_v10_executor import (
    COOLDOWN_AFTER_CLOSE_SEC,
    Executor,
    HARD_SL_PCT,
    HARD_TP_PCT,
    LEVERAGE,
    MAX_CONCURRENT,
    MAX_TOTAL_EXPOSURE,
    MAX_SAME_DIRECTION,
    SCRIPT_DIR,
    TRAIL_ACTIVATE_PCT,
    load_state,
    log,
    save_state,
)
from hermes_v10_okx import OKXRest, OKXWebSocket, WS_PUBLIC
from hermes_v10_signals import (
    BreakRetestCandidate,
    BreakRetestSignal,
    FundingRateCandidate,
    FundingRateSignal,
    LiquidationCandidate,
    LiquidationSignal,
    MeanReversionCandidate,
    MeanReversionSignal,
    TrendFollowCandidate,
    TrendFollowSignal,
    atr,
    adx_di,
    classify_regime,
    REGIME_RANGE,
    REGIME_TREND,
    REGIME_NEUTRAL,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

UNIVERSE_TOP_N = 40
MIN_24H_VOL_USD = 5_000_000
MIN_PRICE = 0.0005
SKIP_SYMBOLS = {"RLS-USDT-SWAP", "BILL-USDT-SWAP"}
PRE_SETTLEMENT_BUFFER_MIN = 12   # 距下次资金费率结算 < 12 分钟时，禁开新仓
NEXT_FUNDING_TIME_TTL = 300      # nextFundingTime 缓存 5 分钟（OKX 切换周期罕见）
MAX_DAILY_TRADES = 40           # 之前 25 太严，63 笔/27h 撞上限错过极端信号
MAX_CONSECUTIVE_LOSSES = 5      # 之前 4，给 42% 胜率的策略多点容错
LOSS_PAUSE_SEC = 1800
SCAN_30M_INTERVAL = 300        # MR scan every 5 min
SCAN_FR_INTERVAL = 60          # FR poll every 60s
MONITOR_INTERVAL = 15          # position monitoring tick
UNIVERSE_REFRESH_INTERVAL = 600  # rebuild watchlist every 10 min
PRICE_STALE_AFTER = 30         # if no tick for 30s, skip the candidate
BRAIN_FILE = os.path.join(SCRIPT_DIR, "hermes_v10_brain.json")
TRADES_JSONL = os.path.join(SCRIPT_DIR, "hermes_v10_trades.jsonl")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Engine:
    def __init__(self) -> None:
        self.rest = OKXRest()
        self.brain = StrategyBrain(brain_file=BRAIN_FILE,
                                   trades_jsonl=TRADES_JSONL,
                                   min_edge=0.001)   # 收紧：必须正期望（之前 -0.002 让冷启动期开了太多负EV单）
        self.state = load_state()
        self.instruments: Dict[str, Dict[str, Any]] = {}
        self.universe: List[str] = []
        # Ticker price cache, updated from WS tickers channel
        self.last_price: Dict[str, float] = {}
        self.last_price_ts: Dict[str, float] = {}
        # ATR cache (per symbol, updated by 30M scan)
        self.atr_abs: Dict[str, float] = {}
        self.atr_pct: Dict[str, float] = {}
        # Per-symbol nextFundingTime cache (ms timestamp + fetched_at)
        # OKX symbols can have 1h/2h/4h/8h periods, possibly auto-switched,
        # so we ALWAYS check via API rather than hardcoding UTC schedule.
        self._next_funding_cache: Dict[str, Dict[str, float]] = {}
        # Daily counter
        self.daily = {"date": "", "count": 0}
        # Track whether we have started
        self._stop = asyncio.Event()
        self.executor = Executor(
            rest=self.rest, brain=self.brain,
            instruments=self.instruments, state=self.state,
        )
        # Signal generators. The liq signal must see the instruments dict
        # so it can convert contract counts to USD notional via ctVal.
        # We pass the same dict reference the engine populates in bootstrap()
        # so updates propagate without any extra plumbing.
        self.liq_signal = LiquidationSignal(self._on_liq_candidate,
                                            instruments=self.instruments)
        self.fr_signal = FundingRateSignal(self._on_fr_candidate_unused_async)
        self.mr_signal = MeanReversionSignal(self._on_mr_candidate)
        self.tf_signal = TrendFollowSignal(self._on_tf_candidate)
        self.br_signal = BreakRetestSignal(self._on_br_candidate)
        # Per-symbol regime cache (updated during scan)
        self._regime_cache: Dict[str, str] = {}
        # WS
        self.ws_public = OKXWebSocket(WS_PUBLIC, self._on_ws_message,
                                      on_log=lambda m: log(m))

    # ---------------------------------------------------------------
    # universe + bootstrap
    # ---------------------------------------------------------------

    async def bootstrap(self) -> None:
        log("=" * 64)
        log(" Hermes v10 — adaptive perpetual strategy")
        log(f" leverage={LEVERAGE}x  hard TP={HARD_TP_PCT*100:.1f}%  "
            f"hard SL={HARD_SL_PCT*100:.1f}%  trail@+{TRAIL_ACTIVATE_PCT*100:.1f}%")
        log(f" max concurrent={MAX_CONCURRENT}  max total exposure={MAX_TOTAL_EXPOSURE:.0%}  "
            f"max same direction={MAX_SAME_DIRECTION}")
        log(f" signals: B&R (Break & Retest) + FR (REST 60s)  [MR/TF/LIQ disabled]")
        log(f" universe top {UNIVERSE_TOP_N}, min24hVol=${MIN_24H_VOL_USD/1e6:.1f}M")
        log("=" * 64)
        # Instruments cache
        rows = await asyncio.to_thread(self.rest.instruments_swap)
        for inst in rows:
            iid = inst.get("instId") or ""
            self.instruments[iid] = {
                "ctVal": safe_float(inst.get("ctVal"), 1.0),
                "lotSz": safe_float(inst.get("lotSz"), 1.0),
                "minSz": safe_float(inst.get("minSz"), 1.0),
                "maxLev": safe_float(inst.get("lever"), 10.0),
                "tickSz": inst.get("tickSz", "0.00001"),
            }
        log(f"  instruments cached: {len(self.instruments)}")
        # Restore positions (if any)
        existing = await asyncio.to_thread(self.rest.positions)
        if existing:
            log(f"  📥 restoring {len(existing)} existing positions")
            saved = self.state.get("position_meta", {})
            from hermes_v10_executor import Position
            for raw in existing:
                iid = raw.get("instId")
                if not iid:
                    continue
                pos_val = safe_float(raw.get("pos"))
                if pos_val == 0:
                    continue
                meta = saved.get(iid, {})
                direction = "LONG" if pos_val > 0 else "SHORT"
                avg = safe_float(raw.get("avgPx"))
                ct_val = safe_float(self.instruments.get(iid, {}).get("ctVal"), 1.0)
                p = Position(
                    inst_id=iid,
                    signal_type=meta.get("signal_type", "?"),
                    direction=direction,
                    entry_price=avg,
                    sz=int(abs(pos_val)),
                    notional=abs(pos_val) * ct_val * avg,
                    atr_abs=safe_float(meta.get("atr_abs"), 0.0),
                    open_ts=safe_float(meta.get("open_ts"), time.time()),
                    open_ts_ms=int(safe_float(meta.get("open_ts"), time.time()) * 1000),
                    highest_price=avg if direction == "LONG" else 0.0,
                    lowest_price=avg if direction == "SHORT" else 1e18,
                    features=meta.get("features", {}),
                    p_win=safe_float(meta.get("p_win"), 0.5),
                    ev=safe_float(meta.get("ev"), 0.0),
                    fraction=safe_float(meta.get("fraction"), 0.0),
                )
                self.executor.positions[iid] = p
                log(f"    {iid} {direction} {p.sz}lots @ ${avg}")
        await self._refresh_universe()

    async def _refresh_universe(self) -> None:
        rows = await asyncio.to_thread(self.rest.tickers_swap)
        ranked: List[Dict[str, Any]] = []
        for x in rows:
            iid = x.get("instId") or ""
            if not iid.endswith("USDT-SWAP") or iid in SKIP_SYMBOLS:
                continue
            last = safe_float(x.get("last"))
            vol = safe_float(x.get("volCcy24h"))
            if last < MIN_PRICE or vol < MIN_24H_VOL_USD:
                continue
            ranked.append({"instId": iid, "last": last, "vol": vol})
        ranked.sort(key=lambda y: y["vol"], reverse=True)
        self.universe = [r["instId"] for r in ranked[:UNIVERSE_TOP_N]]
        # Seed price cache from ticker snapshot
        for r in ranked[:UNIVERSE_TOP_N]:
            self.last_price[r["instId"]] = r["last"]
            self.last_price_ts[r["instId"]] = time.time()
        # Subscribe (or re-subscribe) tickers channel
        sub = [{"channel": "tickers", "instId": iid} for iid in self.universe]
        # LIQ subscription is delegated to LiquidationSignal.subscribe_args()
        # which currently returns [] — see hermes_v10_signals.py for why.
        sub.extend(self.liq_signal.subscribe_args())
        await self.ws_public.add_subscriptions(sub)
        log(f"  universe refreshed: {len(self.universe)} symbols")

    # ---------------------------------------------------------------
    # WS handler
    # ---------------------------------------------------------------

    async def _on_ws_message(self, msg: Dict[str, Any]) -> None:
        ch = msg.get("arg", {}).get("channel")
        if ch == "tickers":
            for row in msg.get("data") or []:
                iid = row.get("instId")
                last = safe_float(row.get("last"))
                if iid and last > 0:
                    self.last_price[iid] = last
                    self.last_price_ts[iid] = time.time()
        elif ch == "liquidation-orders":
            # Currently LiquidationSignal.subscribe_args() is empty so we
            # shouldn't receive these. Kept for forward compatibility if
            # we re-enable LIQ in the future.
            await self.liq_signal.handle_ws_message(
                msg, self._price_lookup, self._atr_abs_lookup
            )
        else:
            return

    def _price_lookup(self, inst_id: str) -> Optional[float]:
        ts = self.last_price_ts.get(inst_id, 0)
        if time.time() - ts > PRICE_STALE_AFTER:
            return None
        return self.last_price.get(inst_id)

    def _atr_abs_lookup(self, inst_id: str) -> Optional[float]:
        return self.atr_abs.get(inst_id)

    # ---------------------------------------------------------------
    # Pre-trade gates
    # ---------------------------------------------------------------

    async def _is_near_settlement(self, inst_id: str) -> Optional[int]:
        """
        Per-symbol check: returns minutes-until-next-funding if < buffer,
        else None.

        Uses the symbol's `nextFundingTime` from the funding-rate API so
        it correctly handles OKX's dynamic 1h/2h/4h/8h periods (the
        period can be auto-switched by OKX when the rate hits its cap).

        Cached for NEXT_FUNDING_TIME_TTL seconds per symbol; the API
        returns the same nextFundingTime value for the entire current
        funding period anyway, so a 5-minute TTL is plenty.
        """
        now_ms = int(time.time() * 1000)
        cache = self._next_funding_cache.get(inst_id)
        if cache and (time.time() - cache["fetched_at"] < NEXT_FUNDING_TIME_TTL):
            next_t = cache["next_funding_ms"]
        else:
            row = await asyncio.to_thread(self.rest.funding_rate_full, inst_id)
            if not row:
                return None
            try:
                next_t = int(row.get("nextFundingTime") or 0)
            except (TypeError, ValueError):
                return None
            if next_t <= 0:
                return None
            self._next_funding_cache[inst_id] = {
                "fetched_at": time.time(),
                "next_funding_ms": next_t,
            }
        delta_ms = next_t - now_ms
        if 0 < delta_ms <= PRE_SETTLEMENT_BUFFER_MIN * 60 * 1000:
            return max(1, delta_ms // 60000)
        return None

    def _check_daily_limit(self) -> bool:
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        if self.daily["date"] != today:
            self.daily = {"date": today, "count": 0}
        return self.daily["count"] >= MAX_DAILY_TRADES

    async def _can_open(self, inst_id: str, candidate: Optional[Dict[str, Any]] = None
                        ) -> Optional[str]:
        """
        Returns reason string if cannot open, None if can open.
        Some "super-extreme" candidates (very high z-score on FR or LIQ) are
        allowed to bypass the daily limit, since those signals are rare and
        historically high-edge — missing them costs more than the marginal
        cost of one extra trade.
        """
        is_super_extreme = False
        if candidate:
            sig = candidate.get("signal_type")
            if sig == "FR" and abs(candidate.get("fr_abs_z", 0)) >= 3.0:
                is_super_extreme = True
            elif sig == "LIQ" and abs(candidate.get("liq_size_z", 0)) >= 4.0:
                is_super_extreme = True
        if self._check_daily_limit() and not is_super_extreme:
            return "daily limit reached"
        if self.brain.is_symbol_cold(inst_id):
            return "symbol cold"
        last_close = self.state.get("last_trade", {}).get(inst_id, 0)
        if time.time() - last_close < COOLDOWN_AFTER_CLOSE_SEC:
            return f"cooldown {int(COOLDOWN_AFTER_CLOSE_SEC - (time.time() - last_close))}s"
        mins = await self._is_near_settlement(inst_id)
        if mins is not None:
            return f"距资金费率结算 {mins} 分钟"
        if len(self.executor.positions) >= MAX_CONCURRENT:
            return "all slots taken"
        # 总暴露上限
        current_exposure = sum(p.fraction for p in self.executor.positions.values())
        if current_exposure >= MAX_TOTAL_EXPOSURE:
            return f"总暴露 {current_exposure:.0%} 已达上限 {MAX_TOTAL_EXPOSURE:.0%}"
        # 同方向集中度限制（防止 3 笔全 LONG 同时爆亏）
        if candidate:
            new_dir = candidate.get("direction", "")
            same_dir_count = sum(1 for p in self.executor.positions.values()
                                 if p.direction == new_dir)
            if same_dir_count >= MAX_SAME_DIRECTION:
                return f"同方向({new_dir})已有 {same_dir_count} 笔，上限 {MAX_SAME_DIRECTION}"
        if inst_id in self.executor.positions:
            return "already open"
        if self.state.get("pause_until"):
            until = datetime.fromisoformat(self.state["pause_until"])
            if datetime.now() < until:
                return f"paused until {self.state['pause_until']}"
            else:
                # Auto-clear expired pause so the displayed state stays clean
                self.state["pause_until"] = None
                self.state["consecutive_losses"] = 0
                save_state(self.state, self.executor.positions)
        return None

    # ---------------------------------------------------------------
    # Candidate handlers
    # ---------------------------------------------------------------

    async def _process_candidate(self, candidate: Dict[str, Any]) -> None:
        iid = candidate["instId"]
        why = await self._can_open(iid, candidate)
        if why:
            log(f"  ⏭️  skip {iid} ({candidate.get('signal_type')}): {why}")
            return
        # Enrich with microstructure (orderbook + 5M flows)
        await self._enrich_candidate(candidate)
        evaluation = self.brain.evaluate(candidate)
        log(f"  🎯 [{candidate.get('signal_type')}] {iid} {candidate['direction']}  "
            f"{evaluation['reason']}")
        if not evaluation["passes"]:
            return
        balance = await self.executor.get_balance()
        # 限制本笔 fraction 不超过剩余暴露空间
        current_exposure = sum(p.fraction for p in self.executor.positions.values())
        headroom = max(0.0, MAX_TOTAL_EXPOSURE - current_exposure)
        eff_fraction = min(evaluation["fraction"], headroom)
        if eff_fraction <= 0:
            log(f"     skip {iid}: 暴露已满 {current_exposure:.0%}/{MAX_TOTAL_EXPOSURE:.0%}")
            return
        if eff_fraction < evaluation["fraction"]:
            log(f"     {iid}: fraction 被压缩 {evaluation['fraction']:.0%} → {eff_fraction:.0%} (暴露上限)")
            evaluation["fraction"] = eff_fraction
        cap = balance * eff_fraction
        if cap < 1.0:
            log(f"     skip {iid}: cap ${cap:.2f} too small")
            return
        opened = await self.executor.open_position(candidate, evaluation, cap)
        if opened:
            self.daily["count"] += 1
            self.state.setdefault("last_trade", {})[iid] = time.time()

    async def _enrich_candidate(self, c: Dict[str, Any]) -> None:
        iid = c["instId"]
        # Orderbook
        try:
            book = await asyncio.to_thread(self.rest.orderbook, iid, 20)
            if book:
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if bids and asks:
                    bp = safe_float(bids[0][0])
                    ap = safe_float(asks[0][0])
                    bs0 = safe_float(bids[0][1])
                    as0 = safe_float(asks[0][1])
                    if bp > 0 and ap > 0 and (bs0 + as0) > 0:
                        c["microprice"] = (bp * as0 + ap * bs0) / (bs0 + as0)
                        c["mid_price"] = (bp + ap) / 2.0
                    c["l2_bid_size"] = sum(safe_float(b[1]) for b in bids[:20])
                    c["l2_ask_size"] = sum(safe_float(a[1]) for a in asks[:20])
        except Exception:
            pass
        # ATR % we already have from the universe scan; ATR abs is inferred
        if "atr_pct" not in c:
            c["atr_pct"] = self.atr_pct.get(iid, 1.0)
        if "atr_abs" not in c:
            c["atr_abs"] = self.atr_abs.get(iid, 0.0)
        # Taker buy/sell from recent 5M candle (column index 8 = volCcyQuote, 9 = takerBuyVolCcy)
        try:
            cs = await asyncio.to_thread(self.rest.candles, iid, "5m", 6)
            tot_buy = 0.0
            tot_vol = 0.0
            for row in cs:
                vol = safe_float(row[5]) if len(row) > 5 else 0.0
                # Some columns vary; we approximate via close vs open for direction
                op = safe_float(row[1])
                cl = safe_float(row[4])
                if vol <= 0:
                    continue
                tot_vol += vol
                if cl >= op:
                    tot_buy += vol
            c["taker_buy_5m"] = tot_buy
            c["taker_sell_5m"] = max(tot_vol - tot_buy, 0.0)
        except Exception:
            pass

    async def _on_liq_candidate(self, lc: LiquidationCandidate) -> None:
        c = {
            "instId": lc.inst_id,
            "signal_type": "LIQ",
            "direction": lc.direction,
            "liq_long_usd": lc.liq_long_usd,
            "liq_short_usd": lc.liq_short_usd,
            "liq_size_z": lc.liq_size_z,
            "price_drop_atr": lc.price_drop_atr,
            "atr_pct": self.atr_pct.get(lc.inst_id, 1.0),
            "atr_abs": self.atr_abs.get(lc.inst_id, 0.0),
        }
        log(f"  ⚡ LIQ cascade {lc.inst_id} {lc.direction} z={lc.liq_size_z:.1f} "
            f"long=${lc.liq_long_usd:,.0f} short=${lc.liq_short_usd:,.0f}")
        await self._process_candidate(c)

    async def _on_fr_candidate_unused_async(self, fc: Any) -> None:
        # Not used: FR signal is polled and processed in tick loop
        return

    async def _on_mr_candidate(self, mc: MeanReversionCandidate) -> None:
        c = {
            "instId": mc.inst_id,
            "signal_type": "MR",
            "direction": mc.direction,
            "mr_zscore": mc.mr_zscore,
            "bb_position": mc.bb_position,
            "atr_pct": mc.atr_pct,
            "atr_abs": self.atr_abs.get(mc.inst_id, 0.0),
        }
        log(f"  📐 MR {mc.inst_id} {mc.direction} z={mc.mr_zscore:+.2f} "
            f"bb={mc.bb_position:+.2f} atr%={mc.atr_pct:.2f} ADX={mc.adx:.0f} [{mc.regime}]")
        await self._process_candidate(c)

    async def _on_tf_candidate(self, tc: TrendFollowCandidate) -> None:
        return  # Disabled, replaced by B&R

    async def _on_br_candidate(self, bc: BreakRetestCandidate) -> None:
        c = {
            "instId": bc.inst_id,
            "signal_type": "MR",  # Reuse brain features (direction-aware)
            "direction": bc.direction,
            "mr_zscore": 0.0,
            "bb_position": 0.0,
            "atr_pct": bc.atr_pct,
            "atr_abs": self.atr_abs.get(bc.inst_id, 0.0),
        }
        log(f"  🔀 B&R {bc.inst_id} {bc.direction} level=${bc.level:.6g} "
            f"confirm={bc.confirmation} ADX={bc.adx:.0f} [{bc.regime}]")
        await self._process_candidate(c)

    # ---------------------------------------------------------------
    # Periodic loops
    # ---------------------------------------------------------------

    async def _loop_funding_rates(self) -> None:
        while not self._stop.is_set():
            try:
                # Sample top-N to keep request count reasonable. We use the
                # full payload so we can populate _next_funding_cache for
                # free, avoiding a second API call inside _is_near_settlement.
                for iid in list(self.universe)[:30]:
                    row = await asyncio.to_thread(self.rest.funding_rate_full, iid)
                    if not row:
                        continue
                    try:
                        next_t = int(row.get("nextFundingTime") or 0)
                        if next_t > 0:
                            self._next_funding_cache[iid] = {
                                "fetched_at": time.time(),
                                "next_funding_ms": next_t,
                            }
                        fr = float(row.get("fundingRate") or 0)
                    except (TypeError, ValueError):
                        continue
                    cand = self.fr_signal.update(iid, fr,
                                                regime=self._regime_cache.get(iid, REGIME_NEUTRAL))
                    if cand:
                        c = {
                            "instId": cand.inst_id,
                            "signal_type": "FR",
                            "direction": cand.direction,
                            "fr": cand.fr,
                            "fr_abs_z": cand.fr_abs_z,
                            "atr_pct": self.atr_pct.get(iid, 1.0),
                            "atr_abs": self.atr_abs.get(iid, 0.0),
                        }
                        log(f"  💸 FR extreme {iid} {cand.direction} "
                            f"FR={cand.fr*100:+.4f}% z={cand.fr_abs_z:.1f}")
                        await self._process_candidate(c)
            except Exception as e:
                log(f"  fr loop error: {e}")
            await asyncio.sleep(SCAN_FR_INTERVAL)

    async def _loop_mr_scan(self) -> None:
        # First scan delayed slightly to let universe populate
        await asyncio.sleep(20)
        while not self._stop.is_set():
            try:
                for iid in list(self.universe)[:30]:
                    cs = await asyncio.to_thread(self.rest.candles, iid, "30m", 50)
                    if not cs or len(cs) < 25:
                        continue
                    rows = list(reversed(cs))
                    closes = [safe_float(r[4]) for r in rows]
                    highs = [safe_float(r[2]) for r in rows]
                    lows = [safe_float(r[3]) for r in rows]
                    a = atr(highs, lows, closes, 14)
                    if a is not None and closes[-1] > 0:
                        self.atr_abs[iid] = a
                        self.atr_pct[iid] = a / closes[-1] * 100.0
                    # Compute regime for this symbol (used by FR signal too)
                    adx_result = adx_di(highs, lows, closes, 14)
                    if adx_result:
                        adx_val = adx_result[0]
                        self._regime_cache[iid] = classify_regime(adx_val)
                    # Try both MR and TF — each has its own regime gate
                    await self.mr_signal.evaluate(iid, cs)
                    await self.tf_signal.evaluate(iid, cs)
                    # Break & Retest (primary signal, works in all regimes)
                    await self.br_signal.evaluate(iid, cs)
                    await asyncio.sleep(0.05)  # rate-limit-friendly
            except Exception as e:
                log(f"  scan error: {e}")
            await asyncio.sleep(SCAN_30M_INTERVAL)

    async def _loop_monitor_positions(self) -> None:
        while not self._stop.is_set():
            try:
                for iid in list(self.executor.positions.keys()):
                    p = self.executor.positions.get(iid)
                    if not p:
                        continue
                    last = self._price_lookup(iid)
                    if last is None:
                        # Fallback: REST ticker for this one symbol
                        d = await asyncio.to_thread(
                            self.rest.get, f"/api/v5/market/ticker?instId={iid}"
                        )
                        rows = d.get("data") or []
                        if rows:
                            last = safe_float(rows[0].get("last"))
                            if last > 0:
                                self.last_price[iid] = last
                                self.last_price_ts[iid] = time.time()
                    if not last:
                        continue
                    reason = await self.executor.monitor_position(p, last)
                    if reason:
                        await self.executor.close_and_settle(iid, reason)
                        # consecutive loss check
                        cl = self.state.get("consecutive_losses", 0)
                        if cl >= MAX_CONSECUTIVE_LOSSES:
                            until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
                            self.state["pause_until"] = until.isoformat()
                            log(f"⏸️ {cl} consecutive losses, pausing for {LOSS_PAUSE_SEC//60}m")
                            save_state(self.state, self.executor.positions)
            except Exception as e:
                log(f"  monitor error: {e}")
            await asyncio.sleep(MONITOR_INTERVAL)

    async def _loop_universe_refresh(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(UNIVERSE_REFRESH_INTERVAL)
            try:
                await self._refresh_universe()
            except Exception as e:
                log(f"  universe refresh error: {e}")

    async def _loop_status(self) -> None:
        await asyncio.sleep(60)
        while not self._stop.is_set():
            try:
                bal = await self.executor.get_balance()
                log(f"  📡 status bal=${bal:.2f}  pos={len(self.executor.positions)}/{MAX_CONCURRENT}  "
                    f"daily={self.daily['count']}/{MAX_DAILY_TRADES}  "
                    f"univ={len(self.universe)}")
                log(f"  {self.brain.status_str()}")
                self.brain.save()
            except Exception as e:
                log(f"  status error: {e}")
            await asyncio.sleep(120)

    # ---------------------------------------------------------------
    # Run
    # ---------------------------------------------------------------

    async def run(self) -> None:
        await self.bootstrap()
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            try:
                s = getattr(signal, sig_name)
                loop.add_signal_handler(s, self._stop.set)
            except (NotImplementedError, AttributeError):
                pass
        ws_task = asyncio.create_task(self.ws_public.run())
        tasks = [
            asyncio.create_task(self._loop_funding_rates()),
            asyncio.create_task(self._loop_mr_scan()),
            asyncio.create_task(self._loop_monitor_positions()),
            asyncio.create_task(self._loop_universe_refresh()),
            asyncio.create_task(self._loop_status()),
        ]
        try:
            await self._stop.wait()
        finally:
            log("🛑 shutdown requested")
            for t in tasks:
                t.cancel()
            await self.ws_public.stop()
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, ws_task, return_exceptions=True)
            self.brain.save(force=True)
            save_state(self.state, self.executor.positions)
            log("bye")


def main() -> None:
    asyncio.run(Engine().run())


if __name__ == "__main__":
    main()
