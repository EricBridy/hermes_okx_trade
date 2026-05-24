# Hermes v10 / v11 Price Study

Hermes 是一个基于 OKX 永续合约的自适应交易机器人。
当前这一版的主策略已经切到价格行为学优先，核心是 `Break & Retest (BR)`。

## 当前结构

- `hermes_v10_main.py`：主循环，负责 universe、WS、候选过滤、风控
- `hermes_v10_signals.py`：信号层，当前以 `BR` 为主，`FR / MR / TF / LIQ` 保留为禁用或兼容
- `hermes_v10_executor.py`：下单、持仓监控、平仓、结算
- `hermes_v10_brain.py`：在线学习、Kelly、信号统计
- `manage_v10.sh`：启动、停止、状态、日志、统计

## 运行要求

- Python 3.11
- OKX 配置文件：`~/.okx/config.toml`
- 需要可访问 OKX 公共/私有接口

## 本地验证

```bash
/root/.local/bin/python3.11 -B -c "import py_compile, tempfile, os; files=['hermes_v10_main.py','hermes_v10_executor.py','hermes_v10_brain.py','hermes_v10_signals.py','test_v10_brain.py']; tmp=tempfile.gettempdir(); [py_compile.compile(f, cfile=os.path.join(tmp, os.path.basename(f)+'.pyc'), doraise=True) for f in files]; print('compiled', len(files))"
/root/.local/bin/python3.11 test_v10_brain.py
```

## 服务器启动

```bash
./manage_v10.sh start
./manage_v10.sh status
./manage_v10.sh logs
```

## 管理命令

```bash
./manage_v10.sh start
./manage_v10.sh stop
./manage_v10.sh restart
./manage_v10.sh status
./manage_v10.sh logs
./manage_v10.sh tail
./manage_v10.sh stats
./manage_v10.sh brain
./manage_v10.sh trades
```

## 策略说明

- 主信号：`BR`
- `FR` 仅保留为实验参考，不再作为主开仓入口
- 大脑文件使用新版本，避免旧统计污染
- 日内亏损刹车、持仓数上限、总暴露上限、冷却期都已启用

## 备注

如果需要完全冷启动，可以先确认当前无持仓，再执行：

```bash
./manage_v10.sh reset
```
