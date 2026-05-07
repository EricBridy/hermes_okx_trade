#!/usr/bin/env python3
"""
OKX 剥头皮 v4.2 — 多仓并行版
同时持3-5个仓位，开仓尽量快（并行API），不跳过扫描
"""

import subprocess, json, time, os, hmac, base64, sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import threading

# ==================== 配置 ====================
LEVERAGE = 5
TP_PCT = 0.0105     # 1.05%
SL_PCT = 0.007      # 0.7%
CRASH_SL_PCT = 0.015 # 1.5%
TIME_STOP_SEC = 300  # 5分钟时间止损
SCAN_INTERVAL = 15   # 15秒扫描一次
COOLDOWN_SEC = 300   # 同一品种冷却5分钟
MAX_CONSECUTIVE_LOSSES = 3
LOSS_PAUSE_SEC = 1800
MIN_VOLATILITY = 0.3   # 放宽：0.5→0.3
ADX_THRESHOLD = 30      # ADX>30强趋势，提高胜率
MAX_CONCURRENT = 2      # 只开2个最高分仓位
SKIP_SYMS = {"RLS-USDT-SWAP", "BILL-USDT-SWAP"}

# 时间窗口 (UTC+8) — 24小时不间断交易
TRADING_WINDOWS = [
    (0, 0, 23, 59),
]

# ==================== 缓存 ====================
INSTRUMENTS_CACHE = {}
FUNDING_RATE_CACHE = {}
FR_CACHE_TTL = 300     # 资金费率缓存5分钟


# ==================== 多仓管理 ====================
positions = {}  # {inst_id: {direction, entry_price, sz, algo_ids, open_time, notional}}
positions_lock = threading.Lock()

# ==================== 日志 ====================
SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
STATE_FILE = os.path.join(SCRIPT_DIR, "v4_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "v4_trades.log")

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
    return {"consecutive_losses": 0, "pause_until": None, "last_trade": {}, "total_pnl": 0, "balance": 0}

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
    """带重试的curl请求"""
    for attempt in range(retries):
        try:
            r = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url], capture_output=True, text=True, timeout=timeout+5)
            d = json.loads(r.stdout)
            if d.get("code") == "50011":  # OKX rate limit
                time.sleep(0.5 * (attempt + 1))
                continue
            return d
        except:
            pass
    return {}

# ==================== 时间窗口 ====================
def in_trading_window():
    now = datetime.now(timezone(timedelta(hours=8)))
    h, m = now.hour, now.minute
    for sh, sm, eh, em in TRADING_WINDOWS:
        if (h > sh or (h == sh and m >= sm)) and (h < eh or (h == eh and m <= em)):
            return True
    return False

# ==================== 合约规格缓存 ====================
def load_instruments_cache():
    global INSTRUMENTS_CACHE
    try:
        d = curl_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP", 15)
        if d.get("data"):
            for inst in d["data"]:
                inst_id = inst["instId"]
                INSTRUMENTS_CACHE[inst_id] = {
                    "ctVal": float(inst.get("ctVal") or 1),
                    "lotSz": float(inst.get("lotSz") or 1),
                    "minSz": float(inst.get("minSz") or 1),
                    "maxLev": float(inst.get("lever") or 10),
                    "tickSz": inst.get("tickSz", "0.00001"),
                }
            log(f"✅ 合约规格缓存: {len(INSTRUMENTS_CACHE)}个")
    except Exception as e:
        log(f"⚠️ 缓存加载失败: {e}")

# ==================== 资金费率缓存 ====================
def get_funding_rates_cached(syms):
    now = time.time()
    result = {}
    need_refresh = []
    
    for sym in syms:
        if sym in FUNDING_RATE_CACHE:
            cache = FUNDING_RATE_CACHE[sym]
            if now - cache["timestamp"] < FR_CACHE_TTL:
                result[sym] = cache["rate"]
            else:
                need_refresh.append(sym)
        else:
            need_refresh.append(sym)
    
    if need_refresh:
        def get_fr(sym):
            try:
                d = curl_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}", 5)
                if d.get("code") == "0" and d["data"]:
                    rate = float(d["data"][0]["fundingRate"])
                    FUNDING_RATE_CACHE[sym] = {"rate": rate, "timestamp": now}
                    return sym, rate
            except:
                pass
            return sym, None
        
        with ThreadPoolExecutor(max_workers=15) as ex:
            for k, v in ex.map(get_fr, need_refresh):
                if v is not None:
                    result[k] = v
    
    return result

