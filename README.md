# mcp-mt5

MCP server for the **MetaTrader 4/5 build pipeline** — compile MQL sources, deploy compiled EAs, run Strategy Tester, parse reports, tail logs. CLI-driven (`MetaEditor64.exe`, `terminal64.exe`), no live broker connection, no Python `MetaTrader5` dependency.

> Complements live-trading MCPs by covering the **dev loop**: edit → compile → deploy → backtest → analyze.

## Why this exists

Existing MetaTrader MCP servers wrap the runtime Python API (positions, orders, quotes). None drive `MetaEditor64.exe` directly to compile `.mq5` files or invoke `terminal64.exe` for headless backtests. This server fills that gap so an LLM agent can iterate on a strategy without a human clicking through MT5's UI.

## Features

- **`compile`** — invoke MetaEditor CLI, parse log into structured `errors[]`/`warnings[]` with file/line/code/message
- **`compile_and_deploy`** — compile + copy `.ex5` to `Experts/` in one shot
- **`run_backtest`** — launch `terminal64.exe /config:tester.ini` (headless when `ShutdownTerminal=1`)
- **`patch_tester_ini`** — programmatically edit `tester.ini` keys before running
- **`read_tester_report`** — parse Strategy Tester HTML report into `summary` (PnL, profit factor, drawdown, etc.) + trade rows
- **`tail_log`** — read tail of `LiveLog.txt`, daily journal, or latest tester log; optional structured parsing
- **`deploy_ea`**, **`install_include`**, **`list_experts`** — file management
- **`list_terminals`** — enumerate all MT4/5 data folders + their `origin.txt` install paths
- **`kill_terminal`** — `taskkill` if MT5 hangs
- **`env_info`** — debug path resolution

## Install

```bash
pip install mcp-mt5
```

(Requires Windows + MetaTrader 4 or 5 already installed.)

For development:

```bash
git clone https://github.com/PHUICMT/mcp-mt5
cd mcp-mt5
pip install -e ".[dev]"
pytest
```

## Configuration

The server resolves the MetaTrader install + data folder in this order:

1. Explicit env vars (`MT5_INSTALL`, `MT5_DATA`, `MT5_TERMINAL_HASH`, `MT5_EDITION`)
2. Auto-scan `%APPDATA%\MetaQuotes\Terminal\*\origin.txt` matching the install path
3. Fall back to portable mode (data colocated with install)

| Env var | Default | Notes |
|---------|---------|-------|
| `MT5_INSTALL` | `C:\Program Files\MetaTrader 5` | Install dir containing `terminal64.exe` |
| `MT5_DATA` | (auto-detected) | `%APPDATA%\MetaQuotes\Terminal\<hash>` |
| `MT5_TERMINAL_HASH` | (auto-detected) | 32-char folder name |
| `MT5_EDITION` | `mt5` | `mt4` or `mt5` |

Run `env_info` once to verify resolution.

## Register with an MCP client

Most MCP clients accept a JSON entry under `mcpServers`. Example config:

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

## Example workflow

```
1. env_info                          → verify paths
2. compile source="MyEA.mq5"         → 0 errors/warnings ✅
3. deploy_ea source_ex="MyEA.ex5"    → copied to Experts/
4. patch_tester_ini config="tester.ini" updates={"Tester.Symbol":"EURUSD"}
5. run_backtest config="tester.ini"
6. read_tester_report                → summary.net_profit, profit_factor, ...
7. tail_log mode="tester" lines=200  → diagnose journal warnings
```

## MT4 support

Set `MT5_EDITION=mt4` and `MT5_INSTALL` to the MT4 install dir. The server will use `metaeditor.exe` (32-bit), `terminal.exe`, and the `MQL4/` data tree.

## Limitations

- Windows-only (MetaTrader CLI binaries don't ship for Linux/macOS; use Wine for ports).
- No live broker access — use a separate MCP server for runtime trading.
- Tester report parser is best-effort (MT5 HTML is brittle); raw HTML is also returned.

## License

MIT
