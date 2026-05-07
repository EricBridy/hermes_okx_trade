#!/usr/bin/env python3
"""v4.2 完整一轮实测benchmark - 真实API调用"""
import time, json, subprocess, os, sys, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

OKX_BASE = "https://www.okx.com"
LEVERAGE = 5
TP_PCT = 0.0105
SL_PCT = 0.007
CRASH_SL_PCT = 0.015
MIN_VOLATILITY = 0.005

TIMINGS = {}

def timed(label):
    """装饰器: 记录每步耗时"""
    class Timer:
        def __enter__(self):
            self.start = time.time()
            return self
        def __exit__(self, *a):
            elapsed = (time.time() - self.start) * 1000
            TIMINGS[label] = elapsed
            print(f"  {label}: {elapsed:.0f}ms")
    return Timer()

def curl_json(url, timeout=5):
    try:
        r = subprocess.run(["curl", "-sS", "-m", str(timeout), url],
                           capture_output=True, text=True, timeout=timeout+2)
        return json.loads(r.stdout) if r.stdout else {}
    except:
        return {}

def okx_post(path, body):
    try:
        r = subprocess.run([
            "curl", "-sS", "-m", "5", "-X", "POST",
            "-H", "Content-Type: application/json",
            "-H", f"OK-ACCESS-KEY: {os.environ.get('OKX_API_KEY', '')}",
            "-H", f"OK-ACCESS-SIGN: placeholder",
            "-H", f"OK-ACCESS-TIMESTAMP: placeholder",
            "-H", f"OK-ACCESS-PASSPHRASE: placeholder",
            f"{OKX_BASE}{path}", "-d", body
        ], capture_output=True, text=True, timeout=7)
        return json.loads(r.stdout) if r.stdout else {}
    except:
        return {}

def okx_signed_post(path, body):
    """带签名的POST请求"""
    import hmac, hashlib, base64
    key = os.environ.get("OKX_API_KEY", "")
    secret = os.environ.get("OKX_SECRET_KEY", "")
    passphrase = os.environ.get("OKX_PASSPHRASE", "")
    
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.utcnow().microsecond//1000:03d}Z"
    sign_str = ts + "POST" + path + body
    sign = base64.b64encode(
        hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()
    ).decode()
    
    headers = [
        "-H", "Content-Type: application/json",
        "-H", f"OK-ACCESS-KEY: {key}",
        "-H", f"OK-ACCESS-SIGN: {sign}",
        "-H", f"OK-ACCESS-TIMESTAMP: ts",
        "-H", f"OK-ACCESS-PASSPHRASE: passphrase",
    ]
    try:
        r = subprocess.run(
            ["curl", "-sS", "-m", "5", "-X", "POST"] + headers + [f"{OKX_BASE}{path}", "-d", body],
            capture_output=True, text=True, timeout=7
        )
        return json.loads(r.stdout) if r.stdout else {}
    except:
        return {}

def get_balance():
    """用环境变量里的key查余额"""
    # 从配置文件读
    try:
        import tomllib
        with open(os.path.expanduser("~/.okx/config.toml"), "rb") as f:
            cfg = tomllib.load(f)
        profile = cfg.get("profiles", {}).get("okx-prod", {})
        api_key = profile.get("api_key", "")
        secret = profile.get("secret_key", "")
        passphrase = profile.get("passphrase", "")
    except:
        return 0
    
    import hmac, hashlib, base64
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.utcnow().microsecond//1000:03d}Z"
    path = "/api/v5/account/balance?ccy=USDT"
    sign_str = ts + "GET" + path
    sign = base64.b64encode(
        hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()
    ).decode()
    
    try:
        r = subprocess.run([
            "curl", "-sS", "-m", "5",
            "-H", f"OK-ACCESS-KEY: {api_key}",
            "-H", f"OK-ACCESS-SIGN: {sign}",
            "-H", f"OK-ACCESS-TIMESTAMP: {ts}",
            "-H", f"OK-ACCESS-PASSPHRASE: {passphrase}",
            f"{OKX_BASE}{path}"
        ], capture_output=True, text=True, timeout=7)
        d = json.loads(r.stdout)
        if d.get("data"):
            return float(d["data"][0]["totalEq"])
    except:
        pass
    return 0

print("=" * 60)
print("🔬 v4.2 完整一轮实测 Benchmark")
print(f"   时间: {datetime.now().strftime('%H:%M:%S')}")
print("=" * 60)

# ==================== STEP 0: 查余额 ====================
print("\n📊 STEP 0: 查余额")
with timed("查余额"):
    balance = get_balance()
print(f"  余额: ${balance:.2f} USDT")

# ==================== STEP 1: 拉全量tickers ====================
print("\n📊 STEP 1: 拉全量tickers")
with timed("拉全量tickers"):
    tickers_data = curl_json(f"{OKX_BASE}/api/v5/market/tickers?instType=SWAP")
raw_count = len(tickers_data.get("data", []))
print(f"  原始数量: {raw_count}个")

# ==================== STEP 2: 本地过滤 ====================
print("\n📊 STEP 2: 本地过滤（成交量>50万，排除RLS/BILL）")
with timed("本地过滤"):
    data = tickers_data.get("data", [])
    skip = {"RLS-USDT-SWAP", "BILL-USDT-SWAP"}
    swaps = []
    for d in data:
        if not d["instId"].endswith("-USDT-SWAP"):
            continue
        if d["instId"] in skip:
            continue
        try:
            vol = float(d.get("volCcy24h", 0))
        except:
            continue
        if vol < 500000:
            continue
        swaps.append({"instId": d["instId"], "last": d["last"], "vol": vol})
    swaps.sort(key=lambda x: x["vol"], reverse=True)
    top = swaps[:40]
