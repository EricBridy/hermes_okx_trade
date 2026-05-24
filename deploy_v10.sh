#!/usr/bin/env bash
# ============================================================
#  Hermes v10 一键部署脚本
#  在服务器上执行：./deploy_v10.sh
# ============================================================

set -e  # 任何一步失败立刻退出

# ---------- 颜色输出辅助 ----------
RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BLUE=$'\e[34m'; BOLD=$'\e[1m'; RESET=$'\e[0m'

ok()    { echo "${GREEN}✓${RESET} $*"; }
info()  { echo "${BLUE}→${RESET} $*"; }
warn()  { echo "${YELLOW}⚠${RESET} $*"; }
fail()  { echo "${RED}✗${RESET} $*"; }
title() { echo; echo "${BOLD}=== $* ===${RESET}"; }

# ---------- 路径常量 ----------
PYTHON_BIN="/root/.local/bin/python3.11"
TARGET_DIR="/root/.hermes/scripts"
BACKUP_DIR="/root/.hermes/backup/v82_$(date +%Y%m%d_%H%M%S)"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
OKX_CONFIG="$HOME/.okx/config.toml"

V10_FILES=(
    hermes_v10_brain.py
    hermes_v10_okx.py
    hermes_v10_signals.py
    hermes_v10_executor.py
    hermes_v10_main.py
    test_v10_brain.py
    manage_v10.sh
)

# ---------- 0. 顶部信息 ----------
title "Hermes v10 部署脚本"
info "源目录:      $SOURCE_DIR"
info "目标目录:    $TARGET_DIR"
info "Python 解释器: $PYTHON_BIN"
echo

# ---------- 1. 检查 Python 3.11 ----------
title "步骤 1/8 — 检查 Python 解释器"
if [ ! -x "$PYTHON_BIN" ]; then
    fail "找不到 $PYTHON_BIN"
    fail "请先安装 Python 3.11，或修改本脚本顶部的 PYTHON_BIN 路径"
    exit 1
fi
PY_VER=$("$PYTHON_BIN" --version 2>&1)
ok "$PY_VER ($PYTHON_BIN)"

# ---------- 2. 检查 OKX 配置 ----------
title "步骤 2/8 — 检查 OKX API 配置"
if [ ! -f "$OKX_CONFIG" ]; then
    fail "找不到 OKX 配置: $OKX_CONFIG"
    fail "需要文件内容（举例）："
    echo "    api_key = \"...\""
    echo "    secret_key = \"...\""
    echo "    passphrase = \"...\""
    exit 1
fi
# 不打印敏感内容，只看是否含三个必要字段
for k in api_key secret_key passphrase; do
    if ! grep -q "^[[:space:]]*$k" "$OKX_CONFIG"; then
        fail "$OKX_CONFIG 缺少字段: $k"
        exit 1
    fi
done
ok "OKX 配置文件存在且字段完整"

# ---------- 3. 检查源文件 ----------
title "步骤 3/8 — 检查 v10 源文件是否齐全"
MISSING=0
for f in "${V10_FILES[@]}"; do
    if [ ! -f "$SOURCE_DIR/$f" ]; then
        fail "缺少文件: $f"
        MISSING=$((MISSING + 1))
    fi
done
if [ $MISSING -gt 0 ]; then
    fail "请确保所有 v10 文件都已上传到 $SOURCE_DIR"
    exit 1
fi
ok "全部 ${#V10_FILES[@]} 个文件齐全"

# ---------- 4. 检查是否还有 v82 在跑 ----------
title "步骤 4/8 — 检查 v82 是否仍在运行"
V82_PIDS=$(pgrep -f "v82_yao_hunter.py" 2>/dev/null || true)
V10_PIDS=$(pgrep -f "hermes_v10_main.py" 2>/dev/null || true)

if [ -n "$V82_PIDS" ]; then
    warn "v82 仍在运行 (PID: $V82_PIDS)"
    echo "${YELLOW}是否要停掉 v82 ？(y/N)${RESET}"
    read -r ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
        info "发送 SIGINT 给 v82..."
        kill -INT $V82_PIDS 2>/dev/null || true
        sleep 5
        # 还活着就强制
        STILL=$(pgrep -f "v82_yao_hunter.py" 2>/dev/null || true)
        if [ -n "$STILL" ]; then
            warn "v82 没响应 SIGINT，强制 kill..."
            kill -9 $STILL 2>/dev/null || true
        fi
        ok "v82 已停止"
    else
        fail "请先手动停掉 v82 再来部署"
        exit 1
    fi
else
    ok "未发现 v82 进程"
fi

if [ -n "$V10_PIDS" ]; then
    warn "v10 已经在跑 (PID: $V10_PIDS)"
    echo "${YELLOW}部署前需要先停掉它。是否停止？(y/N)${RESET}"
    read -r ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
        kill -INT $V10_PIDS 2>/dev/null || true
        sleep 5
        STILL=$(pgrep -f "hermes_v10_main.py" 2>/dev/null || true)
        if [ -n "$STILL" ]; then
            kill -9 $STILL 2>/dev/null || true
        fi
        ok "v10 旧进程已停止"
    else
        fail "请先停掉旧 v10 再来部署"
        exit 1
    fi
