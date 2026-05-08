#!/usr/bin/env python3
"""
OKX 妖币猎手 v7.0 — 候选队列动态管理
通道A: 极端FR反向 (FR<0→做多，复盘验证唯一稳定盈利)
通道B: 动量顺势 (5m涨跌+放量+趋势+链上)

复盘结论(v5.1 API账单, +$5.32 +46%):
- JTO FR<0做多赚$7.66，AI/PIPPIN FR>0做空全亏
- <0.1% FR品种全是噪音
- 通道B: 不依赖FR，靠短期动量+放量捕捉行情
"""

import subprocess, json, time, os, hmac, base64, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================
LEVERAGE = 6
TP_PCT = 0.03          # 3% 止盈
SL_PCT = 0.015         # 1.5% 止损
TRAIL_ACTIVATE = 0.015  # 浮盈1.5%后启动追踪止损
TRAIL_DISTANCE = 0.008  # 追踪止损距离0.8%
TIME_STOP_SEC = 900     # 15分钟时间止损
SCAN_INTERVAL = 30      # 30秒扫描
COOLDOWN_SEC = 1200     # 同一品种冷却20分钟
MAX_CONCURRENT = 2      # 持有候选队列Top 2（动态）
MAX_CONSECUTIVE_LOSSES = 3
LOSS_PAUSE_SEC = 1800   # 连亏暂停30分钟
SWAP_THRESHOLD = 10     # 换仓阈值：候选比持仓高10分才换（覆盖手续费）

# 通道A: 极端FR
FR_EXTREME_THRESHOLD = 0.0005   # |FR| > 0.05% 触发通道A
FR_MAX_THRESHOLD = 0.01         # |FR| > 1% 跳过（陷阱）
CHANNEL_A_VOL_SPIKE = 1.3       # 放量阈值
CHANNEL_A_POSITION_PCT = 0.90   # 仓位90%
# 复盘验证: FR<0做多赚钱，FR>0做空亏钱 → 通道A只做多
CHANNEL_A_DIRECTION = "LONG"    # 只做多(FR<0)

# 通道B: 动量顺势
MOMENTUM_SCORE_THRESHOLD = 5    # 综合评分≥5触发通道B
CHANNEL_B_POSITION_PCT = 0.60   # 仓位60%
# 方向: 顺势（涨做多，跌做空）

# 共用
MIN_24H_VOL = 2000000           # 最小24h成交量 $200万
MIN_PRICE = 0.001
OKX_TAKER_FEE = 0.0005          # 0.05% taker
ROUND_TRIP_FEE = OKX_TAKER_FEE * 2  # 0.1%

# 链上数据
CHAIN_DATA_TTL = 60        # 60秒刷新一次（实时获取）
CHAIN_BONUS = 2
CHAIN_API_BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"
CHAIN_API_TIMEOUT = 3      # 3秒超时（原8秒太长）

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
    "token_metrics": {},   # symbol -> {traders24h, trades24h, sm_pct, insider_pct, holders, kol}
    "last_update": 0
}

# ==================== 多仓管理 ====================
positions = {}
positions_lock = threading.Lock()

