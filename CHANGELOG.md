# Changelog

## 0.4.0 — 2026-05-04

Roadmap completion: cross-broker selection, smoke harness, log subscription, AST refactor.

### New tools
- `select_terminal` — switch the active MetaTrader data folder mid-session by origin path, hash, or install dir
- `smoke_test` — compile + deploy + run a 1-day headless backtest + scan the journal for runtime errors (compile pass ≠ runtime pass)
- `extract_function` — brace-aware refactor that extracts a contiguous block into a new helper, either inline or into an external `.mqh`

### New MCP resources
- `mt5://livelog` — latest tail of `MQL5/Files/LiveLog.txt`
- `mt5://journal` — today's daily MT5 journal log
- `mt5://tester-log` — most recent Strategy Tester journal

Resources let MCP clients re-read on demand for log subscription / polling without a dedicated tool call.

### Internal
- New modules: `smoke.py`, `ast_refactor.py`
- `paths.list_terminal_origins()` helper
- Test count: 38 → 46

## 0.3.0 — 2026-05-04

Full dev-loop expansion: 18 new tools across 7 modules.

### Source analysis
- `extract_inputs` — parse `input <type> <name> = <default>;` declarations into JSON
- `gen_tester_inputs` — auto-build a `[TesterInputs]` block from EA source (translates `PERIOD_*` enums to numeric codes)
- `resolve_includes` — recursive `#include` resolution, reports missing files
- `find_symbol` — grep MQL files skipping comments and string literals
- `code_metrics` — LOC, function count, max nesting per file or aggregated across a tree
- `extract_doc` — pull MetaEditor `//+--+ //| ... +--+` doc blocks into markdown
- `find_magic_collision` — detect duplicate magic-number assignments

### Lint / validation
- `syntax_check` — MetaEditor `/s` syntax-only mode for fast feedback
- `lint_basic` — structural rules (missing `OnInit`/`OnDeinit`, unused inputs, hardcoded magic, hardcoded symbol)
- `check_deprecated` — flag MT4-style API calls (`OrderSend`, `Ask`, `AccountBalance`, …) with CTrade-style replacements
- `validate_tester_ini` — required keys, date format, numeric sanity, cross-check inputs vs EA source

### Formatting
- `format_mql` / `format_check` — clang-format wrap (treats MQL as C++ with an MQL-friendly default style)

### Refactor
- `rename_symbol` — whole-word rename across the project, with `dry_run` preview

### Optimization
- `parse_optimization` — best-effort `.opt` binary reader
- `top_passes` — sort optimization passes by criterion

### Reports
- `compare_reports` — diff two tester reports key-by-key with absolute and percent deltas
- `regression_check` — guard thresholds (e.g. "net_profit may not drop more than 5%") with violation reporting

### Snapshots
- `snapshot_sources` — freeze a copy of source files into a timestamped manifest folder
- `list_snapshots` — enumerate previously captured snapshots

### Internal
- New modules: `analysis.py`, `lint.py`, `formatting.py`, `refactor.py`, `optimization.py`, `reports.py`, `snapshot.py`
- Test suite expanded from 18 → 38 cases

## 0.2.0 — 2026-05-04

- Refactor into `paths.py` (layout detection) + `parsers.py` (compile log + tester report) + `server.py` (MCP tools)
- Auto-detect terminal data folder via `origin.txt` scan
- MT4 support (`MT5_EDITION=mt4`, `metaeditor.exe`, `MQL4/` tree)
- Structured tester report parser (summary key/values + trade row detection)
- New tools: `list_terminals`, `kill_terminal`, `compile_and_deploy`, `patch_tester_ini`, `install_include`
- Pytest test suite (18 tests covering parsers, paths, server tools)
- GitHub Actions CI + PyPI release workflow

## 0.1.0

- Initial release: `compile`, `run_backtest`, `tail_log`, `deploy_ea`, `list_experts`, `read_tester_report`, `env_info`
