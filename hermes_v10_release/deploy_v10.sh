#!/usr/bin/env bash
# ============================================================
#  Hermes v10 一键部署脚本
#
#  用法：
#    ./deploy_v10.sh             部署到当前目录（推荐）
#    ./deploy_v10.sh /目标/路径   部署到指定目录
#
#  所有运行时产出文件（state、brain、日志、交易记录）
#  都会和程序文件一起放在同一目录，方便管理。
# ============================================================

set -e  # 任何一步失败立刻退出

# ---------- 颜色 ----------
RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BLUE=$'\e[34m'; BOLD=$'\e[1m'; RESET=$'\e[0m'

ok()    { echo "${GREEN}✓${RESET} $*"; }
info()  { echo "${BLUE}→${RESET} $*"; }
warn()  { echo "${YELLOW}⚠${RESET} $*"; }
fail()  { echo "${RED}✗${RESET} $*"; }
title() { echo; echo "${BOLD}=== $* ===${RESET}"; }

# ---------- 路径解析 ----------
PYTHON_BIN="${PYTHON_BIN:-/root/.local/bin/python3.11}"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

# 第一个参数是目标目录；不传则用当前目录
if [ -n "$1" ]; then
    TARGET_DIR="$(cd "$1" 2>/dev/null && pwd || echo "$1")"
    # 如果传入的目录还不存在，先标记下来，后面会创建
    NEED_MKDIR=1
else
    TARGET_DIR="$SOURCE_DIR"
    NEED_MKDIR=0
fi

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

# ---------- 顶部信息 ----------
title "Hermes v10 部署脚本"
info "源目录:        $SOURCE_DIR"
info "运行目录:      $TARGET_DIR"
info "Python 解释器: $PYTHON_BIN"
echo "${YELLOW}所有运行产出（state.json / brain.json / 日志）都会写到运行目录${RESET}"
echo

# ---------- 1. 检查 Python 3.11 ----------
title "步骤 1/8 — 检查 Python 解释器"
if [ ! -x "$PYTHON_BIN" ]; then
    fail "找不到 $PYTHON_BIN"
    fail "请先安装 Python 3.11，或用 PYTHON_BIN=/path/to/python3.11 ./deploy_v10.sh"
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

# ---------- 4. 检查是否还有进程在跑 ----------
title "步骤 4/8 — 检查是否有进程在运行"
V82_PIDS=$(pgrep -f "v82_yao_hunter.py" 2>/dev/null || true)
V10_PIDS=$(pgrep -f "hermes_v10_main.py" 2>/dev/null || true)

if [ -n "$V82_PIDS" ]; then
    warn "v82 仍在运行 (PID: $V82_PIDS)"
    echo "${YELLOW}是否要停掉 v82 ？(y/N)${RESET}"
    read -r ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
        kill -INT $V82_PIDS 2>/dev/null || true
        sleep 5
        STILL=$(pgrep -f "v82_yao_hunter.py" 2>/dev/null || true)
        if [ -n "$STILL" ]; then
            kill -9 $STILL 2>/dev/null || true
        fi
        ok "v82 已停止"
    else
        fail "请先手动停掉 v82 再部署"
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
        fail "请先停掉旧 v10 再部署"
        exit 1
    fi
fi

# ---------- 5. 准备目标目录 ----------
title "步骤 5/8 — 准备运行目录"
mkdir -p "$TARGET_DIR"
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"
ok "运行目录: $TARGET_DIR"

# 备份旧的 v10 状态文件（如果有）
if [ "$TARGET_DIR" != "$SOURCE_DIR" ]; then
    BACKUP=0
    BACKUP_DIR="$TARGET_DIR/backup_$(date +%Y%m%d_%H%M%S)"
    for f in hermes_v10_state.json hermes_v10_brain.json hermes_v10_trades.jsonl; do
        if [ -f "$TARGET_DIR/$f" ]; then
            mkdir -p "$BACKUP_DIR"
            cp -p "$TARGET_DIR/$f" "$BACKUP_DIR/"
            BACKUP=$((BACKUP + 1))
        fi
    done
    if [ $BACKUP -gt 0 ]; then
        ok "备份了 $BACKUP 个旧的运行时文件 → $BACKUP_DIR"
    fi
fi

# ---------- 6. 复制文件 ----------
title "步骤 6/8 — 复制程序文件"
if [ "$TARGET_DIR" = "$SOURCE_DIR" ]; then
    ok "源目录和运行目录相同，跳过复制"
else
    for f in "${V10_FILES[@]}"; do
        cp "$SOURCE_DIR/$f" "$TARGET_DIR/$f"
    done
    ok "已复制 ${#V10_FILES[@]} 个文件"
fi
chmod +x "$TARGET_DIR/manage_v10.sh"
chmod +x "$TARGET_DIR/deploy_v10.sh" 2>/dev/null || true

# ---------- 7. 运行单元测试 ----------
title "步骤 7/8 — 运行 brain 单元测试"
cd "$TARGET_DIR"
TEST_OUT=$(mktemp)
if "$PYTHON_BIN" test_v10_brain.py > "$TEST_OUT" 2>&1; then
    if grep -q "ALL v10 BRAIN TESTS PASSED" "$TEST_OUT"; then
        ok "全部 brain 测试通过"
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

# ---------- 8. 主模块 import 烟雾测试 ----------
title "步骤 8/8 — 主模块 import 烟雾测试"
"$PYTHON_BIN" -c "
import hermes_v10_main as m
print('  Engine 类:', m.Engine.__name__)
print('  信号生成器:', m.LiquidationSignal.__name__,
                       m.FundingRateSignal.__name__,
                       m.MeanReversionSignal.__name__)
print('  特征数:', len(m.StrategyBrain().model.w))
print('  运行目录:', m.SCRIPT_DIR)
" || { fail "import 失败"; exit 1; }
ok "主模块 import 正常"

# ---------- 完成 ----------
echo
echo "${GREEN}${BOLD}============================================================${RESET}"
echo "${GREEN}${BOLD}            ✓ 部署完成！${RESET}"
echo "${GREEN}${BOLD}============================================================${RESET}"
echo
echo "运行目录: ${BOLD}$TARGET_DIR${RESET}"
echo "下一步:"
echo
echo "  ${BOLD}cd $TARGET_DIR${RESET}"
echo
echo "  # 1) 前台跑一遍验证启动"
echo "  ${BOLD}$PYTHON_BIN hermes_v10_main.py${RESET}"
echo "     看到 'instruments cached / [ws] connected' 就是 OK，按 Ctrl+C 停"
echo
echo "  # 2) 切后台"
echo "  ${BOLD}./manage_v10.sh start${RESET}"
echo
echo "  # 3) 看状态"
echo "  ${BOLD}./manage_v10.sh status${RESET}"
echo "  ${BOLD}./manage_v10.sh logs${RESET}"
echo
echo "  # 4) 全部命令"
echo "  ${BOLD}./manage_v10.sh help${RESET}"
echo