# ==================== K线信号检查 ====================
def ema(closes, period):
    """Calculate EMA"""
    if len(closes) < period:
        return closes[-1] if closes else 0
    multiplier = 2 / (period + 1)
    ema_val = sum(closes[:period]) / period
    for price in closes[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val
    return ema_val

def calc_adx(highs, lows, closes, period=14):
    """Calculate ADX (Average Directional Index)"""
    n = len(highs)
    if n < period + 2:
        return 0
    
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, n):
        high_diff = highs[i] - highs[i-1]
        low_diff = lows[i-1] - lows[i]
        plus_dm.append(max(high_diff, 0) if high_diff > low_diff else 0)
        minus_dm.append(max(low_diff, 0) if low_diff > high_diff else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    
    if len(tr_list) < period:
        return 0
    
    # Wilder's smoothing
    atr = sum(tr_list[:period]) / period
    plus_di_s = sum(plus_dm[:period]) / period
    minus_di_s = sum(minus_dm[:period]) / period
    dx_vals = []
    
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        plus_di_s = (plus_di_s * (period - 1) + plus_dm[i]) / period
        minus_di_s = (minus_di_s * (period - 1) + minus_dm[i]) / period
        if atr > 0:
            pdi = 100 * plus_di_s / atr
            mdi = 100 * minus_di_s / atr
            s = pdi + mdi
            if s > 0:
                dx_vals.append(abs(pdi - mdi) / s * 100)
    
    if len(dx_vals) < period:
        return sum(dx_vals) / max(len(dx_vals), 1)
    
    adx = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx

def check_signals(sym):
    """EMA9/21交叉 + ADX趋势强度 + 波动率"""
    try:
        # 拉足够多的K线用于EMA21+ADX(14)计算
        d1m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=1m&limit=25", 5)
        d5m = curl_json(f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=5m&limit=25", 5)

        trend_1m = "ERR"
        if d1m.get("data") and len(d1m["data"]) >= 21:
            c1m = d1m["data"][::-1]
            closes_1m = [float(c[4]) for c in c1m]
            ema9 = ema(closes_1m, 9)
            ema21 = ema(closes_1m, 21)
            trend_1m = "UP" if ema9 > ema21 else "DOWN"

        vol_5m = 0
        pos_range = 50
        trend_5m = "ERR"
        adx = 0
        if d5m.get("data") and len(d5m["data"]) >= 21:
            c5m = d5m["data"][::-1]
            highs = [float(c[2]) for c in c5m]
            lows = [float(c[3]) for c in c5m]
            closes_5m = [float(c[4]) for c in c5m]
            
            # EMA趋势
            ema9_5m = ema(closes_5m, 9)
            ema21_5m = ema(closes_5m, 21)
            trend_5m = "UP" if ema9_5m > ema21_5m else "DOWN"
            
            # ADX趋势强度
            adx = calc_adx(highs, lows, closes_5m, 14)
            if adx < ADX_THRESHOLD:
                trend_5m = "FLAT"
            
            # 波动率（最近5根K线平均振幅）
            recent = c5m[-5:]
            ranges = [(float(c[2]) - float(c[3])) / float(c[4]) * 100 for c in recent]
            vol_5m = sum(ranges) / len(ranges)
            
            # 价格位置
            recent_high = max(float(c[2]) for c in recent)
            recent_low = min(float(c[3]) for c in recent)
            if recent_high > recent_low:
                pos_range = (float(recent[-1][4]) - recent_low) / (recent_high - recent_low) * 100

        return {"trend_1m": trend_1m, "trend_5m": trend_5m, "vol_5m": vol_5m, "pos_range": pos_range, "adx": adx}
    except:
        return {"trend_1m": "ERR", "trend_5m": "ERR", "vol_5m": 0, "pos_range": 50, "adx": 0}

# ==================== 市场扫描 ====================
def scan_market():
    d = curl_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP", 15)
    if not d.get("data"):
        return []

    usdt = []
    for x in d["data"]:
        if not x["instId"].endswith("USDT-SWAP"):
            continue
        usdt.append({
            "instId": x["instId"], "last": float(x.get("last", "0")),
            "vol24h": float(x.get("volCcy24h", "0")),
            "high24h": float(x.get("high24h", "0")),
            "low24h": float(x.get("low24h", "0")),
        })

    top = sorted([x for x in usdt if x["vol24h"] > 200000 and x["last"] > 0.001],
                 key=lambda x: x["vol24h"], reverse=True)
    top = [x for x in top if x["instId"] not in SKIP_SYMS]
    log(f"  📊 扫描范围: {len(top)}个合约 (vol24h>200k)")

    return top

# ==================== 交易操作 ====================
def get_balance():
    d = okx_get("/api/v5/account/balance?ccy=USDT")
    if d.get("data"):
        det = d["data"][0]["details"][0]
        return float(det.get("availBal") or det.get("eq") or 0)
    return 0

def get_all_positions():
    """获取所有活跃持仓"""
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
                    "notionalUsd": float(pos.get("notionalUsd", 0)),
                })
    return result

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

