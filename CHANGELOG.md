# Changelog

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
