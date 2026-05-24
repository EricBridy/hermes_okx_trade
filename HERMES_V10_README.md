# Hermes v10 — Adaptive Perpetual Strategy

A complete rewrite of the v8.x / v9 line. **Not an evolution of v82.**

## What it actually does

Three independent signal generators feed into one learning brain:

1. **LIQ** — Liquidation cascade reversal. WebSocket subscription to OKX
   `liquidation-orders`. When a 60-second cumulative cascade crosses
   z-score 3 and price has moved ≥ 1.5 ATR against the squeezed side,
   emit a contrarian candidate.

2. **FR**  — Funding-rate contrarian. REST poll every 60s for top-30
   symbols. When |FR| ≥ 0.05% and z-score ≥ 1.5 vs symbol's own
   |FR| history, emit a contrarian candidate.

3. **MR**  — 30-minute mean reversion. REST scan every 5 minutes.
   When 30M close z-score ≥ 2 vs 20-period mean AND price is at the
   Bollinger band edge, fade the deviation.

Every candidate enters the brain via one entry point: `brain.evaluate()`.
The brain returns `(p_win, expected_value, kelly_fraction)` based on a
unified online logistic regression over a 16-D feature vector. If the
expected value clears the regime-adjusted edge threshold, the executor
opens a position sized via fractional Kelly.

Every closed position routes through one settlement path
(`Executor.close_and_settle`) which calls `brain.record_trade()` with
the realized PnL. The model performs one SGD step. State is persisted.

## Why not v82-style scanning

v82 polled the whole market every 30s, computed its own ad-hoc score,
and used a rigid Channel A / Channel B split. Three structural issues:
- The 8-factor Beta-bandit could not do credit assignment, so it
  never learned (verified in your live data: α=β=2.0 throughout).
- Multiple exit paths bypassed the learning hook entirely (BIO-USDT
  closed without a `record_trade_result` call).
- Hard-coded thresholds for each channel, no Kelly sizing.

v10 fixes all three: one model, one settlement path, Kelly sizing.

## Why this signal mix and timing

