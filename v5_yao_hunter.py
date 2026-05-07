#!/usr/bin/env python3
"""
OKX 妖币猎手 v5.1 — 反向猎杀策略
核心逻辑：极端资金费率 + 异常放量 → 反向开仓猎杀散户

妖币特征：
1. 资金费率极端（|rate| > 0.03%）→ 散户集中做多/做空
2. 成交量暴增（相对均量150%+）→ 主力开始行动
3. 反向开仓：费率极端负→做多（爆空），费率极端正→做空（爆多）
"""

import subprocess, json, time, os, hmac, base64, sys, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================
LEVERAGE = 5
TP_PCT = 0.03          # 3% 止盈（妖币行情大）
SL_PCT = 0.015         # 1.5% 止损
CRASH_SL_PCT = 0.025   # 2.5% 暴跌止损
TRAIL_ACTIVATE = 0.015  # 浮盈1.5%后启动追踪止损
TRAIL_DISTANCE = 0.008  # 追踪止损距离0.8%
TIME_STOP_SEC = 600     # 10分钟时间止损（妖币行情需要更多时间）
SCAN_INTERVAL = 20      # 20秒扫描一次
COOLDOWN_SEC = 600      # 同一品种冷却10分钟
MAX_CONCURRENT = 2      # 最多同时持仓2个
MAX_CONSECUTIVE_LOSSES = 3
LOSS_PAUSE_SEC = 1800

# 妖币筛选阈值
FUNDING_RATE_EXTREME = 0.0003   # |费率| > 0.03% 视为极端
VOLUME_SPIKE_RATIO = 1.1        # 成交量 > 均量110% 视为放量
MIN_24H_VOL = 1000000           # 最小24h成交量 $100万
MIN_PRICE = 0.001               # 最低价格
MAX_FUNDING_RATE = 0.01         # |费率| > 1% 跳过（太极端可能有陷阱）
OKX_TAKER_FEE = 0.0005          # 0.05% taker fee per side
ROUND_TRIP_FEE = OKX_TAKER_FEE * 2  # 0.1% round trip

# 链上数据配置（Binance Web3 公开API，无需认证）
CHAIN_DATA_TTL = 60        # 链上数据60秒刷新一次
CHAIN_BONUS = 2            # 链上数据匹配的加分值
CHAIN_API_BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"


# 时间窗口 (UTC+8) — 24小时
TRADING_WINDOWS = [(0, 0, 23, 59)]

# ==================== 缓存 ====================
INSTRUMENTS_CACHE = {}
FR_HISTORY = {}  # instId -> [last_fr, timestamp] for persistence check
FR_HISTORY_TTL = 300  # 5 minute window for persistence
SKIP_SYMS = {"RLS-USDT-SWAP", "BILL-USDT-SWAP"}

# 链上数据缓存
CHAIN_CACHE = {
    "smart_money_buy": set(),    # 聪明钱买入的代币符号
    "hot_topics": set(),         # AI热门话题关联代币符号
    "smart_money_inflow": set(), # 聪明钱净流入Top代币符号
    "last_update": 0
}

# ==================== 多仓管理 ====================
positions = {}
positions_lock = threading.Lock()

# ==================== 日志 ====================
SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
STATE_FILE = os.path.join(SCRIPT_DIR, "v5_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "v5_trades.log")

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
    return {"consecutive_losses": 0, "pause_until": None, "last_trade": {}, "total_pnl": 0}

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
    import requests
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
    import requests
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
                timeout=8
            )
            if d.get("data"):
                for sig in d["data"]:
                    if sig.get("direction") == "buy" and sig.get("smartMoneyCount", 0) >= 3:
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
            d = curl_json(f"{url}?chainId={chain_id}&rankType=20&sort=20&asc=false", timeout=8)
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
            d = curl_json_post(url, {"chainId": chain_id, "period": "4h", "tagType": 2}, timeout=8)
            if d.get("data"):
                for item in d.get("data", [])[:20]:
                    sym = (item.get("tokenName") or "").upper()
                    if sym:
                        result.add(sym)
        except:
            pass
    return result