fi

# ---------- 5. 备份旧文件 ----------
title "步骤 5/8 — 备份旧版本文件"
mkdir -p "$BACKUP_DIR"
BACKED_UP=0
if [ -d "$TARGET_DIR" ]; then
    for f in v82_yao_hunter.py adaptive_engine.py \
             v82_state.json v82_adaptive.json v82_chain_cache.json \
             v82_trades.log v82_yao_hunter_v78_safety_patch.py; do
        if [ -f "$TARGET_DIR/$f" ]; then
            cp -p "$TARGET_DIR/$f" "$BACKUP_DIR/"
            BACKED_UP=$((BACKED_UP + 1))
        fi
    done
fi
if [ $BACKED_UP -gt 0 ]; then
    ok "已备份 $BACKED_UP 个 v82 文件到 $BACKUP_DIR"
else
    info "没有找到需要备份的 v82 文件（首次部署）"
    rmdir "$BACKUP_DIR" 2>/dev/null || true
fi

# ---------- 6. 创建目录并复制文件 ----------
title "步骤 6/8 — 复制 v10 文件到 $TARGET_DIR"
mkdir -p "$TARGET_DIR"
for f in "${V10_FILES[@]}"; do
    cp "$SOURCE_DIR/$f" "$TARGET_DIR/$f"
done
chmod +x "$TARGET_DIR/manage_v10.sh"
ok "${#V10_FILES[@]} 个文件已复制"

# ---------- 7. 运行单元测试 ----------
title "步骤 7/8 — 运行 brain 单元测试"
cd "$TARGET_DIR"
TEST_OUT=$(mktemp)
if "$PYTHON_BIN" test_v10_brain.py > "$TEST_OUT" 2>&1; then
    if grep -q "ALL v10 BRAIN TESTS PASSED" "$TEST_OUT"; then
        ok "全部 brain 测试通过"
        # 简单展示关键行
        grep -E "(OK|features|acc=|liq_imbalance|microprice)" "$TEST_OUT" | head -10
    else
        fail "测试结果异常"
        cat "$TEST_OUT"
        rm -f "$TEST_OUT"
        exit 1
    fi
else
    fail "brain 测试失败"
    cat "$TEST_OUT"
    rm -f "$TEST_OUT"
    exit 1
fi
rm -f "$TEST_OUT"

# ---------- 8. import 烟雾测试 ----------
title "步骤 8/8 — 主模块 import 烟雾测试（不实际下单）"
"$PYTHON_BIN" -c "
import hermes_v10_main as m
print('  Engine 类:', m.Engine.__name__)
print('  信号生成器:', m.LiquidationSignal.__name__,
                       m.FundingRateSignal.__name__,
                       m.MeanReversionSignal.__name__)
print('  特征数:', len(m.StrategyBrain().model.w))
print('  状态文件路径:', m.SCRIPT_DIR)
" || { fail "import 失败"; exit 1; }
ok "主模块 import 正常"

# ---------- 完成提示 ----------
echo
echo "${GREEN}${BOLD}============================================================${RESET}"
echo "${GREEN}${BOLD}            ✓ 部署完成！${RESET}"
echo "${GREEN}${BOLD}============================================================${RESET}"
echo
echo "下一步操作："
echo
echo "  ${BOLD}cd $TARGET_DIR${RESET}"
echo
echo "  # 1) 先在前台跑一会儿，确认能正常启动"
echo "  ${BOLD}$PYTHON_BIN hermes_v10_main.py${RESET}"
echo
echo "     看到这样的输出说明启动成功（按 Ctrl+C 停止）："
echo "        Hermes v10 — adaptive perpetual strategy"
echo "        instruments cached: ..."
echo "        universe refreshed: ..."
echo "        [ws] connected wss://..."
echo
echo "  # 2) 确认无误后，用管理脚本切到后台"
echo "  ${BOLD}./manage_v10.sh start${RESET}"
echo
echo "  # 3) 查看状态 / 日志"
echo "  ${BOLD}./manage_v10.sh status${RESET}"
echo "  ${BOLD}./manage_v10.sh logs${RESET}"
echo
echo "  # 4) 停止 / 重启"
echo "  ${BOLD}./manage_v10.sh stop${RESET}"
echo "  ${BOLD}./manage_v10.sh restart${RESET}"
echo
echo "  # 5) 24h 后看战绩"
echo "  ${BOLD}./manage_v10.sh stats${RESET}"
echo "  ${BOLD}./manage_v10.sh brain${RESET}"
echo
echo "${YELLOW}提示：v10 第一天是 cold-start 期，模型还没学到东西，"
echo "      会以最小 Kelly 仓位（10%）保守下单。"
echo "      约 30-50 笔后开始分化，150-200 笔后权重稳定。${RESET}"
echo
