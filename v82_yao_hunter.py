#!/usr/bin/env python3
"""
OKX 妖币猎手 v8.2 — 持仓健康度进化版（基于v8.1 + 信号健康度监控）
目标：持仓期间持续监控信号质量，动态调整退出策略，提高盈亏比

v8.2 核心升级：
- 持仓健康度评分(0-100)：趋势一致性/EMA支撑/动量方向/RSI反转/放量确认/动量衰减/量价背离
- 动态退出参数：信号强→放宽追踪让利润跑，信号弱→收紧快速出局
- 信号失效退出：健康度<25+亏损→不等P3直接出局
- 修复横盘检测：ATR阈值从0.8%降到0.25%+CV辅助，不再误判正常市场

继承v8.1所有技术：三层自适应 + 多TF RSI + HA趋势 + Bar Close + ATR动态 + 插针过滤
新增：持仓信号健康度 + 动态追踪止损 + 动态P3时间
"""

import subprocess, json, time, os, hmac, base64, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from adaptive_engine import AdaptiveEngine

# ==================== 配置 ====================
LEVERAGE = 6
TP_PCT = 0.025         # 2.5% 止盈（比3%更容易触发，但手续费占比仍可控）
SL_PCT = 0.015         # 1.5% 止损
TRAIL_ACTIVATE = 0.010  # 浮盈1%激活追踪（更快锁利）
TRAIL_DISTANCE = 0.005  # 追踪止损距离0.5%
TIME_STOP_SEC = 360       # 6分钟 phase1: 浮亏>1%止损
TIME_STOP_P3_SEC = 1200   # 20分钟 phase3: 横盘止损（延长，给更多时间到TP）
SCAN_INTERVAL = 30
COOLDOWN_SEC = 3600      # 同一品种冷却1小时（从2小时缩短）
MAX_CONCURRENT = 2
MAX_CONSECUTIVE_LOSSES = 4
LOSS_PAUSE_SEC = 1200   # 连亏暂停20分钟
SWAP_COOLDOWN = 1800    # 换仓冷却30分钟

# v8.0D: 每日交易上限
MAX_DAILY_TRADES = 25

# v8.0D: 黑名单机制
BLACKLIST_LOSS_COUNT = 4
BLACKLIST_DURATION = 43200  # 12h

# 通道A: 极端FR
FR_EXTREME_THRESHOLD = 0.0005   # |FR| > 0.05% 触发（更宽松，更多机会）
FR_MAX_THRESHOLD = 0.01
CHANNEL_A_VOL_SPIKE = 1.3
CHANNEL_A_POSITION_PCT = 0.40

# 通道B: 动量顺势
MOMENTUM_SCORE_THRESHOLD = 11   # v8.0E: 评分>=11（含多TF加权RSI+2分，满分~24）
CHANNEL_B_POSITION_PCT = 0.30

# v8.0D: 链上不强制要求（FR本身就是最强信号）
MIN_CHAIN_FACTORS = 0

# 共用
MIN_24H_VOL = 3000000           # 最小24h成交量 $300万
MIN_PRICE = 0.001
OKX_TAKER_FEE = 0.0005
ROUND_TRIP_FEE = OKX_TAKER_FEE * 2

# 时间窗口 (UTC+8) — 24小时
TRADING_WINDOWS = [(0, 0, 23, 59)]

# 链上数据
CHAIN_DATA_TTL = 60
CHAIN_BONUS = 2
CHAIN_API_BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"
CHAIN_API_TIMEOUT = 3

# v8.0D: 扫描池
SCAN_TOP_N = 60

# ==================== 缓存 ====================
INSTRUMENTS_CACHE = {}
FR_HISTORY = {}
FR_HISTORY_TTL = 300
SKIP_SYMS = {"RLS-USDT-SWAP", "BILL-USDT-SWAP"}

CHAIN_CACHE = {
    "smart_money_buy": set(),
    "hot_topics": set(),
    "smart_money_inflow": set(),
    "token_metrics": {},   # symbol -> {traders24h, trades24h, sm_pct, insider_pct, holders, kol}
    "last_update": 0
}

# v8.0C: 黑名单 + 每日统计
BLACKLIST = {}
LOSS_STREAK = {}
DAILY_TRADE_COUNT = {"date": "", "count": 0}

# ==================== 多仓管理 ====================
positions = {}
positions_lock = threading.Lock()
swap_cooldown_map = {}    # 仓位 -> 最后一次换仓时间
prev_channel_b = []       # 上一轮扫描的通道B候选（用于持久性检查）