def close_position(inst_id, algo_ids=None):
    pos = get_position(inst_id)
    if not pos:
        return {"code": "0", "msg": "already closed"}

    side = "sell" if pos["pos"] > 0 else "buy"
    r = okx_post("/api/v5/trade/order", json.dumps({
        "instId": inst_id, "tdMode": "cross",
        "side": side, "ordType": "market", "sz": str(int(abs(pos["pos"])))
    }))

    if algo_ids:
        for aid in algo_ids:
            okx_post("/api/v5/trade/cancel-algos", json.dumps([{"instId": inst_id, "algoId": aid}]))

    return r

def open_position_fast(inst_id, direction, balance_for_trade):
    """快速开仓+挂TP/SL（最小化延迟）"""
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

    # 预计算TP/SL价格（使用合约tickSz精度，避免科学计数法）
    tick_sz = float(specs.get("tickSz", "0.00001"))
    tick_decimals = len(specs.get("tickSz", "0.00001").rstrip("0").split(".")[-1]) if "." in specs.get("tickSz", "0.00001") else 0

    def format_price(p):
        """按照合约tick精度格式化价格，避免科学计数法"""
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

    # 检查TP/SL差价是否太小（低于2个tick就跳过）
    tp_diff = abs(price * TP_PCT)
    sl_diff = abs(price * SL_PCT)
    if tp_diff < tick_sz * 2 or sl_diff < tick_sz * 2:
        log(f"  ❌ {inst_id} 价格太低(${price})，TP/SL差价不足2个tick → 跳过")
        return None

    # 设置杠杆（并行不阻塞）
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

    # 等成交
    time.sleep(0.3)

    # 查持仓
    pos = get_position(inst_id)
    if not pos:
        log(f"❌ {inst_id} 获取持仓失败")
        return None

    avg = pos["avgPx"]
    sz = int(abs(pos["pos"]))

    # 挂TP+SL+CRASH（并行3个请求）
    algo_ids = []
    placed_labels = set()
    lock = threading.Lock()

    def place_algo(label, px, trigger_key):
        # TP用tpOrdPx，SL/CRASH用slOrdPx
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

    # 3个algo并行挂
    with ThreadPoolExecutor(max_workers=3) as ex:
        ex.submit(place_algo, "TP", tp, "tpTriggerPx")
        ex.submit(place_algo, "SL", sl, "slTriggerPx")
        ex.submit(place_algo, "CRASH", crash, "slTriggerPx")
    # wait for all to finish
    time.sleep(0.2)

    # TP是必须的，SL/CRASH至少有一个 → 否则强制平仓（重试3次）
    tp_ok = "TP" in placed_labels
    sl_ok = "SL" in placed_labels or "CRASH" in placed_labels
    if not tp_ok or not sl_ok:
        log(f"⚠️ {inst_id} TP={tp_ok} SL={sl_ok} (已挂: {placed_labels}) → 强制平仓")
        for close_attempt in range(3):
            time.sleep(1)
            r = close_position(inst_id, algo_ids)
            verify = get_position(inst_id)
            if not verify:
                log(f"✅ {inst_id} 裸仓已平仓 (attempt {close_attempt+1})")
                break
            log(f"⚠️ {inst_id} 平仓尝试{close_attempt+1}失败，重试...")
        else:
            log(f"🚨 {inst_id} 平仓3次失败！需手动处理")
        return None

    log(f"✅ 开{'多' if direction=='LONG' else '空'} {inst_id} {sz}张 @ ${avg} TP=${tp} SL=${sl} CRASH=${crash}")

    return {
        "instId": inst_id, "direction": direction,
        "entry_price": avg, "sz": sz,
        "algo_ids": algo_ids, "open_time": time.time(),
        "notional": notional_max
    }