def update_chain_cache():
    now = time.time()
    if now - CHAIN_CACHE["last_update"] < CHAIN_DATA_TTL:
        return
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_sm = ex.submit(fetch_smart_money_signals)
        f_ht = ex.submit(fetch_hot_topic_tokens)
        f_in = ex.submit(fetch_smart_money_inflow)
        CHAIN_CACHE["smart_money_buy"] = f_sm.result()
        CHAIN_CACHE["hot_topics"] = f_ht.result()
        CHAIN_CACHE["smart_money_inflow"] = f_in.result()
    CHAIN_CACHE["last_update"] = now
    log(f"  [chain] SM_buy={len(CHAIN_CACHE['smart_money_buy'])} topics={len(CHAIN_CACHE['hot_topics'])} SM_inflow={len(CHAIN_CACHE['smart_money_inflow'])}")

def get_chain_score(ticker_symbol):
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
    return score, tags


# ==================== 时间窗口 ====================
# Funding rate settlement: 00:00/08:00/16:00 UTC = 08:00/16:00/00:00 Beijing
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

# ==================== 资金费率扫描 ====================
def get_funding_rates_batch(syms):
    """并行获取资金费率"""
    results = {}
    def get_fr(sym):
        try:
            d = curl_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}", 5)
            if d.get("code") == "0" and d.get("data"):
                return sym, float(d["data"][0]["fundingRate"])
        except:
            pass
        return sym, None
    
    with ThreadPoolExecutor(max_workers=15) as ex:
        for sym, rate in ex.map(get_fr, syms):
            if rate is not None:
                results[sym] = rate
    return results



# ==================== 妖币扫描 ====================
def scan_yao_market():
    """扫描全市场，筛选妖币候选"""
    # 1. 获取所有USDT-SWAP行情
    d = curl_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP", 15)
    if not d.get("data"):
        return []
    
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
            "high24h": float(x.get("high24h", "0")),
            "low24h": float(x.get("low24h", "0")),
        })
    
    # 取成交量Top60
    top = sorted(usdt, key=lambda x: x["vol24h"], reverse=True)[:60]
    top = [x for x in top if x["instId"] not in SKIP_SYMS]
    syms = [x["instId"] for x in top]
    
    # 2. 并行获取资金费率
    fr_map = get_funding_rates_batch(syms)
    
    # 3. 筛选极端费率品种
    candidates = []
    for item in top:
        sym = item["instId"]
        fr = fr_map.get(sym)
        if fr is None:
            continue
        
        abs_fr = abs(fr)
        
        # 费率太低（正常）→ 跳过
        if abs_fr < FUNDING_RATE_EXTREME:
            continue
        
        # 费率太高（陷阱）→ 跳过
        if abs_fr > MAX_FUNDING_RATE:
            continue
        
        # 费率极端负 → 做多（散户做空，主力可能拉盘爆空）
        # 费率极端正 → 做空（散户做多，主力可能砸盘爆多）
        direction = "LONG" if fr < 0 else "SHORT"
        
        # Track FR history for persistence check
        now = time.time()
        prev = FR_HISTORY.get(sym)
        persistence_bonus = 0
        if prev and (now - prev["ts"]) < FR_HISTORY_TTL:
            # If same direction extreme persisted, give bonus
            if (prev["fr"] < 0 and fr < 0) or (prev["fr"] > 0 and fr > 0):
                persistence_bonus = 1  # Bonus point for sustained extreme
        
        FR_HISTORY[sym] = {"fr": fr, "ts": now}
        
        candidates.append({
            "sym": sym,
            "dir": direction,
            "fr": fr,
            "abs_fr": abs_fr,
            "vol24h": item["vol24h"],
            "last": item["last"],
            "score": abs_fr * 10000 + persistence_bonus,  # will be updated with chain bonus below
        })
    
    # Add chain data bonus
    update_chain_cache()
    for c in candidates:
        ticker = c["sym"].replace("-USDT-SWAP", "")
        chain_bonus, chain_tags = get_chain_score(ticker)
        c["score"] += chain_bonus
        c["chain_tags"] = chain_tags
    
    # Sort by score (abs_fr + persistence + chain bonus)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