print(f"  过滤后: {len(top)}个候选品种")

# ==================== STEP 3: 资金费率 ====================
print(f"\n📊 STEP 3: 资金费率（8个品种并行拉取）")
def fetch_fr(sym):
    d = curl_json(f"{OKX_BASE}/api/v5/public/funding-rate?instId={sym}")
    if d.get("data"):
        fr = d["data"][0].get("fundingRate")
        return (sym, float(fr) if fr else None)
    return (sym, None)

with timed("资金费率(8并行)"):
    with ThreadPoolExecutor(max_workers=15) as ex:
        fr_results = dict(ex.map(lambda s: fetch_fr(s["instId"]), top[:8]))

fr_valid = {k: v for k, v in fr_results.items() if v is not None}
print(f"  获取到: {len(fr_valid)}/{len(top[:8])}个有效费率")

# ==================== STEP 4: K线信号检查 ====================
print(f"\n📊 STEP 4: K线信号检查（30个品种并行，每个拉1m+5m K线）")
def fetch_signal(sym):
    k1 = curl_json(f"{OKX_BASE}/api/v5/market/candles?instId={sym}&bar=1m&limit=10")
    k5 = curl_json(f"{OKX_BASE}/api/v5/market/candles?instId={sym}&bar=5m&limit=6")
    return (sym, len(k1.get("data", [])), len(k5.get("data", [])))

with timed("K线信号(30并行×2)"):
    with ThreadPoolExecutor(max_workers=30) as ex:
        kl_results = list(ex.map(lambda s: fetch_signal(s["instId"]), top[:30]))

print(f"  检查了: {len(kl_results)}个品种")

# ==================== STEP 5: 开仓模拟（用public/time代替真实下单） ====================
print(f"\n📊 STEP 5: 开仓延迟模拟（6步API = 设杠杆+下单+等成交+查持仓+挂3单）")

with timed("开1仓(串行6步)"):
    for _ in range(6):
        curl_json(f"{OKX_BASE}/api/v5/public/time")

with timed("开1仓(并行6步)"):
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda _: curl_json(f"{OKX_BASE}/api/v5/public/time"), range(6)))

# 开5仓模拟
with timed("开5仓(串行=30步)"):
    for _ in range(30):  # 5仓 × 6步
        curl_json(f"{OKX_BASE}/api/v5/public/time")

with timed("开5仓(并行=5×6步)"):
    with ThreadPoolExecutor(max_workers=5) as ex:
        def simulate_open(_):
            for _ in range(6):
                curl_json(f"{OKX_BASE}/api/v5/public/time")
        list(ex.map(simulate_open, range(5)))

# ==================== STEP 6: 持仓监控模拟 ====================
print(f"\n📊 STEP 6: 持仓监控延迟（查持仓+拉价格，并行5个）")
def monitor_sim(_):
    curl_json(f"{OKX_BASE}/api/v5/public/time")  # 代替get_position
    curl_json(f"{OKX_BASE}/api/v5/public/time")  # 代替拉价格

with timed("监控5仓(并行)"):
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(monitor_sim, range(5)))

# ==================== 汇总 ====================
scan_ms = TIMINGS.get("拉全量tickers", 0) + TIMINGS.get("本地过滤", 0) + TIMINGS.get("资金费率(8并行)", 0) + TIMINGS.get("K线信号(30并行×2)", 0)
open1_serial = TIMINGS.get("开1仓(串行6步)", 0)
open1_parallel = TIMINGS.get("开1仓(并行6步)", 0)
open5_serial = TIMINGS.get("开5仓(串行=30步)", 0)
open5_parallel = TIMINGS.get("开5仓(并行=5×6步)", 0)
monitor_ms = TIMINGS.get("监控5仓(并行)", 0)

print("\n" + "=" * 60)
print("📊 完整一轮耗时汇总")
print("=" * 60)
print(f"  扫描总耗时:              {scan_ms:.0f}ms ({scan_ms/1000:.1f}s)")
print(f"  监控5仓:                 {monitor_ms:.0f}ms")
print()
print(f"  ┌─ 场景                    │  旧(串行)  │  新(并行)  │  提速")
print(f"  ├─────────────────────────┼──────────┼──────────┼──────")
print(f"  │ 开1仓                    │ {open1_serial:6.0f}ms │ {open1_parallel:6.0f}ms │ {open1_serial/max(open1_parallel,1):.1f}x")
print(f"  │ 开5仓                    │ {open5_serial:6.0f}ms │ {open5_parallel:6.0f}ms │ {open5_serial/max(open5_parallel,1):.1f}x")
print(f"  │ 监控+扫描+开5仓(一轮)    │ {scan_ms+monitor_ms+open5_serial:6.0f}ms │ {scan_ms+monitor_ms+open5_parallel:6.0f}ms │ {(scan_ms+monitor_ms+open5_serial)/max(scan_ms+monitor_ms+open5_parallel,1):.1f}x")
print()
total_new = scan_ms + monitor_ms + open5_parallel
print(f"  📈 完整一轮(扫描+开5仓):   {total_new:.0f}ms = {total_new/1000:.1f}s")
print(f"  📈 完整一轮(+15s等待):     ~{total_new/1000+15:.1f}s")
print(f"  📈 每小时可完成:           ~{3600/(total_new/1000+15):.0f}轮扫描")
print("=" * 60)