# ==================== 持仓监控 ====================
def monitor_single_position(inst_id, pos_info):
    """监控单个仓位，返回状态"""
    entry = pos_info["entry_price"]
    direction = pos_info["direction"]
    open_time = pos_info["open_time"]

    pos = get_position(inst_id)
    if not pos:
        return "CLOSED"

    d = curl_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", 5)
    current_price = float(d["data"][0]["last"]) if d.get("data") else pos["avgPx"]

    upl = pos["upl"]
    elapsed = time.time() - open_time

    if direction == "LONG":
        pct_change = (current_price - entry) / entry * 100
    else:
        pct_change = (entry - current_price) / entry * 100

    # 利润 > 0.2%名义
    if upl > pos_info["notional"] * 0.002:
        log(f"💰 {inst_id} {direction} 浮盈${upl:.4f} ({pct_change:+.3f}%) → 平仓落袋")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "PROFIT"

    # 亏损 > 0.7%
    if pct_change < -SL_PCT * 100:
        log(f"❌ {inst_id} {direction} 亏损{pct_change:+.3f}% → 砍仓")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "LOSS"

    # 时间止损5分钟
    if elapsed > TIME_STOP_SEC and abs(pct_change) < 0.15:
        log(f"⏰ {inst_id} {direction} 横盘{int(elapsed)}秒 ({pct_change:+.3f}%) → 时间止损")
        close_position(inst_id, pos_info.get("algo_ids"))
        return "TIME_STOP"

    return "HOLDING"

