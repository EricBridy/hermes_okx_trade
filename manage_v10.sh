#!/usr/bin/env bash
# ============================================================
#  Hermes v10 日常管理脚本
#  用法：./manage_v10.sh <命令>
#  命令: start | stop | restart | status | logs | tail | stats | brain | trades | help
# ============================================================

# ---------- 颜色 ----------
RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BLUE=$'\e[34m'; BOLD=$'\e[1m'; RESET=$'\e[0m'

# ---------- 路径 ----------
PYTHON_BIN="/root/.local/bin/python3.11"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAIN_PY="$SCRIPT_DIR/hermes_v10_main.py"
STDOUT_LOG="$SCRIPT_DIR/hermes_v10_stdout.log"
TRADES_LOG="$SCRIPT_DIR/hermes_v10_trades.log"
TRADES_JSONL="$SCRIPT_DIR/hermes_v10_trades.jsonl"
BRAIN_FILE="$SCRIPT_DIR/hermes_v10_brain.json"
STATE_FILE="$SCRIPT_DIR/hermes_v10_state.json"
PID_FILE="$SCRIPT_DIR/hermes_v10.pid"

ok()    { echo "${GREEN}✓${RESET} $*"; }
info()  { echo "${BLUE}→${RESET} $*"; }
warn()  { echo "${YELLOW}⚠${RESET} $*"; }
fail()  { echo "${RED}✗${RESET} $*"; }
title() { echo; echo "${BOLD}=== $* ===${RESET}"; }

# ---------- 找进程 ----------
find_pid() {
    pgrep -f "hermes_v10_main.py" 2>/dev/null | head -1
}

# ---------- 命令实现 ----------

cmd_start() {
    title "启动 Hermes v10"
    PID=$(find_pid)
    if [ -n "$PID" ]; then
        warn "已经在运行 (PID: $PID)，请先 stop 或用 restart"
        return 1
    fi
    if [ ! -f "$MAIN_PY" ]; then
        fail "找不到 $MAIN_PY"
        return 1
    fi
    info "启动命令: $PYTHON_BIN $MAIN_PY"
    cd "$SCRIPT_DIR"
    nohup "$PYTHON_BIN" "$MAIN_PY" >> "$STDOUT_LOG" 2>&1 &
    NEW_PID=$!
    echo $NEW_PID > "$PID_FILE"
    sleep 3
    if kill -0 "$NEW_PID" 2>/dev/null; then
        ok "已后台启动，PID=$NEW_PID"
        info "stdout 日志: $STDOUT_LOG"
        info "交易日志:   $TRADES_LOG"
        info "查看实时输出: ./manage_v10.sh logs"
    else
        fail "启动失败，看下 $STDOUT_LOG 末尾："
        tail -20 "$STDOUT_LOG" 2>/dev/null
        return 1
    fi
}

cmd_stop() {
    title "停止 Hermes v10"
    PID=$(find_pid)
    if [ -z "$PID" ]; then
        info "没有进程在运行"
        return 0
    fi
    info "发送 SIGINT 给 PID $PID（程序会保存 brain 后退出）..."
    kill -INT "$PID" 2>/dev/null
    # 等最多 15 秒
    for i in $(seq 1 15); do
        sleep 1
        if ! kill -0 "$PID" 2>/dev/null; then
            ok "已优雅退出（耗时 ${i}s）"
            rm -f "$PID_FILE"
            return 0
        fi
        echo -n "."
    done
    echo
    warn "15 秒未退出，发送 SIGTERM..."
    kill -TERM "$PID" 2>/dev/null
    sleep 3
    if kill -0 "$PID" 2>/dev/null; then
        warn "仍未退出，强制 kill -9"
        kill -9 "$PID" 2>/dev/null
        sleep 1
    fi
    if kill -0 "$PID" 2>/dev/null; then
        fail "无法停止进程 $PID"
        return 1
    fi
    ok "已停止"
    rm -f "$PID_FILE"
}

cmd_restart() {
    cmd_stop
    sleep 2
    cmd_start
}

