#!/usr/bin/env python3
"""
OKX 妖币猎手 v7.9 — 2仓位+多空兼顾+自适应止损+软过滤+回调入场+市场环境识别
通道A: 极端FR+技术指标验证（FR<0做多/FR>0做空，需K线趋势一致）
通道B: 动量顺势（评分>=9，软过滤替代硬性否决+回调入场确认）

v7.9优化（基于v7.8分析，目标提升盈亏比+开仓频率）：
- 软过滤替代硬性否决：EMA/Stoch/5m量能不满足时扣分而非跳过（增加开仓机会）
- 分阶段时间止损：5分钟浮亏>1%止损/8分钟浮亏止损/10分钟横盘止损（避免正确方向被震出）
- ATR动态止盈止损：根据BB宽度（波动率代理）自适应调整TP/SL
- 市场环境识别：ADX均值判断趋势/震荡，动态调整参数
- 回调入场确认：不追极端，等1-2根回调后入场（改善入场点位）
- 评分门槛 10→9：配合软过滤，适度放宽
"""

import subprocess, json, time, os, hmac, base64, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================
LEVERAGE = 6
TP_PCT = 0.03          # 3% 止盈（基准，会被动态调整）
SL_PCT = 0.015         # 1.5% 止损（基准，会被动态调整）
TRAIL_ACTIVATE = 0.010  # 浮盈1%后启动追踪止损
TRAIL_DISTANCE = 0.006  # 追踪止损距离0.6%
# 分阶段时间止损
TIME_STOP_PHASE1 = 300   # 5分钟：浮亏>1%直接止损（方向大概率错）
TIME_STOP_PHASE2 = 480   # 8分钟：浮亏任何值止损
TIME_STOP_PHASE3 = 600   # 10分钟：浮盈<0.3%止损（横盘太久）
SCAN_INTERVAL = 30      # 30秒扫描
COOLDOWN_SEC = 1200     # 同一品种冷却20分钟
MAX_CONCURRENT = 2      # 2仓位: A独占1个+B独占1个
MAX_CONSECUTIVE_LOSSES = 3
LOSS_PAUSE_SEC = 1800   # 连亏暂停30分钟
SWAP_COOLDOWN = 1800    # 换仓冷却30分钟

# 通道A: 极端FR
FR_EXTREME_THRESHOLD = 0.0005   # |FR| > 0.05% 触发通道A
FR_MAX_THRESHOLD = 0.01         # |FR| > 1% 跳过（陷阱）
CHANNEL_A_VOL_SPIKE = 1.3       # 放量阈值
CHANNEL_A_POSITION_PCT = 0.40   # 仓位1占总资金40%

# 通道B: 动量顺势
MOMENTUM_SCORE_THRESHOLD = 9    # 综合评分>=9触发通道B（配合软过滤适度放宽）
CHANNEL_B_POSITION_PCT = 0.30   # 仓位2占总资金30%

# 共用
MIN_24H_VOL = 2000000           # 最小24h成交量 $200万
MIN_PRICE = 0.001
OKX_TAKER_FEE = 0.0005          # 0.05% taker
ROUND_TRIP_FEE = OKX_TAKER_FEE * 2  # 0.1%

# 链上数据
CHAIN_DATA_TTL = 60        # 60秒刷新一次
CHAIN_BONUS = 2
CHAIN_API_BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"
CHAIN_API_TIMEOUT = 3

# 时间窗口 (UTC+8) — 24小时
TRADING_WINDOWS = [(0, 0, 23, 59)]

# ==================== 缓存 ====================
INSTRUMENTS_CACHE = {}
FR_HISTORY = {}
FR_HISTORY_TTL = 300
SKIP_SYMS = {"RLS-USDT-SWAP", "BILL-USDT-SWAP"}

CHAIN_CACHE = {
    "smart_money_buy": set(),
    "hot_topics": set(),
    "smart_money_inflow": set(),
    "token_metrics": {},
    "last_update": 0
}

# ==================== 多仓管理 ====================
positions = {}
positions_lock = threading.Lock()
swap_cooldown_map = {}
prev_channel_b = []

