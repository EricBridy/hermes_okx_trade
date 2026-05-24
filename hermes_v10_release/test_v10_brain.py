#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for hermes_v10_brain. Run anywhere with stdlib only."""
from __future__ import annotations

import os
import random
import time

from hermes_v10_brain import (
    FEATURE_NAMES,
    NUM_FEATURES,
    FeatureExtractor,
    KellySizer,
    OnlineLogReg,
    PerformanceTracker,
    StrategyBrain,
    TradeRecord,
)


def section(title):
    print("\n" + "=" * 70 + f"\n== {title}\n" + "=" * 70)


def t_safe_features():
    section("FeatureExtractor: empty / partial / full")
    f = FeatureExtractor.build({})
    assert len(f) == NUM_FEATURES
    assert all(isinstance(v, float) for v in f.values())

    # All signal types
    for sig in ("LIQ", "FR", "MR", "BR"):
        f = FeatureExtractor.build({"signal_type": sig, "direction": "LONG"})
        assert f[f"is_{sig.lower()}"] == 1.0
        for other in ("LIQ", "FR", "MR", "BR"):
            if other != sig:
                assert f[f"is_{other.lower()}"] == 0.0

    # LIQ candidate (long-liquidation cascade => we LONG)
    liq = {
        "signal_type": "LIQ", "direction": "LONG",
        "atr_pct": 1.5,
        "liq_size_z": 3.5, "liq_long_usd": 800_000, "liq_short_usd": 100_000,
        "price_drop_atr": 2.0,    # price dropped 2 ATRs
        "microprice": 100.5, "mid_price": 100.0,
        "l2_bid_size": 2000, "l2_ask_size": 800,
        "taker_buy_5m": 600_000, "taker_sell_5m": 400_000,
    }
    fl = FeatureExtractor.build(liq)
    assert fl["is_liq"] == 1.0 and fl["dir_long"] == 1.0
    assert fl["liq_imbalance"] > 0.5         # cascade matches our fade
    assert fl["price_drop_atr"] < 0          # negative because d=+1, drop>0
    assert fl["microprice_bias"] > 0
    assert fl["l2_imbalance"] > 0
    print(f"  LIQ-LONG fade: imb={fl['liq_imbalance']:+.2f} "
          f"drop={fl['price_drop_atr']:+.2f} mp={fl['microprice_bias']:+.2f}")

    # MR candidate (price above mean, z=+2.5 => we SHORT)
    mr = {
        "signal_type": "MR", "direction": "SHORT",
        "mr_zscore": 2.5, "bb_position": 0.9,
        "atr_pct": 0.8,
    }
    fm = FeatureExtractor.build(mr)
    assert fm["is_mr"] == 1.0 and fm["dir_long"] == -1.0
    assert fm["mr_zscore"] > 0    # -d * z = -(-1)*2.5 = +2.5
    assert fm["bb_position"] > 0  # -d * 0.9 = +0.9
    print(f"  MR-SHORT fade: zs={fm['mr_zscore']:+.2f} bb={fm['bb_position']:+.2f}")

    br = {
        "signal_type": "BR", "direction": "LONG",
        "breakout_bar_idx": 2,
        "retest_distance_pct": 0.2,
        "confirmation": "pin_bar",
        "atr_pct": 1.1,
    }
    fb = FeatureExtractor.build(br)
    assert fb["is_br"] == 1.0
    assert fb["br_breakout_age"] > 0.7
    assert fb["br_retest_tight"] > 0.7
    assert fb["br_pin_bar"] == 1.0
    print(f"  BR-LONG retest: age={fb['br_breakout_age']:.2f} tight={fb['br_retest_tight']:.2f}")

    print("  OK")


def t_logreg_learns():
    section("OnlineLogReg: convergence on synthetic data")
    random.seed(42)
    m = OnlineLogReg(eta=0.1, lam=1e-4, eta_decay=1.0)
    correct = 0
    n = 600
    for i in range(n):
        x = [random.gauss(0, 1) for _ in range(NUM_FEATURES)]
        true_p = 0.85 if x[0] > 0 else 0.15
        y = 1 if random.random() < true_p else 0
        if i >= 100:
            pred = 1 if m.predict_proba(x) > 0.5 else 0
            if pred == y:
                correct += 1
        m.update(x, y)
    acc = correct / max(n - 100, 1)
    print(f"  acc={acc:.3f}, top3={m.feature_importance()[:3]}")
    assert acc > 0.65
    assert m.feature_importance()[0][0] == FEATURE_NAMES[0]
    print("  OK")