cmd_status() {
    title "Hermes v10 运行状态"
    PID=$(find_pid)
    if [ -z "$PID" ]; then
        warn "未运行"
    else
        ok "运行中 (PID: $PID)"
        # 进程信息
        if [ -r "/proc/$PID/status" ]; then
            START=$(stat -c %Y "/proc/$PID" 2>/dev/null)
            if [ -n "$START" ]; then
                NOW=$(date +%s)
                UPTIME=$((NOW - START))
                printf "  运行时长: %dh %dm %ds\n" \
                    $((UPTIME / 3600)) $(((UPTIME / 60) % 60)) $((UPTIME % 60))
            fi
            RSS=$(grep VmRSS "/proc/$PID/status" 2>/dev/null | awk '{print $2}')
            if [ -n "$RSS" ]; then
                printf "  内存占用: %.1f MB\n" "$(echo "scale=1; $RSS/1024" | bc)"
            fi
        fi
    fi

    # 当前持仓
    if [ -f "$STATE_FILE" ]; then
        echo
        info "当前状态文件:"
        "$PYTHON_BIN" -c "
import json
try:
    s = json.load(open('$STATE_FILE'))
    print(f\"  交易总数:   {s.get('trade_count', 0)}\")
    print(f\"  累计盈亏:   \${s.get('total_pnl', 0):+.4f}\")
    print(f\"  连续亏损:   {s.get('consecutive_losses', 0)}\")
    pu = s.get('pause_until')
    if pu:
        try:
            from datetime import datetime
            until = datetime.fromisoformat(pu)
            if datetime.now() < until:
                rem = (until - datetime.now()).total_seconds()
                print(f\"  暂停状态:   到 {pu}（剩余 {int(rem//60)} 分钟）\")
            else:
                print(f\"  暂停状态:   已过期（{pu}），下次开仓会自动清理\")
        except Exception:
            print(f\"  暂停状态:   {pu}\")
    else:
        print(f\"  暂停状态:   正常\")
    pm = s.get('position_meta', {})
    print(f\"  当前持仓:   {len(pm)} 个\")
    for sym, m in pm.items():
        print(f\"    - {sym} {m.get('signal_type','?')} {m.get('direction','?')} \"
              f\"p_win={m.get('p_win',0):.2%}\")
except Exception as e:
    print(f'  读取失败: {e}')
"
    fi

    # 大脑简况
    if [ -f "$BRAIN_FILE" ]; then
        echo
        info "大脑状态:"
        "$PYTHON_BIN" -c "
import json
try:
    d = json.load(open('$BRAIN_FILE'))
    m = d.get('model', {})
    k = d.get('kelly', {})
    print(f\"  模型更新次数: {m.get('updates', 0)}\")
    rb = m.get('recent_brier') or [0.25]
    avg_brier = sum(rb)/len(rb)
    print(f\"  Brier 均值:   {avg_brier:.3f} (越低越准, 0.25=随机)\")
    print(f\"  软化温度:     {m.get('temp', 1.0):.2f}\")
    aw = k.get('avg_win', 0.012)
    al = k.get('avg_loss', 0.006)
    print(f\"  Kelly 比率 b: {aw/max(al,1e-6):.2f} (avg_win/avg_loss)\")
    print(f\"  累计胜负:     {k.get('win_count',0):.0f} 胜 / {k.get('loss_count',0):.0f} 负\")
except Exception as e:
    print(f'  读取失败: {e}')
"
    fi
}

cmd_logs() {
    if [ ! -f "$TRADES_LOG" ]; then
        fail "交易日志还不存在，先 start 看看"
        return 1
    fi
    info "实时跟踪 $TRADES_LOG (Ctrl+C 退出)"
    tail -f "$TRADES_LOG"
}

cmd_tail() {
    title "最近 50 行交易日志"
    if [ -f "$TRADES_LOG" ]; then
        tail -50 "$TRADES_LOG"
    else
        warn "$TRADES_LOG 不存在"
    fi
}

cmd_stats() {
    title "交易统计"
    if [ ! -f "$TRADES_JSONL" ]; then
        warn "$TRADES_JSONL 不存在 (还没有平仓的交易)"
        return 0
    fi
    "$PYTHON_BIN" -c "
import json, statistics
recs = []
with open('$TRADES_JSONL') as f:
    for ln in f:
        try:
            recs.append(json.loads(ln))
        except: pass

if not recs:
    print('暂无交易')
    raise SystemExit

print(f\"  交易总数:   {len(recs)}\")
total_pnl = sum(r['pnl_usd'] for r in recs)
print(f\"  累计盈亏:   \${total_pnl:+.4f}\")
wins = [r for r in recs if r['pnl_usd'] > 0]
losses = [r for r in recs if r['pnl_usd'] <= 0]
wr = len(wins) / len(recs) * 100 if recs else 0
print(f\"  胜率:       {len(wins)}/{len(recs)} ({wr:.1f}%)\")
if wins:
    print(f\"  平均盈利:   {statistics.mean(r['pnl_pct'] for r in wins)*100:+.2f}%\")
if losses:
    print(f\"  平均亏损:   {statistics.mean(r['pnl_pct'] for r in losses)*100:+.2f}%\")

print()
print('  --- 各信号源表现 ---')
for sig in ('LIQ', 'FR', 'MR'):
    rs = [r for r in recs if r.get('signal_type') == sig]
    if not rs:
        print(f\"  {sig}: 暂无交易\")
        continue
    w = sum(1 for r in rs if r['pnl_usd'] > 0)
    pnl = sum(r['pnl_usd'] for r in rs)
    print(f\"  {sig}: {w}/{len(rs)} ({w/len(rs)*100:.0f}%) PnL=\${pnl:+.4f}\")

print()
print('  --- 最近 10 笔 ---')
for r in recs[-10:]:
    icon = '✓' if r['pnl_usd'] > 0 else '✗'
    print(f\"  {icon} {r.get('symbol','?'):20s} {r.get('signal_type','?'):3s} \"
          f\"{r.get('direction','?'):5s} \${r['pnl_usd']:+.3f} \"
          f\"({r['pnl_pct']*100:+.2f}%) [{r.get('exit_reason','?')}]\")
"
}

cmd_brain() {
    title "大脑模型状态"
    if [ ! -f "$BRAIN_FILE" ]; then
        warn "$BRAIN_FILE 不存在 (还没保存过)"
        return 0
    fi
    "$PYTHON_BIN" -c "
import json
d = json.load(open('$BRAIN_FILE'))
m = d['model']
k = d['kelly']
t = d.get('tracker', {})

print(f\"  特征版本:     v{m.get('version', '?')}\")
print(f\"  模型更新:     {m.get('updates', 0)} 次\")
print(f\"  当前学习率:   {m.get('eta', 0):.4f}\")
print(f\"  软化温度:     {m.get('temp', 1.0):.2f}\")
rb = m.get('recent_brier') or [0.25]
print(f\"  Brier 均值:   {sum(rb)/len(rb):.3f} (50笔窗口)\")

print()
print('  --- 特征权重（按绝对值排序）---')
NAMES = [
    'atr_pct','hour_sin','hour_cos','is_liq','is_fr','is_mr','dir_long',
    'liq_size_z','liq_imbalance','price_drop_atr','fr_abs_z','mr_zscore',
    'bb_position','microprice_bias','l2_imbalance','taker_buy_ratio'
]
weights = list(zip(NAMES, m.get('w', [0]*len(NAMES))))
for n, w in sorted(weights, key=lambda x: abs(x[1]), reverse=True):
    bar_len = int(abs(w) * 20)
    bar = ('+' if w>0 else '-') * bar_len
    print(f\"  {n:18s}  {w:+.3f}  {bar}\")

print()
print('  --- Kelly 仓位计算 ---')
aw = k.get('avg_win', 0.012)
al = k.get('avg_loss', 0.006)
b = aw / max(al, 1e-6)
print(f\"  avg_win:     {aw*100:.2f}%\")
print(f\"  avg_loss:    {al*100:.2f}%\")
print(f\"  b (赔率):     {b:.2f}\")
print(f\"  累计胜负:     {k.get('win_count',0):.1f} / {k.get('loss_count',0):.1f}\")

print()
print('  --- 各信号源历史 ---')
for sig in ('LIQ', 'FR', 'MR'):
    bs = t.get('by_signal', {}).get(sig, {})
    w = bs.get('wins', 0)
    l = bs.get('losses', 0)
    p = bs.get('pnl', 0)
    n = w + l
    wr = w/n*100 if n else 0
    print(f\"  {sig}: {int(w)}胜/{int(l)}负 ({wr:.0f}%) PnL=\${p:+.4f}\")
"
}

cmd_trades() {
    title "最近 20 笔交易"
    if [ ! -f "$TRADES_JSONL" ]; then
        warn "$TRADES_JSONL 不存在"
        return 0
    fi
    "$PYTHON_BIN" -c "
import json
recs = []
with open('$TRADES_JSONL') as f:
    for ln in f:
        try: recs.append(json.loads(ln))
        except: pass
import datetime as dt
for r in recs[-20:]:
    ot = dt.datetime.fromtimestamp(r['close_ts']).strftime('%m-%d %H:%M')
    icon = '✓' if r['pnl_usd'] > 0 else '✗'
    print(f\"  {ot}  {icon} {r.get('symbol','?'):20s} {r.get('signal_type','?'):3s} \"
          f\"{r.get('direction','?'):5s} 入\${r['entry_price']:.6g} \"
          f\"出\${r['exit_price']:.6g} pnl=\${r['pnl_usd']:+.3f} \"
          f\"({r['pnl_pct']*100:+.2f}%) p={r['p_win_predicted']:.0%} \"
          f\"[{r.get('exit_reason','?')}]\")
"
}

cmd_reset() {
    title "重置大脑（备份 → 删除 → 重启）"
    echo "${YELLOW}这会清除所有已学习的模型权重和 Kelly 统计，从零开始。${RESET}"
    echo "${YELLOW}交易日志 (trades.jsonl) 不会被删除。${RESET}"
    echo
    echo "确认重置？(y/N)"
    read -r ans
    if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
        info "取消"
        return 0
    fi

    # 先停掉程序
    PID=$(find_pid)
    if [ -n "$PID" ]; then
        info "先停止运行中的程序..."
        cmd_stop
        sleep 2
    fi

    # 备份 brain + state
    BACKUP_DIR="$SCRIPT_DIR/brain_backup_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    BACKED=0
    for f in "$BRAIN_FILE" "$STATE_FILE"; do
        if [ -f "$f" ]; then
            cp -p "$f" "$BACKUP_DIR/"
            BACKED=$((BACKED + 1))
        fi
    done
    if [ $BACKED -gt 0 ]; then
        ok "已备份 $BACKED 个文件到 $BACKUP_DIR"
    fi

    # 删除 brain 和 state
    rm -f "$BRAIN_FILE" "$STATE_FILE"
    ok "已删除 brain.json 和 state.json"

    # 重启
    info "重新启动..."
    cmd_start
    echo
    ok "重置完成。大脑从零开始学习。"
    info "旧数据备份在: $BACKUP_DIR"
    info "交易日志保留在: $TRADES_JSONL"
}

cmd_help() {
    cat << EOF
${BOLD}Hermes v10 管理脚本${RESET}

用法:
  ./manage_v10.sh <命令>

命令:
  ${GREEN}start${RESET}      后台启动主程序
  ${GREEN}stop${RESET}       优雅停止（保存 brain 后退出）
  ${GREEN}restart${RESET}    重启
  ${GREEN}reset${RESET}      重置大脑（备份旧 brain → 删除 → 重启，从零学习）
  ${GREEN}status${RESET}     查看进程状态、当前持仓、大脑简况
  ${GREEN}logs${RESET}       实时跟踪交易日志（tail -f）
  ${GREEN}tail${RESET}       最近 50 行日志
  ${GREEN}stats${RESET}      交易统计（胜率/盈亏/各信号表现/最近10笔）
  ${GREEN}brain${RESET}      大脑模型详情（特征权重/Brier/Kelly）
  ${GREEN}trades${RESET}     最近 20 笔交易明细
  ${GREEN}help${RESET}       显示本帮助

文件位置:
  主程序:     $MAIN_PY
  交易日志:   $TRADES_LOG
  详细日志:   $TRADES_JSONL
  状态文件:   $STATE_FILE
  大脑文件:   $BRAIN_FILE
  stdout:    $STDOUT_LOG

EOF
}

# ---------- 路由 ----------
case "${1:-help}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    tail)    cmd_tail ;;
    stats)   cmd_stats ;;
    brain)   cmd_brain ;;
    trades)  cmd_trades ;;
    help|--help|-h) cmd_help ;;
    reset)   cmd_reset ;;
    *)
        fail "未知命令: $1"
        echo
        cmd_help
        exit 1
        ;;
esac
