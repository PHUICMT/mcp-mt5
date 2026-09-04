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

27 tools and 2 resource templates. Every tool carries MCP annotations (read-only / destructive hints), a typed output schema and per-parameter descriptions; failures are reported as tool errors, never as a normal result with an `error` key.

### 🔍 Discovery & terminal selection

| Tool | Description |
|------|-------------|
| `env_info` | Resolved paths, terminal hash, edition, and missing-component issues |
| `list_terminals` | Every MT4/5 terminal data folder under `%APPDATA%\MetaQuotes\Terminal` with its `origin.txt` install path |
| `select_terminal` | Switch the active terminal data folder mid-session by origin path, hash, or install dir |

### 🔨 Build & deploy

| Tool | Description |
|------|-------------|
| `compile` | MetaEditor CLI on a `.mq4`/`.mq5`/`.mqh`. `ok` requires a `Result:` line with 0 errors **and** a freshly written binary. `syntax_only=true` uses `/s` for a fast check without a binary |
| `compile_and_deploy` | Compile, then copy the fresh `.ex4`/`.ex5` into `Experts/` |
| `deploy` | Copy a compiled binary into `Experts/` or a `.mqh` into `Include/`, chosen by extension |
| `smoke_test` | Compile + deploy + 1-day headless backtest + journal scan for runtime errors |
| `list_experts` | Enumerate `Experts/` (defaults to `*.ex5` on MT5, `*.ex4` on MT4) |

### 🔎 Source analysis & lint

| Tool | Description |
|------|-------------|
| `inspect_source` | `input` declarations, `#include` tree with missing files, MetaEditor doc blocks, whole-word symbol search, duplicate magic numbers (`aspects=[...]`) |
| `analyze_mql` | Structural lint (missing `OnInit`/`OnTick`, unused inputs, hardcoded magic/symbol), MT4-style deprecated calls with MT5 replacements, LOC/function/nesting metrics (`checks=[...]`) |
| `validate_tester_ini` | Required keys, date format, numeric ranges; cross-checks `[TesterInputs]` against the EA source |

### ✏️ Format & refactor

| Tool | Description |
|------|-------------|
| `format_mql` | `clang-format` with MQL literals (`D'…'`, `C'…'`) and `input group`/`#property` lines protected. Dry run unless `write=true`; keeps the file's encoding |
| `rename_symbol` | Whole-word rename across a tree, `dry_run` by default |
| `extract_function` | Brace-aware extraction of a line range into a helper (inline or into a `.mqh`), `dry_run` by default |

### 📊 Strategy Tester

| Tool | Description |
|------|-------------|
| `patch_tester_ini` | Update `Section.Key` values in a `tester.ini` in place (encoding preserved) |
| `gen_tester_inputs` | Build a `[TesterInputs]` block from the EA's inputs, optionally written into an ini |
| `run_backtest` | Launch `terminal64.exe /config:tester.ini`, wait for `ShutdownTerminal=1`, stream progress notifications, return `report_path`, `report_uri`, tester log and journal notes (e.g. `start_time_changed`) |
| `start_backtest` / `get_backtest` / `cancel_backtest` | Background runs: launch and get a `run_id` immediately, poll status (or list all runs), kill. Runs persist to `.mt5tmp/runs/` |
| `read_tester_report` | Parse an MT5/MT4 report into a ~50-field `summary`; `response_format="detailed"` adds every trade row. The HTML itself is served as the `mt5://report/{id}` resource |
| `compare_reports` | Diff two reports key by key with absolute/percent deltas; pass `guards` to get `violations`/`ok` |
| `read_optimization` | Read a `Tester/cache/*.opt` cache using the layout MetaQuotes published: header, optimised inputs, and the top N passes by any criterion over **all** passes |
| `kill_terminal` | Kill terminals launched by this server (`all_instances=true` for every instance) |

### 📝 Logs & snapshots