# ==================== 日志 ====================
SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
STATE_FILE = os.path.join(SCRIPT_DIR, "v79_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "v79_trades.log")
CHAIN_CACHE_FILE = os.path.join(SCRIPT_DIR, "v79_chain_cache.json")

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
    """从Binance Web3 social-rush API提取社交情绪+鲸鱼持仓+集中度数据"""
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
                    if holders < 50:
                        continue
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
            save_chain_cache()
            log(f"  [chain] SM_buy={len(sm)} topics={len(ht)} SM_inflow={len(inf)} social={len(social)}")
        except Exception as e:
            log(f"  [chain] API异常: {e}（保留旧缓存）")
            CHAIN_CACHE["last_update"] = now

def get_chain_score(ticker_symbol):
    """链上评分：原始3因子 + 新增3因子"""
    score = 0
    tags = []
    sym = ticker_symbol.upper()

    if sym in CHAIN_CACHE["smart_money_buy"]:
        score += CHAIN_BONUS
        tags.append("SM")
    if sym in CHAIN_CACHE["hot_topics"]:
        score += CHAIN_BONUS
        tags.append("HOT")
    if sym in CHAIN_CACHE["smart_money_inflow"]:
        score += CHAIN_BONUS
        tags.append("INF")

    metrics = CHAIN_CACHE.get("token_metrics", {}).get(sym)
    if metrics:
        traders = metrics.get("traders24h", 0)
        if traders > 2000:
            score += 2
            tags.append(f"社{traders}")
        elif traders > 500:
            score += 1
            tags.append(f"社{traders}")

        sm_pct = metrics.get("sm_pct", 0)
        if sm_pct > 3:
            score += 2
            tags.append(f"鲸{sm_pct:.1f}%")
        elif sm_pct > 1:
            score += 1
            tags.append(f"鲸{sm_pct:.1f}%")

        insider_pct = metrics.get("insider_pct", 0)
        if insider_pct > 5:
            score -= 2
            tags.append(f"⚠️集{insider_pct:.1f}%")
        elif insider_pct > 2:
            score -= 1
            tags.append(f"⚠集{insider_pct:.1f}%")
        elif insider_pct < 0.5 and insider_pct > 0:
            score += 1
            tags.append("去中心化")

    return score, tags


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

# ==================== v7.9新增：市场环境识别 ====================
def detect_market_regime(mt_data_map):
    """通过ADX均值+BB宽度判断当前市场环境"""
    adx_values = [mt.get('adx_14') for mt in mt_data_map.values() if mt.get('adx_14')]
    bb_values = [mt.get('bb_width') for mt in mt_data_map.values() if mt.get('bb_width')]
    
    if not adx_values:
        return "NORMAL", {}
    
    avg_adx = sum(adx_values) / len(adx_values)
    avg_bb = sum(bb_values) / len(bb_values) if bb_values else 3.0
    
    info = {"avg_adx": avg_adx, "avg_bb": avg_bb}
    
    if avg_adx > 30:
        return "TRENDING", info    # 趋势市
    elif avg_adx < 18:
        return "RANGING", info     # 震荡市
    return "NORMAL", info

# ==================== v7.9新增：动态止盈止损 ====================
def calculate_dynamic_tp_sl(mt_data, regime="NORMAL"):
    """基于BB宽度（波动率代理）+ 市场环境的动态TP/SL"""
    bb_width = mt_data.get('bb_width', 3.0) if mt_data else 3.0
    
    # 基于波动率的基础TP/SL
    if bb_width > 5:      # 高波动
        tp_pct = 0.04     # 4%
        sl_pct = 0.018    # 1.8%
    elif bb_width > 3:    # 中波动
        tp_pct = 0.03     # 3%
        sl_pct = 0.015    # 1.5%
    else:                 # 低波动
        tp_pct = 0.022    # 2.2%
        sl_pct = 0.012    # 1.2%
    
    # 市场环境修正
    if regime == "TRENDING":
        tp_pct *= 1.3     # 趋势市放大止盈
        sl_pct *= 0.9     # 趋势市收紧止损
    elif regime == "RANGING":
        tp_pct *= 0.75    # 震荡市快速止盈
        sl_pct *= 1.1     # 震荡市放宽止损（避免被震出）
    
    return tp_pct, sl_pct

# ==================== v7.9新增：回调入场确认 ====================
def check_pullback_entry(mt_data, direction):
    """检查是否处于回调入场的好时机（不追极端）"""
    if not mt_data:
        return True, "无数据默认通过"
    
    last3_up = mt_data.get('last3_1m_ups', 1)
    last3_down = mt_data.get('last3_1m_downs', 1)
    stoch_k = mt_data.get('stoch_k', 50)
    chg_5m = mt_data.get('chg_5m', 0)
    
    if direction == "LONG":
        # 做多最佳入场：5m趋势向上，但最近有1-2根回调，Stoch不在极端
        if abs(chg_5m) > 4:
            return False, f"5m涨幅{chg_5m:.1f}%过大追高风险"
        if last3_up >= 3 and stoch_k > 75:
            return False, "3连阳+Stoch高位=追高"
        # 理想回调入场
        if last3_down >= 1 and 30 <= stoch_k <= 70:
            return True, "回调入场✓"
        # 非极端也允许
        if stoch_k <= 75:
            return True, "位置可接受"
        return False, f"Stoch={stoch_k:.0f}过高"
    
    elif direction == "SHORT":
        if abs(chg_5m) > 4:
            return False, f"5m跌幅{chg_5m:.1f}%过大追空风险"
        if last3_down >= 3 and stoch_k < 25:
            return False, "3连阴+Stoch低位=追空"
        if last3_up >= 1 and 30 <= stoch_k <= 70:
            return True, "反弹入场✓"
        if stoch_k >= 25:
            return True, "位置可接受"
        return False, f"Stoch={stoch_k:.0f}过低"
    
    return True, "ok"

# ==================== 多时间框架K线分析 ====================
def get_multi_timeframe(sym):
    """获取1m/5m/15m K线，计算动量指标"""
    try:
        c1m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=1m&limit=30", 5)
        c5m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=5m&limit=50", 5)
        c15m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=15m&limit=6", 5)

        result = {}

        if c1m.get("data") and len(c1m["data"]) >= 15:
            candles = c1m["data"][::-1]
            closes = [float(c[4]) for c in candles]
            vols = [float(c[5]) for c in candles]
            result['chg_5m'] = (closes[-1] - closes[-5]) / closes[-5] * 100
            result['chg_10m'] = (closes[-1] - closes[-10]) / closes[-10] * 100
            result['chg_15m'] = (closes[-1] - closes[-15]) / closes[-15] * 100
            r1 = sum(vols[-3:]) / 3
            h1 = sum(vols[:-3]) / max(len(vols[:-3]), 1)
            result['vol_1m'] = r1 / max(h1, 0.001)
            ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
            downs = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
            result['ups'] = ups
            result['downs'] = downs

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
                    result['rsi_14'] = 100

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

            if len(cl5) >= 2:
                last_5m_chg = (cl5[-1] - cl5[-2]) / cl5[-2] * 100
                prev_5m_chg = (cl5[-2] - cl5[-3]) / cl5[-3] * 100 if len(cl5) >= 3 else 0
                result['last_5m_chg'] = last_5m_chg
                result['prev_5m_chg'] = prev_5m_chg
                if last_5m_chg > 0.2 and prev_5m_chg > 0.2:
                    result['5m_consecutive'] = 'UP'
                elif last_5m_chg < -0.2 and prev_5m_chg < -0.2:
                    result['5m_consecutive'] = 'DOWN'
                else:
                    result['5m_consecutive'] = 'NONE'

                if len(cl5) >= 4:
                    chg1 = abs((cl5[-1] - cl5[-2]) / cl5[-2] * 100)
                    chg2 = abs((cl5[-2] - cl5[-3]) / cl5[-3] * 100)
                    chg3 = abs((cl5[-3] - cl5[-4]) / cl5[-4] * 100)
                    if chg1 < chg2 < chg3:
                        result['momentum_decay'] = True
                    else:
                        result['momentum_decay'] = False

                if len(cl5) >= 3 and len(vl5) >= 3:
                    price_higher = cl5[-1] > cl5[-2] > cl5[-3]
                    vol_lower = vl5[-1] < vl5[-2] < vl5[-3]
                    price_lower = cl5[-1] < cl5[-2] < cl5[-3]
                    if price_higher and vol_lower:
                        result['vol_price_diverge'] = 'TOP'
                    elif price_lower and vol_lower:
                        result['vol_price_diverge'] = 'BOTTOM'
                    else:
                        result['vol_price_diverge'] = 'NONE'

                highs5 = [float(c[2]) for c in c5]
                lows5 = [float(c[3]) for c in c5]

                # EMA(12,26)
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
                    # v7.9: EMA差距百分比（趋势强度）
                    result['ema_gap_pct'] = abs(ema12 - ema26) / ema26 * 100

                # 5m成交量均值
                if len(vl5) >= 5:
                    avg_vol_5m = sum(vl5[:-2]) / max(len(vl5[:-2]), 1)
                    recent_vol_5m = sum(vl5[-2:]) / 2
                    result['vol_5m_ratio'] = recent_vol_5m / max(avg_vol_5m, 0.001)

                # ADX(14)
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

                # BB宽度(20)
                if len(cl5) >= 20:
                    _bb_closes = cl5[-20:]
                    _bb_sma = sum(_bb_closes) / 20
                    _bb_std = (sum((c - _bb_sma)**2 for c in _bb_closes) / 20) ** 0.5
                    if _bb_sma > 0:
                        result['bb_width'] = (2 * _bb_std * 2) / _bb_sma * 100

                # ROC(10)
                if len(cl5) >= 11 and cl5[-11] > 0:
                    result['roc_10'] = (cl5[-1] - cl5[-11]) / cl5[-11] * 100

                # Stochastic K(14)
                if len(highs5) >= 14:
                    _k_period = 14
                    _h = max(highs5[-_k_period:])
                    _l = min(lows5[-_k_period:])
                    if _h != _l:
                        result['stoch_k'] = (cl5[-1] - _l) / (_h - _l) * 100
                    else:
                        result['stoch_k'] = 50

        # 15分钟趋势
        if c15m.get("data") and len(c15m["data"]) >= 4:
            c15 = c15m["data"][::-1]
            cl15 = [float(c[4]) for c in c15]
            chg_15m_recent = (cl15[-1] - cl15[-2]) / cl15[-2] * 100
            chg_15m_2 = (cl15[-1] - cl15[-3]) / cl15[-3] * 100
            chg_15m_3 = (cl15[-1] - cl15[-4]) / cl15[-4] * 100
            result['chg_15m_recent'] = chg_15m_recent
            result['chg_15m_2'] = chg_15m_2
            result['chg_15m_3'] = chg_15m_3
            if chg_15m_3 > 0.3:
                result['trend_15m'] = 'UP'
            elif chg_15m_3 < -0.3:
                result['trend_15m'] = 'DOWN'
            else:
                result['trend_15m'] = 'FLAT'

        return result
    except:
        return {}


# ==================== v7.9 通道B评分：软过滤替代硬性否决 ====================
def calculate_momentum_score(chg_5m, vol_1m, ups, downs, chain_bonus, fr, trend_15m=None, chg_5m_dir=None, mt_data=None):
    """通道B综合评分 v7.9（软过滤版）
    保留一票否决：动量衰减、量价背离（真正的反转信号）
    改为扣分：EMA不一致、Stoch极端、5m量能不足、15m趋势不一致
    满分约25分
    """
    score = 0

    # === 保留一票否决（真正的反转信号，不可妥协）===
    
    # 动量衰减 — 3根5m涨跌幅递减 = 动量在消退
    if mt_data and mt_data.get("momentum_decay"):
        return 0

    # 量价背离
    if mt_data:
        diverge = mt_data.get("vol_price_diverge", "NONE")
        if chg_5m_dir == "LONG" and diverge == "TOP":
            return 0  # 价涨量缩 = 假突破
        if chg_5m_dir == "SHORT" and diverge == "BOTTOM":
            return 0  # 价跌量缩 = 假跌破

    # RSI极端区域（保留一票否决，但放宽阈值）
    rsi = mt_data.get("rsi_14", 50) if mt_data else 50
    if chg_5m_dir == "LONG" and rsi > 75:
        return 0  # 超买区做多（从70放宽到75）
    if chg_5m_dir == "SHORT" and rsi < 25:
        return 0  # 超卖区做空（从30放宽到25）

    # 1m反转检测（保留，但需要价格也确认）
    if mt_data:
        last3_up = mt_data.get("last3_1m_ups", 1)
        last3_down = mt_data.get("last3_1m_downs", 1)
        chg_5m_val = mt_data.get("chg_5m", 0)
        # 做多但最近3根1m全阴 且 5m已转负 = 确认反转
        if chg_5m_dir == "LONG" and last3_down >= 3 and chg_5m_val < 0:
            return 0
        # 做空但最近3根1m全阳 且 5m已转正 = 确认反转
        if chg_5m_dir == "SHORT" and last3_up >= 3 and chg_5m_val > 0:
            return 0

    # === 软过滤（扣分/加分，不直接否决）===

    # 15m趋势一致性（从一票否决改为±分）
    if trend_15m and chg_5m_dir:
        if chg_5m_dir == "LONG" and trend_15m == "DOWN":
            score -= 3  # 逆势重扣
        elif chg_5m_dir == "SHORT" and trend_15m == "UP":
            score -= 3
        elif chg_5m_dir == "LONG" and trend_15m == "UP":
            score += 2  # 顺势加分
        elif chg_5m_dir == "SHORT" and trend_15m == "DOWN":
            score += 2

    # EMA排列（软过滤）
    if mt_data:
        ema_bull = mt_data.get('ema_bullish')
        ema_bear = mt_data.get('ema_bearish')
        ema_gap = mt_data.get('ema_gap_pct', 0)
        if ema_bull is not None or ema_bear is not None:
            if chg_5m_dir == "LONG" and ema_bull:
                score += 2
                if ema_gap > 0.3:
                    score += 1  # EMA差距大=趋势强
            elif chg_5m_dir == "SHORT" and ema_bear:
                score += 2
                if ema_gap > 0.3:
                    score += 1
            else:
                score -= 2  # EMA不一致扣分

    # Stochastic位置（软过滤）
    if mt_data:
        stoch_k = mt_data.get('stoch_k', 50)
        if chg_5m_dir == "LONG":
            if stoch_k > 85:
                score -= 3  # 极端超买重扣
            elif stoch_k > 75:
                score -= 1  # 偏高轻扣
            elif 30 <= stoch_k <= 60:
                score += 1  # 理想区间加分
        elif chg_5m_dir == "SHORT":
            if stoch_k < 15:
                score -= 3  # 极端超卖重扣
            elif stoch_k < 25:
                score -= 1
            elif 40 <= stoch_k <= 70:
                score += 1

    # 5m量能确认（软过滤）
    if mt_data:
        vol_5m_ratio = mt_data.get('vol_5m_ratio', 1.0)
        if vol_5m_ratio >= 1.8:
            score += 2  # 强放量
        elif vol_5m_ratio >= 1.2:
            score += 1  # 温和放量
        elif vol_5m_ratio < 0.7:
            score -= 2  # 量能萎缩

    # === 原有评分因子 ===

    # 5m连续确认
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

    # 6. 5m连续确认 (+1分)
    score += consecutive_bonus

    # 7. ADX趋势强度加分（v7.9：ADX 20-35 = 健康趋势）
    if mt_data:
        adx = mt_data.get('adx_14', 25)
        if 20 <= adx <= 35:
            score += 1  # 健康趋势区间
        elif adx > 45:
            score -= 1  # 趋势过强=可能反转

    return max(score, 0)

def check_short_safety(mt_data):
    """做空安全检查：RSI<70且15m趋势不是强势向上"""
    rsi = mt_data.get("rsi_14", 50)
    trend = mt_data.get("trend_15m", "FLAT")
    chg_15m_3 = mt_data.get("chg_15m_3", 0)
    if rsi > 70:
        return False, f"RSI={rsi:.0f}>70"
    if trend == "UP" and chg_15m_3 > 1.0:
        return False, f"15m强势上涨{chg_15m_3:.1f}%"
    return True, "ok"

def recalc_position_score(inst_id, pos_info, mt_data_map, fr_map):
    """每轮重新计算持仓的实时评分"""
    channel = pos_info.get("channel", "?")
    direction = pos_info.get("direction", "?")
    ticker = inst_id.replace("-USDT-SWAP", "")
    
    mt = mt_data_map.get(inst_id, {})
    fr = fr_map.get(inst_id)
    chain_bonus, chain_tags = get_chain_score(ticker)
    
    if channel == "A":
        if fr is None:
            return pos_info.get("score", 0), chain_tags
        abs_fr = abs(fr)
        if abs_fr < FR_EXTREME_THRESHOLD:
            return 0, chain_tags
        vol_1m = mt.get("vol_1m", 0)
        score = abs_fr * 10000 + chain_bonus
        if vol_1m >= CHANNEL_A_VOL_SPIKE:
            score += 1
        return score, chain_tags
    
    elif channel == "B":
        if "chg_5m" not in mt:
            return pos_info.get("score", 0), chain_tags
        
        chg_5m = mt.get("chg_5m", 0)
        vol_1m = mt.get("vol_1m", 0)
        ups = mt.get("ups", 0)
        downs = mt.get("downs", 0)
        trend_15m = mt.get("trend_15m", None)
        
        # 方向反转检测（保留硬性）
        if direction == "LONG" and chg_5m < -0.3:
            return 0, chain_tags
        if direction == "SHORT" and chg_5m > 0.3:
            return 0, chain_tags
        
        score = calculate_momentum_score(
            chg_5m, vol_1m, ups, downs, chain_bonus, fr,
            trend_15m, direction, mt
        )
        return score, chain_tags
    
    return pos_info.get("score", 0), []


# ==================== 双通道扫描 ====================
def scan_dual_channel():
    """双通道扫描：通道A(极端FR) + 通道B(动量顺势+软过滤+回调入场)"""
    d = curl_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP", 15)
    if not d.get("data"):
        return [], [], {}, {}, "NORMAL"

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

    top = sorted(usdt, key=lambda x: x["vol24h"], reverse=True)[:80]
    top = [x for x in top if x["instId"] not in SKIP_SYMS]
    syms = [x["instId"] for x in top]

    fr_map = get_funding_rates_batch(syms)

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

    update_chain_cache()

    # v7.9: 市场环境识别
    regime, regime_info = detect_market_regime(mt_data)

    channel_a_candidates = []
    channel_b_candidates = []

    for item in top:
        sym = item["instId"]
        ticker = sym.replace("-USDT-SWAP", "")
        fr = fr_map.get(sym)
        mt = mt_data.get(sym, {})

        chain_bonus, chain_tags = get_chain_score(ticker)

        # ===== 通道A: 极端FR + 技术指标验证 =====
        if fr is not None:
            abs_fr = abs(fr)
            if FR_EXTREME_THRESHOLD <= abs_fr <= FR_MAX_THRESHOLD:
                vol_1m_a = mt.get("vol_1m", 0)
                if vol_1m_a >= CHANNEL_A_VOL_SPIKE:
                    chg_5m_a = mt.get("chg_5m", 0)
                    trend_15m_a = mt.get("trend_15m", "FLAT")
                    rsi_a = mt.get("rsi_14", 50)
                    adx_a = mt.get("adx_14", 25)
                    bb_a = mt.get("bb_width", 3.0)
                    stoch_a = mt.get("stoch_k", 50)
                    
                    if adx_a >= 45:
                        continue
                    if bb_a < 1.2:
                        continue

                    if fr < 0 and (chg_5m_a > 0.1 or trend_15m_a == "UP"):
                        if rsi_a > 75:
                            continue
                        if stoch_a > 85:
                            continue
                        direction_a = "LONG"
                    elif fr > 0 and chg_5m_a < -0.1 and trend_15m_a != "UP":
                        if rsi_a < 25:
                            continue
                        if stoch_a < 15:
                            continue
                        roc_a = mt.get("roc_10", 0)
                        if roc_a > 0.5:
                            continue
                        direction_a = "SHORT"
                    else:
                        continue
                    
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
                        "mt_data": mt,
                    })

        # ===== 通道B: 动量顺势（v7.9软过滤版）=====
        if mt and "chg_5m" in mt:
            chg_5m = mt.get("chg_5m", 0)
            vol_1m = mt.get("vol_1m", 0)
            ups = mt.get("ups", 0)
            downs = mt.get("downs", 0)
            trend_15m = mt.get("trend_15m", None)

            if chg_5m > 0.3:
                direction = "LONG"
            elif chg_5m < -0.3:
                direction = "SHORT"
            else:
                continue

            # 评分（v7.9软过滤版）
            mom_score = calculate_momentum_score(chg_5m, vol_1m, ups, downs, chain_bonus, fr, trend_15m, direction, mt)

            if mom_score >= MOMENTUM_SCORE_THRESHOLD:
                # 做空安全检查（放宽版）
                if direction == "SHORT":
                    safe, reason = check_short_safety(mt)
                    if not safe:
                        continue

                # ADX过滤（保留但放宽）
                adx_b = mt.get("adx_14", 25)
                if adx_b >= 50:
                    continue

                # BB宽度过滤（保留但放宽）
                bb_b = mt.get("bb_width", 3.0)
                if bb_b < 1.0:
                    continue

                # ROC方向确认（保留但放宽阈值）
                roc_b = mt.get("roc_10", 0)
                if direction == "SHORT" and roc_b > 0.5:
                    continue
                if direction == "LONG" and roc_b < -0.5:
                    continue

                # v7.9: 回调入场确认
                pullback_ok, pullback_reason = check_pullback_entry(mt, direction)
                if not pullback_ok:
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
                    "mt_data": mt,
                    "pullback": pullback_reason,
                })

    channel_a_candidates.sort(key=lambda x: x["score"], reverse=True)
    channel_b_candidates.sort(key=lambda x: x["score"], reverse=True)

    return channel_a_candidates, channel_b_candidates, mt_data, fr_map, regime


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
    """查询OKX账单API获取最近一笔已实现盈亏"""
    try:
        d = okx_get(f"/api/v5/account/bills?instId={inst_id}&instType=SWAP&limit=5")
        if d.get("data"):
            for bill in d["data"]:
                pnl = float(bill.get("realizedPnl", 0))
                fee = float(bill.get("fee", 0))
                if pnl != 0:
                    return pnl, fee
        return 0, 0
    except:
        return 0, 0

