# Hermes v9.0 — Self-Iterating OKX Perpetual Strategy

Replaces `v82_yao_hunter.py` + `adaptive_engine.py`. Built from a clean
sheet with three goals fixed up-front:

1. Every closed trade flows through **one** settlement path
   (`close_and_settle()`), so the learning loop never has missing data.
2. The model used to weight signals is an **online logistic regression**
   (SGD + L2), which gives proper joint credit assignment across a fixed
   16-D feature vector — unlike the v8.x bandit, which was structurally
   unable to learn because every trade activated multiple "factors"
   simultaneously.
3. Position size is **fractional Kelly**, computed from the model's
   probability estimate plus a rolling avg-win / avg-loss tracker. This
   makes risk auto-shrink during losing streaks and auto-grow during
   winning streaks, with a hard cap.

## Files

| file | role |
|------|------|
| `hermes_v9_brain.py`      | All learning logic — feature extractor, model, Kelly sizer, regime detector, performance tracker. Pure Python, no exchange deps. |
| `hermes_v9_strategy.py`   | OKX-specific trading loop. Imports `hermes_v9_brain`. |
| `test_v9_brain.py`        | Smoke tests + a synthetic-data convergence test that proves the model learns. |

## State files (created at runtime in `~/.hermes/scripts/`)

| file | purpose |
|------|---------|
| `hermes_v9_state.json`        | Pause flags, total PnL, trade count, last-trade timestamps, position metadata |
| `hermes_v9_brain.json`        | Model weights, Kelly stats, regime history, per-symbol cooldowns |
| `hermes_v9_trades.jsonl`      | One JSON line per closed trade — full feature vector, predicted p_win, realized PnL, exit reason. This is the offline-analysis log. |
| `hermes_v9_chain_cache.json`  | Web3 smart-money cache (TTL 60s) |

## Running

```sh
cd e:/Users/xwcEr/Desktop/hermes_okx_trade-7.8
python test_v9_brain.py        # one-off correctness check
python hermes_v9_strategy.py   # the actual bot
```

The strategy reads OKX credentials from `~/.okx/config.toml` (same as v8.x).

## What the brain learns

| Feature                | Source                                        |
|------------------------|-----------------------------------------------|
| `fr_abs`               | Funding rate magnitude                        |
| `fr_dir_align`         | Contrarian alignment (extreme FR vs trade dir) |
| `oi_delta_15m`         | Open interest change over ~15 min             |
| `oi_price_dir`         | OI rising with our direction = real new money |
| `microprice_bias`      | Size-weighted mid - mid (best LOB predictor)  |
| `l2_imbalance`         | Aggregated bid - ask depth, direction-aware   |
| `taker_buy_ratio`      | Buy taker volume / total taker volume         |
| `vol_spike_1m`         | Recent 1m volume / baseline                   |
| `ema_alignment`        | 5m + 15m EMA stack                            |
| `ha_trend_15m`         | Heikin-Ashi 15m trend                         |
| `rsi_weighted`         | 0.2·RSI(1m) + 0.3·RSI(5m) + 0.5·RSI(15m), zoned |
| `atr_pct`              | ATR(14) on 5m, in percent                     |
| `bb_squeeze`           | BB width below 1.5%                           |
| `chain_score`          | Smart-money / hot-topics / inflow bonus       |
| `hour_sin`, `hour_cos` | Cyclic time-of-day (UTC+8)                    |

After each closed trade:
```
y = 1 if pnl > 0 else 0
p = sigmoid(w·x + b)
w += eta * ((y - p) * x  -  lam * w)
b += eta * (y - p)
```
With `eta` decaying slowly toward `eta_min` and a Brier-score-driven
temperature scaler that softens predictions when calibration drifts.

## Exit logic

A position can leave through six paths, all of which call
`close_and_settle()`:

| path           | trigger                                        |
|----------------|------------------------------------------------|
| `TP`           | Hard 3% take-profit (server algo or local)     |
| `SL`           | Hard 1.5% stop-loss (server algo or local)     |
| `TRAIL`        | Chandelier stop: peak − `2.5 * ATR`            |
| `TIME_LOSS`    | >6 min and losing >1%                          |
| `TIME_FLAT`    | >25 min and not making progress                |
| `SIGNAL_DECAY` | strong 5m reversal + already losing            |
| `EXTERNAL`     | OKX shows the position was closed externally   |

## Position sizing

```
b = avg_win / avg_loss                                  # rolling, decay 0.95
f_full = (p · b - (1 - p)) / b                          # Kelly formula
fraction = clip(0.25 · f_full,  10% .. 40%)             # 1/4 Kelly, capped
expected_value = p · avg_win - (1 - p) · avg_loss - fees
```
A trade is taken only if `expected_value > regime_adjusted_min_edge`.

## What's intentionally NOT included

- **Backtesting harness** — backtests on funding-rate strategies are
  notoriously misleading because the FR distribution itself shifts with
  the strategy mass deployed at it (KuCoin/BitMEX both noted yields
  collapsing as more capital chased them). The system is designed to
  learn forward, in production, with real fills.
- **Per-feature interaction terms** — kept linear for now. If the
  Brier score plateaus high after several hundred trades, add
  hand-crafted interactions (e.g. `fr_abs * chain_score`) before
  reaching for non-linear models.
- **Walk-forward retraining** — the SGD update *is* online learning;
  we let `eta` decay slowly so old experience isn't overwritten. If you
  later want batch retraining from the JSONL log, the data is there.

## Migration from v8.x

The v9 state files have new names so you can run v8 and v9 side-by-side
during cutover. Once you trust v9, delete `v82_*` files and the old
`adaptive_engine.py`. The Web3 chain-cache code path is kept unchanged
because it was working.

## References

Strategy choices are grounded in:

- Funding-rate / OI joint signal interpretation —
  [CoinGlass](https://www.coinglass.com/learn/price-oi-and-cvd-en),
  [Phemex](https://phemex.com/academy/what-is-funding-rate-in-crypto-futures),
  [BloFin](https://blofin.com/academy/education/funding-and-open-interest-signals)
- Microprice / L2 imbalance as the strongest single short-horizon
  predictor —
  [Microstructure Lab](https://themicrostructurelab.substack.com/p/microstructure-alpha-atlas-across),
  [DeepLOB / Westray](https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html)
- Chandelier / ATR-based trailing exits for volatile assets —
  [QuantStrategy](https://quantstrategy.io/blog/how-to-use-the-chandelier-exit-in-trading/)
- Fractional-Kelly sizing under estimation noise —
  [Altrady](https://www.altrady.com/blog/risk-management/kelly-criterion-crypto-position-sizing),
  [QuantStrategy](https://quantstrategy.io/blog/applying-the-kelly-criterion-to-trading-maximizing-growth)
- Online logistic regression for trading signal weighting —
  [QuantInsti](https://blog.quantinsti.com/machine-learning-logistic-regression-python/),
  [scikit-learn SGDClassifier docs](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.SGDClassifier.html)

Content was rephrased and synthesised for compliance with licensing.