| Tool | Description |
|------|-------------|
| `tail_log` | Tail `Files/LiveLog.txt`, the daily `MQL5/Logs/YYYYMMDD.log`, or the newest tester log, optionally parsed into `{ts, source, message}` |
| `snapshot_sources` / `list_snapshots` | Freeze source files into a timestamped folder with a `manifest.json`; list them |

### 📡 MCP resources

| URI | Description |
|-----|-------------|
| `mt5://log/{mode}` | Last 500 lines of `livelog`, `journal` or `tester` |
| `mt5://report/{id}` | Full HTML of a report returned by `read_tester_report` / `get_backtest` (`latest` = newest on disk) |

### Renamed in 0.5.0

| Before | Now |
|---|---|
| `syntax_check` | `compile(syntax_only=true)` |
| `deploy_ea`, `install_include` | `deploy` |
| `extract_inputs`, `resolve_includes`, `extract_doc`, `find_symbol`, `find_magic_collision` | `inspect_source` |
| `lint_basic`, `check_deprecated`, `code_metrics` | `analyze_mql` |
| `format_check` | `format_mql` (dry run by default) |
| `parse_optimization`, `top_passes` | `read_optimization` |
| `regression_check` | `compare_reports(guards=…)` |
| `list_backtests` | `get_backtest()` without `run_id` |
| `mt5://livelog`, `mt5://journal`, `mt5://tester-log` | `mt5://log/{mode}` |

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
  "terminal_hash": "<32-char-hex-hash>",
  "metaeditor": "C:\\Program Files\\MetaTrader 5\\MetaEditor64.exe",
  "experts_dir": "C:\\Users\\<you>\\AppData\\Roaming\\MetaQuotes\\Terminal\\<hash>\\MQL5\\Experts",
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
2. compile_and_deploy source="C:\\...\\MyEA.mq5"       → 0 errors, .ex5 deployed ✅
3. patch_tester_ini config="tester.ini" updates={
     "Tester.Symbol": "EURUSD",
     "Tester.FromDate": "2025.01.01",
     "TesterInputs.RiskPct": "1.5"
   }
4. run_backtest config="tester.ini" wait=true
5. read_tester_report path=<report_path from step 4>  → summary.net_profit = 1234.56
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
pytest                    # runs the test suite (no MetaTrader needed)
ruff check src tests      # lints
```

CI runs on Windows for Python 3.10, 3.11, and 3.12 against every push to `main`. Tagging a release (e.g. `v0.2.0`) triggers an OIDC publish to PyPI.

### Project layout

```
mcp-mt5/
├── src/mcp_mt5/
│   ├── server.py        # FastMCP tool definitions
│   ├── paths.py         # Layout detection + origin.txt scan
│   ├── parsers.py       # Compile log, tester report/journal parsers, encoding helpers
│   ├── analysis.py, lint.py, formatting.py, refactor.py, ast_refactor.py
│   ├── optimization.py  # .opt cache reader (documented TesterOptCache layout)
│   ├── reports.py, snapshot.py, smoke.py, workdir.py
├── tests/               # pytest suite, no live MT5 required
├── examples/            # Sample tester.ini + client config
└── .github/workflows/   # CI + PyPI release
```

---

## Limitations

- **Windows-only.** MetaTrader CLI binaries don't ship for Linux/macOS. Wine ports may work but are untested.
- **No live broker access.** This server intentionally never authenticates to a broker. Use a separate MCP server for runtime trading.
- **Tester report parsing is best-effort.** MetaTrader's HTML output isn't a stable schema; the raw HTML is also returned alongside the parsed structure so you can fall back to text inspection when needed.
- **Optimisation caches (`Tester/cache/*.opt`) are parsed with the layout MetaQuotes published in *MQL5 Programming for Traders*.** Files with an unrecognised record layout are reported as such rather than guessed.

---

## Roadmap

All v0.3.x roadmap items shipped in v0.4.0. Future ideas:

- Real tree-sitter MQL grammar for `extract_function` (current implementation is brace-counting + regex)
- WebSocket transport for long-lived sessions (currently stdio only)
- Linux/Wine port for non-Windows agents

---

## License

[MIT](LICENSE) © 2026 PHUICMT