# ==================== 日志 ====================
SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
STATE_FILE = os.path.join(SCRIPT_DIR, "v82_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "v82_trades.log")
CHAIN_CACHE_FILE = os.path.join(SCRIPT_DIR, "v82_chain_cache.json")

# v8.1: 自适应引擎实例（全局）
adaptive = AdaptiveEngine()

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass

# ==================== 状态 ====================
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except:
        pass
    return {"consecutive_losses": 0, "pause_until": None, "last_trade": {}, "total_pnl": 0, "trade_count": 0}

def save_state(state):
    # 持久化仓位元数据（channel/source_channel/score），用于重启恢复
    pos_meta = {}
    for pid, pinfo in positions.items():
        pos_meta[pid] = {
            "channel": pinfo.get("channel", ""),
            "source_channel": pinfo.get("source_channel", ""),
            "score": pinfo.get("score", 0),
        }
    state["position_meta"] = pos_meta
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ==================== OKX API ====================
def get_creds():
    with open(os.path.expanduser("~/.okx/config.toml")) as f:
        cfg = f.read()
    lines = cfg.strip().split("\n")
    api_key = secret_key = passphrase = ""
    for l in lines:
        l = l.strip()
        if l.startswith("api_key"): api_key = l.split("=", 1)[1].strip().strip('"').strip("'")
        elif l.startswith("secret_key"): secret_key = l.split("=", 1)[1].strip().strip('"').strip("'")
        elif l.startswith("passphrase"): passphrase = l.split("=", 1)[1].strip().strip('"').strip("'")
    return api_key, secret_key, passphrase

def okx_post(path, body_str):
    try:
        import requests
    except ImportError:
        log("❌ requests未安装")
        return {"code": "-1", "msg": "requests not installed"}
    api_key, secret_key, passphrase = get_creds()
    for attempt in range(3):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        msg = ts + "POST" + path + body_str
        mac = hmac.new(bytes(secret_key, "utf-8"), bytes(msg, "utf-8"), "sha256")
        sign = base64.b64encode(mac.digest()).decode()
        headers = {"OK-ACCESS-KEY": api_key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": passphrase, "Content-Type": "application/json"}
        try:
            r = requests.post("https://www.okx.com" + path, headers=headers, data=body_str, timeout=10)
            d = r.json()
            if d.get("code") == "50011":
                time.sleep(1.5 * (attempt + 1))
                continue
            return d
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return {"code": "-1", "msg": str(e)}
    return {"code": "-1", "msg": "max retries"}

def okx_get(path):
    try:
        import requests
    except ImportError:
        log("❌ requests未安装")
        return {"code": "-1", "msg": "requests not installed"}
    api_key, secret_key, passphrase = get_creds()
    for attempt in range(3):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        msg = ts + "GET" + path
        mac = hmac.new(bytes(secret_key, "utf-8"), bytes(msg, "utf-8"), "sha256")
        sign = base64.b64encode(mac.digest()).decode()
        headers = {"OK-ACCESS-KEY": api_key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": passphrase}
        try:
            r = requests.get("https://www.okx.com" + path, headers=headers, timeout=10)
            d = r.json()
            if d.get("code") == "50011":
                time.sleep(1.5 * (attempt + 1))
                continue
            return d
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return {"code": "-1", "msg": str(e)}
    return {"code": "-1", "msg": "max retries"}

def curl_json(url, timeout=10, retries=3):
    for attempt in range(retries):
        try:
            r = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url], capture_output=True, text=True, timeout=timeout+5)
            d = json.loads(r.stdout)
            if d.get("code") == "50011":
                time.sleep(0.5 * (attempt + 1))
                continue
            return d
        except:
            pass
    return {}

def curl_json_post(url, body, timeout=10, retries=2):
    for attempt in range(retries):
        try:
            body_str = json.dumps(body)
            r = subprocess.run(
                ["curl", "-sS", "-X", "POST", "--max-time", str(timeout),
                 "-H", "Content-Type: application/json",
                 "-H", "Accept-Encoding: identity",
                 "-H", "User-Agent: binance-web3/2.1 (Skill)",
                 "-d", body_str, url],
                capture_output=True, text=True, timeout=timeout+5
            )
            return json.loads(r.stdout)
        except:
            pass
    return {}

# ==================== Chain Data (Binance Web3) ====================
def fetch_smart_money_signals():
    result = set()
    for chain_id in ["CT_501", "56"]:
        try:
            d = curl_json_post(
                CHAIN_API_BASE + "/buw/wallet/web/signal/smart-money/ai",
                {"chainId": chain_id, "page": 1, "pageSize": 50},
                timeout=CHAIN_API_TIMEOUT
            )
            if d.get("data"):
                for sig in d["data"]:
                    if sig.get("direction") == "buy" and sig.get("smartMoneyCount", 0) >= 2:
                        ticker = (sig.get("ticker") or "").upper()
                        if ticker:
                            result.add(ticker)
        except:
            pass
    return result

def fetch_hot_topic_tokens():
    result = set()
    for chain_id in ["CT_501", "56"]:
        try:
            url = "https://web3.binance.com/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai"
            d = curl_json(f"{url}?chainId={chain_id}&rankType=20&sort=20&asc=false", timeout=CHAIN_API_TIMEOUT)
            if d.get("data"):
                for topic in d.get("data", []):
                    for t in topic.get("tokenList", [])[:5]:
                        sym = (t.get("symbol") or "").upper()
                        if sym:
                            result.add(sym)
        except:
            pass
    return result

def fetch_smart_money_inflow():
    result = set()
    for chain_id in ["CT_501", "56"]:
        try:
            url = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai"
            d = curl_json_post(url, {"chainId": chain_id, "period": "4h", "tagType": 2}, timeout=CHAIN_API_TIMEOUT)
            if d.get("data"):
                for item in d.get("data", [])[:20]:
                    sym = (item.get("tokenName") or "").upper()
                    if sym:
                        result.add(sym)
        except:
            pass
    return result

def fetch_social_metrics():
    """从Binance Web3 social-rush API提取社交情绪+鲸鱼持仓+集中度数据
    返回: dict[symbol -> {traders24h, trades24h, sm_pct, insider_pct, holders, kol}]
    """
    result = {}
    for chain_id in ["CT_501", "56"]:
        try:
            url = "https://web3.binance.com/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai"
            d = curl_json(f"{url}?chainId={chain_id}&rankType=10&sort=20&asc=false", timeout=CHAIN_API_TIMEOUT)
            if not d.get("data"):
                continue
            for topic in d["data"]:
                for t in topic.get("tokenList", []):
                    sym = (t.get("symbol") or "").upper()
                    if not sym:
                        continue
                    holders = int(t.get("holders") or 0)
                    if holders < 50:  # 排除极小代币
                        continue
                    # 取最大holders的那个条目（避免重复覆盖小数据）
                    existing = result.get(sym, {})
                    if holders > existing.get("holders", 0):
                        sm_pct = t.get("smartMoneyHoldingPercent")
                        insider_pct = t.get("insiderHoldingPercent")
                        result[sym] = {
                            "traders24h": int(t.get("uniqueTrader24h") or 0),
                            "trades24h": int(t.get("count24h") or 0),
                            "sm_pct": float(sm_pct) if sm_pct else 0,
                            "insider_pct": float(insider_pct) if insider_pct else 0,
                            "holders": holders,
                            "kol": int(t.get("kolHolders") or 0),
                        }
        except:
            pass
    return result


def save_chain_cache():
    """持久化链上缓存到磁盘"""
    try:
        data = {
            "smart_money_buy": list(CHAIN_CACHE["smart_money_buy"]),
            "hot_topics": list(CHAIN_CACHE["hot_topics"]),
            "smart_money_inflow": list(CHAIN_CACHE["smart_money_inflow"]),
            "token_metrics": CHAIN_CACHE["token_metrics"],
            "last_update": CHAIN_CACHE["last_update"]
        }
        with open(CHAIN_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

def load_chain_cache():
    """从磁盘恢复链上缓存（进程重启后使用）"""
    try:
        if os.path.exists(CHAIN_CACHE_FILE):
            with open(CHAIN_CACHE_FILE) as f:
                data = json.load(f)
            CHAIN_CACHE["smart_money_buy"] = set(data.get("smart_money_buy", []))
            CHAIN_CACHE["hot_topics"] = set(data.get("hot_topics", []))
            CHAIN_CACHE["smart_money_inflow"] = set(data.get("smart_money_inflow", []))
            CHAIN_CACHE["token_metrics"] = data.get("token_metrics", {})
            age = time.time() - data.get("last_update", 0)
            tm_count = len(CHAIN_CACHE["token_metrics"])
            log(f"  [chain] 从磁盘恢复缓存: SM={len(CHAIN_CACHE['smart_money_buy'])} HOT={len(CHAIN_CACHE['hot_topics'])} INF={len(CHAIN_CACHE['smart_money_inflow'])} metrics={tm_count} (缓存{int(age)}秒前)")
            CHAIN_CACHE["last_update"] = data.get("last_update", 0)
            return True
    except:
        pass
    return False

def update_chain_cache():
    now = time.time()
    if now - CHAIN_CACHE["last_update"] < CHAIN_DATA_TTL:
        return
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_sm = ex.submit(fetch_smart_money_signals)
        f_ht = ex.submit(fetch_hot_topic_tokens)
        f_in = ex.submit(fetch_smart_money_inflow)
        f_social = ex.submit(fetch_social_metrics)
        try:
            sm = f_sm.result(timeout=CHAIN_API_TIMEOUT + 2)
            ht = f_ht.result(timeout=CHAIN_API_TIMEOUT + 2)
            inf = f_in.result(timeout=CHAIN_API_TIMEOUT + 2)
            social = f_social.result(timeout=CHAIN_API_TIMEOUT + 2)
            CHAIN_CACHE["smart_money_buy"] = sm
            CHAIN_CACHE["hot_topics"] = ht
            CHAIN_CACHE["smart_money_inflow"] = inf
            CHAIN_CACHE["token_metrics"] = social
            CHAIN_CACHE["last_update"] = now
            save_chain_cache()  # 持久化到磁盘
            log(f"  [chain] SM_buy={len(sm)} topics={len(ht)} SM_inflow={len(inf)} social={len(social)}")
        except Exception as e:
            log(f"  [chain] API异常: {e}（保留旧缓存）")
            CHAIN_CACHE["last_update"] = now

def get_chain_score(ticker_symbol):
    """链上评分：原始3因子 + 新增3因子（社交情绪/鲸鱼持仓/持仓集中度）"""
    score = 0
    tags = []
    sym = ticker_symbol.upper()

    # 原始3因子（各+2分）
    if sym in CHAIN_CACHE["smart_money_buy"]:
        score += CHAIN_BONUS
        tags.append("SM")
    if sym in CHAIN_CACHE["hot_topics"]:
        score += CHAIN_BONUS
        tags.append("HOT")
    if sym in CHAIN_CACHE["smart_money_inflow"]:
        score += CHAIN_BONUS
        tags.append("INF")

    # 新增3因子
    metrics = CHAIN_CACHE.get("token_metrics", {}).get(sym)
    if metrics:
        # 1. 社交情绪（交易活跃度）— traders24h > 500 → +1，> 2000 → +2
        traders = metrics.get("traders24h", 0)
        if traders > 2000:
            score += 2
            tags.append(f"社{traders}")
        elif traders > 500:
            score += 1
            tags.append(f"社{traders}")

        # 2. 鲸鱼持仓（smartMoneyHoldingPercent > 3% → +2, > 1% → +1）
        sm_pct = metrics.get("sm_pct", 0)
        if sm_pct > 3:
            score += 2
            tags.append(f"鲸{sm_pct:.1f}%")
        elif sm_pct > 1:
            score += 1
            tags.append(f"鲸{sm_pct:.1f}%")

        # 3. 持仓集中度（insiderHoldingPercent 是风险信号！越高=内部人持仓越多=rug风险越大）
        # insider > 5% → -2分（高风险）, > 2% → -1分（中风险）
        # insider < 0.5% → +1分（去中心化，正面信号）
        insider_pct = metrics.get("insider_pct", 0)
        if insider_pct > 5:
            score -= 2
            tags.append(f"⚠️集{insider_pct:.1f}%")
        elif insider_pct > 2:
            score -= 1
            tags.append(f"⚠集{insider_pct:.1f}%")
        elif insider_pct < 0.5 and insider_pct > 0:
            score += 1
            tags.append(f"去中心化")

    return score, tags


# ==================== v8.0C: 黑名单/日计数/链上强度 ====================
def check_blacklist(sym):
    now = time.time()
    if sym in BLACKLIST:
        if now < BLACKLIST[sym]:
            return True
        else:
            del BLACKLIST[sym]
    return False

def record_trade_result(sym, pnl):
    if pnl > 0:
        LOSS_STREAK[sym] = 0
    else:
        LOSS_STREAK[sym] = LOSS_STREAK.get(sym, 0) + 1
        if LOSS_STREAK[sym] >= BLACKLIST_LOSS_COUNT:
            BLACKLIST[sym] = time.time() + BLACKLIST_DURATION
            log(f"  🚫 {sym} 连亏{LOSS_STREAK[sym]}次 → 拉黑{BLACKLIST_DURATION//3600}小时")
            LOSS_STREAK[sym] = 0

def check_daily_limit():
    today = datetime.now().strftime("%Y-%m-%d")
    if DAILY_TRADE_COUNT["date"] != today:
        DAILY_TRADE_COUNT["date"] = today
        DAILY_TRADE_COUNT["count"] = 0
    return DAILY_TRADE_COUNT["count"] >= MAX_DAILY_TRADES

def incr_daily_trade():
    today = datetime.now().strftime("%Y-%m-%d")
    if DAILY_TRADE_COUNT["date"] != today:
        DAILY_TRADE_COUNT["date"] = today
        DAILY_TRADE_COUNT["count"] = 0
    DAILY_TRADE_COUNT["count"] += 1

def count_chain_factors(tags):
    hard_factors = {"SM", "HOT", "INF"}
    count = sum(1 for t in tags if t in hard_factors)
    for t in tags:
        if t.startswith("社") or t.startswith("鲸"):
            count += 1
    return count


# ==================== 时间窗口 ====================
SETTLEMENT_HOURS_UTC = [0, 8, 16]
PRE_SETTLEMENT_BUFFER_MIN = 15

def in_trading_window():
    now = datetime.now(timezone(timedelta(hours=8)))
    h, m = now.hour, now.minute
    for sh, sm, eh, em in TRADING_WINDOWS:
        if (h > sh or (h == sh and m >= sm)) and (h < eh or (h == eh and m <= em)):
            return True
    return False

def is_near_settlement():
    now_utc = datetime.now(timezone.utc)
    for h in SETTLEMENT_HOURS_UTC:
        settlement = now_utc.replace(hour=h, minute=0, second=0, microsecond=0)
        diff = (settlement - now_utc).total_seconds()
        if diff <= 0:
            settlement += timedelta(days=1)
            diff = (settlement - now_utc).total_seconds()
        if 0 < diff <= PRE_SETTLEMENT_BUFFER_MIN * 60:
            return True, int(diff)
    return False, 0

# ==================== 合约规格缓存 ====================
def load_instruments_cache():
    global INSTRUMENTS_CACHE
    try:
        d = curl_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP", 15)
        if d.get("data"):
            for inst in d["data"]:
                inst_id = inst["instId"]
                max_lev = float(inst.get("lever") or 10)
                INSTRUMENTS_CACHE[inst_id] = {
                    "ctVal": float(inst.get("ctVal") or 1),
                    "lotSz": float(inst.get("lotSz") or 1),
                    "minSz": float(inst.get("minSz") or 1),
                    "maxLev": max_lev,
                    "tickSz": inst.get("tickSz", "0.00001"),
                }
            log(f"✅ 合约规格缓存: {len(INSTRUMENTS_CACHE)}个")
    except Exception as e:
        log(f"⚠️ 缓存加载失败: {e}")

# ==================== 资金费率 ====================
def get_funding_rates_batch(syms):
    results = {}
    def get_fr(sym):
        try:
            d = curl_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}", 5)
            if d.get("code") == "0" and d.get("data"):
                return sym, float(d["data"][0]["fundingRate"])
        except:
            pass
        return sym, None
    with ThreadPoolExecutor(max_workers=10) as ex:
        for sym, rate in ex.map(get_fr, syms):
            if rate is not None:
                results[sym] = rate
    return results

# ==================== 多时间框架K线分析 ====================
def get_multi_timeframe(sym):
    """获取1m/5m/15m K线，计算动量指标"""
    try:
        # 1分钟K线
        c1m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=1m&limit=30", 5)
        # 5分钟K线（拉50根用于ADX计算，ADX需要28+根）
        c5m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=5m&limit=50", 5)
        # 15分钟K线（v8.0E：扩大到20根用于HA和RSI计算）
        c15m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=15m&limit=20", 5)

        result = {}

        if c1m.get("data") and len(c1m["data"]) >= 15:
            candles = c1m["data"][::-1]
            closes = [float(c[4]) for c in candles]
            vols = [float(c[5]) for c in candles]
            result['chg_5m'] = (closes[-1] - closes[-5]) / closes[-5] * 100
            result['chg_10m'] = (closes[-1] - closes[-10]) / closes[-10] * 100
            result['chg_15m'] = (closes[-1] - closes[-15]) / closes[-15] * 100
            # 1分钟放量
            r1 = sum(vols[-3:]) / 3
            h1 = sum(vols[:-3]) / max(len(vols[:-3]), 1)
            result['vol_1m'] = r1 / max(h1, 0.001)
            # 连涨/连跌根数
            ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
            downs = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
            result['ups'] = ups
            result['downs'] = downs

            # === RSI(14) ===
            if len(closes) >= 15:
                deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                gains = [d if d > 0 else 0 for d in deltas[:14]]
                losses = [-d if d < 0 else 0 for d in deltas[:14]]
                avg_gain = sum(gains) / 14
                avg_loss = sum(losses) / 14
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    result['rsi_14'] = 100 - (100 / (1 + rs))
                else:
                    result['rsi_14'] = 100  # 全涨

            # === 1m反转检测：最近3根1m的涨跌方向 ===
            if len(candles) >= 3:
                last3_up = sum(1 for i in range(-3, 0) if closes[i] > closes[i-1])
                last3_down = 3 - last3_up
                result['last3_1m_ups'] = last3_up
                result['last3_1m_downs'] = last3_down

        if c5m.get("data") and len(c5m["data"]) >= 4:
            c5 = c5m["data"][::-1]
            cl5 = [float(c[4]) for c in c5]
            vl5 = [float(c[5]) for c in c5]
            result['chg_25m'] = (cl5[-1] - cl5[-4]) / cl5[-4] * 100
            r5 = sum(vl5[-2:]) / 2
            h5 = sum(vl5[:-2]) / max(len(vl5[:-2]), 1)
            result['vol_5m'] = r5 / max(h5, 0.001)

            # === 5m连续确认：最近2根5m是否同向 ===
            if len(cl5) >= 2:
                last_5m_chg = (cl5[-1] - cl5[-2]) / cl5[-2] * 100
                prev_5m_chg = (cl5[-2] - cl5[-3]) / cl5[-3] * 100 if len(cl5) >= 3 else 0
                result['last_5m_chg'] = last_5m_chg
                result['prev_5m_chg'] = prev_5m_chg
                # 两根同向且同向幅度 > 0.2%
                if last_5m_chg > 0.2 and prev_5m_chg > 0.2:
                    result['5m_consecutive'] = 'UP'
                elif last_5m_chg < -0.2 and prev_5m_chg < -0.2:
                    result['5m_consecutive'] = 'DOWN'
                else:
                    result['5m_consecutive'] = 'NONE'

                # === 动量衰减检测（新）===
                # 连续3根5m的涨跌幅递减 = 动量在消退
                if len(cl5) >= 4:
                    chg1 = abs((cl5[-1] - cl5[-2]) / cl5[-2] * 100)
                    chg2 = abs((cl5[-2] - cl5[-3]) / cl5[-3] * 100)
                    chg3 = abs((cl5[-3] - cl5[-4]) / cl5[-4] * 100)
                    if chg1 < chg2 < chg3:
                        result['momentum_decay'] = True  # 动量递减
                    else:
                        result['momentum_decay'] = False

                # === 量价背离检测（新）===
                # 价格创新高但成交量递减 → 假突破信号
                if len(cl5) >= 3 and len(vl5) >= 3:
                    price_higher = cl5[-1] > cl5[-2] > cl5[-3]
                    vol_lower = vl5[-1] < vl5[-2] < vl5[-3]
                    price_lower = cl5[-1] < cl5[-2] < cl5[-3]
                    # 上涨时量缩 = 顶背离；下跌时量缩 = 底背离（对做空是风险）
                    if price_higher and vol_lower:
                        result['vol_price_diverge'] = 'TOP'  # 顶背离：价涨量缩
                    elif price_lower and vol_lower:
                        result['vol_price_diverge'] = 'BOTTOM'  # 底背离：价跌量缩
                    else:
                        result['vol_price_diverge'] = 'NONE'

                # === v7.7新增技术指标（基于5m K线）===
                highs5 = [float(c[2]) for c in c5]
                lows5 = [float(c[3]) for c in c5]

                # === EMA(12,26) 排列（v7.8新增：趋势骨架）===
                if len(cl5) >= 26:
                    def _ema(data, period):
                        k = 2 / (period + 1)
                        ema_val = sum(data[:period]) / period
                        for v in data[period:]:
                            ema_val = v * k + ema_val * (1 - k)
                        return ema_val
                    ema12 = _ema(cl5, 12)
                    ema26 = _ema(cl5, 26)
                    result['ema12'] = ema12
                    result['ema26'] = ema26
                    result['ema_bullish'] = ema12 > ema26
                    result['ema_bearish'] = ema12 < ema26

                # === 5m成交量均值（v7.8新增：确认资金参与）===
                if len(vl5) >= 5:
                    avg_vol_5m = sum(vl5[:-2]) / max(len(vl5[:-2]), 1)
                    recent_vol_5m = sum(vl5[-2:]) / 2
                    result['vol_5m_ratio'] = recent_vol_5m / max(avg_vol_5m, 0.001)

                # ADX(14) — 趋势强度（>40趋势过强，做空危险）
                if len(highs5) >= 29:
                    _plus_dm, _minus_dm, _trs = [], [], []
                    for _i in range(1, len(highs5)):
                        _up = highs5[_i] - highs5[_i-1]
                        _down = lows5[_i-1] - lows5[_i]
                        _plus_dm.append(_up if _up > _down and _up > 0 else 0)
                        _minus_dm.append(_down if _down > _up and _down > 0 else 0)
                        _tr = max(highs5[_i]-lows5[_i], abs(highs5[_i]-cl5[_i-1]), abs(lows5[_i]-cl5[_i-1]))
                        _trs.append(_tr)
                    _atr = sum(_trs[:14]) / 14
                    _pdi_s = sum(_plus_dm[:14]) / 14
                    _mdi_s = sum(_minus_dm[:14]) / 14
                    _dxs = []
                    for _i in range(14, len(_trs)):
                        _atr = _atr * 13/14 + _trs[_i]/14
                        _pdi_s = _pdi_s * 13/14 + _plus_dm[_i]/14
                        _mdi_s = _mdi_s * 13/14 + _minus_dm[_i]/14
                        if _atr == 0: continue
                        _pdi = 100 * _pdi_s / _atr
                        _mdi = 100 * _mdi_s / _atr
                        if _pdi + _mdi == 0: _dxs.append(0)
                        else: _dxs.append(100 * abs(_pdi - _mdi) / (_pdi + _mdi))
                    if len(_dxs) >= 14:
                        _adx = sum(_dxs[:14]) / 14
                        for _i in range(14, len(_dxs)):
                            _adx = (_adx * 13 + _dxs[_i]) / 14
                        result['adx_14'] = _adx

                # BB宽度(20) — 布林带宽度百分比（太窄无空间，太宽风险大）
                if len(cl5) >= 20:
                    _bb_closes = cl5[-20:]
                    _bb_sma = sum(_bb_closes) / 20
                    _bb_std = (sum((c - _bb_sma)**2 for c in _bb_closes) / 20) ** 0.5
                    if _bb_sma > 0:
                        result['bb_width'] = (2 * _bb_std * 2) / _bb_sma * 100

                # ROC(10) — 价格变化率（确认趋势方向）
                if len(cl5) >= 11 and cl5[-11] > 0:
                    result['roc_10'] = (cl5[-1] - cl5[-11]) / cl5[-11] * 100

                # Stochastic K(14) — 随机指标
                if len(highs5) >= 14:
                    _k_period = 14
                    _h = max(highs5[-_k_period:])
                    _l = min(lows5[-_k_period:])
                    if _h != _l:
                        result['stoch_k'] = (cl5[-1] - _l) / (_h - _l) * 100
                    else:
                        result['stoch_k'] = 50

        # 15分钟趋势（v8.0E：使用 Heikin-Ashi 平均K线 + Bar Close 确认）
        if c15m.get("data") and len(c15m["data"]) >= 5:
            c15 = c15m["data"][::-1]
            cl15 = [float(c[4]) for c in c15]
            op15 = [float(c[1]) for c in c15]
            hi15 = [float(c[2]) for c in c15]
            lo15 = [float(c[3]) for c in c15]

            # v8.0E: Heikin-Ashi 计算（15m用已收盘K线，避免正在形成的K线骗信号）
            # Bar Close 确认：用 cl15[-2] 作为最新"已收盘"数据，cl15[-1] 是正在形成的
            ha_close_list = []
            ha_open_list = []
            for i in range(len(cl15)):
                ha_close = (op15[i] + hi15[i] + lo15[i] + cl15[i]) / 4
                ha_close_list.append(ha_close)
                if i == 0:
                    ha_open_list.append((op15[i] + cl15[i]) / 2)
                else:
                    ha_open_list.append((ha_open_list[i-1] + ha_close_list[i-1]) / 2)

            # 最近3根已收盘 HA K线的方向（排除正在形成的最后一根）
            ha_last3_up = sum(1 for i in range(-4, -1) if ha_close_list[i] > ha_open_list[i])
            ha_last3_down = sum(1 for i in range(-4, -1) if ha_close_list[i] < ha_open_list[i])
            result['ha_15m_ups'] = ha_last3_up
            result['ha_15m_downs'] = ha_last3_down

            # 使用 shift=1（已收盘K线）计算3根涨跌
            chg_15m_recent = (cl15[-2] - cl15[-3]) / cl15[-3] * 100 if len(cl15) >= 3 else 0
            chg_15m_3 = (cl15[-2] - cl15[-5]) / cl15[-5] * 100 if len(cl15) >= 5 else 0
            result['chg_15m_recent'] = chg_15m_recent
            result['chg_15m_3'] = chg_15m_3

            # v8.0E: 15m趋势需满足 (HA连续同向 ≥ 2根) + (价格净变化>0.3%)
            if ha_last3_up >= 2 and chg_15m_3 > 0.3:
                result['trend_15m'] = 'UP'
            elif ha_last3_down >= 2 and chg_15m_3 < -0.3:
                result['trend_15m'] = 'DOWN'
            else:
                result['trend_15m'] = 'FLAT'

            # v8.0E: 15m RSI（用于多TF加权评分）
            if len(cl15) >= 15:
                deltas15 = [cl15[i] - cl15[i-1] for i in range(1, min(15, len(cl15)))]
                gains15 = [d if d > 0 else 0 for d in deltas15]
                losses15 = [-d if d < 0 else 0 for d in deltas15]
                avg_g15 = sum(gains15) / max(len(gains15), 1)
                avg_l15 = sum(losses15) / max(len(losses15), 1)
                if avg_l15 > 0:
                    rs15 = avg_g15 / avg_l15
                    result['rsi_15m'] = 100 - (100 / (1 + rs15))
                else:
                    result['rsi_15m'] = 100

        # v8.0E: 5m RSI（用于多TF加权评分）
        if c5m.get("data") and len(c5m["data"]) >= 15:
            c5_data = c5m["data"][::-1]
            cl5_data = [float(c[4]) for c in c5_data]
            deltas5 = [cl5_data[i] - cl5_data[i-1] for i in range(1, 15)]
            gains5 = [d if d > 0 else 0 for d in deltas5]
            losses5 = [-d if d < 0 else 0 for d in deltas5]
            avg_g5 = sum(gains5) / 14
            avg_l5 = sum(losses5) / 14
            if avg_l5 > 0:
                rs5 = avg_g5 / avg_l5
                result['rsi_5m'] = 100 - (100 / (1 + rs5))
            else:
                result['rsi_5m'] = 100

        # v8.0E: ATR(14) on 5m — 用于动态RSI阈值
        if c5m.get("data") and len(c5m["data"]) >= 15:
            c5_d = c5m["data"][::-1]
            cl5_d = [float(c[4]) for c in c5_d]
            hi5_d = [float(c[2]) for c in c5_d]
            lo5_d = [float(c[3]) for c in c5_d]
            trs = []
            for i in range(1, 15):
                tr = max(hi5_d[i] - lo5_d[i],
                         abs(hi5_d[i] - cl5_d[i-1]),
                         abs(lo5_d[i] - cl5_d[i-1]))
                trs.append(tr)
            atr14 = sum(trs) / 14
            # ATR 占价格的百分比（波动率代理）
            if cl5_d[-1] > 0:
                result['atr_pct'] = atr14 / cl5_d[-1] * 100

        # v8.0E: 插针检测（1m K线影线/实体比 > 3:1 视为插针）
        if c1m.get("data") and len(c1m["data"]) >= 3:
            c1_d = c1m["data"][::-1]
            last_1m = c1_d[-1]
            op1m = float(last_1m[1])
            hi1m = float(last_1m[2])
            lo1m = float(last_1m[3])
            cl1m = float(last_1m[4])
            body = abs(cl1m - op1m)
            upper_wick = hi1m - max(op1m, cl1m)
            lower_wick = min(op1m, cl1m) - lo1m
            if body > 0:
                upper_ratio = upper_wick / body
                lower_ratio = lower_wick / body
                result['wick_upper_ratio'] = upper_ratio
                result['wick_lower_ratio'] = lower_ratio
                # 上影线过长 = 假突破（做多风险）
                result['pin_bar_top'] = upper_ratio > 3 and body > 0
                # 下影线过长 = 假跌破（做空风险）
                result['pin_bar_bottom'] = lower_ratio > 3 and body > 0
            else:
                result['pin_bar_top'] = False
                result['pin_bar_bottom'] = False

        return result
    except:
        return {}

def calculate_momentum_score(chg_5m, vol_1m, ups, downs, chain_bonus, fr, trend_15m=None, chg_5m_dir=None, mt_data=None):
    """通道B综合评分 v8.1（多TF加权RSI + ATR动态阈值 + 插针过滤 + 自适应因子权重）"""
    score = 0

    # ===== v8.1: 获取因子权重（Thompson Sampling采样）=====
    factor_weights = adaptive.bandit.sample_weights()

    # ===== v8.0E 新增：多TF RSI加权评分 =====
    # 根据MQL5案例：1m(20%) + 5m(30%) + 15m(50%)，高TF权重大
    rsi_1m = mt_data.get("rsi_14", 50) if mt_data else 50
    rsi_5m = mt_data.get("rsi_5m", 50) if mt_data else 50
    rsi_15m = mt_data.get("rsi_15m", 50) if mt_data else 50
    atr_pct = mt_data.get("atr_pct", 1.0) if mt_data else 1.0

    # ATR动态阈值：高波动（ATR>2%）时放宽阈值，低波动时收紧
    if atr_pct > 2.0:
        rsi_high = 75
        rsi_low = 25
    elif atr_pct > 1.0:
        rsi_high = 70
        rsi_low = 30
    else:
        rsi_high = 65
        rsi_low = 35

    # 加权RSI评分（偏离50的程度，用于判断趋势强度）
    weighted_rsi = rsi_1m * 0.2 + rsi_5m * 0.3 + rsi_15m * 0.5

    # 0. 15m趋势一致性检查（一票否决，保留）
    if trend_15m and chg_5m_dir:
        if chg_5m_dir == "LONG" and trend_15m == "DOWN":
            return 0
        if chg_5m_dir == "SHORT" and trend_15m == "UP":
            return 0

    # 0b. 多TF加权RSI一票否决（替代原来的单TF RSI硬阈值）
    if chg_5m_dir == "LONG" and weighted_rsi > rsi_high + 5:
        return 0  # 加权RSI过高（超极端区），做多危险
    if chg_5m_dir == "SHORT" and weighted_rsi < rsi_low - 5:
        return 0  # 加权RSI过低，做空危险

    # 0c. 1m反转检测（一票否决）
    if mt_data:
        last3_up = mt_data.get("last3_1m_ups", 1)
        last3_down = mt_data.get("last3_1m_downs", 1)
        if chg_5m_dir == "LONG" and last3_down >= 3:
            return 0
        if chg_5m_dir == "SHORT" and last3_up >= 3:
            return 0

    # 0d. 动量衰减（一票否决）
    if mt_data and mt_data.get("momentum_decay"):
        return 0

    # 0e. 量价背离（一票否决）
    if mt_data:
        diverge = mt_data.get("vol_price_diverge", "NONE")
        if chg_5m_dir == "LONG" and diverge == "TOP":
            return 0
        if chg_5m_dir == "SHORT" and diverge == "BOTTOM":
            return 0

    # 0f. v8.0E: 插针过滤（一票否决）
    if mt_data:
        if chg_5m_dir == "LONG" and mt_data.get("pin_bar_top"):
            return 0  # 上影线过长=假突破，做多危险
        if chg_5m_dir == "SHORT" and mt_data.get("pin_bar_bottom"):
            return 0  # 下影线过长=假跌破，做空危险

    # 0g. 5m连续确认（加分）
    consecutive_bonus = 0
    if mt_data:
        consec = mt_data.get("5m_consecutive", "NONE")
        if chg_5m_dir == "LONG" and consec == "UP":
            consecutive_bonus = 1
        elif chg_5m_dir == "SHORT" and consec == "DOWN":
            consecutive_bonus = 1

    # 1. 5m涨跌幅度 (1-3分)
    abs_chg = abs(chg_5m)
    if abs_chg > 2:
        score += 3
    elif abs_chg > 1:
        score += 2
    elif abs_chg > 0.5:
        score += 1

    # 2. 1m放量倍数 (1-3分)
    if vol_1m > 3:
        score += 3
    elif vol_1m > 2:
        score += 2
    elif vol_1m > 1.5:
        score += 1

    # 3. 1m趋势一致性 (1-2分)
    if ups > 12 or downs > 12:
        score += 2
    elif ups > 10 or downs > 10:
        score += 1

    # 4. 链上信号 (0-11分)
    score += chain_bonus

    # 5. FR方向一致 (+1分)
    if fr is not None:
        if (fr < 0 and chg_5m > 0) or (fr > 0 and chg_5m < 0):
            score += 1

    # 6. 15m趋势强度 (+1分)
    if trend_15m and chg_5m_dir:
        if chg_5m_dir == "LONG" and trend_15m == "UP":
            score += 1
        elif chg_5m_dir == "SHORT" and trend_15m == "DOWN":
            score += 1

    # 7. 5m连续确认 (+1分)
    score += consecutive_bonus

    # 8. v8.0E: 多TF RSI加权评分加分（在理想区间+2分，趋势延续信号）
    if chg_5m_dir == "LONG":
        # 做多理想区间：加权RSI 40-65（上升趋势中，不超买）
        if 40 <= weighted_rsi <= 65:
            score += 2
        elif 30 <= weighted_rsi < 40:
            score += 1  # 轻度超卖反弹
    elif chg_5m_dir == "SHORT":
        # 做空理想区间：加权RSI 35-60
        if 35 <= weighted_rsi <= 60:
            score += 2
        elif 60 < weighted_rsi <= 70:
            score += 1  # 轻度超买回落

    return score

def check_short_safety(mt_data):
    """做空安全检查：RSI<65且15m趋势向下才允许做空"""
    rsi = mt_data.get("rsi_14", 50)
    trend = mt_data.get("trend_15m", "FLAT")
    if rsi > 65:
        return False, f"RSI={rsi:.0f}>65"
    if trend == "UP":
        return False, "15m趋势向上"
    return True, "ok"

def recalc_position_score(inst_id, pos_info, mt_data_map, fr_map):
    """每轮重新计算持仓的实时评分（考虑当前K线+FR状态）"""
    channel = pos_info.get("channel", "?")
    direction = pos_info.get("direction", "?")
    ticker = inst_id.replace("-USDT-SWAP", "")
    
    # 获取当前数据
    mt = mt_data_map.get(inst_id, {})
    fr = fr_map.get(inst_id)
    chain_bonus, chain_tags = get_chain_score(ticker)
    
    if channel == "A":
        # 通道A评分 = FR强度 + 持久性 + 链上 + 放量
        if fr is None:
            return pos_info.get("score", 0), chain_tags
        abs_fr = abs(fr)
        if abs_fr < FR_EXTREME_THRESHOLD:
            return 0, chain_tags  # FR已消失，评分归零
        vol_1m = mt.get("vol_1m", 0)
        score = abs_fr * 10000 + chain_bonus
        if vol_1m >= CHANNEL_A_VOL_SPIKE:
            score += 1
        return score, chain_tags
    
    elif channel == "B":
        # 通道B重新计算完整评分
        if "chg_5m" not in mt:
            return pos_info.get("score", 0), chain_tags
        
        chg_5m = mt.get("chg_5m", 0)
        vol_1m = mt.get("vol_1m", 0)
        ups = mt.get("ups", 0)
        downs = mt.get("downs", 0)
        trend_15m = mt.get("trend_15m", None)
        
        # 检查方向是否反转
        if direction == "LONG" and chg_5m < -0.1:
            return 0, chain_tags  # 5m已转跌，该跑了
        if direction == "SHORT" and chg_5m > 0.1:
            return 0, chain_tags  # 5m已转涨，该跑了
        
        score = calculate_momentum_score(
            chg_5m, vol_1m, ups, downs, chain_bonus, fr,
            trend_15m, direction, mt
        )
        return score, chain_tags
    
    return pos_info.get("score", 0), []

# ==================== 双通道扫描 ====================
def scan_dual_channel():
    """双通道扫描：通道A(极端FR) + 通道B(动量顺势)"""
    # 1. 获取所有USDT-SWAP行情
    d = curl_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP", 15)
    if not d.get("data"):
        return [], [], {}, {}

    usdt = []
    for x in d["data"]:
        if not x["instId"].endswith("USDT-SWAP"):
            continue
        vol24h = float(x.get("volCcy24h", "0"))
        last = float(x.get("last", "0"))
        if vol24h < MIN_24H_VOL or last < MIN_PRICE:
            continue
        usdt.append({
            "instId": x["instId"],
            "last": last,
            "vol24h": vol24h,
        })

    top = sorted(usdt, key=lambda x: x["vol24h"], reverse=True)[:SCAN_TOP_N]
    top = [x for x in top if x["instId"] not in SKIP_SYMS]
    syms = [x["instId"] for x in top]

    # 2. 并行获取资金费率
    fr_map = get_funding_rates_batch(syms)

    # 3. 并行获取K线数据（候选 + 持仓，都要K线数据）
    kline_syms = list(set(syms[:30] + [p for p in positions if p in syms]))
    mt_data = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(get_multi_timeframe, s): s for s in kline_syms}
        for f in as_completed(futures):
            sym = futures[f]
            try:
                mt_data[sym] = f.result()
            except:
                pass

    # 4. 更新链上缓存
    update_chain_cache()

    # 5. 构建候选列表
    channel_a_candidates = []  # 极端FR反向
    channel_b_candidates = []  # 动量顺势

    for item in top:
        sym = item["instId"]
        ticker = sym.replace("-USDT-SWAP", "")
        fr = fr_map.get(sym)
        mt = mt_data.get(sym, {})

        chain_bonus, chain_tags = get_chain_score(ticker)

        # ===== 通道A: 极端FR + 技术指标验证 + ADX/BB过滤（v7.7）======
        if fr is not None:
            abs_fr = abs(fr)
            if FR_EXTREME_THRESHOLD <= abs_fr <= FR_MAX_THRESHOLD:
                vol_1m_a = mt.get("vol_1m", 0)
                if vol_1m_a >= CHANNEL_A_VOL_SPIKE:
                    chg_5m_a = mt.get("chg_5m", 0)
                    trend_15m_a = mt.get("trend_15m", "FLAT")
                    rsi_a = mt.get("rsi_14", 50)
                    adx_a = mt.get("adx_14", 25)  # 无数据时默认25，不触发>=40过滤
                    bb_a = mt.get("bb_width", 3.0)  # 无数据时默认3%，不触发<1.5%过滤
                    
                    # v7.7: ADX过滤 — 趋势过强做空危险
                    if adx_a >= 40:
                        continue
                    # v7.7: BB宽度过滤 — 太窄无空间，太宽风险大
                    if bb_a < 1.5:
                        continue
                    
                    # v7.8: Stochastic位置过滤（避免追极端）
                    stoch_a = mt.get("stoch_k", 50)

                    # v8.0E: 插针过滤
                    pin_top_a = mt.get("pin_bar_top", False)
                    pin_bot_a = mt.get("pin_bar_bottom", False)

                    # FR<0做多：需要5m上涨或15m向上
                    if fr < 0 and (chg_5m_a > 0.1 or trend_15m_a == "UP"):
                        # 做多额外过滤：RSI不能超买 + Stochastic不能超买 + 不是插针顶
                        if rsi_a > 70:
                            continue
                        if stoch_a > 80:
                            continue
                        if pin_top_a:
                            continue  # v8.0E: 上影线过长=假突破，做多危险
                        direction_a = "LONG"
                    # FR>0做空：需要5m下跌且15m向下
                    elif fr > 0 and chg_5m_a < -0.1 and trend_15m_a == "DOWN":
                        # 做空额外过滤：RSI不能超卖 + Stochastic不能超卖 + 不是插针底
                        if rsi_a < 30:
                            continue
                        if stoch_a < 20:
                            continue
                        if pin_bot_a:
                            continue  # v8.0E: 下影线过长=假跌破，做空危险
                        # v7.7: ROC确认方向（价格已在跌）
                        roc_a = mt.get("roc_10", 0)
                        if roc_a > 0:
                            continue
                        direction_a = "SHORT"
                    else:
                        continue
                    
                    # 跟踪FR持久性
                    now = time.time()
                    prev = FR_HISTORY.get(sym)
                    persistence_bonus = 0
                    if prev and (now - prev["ts"]) < FR_HISTORY_TTL:
                        if (prev["fr"] < 0 and fr < 0) or (prev["fr"] > 0 and fr > 0):
                            persistence_bonus = 1
                    FR_HISTORY[sym] = {"fr": fr, "ts": now}

                    channel_a_candidates.append({
                        "sym": sym,
                        "dir": direction_a,
                        "fr": fr,
                        "abs_fr": abs_fr,
                        "vol24h": item["vol24h"],
                        "score": abs_fr * 10000 + persistence_bonus + chain_bonus,
                        "channel": "A",
                        "chain_tags": chain_tags,
                        "position_pct": CHANNEL_A_POSITION_PCT,
                    })

        # ===== 通道B: 动量顺势 =====
        if mt and "chg_5m" in mt:
            chg_5m = mt.get("chg_5m", 0)
            vol_1m = mt.get("vol_1m", 0)
            ups = mt.get("ups", 0)
            downs = mt.get("downs", 0)
            trend_15m = mt.get("trend_15m", None)

            # 先确定方向
            if chg_5m > 0:
                direction = "LONG"
            elif chg_5m < 0:
                direction = "SHORT"
            else:
                continue

            # v8.0C: 链上至少1个因子
            if count_chain_factors(chain_tags) < MIN_CHAIN_FACTORS:
                continue

            # 评分（传入15m趋势+完整mt_data做多维度过滤）
            mom_score = calculate_momentum_score(chg_5m, vol_1m, ups, downs, chain_bonus, fr, trend_15m, direction, mt)

            if mom_score >= MOMENTUM_SCORE_THRESHOLD:
                # 做空安全检查（方案6）
                if direction == "SHORT":
                    safe, reason = check_short_safety(mt)
                    if not safe:
                        continue
                # v7.7: ADX过滤 — 趋势过强做空危险
                adx_b = mt.get("adx_14", 25)  # 无数据时默认25，不触发>=40过滤
                if adx_b >= 40:
                    continue
                # v7.7: BB宽度过滤 — 太窄无空间
                bb_b = mt.get("bb_width", 3.0)  # 无数据时默认3%，不触发<1.5%过滤
                if bb_b < 1.5:
                    continue
                # v7.7: ROC确认方向（做空需已在跌，做多需已在涨）
                roc_b = mt.get("roc_10", 0)
                if direction == "SHORT" and roc_b > 0:
                    continue
                if direction == "LONG" and roc_b < 0:
                    continue
                # 15m趋势必须与开仓方向一致（降噪：避免小周期假突破）
                if trend_15m:
                    if direction == "LONG" and trend_15m != "UP":
                        continue
                    if direction == "SHORT" and trend_15m != "DOWN":
                        continue
                # v7.8: EMA排列过滤
                ema_bull = mt.get('ema_bullish')
                ema_bear = mt.get('ema_bearish')
                if ema_bull is None and ema_bear is None:
                    continue
                if direction == "LONG" and not ema_bull:
                    continue
                if direction == "SHORT" and not ema_bear:
                    continue
                # v7.8: 5m量能确认
                vol_5m_ratio = mt.get('vol_5m_ratio', 1.0)
                if vol_5m_ratio < 1.1:
                    continue
                # v7.8: Stochastic入场时机
                stoch_k = mt.get('stoch_k', 50)
                if direction == "LONG" and stoch_k > 80:
                    continue
                if direction == "SHORT" and stoch_k < 20:
                    continue
                channel_b_candidates.append({
                    "sym": sym,
                    "dir": direction,
                    "fr": fr,
                    "vol24h": item["vol24h"],
                    "chg_5m": chg_5m,
                    "vol_1m": vol_1m,
                    "score": mom_score,
                    "channel": "B",
                    "chain_tags": chain_tags,
                    "position_pct": CHANNEL_B_POSITION_PCT,
                    "trend_15m": trend_15m,
                })

    # 排序
    channel_a_candidates.sort(key=lambda x: x["score"], reverse=True)
    channel_b_candidates.sort(key=lambda x: x["score"], reverse=True)

    # v8.1: 收集ATR数据更新市场状态检测
    atr_pcts = [mt_data[s].get("atr_pct", 1.0) for s in mt_data if "atr_pct" in mt_data[s]]
    adaptive.update_regime(atr_pcts)

    # v8.1: 自适应过滤 — 品种冷却 + 动态门槛
    filtered_a = []
    for c in channel_a_candidates:
        sym = c["sym"]
        # 品种冷却检查
        if adaptive.is_symbol_blocked(sym):
            continue
        # 动态门槛：通道A用 score 与自适应门槛比较
        adj_threshold = adaptive.get_adjusted_threshold(FR_EXTREME_THRESHOLD * 10000, sym, "A")
        if c["score"] < adj_threshold:
            continue
        filtered_a.append(c)
    channel_a_candidates = filtered_a

    filtered_b = []
    for c in channel_b_candidates:
        sym = c["sym"]
        # 品种冷却检查
        if adaptive.is_symbol_blocked(sym):
            continue
        # 动态门槛
        adj_threshold = adaptive.get_adjusted_threshold(MOMENTUM_SCORE_THRESHOLD, sym, "B")
        if c["score"] < adj_threshold:
            continue
        filtered_b.append(c)
    channel_b_candidates = filtered_b

    return channel_a_candidates, channel_b_candidates, mt_data, fr_map