def open_position(inst_id, direction, balance_for_trade, channel_label="", mt_data=None, regime="NORMAL"):
    """开仓 + 挂动态TP/SL"""
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
    
    # v7.9: 动态TP/SL
    tp_pct, sl_pct = calculate_dynamic_tp_sl(mt_data, regime)
    
    round_trip_fee_cost = notional_actual * ROUND_TRIP_FEE
    potential_tp_profit = notional_actual * tp_pct
    if potential_tp_profit < round_trip_fee_cost * 1.5:
        log(f"  ❌ {inst_id} TP利润${potential_tp_profit:.4f} < 1.5x手续费${round_trip_fee_cost:.4f} → 跳过")
        return None

    tick_sz = float(specs.get("tickSz", "0.00001"))
    tick_decimals = len(specs.get("tickSz", "0.00001").rstrip("0").split(".")[-1]) if "." in specs.get("tickSz", "0.00001") else 0

    def format_price(p):
        rounded = round(p / tick_sz) * tick_sz
        if tick_decimals > 0:
            return f"{rounded:.{tick_decimals}f}"
        return str(rounded)

    if direction == "LONG":
        tp = format_price(price * (1 + tp_pct))
        sl = format_price(price * (1 - sl_pct))
        side = "buy"
        close_side = "sell"
    else:
        tp = format_price(price * (1 - tp_pct))
        sl = format_price(price * (1 + sl_pct))
        side = "sell"
        close_side = "buy"

    tp_diff = abs(price * tp_pct)
    sl_diff = abs(price * sl_pct)
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
        log(f"⚠️ {inst_id} TP={tp_ok} SL={sl_ok} → 强制平仓")
        for close_attempt in range(3):
            time.sleep(1)
            close_position(inst_id, algo_ids)
            if not get_position(inst_id):
                log(f"✅ {inst_id} 裸仓已平仓")
                break
        else:
            log(f"🚨 {inst_id} 平仓3次失败！需手动处理")
        return None

    log(f"✅ [{channel_label}] 开{'多' if direction=='LONG' else '空'} {inst_id} {sz}张 @ ${avg} TP=${tp}({tp_pct*100:.1f}%) SL=${sl}({sl_pct*100:.1f}%) [{regime}]")
    return {
        "instId": inst_id, "direction": direction,
        "entry_price": avg, "sz": sz,
        "algo_ids": algo_ids, "open_time": time.time(),
        "notional": notional_actual, "trail_activated": False,
        "highest_pnl_pct": 0, "fr": 0,
        "last_upl": 0, "channel": channel_label,
        "score": 0,
        "tp_pct": tp_pct, "sl_pct": sl_pct,  # 记录动态TP/SL
    }