# ==================== 日志 ====================
SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
STATE_FILE = os.path.join(SCRIPT_DIR, "v7_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "v7_trades.log")
CHAIN_CACHE_FILE = os.path.join(SCRIPT_DIR, "v7_chain_cache.json")

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
        # 5分钟K线
        c5m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=5m&limit=10", 5)
        # 15分钟K线（新增）
        c15m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=15m&limit=6", 5)

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

        # 15分钟趋势（新增）
        if c15m.get("data") and len(c15m["data"]) >= 4:
            c15 = c15m["data"][::-1]
            cl15 = [float(c[4]) for c in c15]
            # 最近3根15m的涨跌
            chg_15m_recent = (cl15[-1] - cl15[-2]) / cl15[-2] * 100  # 最近1根
            chg_15m_2 = (cl15[-1] - cl15[-3]) / cl15[-3] * 100       # 最近2根
            chg_15m_3 = (cl15[-1] - cl15[-4]) / cl15[-4] * 100       # 最近3根
            result['chg_15m_recent'] = chg_15m_recent
            result['chg_15m_2'] = chg_15m_2
            result['chg_15m_3'] = chg_15m_3
            # 15m趋势方向：3根都同向才是真趋势
            if chg_15m_3 > 0.3:
                result['trend_15m'] = 'UP'
            elif chg_15m_3 < -0.3:
                result['trend_15m'] = 'DOWN'
            else:
                result['trend_15m'] = 'FLAT'

        return result
    except:
        return {}

def calculate_momentum_score(chg_5m, vol_1m, ups, downs, chain_bonus, fr, trend_15m=None, chg_5m_dir=None, mt_data=None):
    """通道B综合评分（满分22，含6个链上因子，集中度为减分项）"""
    score = 0

    # 0. 15m趋势一致性检查（一票否决）
    if trend_15m and chg_5m_dir:
        if chg_5m_dir == "LONG" and trend_15m == "DOWN":
            return 0
        if chg_5m_dir == "SHORT" and trend_15m == "UP":
            return 0

    # 0b. RSI极端区域（一票否决）
    rsi = mt_data.get("rsi_14", 50) if mt_data else 50
    if chg_5m_dir == "LONG" and rsi > 70:
        return 0  # 超买区做多 = 等着被瀑布
    if chg_5m_dir == "SHORT" and rsi < 30:
        return 0  # 超卖区做空 = 等着被拉爆

    # 0c. 1m反转检测（一票否决）
    if mt_data:
        last3_up = mt_data.get("last3_1m_ups", 1)
        last3_down = mt_data.get("last3_1m_downs", 1)
        if chg_5m_dir == "LONG" and last3_down >= 3:
            return 0  # 做多但最近3根1m全阴 = 正在反转
        if chg_5m_dir == "SHORT" and last3_up >= 3:
            return 0  # 做空但最近3根1m全阳 = 正在反转

    # 0e. 动量衰减（一票否决）— 3根5m涨跌幅递减
    if mt_data and mt_data.get("momentum_decay"):
        return 0  # 动量在消退，追进去就是接盘

    # 0f. 量价背离（一票否决）
    if mt_data:
        diverge = mt_data.get("vol_price_diverge", "NONE")
        if chg_5m_dir == "LONG" and diverge == "TOP":
            return 0  # 价涨量缩 = 假突破
        if chg_5m_dir == "SHORT" and diverge == "BOTTOM":
            return 0  # 价跌量缩 = 假跌破

    # 0d. 5m连续确认
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

    # 4. 链上信号 (0-4分)
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

    return score

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
        return [], []

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

        # ===== 通道A: 极端FR =====
        if fr is not None:
            abs_fr = abs(fr)
            if FR_EXTREME_THRESHOLD <= abs_fr <= FR_MAX_THRESHOLD:
                # 复盘验证: 只做FR<0做多
                if fr < 0:
                    # 必须有放量信号（避免低流动性滑点）
                    vol_1m_a = mt.get("vol_1m", 0)
                    if vol_1m_a >= CHANNEL_A_VOL_SPIKE:
                        # 跟踪FR持久性
                        now = time.time()
                        prev = FR_HISTORY.get(sym)
                        persistence_bonus = 0
                        if prev and (now - prev["ts"]) < FR_HISTORY_TTL:
                            if prev["fr"] < 0 and fr < 0:
                                persistence_bonus = 1
                        FR_HISTORY[sym] = {"fr": fr, "ts": now}

                        channel_a_candidates.append({
                            "sym": sym,
                            "dir": "LONG",
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

            # 评分（传入15m趋势+完整mt_data做多维度过滤）
            mom_score = calculate_momentum_score(chg_5m, vol_1m, ups, downs, chain_bonus, fr, trend_15m, direction, mt)

            if mom_score >= MOMENTUM_SCORE_THRESHOLD:
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

    # 时间止损
    if elapsed > TIME_STOP_SEC and abs(pct_change) < 0.3:
        log(f"⏰ {inst_id} 横盘{int(elapsed)}秒 ({pct_change:+.3f}%) → 时间止损")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "TIME_STOP"

    return "HOLDING"

# ==================== 主循环 ====================
def main():
    log("=" * 60)
    log("🔥 OKX 妖币猎手 v7.0 启动（候选队列动态管理）")
    log(f"  杠杆: {LEVERAGE}x  TP: {TP_PCT*100}%  SL: {SL_PCT*100}%")
    log(f"  通道A: FR>{FR_EXTREME_THRESHOLD*100}%做多 仓位{CHANNEL_A_POSITION_PCT*100:.0f}%")
    log(f"  通道B: 动量评分>={MOMENTUM_SCORE_THRESHOLD} 顺势 仓位{CHANNEL_B_POSITION_PCT*100:.0f}%")
    log(f"  追踪止损: 激活{TRAIL_ACTIVATE*100}% 距离{TRAIL_DISTANCE*100}%")
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

    existing = get_all_positions()
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
            positions[inst_id] = {
                "instId": inst_id, "direction": direction,
                "entry_price": entry_price, "sz": int(abs(pos_val)),
                "algo_ids": matched_algos, "open_time": time.time(),
                "notional": notional, "trail_activated": False,
                "highest_pnl_pct": 0, "fr": 0,
                "last_upl": float(p.get("upl", 0)), "channel": "?"
            }
            algo_status = f" algo={len(matched_algos)}" if matched_algos else " ⚠️无挂单"
            log(f"  📥 {inst_id} {direction} {int(abs(pos_val))}张 @ ${entry_price}{algo_status}")
        log(f"  共加载 {len(existing)} 个持仓")
    else:
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
            log(f"📡 扫描#{scan_count} 余额:${balance:.2f} 仓位:{len(positions)}")

            channel_a, channel_b, mt_data_map, fr_map = scan_dual_channel()

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

            # === 2. 统一候选队列（归一化到0-100分）===
            unified_queue = []  # (score_100, candidate_dict)

            for c in channel_a:
                # 通道A: score范围约1-10，归一化到0-100
                score_100 = min(c["score"] / 10.0 * 100, 100)
                c["score_100"] = score_100
                unified_queue.append(c)

            for c in channel_b:
                # 通道B: score范围0-22(含6个链上因子，集中度为减分项)，归一化到0-100
                score_100 = min(c["score"] / 22.0 * 100, 100)
                c["score_100"] = score_100
                unified_queue.append(c)

            # 去重（同一sym不重复）
            seen = set()
            deduped = []
            for c in unified_queue:
                if c["sym"] not in seen and c["sym"] not in positions:
                    seen.add(c["sym"])
                    deduped.append(c)
            deduped.sort(key=lambda x: x["score_100"], reverse=True)

            # 冷却过滤
            now_ts = time.time()
            cooled = []
            for c in deduped:
                last = state.get("last_trade", {}).get(c["sym"], 0)
                if now_ts - last < COOLDOWN_SEC:
                    remaining = int(COOLDOWN_SEC - (now_ts - last))
                    log(f"  ⏳ {c['sym']} 冷却中({remaining}s)")
                else:
                    cooled.append(c)

            # 打印候选队列Top5
            if cooled:
                log(f"  📋 候选队列Top5:")
                for c in cooled[:5]:
                    tags = f"[{','.join(c.get('chain_tags',[]))}]" if c.get("chain_tags") else ""
                    log(f"    [{c['channel']}] {c['sym']} {'↑' if c['dir']=='LONG' else '↓'} "
                        f"score={c['score']}({c['score_100']:.0f}/100) {tags}")
            else:
                log(f"  ⏳ 无候选")

            # === 3. 决策引擎：候选队列Top2 vs 持仓 ===
            near_settle, settle_secs = is_near_settlement()
            if near_settle:
                log(f"  ⏳ 距资金费率结算仅{settle_secs//60}分钟，暂停开仓")
                time.sleep(60)
                continue

            # 构建持仓排名（按score升序 = 最差在前）
            pos_ranked = sorted(positions.items(), key=lambda x: x[1].get("score", 0))
            # 归一化持仓评分
            pos_scores_100 = {}
            for pid, pinfo in pos_ranked:
                if pinfo.get("channel") == "A":
                    pos_scores_100[pid] = min(pinfo.get("score", 0) / 10.0 * 100, 100)
                else:
                    pos_scores_100[pid] = min(pinfo.get("score", 0) / 22.0 * 100, 100)

            # Top N候选
            top_candidates = cooled[:MAX_CONCURRENT]

            if len(cooled) > 0 and balance >= 1:
                # 有空位：直接开top候选
                slots_available = MAX_CONCURRENT - len(positions)
                if slots_available > 0:
                    n_open = min(len(top_candidates), slots_available)
                    for cand in top_candidates[:n_open]:
                        balance = get_balance()
                        if balance < 1:
                            break
                        # 按权重分配余额
                        total_weight = sum(
                            1.5 if c["channel"] == "A" else 1.0
                            for c in top_candidates[:n_open]
                        )
                        my_weight = 1.5 if cand["channel"] == "A" else 1.0
                        per_slot = balance * (my_weight / total_weight) * 0.95
                        log(f"🔥 [{cand['channel']}] {cand['sym']} {cand['dir']} "
                            f"score:{cand['score']}({cand['score_100']:.0f}/100) 仓位{per_slot/balance*100:.0f}%")
                        pos = open_position(cand["sym"], cand["dir"], per_slot, cand["channel"])
                        if pos:
                            pos["fr"] = cand.get("fr", 0)
                            pos["score"] = cand.get("score", 0)
                            with positions_lock:
                                positions[cand["sym"]] = pos
                            save_state(state)
                        time.sleep(5)

                # 无空位：检查是否有值得换仓的
                elif len(pos_ranked) > 0 and len(cooled) > 0:
                    # 最佳候选 vs 最差持仓（归一化后比较）
                    best_cand = top_candidates[0]
                    worst_pos_id, worst_pos = pos_ranked[0]
                    worst_score_100 = pos_scores_100.get(worst_pos_id, 0)
                    best_score_100 = best_cand["score_100"]

                    # 评分差 > 换仓阈值（覆盖手续费成本）
                    score_diff = best_score_100 - worst_score_100
                    if score_diff >= SWAP_THRESHOLD:
                        log(f"  🔄 换仓: {worst_pos_id}(score={worst_pos.get('score',0)}→{worst_score_100:.0f}/100) "
                            f"→ {best_cand['sym']}({best_cand['score']}→{best_score_100:.0f}/100) 差值:{score_diff:.0f}")
                        close_position(worst_pos_id, positions[worst_pos_id].get("algo_ids"))
                        time.sleep(1)
                        if worst_pos_id in positions:
                            with positions_lock:
                                del positions[worst_pos_id]
                        state["last_trade"][worst_pos_id] = time.time()

                        # 开新仓
                        balance = get_balance()
                        if balance >= 1:
                            per_slot = balance * 0.90
                            log(f"🔥 [{best_cand['channel']}] {best_cand['sym']} {best_cand['dir']} "
                                f"score:{best_cand['score']} 仓位{per_slot/balance*100:.0f}%")
                            pos = open_position(best_cand["sym"], best_cand["dir"], per_slot, best_cand["channel"])
                            if pos:
                                pos["fr"] = best_cand.get("fr", 0)
                                pos["score"] = best_cand.get("score", 0)
                                with positions_lock:
                                    positions[best_cand["sym"]] = pos
                        save_state(state)
                    else:
                        log(f"  ⏳ Top候选({best_score_100:.0f}) - 最差持仓({worst_score_100:.0f}) "
                            f"= {score_diff:.0f} < 阈值{SWAP_THRESHOLD}，不换")

            # 打印持仓实时状态
            if positions:
                for pid, pinfo in positions.items():
                    ch = pinfo.get("channel", "?")
                    sc = pinfo.get("score", 0)
                    log(f"  💼 [{ch}] {pid} {pinfo.get('direction','?')} score={sc}")

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