# ==================== 交易操作 ====================
def get_balance():
    try:
        d = okx_get("/api/v5/account/balance?ccy=USDT")
        if d.get("data") and d["data"][0].get("details") and len(d["data"][0]["details"]) > 0:
            det = d["data"][0]["details"][0]
            return float(det.get("availBal") or det.get("eq") or 0)
    except Exception as e:
        log(f"⚠️ get_balance异常: {e}")
    return 0

def get_position(inst_id):
    d = okx_get(f"/api/v5/account/positions?instId={inst_id}")
    if d.get("data"):
        for pos in d["data"]:
            if pos.get("instId") == inst_id and pos.get("pos") and float(pos["pos"]) != 0:
                return {
                    "pos": float(pos["pos"]),
                    "avgPx": float(pos["avgPx"]),
                    "upl": float(pos.get("upl", 0)),
                    "notionalUsd": float(pos.get("notionalUsd", 0)),
                }
    return None

def get_all_positions():
    d = okx_get("/api/v5/account/positions")
    result = []
    if d.get("data"):
        for pos in d["data"]:
            if pos.get("pos") and float(pos["pos"]) != 0:
                result.append({
                    "instId": pos["instId"],
                    "pos": float(pos["pos"]),
                    "avgPx": float(pos["avgPx"]),
                    "upl": float(pos.get("upl", 0)),
                })
    return result

