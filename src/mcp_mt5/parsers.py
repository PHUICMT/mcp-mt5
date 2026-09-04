"""Parsers for MetaEditor compile log, Strategy Tester reports and journals, plus encoding helpers."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def detect_encoding(raw: bytes) -> str:
    """Guess the text encoding of `raw`.

    MetaTrader writes many of its own files (logs, ini, reports, a large share of
    user `.mq5` sources) as UTF-16 LE, sometimes without a BOM. Returns one of
    ``utf-16-le``, ``utf-16-be``, ``utf-8-sig``, ``utf-8``.
    """
    if raw[:2] == b"\xff\xfe":
        return "utf-16-le"
    if raw[:2] == b"\xfe\xff":
        return "utf-16-be"
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    sample = raw[:4096]
    if len(sample) >= 4 and b"\x00" in sample:
        odd_nulls = sample[1::2].count(0)
        even_nulls = sample[0::2].count(0)
        half = len(sample) // 2 or 1
        if odd_nulls > half * 0.6 and even_nulls < half * 0.2:
            return "utf-16-le"
        if even_nulls > half * 0.6 and odd_nulls < half * 0.2:
            return "utf-16-be"
    return "utf-8"


def _decode(raw: bytes, encoding: str) -> str:
    if encoding in ("utf-16-le", "utf-16-be"):
        # Strip a BOM if present, then decode with the explicit byte order.
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            raw = raw[2:]
        return raw.decode(encoding, errors="replace")
    return raw.decode(encoding, errors="replace")


def read_text_auto(path: Path) -> str:
    """Read a text file whose encoding is UTF-8, UTF-8 with BOM, or UTF-16 (with or without BOM)."""
    raw = Path(path).read_bytes()
    return _decode(raw, detect_encoding(raw))


def file_encoding(path: Path) -> str:
    """Return the detected encoding of an existing file (``utf-8`` if it does not exist)."""
    p = Path(path)
    if not p.exists():
        return "utf-8"
    return detect_encoding(p.read_bytes())


def write_text_preserving(path: Path, text: str, default_encoding: str = "utf-8") -> str:
    """Write `text` to `path` using the encoding the file already has.

    UTF-16 files are written back as UTF-16 with a BOM (what MetaTrader expects);
    UTF-8-with-BOM keeps its BOM. Returns the encoding used.
    """
    p = Path(path)
    enc = file_encoding(p) if p.exists() else default_encoding
    if enc == "utf-16-le":
        p.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    elif enc == "utf-16-be":
        p.write_bytes(b"\xfe\xff" + text.encode("utf-16-be"))
    else:
        p.write_text(text, encoding=enc)
    return enc


# ---------------------------------------------------------------------------
# MetaEditor compile log
# ---------------------------------------------------------------------------

_DIAG_RE = re.compile(
    r"^(?P<file>.*?)\((?P<line>\d+),(?P<col>\d+)\)\s*:\s*(?P<sev>error|warning)\s+(?P<code>\d+):\s*(?P<msg>.*)$"
)
_RESULT_RE = re.compile(r"Result:\s*(\d+)\s*errors?,\s*(\d+)\s*warnings?", re.IGNORECASE)


def parse_compile_log(text: str) -> dict:
    """Extract structured diagnostics + result summary from MetaEditor /log output.

    `ok` is True only when a ``Result:`` line is present *and* reports zero errors.
    A log without a ``Result:`` line is the documented shape of MetaEditor's silent
    failure mode (no binary, no diagnostics), so it is never treated as success.
    """
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

    result_line_found = result_errors is not None
    return {
        "errors": errors,
        "warnings": warnings,
        "result_errors": result_errors if result_line_found else len(errors),
        "result_warnings": result_warnings if result_line_found else len(warnings),
        "result_line_found": result_line_found,
        "ok": result_line_found and result_errors == 0,
    }


# ---------------------------------------------------------------------------
# Strategy Tester HTML report
# ---------------------------------------------------------------------------

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


# Labels as printed by MT5 (title case) and MT4 (sentence case) reports, matched case-insensitively.
_KV_KEYS = {
    # identity
    "expert": "expert", "symbol": "symbol", "period": "period", "company": "company",
    "currency": "currency", "leverage": "leverage", "model": "model", "inputs": "inputs",
    # data quality
    "history quality": "history_quality", "modelling quality": "history_quality", "modeling quality": "history_quality",
    "bars": "bars", "bars in test": "bars", "ticks": "ticks", "ticks modelled": "ticks", "symbols": "symbols",
    # money
    "initial deposit": "initial_deposit", "withdrawal": "withdrawal",
    "total net profit": "net_profit", "gross profit": "gross_profit", "gross loss": "gross_loss",
    "profit factor": "profit_factor", "expected payoff": "expected_payoff", "recovery factor": "recovery_factor",
    "sharpe ratio": "sharpe_ratio", "ahpr": "ahpr", "ghpr": "ghpr", "lr correlation": "lr_correlation",
    "lr standard error": "lr_standard_error", "z-score": "z_score", "margin level": "margin_level",
    "ontester result": "ontester_result",
    # drawdown
    "maximal drawdown": "max_drawdown", "absolute drawdown": "absolute_drawdown", "relative drawdown": "relative_drawdown",
    "balance drawdown absolute": "balance_drawdown_abs", "balance drawdown maximal": "balance_drawdown_max",
    "balance drawdown relative": "balance_drawdown_rel", "equity drawdown absolute": "equity_drawdown_abs",
    "equity drawdown maximal": "equity_drawdown_max", "equity drawdown relative": "equity_drawdown_rel",
    # trades
    "total trades": "total_trades", "total deals": "total_deals",
    "short trades (won %)": "short_trades_won_pct", "short positions (won %)": "short_trades_won_pct",
    "long trades (won %)": "long_trades_won_pct", "long positions (won %)": "long_trades_won_pct",
    "profit trades (% of total)": "profit_trades_pct", "loss trades (% of total)": "loss_trades_pct",
    "largest profit trade": "largest_profit_trade", "largest loss trade": "largest_loss_trade",
    "average profit trade": "average_profit_trade", "average loss trade": "average_loss_trade",
    "maximum consecutive wins ($)": "max_consecutive_wins", "maximum consecutive losses ($)": "max_consecutive_losses",
    "maximal consecutive profit (count)": "max_consecutive_profit", "maximal consecutive loss (count)": "max_consecutive_loss",
    "average consecutive wins": "avg_consecutive_wins", "average consecutive losses": "avg_consecutive_losses",
    "minimal position holding time": "min_holding_time", "maximal position holding time": "max_holding_time",
    "average position holding time": "avg_holding_time",
}


def _norm_label(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.replace("\xa0", " ")).strip().rstrip(":").strip().lower()


def parse_tester_report(html: str, max_trades: int | None = None) -> dict:
    """Best-effort structured parse of an MT5 or MT4 tester report (.htm).

    Summary labels are matched case-insensitively against both MT5 ("Total Net Profit")
    and MT4 ("Total net profit") spellings, scanning every `label, value` cell pair in a
    row. The first value seen for a key wins, so the deals-table header row
    ("Symbol | Type | …") can never overwrite the real `Symbol:` entry above it.

    Returns `summary` (raw strings as printed, e.g. "10 000.00"), `trade_rows_detected`,
    a short `trades_sample`, and `trades` (all detected trade rows, or the first `max_trades`).
    """
    parser = _ReportParser()
    parser.feed(html)
    rows = parser.rows

    summary: dict = {}
    for row in rows:
        for i in range(len(row) - 1):
            key = _KV_KEYS.get(_norm_label(row[i]))
            if not key or key in summary:
                continue
            value = row[i + 1].strip()
            if not value or _norm_label(value) in _KV_KEYS:
                continue
            summary[key] = value

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
        "trades": trade_rows if max_trades is None else trade_rows[:max_trades],
    }


# ---------------------------------------------------------------------------
# Journals
# ---------------------------------------------------------------------------

_JOURNAL_TABBED = re.compile(
    r"^(?P<code>[A-Z0-9]{2})\t(?P<n>\d+)\t(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\t(?P<src>[^\t]*)\t(?P<msg>.*)$"
)
_JOURNAL_DATED_FULL = re.compile(
    r"^(?P<ts>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<src>[^\t]+?)\t(?P<msg>.*)$"
)
_JOURNAL_DATED_SIMPLE = re.compile(r"^(?P<ts>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<msg>.*)$")


def iter_journal_lines(text: str, date: str | None = None) -> Iterator[dict]:
    """Parse MetaTrader journal lines in either on-disk format.

    Current builds write `<2-char code>\t<int>\t<HH:MM:SS.mmm>\t<Source>\t<Message>` and keep the
    date only in the file name (`YYYYMMDD.log`); pass `date` to prefix the timestamps. Older
    exports use `YYYY.MM.DD HH:MM:SS.mmm  Source\tMessage`.
    """
    day = f"{date[:4]}.{date[4:6]}.{date[6:8]} " if date and len(date) == 8 and date.isdigit() else ""
    for line in text.splitlines():
        m = _JOURNAL_TABBED.match(line)
        if m:
            yield {"ts": day + m.group("time"), "source": m.group("src").strip(), "message": m.group("msg"),
                   "code": m.group("code")}
            continue
        m = _JOURNAL_DATED_FULL.match(line)
        if m:
            yield {"ts": m.group("ts"), "source": m.group("src").strip(), "message": m.group("msg")}
            continue
        m = _JOURNAL_DATED_SIMPLE.match(line)
        if m:
            yield {"ts": m.group("ts"), "source": "", "message": m.group("msg")}


_JOURNAL_NOTE_PATTERNS = {
    "start_time_changed": re.compile(r"start time changed to\s+(.+?)\s+to provide data", re.IGNORECASE),
    "history_begins": re.compile(r"history data begins from\s+(\S+)", re.IGNORECASE),
    "real_ticks_begin": re.compile(r"real ticks begin from\s+(\S+)", re.IGNORECASE),
    "tested_with_error": re.compile(r"tested with error", re.IGNORECASE),
    "log_file_written": re.compile(r'log file "([^"]+)" written', re.IGNORECASE),
    "history_quality": re.compile(r"history quality\s+(\d+(?:\.\d+)?)\s*%", re.IGNORECASE),
}


def parse_tester_journal_notes(text: str) -> dict:
    """Extract the tester journal lines that change how a report must be read.

    Most important: ``start time changed to …`` means the tester silently moved the
    requested `FromDate` forward because history was missing, so the report covers a
    different period than the ini asked for.
    """
    notes: dict = {}
    warnings: list[str] = []
    for line in text.splitlines():
        for key, pat in _JOURNAL_NOTE_PATTERNS.items():
            m = pat.search(line)
            if not m:
                continue
            value = m.group(1).strip() if m.groups() else True
            notes.setdefault(key, value)
    if "start_time_changed" in notes:
        warnings.append(
            f"tester moved the start date to {notes['start_time_changed']} because history was "
            "insufficient; the report does not cover the requested FromDate"
        )
    if notes.get("tested_with_error"):
        warnings.append("tester journal reports 'tested with error'")
    return {"notes": notes, "warnings": warnings}