def t_kelly():
    section("KellySizer")
    k = KellySizer()
    assert k.fraction(0.3) == 0.0
    assert 0.10 <= k.fraction(0.7) <= 0.40
    for _ in range(10):
        k.record(0.012)
    for _ in range(5):
        k.record(-0.006)
    print(f"  avg_win={k.avg_win:.4f} avg_loss={k.avg_loss:.4f}")
    print(f"  f@0.65={k.fraction(0.65):.2%} f@0.45={k.fraction(0.45):.2%}")
    print("  OK")


def t_tracker():
    section("PerformanceTracker cooldown + per-signal stats")
    t = PerformanceTracker()
    sym = "TEST-USDT-SWAP"
    t.record(sym, "LIQ", -1.0)
    t.record(sym, "LIQ", -1.0)
    assert not t.is_cold(sym)
    t.record(sym, "LIQ", -1.0)
    assert t.is_cold(sym)
    t.record("OTHER", "FR", 0.5)
    t.record("OTHER2", "BR", 0.2)
    print(f"  {t.status_str()}")
    print("  OK")


def t_brain_end_to_end():
    section("StrategyBrain end-to-end")
    bf = os.path.abspath("brain_test_tmp.json")
    tl = os.path.abspath("trades_test_tmp.jsonl")
    try:
        b = StrategyBrain(brain_file=bf, trades_jsonl=tl, min_edge=0.0001)

    # Synthetic rule: LIQ cascade with strong reversal signals = win
        random.seed(11)
        for _ in range(60):
            cand = {
                "signal_type": "LIQ",
            "direction": random.choice(["LONG", "SHORT"]),
            "atr_pct": 1.0,
            "liq_size_z": random.uniform(0, 5),
                "liq_long_usd": random.uniform(0, 1_000_000),
                "liq_short_usd": random.uniform(0, 1_000_000),
                "price_drop_atr": random.uniform(-3, 3),
                "microprice": 100 + random.uniform(-0.1, 0.1),
                "mid_price": 100.0,
                "l2_bid_size": random.uniform(500, 2500),
                "l2_ask_size": random.uniform(500, 2500),
            "taker_buy_5m": random.uniform(100_000, 1_000_000),
            "taker_sell_5m": random.uniform(100_000, 1_000_000),
        }
            ev = b.evaluate(cand)
            f = ev["features"]
            # Simulate: win when both liq_imbalance>0 AND microprice_bias>0
            wins = (f["liq_imbalance"] > 0.2) and (f["microprice_bias"] > 0.2)
            pnl_pct = 0.012 if (wins and random.random() < 0.85) else -0.006
            rec = TradeRecord(
                symbol="SYNTH-USDT-SWAP", signal_type="LIQ",
                direction=cand["direction"], entry_price=100.0,
                exit_price=100.0 * (1 + pnl_pct),
                pnl_usd=10 * pnl_pct, pnl_pct=pnl_pct,
                open_ts=time.time() - 100, close_ts=time.time(),
                features=f,
                p_win_predicted=ev["p_win"],
                expected_value=ev["ev"],
                fraction_used=ev["fraction"],
                exit_reason="TEST",
            )
            b.record_trade(rec)

        wd = dict(zip(FEATURE_NAMES, b.model.w))
        print(f"  liq_imbalance weight = {wd['liq_imbalance']:+.3f}")
        print(f"  microprice_bias weight = {wd['microprice_bias']:+.3f}")
        print(f"  status: {b.status_str()}")
        assert wd["liq_imbalance"] > 0
        assert wd["microprice_bias"] > 0

        br_ev = b.evaluate({
            "signal_type": "BR",
            "direction": "LONG",
            "atr_pct": 1.0,
            "breakout_bar_idx": 2,
            "retest_distance_pct": 0.2,
            "confirmation": "engulfing",
            "br_level": 100.0,
            "br_adx": 28.0,
            "br_regime": "TREND",
            "microprice": 100.1,
            "mid_price": 100.0,
            "l2_bid_size": 2000,
            "l2_ask_size": 1000,
            "taker_buy_5m": 500000,
            "taker_sell_5m": 250000,
        })
        assert br_ev["features"]["is_br"] == 1.0

        # Round-trip persistence
        b.save(force=True)
        b2 = StrategyBrain(brain_file=bf, trades_jsonl=tl)
        for w1, w2 in zip(b.model.w, b2.model.w):
            assert abs(w1 - w2) < 1e-12
        print("  reload OK")
    finally:
        for path in (bf, tl):
            try:
                os.remove(path)
            except Exception:
                pass


def main():
    t_safe_features()
    t_logreg_learns()
    t_kelly()
    t_tracker()
    t_brain_end_to_end()
    print("\n" + "=" * 70 + "\nALL v10 BRAIN TESTS PASSED\n" + "=" * 70)


if __name__ == "__main__":
    main()
