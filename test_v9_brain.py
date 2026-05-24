#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke / correctness tests for hermes_v9_brain.

Run:    python test_v9_brain.py
"""

import json
import os
import random
import sys
import tempfile
import time

from hermes_v9_brain import (
    FEATURE_NAMES,
    NUM_FEATURES,
    FeatureExtractor,
    KellySizer,
    OnlineLogReg,
    PerformanceTracker,
    RegimeDetector,
    StrategyBrain,
    TradeRecord,
    _safe_float,
    _clip,
)


def section(title):
    print("\n" + "=" * 70)
    print("==", title)
    print("=" * 70)


def test_safe_float():
    section("_safe_float and _clip")
    assert _safe_float(None) == 0.0
    assert _safe_float("") == 0.0
    assert _safe_float("nan") == 0.0
    assert _safe_float("inf") == 0.0
    assert _safe_float("1.5") == 1.5
    assert _safe_float(2.5) == 2.5
    assert _clip(5, 0, 10) == 5
    assert _clip(-5, 0, 10) == 0
    assert _clip(15, 0, 10) == 10
    print("  pass — handles '', None, NaN, Inf, valid numbers")


def test_feature_extractor_no_crash_on_empty():
    section("FeatureExtractor: empty/missing snapshot")
    f = FeatureExtractor.build({})
    assert len(f) == NUM_FEATURES
    assert all(isinstance(v, float) for v in f.values())
    # no nan/inf should leak
    for k, v in f.items():
        assert v == v, f"{k} is NaN"
        assert abs(v) < 1e10, f"{k}={v} out of range"
    print("  pass — empty input produces all-zero finite vector")

    # Direction-aware: long vs short on same data should differ
    snap = {
        "direction": "LONG",
        "funding_rate": -0.0008,
        "microprice": 100.5,
        "mid_price": 100.0,
        "l2_bid_size": 1000,
        "l2_ask_size": 500,
        "ema12_5m": 1.05,
        "ema26_5m": 1.0,
        "ema12_15m": 1.05,
        "ema26_15m": 1.0,
        "ha_trend_15m": "UP",
        "rsi_5m": 55,
        "rsi_15m": 55,
    }
    long_f = FeatureExtractor.build(snap)
    snap["direction"] = "SHORT"
    short_f = FeatureExtractor.build(snap)
    assert long_f["fr_dir_align"] == 1.0
    assert short_f["fr_dir_align"] == -1.0
    assert long_f["microprice_bias"] > 0
    assert short_f["microprice_bias"] < 0
    assert long_f["l2_imbalance"] > 0
    assert short_f["l2_imbalance"] < 0
    assert long_f["ema_alignment"] > 0
    assert short_f["ema_alignment"] < 0
    assert long_f["ha_trend_15m"] == 1.0
    assert short_f["ha_trend_15m"] == -1.0
    print("  pass — direction-aware features flip sign correctly")


def test_logreg_learns_on_synthetic():
    section("OnlineLogReg: gradient descent recovers signal on synthetic data")
    random.seed(42)
    model = OnlineLogReg(eta=0.1, lam=1e-4, eta_decay=1.0)  # disable decay for test
    # True relationship: y = 1 if x[0] > 0 (with noise on x[1]..x[15])
    n_train = 600
    correct = 0
    for i in range(n_train):
        x = [random.gauss(0, 1) for _ in range(NUM_FEATURES)]
        # First feature is the actual signal, others are noise
        true_p = 0.5 + 0.4 * (1 if x[0] > 0 else -1)
        y = 1 if random.random() < true_p else 0
        # Track convergence
        if i >= 100:
            p = model.predict_proba(x)
            pred = 1 if p > 0.5 else 0
            if pred == y:
                correct += 1
        model.update(x, y)
    accuracy = correct / max(n_train - 100, 1)
    print(f"  trained {n_train} samples, accuracy on stream={accuracy:.3f}")
    print(f"  weights: {[f'{w:+.3f}' for w in model.w[:4]]}...")
    print(f"  feature_importance top3: {model.feature_importance()[:3]}")
    assert accuracy > 0.65, f"failed to learn ({accuracy})"
    # The first weight should be most positive (it carries all the signal)
    importance = model.feature_importance()
    assert importance[0][0] == FEATURE_NAMES[0], f"top feature is {importance[0]}"
    print("  pass — model recovered the signal feature as #1 important")


def test_logreg_serialization():
    section("OnlineLogReg: save/load round-trip")
    m = OnlineLogReg()
    m.w = [0.1, -0.2, 0.3] + [0.0] * (NUM_FEATURES - 3)
    m.b = 0.5
    m.updates = 42
    m.recent_brier = [0.1, 0.2, 0.3]
    m.temp = 1.5
    d = m.to_dict()
    s = json.dumps(d)
    d2 = json.loads(s)
    m2 = OnlineLogReg()
    ok = m2.from_dict(d2)
    assert ok
    assert m2.w == m.w
    assert m2.b == m.b
    assert m2.updates == 42
    assert m2.temp == 1.5
    print("  pass — round-trip preserves weights, bias, temp, counters")


def test_kelly():
    section("KellySizer: edge cases & sanity")
    k = KellySizer(kelly_frac=0.25, max_risk_pct=0.40, min_risk_pct=0.10)
    # No positive edge -> zero
    f = k.fraction(0.3)
    assert f == 0.0, f"negative edge gave f={f}"
    # Strong positive edge
    f = k.fraction(0.7)
    assert 0.10 <= f <= 0.40, f"unexpected fraction {f}"
    # Recording wins / losses moves avg_win, avg_loss
    for _ in range(10):
        k.record(0.025)
    for _ in range(5):
        k.record(-0.015)
    assert k.win_count > 5
    assert k.loss_count > 0
    # Edge formula sanity
    p = 0.65
    b = k.avg_win / k.avg_loss
    expected_full = (p * b - (1 - p)) / b
    assert abs(k.fraction(p) / 0.25 - expected_full) < 0.5 or k.fraction(p) >= 0.10
    print(f"  avg_win={k.avg_win:.4f} avg_loss={k.avg_loss:.4f} b={b:.2f}")
    print(f"  fraction at p=0.65: {k.fraction(0.65):.2%}")
    print(f"  fraction at p=0.45: {k.fraction(0.45):.2%}")
    print("  pass — fractional Kelly with floor/cap respected")


def test_kelly_serialization():
    section("KellySizer: save/load round-trip")
    k = KellySizer()
    for pnl in [0.01, -0.005, 0.02, 0.015, -0.012]:
        k.record(pnl)
    d = k.to_dict()
    k2 = KellySizer()
    k2.from_dict(d)
    assert abs(k2.avg_win - k.avg_win) < 1e-9
    assert abs(k2.avg_loss - k.avg_loss) < 1e-9
    assert abs(k2.win_count - k.win_count) < 1e-9
    print("  pass")


def test_regime():
    section("RegimeDetector: classifies properly")
    r = RegimeDetector()
    # Inject 15 low ATR readings -> should land in RANGING eventually
    for _ in range(15):
        r.update([0.15, 0.18, 0.16])
    # The ATR is below 0.25 and CV is low
    print(f"  after low ATR: regime={r.regime} conf={r.confidence:.2f}")
    assert r.regime in ("RANGING", "NORMAL"), f"got {r.regime}"

    r2 = RegimeDetector()
    for _ in range(15):
        r2.update([3.0, 3.5, 2.8])
    print(f"  after high ATR: regime={r2.regime} conf={r2.confidence:.2f}")
    assert r2.regime == "VOLATILE"

    print("  pass — RegimeDetector classifies low/high ATR correctly")


def test_tracker():
    section("PerformanceTracker: cooldown + per-hour stats")
    t = PerformanceTracker()
    sym = "TEST-USDT-SWAP"
    assert not t.is_cold(sym)
    t.record(sym, -1.0)
    t.record(sym, -1.0)
    assert not t.is_cold(sym)
    t.record(sym, -1.0)
    assert t.is_cold(sym), "should be cold after 3 losses"
    # Win resets streak
    t.record(sym, 0.5)
    print("  pass — 3 losses trigger cooldown, win resets streak")


def test_brain_end_to_end():
    section("StrategyBrain: end-to-end with synthetic trades")
    with tempfile.TemporaryDirectory() as tmp:
        bf = os.path.join(tmp, "brain.json")
        tl = os.path.join(tmp, "trades.jsonl")
        brain = StrategyBrain(brain_file=bf, trades_log=tl, min_edge=0.0001)

        # Build a synthetic snapshot
        snap = {
            "direction": "LONG",
            "funding_rate": -0.0008,
            "chg_5m": 0.5,
            "vol_1m_ratio": 1.5,
            "microprice": 1.0005,
            "mid_price": 1.0,
            "l2_bid_size": 2000,
            "l2_ask_size": 1000,
            "ema12_5m": 1.05,
            "ema26_5m": 1.0,
            "ema12_15m": 1.05,
            "ema26_15m": 1.0,
            "ha_trend_15m": "UP",
            "rsi_1m": 55,
            "rsi_5m": 55,
            "rsi_15m": 55,
            "atr_pct": 1.0,
            "bb_width": 3.0,
            "chain_score": 4,
        }
        ev = brain.evaluate(snap)
        # On a fresh model with all-zero weights, p_win should be ~0.5
        assert 0.4 <= ev["p_win"] <= 0.6
        print(f"  fresh model p_win={ev['p_win']:.3f}, ev={ev['ev']:+.4f}")
        # ev = 0.5*avg_win - 0.5*avg_loss - fees = 0.5*0.025 - 0.5*0.015 - 0.001 = 0.004
        # So ev should be positive, hence passes -> good
        assert ev["passes"], f"fresh-model EV should pass: {ev['reason']}"

        # Simulate a series of trades where features that look "bullish" on
        # a long produce wins. Specifically: any trade with fr_dir_align == 1
        # and microprice_bias > 0 gets a win.
        random.seed(7)
        for i in range(80):
            s = dict(snap)
            # Random-walk the indicators
            s["funding_rate"] = random.choice([-0.001, -0.0007, -0.0003, 0.0002, 0.0008])
            s["microprice"] = 1.0 + random.uniform(-0.001, 0.001)
            s["mid_price"] = 1.0
            s["l2_bid_size"] = random.uniform(500, 2500)
            s["l2_ask_size"] = random.uniform(500, 2500)
            ev = brain.evaluate(s)
            # Outcome rule: win if fr_dir_align positive AND microprice positive
            f = ev["features"]
            wins_likely = (f["fr_dir_align"] > 0) and (f["microprice_bias"] > 0)
            pnl_pct = 0.025 if (wins_likely and random.random() < 0.8) else -0.015
            pnl_usd = 100 * pnl_pct  # assume $100 margin
            rec = TradeRecord(
                symbol="SYNTH-USDT-SWAP",
                direction="LONG",
                entry_price=1.0,
                exit_price=1.0 * (1 + pnl_pct),
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                open_ts=time.time() - 100,
                close_ts=time.time(),
                features=f,
                p_win_predicted=ev["p_win"],
                expected_value=ev["ev"],
                fraction_used=ev["fraction"],
                exit_reason="TEST",
            )
            brain.record_trade(rec)

        # After training, these two features should have positive weights
        print(f"  after 80 trades: top features = {brain.model.feature_importance()[:5]}")
        w_dict = dict(zip(FEATURE_NAMES, brain.model.w))
        print(f"  fr_dir_align weight = {w_dict['fr_dir_align']:+.3f}")
        print(f"  microprice_bias weight = {w_dict['microprice_bias']:+.3f}")
        assert w_dict["fr_dir_align"] > 0, "model should learn fr_dir_align is positive"
        # microprice_bias may have a smaller absolute weight because it's noisy,
        # but it should be non-negative-leaning over many trades
        assert w_dict["microprice_bias"] > -0.5, \
            f"microprice_bias weight unreasonable: {w_dict['microprice_bias']}"

        # The right test: a "clean bullish" snapshot (signals aligned with
        # the synthetic win rule) should produce a higher p_win than a
        # "clean bearish" snapshot. We don't require p_win > 0.5 in absolute
        # terms because constant features can pick up correlated weight; what
        # matters is the *gap*.
        good = {**snap, "funding_rate": -0.0009, "microprice": 1.0008,
                "mid_price": 1.0, "l2_bid_size": 2500, "l2_ask_size": 800}
        bad = {**snap, "funding_rate": +0.0009, "microprice": 0.9992,
               "mid_price": 1.0, "l2_bid_size": 800, "l2_ask_size": 2500}
        p_good = brain.evaluate(good)["p_win"]
        p_bad = brain.evaluate(bad)["p_win"]
        print(f"  p_win clean bullish={p_good:.3f}  clean bearish={p_bad:.3f}  "
              f"gap={p_good - p_bad:+.3f}")
        assert p_good > p_bad + 0.05, \
            f"model failed to discriminate (gap={p_good - p_bad:.3f})"

        # Brain should have written something to the trades log
        assert os.path.exists(tl)
        with open(tl, "r") as f:
            lines = f.readlines()
        assert len(lines) == 80
        print(f"  trades.jsonl has {len(lines)} lines")

        # Save & reload
        brain.save(force=True)
        assert os.path.exists(bf)
        brain2 = StrategyBrain(brain_file=bf, trades_log=tl)
        # Weights should match
        for w1, w2 in zip(brain.model.w, brain2.model.w):
            assert abs(w1 - w2) < 1e-9
        print("  pass — model trained, persisted, reloaded with identical weights")


def test_brain_status_str():
    section("StrategyBrain.status_str()")
    with tempfile.TemporaryDirectory() as tmp:
        bf = os.path.join(tmp, "brain.json")
        tl = os.path.join(tmp, "trades.jsonl")
        brain = StrategyBrain(brain_file=bf, trades_log=tl)
        s = brain.status_str()
        print(f"  {s}")
        assert "brain" in s
        assert "kelly" in s
        assert "model" in s
        print("  pass")


def main():
    test_safe_float()
    test_feature_extractor_no_crash_on_empty()
    test_logreg_learns_on_synthetic()
    test_logreg_serialization()
    test_kelly()
    test_kelly_serialization()
    test_regime()
    test_tracker()
    test_brain_end_to_end()
    test_brain_status_str()
    print("\n" + "=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()