# ==================== 持仓监控（v7.9分阶段时间止损）====================
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

    # 追踪止损
    if pos_info.get("trail_activated"):
        trail_stop_pct = pos_info["highest_pnl_pct"] - TRAIL_DISTANCE * 100
        if pct_change < trail_stop_pct:
            log(f"🔄 {inst_id} 追踪止损! 最高{pos_info['highest_pnl_pct']:+.2f}% → 当前{pct_change:+.2f}% (止损线{trail_stop_pct:+.2f}%)")
            close_position(inst_id, pos_info.get("algo_ids"))
            return "TRAIL_STOP"
    elif pct_change >= TRAIL_ACTIVATE * 100:
        pos_info["trail_activated"] = True
        log(f"🔔 {inst_id} 追踪止损激活! 浮盈{pct_change:+.2f}% > {TRAIL_ACTIVATE*100}%")

    # 紧急止盈（6%）
    emergency_tp = 6.0
    if pct_change >= emergency_tp:
        log(f"🚀 {inst_id} 紧急止盈{pct_change:+.3f}% >= {emergency_tp}% → 立即落袋")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "PROFIT"

    # === v7.9 分阶段时间止损 ===
    
    # Phase 1: 5分钟后，浮亏>1% = 方向大概率错了，快速止损
    if elapsed > TIME_STOP_PHASE1 and pct_change < -1.0:
        log(f"⏰ {inst_id} Phase1: {int(elapsed)}秒 浮亏{pct_change:+.2f}%>1% → 快速止损")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "TIME_STOP"
    
    # Phase 2: 8分钟后，任何浮亏都止损
    if elapsed > TIME_STOP_PHASE2 and pct_change < 0:
        log(f"⏰ {inst_id} Phase2: {int(elapsed)}秒 浮亏{pct_change:+.2f}% → 时间止损")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "TIME_STOP"
    
    # Phase 3: 10分钟后，浮盈<0.3% = 横盘太久，出局
    if elapsed > TIME_STOP_PHASE3:
        if pct_change < 0.3:
            log(f"⏰ {inst_id} Phase3: {int(elapsed)}秒 横盘{pct_change:+.2f}%<0.3% → 出局")
            close_position(inst_id, pos_info.get("algo_ids"))
            return "TIME_STOP"
        elif pct_change >= 0.5 and not pos_info.get("trail_activated"):
            # 浮盈>0.5%，激活追踪止损保护利润
            pos_info["trail_activated"] = True
            pos_info["highest_pnl_pct"] = pct_change
            log(f"🔔 {inst_id} 10分钟+浮盈{pct_change:+.2f}% → 保本追踪激活")

    return "HOLDING"


