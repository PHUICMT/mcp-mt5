# Changelog

## 0.4.2 — 2026-09-04

Bug-fix release. No new tools; several results that were silently wrong are now correct.

### Fixed
- **Install**: pin `mcp>=1.28,<2`. `mcp` 2.x renamed `FastMCP`, so every fresh install of 0.4.1 failed to import.
- **CI**: pin the ruff rule set (`E4,E7,E9,F`) so default-rule changes in newer ruff cannot turn CI red.
- **Compile success** now requires a `Result:` line with zero errors *and* an `.ex5`/`.ex4` newer than the invocation. A log without `Result:` (MetaEditor's silent-failure shape) is no longer treated as success. `compile` returns `result_line_found`, `binary_path`, `binary_fresh`.
- **`smoke_test`** used `"0 errors" not in log`, which matched `"10 errors"`; it now uses the compile-log parser and the freshness check, and only scans tester logs written by its own run.
- **`read_tester_report`** looked in `Tester/`; MT5 writes `Report=` relative to the data folder (or the install dir in `/portable`). Discovery now covers both plus `Tester/`, newest first, and `run_backtest` resolves the expected report path from the ini it ran (`report_path`).
- **`run_backtest` / `smoke_test`** detach the terminal's stdout/stderr (a child writing to stdout corrupts the MCP stdio transport), track the terminal PID, and surface tester journal notes: `start time changed to …` (the tester moved `FromDate`), `history data begins from`, `tested with error`.
- **`kill_terminal`** kills only terminals launched by this server; `all_instances=true` restores the old kill-everything behaviour. A live-trading terminal on the same machine is no longer collateral.
- **`top_passes`** ranked only the first 50 passes; it now ranks the complete pass list.
- **`parse_optimization`** replaces the guessed `.opt` record offsets with the `TesterOptCache` layout MetaQuotes published (`OptReader.mqh`): full header, input descriptors, all ENUM_STATISTICS fields and the optimised input values per pass. Unknown layouts are reported, never guessed. `find_latest_opt` can select by expert/symbol/period from the documented filename schema instead of mtime.
- **`format_mql`** defaults to a dry run (`write=false`) and protects `D'…'`, `C'…'`, `S'…'` literals and `input group` / `#property` / `#resource` / `#import` lines, which clang-format used to mangle.
- **Encoding**: UTF-16 files without a BOM are detected; `patch_tester_ini`, `gen_tester_inputs`, `rename_symbol`, `extract_function` and `format_mql` write back in the file's original encoding (MT5 writes ini/set files as UTF-16 LE).
- **`list_experts`** defaults to `*.ex4` on MT4.
- **`extract_function`** only turns declarations inside the enclosing function into parameters (globals and other functions' locals leaked before).
- **`find_symbol`** no longer re-splits the file for every match (quadratic on large files).
- Shared `workdir()` helper replaces two copies; `list_terminals` reuses `paths.list_terminal_origins`; a `reset_layout_cache()` helper fixes test-order dependence on the layout cache.
- README: tool/test counts and the stale "optimisation not parsed yet" limitation.
- **Report parser, verified against real MT5 and MT4 reports from public repositories**: labels are matched case-insensitively for both MT5 ("Total Net Profit") and MT4 ("Total net profit") spellings, the first value seen wins (the deals-table header "Symbol | Type" no longer overwrites `symbol`), and ~45 additional fields are extracted (balance/equity drawdown absolute/maximal/relative, History Quality, AHPR/GHPR, LR correlation, Z-Score, consecutive-trade stats, holding times). `compare_reports`/`regression_check` now parse MT5's space-grouped numbers ("10 000.00") and composite cells ("359.61 (3.34%)").

## 0.4.1 — 2026-05-04

### Changed
- `compile`, `syntax_check`, and `smoke_test` now write logs and temporary tester ini
  files into a hidden `.mt5tmp/` directory next to the source instead of leaking
  `*.log` and `*.smoke.ini` files into the project root.
- Override the working directory via the `MT5_WORK_DIR` environment variable.
- Add `.mt5tmp/` to your project `.gitignore` to keep these artefacts out of VCS.

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
