# Roadmap

Future feature ideas for `mcp-mt5`, grouped by impact tier. Effort legend: 🟢 small / 🟡 medium / 🔴 large.

---

## v0.5.0 — Strategy research pipeline

The next minor release focuses on closing the loop from "I have a backtest" to "I trust this backtest". These five tools compose into a research-grade pipeline:

```
walk_forward_split → run each window → compare_strategies → monte_carlo_trades → markdown_report
```

| Tool | Description | Effort |
|------|-------------|--------|
| `walk_forward_split` | Generate IS/OOS `tester.ini` pairs (e.g. 6-month train + 1-month test, sliding window) | 🟢 |
| `monte_carlo_trades` | Shuffle backtest trade order, recompute equity curves, return Sharpe / max-DD distribution | 🟡 |
| `compare_strategies` | Two EAs over the same config — head-to-head net profit, drawdown, profit factor, Sharpe | 🟢 |
| `equity_curve_to_image` | Render PnL curve to PNG via matplotlib (optional dep) | 🟢 |
| `report_to_csv` | Tester HTML → trades CSV for Excel / pandas downstream | 🟢 |

---

## Tier A — Strategy research (high value)

| Tool | Description | Effort |
|------|-------------|--------|
| `walk_forward_split` | IS/OOS pair generator | 🟢 |
| `monte_carlo_trades` | Trade-shuffle robustness score | 🟡 |
| `mutate_param` | Generate ±10% / ±25% variants of a single `Inp*` | 🟢 |
| `parameter_sweep` | Run N variants sequentially, collect into table | 🟡 |
| `compare_strategies` | A/B compare two EAs | 🟢 |
| `equity_curve_to_image` | PNG render of PnL curve | 🟢 |
| `report_to_csv` | Trades CSV export | 🟢 |

## Tier B — Code quality

| Tool | Description | Effort |
|------|-------------|--------|
| `find_dead_code` | Functions defined but never called | 🟢 |
| `complexity` | Cyclomatic complexity per function (warn > 10) | 🟢 |
| `dependency_graph_dot` | Render `#include` tree as DOT / mermaid | 🟢 |
| `pre_commit_hook` | Emit `.git/hooks/pre-commit` running syntax_check + lint | 🟢 |
| `markdown_report` | Aggregate multi-tool output into a sharable MD report | 🟢 |

## Tier C — Debug helpers

| Tool | Description | Effort |
|------|-------------|--------|
| `inject_print` | Auto-inject `LogDebug()` at branch points, recompile, rerun | 🟡 |
| `stack_trace_decode` | Parse runtime error log → resolved `file:line` + source context | 🟢 |
| `breakpoint_suggest` | Find good lines for `// @watch` (MQL Clangd integration) | 🟡 |
| `tester_status` | Read live tester log → progress %, ETA | 🟢 |

## Tier D — Data prep

| Tool | Description | Effort |
|------|-------------|--------|
| `download_history_check` | Pre-flight: verify history covers `tester.ini` date range | 🟡 |
| `symbol_specs` | Parse `symbols.raw` → tick size / digits / contract size table | 🟡 |
| `mock_input_set` | Random valid `Inp*` set for fuzz testing | 🟢 |

## Tier E — Workflow / CI

| Tool | Description | Effort |
|------|-------------|--------|
| `cron_backtest` | Register Windows Task Scheduler entry for overnight backtests | 🟡 |
| `git_status_for_strategy` | `git log` filtered to `.mq5`/`.mqh` only — what changed since last green smoke | 🟢 |
| `auto_changelog` | git log → CHANGELOG.md entry suggestion | 🟢 |
| `run_parallel` | Spawn N MT5 instances on different configs (license caveat: typically 1 instance per install) | 🔴 |

## Tier F — Reporting / notification

| Tool | Description | Effort |
|------|-------------|--------|
| `slack_post_report` | POST backtest summary to a Slack webhook | 🟢 |
| `daily_summary` | One-line PnL / trade-count from today's journal | 🟢 |

## Tier G — Strategy intelligence (LLM-native)

| Tool | Description | Effort |
|------|-------------|--------|
| `extract_strategy_logic` | Source → plain-English entry/exit/risk summary | 🟡 |
| `find_inconsistencies` | E.g. SL > TP but comment claims "tight stop" | 🟡 |

## Tier H — Editor / IDE assists

| Tool | Description | Effort |
|------|-------------|--------|
| `goto_definition` | Resolve function name → `file:line` | 🟢 |
| `complete_signature` | Generate call template from definition | 🟢 |

---

## Already shipped (for reference)

- v0.1.0 — initial 7 tools (compile, run_backtest, tail_log, deploy_ea, list_experts, read_tester_report, env_info)
- v0.2.0 — refactor + auto-detect terminal hash + MT4 support + tester report parser
- v0.3.0 — full dev-loop expansion (18 new tools across analysis/lint/format/refactor/optimization/reports/snapshot)
- v0.4.0 — roadmap completion (select_terminal, smoke_test, extract_function, 3 MCP resources)
- v0.4.1 — redirect logs to `.mt5tmp/` workdir

---

## Out of scope

These belong in separate MCP servers, not here:

- Live trading (positions, orders, quotes) — pair with a runtime trading MCP
- Broker authentication / market data API access
- Linux/macOS native support — MetaTrader CLI is Windows-only; Wine works but not officially supported
- AST-based MQL parser (would need a real tree-sitter MQL grammar; current `extract_function` uses brace-counting)

---

## Contributing

If you want to take any of these on, open an issue first to align on shape before coding. Effort tags are estimates from the maintainer's perspective — actual time depends on your familiarity with MQL and MT5 internals.