# ==================== 成交量放量检测 ====================
def check_volume_spike(sym):
    """检查最近成交量是否放量（相对5m均量）"""
    try:
        d = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=5m&limit=30", 5)
        if not d.get("data") or len(d["data"]) < 20:
            return False, 0
        
        candles = d["data"][::-1]  # 时间正序
        vols = [float(c[5]) for c in candles]  # vol字段
        
        # 最近3根K线平均成交量 vs 前20根均量
        recent_avg = sum(vols[-3:]) / 3
        hist_avg = sum(vols[:-3]) / max(len(vols[:-3]), 1)
        
        if hist_avg == 0:
            return False, 0
        
        ratio = recent_avg / hist_avg
        return ratio >= VOLUME_SPIKE_RATIO, ratio
    except:
        return False, 0

# ==================== 交易操作 ====================
def get_balance():
    d = okx_get("/api/v5/account/balance?ccy=USDT")
    if d.get("data"):
        det = d["data"][0]["details"][0]
        return float(det.get("availBal") or det.get("eq") or 0)
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
    
    # Fix: 先取消algo挂单，再市价平仓
    if algo_ids:
        for aid in algo_ids:
            okx_post("/api/v5/trade/cancel-algos", json.dumps([{"instId": inst_id, "algoId": aid}]))
    
    side = "sell" if pos["pos"] > 0 else "buy"
    r = okx_post("/api/v5/trade/order", json.dumps({
        "instId": inst_id, "tdMode": "cross",
        "side": side, "ordType": "market", "sz": str(int(abs(pos["pos"])))
    }))
    
    return r

def open_position(inst_id, direction, balance_for_trade):
    """妖币开仓 — 宽止盈止损 + 追踪止损"""
    specs = INSTRUMENTS_CACHE.get(inst_id)
    if not specs:
        log(f"❌ {inst_id} 缓存未命中")
        return None
    
    leverage = min(LEVERAGE, int(specs["maxLev"]))
    margin = balance_for_trade * 0.95
    notional_max = margin * leverage
    
    # 获取当前价格
    d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", 5)
    if not d.get("data"):
        return None
    price = float(d["data"][0]["last"])
    
    lots = int(notional_max / (specs["ctVal"] * price))
    if lots < 1:
        return None
    
    # Fix: Fee pre-check - ensure potential profit at TP > round-trip fees
    notional_actual = lots * specs["ctVal"] * price
    round_trip_fee_cost = notional_actual * ROUND_TRIP_FEE
    potential_tp_profit = notional_actual * TP_PCT
    if potential_tp_profit < round_trip_fee_cost:
        log(f"  ❌ {inst_id} TP利润${potential_tp_profit:.4f} < 手续费${round_trip_fee_cost:.4f} → 跳过")
        return None
    
    # 价格格式化
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
        crash = format_price(price * (1 - CRASH_SL_PCT))
        side = "buy"
        close_side = "sell"
    else:
        tp = format_price(price * (1 - TP_PCT))
        sl = format_price(price * (1 + SL_PCT))
        crash = format_price(price * (1 + CRASH_SL_PCT))
        side = "sell"
        close_side = "buy"
    
    # TP/SL精度检查
    tp_diff = abs(price * TP_PCT)
    sl_diff = abs(price * SL_PCT)
    if tp_diff < tick_sz * 2 or sl_diff < tick_sz * 2:
        log(f"  ❌ {inst_id} 价格太低(${price})，TP/SL差价不足 → 跳过")
        return None
    
    # 设置杠杆
    okx_post("/api/v5/account/set-leverage", json.dumps({
        "instId": inst_id, "lever": str(leverage), "mgnMode": "cross"
    }))
    
    # 开仓
    result = okx_post("/api/v5/trade/order", json.dumps({
        "instId": inst_id, "tdMode": "cross",
        "side": side, "ordType": "market", "sz": str(lots)
    }))
    
    if result.get("code") != "0":
        log(f"❌ {inst_id} 开仓失败: code={result.get('code')} msg={result.get('msg', '')}")
        return None
    
    time.sleep(0.5)
    
    # 查持仓
    pos = get_position(inst_id)
    if not pos:
        log(f"❌ {inst_id} 获取持仓失败")
        return None
    
    avg = pos["avgPx"]
    sz = int(abs(pos["pos"]))
    
    # 挂TP+SL+CRASH
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
    
    with ThreadPoolExecutor(max_workers=3) as ex:
        ex.submit(place_algo, "TP", tp, "tpTriggerPx")
        ex.submit(place_algo, "SL", sl, "slTriggerPx")
        ex.submit(place_algo, "CRASH", crash, "slTriggerPx")
    time.sleep(0.3)
    
    tp_ok = "TP" in placed_labels
    sl_ok = "SL" in placed_labels or "CRASH" in placed_labels
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
    
    log(f"✅ 开{'多' if direction=='LONG' else '空'} {inst_id} {sz}张 @ ${avg} TP=${tp} SL=${sl}")
    
    return {
        "instId": inst_id, "direction": direction,
        "entry_price": avg, "sz": sz,
        "algo_ids": algo_ids, "open_time": time.time(),
        "notional": notional_actual, "trail_activated": False,
        "highest_pnl_pct": 0, "fr": 0,
        "last_upl": 0  # 首次monitor前为0，CLOSED时保守判定
    }

