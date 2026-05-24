"""
自适应引擎 v1.0 — 三层自进化系统
用于 v81_yao_hunter.py

第1层: ATR状态检测（Regime Detection）
  - 实时判断市场处于"趋势"/"横盘"/"高波动"状态
  - 低波动横盘时自动提高开仓门槛或暂停

第2层: 因子权重自适应（Multi-Armed Bandit / Thompson Sampling）
  - 每个因子维护 Beta 分布（成功/失败计数）
  - 交易结束后更新，下次开仓时用采样决定权重
  - 时间衰减让近期表现权重更大

第3层: 品种/通道表现追踪（Performance Tracker）
  - 按品种、通道、时段追踪胜率和期望
  - 连续失败的品种/通道/时段自动冷却或提高门槛
"""

import json
import os
import time
import random
import math
from datetime import datetime, timezone, timedelta

ADAPTIVE_STATE_FILE = os.path.join(os.path.expanduser("~/.hermes/scripts"), "v82_adaptive.json")

# ============================================================
# 第1层: 市场状态检测 (Regime Detection)
# ============================================================

class RegimeDetector:
    """
    基于ATR百分位和变异系数判断市场状态
    状态: TRENDING(趋势), RANGING(横盘), VOLATILE(高波动), NORMAL(正常)
    
    v8.1.1 修复:
    - 横盘阈值从0.8%降到0.25%（实际市场ATR中位数通常在0.3-0.6%）
    - 增加变异系数(CV)辅助判断：ATR低且CV低才是真横盘
    - 置信度计算更平滑，不再一步拉满
    - should_pause 改为仅在极端情况下触发，且有自动恢复
    - 横盘时门槛提高幅度从30-60%降到10-25%
    """
    
    # 历史ATR窗口（保留最近60个扫描周期的ATR数据）
    ATR_HISTORY_SIZE = 60
    
    def __init__(self):
        self.atr_history = []  # [(timestamp, atr_pct)]
        self.current_regime = "UNKNOWN"
        self.regime_since = time.time()
        self.regime_confidence = 0.0
    
    def update(self, market_atr_pcts):
        """
        输入: 本轮扫描中所有候选品种的 atr_pct 列表
        输出: 更新当前市场状态
        """
        if not market_atr_pcts:
            return
        
        # 取中位数作为市场整体波动率
        sorted_atrs = sorted(market_atr_pcts)
        median_atr = sorted_atrs[len(sorted_atrs) // 2]
        
        now = time.time()
        self.atr_history.append((now, median_atr))
        
        # 保留最近N个
        if len(self.atr_history) > self.ATR_HISTORY_SIZE:
            self.atr_history = self.atr_history[-self.ATR_HISTORY_SIZE:]
        
        # 需要至少10个数据点才能判断
        if len(self.atr_history) < 10:
            self.current_regime = "UNKNOWN"
            return
        
        recent_atrs = [x[1] for x in self.atr_history[-10:]]
        all_atrs = [x[1] for x in self.atr_history]
        
        avg_recent = sum(recent_atrs) / len(recent_atrs)
        avg_all = sum(all_atrs) / len(all_atrs)
        
        # 计算变异系数(CV) — ATR变化的稳定性
        # CV低 = ATR几乎不变 = 真正的死水横盘
        if avg_recent > 0:
            std_recent = (sum((x - avg_recent)**2 for x in recent_atrs) / len(recent_atrs)) ** 0.5
            cv_recent = std_recent / avg_recent
        else:
            cv_recent = 0
        
        # 判断状态
        old_regime = self.current_regime
        
        if avg_recent < 0.25 and cv_recent < 0.15:
            # ATR < 0.25% 且变化极小 = 真正的极低波动横盘
            self.current_regime = "RANGING"
            # 置信度：ATR越低+CV越低 → 越确定是横盘
            atr_factor = min(1.0, (0.25 - avg_recent) / 0.15)  # 0.25→0, 0.10→1.0
            cv_factor = min(1.0, (0.15 - cv_recent) / 0.10)    # CV低→高置信
            self.regime_confidence = min(0.95, atr_factor * 0.6 + cv_factor * 0.4)
        elif avg_recent < 0.4 and cv_recent < 0.08:
            # ATR偏低且极度稳定 = 轻度横盘（只提高门槛，不暂停）
            self.current_regime = "RANGING"
            self.regime_confidence = min(0.5, (0.4 - avg_recent) / 0.3)
        elif avg_recent > 2.5:
            # ATR > 2.5% = 高波动
            self.current_regime = "VOLATILE"
            self.regime_confidence = min(1.0, (avg_recent - 2.5) / 2.0)
        elif avg_recent > avg_all * 1.3:
            # 近期ATR明显高于历史均值 = 趋势启动
            self.current_regime = "TRENDING"
            self.regime_confidence = min(1.0, (avg_recent / avg_all - 1.0) / 0.5)
        else:
            # 正常状态
            self.current_regime = "NORMAL"
            self.regime_confidence = 0.5
        
        if old_regime != self.current_regime:
            self.regime_since = now
        
        # 自动恢复：横盘超过60分钟后逐步降低置信度（允许尝试交易）
        if self.current_regime == "RANGING":
            duration = now - self.regime_since
            if duration > 3600:  # 超过1小时
                decay = min(0.3, (duration - 3600) / 7200)  # 最多降0.3
                self.regime_confidence = max(0.2, self.regime_confidence - decay)
    
    def get_score_multiplier(self):
        """
        根据市场状态返回评分门槛乘数
        横盘时适度提高门槛（乘数>1），趋势时降低门槛（乘数<1）
        """
        if self.current_regime == "RANGING":
            # 横盘：门槛提高 10-25%（温和调整，不再激进）
            return 1.1 + 0.15 * self.regime_confidence
        elif self.current_regime == "VOLATILE":
            # 高波动：门槛提高 15%（避免被假突破骗）
            return 1.15
        elif self.current_regime == "TRENDING":
            # 趋势：门槛降低 10-20%
            return 0.9 - 0.1 * self.regime_confidence
        else:
            return 1.0
    
    def should_pause(self):
        """
        仅在极端横盘时暂停（条件大幅收紧）
        - 需要置信度 > 0.85（真正的极低波动）
        - 需要持续 > 45分钟
        - 且最近ATR中位数 < 0.2%（几乎没有波动）
        """
        if self.current_regime == "RANGING":
            duration = time.time() - self.regime_since
            if duration > 2700 and self.regime_confidence > 0.85:
                # 额外检查：最近ATR是否真的极低
                if len(self.atr_history) >= 5:
                    recent_5 = [x[1] for x in self.atr_history[-5:]]
                    if sum(recent_5) / len(recent_5) < 0.20:
                        return True
        return False
    
    def get_status_str(self):
        """返回状态字符串用于日志"""
        duration_min = int((time.time() - self.regime_since) / 60)
        return f"{self.current_regime}({self.regime_confidence:.0%},{duration_min}min)"
    
    def to_dict(self):
        return {
            "atr_history": self.atr_history[-30:],  # 只保存最近30个
            "current_regime": self.current_regime,
            "regime_since": self.regime_since,
            "regime_confidence": self.regime_confidence,
        }
    
    def from_dict(self, d):
        self.atr_history = d.get("atr_history", [])
        self.current_regime = d.get("current_regime", "UNKNOWN")
        self.regime_since = d.get("regime_since", time.time())
        self.regime_confidence = d.get("regime_confidence", 0.0)


# ============================================================
# 第2层: 因子权重自适应 (Thompson Sampling Bandit)
# ============================================================

class FactorBandit:
    """
    Multi-Armed Bandit 因子权重自适应
    
    每个因子维护 Beta(alpha, beta) 分布:
    - alpha = 成功次数 (加权)
    - beta = 失败次数 (加权)
    
    使用 Thompson Sampling 采样决定因子权重
    加入时间衰减（decay），让近期表现更重要
    """
    
    # 因子列表
    FACTORS = [
        "fr_signal",       # 资金费率信号
        "trend_15m",       # 15分钟趋势
        "rsi_weighted",    # 多TF加权RSI
        "volume_spike",    # 放量信号
        "chain_score",     # 链上评分
        "momentum_5m",     # 5分钟动量
        "ema_alignment",   # EMA排列
        "stoch_position",  # 随机指标位置
    ]
    
    DECAY = 0.95  # 每次更新时历史数据衰减5%
    PRIOR_ALPHA = 2.0  # 先验：假设每个因子初始有2次成功
    PRIOR_BETA = 2.0   # 先验：假设每个因子初始有2次失败
    
    def __init__(self):
        # 每个因子的 Beta 分布参数
        self.factors = {}
        for f in self.FACTORS:
            self.factors[f] = {
                "alpha": self.PRIOR_ALPHA,
                "beta": self.PRIOR_BETA,
                "total_trades": 0,
                "last_update": 0,
            }
    
    def sample_weights(self):
        """
        Thompson Sampling: 从每个因子的 Beta 分布中采样
        返回归一化权重字典
        """
        samples = {}
        for f, params in self.factors.items():
            # Beta 分布采样
            sample = random.betavariate(
                max(params["alpha"], 0.1),
                max(params["beta"], 0.1)
            )
            samples[f] = sample
        
        # 归一化到 [0.5, 2.0] 范围（不会完全关闭任何因子）
        if not samples:
            return {f: 1.0 for f in self.FACTORS}
        
        max_s = max(samples.values())
        min_s = min(samples.values())
        range_s = max_s - min_s if max_s != min_s else 1.0
        
        weights = {}
        for f, s in samples.items():
            # 映射到 [0.5, 2.0]
            normalized = (s - min_s) / range_s  # [0, 1]
            weights[f] = 0.5 + normalized * 1.5  # [0.5, 2.0]
        
        return weights
    
    def get_expected_values(self):
        """返回每个因子的期望胜率（用于日志显示）"""
        evs = {}
        for f, params in self.factors.items():
            evs[f] = params["alpha"] / (params["alpha"] + params["beta"])
        return evs
    
    def update(self, trade_result, active_factors):
        """
        交易结束后更新因子分布
        
        trade_result: "WIN" 或 "LOSS"
        active_factors: 本次交易中活跃的因子列表
                       例如 ["fr_signal", "trend_15m", "rsi_weighted"]
        """
        now = time.time()
        
        # 先对所有因子做时间衰减
        for f in self.factors:
            self.factors[f]["alpha"] = max(
                self.PRIOR_ALPHA,
                self.factors[f]["alpha"] * self.DECAY
            )
            self.factors[f]["beta"] = max(
                self.PRIOR_BETA,
                self.factors[f]["beta"] * self.DECAY
            )
        
        # 更新活跃因子
        for f in active_factors:
            if f in self.factors:
                if trade_result == "WIN":
                    self.factors[f]["alpha"] += 1.0
                else:
                    self.factors[f]["beta"] += 1.0
                self.factors[f]["total_trades"] += 1
                self.factors[f]["last_update"] = now
    
    def get_factor_adjustment(self, factor_name):
        """
        获取单个因子的调整系数
        胜率高的因子返回 > 1.0，胜率低的返回 < 1.0
        """
        if factor_name not in self.factors:
            return 1.0
        
        params = self.factors[factor_name]
        expected = params["alpha"] / (params["alpha"] + params["beta"])
        
        # 映射到 [0.6, 1.4] 范围
        # expected=0.5 → 1.0, expected=0.7 → 1.4, expected=0.3 → 0.6
        return 0.6 + expected * 0.8
    
    def to_dict(self):
        return {"factors": self.factors}
    
    def from_dict(self, d):
        saved = d.get("factors", {})
        for f in self.FACTORS:
            if f in saved:
                self.factors[f] = saved[f]


# ============================================================
# 第3层: 品种/通道/时段表现追踪 (Performance Tracker)
# ============================================================

class PerformanceTracker:
    """
    追踪每个品种、通道、时段的历史表现
    自动冷却表现差的组合
    """
    
    # 时段定义 (UTC+8)
    TIME_SLOTS = {
        "asian_night": (0, 8),    # 00:00-08:00 亚洲深夜（低波动）
        "asian_morning": (8, 12), # 08:00-12:00 亚洲早盘
        "asian_afternoon": (12, 16), # 12:00-16:00 亚洲午盘
        "europe": (16, 20),       # 16:00-20:00 欧洲盘
        "us": (20, 24),           # 20:00-24:00 美盘
    }
    
    DECAY = 0.92  # 历史衰减（比因子衰减更快，品种表现变化快）
    MIN_TRADES_FOR_JUDGMENT = 3  # 至少3笔交易才做判断
    COLD_THRESHOLD = 0.30  # 胜率低于30%触发冷却
    COLD_DURATION = 7200   # 冷却2小时
    
    def __init__(self):
        # symbol -> {wins, losses, total_pnl, last_trade_time, cold_until}
        self.symbols = {}
        # channel -> {wins, losses, total_pnl}
        self.channels = {"A": {"wins": 0, "losses": 0, "pnl": 0.0},
                         "B": {"wins": 0, "losses": 0, "pnl": 0.0}}
        # time_slot -> {wins, losses, total_pnl}
        self.time_slots = {}
        for slot in self.TIME_SLOTS:
            self.time_slots[slot] = {"wins": 0, "losses": 0, "pnl": 0.0}
        # 同币连续亏损追踪
        self.symbol_streaks = {}  # symbol -> consecutive_losses
    
    def get_current_time_slot(self):
        """获取当前时段（UTC+8）"""
        utc8 = datetime.now(timezone(timedelta(hours=8)))
        hour = utc8.hour
        for slot_name, (start, end) in self.TIME_SLOTS.items():
            if start <= hour < end:
                return slot_name
        return "us"  # fallback
    
    def record_trade(self, symbol, channel, pnl, trade_time=None):
        """记录一笔交易结果"""
        if trade_time is None:
            trade_time = time.time()
        
        is_win = pnl > 0
        
        # 更新品种统计
        if symbol not in self.symbols:
            self.symbols[symbol] = {
                "wins": 0, "losses": 0, "pnl": 0.0,
                "last_trade": 0, "cold_until": 0
            }
        
        # 衰减历史
        self.symbols[symbol]["wins"] *= self.DECAY
        self.symbols[symbol]["losses"] *= self.DECAY
        self.symbols[symbol]["pnl"] *= self.DECAY
        
        if is_win:
            self.symbols[symbol]["wins"] += 1
            self.symbol_streaks[symbol] = 0
        else:
            self.symbols[symbol]["losses"] += 1
            self.symbol_streaks[symbol] = self.symbol_streaks.get(symbol, 0) + 1
        
        self.symbols[symbol]["pnl"] += pnl
        self.symbols[symbol]["last_trade"] = trade_time
        
        # 同币连亏3次 → 冷却
        if self.symbol_streaks.get(symbol, 0) >= 3:
            self.symbols[symbol]["cold_until"] = trade_time + self.COLD_DURATION
        
        # 更新通道统计
        if channel in self.channels:
            self.channels[channel]["wins" if is_win else "losses"] += 1
            self.channels[channel]["pnl"] += pnl
        
        # 更新时段统计
        slot = self.get_current_time_slot()
        self.time_slots[slot]["wins" if is_win else "losses"] += 1
        self.time_slots[slot]["pnl"] += pnl
    
    def is_symbol_cold(self, symbol):
        """检查品种是否在冷却中"""
        if symbol not in self.symbols:
            return False
        cold_until = self.symbols[symbol].get("cold_until", 0)
        return time.time() < cold_until
    
    def get_symbol_score_adjustment(self, symbol):
        """
        根据品种历史表现返回评分调整系数
        表现好的品种 > 1.0，表现差的 < 1.0
        """
        if symbol not in self.symbols:
            return 1.0  # 新品种不调整
        
        s = self.symbols[symbol]
        total = s["wins"] + s["losses"]
        if total < self.MIN_TRADES_FOR_JUDGMENT:
            return 1.0  # 数据不足不调整
        
        win_rate = s["wins"] / total
        
        # 映射: 胜率50% → 1.0, 胜率70% → 1.2, 胜率30% → 0.7
        return 0.5 + win_rate
    
    def get_channel_score_adjustment(self, channel):
        """根据通道历史表现返回调整系数"""
        if channel not in self.channels:
            return 1.0
        
        c = self.channels[channel]
        total = c["wins"] + c["losses"]
        if total < self.MIN_TRADES_FOR_JUDGMENT:
            return 1.0
        
        win_rate = c["wins"] / total
        return 0.6 + win_rate * 0.8  # [0.6, 1.4]
    
    def get_time_slot_adjustment(self):
        """根据当前时段历史表现返回调整系数"""
        slot = self.get_current_time_slot()
        ts = self.time_slots.get(slot, {})
        total = ts.get("wins", 0) + ts.get("losses", 0)
        
        if total < self.MIN_TRADES_FOR_JUDGMENT:
            return 1.0
        
        win_rate = ts["wins"] / total
        
        # 时段胜率低于35% → 大幅提高门槛
        if win_rate < 0.35:
            return 1.5  # 门槛提高50%
        elif win_rate < 0.45:
            return 1.2  # 门槛提高20%
        elif win_rate > 0.60:
            return 0.85  # 门槛降低15%
        return 1.0
    
    def get_combined_adjustment(self, symbol, channel):
        """综合三个维度的调整系数"""
        sym_adj = self.get_symbol_score_adjustment(symbol)
        ch_adj = self.get_channel_score_adjustment(channel)
        ts_adj = self.get_time_slot_adjustment()
        
        # 取几何平均（避免极端值）
        combined = (sym_adj * ch_adj * ts_adj) ** (1/3)
        
        # 限制在 [0.5, 1.8] 范围
        return max(0.5, min(1.8, combined))
    
    def get_status_str(self):
        """返回状态摘要"""
        slot = self.get_current_time_slot()
        ts = self.time_slots.get(slot, {})
        total = ts.get("wins", 0) + ts.get("losses", 0)
        wr = ts["wins"] / total * 100 if total > 0 else 0
        
        ch_a = self.channels["A"]
        ch_b = self.channels["B"]
        a_total = ch_a["wins"] + ch_a["losses"]
        b_total = ch_b["wins"] + ch_b["losses"]
        a_wr = ch_a["wins"] / a_total * 100 if a_total > 0 else 0
        b_wr = ch_b["wins"] / b_total * 100 if b_total > 0 else 0
        
        cold_count = sum(1 for s in self.symbols.values() 
                        if time.time() < s.get("cold_until", 0))
        
        return (f"时段={slot}({wr:.0f}%/{total}笔) "
                f"A={a_wr:.0f}%/{a_total} B={b_wr:.0f}%/{b_total} "
                f"冷却品种={cold_count}")
    
    def to_dict(self):
        return {
            "symbols": self.symbols,
            "channels": self.channels,
            "time_slots": self.time_slots,
            "symbol_streaks": self.symbol_streaks,
        }
    
    def from_dict(self, d):
        self.symbols = d.get("symbols", {})
        self.channels = d.get("channels", self.channels)
        self.time_slots = d.get("time_slots", self.time_slots)
        self.symbol_streaks = d.get("symbol_streaks", {})


# ============================================================
# 统一接口: AdaptiveEngine
# ============================================================

class AdaptiveEngine:
    """
    三层自适应引擎的统一接口
    """
    
    def __init__(self):
        self.regime = RegimeDetector()
        self.bandit = FactorBandit()
        self.tracker = PerformanceTracker()
        self._load()
    
    def _load(self):
        """从磁盘加载状态"""
        try:
            if os.path.exists(ADAPTIVE_STATE_FILE):
                with open(ADAPTIVE_STATE_FILE, "r") as f:
                    data = json.load(f)
                self.regime.from_dict(data.get("regime", {}))
                self.bandit.from_dict(data.get("bandit", {}))
                self.tracker.from_dict(data.get("tracker", {}))
        except Exception:
            pass  # 加载失败就用默认值
    
    def save(self):
        """保存状态到磁盘"""
        try:
            data = {
                "regime": self.regime.to_dict(),
                "bandit": self.bandit.to_dict(),
                "tracker": self.tracker.to_dict(),
                "last_save": time.time(),
            }
            os.makedirs(os.path.dirname(ADAPTIVE_STATE_FILE), exist_ok=True)
            with open(ADAPTIVE_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
    
    # ===== 扫描阶段调用 =====
    
    def update_regime(self, atr_pcts):
        """每轮扫描时更新市场状态（传入所有候选品种的ATR%列表）"""
        self.regime.update(atr_pcts)
    
    def should_pause_trading(self):
        """是否应该暂停交易（横盘太久）"""
        return self.regime.should_pause()
    
    def get_adjusted_threshold(self, base_threshold, symbol, channel):
        """
        获取自适应调整后的开仓门槛
        
        base_threshold: 原始门槛（如 MOMENTUM_SCORE_THRESHOLD=11）
        symbol: 品种 (如 "BASED-USDT-SWAP")
        channel: 通道 ("A" 或 "B")
        
        返回: 调整后的门槛值
        """
        # 第1层: 市场状态
        regime_mult = self.regime.get_score_multiplier()
        
        # 第2层: 因子权重（这里用通道对应的主因子）
        if channel == "A":
            factor_adj = self.bandit.get_factor_adjustment("fr_signal")
        else:
            factor_adj = self.bandit.get_factor_adjustment("momentum_5m")
        # 因子表现好 → 降低门槛（factor_adj > 1 时门槛降低）
        factor_mult = 2.0 - factor_adj  # factor_adj=1.4 → mult=0.6
        
        # 第3层: 品种/通道/时段表现
        perf_mult = self.tracker.get_combined_adjustment(symbol, channel)
        # 表现好 → 降低门槛（perf_adj > 1 时门槛降低）
        # 但这里 combined_adjustment 已经是"表现好>1"，我们需要反转
        perf_threshold_mult = 2.0 - perf_mult  # perf=1.2 → mult=0.8
        
        # 综合: 取三层的几何平均
        combined = (regime_mult * factor_mult * perf_threshold_mult) ** (1/3)
        
        # 限制调整范围 [0.7, 1.8]（不会太激进）
        combined = max(0.7, min(1.8, combined))
        
        return base_threshold * combined
    
    def is_symbol_blocked(self, symbol):
        """品种是否被冷却（连续亏损）"""
        return self.tracker.is_symbol_cold(symbol)
    
    def get_active_factors(self, candidate):
        """
        根据候选信号判断哪些因子是活跃的
        用于交易结束后更新 Bandit
        """
        factors = []
        
        if candidate.get("fr") and abs(candidate["fr"]) > 0.0005:
            factors.append("fr_signal")
        if candidate.get("trend_15m") in ("UP", "DOWN"):
            factors.append("trend_15m")
        if candidate.get("score", 0) > 0:
            factors.append("momentum_5m")
        
        # 从 channel 推断
        if candidate.get("channel") == "A":
            factors.append("fr_signal")
        if candidate.get("channel") == "B":
            factors.append("ema_alignment")
            factors.append("stoch_position")
            factors.append("volume_spike")
        
        # chain bonus
        if candidate.get("chain_tags"):
            factors.append("chain_score")
        
        return list(set(factors))
    
    # ===== 交易结束后调用 =====
    
    def record_trade_result(self, symbol, channel, pnl, active_factors=None):
        """
        交易结束后记录结果，更新所有三层
        
        symbol: 品种
        channel: "A" 或 "B"
        pnl: 盈亏金额
        active_factors: 本次交易活跃的因子列表
        """
        # 第2层: 更新 Bandit
        result = "WIN" if pnl > 0 else "LOSS"
        if active_factors:
            self.bandit.update(result, active_factors)
        
        # 第3层: 更新 Tracker
        self.tracker.record_trade(symbol, channel, pnl)
        
        # 定期保存
        self.save()
    
    # ===== 状态报告 =====
    
    def get_status_log(self):
        """返回自适应引擎状态的日志行"""
        regime_str = self.regime.get_status_str()
        tracker_str = self.tracker.get_status_str()
        
        # 因子期望值
        evs = self.bandit.get_expected_values()
        top_factors = sorted(evs.items(), key=lambda x: x[1], reverse=True)[:3]
        factor_str = " ".join(f"{f[0].split('_')[0]}={f[1]:.0%}" for f in top_factors)
        
        return f"[自适应] 状态={regime_str} | {tracker_str} | 因子TOP3: {factor_str}"