# ==================== 主循环 ====================
def main():
    log("=" * 60)
    log("🔥 OKX 妖币猎手 v7.9 启动（软过滤+动态止损+回调入场+市场环境识别）")
    log(f"  杠杆: {LEVERAGE}x  TP/SL: 动态（基于BB宽度+市场环境）")
    log(f"  通道A: |FR|>{FR_EXTREME_THRESHOLD*100}%+技术验证+ADX<45+BB>1.2% 仓位{CHANNEL_A_POSITION_PCT*100:.0f}%")
    log(f"  通道B: 动量>={MOMENTUM_SCORE_THRESHOLD}(软过滤)+回调入场+ADX<50 仓位{CHANNEL_B_POSITION_PCT*100:.0f}%")
    log(f"  v7.9新增: 软过滤评分 + 分阶段时间止损(5/8/10分钟) + ATR动态TP/SL + 回调入场 + 市场环境自适应")
    log(f"  追踪止损: 激活{TRAIL_ACTIVATE*100}% 距离{TRAIL_DISTANCE*100}%")
    log(f"  时间止损: Phase1={TIME_STOP_PHASE1}s(亏>1%) Phase2={TIME_STOP_PHASE2}s(任何亏) Phase3={TIME_STOP_PHASE3}s(盈<0.3%)")
    log(f"  最大同时持仓: {MAX_CONCURRENT}")
    log("=" * 60)

    load_instruments_cache()
    load_chain_cache()
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
                "tp_pct": TP_PCT, "sl_pct": SL_PCT,
            }
            algo_status = f" algo={len(matched_algos)}" if matched_algos else " ⚠️无挂单"
            log(f"  📥 {inst_id} {direction} {int(abs(pos_val))}张 @ ${entry_price}{algo_status}")
        log(f"  共加载 {len(existing)} 个持仓")

    # 清理重复通道分配
    channel_counts = {}
    for pid, pinfo in positions.items():
        ch = pinfo.get("channel", "?")
        if ch not in channel_counts:
            channel_counts[ch] = []
        channel_counts[ch].append(pid)
    for ch, pids in channel_counts.items():
        if ch in ("A", "B") and len(pids) > 1:
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
                        saved_upl = positions.get(inst_id, {}).get("last_upl", 0) if inst_id in positions else 0

                        if result == "CLOSED":
                            time.sleep(0.5)
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

                        with positions_lock:
                            if inst_id in positions:
                                del positions[inst_id]

                        if result in ("PROFIT", "TRAIL_STOP"):
                            state["consecutive_losses"] = 0
                            state["total_pnl"] = state.get("total_pnl", 0) + saved_upl
                            state["trade_count"] = state.get("trade_count", 0) + 1
                        elif result == "TIME_STOP":
                            if saved_upl > 0:
                                state["consecutive_losses"] = 0
                            else:
                                state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
                            state["total_pnl"] = state.get("total_pnl", 0) + saved_upl
                            state["trade_count"] = state.get("trade_count", 0) + 1
                            if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                                pause_until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
                                state["pause_until"] = pause_until.isoformat()
                                log(f"⏸️ {state['consecutive_losses']}连亏，暂停{LOSS_PAUSE_SEC//60}分钟")

                        state["last_trade"][inst_id] = time.time()
                        save_state(state)

            # 安全检查：无挂单保护的仓位强制平仓
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

            channel_a, channel_b, mt_data_map, fr_map, regime = scan_dual_channel()

            # 归一化评分
            for c in channel_a:
                c["score_100"] = min(c["score"] / 10.0 * 100, 100)
            for c in channel_b:
                c["score_100"] = min(c["score"] / 25.0 * 100, 100)

            # 打印通道候选
            log(f"  🌐 市场环境: {regime}")
            if channel_a:
                log(f"  🔴 通道A: {len(channel_a)}个候选")
                for c in channel_a[:3]:
                    tags = f"[{','.join(c.get('chain_tags',[]))}]" if c.get("chain_tags") else ""
                    log(f"    {c['sym']} {'↑' if c['dir']=='LONG' else '↓'} FR={c['fr']*100:+.4f}% score={c['score']}({c['score_100']:.0f}/100) {tags}")
            if channel_b:
                log(f"  🔵 通道B: {len(channel_b)}个候选")
                for c in channel_b[:3]:
                    tags = f"[{','.join(c.get('chain_tags',[]))}]" if c.get("chain_tags") else ""
                    pb = c.get("pullback", "")
                    log(f"    {c['sym']} {'↑' if c['dir']=='LONG' else '↓'} score={c['score']}({c['score_100']:.0f}/100) {tags} {pb}")

            # === 1. 重新评分所有持仓 ===
            for inst_id in list(positions.keys()):
                if inst_id in mt_data_map or inst_id in fr_map:
                    new_score, tags = recalc_position_score(
                        inst_id, positions[inst_id], mt_data_map, fr_map
                    )
                    old_score = positions[inst_id].get("score", 0)
                    positions[inst_id]["score"] = new_score
                    if abs(new_score - old_score) >= 3:
                        log(f"  📊 {inst_id} 评分变化: {old_score}→{new_score} [{','.join(tags)}]")

            # === 2. 结算检查 ===
            near_settle, settle_sec = is_near_settlement()
            if near_settle:
                log(f"  ⚠️ 距结算{settle_sec}秒，暂停开仓")
                time.sleep(SCAN_INTERVAL)
                continue

            # === 3. 换仓逻辑（评分归零+价格反向才换）===
            for inst_id in list(positions.keys()):
                pos_info = positions[inst_id]
                channel = pos_info.get("channel", "?")
                current_score = pos_info.get("score", 0)
                direction = pos_info.get("direction", "?")
                
                if current_score > 0:
                    continue  # 评分还在，不换
                
                # 评分归零，检查价格是否也反向
                mt_current = mt_data_map.get(inst_id, {})
                chg_5m_current = mt_current.get("chg_5m", 0)
                
                price_reversed = False
                if direction == "LONG" and chg_5m_current < -0.2:
                    price_reversed = True
                elif direction == "SHORT" and chg_5m_current > 0.2:
                    price_reversed = True
                
                if not price_reversed:
                    continue  # 价格没反向，再等等
                
                # 检查换仓冷却
                now = time.time()
                last_swap = swap_cooldown_map.get(channel, 0)
                if now - last_swap < SWAP_COOLDOWN:
                    continue
                
                # 找替代候选
                candidates = channel_a if channel == "A" else channel_b
                if not candidates:
                    continue
                
                best = candidates[0]
                if best["sym"] == inst_id:
                    if len(candidates) > 1:
                        best = candidates[1]
                    else:
                        continue
                
                # 冷却检查
                last_trade_time = state.get("last_trade", {}).get(best["sym"], 0)
                if now - last_trade_time < COOLDOWN_SEC:
                    continue
                
                log(f"🔄 [{channel}] 换仓: {inst_id}(score=0,价格反向) → {best['sym']}(score={best['score']})")
                close_position(inst_id, pos_info.get("algo_ids"))
                with positions_lock:
                    if inst_id in positions:
                        del positions[inst_id]
                state["last_trade"][inst_id] = now
                swap_cooldown_map[channel] = now
                
                # 开新仓
                balance = get_balance()
                trade_balance = balance * best["position_pct"]
                if trade_balance > 5:
                    new_pos = open_position(best["sym"], best["dir"], trade_balance, channel, best.get("mt_data"), regime)
                    if new_pos:
                        new_pos["score"] = best["score"]
                        new_pos["channel"] = channel
                        new_pos["source_channel"] = channel
                        with positions_lock:
                            positions[best["sym"]] = new_pos
                        save_state(state)

            # === 4. 开新仓（空仓位填充）===
            current_channels = {pos_info.get("channel") for pos_info in positions.values()}
            
            # 通道A空位
            if "A" not in current_channels and channel_a and balance > 10:
                best_a = channel_a[0]
                last_trade_time = state.get("last_trade", {}).get(best_a["sym"], 0)
                if time.time() - last_trade_time >= COOLDOWN_SEC:
                    trade_balance = balance * best_a["position_pct"]
                    new_pos = open_position(best_a["sym"], best_a["dir"], trade_balance, "A", best_a.get("mt_data"), regime)
                    if new_pos:
                        new_pos["score"] = best_a["score"]
                        new_pos["channel"] = "A"
                        new_pos["source_channel"] = "A"
                        with positions_lock:
                            positions[best_a["sym"]] = new_pos
                        state["last_trade"][best_a["sym"]] = time.time()
                        save_state(state)
                        balance = get_balance()

            # 通道B空位
            if "B" not in current_channels and channel_b and balance > 10:
                best_b = channel_b[0]
                last_trade_time = state.get("last_trade", {}).get(best_b["sym"], 0)
                if time.time() - last_trade_time >= COOLDOWN_SEC:
                    trade_balance = balance * best_b["position_pct"]
                    new_pos = open_position(best_b["sym"], best_b["dir"], trade_balance, "B", best_b.get("mt_data"), regime)
                    if new_pos:
                        new_pos["score"] = best_b["score"]
                        new_pos["channel"] = "B"
                        new_pos["source_channel"] = "B"
                        with positions_lock:
                            positions[best_b["sym"]] = new_pos
                        state["last_trade"][best_b["sym"]] = time.time()
                        save_state(state)

            save_state(state)
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 手动停止")
            break
        except Exception as e:
            log(f"❌ 主循环异常: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
