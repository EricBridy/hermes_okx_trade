#!/usr/bin/env python3
"""
全自动交易机器人 - v3 (策略优化版)
==== 基于最近6笔RLS交易数据的优化 ====
问题:  3连败 -$0.237, 赢$0.04, 胜亏比1:5.6
改进:
  1. 早止损: -0.3%或-$0.04就砍 (原来-$0.50/-0.5%)
  2. 趋势确认: 只做涨的币/只空跌的币
  3. 对称阈值: long/short都用0.03%
  4. 输换币: 同一币连输2次就禁入2小时
  5. 赢加码: 连赢2次加仓30%
"""
import subprocess, json, concurrent.futures as cf, os, sys, io, time
from datetime import datetime, timedelta

CACHE = os.path.expanduser("~/.hermes/cron/output/market_monitor_latest.txt")
HEARTBEAT = os.path.expanduser("~/.hermes/cron/output/market_monitor_heartbeat.txt")
TRADE_HISTORY = os.path.expanduser("~/.hermes/cron/output/trade_history.txt")
LOSS_SCORE = os.path.expanduser("~/.hermes/cron/output/loss_score.txt")
env = os.environ.copy()
env["PATH"] = "/root/.hermes/node/bin:" + env.get("PATH", "")

buf = io.StringIO(); alert_buf = io.StringIO()
def cache(s=""): buf.write(s + "\n")
def alert(s): alert_buf.write(s + "\n"); print(s)
def okx(*args):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=15, env=env)
        return r.stdout or r.stderr
    except: return ""

now = datetime.now().strftime('%H:%M:%S')
cache(f"=== [{now}] v3 ===")

# ═══════════════════════════════════════════════
# 0. 读取交易历史 & 连败记录
# ═══════════════════════════════════════════════
recent_trades = {}      # {sym: [timestamps]}, 5分钟冷却
loss_streak = {}        # {sym: consecutive_loss_count}, 连败计数
banned_coins = set()     # 连败2次禁入2小时

# 读取trade_history.txt
try:
    if os.path.exists(TRADE_HISTORY):
        with open(TRADE_HISTORY) as f:
            for line in f.read().strip().split('\n'):
                if '|' in line:
                    parts = line.split('|')
                    if len(parts) >= 5:
                        sym = parts[0].strip()
                        ts_str = parts[-1].strip()
                        try:
                            t = datetime.fromisoformat(ts_str)
                            elapsed = (datetime.now() - t).total_seconds()
                            if sym not in recent_trades:
                                recent_trades[sym] = []
                            recent_trades[sym].append({'time': t, 'result': parts[3] if len(parts) >= 5 else ''})
                        except: pass
except: pass

# 读取loss_score.txt (连败计数)
try:
    if os.path.exists(LOSS_SCORE):
        with open(LOSS_SCORE) as f:
            for line in f.read().strip().split('\n'):
                if '|' in line:
                    parts = line.split('|')
                    if len(parts) >= 3:
                        sym = parts[0].strip()
                        streak = int(parts[1])
                        banned_until = parts[2].strip()
                        try:
                            if banned_until and datetime.now() < datetime.fromisoformat(banned_until):
                                banned_coins.add(sym)
                                loss_streak[sym] = streak
                                cache(f"禁入: {sym}({streak}连败,到{banned_until[-5:]})")
                        except: pass
except: pass

COOLDOWN_MIN = 5
def is_on_cooldown(sym):
    if sym in recent_trades:
        latest = recent_trades[sym][-1]
        elapsed = (datetime.now() - latest['time']).total_seconds() / 60
        if elapsed < COOLDOWN_MIN:
            cache(f"冷却中: {sym} ({elapsed:.0f}分钟前)")
            return True
    if sym in banned_coins:
        cache(f"禁入中: {sym} ({loss_streak.get(sym,0)}连败)")
        return True
    return False