def close_position(inst_id, algo_ids=None):
    pos = get_position(inst_id)
    if not pos:
        return {"code": "0", "msg": "already closed"}
    if algo_ids:
        for aid in algo_ids:
            okx_post("/api/v5/trade/cancel-algos", json.dumps([{"instId": inst_id, "algoId": aid}]))
    side = "sell" if pos["pos"] > 0 else "buy"
    r = okx_post("/api/v5/trade/order", json.dumps({
        "instId": inst_id, "tdMode": "cross",
        "side": side, "ordType": "market", "sz": str(int(abs(pos["pos"])))
    }))
    return r

def get_realized_pnl(inst_id):
    """查询OKX账单API获取最近一笔已实现盈亏（精确值）"""
    try:
        d = okx_get(f"/api/v5/account/bills?instId={inst_id}&instType=SWAP&limit=5")
        if d.get("data"):
            for bill in d["data"]:
                pnl = float(bill.get("realizedPnl", 0))
                fee = float(bill.get("fee", 0))
                # realizedPnl已经扣了手续费，所以直接用
                if pnl != 0:
                    return pnl, fee
        return 0, 0
    except:
        return 0, 0

def open_position(inst_id, direction, balance_for_trade, channel_label=""):
    """开仓 + 挂TP/SL"""
    specs = INSTRUMENTS_CACHE.get(inst_id)
    if not specs:
        log(f"❌ {inst_id} 缓存未命中")
        return None

    leverage = min(LEVERAGE, int(specs["maxLev"]))
    margin = balance_for_trade * 0.95
    notional_max = margin * leverage

    d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", 5)
    if not d.get("data"):
        return None
    price = float(d["data"][0]["last"])

    lots = int(notional_max / (specs["ctVal"] * price))
    if lots < 1:
        return None

    notional_actual = lots * specs["ctVal"] * price
    round_trip_fee_cost = notional_actual * ROUND_TRIP_FEE
    potential_tp_profit = notional_actual * TP_PCT
    if potential_tp_profit < round_trip_fee_cost:
        log(f"  ❌ {inst_id} TP利润${potential_tp_profit:.4f} < 手续费${round_trip_fee_cost:.4f} → 跳过")
        return None

    tick_sz = float(specs.get("tickSz", "0.00001"))
    tick_decimals = len(specs.get("tickSz", "0.00001").rstrip("0").split(".")[-1]) if "." in specs.get("tickSz", "0.00001") else 0

    def format_price(p):
        rounded = round(p / tick_sz) * tick_sz
        if tick_decimals > 0:
            return f"{rounded:.{tick_decimals}f}"
        return str(rounded)

    if direction == "LONG":
        tp = format_price(price * (1 + TP_PCT))
        sl = format_price(price * (1 - SL_PCT))
        side = "buy"
        close_side = "sell"
    else:
        tp = format_price(price * (1 - TP_PCT))
        sl = format_price(price * (1 + SL_PCT))
        side = "sell"
        close_side = "buy"

    tp_diff = abs(price * TP_PCT)
    sl_diff = abs(price * SL_PCT)
    if tp_diff < tick_sz * 2 or sl_diff < tick_sz * 2:
        log(f"  ❌ {inst_id} 价格太低(${price})，TP/SL差价不足 → 跳过")
        return None

    okx_post("/api/v5/account/set-leverage", json.dumps({
        "instId": inst_id, "lever": str(leverage), "mgnMode": "cross"
    }))

    result = okx_post("/api/v5/trade/order", json.dumps({
        "instId": inst_id, "tdMode": "cross",
        "side": side, "ordType": "market", "sz": str(lots)
    }))

    if result.get("code") != "0":
        log(f"❌ {inst_id} 开仓失败: code={result.get('code')} msg={result.get('msg', '')}")
        return None

    time.sleep(0.5)

    pos = get_position(inst_id)
    if not pos:
        log(f"❌ {inst_id} 获取持仓失败")
        return None

    avg = pos["avgPx"]
    sz = int(abs(pos["pos"]))

    # 挂TP+SL
    algo_ids = []
    placed_labels = set()
    lock = threading.Lock()

    def place_algo(label, px, trigger_key):
        ord_px_key = "tpOrdPx" if label == "TP" else "slOrdPx"
        body = {
            "instId": inst_id, "tdMode": "cross",
            "side": close_side, "sz": str(sz),
            "ordType": "conditional",
            trigger_key: px, ord_px_key: "-1",
            "reduceOnly": True
        }
        r = okx_post("/api/v5/trade/order-algo", json.dumps(body))
        if r.get("code") == "0":
            with lock:
                algo_ids.append(r["data"][0].get("algoId", ""))
                placed_labels.add(label)
        else:
            log(f"  ❌ {label} @ ${px} 失败: {r.get('msg','')}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(place_algo, "TP", tp, "tpTriggerPx")
        ex.submit(place_algo, "SL", sl, "slTriggerPx")
    time.sleep(0.3)

    tp_ok = "TP" in placed_labels
    sl_ok = "SL" in placed_labels
    if not tp_ok or not sl_ok:
        log(f"⚠️ {inst_id} TP={tp_ok} SL={sl_ok} (已挂: {placed_labels}) → 强制平仓")
        for close_attempt in range(3):
            time.sleep(1)
            close_position(inst_id, algo_ids)
            if not get_position(inst_id):
                log(f"✅ {inst_id} 裸仓已平仓")
                break
        else:
            log(f"🚨 {inst_id} 平仓3次失败！需手动处理")
        return None

    log(f"✅ [{channel_label}] 开{'多' if direction=='LONG' else '空'} {inst_id} {sz}张 @ ${avg} TP=${tp} SL=${sl}")
    return {
        "instId": inst_id, "direction": direction,
        "entry_price": avg, "sz": sz,
        "algo_ids": algo_ids, "open_time": time.time(),
        "notional": notional_actual, "trail_activated": False,
        "highest_pnl_pct": 0, "fr": 0,
        "last_upl": 0, "channel": channel_label,
        "score": 0,  # 开仓后由调用方设置
    }

# ==================== 持仓健康度评估 ====================
def calc_position_health(inst_id, pos_info, mt_data_map):
    """
    计算持仓信号健康度 (0-100)
    基于开仓时的技术指标是否仍然支持当前方向
    
    返回: health_score (0-100), reasons (list of str)
    """
    direction = pos_info.get("direction", "LONG")
    mt = mt_data_map.get(inst_id, {})
    reasons = []
    score = 50  # 基础分：无数据时中性
    
    if not mt:
        return score, ["无数据"]
    
    # === 1. 趋势一致性 (±25分) ===
    # 15m HA趋势是否和持仓方向一致
    trend_15m = mt.get("trend_15m", "FLAT")
    if direction == "LONG":
        if trend_15m == "UP":
            score += 25
            reasons.append("趋势↑")
        elif trend_15m == "DOWN":
            score -= 25
            reasons.append("趋势反转↓")
        # FLAT不加不减
    else:  # SHORT
        if trend_15m == "DOWN":
            score += 25
            reasons.append("趋势↓")
        elif trend_15m == "UP":
            score -= 25
            reasons.append("趋势反转↑")
    
    # === 2. EMA排列支撑 (±15分) ===
    ema_bull = mt.get("ema_bullish")
    ema_bear = mt.get("ema_bearish")
    if direction == "LONG":
        if ema_bull:
            score += 15
            reasons.append("EMA多")
        elif ema_bear:
            score -= 15
            reasons.append("EMA空")
    else:
        if ema_bear:
            score += 15
            reasons.append("EMA空")
        elif ema_bull:
            score -= 15
            reasons.append("EMA多")
    
    # === 3. 5分钟动量方向 (±20分) ===
    chg_5m = mt.get("chg_5m", 0)
    if direction == "LONG":
        if chg_5m > 0.3:
            score += 20
            reasons.append(f"5m+{chg_5m:.1f}%")
        elif chg_5m < -0.3:
            score -= 20
            reasons.append(f"5m{chg_5m:.1f}%")
    else:
        if chg_5m < -0.3:
            score += 20
            reasons.append(f"5m{chg_5m:.1f}%")
        elif chg_5m > 0.3:
            score -= 20
            reasons.append(f"5m+{chg_5m:.1f}%")
    
    # === 4. RSI反转区检测 (±15分) ===
    rsi_14 = mt.get("rsi_14", 50)
    if direction == "LONG":
        if rsi_14 > 75:
            score -= 15
            reasons.append(f"RSI超买{rsi_14:.0f}")
        elif rsi_14 < 35:
            score += 10  # 还有上涨空间
    else:
        if rsi_14 < 25:
            score -= 15
            reasons.append(f"RSI超卖{rsi_14:.0f}")
        elif rsi_14 > 65:
            score += 10
    
    # === 5. 放量反向检测 (±15分) ===
    vol_1m = mt.get("vol_1m", 1.0)
    last_5m_chg = mt.get("last_5m_chg", 0)
    if vol_1m > 1.5:  # 放量
        if direction == "LONG" and last_5m_chg < -0.2:
            score -= 15
            reasons.append(f"放量下跌")
        elif direction == "SHORT" and last_5m_chg > 0.2:
            score -= 15
            reasons.append(f"放量上涨")
        elif direction == "LONG" and last_5m_chg > 0.2:
            score += 10
            reasons.append(f"放量上涨")
        elif direction == "SHORT" and last_5m_chg < -0.2:
            score += 10
            reasons.append(f"放量下跌")
    
    # === 6. 动量衰减检测 (-10分) ===
    if mt.get("momentum_decay"):
        score -= 10
        reasons.append("动量衰减")
    
    # === 7. 量价背离检测 (-10分) ===
    vol_diverge = mt.get("vol_price_diverge", "NONE")
    if direction == "LONG" and vol_diverge == "TOP":
        score -= 10
        reasons.append("顶背离")
    elif direction == "SHORT" and vol_diverge == "BOTTOM":
        score -= 10
        reasons.append("底背离")
    
    # 限制在 [0, 100]
    score = max(0, min(100, score))
    return score, reasons


# ==================== 持仓监控 ====================
def monitor_position(inst_id, pos_info):
    entry = pos_info["entry_price"]
    direction = pos_info["direction"]
    open_time = pos_info["open_time"]

    pos = get_position(inst_id)
    if not pos:
        return "CLOSED"

    d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", 5)
    if not d.get("data"):
        log(f"  ⚠️ {inst_id} ticker API失败，本轮跳过监控")
        return "HOLDING"
    current_price = float(d["data"][0]["last"])

    upl = pos["upl"]
    pos_info["last_upl"] = upl
    elapsed = time.time() - open_time

    if direction == "LONG":
        pct_change = (current_price - entry) / entry * 100
    else:
        pct_change = (entry - current_price) / entry * 100

    if pct_change > pos_info.get("highest_pnl_pct", 0):
        pos_info["highest_pnl_pct"] = pct_change

    # === v8.1.1: 获取持仓健康度（由主循环扫描阶段写入） ===
    health = pos_info.get("health_score", 50)
    
    # === 根据健康度动态调整追踪止损参数 ===
    if health >= 70:
        # 信号强：放宽追踪参数，让利润跑
        trail_activate_pct = TRAIL_ACTIVATE * 100 * 1.3   # 1.3% 才激活
        trail_distance_pct = TRAIL_DISTANCE * 100 * 1.4   # 0.7% 回撤距离
    elif health >= 40:
        # 信号中性：使用默认参数
        trail_activate_pct = TRAIL_ACTIVATE * 100          # 1.0%
        trail_distance_pct = TRAIL_DISTANCE * 100          # 0.5%
    else:
        # 信号恶化：收紧追踪，快速锁利
        trail_activate_pct = TRAIL_ACTIVATE * 100 * 0.7   # 0.7% 就激活
        trail_distance_pct = TRAIL_DISTANCE * 100 * 0.6   # 0.3% 回撤就走

    # 追踪止损（使用动态参数）
    if pos_info.get("trail_activated"):
        trail_stop_pct = pos_info["highest_pnl_pct"] - trail_distance_pct
        if pct_change < trail_stop_pct:
            log(f"🔄 {inst_id} 追踪止损! 最高{pos_info['highest_pnl_pct']:+.2f}% → 当前{pct_change:+.2f}% (止损线{trail_stop_pct:+.2f}%) 健康={health}")
            close_position(inst_id, pos_info.get("algo_ids"))
            return "TRAIL_STOP"
    elif pct_change >= trail_activate_pct:
        pos_info["trail_activated"] = True
        log(f"🔔 {inst_id} 追踪止损激活! 浮盈{pct_change:+.2f}% > {trail_activate_pct:.1f}% 健康={health}")

    # 紧急止盈（6%）
    emergency_tp = 6.0
    if pct_change >= emergency_tp:
        log(f"🚀 {inst_id} 紧急止盈{pct_change:+.3f}% >= {emergency_tp}% → 立即落袋")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "PROFIT"

    # === v8.1.1: 信号恶化快速退出（替代部分P1/P3逻辑） ===
    # 健康度极低 + 已亏损 → 不等P1/P3，直接出局
    if health < 25 and pct_change < -0.3 and elapsed > 120:
        log(f"⚠️ {inst_id} 信号恶化(健康={health}) + 浮亏{pct_change:+.2f}% → 提前出局 [{','.join(pos_info.get('health_reasons',[]))}]")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "TIME_STOP"

    # P1: 浮亏>1% 快速止损（保留，作为兜底）
    if elapsed > TIME_STOP_SEC and pct_change < -1.0:
        log(f"⏰ {inst_id} P1: {int(elapsed)}s 浮亏{pct_change:+.2f}%>1% → 快速止损")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "TIME_STOP"

    # P3: 横盘止损（根据健康度动态调整时间和阈值）
    if health >= 60:
        # 信号健康：延长P3到30分钟，阈值放宽到0.2%
        p3_time = 1800
        p3_threshold = 0.2
    elif health >= 40:
        # 信号中性：标准P3
        p3_time = TIME_STOP_P3_SEC
        p3_threshold = 0.3
    else:
        # 信号差：缩短P3到15分钟
        p3_time = 900
        p3_threshold = 0.3

    if elapsed > p3_time:
        if pct_change < p3_threshold:
            log(f"⏰ {inst_id} P3: {int(elapsed)}s 横盘{pct_change:+.2f}%<{p3_threshold}% → 出局 健康={health}")
            close_position(inst_id, pos_info.get("algo_ids"))
            return "TIME_STOP"
        if pct_change >= 0.5 and not pos_info.get("trail_activated"):
            pos_info["trail_activated"] = True
            pos_info["highest_pnl_pct"] = pct_change
            log(f"🔔 {inst_id} {int(elapsed/60)}分钟+浮盈{pct_change:+.2f}% → 保本追踪激活")

    return "HOLDING"

# ==================== 主循环 ====================
def main():
    log("=" * 60)
    log("🔥 OKX 妖币猎手 v8.2 启动（持仓健康度进化 — 信号监控+动态退出+三层自适应）")
    log(f"  杠杆: {LEVERAGE}x  TP: {TP_PCT*100}%  SL: {SL_PCT*100}%")
    log(f"  通道A: |FR|>{FR_EXTREME_THRESHOLD*100}%+技术验证+插针过滤 仓位{CHANNEL_A_POSITION_PCT*100:.0f}%")
    log(f"  通道B: 动量>={MOMENTUM_SCORE_THRESHOLD}+多TF加权RSI+ATR动态阈值 仓位{CHANNEL_B_POSITION_PCT*100:.0f}%")
    log(f"  v8.1: 三层自适应 | ATR状态检测 | Thompson Sampling因子权重 | 品种/时段追踪")
    log(f"  追踪止损: 激活{TRAIL_ACTIVATE*100}% 距离{TRAIL_DISTANCE*100}%")
    log(f"  时间止损: P1={TIME_STOP_SEC}s(亏>1%) P3={TIME_STOP_P3_SEC}s(横盘<0.3%)")
    log(f"  黑名单: 连亏{BLACKLIST_LOSS_COUNT}次→拉黑{BLACKLIST_DURATION//3600}h | 冷却{COOLDOWN_SEC//3600}h")
    log(f"  最大同时持仓: {MAX_CONCURRENT}")
    log("=" * 60)

    load_instruments_cache()
    load_chain_cache()  # 启动时从磁盘恢复链上缓存
    state = load_state()
    scan_count = 0

    # 恢复已有持仓
    log("📥 加载已有持仓和挂单...")
    all_pending_algos = okx_get("/api/v5/trade/orders-algo-pending?ordType=conditional")
    algo_map = {}
    if all_pending_algos.get("data"):
        for a in all_pending_algos["data"]:
            aid_inst = a.get("instId", "")
            if aid_inst not in algo_map:
                algo_map[aid_inst] = []
            algo_map[aid_inst].append(a["algoId"])

    # 重启前先查FR数据，用于判断恢复持仓的通道来源
    # 这样可以正确分配channel而不是用"?"
    # 重启前查询已保存的仓位通道信息
    _saved_pos_meta = state.get("position_meta", {})
    _restore_fr_map = {}
    _restore_existing = get_all_positions()
    if _restore_existing:
        _restore_syms = [p["instId"] for p in _restore_existing]
        _restore_fr_map = get_funding_rates_batch(_restore_syms)

    existing = _restore_existing
    protected_inst_ids = set()
    if existing:
        for p in existing:
            inst_id = p["instId"]
            protected_inst_ids.add(inst_id)
            pos_val = p["pos"]
            direction = "LONG" if pos_val > 0 else "SHORT"
            entry_price = p["avgPx"]
            specs = INSTRUMENTS_CACHE.get(inst_id, {})
            notional = abs(pos_val) * specs.get("ctVal", 1) * entry_price
            matched_algos = algo_map.get(inst_id, [])
            # 通过保存的元数据>FR判断恢复持仓的通道来源
            restored_channel = "?"
            saved_meta = _saved_pos_meta.get(inst_id, {})
            if saved_meta.get("channel"):
                restored_channel = saved_meta["channel"]
            else:
                restored_fr = _restore_fr_map.get(inst_id)
                if restored_fr is not None:
                    if abs(restored_fr) >= FR_EXTREME_THRESHOLD and restored_fr < 0:
                        restored_channel = "A"
                    else:
                        restored_channel = "B"
            positions[inst_id] = {
                "instId": inst_id, "direction": direction,
                "entry_price": entry_price, "sz": int(abs(pos_val)),
                "algo_ids": matched_algos, "open_time": time.time(),
                "notional": notional, "trail_activated": False,
                "highest_pnl_pct": 0, "fr": _restore_fr_map.get(inst_id) or 0,
                "last_upl": float(p.get("upl", 0)), "channel": restored_channel,
                "score": saved_meta.get("score", 0),
                "source_channel": saved_meta.get("source_channel", ""),
            }
            algo_status = f" algo={len(matched_algos)}" if matched_algos else " ⚠️无挂单"
            log(f"  📥 {inst_id} {direction} {int(abs(pos_val))}张 @ ${entry_price}{algo_status}")
        log(f"  共加载 {len(existing)} 个持仓")

    # 清理重复通道分配（重启时可能2个持仓都被分配为channel="A"）
    channel_counts = {}
    for pid, pinfo in positions.items():
        ch = pinfo.get("channel", "?")
        if ch not in channel_counts:
            channel_counts[ch] = []
        channel_counts[ch].append(pid)
    for ch, pids in channel_counts.items():
        if ch in ("A", "B") and len(pids) > 1:
            # 保留第一个，关闭多余的
            for extra_pid in pids[1:]:
                log(f"  ⚠️ 通道{ch}重复: {extra_pid} → 关闭多余持仓")
                close_position(extra_pid, positions[extra_pid].get("algo_ids"))
                with positions_lock:
                    if extra_pid in positions:
                        del positions[extra_pid]
    if not positions:
        log("  无持仓")

    # 清理残留algo单
    stale_count = 0
    for inst_id, algos in algo_map.items():
        if inst_id not in protected_inst_ids:
            for aid in algos:
                r = okx_post("/api/v5/trade/cancel-algos", json.dumps([{"instId": inst_id, "algoId": aid}]))
                log(f"  🧹 清理残留: {inst_id} algoId={aid}: {r.get('code')}")
                stale_count += 1
    if stale_count:
        log(f"  共清理 {stale_count} 个残留挂单")
    else:
        log("  无残留挂单")

    while True:
        try:
            if not in_trading_window():
                time.sleep(120)
                continue

            if state.get("pause_until"):
                pause_until = datetime.fromisoformat(state["pause_until"])
                if datetime.now() < pause_until:
                    remaining = int((pause_until - datetime.now()).total_seconds())
                    log(f"⏸️ 连亏暂停中，剩余{remaining}秒")
                    time.sleep(30)
                    continue
                else:
                    state["pause_until"] = None
                    state["consecutive_losses"] = 0
                    save_state(state)

            # 监控持仓
            if positions:
                for inst_id in list(positions.keys()):
                    try:
                        result = monitor_position(inst_id, positions[inst_id])
                    except Exception as e:
                        log(f"⚠️ 监控 {inst_id} 异常: {e}")
                        continue

                    if result in ("PROFIT", "TRAIL_STOP", "TIME_STOP", "CLOSED"):
                        # 先保存upl（后续del会清除）
                        saved_upl = positions.get(inst_id, {}).get("last_upl", 0) if inst_id in positions else 0

                        if result == "CLOSED":
                            # 用OKX账单API查精确盈亏（不依赖last_upl估算）
                            time.sleep(0.5)  # 等待OKX记录账单
                            actual_pnl, actual_fee = get_realized_pnl(inst_id)
                            pos_upl = actual_pnl if actual_pnl != 0 else (
                                positions[inst_id].get("last_upl", 0) if inst_id in positions else 0)
                            log(f"  📊 {inst_id} CLOSED upl=${pos_upl:.4f} fee=${actual_fee:.4f}")
                            if pos_upl > 0:
                                state["consecutive_losses"] = 0
                            else:
                                state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
                                if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                                    pause_until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
                                    state["pause_until"] = pause_until.isoformat()
                                    log(f"⏸️ {state['consecutive_losses']}连亏，暂停{LOSS_PAUSE_SEC//60}分钟")
                            state["total_pnl"] = state.get("total_pnl", 0) + pos_upl
                            state["trade_count"] = state.get("trade_count", 0) + 1
                            # v8.1: 记录到自适应引擎
                            _closed_ch = positions.get(inst_id, {}).get("channel", "?")
                            _closed_factors = positions.get(inst_id, {}).get("active_factors", [])
                            adaptive.record_trade_result(inst_id, _closed_ch, pos_upl, _closed_factors)

                        with positions_lock:
                            if inst_id in positions:
                                del positions[inst_id]

                        if result in ("PROFIT", "TRAIL_STOP"):
                            state["consecutive_losses"] = 0
                            state["total_pnl"] = state.get("total_pnl", 0) + saved_upl
                            state["trade_count"] = state.get("trade_count", 0) + 1
                            # v8.1: 记录盈利到自适应引擎
                            adaptive.record_trade_result(inst_id, "A", saved_upl, [])
                        elif result == "TIME_STOP":
                            if saved_upl > 0:
                                state["consecutive_losses"] = 0
                            else:
                                state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
                            state["total_pnl"] = state.get("total_pnl", 0) + saved_upl
                            state["trade_count"] = state.get("trade_count", 0) + 1
                            # v8.1: 记录到自适应引擎
                            adaptive.record_trade_result(inst_id, "A", saved_upl, [])
                            if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                                pause_until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
                                state["pause_until"] = pause_until.isoformat()
                                log(f"⏸️ {state['consecutive_losses']}连亏，暂停{LOSS_PAUSE_SEC//60}分钟")

                        state["last_trade"][inst_id] = time.time()
                        save_state(state)

            # 安全检查
            if positions:
                pending = okx_get("/api/v5/trade/orders-algo-pending?ordType=conditional")
                protected_syms = set()
                if pending.get("data"):
                    for a in pending["data"]:
                        protected_syms.add(a.get("instId", ""))
                for inst_id in list(positions.keys()):
                    if inst_id not in protected_syms:
                        log(f"🚨 {inst_id} 无挂单保护 → 强制平仓")
                        for _ in range(3):
                            time.sleep(1)
                            close_position(inst_id, positions[inst_id].get("algo_ids"))
                            if not get_position(inst_id):
                                break
                        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
                        if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                            pause_until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
                            state["pause_until"] = pause_until.isoformat()
                            log(f"⏸️ 安全平仓{inst_id}，{state['consecutive_losses']}连亏，暂停{LOSS_PAUSE_SEC//60}分钟")
                        state["last_trade"][inst_id] = time.time()
                        save_state(state)
                        with positions_lock:
                            if inst_id in positions:
                                del positions[inst_id]

            # ===== 双通道扫描 =====
            scan_count += 1
            balance = get_balance()
            log(f"📡 扫描#{scan_count} 余额:${balance:.2f} 仓位:{len(positions)}/{MAX_CONCURRENT}")

            channel_a, channel_b, mt_data_map, fr_map = scan_dual_channel()

            # v8.1.1: 自适应暂停检查（仅极端横盘时暂停，且限制最多跳过3轮）
            if adaptive.should_pause_trading() and not positions:
                # 计算已跳过轮数，最多连续跳过3轮后强制恢复扫描
                _pause_skip_count = getattr(adaptive, '_pause_skip_count', 0)
                if _pause_skip_count < 3:
                    adaptive._pause_skip_count = _pause_skip_count + 1
                    log(f"  🧠 [自适应] 市场极低波动，跳过本轮({_pause_skip_count+1}/3) | {adaptive.regime.get_status_str()}")
                    time.sleep(SCAN_INTERVAL)
                    continue
                else:
                    # 超过3轮强制恢复，只记录日志
                    log(f"  🧠 [自适应] 横盘但已跳过3轮，恢复扫描 | {adaptive.regime.get_status_str()}")
                    adaptive._pause_skip_count = 0
            else:
                adaptive._pause_skip_count = 0

            # 归一化评分
            for c in channel_a:
                c["score_100"] = min(c["score"] / 10.0 * 100, 100)
            for c in channel_b:
                c["score_100"] = min(c["score"] / 22.0 * 100, 100)

            # v8.1: 每20轮输出自适应引擎状态
            if scan_count % 20 == 0:
                log(f"  {adaptive.get_status_log()}")

            # 打印通道候选
            if channel_a:
                log(f"  🔴 通道A: {len(channel_a)}个候选")
                for c in channel_a[:3]:
                    tags = f"[{','.join(c.get('chain_tags',[]))}]" if c.get("chain_tags") else ""
                    log(f"    {c['sym']} {'↑' if c['dir']=='LONG' else '↓'} FR={c['fr']*100:+.4f}% score={c['score']}({c['score_100']:.0f}/100) {tags}")
            if channel_b:
                log(f"  🔵 通道B: {len(channel_b)}个候选")
                for c in channel_b[:3]:
                    tags = f"[{','.join(c.get('chain_tags',[]))}]" if c.get("chain_tags") else ""
                    log(f"    {c['sym']} {'↑' if c['dir']=='LONG' else '↓'} score={c['score']}({c['score_100']:.0f}/100) {tags}")

            # === 1. 重新评分所有持仓 + 计算健康度 ===
            for inst_id in list(positions.keys()):
                if inst_id in mt_data_map or inst_id in fr_map:
                    new_score, tags = recalc_position_score(
                        inst_id, positions[inst_id], mt_data_map, fr_map
                    )
                    old_score = positions[inst_id].get("score", 0)
                    positions[inst_id]["score"] = new_score
                    if abs(new_score - old_score) >= 3:
                        log(f"  📊 {inst_id} 评分变化: {old_score}→{new_score} [{','.join(tags)}]")
                
                # v8.1.1: 计算持仓健康度
                if inst_id in mt_data_map:
                    health, health_reasons = calc_position_health(inst_id, positions[inst_id], mt_data_map)
                    old_health = positions[inst_id].get("health_score", 50)
                    positions[inst_id]["health_score"] = health
                    positions[inst_id]["health_reasons"] = health_reasons
                    # 健康度大幅变化时输出日志
                    if abs(health - old_health) >= 20:
                        log(f"  💊 {inst_id} 健康度: {old_health}→{health} [{','.join(health_reasons)}]")

            # === 3. 结算检查 ===
            near_settle, settle_secs = is_near_settlement()
            if near_settle:
                log(f"  ⏳ 距资金费率结算仅{settle_secs//60}分钟，暂停开仓")
                time.sleep(60)
                continue

            # 冷却过滤
            now_ts = time.time()
            def is_cooled(sym):
                last = state.get("last_trade", {}).get(sym, 0)
                return now_ts - last >= COOLDOWN_SEC

            # ============================================================
            # 仓位1管理: 通道A独占
            # ============================================================
            # 暂停检查（防止连亏暂停后仍开仓）
            if state.get("pause_until"):
                pause_until = datetime.fromisoformat(state["pause_until"])
                if datetime.now() < pause_until:
                    remaining = int((pause_until - datetime.now()).total_seconds())
                    log(f"⏸️ 连亏暂停中，剩余{remaining}秒")
                    time.sleep(60)
                    continue

            slot1_id = None
            for pid, pinfo in positions.items():
                if pinfo.get("channel") == "A":
                    slot1_id = pid
                    break

            if slot1_id:
                # 检查换仓条件: FR消失或方向反了（方案5：多空兼顾）
                pos_info = positions[slot1_id]
                fr_val = fr_map.get(slot1_id)
                need_swap_a = False
                pos_dir = pos_info.get("direction", "LONG")
                # 优先用FR直接判断
                if fr_val is not None:
                    if abs(fr_val) < FR_EXTREME_THRESHOLD:
                        log(f"  🔄 [仓位1] {slot1_id} FR消失(|FR|={abs(fr_val)*100:.4f}%<{FR_EXTREME_THRESHOLD*100}%) → 需要换仓")
                        need_swap_a = True
                    elif pos_dir == "LONG" and fr_val > 0:
                        log(f"  🔄 [仓位1] {slot1_id} 做多但FR变正({fr_val*100:+.4f}%) → 需要换仓")
                        need_swap_a = True
                    elif pos_dir == "SHORT" and fr_val < 0:
                        log(f"  🔄 [仓位1] {slot1_id} 做空但FR变负({fr_val*100:+.4f}%) → 需要换仓")
                        need_swap_a = True
                else:
                    # FR数据不可用，用recalc评分兜底
                    recalc_score, _ = recalc_position_score(slot1_id, pos_info, mt_data_map, fr_map)
                    if recalc_score == 0:
                        log(f"  🔄 [仓位1] {slot1_id} FR不可用+评分为0 → 需要换仓")
                        need_swap_a = True
                    else:
                        log(f"  ⚠️ [仓位1] {slot1_id} FR数据不可用，评分={recalc_score}，暂不换仓")

                if need_swap_a:
                    # 检查换仓冷却
                    last_swap = swap_cooldown_map.get("slot1", 0)
                    if now_ts - last_swap < SWAP_COOLDOWN:
                        remaining = int(SWAP_COOLDOWN - (now_ts - last_swap))
                        log(f"  ⏳ [仓位1] 换仓冷却中({remaining}s)")
                    else:
                        close_position(slot1_id, pos_info.get("algo_ids"))
                        time.sleep(1)
                        with positions_lock:
                            if slot1_id in positions:
                                del positions[slot1_id]
                        state["last_trade"][slot1_id] = now_ts
                        swap_cooldown_map["slot1"] = now_ts
                        slot1_id = None
                        save_state(state)
            else:
                # 仓位1空着，开通道A Top1（暂停检查在上方，此处无需重复）
                if channel_a and balance >= 1:
                    top_a = None
                    for c in channel_a:
                        if is_cooled(c["sym"]) and c["sym"] not in positions:
                            top_a = c
                            break
                    if top_a:
                        per_slot = balance * CHANNEL_A_POSITION_PCT
                        log(f"🔥 [仓位1] {top_a['sym']} {top_a['dir']} FR={top_a['fr']*100:+.4f}% score={top_a['score']} 仓位${per_slot:.2f}")
                        pos = open_position(top_a["sym"], top_a["dir"], per_slot, "A")
                        if pos:
                            pos["fr"] = top_a.get("fr", 0)
                            pos["score"] = top_a.get("score", 0)
                            pos["active_factors"] = adaptive.get_active_factors(top_a)  # v8.1
                            with positions_lock:
                                positions[top_a["sym"]] = pos
                            save_state(state)
                            balance = get_balance()  # 刷新余额供后续仓位使用

            # ============================================================
            # 仓位2管理: 通道B独占
            # ============================================================
            slot2_id = None
            for pid, pinfo in positions.items():
                if pinfo.get("channel") == "B":
                    slot2_id = pid
                    break

            if slot2_id:
                # 检查换仓条件: 方向反转或（评分归零+价格跌破开仓价或持续2分钟）（BUG#2修复）
                pos_info = positions[slot2_id]
                pos_score = pos_info.get("score", 0)
                need_swap_b = False

                # 评分归零时间追踪
                if pos_score == 0:
                    if "score_zero_since" not in pos_info:
                        pos_info["score_zero_since"] = now_ts
                    score_zero_elapsed = now_ts - pos_info["score_zero_since"]
                else:
                    pos_info.pop("score_zero_since", None)
                    score_zero_elapsed = 0

                # 获取当前价格判断是否价格也反了
                _d2 = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={slot2_id}", 5)
                _cur_px2 = float(_d2["data"][0]["last"]) if _d2.get("data") else pos_info.get("entry_price", 0)
                _entry2 = pos_info.get("entry_price", 0)
                if pos_info.get("direction") == "LONG":
                    _px_adverse = _cur_px2 < _entry2 * 0.995  # 跌破0.5%
                else:
                    _px_adverse = _cur_px2 > _entry2 * 1.005  # 涨破0.5%

                if pos_score == 0 and _px_adverse:
                    log(f"  🔄 [仓位2] {slot2_id} 评分归零+价格反向 → 需要换仓")
                    need_swap_b = True
                elif pos_score == 0 and score_zero_elapsed > 120:
                    log(f"  🔄 [仓位2] {slot2_id} 评分归零持续{int(score_zero_elapsed)}秒>2分钟 → 强制换仓")
                    need_swap_b = True
                elif pos_info.get("direction") == "LONG":
                    chg = mt_data_map.get(slot2_id, {}).get("chg_5m", 0)
                    if chg < -0.3:
                        log(f"  🔄 [仓位2] {slot2_id} 做多但5m转跌({chg:+.2f}%) → 需要换仓")
                        need_swap_b = True
                elif pos_info.get("direction") == "SHORT":
                    chg = mt_data_map.get(slot2_id, {}).get("chg_5m", 0)
                    if chg > 0.3:
                        log(f"  🔄 [仓位2] {slot2_id} 做空但5m转涨({chg:+.2f}%) → 需要换仓")
                        need_swap_b = True

                if need_swap_b:
                    last_swap = swap_cooldown_map.get("slot2", 0)
                    if now_ts - last_swap < SWAP_COOLDOWN:
                        remaining = int(SWAP_COOLDOWN - (now_ts - last_swap))
                        log(f"  ⏳ [仓位2] 换仓冷却中({remaining}s)")
                    else:
                        close_position(slot2_id, pos_info.get("algo_ids"))
                        time.sleep(1)
                        with positions_lock:
                            if slot2_id in positions:
                                del positions[slot2_id]
                        state["last_trade"][slot2_id] = now_ts
                        swap_cooldown_map["slot2"] = now_ts
                        slot2_id = None
                        save_state(state)
            else:
                # 仓位2空着，开通道B Top1（需连续2轮score≥10才开仓）
                if channel_b and balance >= 1:
                    top_b = None
                    for c in channel_b:
                        if is_cooled(c["sym"]) and c["sym"] not in positions:
                            # 持久性检查：该品种在上一轮扫描中也必须score≥10
                            prev_match = [p for p in prev_channel_b if p["sym"] == c["sym"] and p["score"] >= MOMENTUM_SCORE_THRESHOLD]
                            if not prev_match:
                                log(f"  ⏳ [仓位2] {c['sym']} score={c['score']} 但上轮不在候选 → 等持久确认")
                                continue
                            top_b = c
                            break
                    if top_b:
                        per_slot = balance * CHANNEL_B_POSITION_PCT
                        log(f"🔥 [仓位2] {top_b['sym']} {top_b['dir']} score={top_b['score']} 仓位${per_slot:.2f}")
                        pos = open_position(top_b["sym"], top_b["dir"], per_slot, "B")
                        if pos:
                            pos["score"] = top_b.get("score", 0)
                            pos["active_factors"] = adaptive.get_active_factors(top_b)  # v8.1
                            with positions_lock:
                                positions[top_b["sym"]] = pos
                            save_state(state)
                            balance = get_balance()  # 刷新余额供后续仓位使用

            # 打印持仓实时状态
            if positions:
                for pid, pinfo in positions.items():
                    ch = pinfo.get("channel", "?")
                    sc = pinfo.get("score", 0)
                    src = pinfo.get("source_channel", "")
                    src_str = f"(来自{src})" if src else ""
                    log(f"  💼 [{ch}] {pid} {pinfo.get('direction','?')} score={sc}{src_str}")

            # 保存本轮通道B候选供下轮持久性检查
            prev_channel_b = channel_b[:]

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 手动停止")
            for inst_id, pos_info in list(positions.items()):
                close_position(inst_id, pos_info.get("algo_ids"))
            break
        except Exception as e:
            log(f"⚠️ 异常: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    main()
