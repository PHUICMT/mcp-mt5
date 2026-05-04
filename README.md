# mcp-mt5

> **Model Context Protocol server for the MetaTrader 4/5 build pipeline.**
> Compile MQL sources, deploy compiled EAs, run Strategy Tester, parse reports, tail logs — all driven by an LLM agent without touching the MetaTrader UI.

[![CI](https://github.com/PHUICMT/mcp-mt5/actions/workflows/ci.yml/badge.svg)](https://github.com/PHUICMT/mcp-mt5/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](https://www.metaquotes.net/)

---

## What this is — and what it isn't

| ✅ This server | ❌ Not this server |
|----------------|--------------------|
| MetaTrader **dev harness** — compile, deploy, backtest, parse | Live trading (orders, positions, quotes) |
| Wraps `MetaEditor64.exe` / `terminal64.exe` CLI directly | Wraps the `MetaTrader5` Python package |
| Runs entirely offline against installed terminal | Connects to a broker server |
| Iterates strategies *before* they go live | Executes strategies in production |

> **Use case:** an LLM agent edits `.mq5` source → compiles → deploys → runs Strategy Tester → reads report → adjusts → repeats. No broker login, no human in the loop, no risk of real-money execution.

For runtime trading, pair this with a live-trading MCP — they target different layers and compose well.

---

## Tools

The server exposes 11 tools across four categories.

### 🔍 Discovery

| Tool | Description |
|------|-------------|
| `env_info` | Dump resolved paths, terminal hash, edition, and missing-component issues |
| `list_terminals` | Enumerate every MT4/5 terminal data folder under `%APPDATA%\MetaQuotes\Terminal` along with each `origin.txt` install path |

### 🔨 Build & deploy

| Tool | Description |
|------|-------------|
| `compile` | Invoke MetaEditor CLI on a `.mq4`/`.mq5`/`.mqh` source. Returns structured `errors[]`/`warnings[]` (file, line, column, error code, message) plus log excerpt |
| `compile_and_deploy` | Compile, then copy the resulting `.ex4`/`.ex5` into the terminal's `Experts/` folder in one call |
| `deploy_ea` | Copy a compiled binary into `Experts/` (with optional rename) |
| `install_include` | Copy a `.mqh` header into the terminal `Include/` folder — handy for libraries like `LiveLog.mqh` |
| `list_experts` | Enumerate `Experts/` recursively with size and modification time |

### 📊 Strategy Tester

| Tool | Description |
|------|-------------|
| `patch_tester_ini` | Programmatically update keys in a `tester.ini` (e.g. `Tester.Symbol`, `Tester.FromDate`, `TesterInputs.RiskPct`) before running |
| `run_backtest` | Launch `terminal64.exe /config:tester.ini`, optionally headless (when `ShutdownTerminal=1`), and return the latest tester log path |
| `read_tester_report` | Locate and parse the latest tester HTML report into a structured `summary` (net profit, profit factor, drawdown, trade counts, etc.) plus a sample of trade rows |
| `kill_terminal` | `taskkill` if the terminal hangs |

### 📝 Logs

| Tool | Description |
|------|-------------|
| `tail_log` | Tail the last *N* lines of either `Files/LiveLog.txt`, the daily `Logs/YYYYMMDD.log`, or the most recent tester log. Optional structured parse into `{ts, source, message}` records |

---

## Quick start

### Install

```bash
pip install mcp-mt5
```

> Requires Windows + an installed MetaTrader 4 or 5 terminal.

### Register with an MCP client

Most MCP clients accept a JSON entry under `mcpServers`. The server inherits its configuration from environment variables:

```json
{
  "mcpServers": {
    "mt5": {
      "command": "mcp-mt5",
      "env": {
        "MT5_INSTALL": "C:\\Program Files\\MetaTrader 5"
      }
    }
  }
}
```

Refer to your client's documentation for the exact config file location.

### Verify the install

Once registered, ask your agent to call `env_info`:

```json
{
  "edition": "mt5",
  "install": "C:\\Program Files\\MetaTrader 5",
  "terminal_hash": "D0E8209F77C8CF37AD8BF550E51FF075",
  "metaeditor": "C:\\Program Files\\MetaTrader 5\\MetaEditor64.exe",
  "experts_dir": "C:\\Users\\...\\MQL5\\Experts",
  "issues": []
}
```

An empty `issues` array means everything is wired up correctly.

---

## Configuration

Resolution priority for the MetaTrader install + data folder:

1. **Explicit env vars** (below)
2. **Auto-scan** of `%APPDATA%\MetaQuotes\Terminal\*\origin.txt` for a folder whose origin matches `MT5_INSTALL`
3. **Portable mode** fallback (data colocated with install dir)

| Env var | Default | Notes |
|---------|---------|-------|
| `MT5_INSTALL` | `C:\Program Files\MetaTrader 5` | Install dir containing `terminal64.exe` |
| `MT5_DATA` | _(auto-detected)_ | `%APPDATA%\MetaQuotes\Terminal\<hash>` |
| `MT5_TERMINAL_HASH` | _(auto-detected)_ | 32-char folder name |
| `MT5_EDITION` | `mt5` | Set to `mt4` for MetaTrader 4 |

### MT4 support

Set `MT5_EDITION=mt4` and point `MT5_INSTALL` at your MT4 install. The server switches to `metaeditor.exe` (32-bit), `terminal.exe`, and the `MQL4/` data tree automatically.

---

## Example workflow

A typical LLM-driven iteration loop:

```
1. env_info                                          → verify paths
2. compile_and_deploy source="MyEA.mq5"              → 0 errors, .ex5 deployed ✅
3. patch_tester_ini config="tester.ini" updates={
     "Tester.Symbol": "EURUSD",
     "Tester.FromDate": "2025.01.01",
     "TesterInputs.RiskPct": "1.5"
   }
4. run_backtest config="tester.ini" wait=true
5. read_tester_report                                → summary.net_profit = 1234.56
                                                       summary.profit_factor = 1.45
6. tail_log mode="tester" lines=200 structured=true  → diagnose journal warnings
7. <edit Signal.mqh based on findings>
8. → loop back to step 2
```

---

## A sample `tester.ini`

```ini
; Launch: terminal64.exe /config:tester.ini
; Period codes: M1=1, M5=5, M15=15, H1=16385, H4=16388, D1=16408
; Model: 0=Every tick, 1=1 min OHLC, 4=Real ticks

[Tester]
Expert=MyEA
Symbol=EURUSD
Period=M15
Model=1
FromDate=2024.01.01
ToDate=2024.12.31
Deposit=10000
Currency=USD
Leverage=500
Visual=0
ShutdownTerminal=1     ; required so run_backtest can wait for the run to finish
Report=tester_report

[TesterInputs]
; ParamName=value||start||step||stop||(N=fixed|Y=optimize)
; RiskPct=1.0||0.1||0.1||3.0||N
```

A more complete sample lives at [`examples/tester.ini`](examples/tester.ini).

---

## Development

```bash
git clone https://github.com/PHUICMT/mcp-mt5
cd mcp-mt5
pip install -e ".[dev]"
pytest                    # runs the 18-test suite
ruff check src tests      # lints
```

CI runs on Windows for Python 3.10, 3.11, and 3.12 against every push to `main`. Tagging a release (e.g. `v0.2.0`) triggers an OIDC publish to PyPI.

### Project layout

```
mcp-mt5/
├── src/mcp_mt5/
│   ├── server.py        # FastMCP tool definitions
│   ├── paths.py         # Layout detection + origin.txt scan
│   └── parsers.py       # Compile log + tester HTML report parsers
├── tests/               # 18 pytest tests, no live MT5 required
├── examples/            # Sample tester.ini + client config
└── .github/workflows/   # CI + PyPI release
```

---

## Limitations

- **Windows-only.** MetaTrader CLI binaries don't ship for Linux/macOS. Wine ports may work but are untested.
- **No live broker access.** This server intentionally never authenticates to a broker. Use a separate MCP server for runtime trading.
- **Tester report parsing is best-effort.** MetaTrader's HTML output isn't a stable schema; the raw HTML is also returned alongside the parsed structure so you can fall back to text inspection when needed.
- **Optimization runs are not parsed yet.** Single-pass backtests are fully supported; `.opt` results are on the roadmap.

---

## Roadmap

- Strategy Tester optimization (`Optimization=1`) result parser
- Auto-snapshot of source files at backtest time (parity with the `MQL Clangd` extension's source-snapshot feature)
- Cross-broker terminal selection helper (`select_terminal` by origin path or hash)
- Long-running log subscription via MCP resources

---

## License

[MIT](LICENSE) © 2026 PHUICMT