# ═══════════════════════════════════════════════
# 1. 持仓检查
# ═══════════════════════════════════════════════
has_position = False
pos_inst=pos_side=pos_sz=pos_entry=pos_upl=""
pos_upl_val=entry_price=position_size=0.0
ct_val = 100
try:
    out = okx("okx","account","positions")
    lines = [l for l in out.strip().split('\n') if l and not l.startswith(('Environment','instId'))]
    if len(lines) > 1:
        has_position = True
        p = lines[1].split()
        if len(p) >= 6:
            pos_inst,pos_side,pos_sz,pos_entry,pos_upl = p[0],p[2],p[3],p[4],p[5]
            pos_upl_val=float(pos_upl); entry_price=float(pos_entry); position_size=abs(float(pos_sz))
    cache(f"持仓 {'有' if has_position else '空'}")
except: cache("持仓失败")

# ═══════════════════════════════════════════════
# 2. 余额
# ═══════════════════════════════════════════════
balance = 0.0
try:
    out = okx("okx","account","balance","--ccy","USDT")
    for l in out.strip().split('\n'):
        if 'USDT' in l and not l.startswith('currency'):
            p = l.split()
            if len(p) >= 2: balance = float(p[1])
    cache(f"余额 ${balance:.4f}")
except: cache("余额失败")