Network research dated 2026-Q1/Q2 (rephrased for compliance):
- Q1 2026 BTC -22% in a single quarter; daily trend-following
  collapsed for the first time in two years
  ([Delphic Alpha League Tables — March 2026](https://delphicalpha.substack.com/p/crypto-strategy-league-tables-march))
- Mean reversion posted its best month ever on 30-minute bars in March
- May 13-14 2026: $400M-$1.4B liquidation events with rapid reversals
  ([CoinDesk](https://www.coindesk.com/markets/2026/05/14/bitcoin-stuck-below-usd80-000-as-leveraged-longs-unwind-altcoins-slide))
- Realized vol compressed from 80%+ (2021) to 45-55% (2025-2026)
  → favours mean reversion over breakout
  ([TradeAlgo guide](https://www.tradealgo.com/trading-guides/crypto/bitcoin-trading))

Your latency profile (Aliyun Virginia → Cloudflare edge 1-3ms,
to OKX backend ~256ms RTT) cleanly supports 30-minute timeframes
and event-driven liquidation captures, but rules out sub-minute
scalping.

## Files

```
hermes_v10_brain.py       Learning core: features, online logreg, Kelly,
                          performance tracker, save/load. Pure stdlib.
hermes_v10_okx.py         OKX REST + WebSocket client. Hand-rolled
                          RFC6455 client so it works on any Python 3.7+.
hermes_v10_signals.py     LiquidationSignal / FundingRateSignal /
                          MeanReversionSignal — three signal generators.
hermes_v10_executor.py    Position dataclass + Executor with the single
                          close_and_settle() path.
hermes_v10_main.py        Async entry point with five concurrent loops:
                          WS, FR poll, MR scan, position monitor,
                          universe refresh, status.
test_v10_brain.py         Smoke tests: feature extraction, gradient
                          descent convergence, Kelly, tracker,
                          end-to-end brain training.
test_okx_latency.py       Stdlib-only OKX latency probe (already run).
probe_ws.py               WebSocket-specific probe (already run).
```

## Runtime files (created in `~/.hermes/scripts/`)

```
hermes_v10_state.json          Pause flags, total PnL, trade count,
                                position metadata.
hermes_v10_brain.json          Model weights, Kelly stats, tracker.
hermes_v10_trades.jsonl        One JSON line per closed trade with
                                full feature vector — the offline
                                analysis log.
hermes_v10_trades.log          Human-readable run log.
```

## Deploy to the server

The server has Python 3.6.8 by default. v10 needs 3.7+ for `dataclasses`
and `from __future__ import annotations`. You already have 3.11 at
`/root/.local/bin/python3.11`. Use it.

```bash
# Upload the new files (do NOT copy to /root/.hermes/scripts yet)
scp hermes_v10_*.py test_v10_brain.py root@<server>:/tmp/v10/

# On the server:
cd /tmp/v10
/root/.local/bin/python3.11 test_v10_brain.py    # must print "ALL v10 BRAIN TESTS PASSED"
```

Then move into place:

```bash
mkdir -p /root/.hermes/scripts
cp /tmp/v10/hermes_v10_*.py /root/.hermes/scripts/
cd /root/.hermes/scripts
```

Make sure v82 is fully stopped first (you said it already is). Then:

```bash
# Foreground (to confirm it boots cleanly):
/root/.local/bin/python3.11 hermes_v10_main.py

# Background (after you've verified it boots):
nohup /root/.local/bin/python3.11 hermes_v10_main.py \
    >> /root/.hermes/scripts/hermes_v10_stdout.log 2>&1 &
```

To stop:

```bash
pkill -INT -f hermes_v10_main.py
```

It catches SIGINT/SIGTERM and exits cleanly: closes the WS, saves the
brain, persists state. Open positions are NOT auto-closed on shutdown
— OCO algo orders on OKX continue to protect them.

## Tuning knobs (in `hermes_v10_main.py`)

```python
UNIVERSE_TOP_N = 40          # how many top-volume symbols to watch
MIN_24H_VOL_USD = 5_000_000  # raise to filter for liquidity
MAX_DAILY_TRADES = 25
MAX_CONCURRENT = 2           # number of simultaneous positions
SCAN_FR_INTERVAL = 60        # how often to poll funding rates
SCAN_30M_INTERVAL = 300      # how often to scan 30M MR signals
```

In `hermes_v10_executor.py`:

```python
LEVERAGE = 6
HARD_TP_PCT = 0.025          # 2.5% take-profit ceiling
HARD_SL_PCT = 0.012          # 1.2% stop-loss ceiling
ATR_TP_MULT = 2.5            # actual TP = min(2.5*ATR, 2.5%)
ATR_SL_MULT = 1.5
TRAIL_ATR_MULT = 2.0
TRAIL_ACTIVATE_PCT = 0.006   # start trailing after +0.6%
```

In `hermes_v10_brain.py`:

```python
class KellySizer:
    kelly_frac = 0.25       # quarter-Kelly (safe under estimation noise)
    max_risk_pct = 0.40     # never put more than 40% of equity at risk
    min_risk_pct = 0.10     # if we open at all, at least 10%
```

## Cold-start expectations

On day one the model has zero data. `evaluate()` will return `p_win ≈ 0.5`,
`ev ≈ +0.0040` for any candidate, so it will trade at the 10% min risk.
After ~30-50 closed trades the model starts to discriminate; expect
~150-200 trades before per-signal weights stabilise.

The `hermes_v10_trades.jsonl` log gives you a complete record. If you
ever want to retrain offline (batch logreg over the whole log), the
features and labels are right there.

## Operational sanity checks after the first 24h

```bash
# Number of trades, PnL summary
cat ~/.hermes/scripts/hermes_v10_trades.jsonl | wc -l
python3.11 -c "
import json, statistics
recs = [json.loads(x) for x in open('/root/.hermes/scripts/hermes_v10_trades.jsonl')]
print('trades:', len(recs))
print('total pnl:', sum(r['pnl_usd'] for r in recs))
print('win rate:', sum(1 for r in recs if r['pnl_usd']>0) / max(len(recs),1))
print('avg win:', statistics.mean([r['pnl_pct'] for r in recs if r['pnl_usd']>0] or [0]))
print('avg loss:', statistics.mean([r['pnl_pct'] for r in recs if r['pnl_usd']<=0] or [0]))
print('per signal:')
for sig in ('LIQ','FR','MR'):
    rs = [r for r in recs if r['signal_type']==sig]
    if rs:
        wins = sum(1 for r in rs if r['pnl_usd']>0)
        print(f'  {sig}: n={len(rs)} wins={wins} ({wins/len(rs):.0%}) pnl=\${sum(r[\"pnl_usd\"] for r in rs):.2f}')
"

# Brain state
python3.11 -c "
import json
d = json.load(open('/root/.hermes/scripts/hermes_v10_brain.json'))
m = d['model']
names = ['atr_pct','hour_sin','hour_cos','is_liq','is_fr','is_mr','dir_long',
         'liq_size_z','liq_imbalance','price_drop_atr','fr_abs_z','mr_zscore',
         'bb_position','microprice_bias','l2_imbalance','taker_buy_ratio']
print('updates:', m['updates'])
print('temp:', m['temp'])
print('top-5 weights:')
for n, w in sorted(zip(names, m['w']), key=lambda x: abs(x[1]), reverse=True)[:5]:
    print(f'  {n:20s} {w:+.3f}')
print('avg recent brier:', sum(m['recent_brier'])/max(len(m['recent_brier']),1))
"
```

## Things deliberately NOT in v10

- **No backtest harness.** The funding-rate edge collapses as more
  capital chases it. The model is designed to learn forward, in
  production, with real fills.
- **No Channel A/B split.** Replaced by a single model + signal-type
  one-hot features. The model learns interactions itself.
- **No ad-hoc scoring formulas.** Replaced by `p_win`, `expected_value`,
  Kelly fraction.
- **No sub-minute timeframe.** 256ms latency makes 5-minute strategies
  marginal and 1-minute strategies negative-expected-value.
- **No spot market dependency.** Pure perpetual. If you ever want
  delta-neutral funding harvesting that requires a spot leg, that's
  a separate strategy and a separate file.

## References (rephrased for compliance with content licensing)

- 2026 Q1 market state and trend-following collapse:
  [Delphic Alpha League Tables March 2026](https://delphicalpha.substack.com/p/crypto-strategy-league-tables-march),
  [CoinDesk Q1 2026 review](https://www.coindesk.com/coindesk-indices/2026/04/08/crypto-for-advisors-crypto-s-performance-q1)
- May 2026 liquidation events:
  [CoinDesk May 14 2026](https://www.coindesk.com/markets/2026/05/14/bitcoin-stuck-below-usd80-000-as-leveraged-longs-unwind-altcoins-slide)
- Liquidation-reversal strategy mechanics:
  [Hummingbot V2 Liquidation Sniper](https://hummingbot.org/blog/coding-a-liquidation-sniper-v2-strategy-controller/)
- Microprice and L2 imbalance as top short-horizon predictors:
  [Microstructure Lab Atlas](https://themicrostructurelab.substack.com/p/microstructure-alpha-atlas-across)
- Funding-rate contrarian signal at extremes:
  [Phemex academy](https://phemex.com/academy/what-is-funding-rate-in-crypto-futures)
- Online logistic regression for trading signals:
  [QuantInsti](https://blog.quantinsti.com/machine-learning-logistic-regression-python/)
- Fractional Kelly:
  [QuantStrategy](https://quantstrategy.io/blog/applying-the-kelly-criterion-to-trading-maximizing-growth)
