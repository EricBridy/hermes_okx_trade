# OKX Perpetual Swap Backtester

**通用 OKX 永续合约回测框架** — 不绑定特定策略版本，支持任意品种、多时间框架、资金费率、手续费、止盈止损、追踪止损。

```
📊 311+ 合约支持  |  📈 4种内置策略  |  🔧 自定义策略  |  💰 真实费率模拟
```

---

## 目录

- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [命令详解](#命令详解)
- [内置策略](#内置策略)
- [自定义策略](#自定义策略)
- [回测参数配置](#回测参数配置)
- [数据说明](#数据说明)
- [输出说明](#输出说明)
- [项目结构](#项目结构)
- [常见问题](#常见问题)

---

## 环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.8+ |
| 依赖 | **无第三方依赖**（纯标准库） |
| 网络 | 需要访问 `www.okx.com`（用于下载历史数据） |
| 磁盘 | 每品种每天约 5-10MB 缓存数据 |

```bash
# 确认 Python 版本
python3 --version

# 无需 pip install，开箱即用
```

---

## 快速开始

### 第 1 步：克隆 backtest 分支

```bash
git clone -b backtest https://github.com/EricBridy/hermes_okx_trade.git
cd hermes_okx_trade
```

### 第 2 步：列出所有可用合约

```bash
python3 okx_backtest.py list
```

输出示例：
```
📋 所有 USDT-SWAP 合约: 311 个

品种                         24h成交额           价格
-------------------------------------------------------
BTC-USDT-SWAP         $45,230,000,000    80,800.0000
ETH-USDT-SWAP         $18,650,000,000     3,200.0000
SOL-USDT-SWAP          $8,120,000,000       180.0000
```

### 第 3 步：下载历史数据

```bash
python3 okx_backtest.py download --symbols BTC,ETH,SOL --days 7
```

### 第 4 步：运行回测

```bash
python3 okx_backtest.py backtest --strategy dual_channel --symbols BTC,ETH,SOL --days 7
```

### 第 5 步：查看报告

回测完成后自动生成 HTML 报告到 `backtest_reports/` 目录。

---

## 命令详解

### `list` — 列出可用合约

```bash
python3 okx_backtest.py list                    # 默认显示 Top 30
python3 okx_backtest.py list --limit 50         # 显示 Top 50
python3 okx_backtest.py list --limit 100        # 显示 Top 100
```

### `download` — 下载历史数据

```bash
# 下载指定品种
python3 okx_backtest.py download --symbols BTC,ETH,SOL,DOGE,XRP

# 下载 30 天数据
python3 okx_backtest.py download --symbols BTC,ETH --days 30

# 下载所有可用合约（注意：数据量较大）
python3 okx_backtest.py download --symbols $(python3 okx_backtest.py list --limit 50 | awk 'NR>3{print $1}' | tr '\n' ',') --days 7
```

参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--symbols` | BTC,ETH,SOL,DOGE,XRP,PEPE,WIF,AVAX,LINK,BONK | 逗号分隔的品种列表（自动加 `-USDT-SWAP` 后缀） |
| `--days` | 7 | 下载天数 |
| `--list` | - | 同时列出所有可用合约 |

### `backtest` — 运行回测

```bash
# 基础用法
python3 okx_backtest.py backtest --strategy momentum --days 7

# 指定品种
python3 okx_backtest.py backtest --strategy dual_channel --symbols BTC,ETH,SOL --days 14

# 自定义参数
python3 okx_backtest.py backtest --strategy momentum --days 7 --balance 100 --leverage 10 --tp 2.0 --sl 1.0

# 使用自定义策略文件
python3 okx_backtest.py backtest --strategy my_strategy.py --days 7
```

参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--strategy` | momentum | 策略名或 .py 文件路径 |
| `--symbols` | 自动选 Top 10 | 逗号分隔品种（空=自动选成交量 Top 10） |
| `--days` | 7 | 回测天数 |
| `--balance` | 20.0 | 初始余额 (USDT) |
| `--leverage` | 6 | 杠杆倍数 |
| `--tp` | 3.0 | 止盈百分比 |
| `--sl` | 1.5 | 止损百分比 |

---

## 内置策略

### 1. `momentum` — 动量顺势

顺势交易：5m 涨做多、跌做空，需要多维度过滤。

```
开仓条件:
  - 5m 涨跌幅 > 0.1%
  - ADX < 40（趋势不过强）
  - BB 宽度 > 1.5%（有波动空间）
  - ROC 方向确认
  - RSI 不在极端区域
  - 综合评分 ≥ 8
```

```bash
python3 okx_backtest.py backtest --strategy momentum --days 7
```

### 2. `funding_reversal` — 资金费率反转

极端资金费率时反向开仓：FR<0 做多，FR>0 做空。

```
开仓条件:
  - |FR| > 0.05% 且 < 5%
  - 放量 > 1.5x
  - ADX < 40
  - BB 宽度 > 1.5%
  - 技术方向确认
```

```bash
python3 okx_backtest.py backtest --strategy funding_reversal --days 7
```

### 3. `dual_channel` — 双通道组合

同时运行 momentum + funding_reversal，合并信号。

```bash
python3 okx_backtest.py backtest --strategy dual_channel --days 7
```

### 策略对比

| 策略 | 类型 | 适合行情 | 信号频率 |
|------|------|----------|----------|
| `momentum` | 顺势 | 单边趋势 | 中等 |
| `funding_reversal` | 逆势 | 极端情绪反转 | 较少 |
| `dual_channel` | 混合 | 各种行情 | 较多 |

---

## 自定义策略

创建一个 `.py` 文件，继承 `BaseStrategy` 并实现 `on_bar()` 方法。

### 模板

```python
# my_strategy.py
from okx_backtest import BaseStrategy, Signal, calc_rsi, calc_adx, calc_bb_width

class Strategy(BaseStrategy):
    def __init__(self, config=None):
        super().__init__(config)
        self.name = "my_custom_strategy"

    def on_bar(self, ctx):
        """
        每根 5m K线 触发一次。

        ctx 字典内容:
          ctx["symbol"]     — 当前品种 (如 "BTC-USDT-SWAP")
          ctx["bar_index"]  — 当前 K线索引
          ctx["timestamps"] — 时间戳数组
          ctx["opens"]      — 开盘价数组
          ctx["highs"]      — 最高价数组
          ctx["lows"]       — 最低价数组
          ctx["closes"]     — 收盘价数组（已排序，最近在末尾）
          ctx["vols"]       — 成交量数组
          ctx["funding"]    — 资金费率历史列表
          ctx["balance"]    — 当前余额
          ctx["positions"]  — 当前活跃持仓列表

        返回:
          [] — 无信号
          [Signal("BTC-USDT-SWAP", "LONG", 10, "RSI超卖反转")]
          — 开多 BTC，评分10

        注意: 前 50 根 K线是指标预热期，指标数据可能不完整
        """
        signals = []
        c = ctx["closes"]

        # 至少需要 50 根 K线
        if len(c) < 50:
            return []

        # 计算指标
        rsi = calc_rsi(c)
        adx = calc_adx(ctx["highs"], ctx["lows"], c)

        # 你的策略逻辑
        if rsi < 30 and adx is not None and adx > 25:
            signals.append(Signal(
                ctx["symbol"], "LONG", 10,
                f"RSI={rsi:.0f} ADX={adx:.0f}"
            ))

        return signals
```

### 运行自定义策略

```bash
python3 okx_backtest.py backtest --strategy my_strategy.py --days 7
```

### 内置指标函数

| 函数 | 说明 | 参数 |
|------|------|------|
| `calc_rsi(closes, period=14)` | RSI 相对强弱 | 收盘价数组 |
| `calc_adx(highs, lows, closes, period=14)` | ADX 趋势强度 | 高/低/收数组 |
| `calc_bb_width(closes, period=20)` | 布林带宽度 % | 收盘价数组 |
| `calc_atr(highs, lows, closes, period=14)` | ATR 真实波幅 | 高/低/收数组 |
| `calc_stoch_k(highs, lows, closes, period=14)` | 随机指标 K | 高/低/收数组 |
| `calc_roc(closes, period=10)` | 价格变化率 ROC | 收盘价数组 |
| `calc_sma(closes, period)` | 简单移动平均 | 收盘价数组 |
| `calc_ema(closes, period)` | 指数移动平均 | 收盘价数组 |
| `find_funding_at(funding_list, ts_ms)` | 查找最近资金费率 | 费率列表+时间戳 |

### Signal 对象

```python
Signal(
    symbol="BTC-USDT-SWAP",   # 品种 (必须带 -USDT-SWAP)
    direction="LONG",          # "LONG" 或 "SHORT"
    score=10,                  # 评分 (越高越优先)
    reason="RSI超卖反转"       # 原因说明
)
```

---

## 回测参数配置

通过 `--balance` / `--leverage` / `--tp` / `--sl` 覆盖默认值：

```bash
python3 okx_backtest.py backtest --strategy momentum \
  --balance 100 \       # 100 USDT 初始资金
  --leverage 10 \       # 10 倍杠杆
  --tp 2.0 \            # 止盈 2%
  --sl 1.0              # 止损 1%
```

### 完整默认参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `initial_balance` | 20.0 | 初始余额 USDT |
| `leverage` | 6 | 杠杆倍数 |
| `position_pct` | 0.40 | 每笔仓位占可用资金 40% |
| `tp_pct` | 0.03 (3%) | 止盈 |
| `sl_pct` | 0.015 (1.5%) | 止损 |
| `trail_activate_pct` | 0.015 (1.5%) | 浮盈达到 1.5% 激活追踪止损 |
| `trail_distance_pct` | 0.008 (0.8%) | 追踪止损距离 0.8% |
| `time_stop_seconds` | 480 (8min) | 持仓超过 8 分钟触发时间止损 |
| `taker_fee` | 0.0005 (0.05%) | Taker 手续费（单边） |
| `maker_fee` | 0.0002 (0.02%) | Maker 手续费（单边） |
| `max_positions` | 2 | 最大同时持仓数 |
| `cooldown_seconds` | 2400 (40min) | 同品种平仓后冷却期 |
| `max_consecutive_loss` | 3 | 连亏 N 笔后暂停交易 |

### 修改方式

在自定义策略文件中覆盖：

```python
class Strategy(BaseStrategy):
    def __init__(self, config=None):
        # 覆盖默认参数
        custom_config = {
            "leverage": 10,
            "tp_pct": 0.02,
            "sl_pct": 0.01,
            "max_positions": 3,
        }
        super().__init__({**custom_config, **(config or {})})
        self.name = "aggressive"
```

---

## 数据说明

### 下载内容

每个品种下载 4 个时间框架 + 资金费率：

| 数据 | OKX API | 说明 |
|------|---------|------|
| 1m K线 | `/api/v5/market/history-candles` | 1 分钟 |
| 5m K线 | `/api/v5/market/history-candles` | 5 分钟（主时间框架） |
| 15m K线 | `/api/v5/market/history-candles` | 15 分钟 |
| 1H K线 | `/api/v5/market/history-candles` | 1 小时 |
| 资金费率 | `/api/v5/public/funding-rate-history` | 每 8 小时结算一次 |

### 数据缓存

- 缓存目录：`~/.hermes/scripts/backtest_data/`
- 文件命名：`{SYMBOL}_{BAR}_{DAYS}d.json`
- 已下载的数据会自动跳过，不会重复下载
- 删除缓存文件即可重新下载

### API 限制

- OKX 公开 API 无需 API Key
- 每次请求最多 300 根 K线
- 天数较多时自动分页（300根/次）
- 已内置速率控制（每次请求间隔 100-150ms）

---

## 输出说明

### 终端报告

```
============================================================
📊 回测报告 — momentum
============================================================
  初始余额:  $20.00
  最终余额:  $21.35
  总净利润:  $1.3500 (6.75%)
  总手续费:  $0.2100
  总资金费率: $0.0800
  总交易数:  15
  胜率:      60.0% (9W/6L)
  平均盈利:  $0.4500
  平均亏损:  $-0.2800
  盈亏比:    1.61
  最大回撤:  $0.8500 (4.02%)

  品种                    交易   胜率     净利润
  --------------------------------------------
  PEPE-USDT-SWAP            5   80% $  0.8200
  DOGE-USDT-SWAP            3   67% $  0.3300
  SOL-USDT-SWAP             4   50% $  0.1500
  BTC-USDT-SWAP             3   33% $  0.0500

  平仓原因:
    TP: 9
    SL: 3
    TIMEOUT: 3
============================================================
```

### HTML 报告

自动生成到 `backtest_reports/` 目录，包含：

- 📊 关键指标卡片（余额/净利润/胜率/最大回撤）
- 📈 权益曲线图（Canvas 绘制）
- 📋 完整交易记录表

文件名格式：`report_{策略名}_{时间戳}.html`

### 交易记录 JSON

同时保存 JSON 格式交易记录，方便程序化分析：

文件名格式：`trades_{策略名}_{时间戳}.json`

每笔交易包含：
```json
{
  "symbol": "BTC-USDT-SWAP",
  "direction": "LONG",
  "entry_price": 80500.0,
  "close_price": 82915.0,
  "size": 1,
  "notional": 805.0,
  "open_time": 1778400000000,
  "close_time": 1778401800000,
  "close_reason": "TP",
  "pnl": 24.15,
  "fee_paid": 0.805,
  "funding_paid": 0.12,
  "total_cost": 0.925,
  "net_pnl": 23.225,
  "duration_seconds": 1800
}
```

---

## 项目结构

```
okx_backtest.py              # 主程序 (回测引擎 + CLI)
backtest_data/               # 历史数据缓存 (自动创建)
  BTC-USDT-SWAP_5m_7d.json
  BTC-USDT-SWAP_1m_7d.json
  BTC-USDT-SWAP_15m_7d.json
  BTC-USDT-SWAP_1H_7d.json
  BTC-USDT-SWAP_funding_7d.json
  ...
backtest_reports/            # 回测报告输出 (自动创建)
  report_momentum_20260510_193955.html
  trades_momentum_20260510_193955.json
```

---

## 常见问题

### Q: 回测结果 0 笔交易怎么办？

可能原因：
1. **品种太少或波动太小** — 换高波动品种（PEPE, DOGE, WIF, BONK 等 meme 币）
2. **天数不够** — 加大 `--days`
3. **策略太严格** — 降低评分阈值或放宽过滤条件

```bash
# 推荐：高波动品种
python3 okx_backtest.py backtest --strategy dual_channel --symbols PEPE,DOGE,WIF,BONK,SOL --days 14
```

### Q: 数据下载很慢？

- OKX API 有速率限制，每个品种约需 1 秒
- 10 个品种 × 4 时间框架 ≈ 10-15 秒
- 数据已缓存，第二次运行会直接读缓存

### Q: 如何回测自定义参数组合？

```bash
# 参数扫描示例
for tp in 1.5 2.0 2.5 3.0; do
  for sl in 0.8 1.0 1.5 2.0; do
    python3 okx_backtest.py backtest --strategy momentum \
      --symbols PEPE,DOGE --days 7 --tp $tp --sl $sl \
      2>&1 | grep "总净利润"
  done
done
```

### Q: 回测和实盘有什么区别？

| 差异 | 回测 | 实盘 |
|------|------|------|
| 滑点 | 无（按收盘价） | 有 |
| 延迟 | 无 | 有（网络+API） |
| 流动性 | 假设无限 | 有限 |
| 追踪止损 | 逐根检查 | 逐秒检查 |
| 资金费率 | 按最近费率近似 | 精确到结算时刻 |

**建议**：回测结果 × 0.7-0.8 作为实盘预期更合理。

### Q: 如何分析历史数据？

```python
import json

with open("backtest_reports/trades_momentum_xxx.json") as f:
    trades = json.load(f)

# 打印每笔交易
for t in trades:
    print(f"{t['symbol']} {t['direction']} PnL=${t['net_pnl']:.4f} ({t['close_reason']})")

# 按品种统计
from collections import defaultdict
stats = defaultdict(lambda: {"count": 0, "pnl": 0})
for t in trades:
    stats[t["symbol"]]["count"] += 1
    stats[t["symbol"]]["pnl"] += t["net_pnl"]

for sym, s in sorted(stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
    print(f"{sym}: {s['count']} trades, ${s['pnl']:.4f}")
```

---

## License

MIT — 仅供学习和研究目的。加密货币合约交易风险极高，请谨慎使用。