# ==================== 持仓监控（带追踪止损） ====================
def monitor_position(inst_id, pos_info):
    """监控单个仓位，支持追踪止损"""
    entry = pos_info["entry_price"]
    direction = pos_info["direction"]
    open_time = pos_info["open_time"]
    
    pos = get_position(inst_id)
    if not pos:
        return "CLOSED"
    
    d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", 5)
    current_price = float(d["data"][0]["last"]) if d.get("data") else pos["avgPx"]
    
    upl = pos["upl"]
    pos_info["last_upl"] = upl  # 记录最新upl，用于CLOSED状态判断盈亏
    elapsed = time.time() - open_time
    
    if direction == "LONG":
        pct_change = (current_price - entry) / entry * 100
    else:
        pct_change = (entry - current_price) / entry * 100
    
    # 更新最高浮盈
    if pct_change > pos_info.get("highest_pnl_pct", 0):
        pos_info["highest_pnl_pct"] = pct_change
    
    # 追踪止损逻辑
    if pos_info.get("trail_activated"):
        trail_stop_pct = pos_info["highest_pnl_pct"] - TRAIL_DISTANCE * 100
        if pct_change < trail_stop_pct:
            log(f"🔄 {inst_id} 追踪止损! 最高{pos_info['highest_pnl_pct']:+.2f}% → 当前{pct_change:+.2f}% (止损线{trail_stop_pct:+.2f}%)")
            close_position(inst_id, pos_info.get("algo_ids"))
            return "TRAIL_STOP"
    elif pct_change >= TRAIL_ACTIVATE * 100:
        pos_info["trail_activated"] = True
        log(f"🔔 {inst_id} 追踪止损激活! 浮盈{pct_change:+.2f}% > {TRAIL_ACTIVATE*100}%")
    
    # 紧急止盈 — 浮盈达到TP_PCT*2（6%）时立即落袋，防暴涨后回撤
    # 追踪止损管理1.5%-6%的利润区间，紧急止盈捕获极端利润
    emergency_tp = TP_PCT * 200  # 6%
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
    log("🔥 OKX 妖币猎手 v5.1 启动")
    log(f"  杠杆: {LEVERAGE}x  TP: {TP_PCT*100}%  SL: {SL_PCT*100}%")
    log(f"  费率极端: >{FUNDING_RATE_EXTREME*100}%  放量: >{VOLUME_SPIKE_RATIO*100}%")
    log(f"  追踪止损: 激活{TRAIL_ACTIVATE*100}% 距离{TRAIL_DISTANCE*100}%")
    log(f"  最大同时持仓: {MAX_CONCURRENT}")
    log("=" * 60)
    
    load_instruments_cache()
    state = load_state()
    scan_count = 0
    
    # Step 1: Query ALL pending algo orders first (before any cleanup)
    log("📥 加载已有持仓和挂单...")
    all_pending_algos = okx_get("/api/v5/trade/orders-algo-pending?ordType=conditional")
    algo_map = {}  # instId -> [algoId, ...]
    if all_pending_algos.get("data"):
        for a in all_pending_algos["data"]:
            aid_inst = a.get("instId", "")
            if aid_inst not in algo_map:
                algo_map[aid_inst] = []
            algo_map[aid_inst].append(a["algoId"])
    
    # Step 2: Load existing positions and match with algos
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
                "last_upl": float(p.get("upl", 0))  # 恢复时初始化upl用于CLOSED盈亏判断
            }
            algo_status = f" algo={len(matched_algos)}" if matched_algos else " ⚠️无挂单"
            log(f"  📥 {inst_id} {direction} {int(abs(pos_val))}张 @ ${entry_price}{algo_status}")
        log(f"  共加载 {len(existing)} 个持仓")
    else:
        log("  无持仓")
    
    # Step 3: Cancel stale algos that DON'T belong to any recovered position
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
            # 时间窗口检查
            if not in_trading_window():
                time.sleep(120)
                continue
            
            # 暂停检查
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
            
            # 并行监控所有持仓
            if positions:
                for inst_id in list(positions.keys()):
                    try:
                        result = monitor_position(inst_id, positions[inst_id])
                    except Exception as e:
                        log(f"⚠️ 监控 {inst_id} 异常: {e}")
                        continue
                    
                    if result in ("PROFIT", "TRAIL_STOP", "TIME_STOP", "CLOSED"):
                        # CLOSED状态：查询last_upl判断盈亏（algo单触发的平仓）
                        if result == "CLOSED":
                            pos_upl = positions[inst_id].get("last_upl", None)
                            if pos_upl is not None and pos_upl > 0:
                                state["consecutive_losses"] = 0
                                state["total_pnl"] = state.get("total_pnl", 0) + 1
                                log(f"  📊 {inst_id} CLOSED(盈利) upl=${pos_upl:.4f}")
                            else:
                                # last_upl为None/0说明没读到实际upl，保守算亏损
                                state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
                                if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                                    pause_until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
                                    state["pause_until"] = pause_until.isoformat()
                                    log(f"⏸️ {state['consecutive_losses']}连亏，暂停{LOSS_PAUSE_SEC//60}分钟")
                                log(f"  📊 {inst_id} CLOSED(亏损) upl=${pos_upl if pos_upl else 'N/A'}")
                        
                        with positions_lock:
                            if inst_id in positions:
                                del positions[inst_id]
                        
                        if result in ("PROFIT", "TRAIL_STOP"):
                            state["consecutive_losses"] = 0
                            state["total_pnl"] = state.get("total_pnl", 0) + 1
                        elif result == "TIME_STOP":
                            state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
                            if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                                pause_until = datetime.now() + timedelta(seconds=LOSS_PAUSE_SEC)
                                state["pause_until"] = pause_until.isoformat()
                                log(f"⏸️ {state['consecutive_losses']}连亏，暂停{LOSS_PAUSE_SEC//60}分钟")
                        
                        state["last_trade"][inst_id] = time.time()
                        save_state(state)
            
            # 安全检查：验证所有持仓都有TP/SL挂单
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
                        # 安全检查平仓记录为亏损（无法判断盈亏，默认触发连亏保护）
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
            
            # 扫描妖币信号
            active_count = len(positions)
            if active_count < MAX_CONCURRENT:
                scan_count += 1
                balance = get_balance()
                log(f"📡 妖币扫描#{scan_count} 余额:${balance:.2f} 仓位:{active_count}/{MAX_CONCURRENT}")
                
                candidates = scan_yao_market()
                
                if candidates:
                    log(f"  🎯 发现{len(candidates)}个妖币候选:")
                    for c in candidates[:5]:
                        fr_pct = c["fr"] * 100
                        chain_tags = c.get("chain_tags", [])
                        tag_str = f" [{','.join(chain_tags)}]" if chain_tags else ""
                        log(f"    {c['sym']} {'↑多' if c['dir']=='LONG' else '↓空'} FR={fr_pct:+.4f}% vol24h=${c['vol24h']/1e6:.1f}M{tag_str}")
                    
                    # 检查放量（取前5个候选）
                    confirmed = []
                    for c in candidates[:5]:
                        # 费率>0.1%（极端）跳过放量检查，直接确认
                        if c["abs_fr"] >= 0.001:
                            c["vol_ratio"] = 99  # 标记为极端信号
                            confirmed.append(c)
                            log(f"  🔥 {c['sym']} 极端费率{c['fr']*100:+.4f}% 跳过放量检查")
                            continue
                        is_spike, ratio = check_volume_spike(c["sym"])
                        if is_spike:
                            c["vol_ratio"] = ratio
                            confirmed.append(c)
                            log(f"  ✅ {c['sym']} 放量确认! {ratio:.1f}x均量")
                        else:
                            log(f"  ⏳ {c['sym']} 未放量({ratio:.1f}x) 等待...")
                    
                    # 开仓（含冷却检查）
                    slots = MAX_CONCURRENT - active_count
                    if confirmed and slots > 0:
                        # Fix: Filter out symbols on cooldown (last traded within COOLDOWN_SEC)
                        now_ts = time.time()
                        cooled = []
                        for c in confirmed:
                            last = state.get("last_trade", {}).get(c["sym"], 0)
                            if now_ts - last < COOLDOWN_SEC:
                                remaining = int(COOLDOWN_SEC - (now_ts - last))
                                log(f"  ⏳ {c['sym']} 冷却中({remaining}s)")
                            else:
                                cooled.append(c)
                        
                        if not cooled:
                            log(f"  ⏳ 所有候选品种均在冷却中")
                            time.sleep(SCAN_INTERVAL)
                            continue
                        
                        # Check settlement proximity
                        near_settle, settle_secs = is_near_settlement()
                        if near_settle:
                            log(f"  ⏳ 距资金费率结算仅{settle_secs//60}分钟，暂停开仓")
                            time.sleep(60)
                            continue
                        
                        n_open = min(len(cooled), slots)
                        per_slot = balance / max(n_open + active_count, 1)
                        if per_slot >= 1:
                            for cand in cooled[:n_open]:
                                log(f"🔥 妖币开仓: {cand['sym']} {cand['dir']} FR={cand['fr']*100:+.4f}% vol={cand['vol_ratio']:.1f}x")
                                pos = open_position(cand["sym"], cand["dir"], per_slot)
                                if pos:
                                    pos["fr"] = cand["fr"]
                                    with positions_lock:
                                        positions[cand["sym"]] = pos
                                    save_state(state)
                                time.sleep(5)
                    else:
                        if not confirmed:
                            log(f"  ⏳ 无放量确认品种，等待下一周期...")
                        elif slots == 0:
                            log(f"  ⏳ 已满仓，等待持仓平仓...")
                else:
                    log(f"  ❌ 无极端费率品种 (扫描{60}个)")
            
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