# Hermes v10 / v11 Price Study

Hermes is an OKX-only adaptive perpetual-swap trading bot. This branch moves
the active alpha toward price action, with Break & Retest (`BR`) as the primary
signal family.

## Files

- `hermes_v10_main.py` - async engine, universe refresh, risk gates, routing
- `hermes_v10_signals.py` - signal layer; `BR` is active, legacy families are disabled or compatibility-only
- `hermes_v10_executor.py` - order placement, position monitoring, settlement
- `hermes_v10_brain.py` - online learning, Kelly sizing, per-signal statistics
- `manage_v10.sh` - server lifecycle and inspection commands
- `test_v10_brain.py` - smoke tests for the learning layer

## Runtime Requirements

- Python 3.11
- OKX credentials at `~/.okx/config.toml`
- Network access to OKX REST and WebSocket endpoints

## Validation

```bash
/root/.local/bin/python3.11 -B -c "import py_compile, tempfile, os; files=['hermes_v10_main.py','hermes_v10_executor.py','hermes_v10_brain.py','hermes_v10_signals.py','test_v10_brain.py']; tmp=tempfile.gettempdir(); [py_compile.compile(f, cfile=os.path.join(tmp, os.path.basename(f)+'.pyc'), doraise=True) for f in files]; print('compiled', len(files))"
/root/.local/bin/python3.11 test_v10_brain.py
```

## Server Commands

```bash
./manage_v10.sh start
./manage_v10.sh stop
./manage_v10.sh restart
./manage_v10.sh status
./manage_v10.sh logs
./manage_v10.sh stats
./manage_v10.sh brain
./manage_v10.sh trades
```

## Strategy Notes

- Primary signal: `BR`
- `FR` is no longer used as a main entry alpha
- `BR` is tracked separately from `MR`
- New learning files use the `hermes_v11_*` prefix to avoid old-label pollution
- Daily loss brake, total exposure cap, same-direction cap, and cooldowns remain active
- Small accounts can upsize a passing signal to the minimum OKX lot when the normal Kelly fraction is too small, while still respecting total exposure headroom

## Cold Start

Only reset when there are no open positions and you intentionally want to clear
runtime state:

```bash
./manage_v10.sh reset
```