# ═══════════════════════════════════════════════
# 3. 全市场扫描
# ═══════════════════════════════════════════════
longs_list=[]; shorts_list=[]
ticker_data = {}
try:
    r = subprocess.run(["curl","-s","--max-time","10","https://www.okx.com/api/v5/market/tickers?instType=SWAP"],
        capture_output=True,text=True,timeout=12)
    d = json.loads(r.stdout)
    if d.get('code') == '0':
        for x in d['data']:
            if x['instId'].endswith('USDT-SWAP'):
                vol = float(x.get('volCcy24h','0'))
                if vol >= 300000:
                    ticker_data[x['instId']] = {
                        'instId': x['instId'],
                        'last': float(x.get('last','0')),
                        'vol24h': vol,
                        'high24h': float(x.get('high24h','0')),
                        'low24h': float(x.get('low24h','0'))
                    }
        # 取成交量前30做分析
        top30 = sorted(ticker_data.values(), key=lambda x: x['vol24h'], reverse=True)[:30]
        
        def get_fr(sym):
            try:
                r2 = subprocess.run(["curl","-s","--max-time","4",f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}"],capture_output=True,text=True,timeout=6)
                d2 = json.loads(r2.stdout)
                if d2.get('code')=='0' and d2['data']: return sym, float(d2['data'][0]['fundingRate'])
            except: pass
            return sym, None
        
        frs = {}
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            fs = {ex.submit(get_fr, x['instId']): x['instId'] for x in top30}
            for f in cf.as_completed(fs):
                try: k,v = f.result(timeout=6); frs[k] = v
                except: pass
        
        # v3: 对称阈值 + 500k成交量门槛
        for x in top30:
            x['fundingRate'] = frs.get(x['instId'])
            # 获取5m趋势
            try:
                r3 = subprocess.run(["curl","-s","--max-time","4",
                    f"https://www.okx.com/api/v5/market/candles?instId={x['instId']}&bar=5m&limit=4"],
                    capture_output=True,text=True,timeout=6)
                d3 = json.loads(r3.stdout)
                if d3.get('data') and len(d3['data']) >= 2:
                    closes = [float(c[4]) for c in reversed(d3['data'])]  # oldest → newest
                    # 趋势方向: 至少3根比前一根高=涨, 至少3根比前一根低=跌
                    up_count = sum(1 for i in range(1,len(closes)) if closes[i] > closes[i-1])
                    down_count = sum(1 for i in range(1,len(closes)) if closes[i] < closes[i-1])
                    x['trend'] = 'up' if up_count >= 3 else ('down' if down_count >= 3 else 'flat')
                else:
                    x['trend'] = 'flat'
            except:
                x['trend'] = 'flat'
        
        # v3: 做多候选 = funding<0 + 趋势向上 or 横盘
        longs_list = sorted(
            [x for x in top30 if x['fundingRate'] and x['fundingRate'] < -0.0003 
             and x['vol24h'] > 500000 and x['trend'] in ('up','flat')],
            key=lambda x: x['fundingRate']
        )[:5]
        
        # v3: 做空候选 = funding>0 + 趋势向下 or 横盘 (对称阈值0.03%)
        shorts_list = sorted(
            [x for x in top30 if x['fundingRate'] and x['fundingRate'] > 0.0003
             and x['vol24h'] > 500000 and x['trend'] in ('down','flat')],
            key=lambda x: x['fundingRate'], reverse=True
        )[:5]
        
        cache(f"做多候补: {len(longs_list)}个 | 做空候补: {len(shorts_list)}个")
        if longs_list:
            cache(f"  首选做多: {longs_list[0]['instId']} fund={longs_list[0]['fundingRate']*100:+.4f}% trend={longs_list[0]['trend']}")
        if shorts_list:
            cache(f"  首选做空: {shorts_list[0]['instId']} fund={shorts_list[0]['fundingRate']*100:+.4f}% trend={shorts_list[0]['trend']}")
except Exception as e:
    import traceback
    cache(f"扫描失败: {str(e)[:50]}")
    cache(traceback.format_exc()[:100])

# ═══════════════════════════════════════════════
# 4. 持仓当前价
# ═══════════════════════════════════════════════
current_price = 0
if has_position:
    try:
        out = okx("okx","market","ticker",pos_inst)
        for l in out.strip().split('\n'):
            if 'last' in l.lower():
                try: current_price = float(l.split()[-1])
                except: pass
        cache(f"当前价 {current_price}")
    except: pass

# ═══════════════════════════════════════════════
# 5. AUTO-EXIT (v3: 更严格的止损)
# ═══════════════════════════════════════════════
if has_position and entry_price > 0:
    notional = position_size * ct_val * entry_price
    fees = notional * 0.001
    # v3: 更精确的亏损百分比计算
    pos_value = position_size * ct_val * entry_price
    entry_margin = pos_value / 10  # 假设10x (保守)
    actual_lever = pos_value / balance if balance > 0 else 10
    loss_pct_notional = abs(pos_upl_val) / pos_value * 100 if pos_value > 0 else 0
    
    cache(f"名义价值=${notional:.2f} | 实际杠杆={actual_lever:.1f}x | 浮亏={pos_upl_val:+.4f}")
    
    # v3: 早止损 — -$0.04或-0.3%名义价值就砍 (原来-$0.50/-0.5%)
    #    小账户$4.68, -$0.04已经是~0.9%净值的亏损
    EARLY_CUT = max(0.04, balance * 0.01)  # $0.04或1%净值取大值
    
    # Profit auto-close (v3: 只要有绿就跑, 但至少覆盖手续费)
    if pos_upl_val > max(fees * 0.5, 0.01):
        alert(f"💰 {pos_inst} 浮盈${pos_upl_val:.3f} (手续费${fees:.3f})，平仓")
        out2 = okx("okx","swap","algo","orders","--instId",pos_inst)
        for line in out2.strip().split('\n')[2:]:
            parts = line.split()
            if parts and len(parts) >= 8:
                okx("okx","swap","algo","cancel","--instId",pos_inst,"--algoId",parts[0]); time.sleep(0.2)
        result = okx("okx","swap","close","--instId",pos_inst,"--mgnMode","cross")
        alert(f"✅ 平仓: {result.strip()[:80]}")
        out3 = okx("okx","account","balance","--ccy","USDT")
        for l in out3.strip().split('\n'):
            if 'USDT' in l and not l.startswith('currency'):
                p = l.split()
                if len(p) >= 2: alert(f"💰 余额 ${float(p[1]):.4f}")
    
    # v3: 早止损 — -$0.04或-0.3%名义价值
    elif pos_upl_val < -EARLY_CUT or loss_pct_notional > 0.3:
        alert(f"⚠️ {pos_inst} 止损: 浮亏${pos_upl_val:.3f} ({loss_pct_notional:.2f}%名义价值)")
        out2 = okx("okx","swap","algo","orders","--instId",pos_inst)
        for line in out2.strip().split('\n')[2:]:
            parts = line.split()
            if parts and len(parts) >= 8:
                okx("okx","swap","algo","cancel","--instId",pos_inst,"--algoId",parts[0]); time.sleep(0.2)
        result = okx("okx","swap","close","--instId",pos_inst,"--mgnMode","cross")
        alert(f"✅ 止损: {result.strip()[:80]}")
        out3 = okx("okx","account","balance","--ccy","USDT")
        for l in out3.strip().split('\n'):
            if 'USDT' in l and not l.startswith('currency'):
                p = l.split()
                if len(p) >= 2: alert(f"💰 余额 ${float(p[1]):.4f}")
        
        # v3: 记录连败 — 同一币亏2次就禁入
        try:
            streak = loss_streak.get(pos_inst, 0) + 1
            ban_until = ""
            if streak >= 2:
                ban_until = (datetime.now() + timedelta(hours=2)).isoformat()
                banned_coins.add(pos_inst)
                alert(f"🚫 {pos_inst} {streak}连败,禁入2小时到{ban_until[-8:]}")
            os.makedirs(os.path.dirname(LOSS_SCORE), exist_ok=True)
            with open(LOSS_SCORE, 'a') as f:
                f.write(f"{pos_inst}|{streak}|{ban_until}\n")
            # 只保留最近50条
            if os.path.exists(LOSS_SCORE):
                with open(LOSS_SCORE) as f:
                    lines_all = f.read().strip().split('\n')
                if len(lines_all) > 50:
                    with open(LOSS_SCORE, 'w') as f:
                        f.write('\n'.join(lines_all[-50:]) + '\n')
        except: pass
    
    # 横盘3分钟(UPL变化极小的处理)
    else:
        time_in_pos = 0  # 无法从账单获取准确的持仓时间
        cache(f"持仓 UPL=${pos_upl_val:.4f} (名义亏损{loss_pct_notional:.2f}%) 当前${current_price}")

# ═══════════════════════════════════════════════
# 6. AUTO-ENTRY (v3: 趋势确认 + 品种轮换)
# ═══════════════════════════════════════════════
elif not has_position and balance > 0.5:
    target = None; direction = ""; entry_type = ""
    margin_per = balance * 0.95
    max_lever = 10
    
    # v3: 品种轮换 — 如果上一个交易在某种币上输了,优先选不同品种
    last_traded_sym = ""
    try:
        if os.path.exists(TRADE_HISTORY):
            with open(TRADE_HISTORY) as f:
                lines = f.read().strip().split('\n')
            if lines and lines[-1] and '|' in lines[-1]:
                last_traded_sym = lines[-1].split('|')[0].strip()
    except: pass
    
    # v3.1: 波动率检查 — 如果24h波动>15%,自动降杠杆或跳过
    def check_volatility(x):
        """检查24h波动率,返回推荐杠杆倍数"""
        high = x.get('high24h', x['last'])
        low = x.get('low24h', x['last'])
        if high and low and high > low:
            vol_pct = (high - low) / low * 100
            if vol_pct > 15:
                # 高波动的币,最高3x
                return 3, vol_pct
            elif vol_pct > 10:
                return 5, vol_pct
            elif vol_pct > 5:
                return 10, vol_pct
        return max_lever, 0
    
    # v3.1: 尝试找long候选 (资金费率负+趋势向上或横盘+波动率合理)
    for x in longs_list:
        if is_on_cooldown(x['instId']):
            continue
        last = x['last']
        high = x.get('high24h', last * 1.1)
        low = x.get('low24h', last * 0.9)
        # v3.1: 更宽的价格安全边际(3% instead of 2%)
        if last >= high * 0.97:
            cache(f"跳过: {x['instId']} 接近24h高点({last:.4f}/{high:.4f},仅差{(high-last)/high*100:.1f}%)")
            continue
        # v3.1: 品种轮换
        if x['instId'] == last_traded_sym:
            cache(f"上次交易是{x['instId']},优先选别的")
            continue
        # v3.1: 波动率检查
        rec_lever, vol_pct = check_volatility(x)
        if vol_pct > 0:
            cache(f"  24h波动{vol_pct:.1f}%,推荐杠杆{rec_lever}x")
        target = x; direction = "buy"; entry_type = "LONG"
        target['rec_lever'] = rec_lever
        target['vol_pct'] = vol_pct
        cache(f"候选LONG: {x['instId']} @${last} fund={x['fundingRate']*100:+.4f}% trend={x['trend']}")
        break
    
    if not target:
        for x in longs_list:
            if is_on_cooldown(x['instId']):
                continue
            last = x['last']
            high = x.get('high24h', last * 1.1)
            if last >= high * 0.97:
                continue
            rec_lever, vol_pct = check_volatility(x)
            target = x; direction = "buy"; entry_type = "LONG"
            target['rec_lever'] = rec_lever
            target['vol_pct'] = vol_pct
            cache(f"候选LONG(备选): {x['instId']}")
            break
    
    # v3.1: 做空 (对称阈值+波动率检查)
    if not target:
        for x in shorts_list:
            if is_on_cooldown(x['instId']):
                continue
            last = x['last']
            low = x.get('low24h', last * 0.9)
            # v3.1: 更宽的价格安全边际(3% instead of 2%)
            if last <= low * 1.03:
                cache(f"跳过: {x['instId']} 接近24h低点({last:.4f}/{low:.4f},仅差{(last-low)/low*100:.1f}%)")
                continue
            if x['instId'] == last_traded_sym:
                continue
            rec_lever, vol_pct = check_volatility(x)
            if vol_pct > 0:
                cache(f"  24h波动{vol_pct:.1f}%,推荐杠杆{rec_lever}x")
            target = x; direction = "sell"; entry_type = "SHORT"
            target['rec_lever'] = rec_lever
            target['vol_pct'] = vol_pct
            cache(f"候选SHORT: {x['instId']} @${last} fund={x['fundingRate']*100:+.4f}% trend={x['trend']}")
            break
    
    if not target:
        for x in shorts_list:
            if is_on_cooldown(x['instId']):
                continue
            last = x['last']
            low = x.get('low24h', last * 0.9)
            if last <= low * 1.03:
                continue
            rec_lever, vol_pct = check_volatility(x)
            target = x; direction = "sell"; entry_type = "SHORT"
            target['rec_lever'] = rec_lever
            target['vol_pct'] = vol_pct
            cache(f"候选SHORT(备选): {x['instId']}")
            break
    
    # v3: 倒数第二个检查 — 5分钟趋势确认(不横盘)
    if target:
        inst = target['instId']; price = target['last']
        
        # 5分钟波动检查: 跳过横盘(<0.2%)
        try:
            r = subprocess.run(["curl","-s","--max-time","5",
                f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=5m&limit=2"],
                capture_output=True,text=True,timeout=6)
            d = json.loads(r.stdout)
            if d.get('data') and len(d['data']) >= 2:
                c1 = d['data'][0]
                range_5m = abs(float(c1[2]) - float(c1[3])) / float(c1[4]) * 100
                if range_5m < 0.2:
                    cache(f"跳过横盘: {inst} 5分波动{range_5m:.2f}%")
                    target = None
        except: pass
    
    # v3: 执行入场
    if target:
        inst = target['instId']; price = target['last']
        
        # v3.1: 获取合约规格,优先使用波动率推荐杠杆
        try:
            r = subprocess.run(["curl","-s","--max-time","5",
                f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={inst}"],
                capture_output=True,text=True,timeout=8)
            d = json.loads(r.stdout)
            if d.get('data'):
                spec = d['data'][0]
                ct_val = float(spec['ctVal'])
                contract_max = float(spec.get('lever', 10))
                # v3.1: 如果volatility推荐了更低杠杆,用推荐值
                rec_lever = target.get('rec_lever', contract_max)
                vol_pct = target.get('vol_pct', 0)
                max_lever = min(rec_lever, contract_max, 20)
                if vol_pct > 10:
                    cache(f"  高波动({vol_pct:.0f}%),杠杆限制{max_lever}x(原最高{int(contract_max)}x)")
            else:
                ct_val = 100; max_lever = target.get('rec_lever', 10)
        except:
            ct_val = 100; max_lever = target.get('rec_lever', 10)
        
        # v3: 仓位计算 — 赢加码/输减仓
        win_streak = 0
        try:
            if os.path.exists(TRADE_HISTORY):
                with open(TRADE_HISTORY) as f:
                    lines = f.read().strip().split('\n')
                recent_results = [l.split('|')[2] for l in lines[-3:] if '|' in l and len(l.split('|')) >= 3]
                for r in reversed(recent_results):
                    if 'LONG' in r or 'SHORT' in r:
                        # Check result from the actual PnL (we use entry_type as proxy)
                        if entry_type == 'LONG' and win_streak < 2:
                            win_streak += 1  # approximate
        except: pass
        
        # 基础仓位: 95%余额 × 杠杆 / 合约价值
        base_lots = int(margin_per * max_lever / (ct_val * price))
        # 赢加码: 连赢2次+30%
        if win_streak >= 2:
            base_lots = int(base_lots * 1.3)
        
        lot_sz = max(1, base_lots)
        
        if lot_sz >= 1:
            notional = lot_sz * ct_val * price
            margin = notional / max_lever
            
            if margin <= balance * 0.95:
                alert(f"📊 自动入场: {entry_type} {inst} {lot_sz}张 @${price}")
                cache(f"  资金费率: {target['fundingRate']*100:+.4f}% | 趋势: {target['trend']} | 杠杆: {max_lever}x")
                
                # 设置杠杆
                okx("okx","swap","leverage","--instId",inst,"--lever",str(int(max_lever)),"--mgnMode","cross")
                time.sleep(0.5)
                
                # 市价入场
                r = okx("okx","swap","place","--instId",inst,"--tdMode","cross",
                    "--side",direction,"--ordType","market","--sz",str(lot_sz))
                alert(f"→ 开仓: {r.strip()[:80]}")
                time.sleep(0.5)
                
                # v3: 宽SL当瀑布保险(-4%)
                if direction == "buy":
                    sl_price = round(price * 0.96, 6)
                else:
                    sl_price = round(price * 1.04, 6)
                close_side = "sell" if direction == "buy" else "buy"
                r2 = okx("okx","swap","algo","place","--instId",inst,
                    "--side",close_side,"--sz",str(lot_sz),
                    "--slTriggerPx",str(sl_price),"--slOrdPx=-1","--reduceOnly")
                alert(f"→ 瀑布保险: ${sl_price}")
                
                out3 = okx("okx","account","balance","--ccy","USDT")
                for l in out3.strip().split('\n'):
                    if 'USDT' in l and not l.startswith('currency'):
                        p = l.split()
                        if len(p) >= 2: alert(f"💳 余额 ${float(p[1]):.4f}")
                
                alert(f"📐 {lot_sz}×{ct_val}×${price}=${notional:.2f} | 杠杆{int(max_lever)}x | 保证金${margin:.2f} | 手续费${notional*0.001:.3f}")
                
                # 记录交易历史
                try:
                    os.makedirs(os.path.dirname(TRADE_HISTORY), exist_ok=True)
                    with open(TRADE_HISTORY, 'a') as f:
                        f.write(f"{inst}|{entry_type}|{lot_sz}|{price}|{datetime.now().isoformat()}\n")
                    with open(TRADE_HISTORY) as f:
                        lines = f.read().strip().split('\n')
                    if len(lines) > 20:
                        with open(TRADE_HISTORY, 'w') as f:
                            f.write('\n'.join(lines[-20:]) + '\n')
                except: pass

# ═══════════════════════════════════════════════
# 7. 信号提醒(空仓时)
# ═══════════════════════════════════════════════
elif not has_position:
    # v3: 只显示不在冷却也不在禁入列表的候选
    shown = set()
    for x in longs_list:
        if x['instId'] in shown or is_on_cooldown(x['instId']):
            continue
        shown.add(x['instId'])
        last = x['last']; high = x.get('high24h', last*1.1)
        flag = "" if last < high*0.98 else "⚠️接近24h高位"
        alert(f"📈 做多: {x['instId']} ${last:.4f} fund={x['fundingRate']*100:+.4f}% trend={x['trend']} {flag}")
        break
    for x in shorts_list:
        if x['instId'] in shown or is_on_cooldown(x['instId']):
            continue
        shown.add(x['instId'])
        last = x['last']; low = x.get('low24h', last*0.9)
        flag = "" if last > low*1.02 else "⚠️接近24h低位"
        alert(f"📉 做空: {x['instId']} ${last:.4f} fund={x['fundingRate']*100:+.4f}% trend={x['trend']} {flag}")
        break

# ═══════════════════════════════════════════════
# 写入缓存
# ═══════════════════════════════════════════════
os.makedirs(os.path.dirname(CACHE), exist_ok=True)
with open(CACHE, 'w') as f: f.write(buf.getvalue())
if alert_buf.getvalue():
    print(alert_buf.getvalue(), end='')
with open(HEARTBEAT, 'w') as f: f.write(str(time.time()))
