"""Parsers for MetaEditor compile log and Strategy Tester reports."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator


def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


_DIAG_RE = re.compile(r"^(?P<file>.*?)\((?P<line>\d+),(?P<col>\d+)\)\s*:\s*(?P<sev>error|warning)\s+(?P<code>\d+):\s*(?P<msg>.*)$")
_RESULT_RE = re.compile(r"Result:\s*(\d+)\s*errors?,\s*(\d+)\s*warnings?", re.IGNORECASE)


def parse_compile_log(text: str) -> dict:
    """Extract structured diagnostics + result summary from MetaEditor /log output."""
    errors: list[dict] = []
    warnings: list[dict] = []
    result_errors = result_warnings = None

    for line in text.splitlines():
        m = _DIAG_RE.match(line.strip())
        if m:
            d = {
                "file": m.group("file").strip(),
                "line": int(m.group("line")),
                "col": int(m.group("col")),
                "code": int(m.group("code")),
                "message": m.group("msg").strip(),
            }
            if m.group("sev") == "error":
                errors.append(d)
            else:
                warnings.append(d)
            continue
        rm = _RESULT_RE.search(line)
        if rm:
            result_errors = int(rm.group(1))
            result_warnings = int(rm.group(2))

    return {
        "errors": errors,
        "warnings": warnings,
        "result_errors": result_errors if result_errors is not None else len(errors),
        "result_warnings": result_warnings if result_warnings is not None else len(warnings),
        "ok": (result_errors == 0) if result_errors is not None else len(errors) == 0,
    }


class _ReportParser(HTMLParser):
    """Pull rows out of MT5 tester report HTML tables."""

    def __init__(self) -> None:
        super().__init__()
        self.in_td = False
        self.in_th = False
        self.row: list[str] = []
        self.rows: list[list[str]] = []
        self.cell_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        t = tag.lower()
        if t == "tr":
            self.row = []
        elif t in ("td", "th"):
            self.cell_buf = []
            if t == "td":
                self.in_td = True
            else:
                self.in_th = True

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t in ("td", "th"):
            self.row.append("".join(self.cell_buf).strip())
            self.in_td = self.in_th = False
        elif t == "tr":
            if self.row:
                self.rows.append(self.row)
            self.row = []

    def handle_data(self, data: str):
        if self.in_td or self.in_th:
            self.cell_buf.append(data)


_KV_KEYS = {
    "Total Net Profit": "net_profit",
    "Gross Profit": "gross_profit",
    "Gross Loss": "gross_loss",
    "Profit Factor": "profit_factor",
    "Expected Payoff": "expected_payoff",
    "Recovery Factor": "recovery_factor",
    "Sharpe Ratio": "sharpe_ratio",
    "Total Trades": "total_trades",
    "Short Trades (won %)": "short_trades_won_pct",
    "Long Trades (won %)": "long_trades_won_pct",
    "Profit Trades (% of total)": "profit_trades_pct",
    "Loss trades (% of total)": "loss_trades_pct",
    "Maximal Drawdown": "max_drawdown",
    "Balance Drawdown Maximal": "balance_drawdown_max",
    "Equity Drawdown Maximal": "equity_drawdown_max",
    "Initial Deposit": "initial_deposit",
    "Symbol": "symbol",
    "Period": "period",
    "Expert": "expert",
}


def parse_tester_report(html: str) -> dict:
    """Best-effort structured parse of MT5 tester report .htm.

    Returns dict with `summary` (key/value stats) and `trades` (list of rows when detected).
    """
    parser = _ReportParser()
    parser.feed(html)
    rows = parser.rows

    summary: dict = {}
    for row in rows:
        if len(row) >= 2 and row[0].rstrip(":").strip() in _KV_KEYS:
            key = _KV_KEYS[row[0].rstrip(":").strip()]
            summary[key] = row[1].strip() if len(row) > 1 else None
        elif len(row) >= 4:
            for i in range(0, len(row) - 1, 2):
                label = row[i].rstrip(":").strip()
                if label in _KV_KEYS:
                    summary[_KV_KEYS[label]] = row[i + 1].strip()

    trade_rows: list[dict] = []
    for row in rows:
        if len(row) >= 8:
            for cell in row[:4]:
                c = cell.lower().strip()
                if c.startswith("buy") or c.startswith("sell") or c in ("in", "out"):
                    trade_rows.append({"cols": row})
                    break

    return {
        "summary": summary,
        "trade_rows_detected": len(trade_rows),
        "trades_sample": trade_rows[:5],
    }


def iter_journal_lines(text: str) -> Iterator[dict]:
    """Parse MT5 journal lines: 'YYYY.MM.DD HH:MM:SS.mmm  Source\tMessage'."""
    pat_full = re.compile(r"^(?P<ts>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<src>[^\t]+?)\t(?P<msg>.*)$")
    pat_simple = re.compile(r"^(?P<ts>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<msg>.*)$")
    for line in text.splitlines():
        m = pat_full.match(line)
        if m:
            yield {"ts": m.group("ts"), "source": m.group("src").strip(), "message": m.group("msg")}
            continue
        m = pat_simple.match(line)
        if m:
            yield {"ts": m.group("ts"), "source": "", "message": m.group("msg")}