# ==================== 主循环 ====================
def main():
    log("=" * 50)
    log("🚀 OKX v4.2 多仓并行版启动")
    log(f"  杠杆: {LEVERAGE}x  TP: {TP_PCT*100}%  SL: {SL_PCT*100}%  ADX>{ADX_THRESHOLD}")
    log(f"  最大同时持仓: {MAX_CONCURRENT}  扫描间隔: {SCAN_INTERVAL}s  pos_range: <40/>60")
    log("=" * 50)

    load_instruments_cache()
    state = load_state()
    scan_count = 0

    # 启动时加载已有持仓（防止重启后丢失仓位跟踪）
    existing = get_all_positions()
    for p in existing:
        inst_id = p["instId"]
        if inst_id not in positions:
            # 查algo挂单
            algos = okx_get(f"/api/v5/trade/orders-algo-pending?instType=SWAP&instId={inst_id}")
            algo_ids = []
            if algos.get("data"):
                for a in algos["data"]:
                    if a.get("algoId"):
                        algo_ids.append(str(a["algoId"]))
            direction = "LONG" if p["pos"] > 0 else "SHORT"
            positions[inst_id] = {
                "instId": inst_id, "direction": direction,
                "entry_price": p["avgPx"], "sz": int(abs(p["pos"])),
                "algo_ids": algo_ids, "open_time": time.time(),
                "notional": p.get("notionalUsd", 0) or (abs(p["pos"]) * p["avgPx"])
            }
            log(f"📌 加载已有仓位: {inst_id} {direction} {p['pos']}张 @ ${p['avgPx']} algoIds={len(algo_ids)}")
    if positions:
        log(f"📌 共加载 {len(positions)} 个已有仓位")

    while True:
        try:
            # 时间窗口检查
            if not in_trading_window():
                if positions:
                    for inst_id in list(positions.keys()):
                        log(f"⏰ 不在窗口，平仓 {inst_id}")
                        close_position(inst_id, positions[inst_id].get("algo_ids"))
                        with positions_lock:
                            del positions[inst_id]
                    save_state(state)

                now = datetime.now(timezone(timedelta(hours=8)))
                next_window = None
                for sh, sm, eh, em in TRADING_WINDOWS:
                    if now.hour < sh or (now.hour == sh and now.minute < sm):
                        next_window = f"{sh:02d}:{sm:02d}"
                        break
                if not next_window:
                    next_window = "明天09:00"
                log(f"💤 不在窗口，下次: {next_window}")
                time.sleep(120)
                continue

            # 暂停检查
            if state.get("pause_until"):
                pause_until = datetime.fromisoformat(state["pause_until"])
                if datetime.now() < pause_until:
                    remaining = int((pause_until - datetime.now()).total_seconds())
                    log(f"⏸️ 暂停中，剩余{remaining}秒")
                    time.sleep(30)
                    continue
                else:
                    state["pause_until"] = None
                    state["consecutive_losses"] = 0
                    save_state(state)

            # 并行监控所有持仓
            if positions:
                with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
                    futures = {}
                    for inst_id, pos_info in list(positions.items()):
                        futures[ex.submit(monitor_single_position, inst_id, pos_info)] = inst_id

                    for future in as_completed(futures):
                        inst_id = futures[future]
                        try:
                            result = future.result()
                        except Exception as e:
                            log(f"⚠️ 监控 {inst_id} 异常: {e}")
                            continue

                        if result in ("PROFIT", "LOSS", "TIME_STOP", "CLOSED"):
                            with positions_lock:
                                if inst_id in positions:
                                    del positions[inst_id]

                            if result == "PROFIT":
                                state["consecutive_losses"] = 0
                                state["total_pnl"] = state.get("total_pnl", 0) + 1
                            elif result == "LOSS":
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
                        for close_attempt in range(3):
                            time.sleep(1)
                            close_position(inst_id, positions[inst_id].get("algo_ids"))
                            if not get_position(inst_id):
                                log(f"✅ {inst_id} 裸仓已平仓")
                                break
                        with positions_lock:
                            if inst_id in positions:
                                del positions[inst_id]

            # 扫描新信号（只要仓位数 < MAX_CONCURRENT）
            active_count = len(positions)
            if active_count < MAX_CONCURRENT:
                scan_count += 1
                if scan_count % 1 == 0:
                    balance = get_balance()
                    log(f"📡 扫描#{scan_count} 余额:${balance:.2f} 仓位:{active_count}/{MAX_CONCURRENT}")

                top = scan_market()
                if not top:
                    time.sleep(SCAN_INTERVAL)
                    continue

                candidates_to_check = [x["instId"] for x in top]

                candidates = []
                rejected_syms = []
                rej_lock = threading.Lock()
                def check_one(sym):
                    # 跳过已持仓品种
                    if sym in positions:
                        return None
                    # 检查冷却
                    last = state.get("last_trade", {}).get(sym, 0)
                    if time.time() - last < COOLDOWN_SEC:
                        return None
                    
                    sig = check_signals(sym)

                    # EMA趋势确认 + ADX强度 + 波动率（移除fr和pos_range过滤）
                    if sig["trend_1m"] == "UP" and sig["trend_5m"] == "UP" and sig.get("adx", 0) >= ADX_THRESHOLD and sig["vol_5m"] > MIN_VOLATILITY:
                        return {"sym": sym, "dir": "LONG", "score": sig["vol_5m"]}
                    elif sig["trend_1m"] == "DOWN" and sig["trend_5m"] == "DOWN" and sig.get("adx", 0) >= ADX_THRESHOLD and sig["vol_5m"] > MIN_VOLATILITY:
                        return {"sym": sym, "dir": "SHORT", "score": sig["vol_5m"]}
                    else:
                        reject_reason = f"{sym}: {sig['trend_1m']}/{sig['trend_5m']} pr={sig['pos_range']:.0f} vol={sig['vol_5m']:.3f} adx={sig.get('adx',0):.1f}"
                        with rej_lock:
                            if len(rejected_syms) < 10:
                                rejected_syms.append(reject_reason)
                    return None

                with ThreadPoolExecutor(max_workers=8) as ex:
                    for r in ex.map(check_one, candidates_to_check):
                        if r:
                            candidates.append(r)

                # 每20轮打印诊断：被拒绝的品种及原因
                if rejected_syms:
                    log(f"  ❌ 被拒: {len(candidates_to_check)}查 {len(candidates)}过 {'; '.join(rejected_syms[:5])}")
                elif not candidates_to_check:
                    log(f"  ❌ 无品种有资金费率数据 (top={len(top)})")
                elif len(candidates) == 0:
                    log(f"  ❌ 全部{len(candidates_to_check)}个品种被拒，无合格信号")

                # 开仓（按score排序，串行开仓+2秒错开，避免OKX限流）
                candidates.sort(key=lambda x: x["score"], reverse=True)
                slots = MAX_CONCURRENT - active_count

                if candidates[:slots]:
                    balance = get_balance()
                    n_open = min(len(candidates), slots, 1)  # 只开1个最高分
                    per_slot = balance / max(n_open + active_count, 1)
                    if per_slot >= 1:
                        log(f"🚀 串行开{n_open}个仓位...")
                        for cand in candidates[:n_open]:
                            log(f"🎯 {cand['sym']} {cand['dir']} score={cand['score']:.2f}")
                            pos = open_position_fast(cand["sym"], cand["dir"], per_slot)
                            if pos:
                                with positions_lock:
                                    positions[cand["sym"]] = pos
                                save_state(state)
                            time.sleep(5)  # 错开5秒，避免限流

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 手动停止")
            for inst_id, pos_info in list(positions.items()):
                close_position(inst_id, pos_info.get("algo_ids"))
            break
        except Exception as e:
            log(f"⚠️ 异常: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
