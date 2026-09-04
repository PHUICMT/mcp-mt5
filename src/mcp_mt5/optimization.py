"""Strategy Tester optimization: locate and parse `Tester/cache/*.opt` result caches.

The `.opt` layout follows `OptReader.mqh` published by MetaQuotes with the book
*MQL5 Programming for Traders* (https://www.mql5.com/en/book/automation/tester) and
independently re-implemented by fxsaber's TesterCache library. MQL5 structs are
byte-packed, so every struct below is read little-endian without padding.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

# --- TesterOptCacheHeader -------------------------------------------------------------
# uint version; ushort copyright[64]; ushort name[16]; int head_reserve[66];
# uint header_size; uint record_size; ushort expert_name[64]; ushort expert_path[128];
# ushort server[64]; ushort symbol[32]; ushort period; datetime date_from, date_to, date_forward;
# int opt_mode; int ticks_mode; int last_criterion; uint msc_min, msc_max, msc_avg;
# int common_reserve[16]; ushort group[80]; ushort trade_currency[32];
# int trade_deposit, trade_condition, trade_leverage, trade_hedging, trade_currency_digits, trade_pips;
# int trade_reserve[5]; char hash_ex5[16]; uint parameters_size, parameters_total;
# uint opt_params_size, opt_params_total; uint dwords_cnt; uint snapshot_size;
# uint passes_total; uint passes_passed;
_HEADER_FMT = (
    "<I" "128s" "32s" "66i" "II" "128s" "256s" "128s" "64s" "H" "qqq" "iii" "III" "16i"
    "160s" "64s" "iiiiii" "5i" "16s" "II" "II" "I" "I" "I" "I"
)
HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 1442
SIGNATURE = "TesterOptCache"

# --- TestCacheInput: ushort name[64]; int flag; int type; int digits; int offset; int size;
#     int unknown; union{long integers[3]; double numbers[3];} range
_INPUT_FMT = "<128s" "iiiiii" "24s"
INPUT_SIZE = struct.calcsize(_INPUT_FMT)  # 176
TYPE_OFFSET = 75  # ENUM_DATATYPE + 75
_DOUBLE_TYPES = {12, 13}  # TYPE_FLOAT, TYPE_DOUBLE

# --- ExpTradeSummary: ulong pass; 27 doubles; 14 ints
_SUMMARY_DOUBLES = [
    "deposit", "withdrawal", "profit", "grossprofit", "grossloss", "maxprofit", "minprofit",
    "conprofitmax", "maxconprofit", "conlossmax", "maxconloss", "balancemin", "maxdrawdown",
    "drawdownpercent", "reldrawdown", "reldrawdownpercent", "equitymin", "maxdrawdown_e",
    "drawdownpercent_e", "reldrawdown_e", "reldrawdownpercnt_e", "expected_payoff",
    "profit_factor", "recovery_factor", "sharpe_ratio", "margin_level", "custom_fitness",
]
_SUMMARY_INTS = [
    "deals", "trades", "profittrades", "losstrades", "shorttrades", "longtrades",
    "winshorttrades", "winlongtrades", "conprofitmax_trades", "maxconprofit_trades",
    "conlossmax_trades", "maxconloss_trades", "avgconwinners", "avgconloosers",
]
_SUMMARY_FMT = "<Q" + "d" * 27 + "i" * 14
SUMMARY_SIZE = struct.calcsize(_SUMMARY_FMT)  # 280
SYMBOLS_SIZE = SUMMARY_SIZE + 64            # + ushort watch[32]
MATH_SIZE = struct.calcsize("<Qd")          # 16

# Convenience aliases so callers can use the same criterion names as before.
_ALIASES = {"drawdown": "maxdrawdown", "pass_id": "pass"}


def _wstr(b: bytes) -> str:
    return b.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def _record_kind(header: dict) -> tuple[str, int, bool] | None:
    """Return (kind, base_size, has_appendix) for the record layout, or None if unknown."""
    size = header["record_size"] - header["opt_params_size"] - header["dwords_cnt"] * 4
    for kind, base in (("summary", SUMMARY_SIZE), ("summary_by_symbols", SYMBOLS_SIZE), ("math", MATH_SIZE)):
        if size == base:
            return kind, base, False
        if size == base + 8:
            return kind, base, True
    return None


def parse_opt_header(raw: bytes) -> dict | None:
    """Parse the TesterOptCache header; None if the signature is absent."""
    if len(raw) < HEADER_SIZE:
        return None
    fields = list(struct.unpack_from(_HEADER_FMT, raw, 0))
    pos = [0]

    def take(n: int = 1):
        chunk = fields[pos[0]:pos[0] + n]
        pos[0] += n
        return chunk[0] if n == 1 else chunk

    version = take()
    take()                      # copyright[64]
    name = _wstr(take())        # name[16] == "TesterOptCache"
    if name != SIGNATURE:
        return None
    take(66)                    # head_reserve
    header_size, record_size = take(), take()
    expert_name, expert_path, server, symbol = _wstr(take()), _wstr(take()), _wstr(take()), _wstr(take())
    period = take()
    date_from, date_to, date_forward = take(), take(), take()
    opt_mode, ticks_mode, last_criterion = take(), take(), take()
    take(3)                     # msc_min, msc_max, msc_avg
    take(16)                    # common_reserve
    group, trade_currency = _wstr(take()), _wstr(take())
    trade_deposit, _cond, trade_leverage, trade_hedging, _digits, _pips = take(6)
    take(5)                     # trade_reserve
    hash_ex5 = take().hex()
    parameters_size, parameters_total, opt_params_size, opt_params_total = take(4)
    dwords_cnt, snapshot_size, passes_total, passes_passed = take(4)
    return {
        "version": version,
        "header_size": header_size,
        "record_size": record_size,
        "expert_name": expert_name,
        "expert_path": expert_path,
        "server": server,
        "symbol": symbol,
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "date_forward": date_forward,
        "opt_mode": opt_mode,
        "ticks_mode": ticks_mode,
        "last_criterion": last_criterion,
        "group": group,
        "trade_currency": trade_currency,
        "trade_deposit": trade_deposit,
        "trade_leverage": trade_leverage,
        "trade_hedging": trade_hedging,
        "hash_ex5": hash_ex5,
        "parameters_size": parameters_size,
        "parameters_total": parameters_total,
        "opt_params_size": opt_params_size,
        "opt_params_total": opt_params_total,
        "dwords_cnt": dwords_cnt,
        "snapshot_size": snapshot_size,
        "passes_total": passes_total,
        "passes_passed": passes_passed,
    }


def _parse_inputs(raw: bytes, offset: int, count: int) -> list[dict]:
    inputs = []
    for k in range(count):
        name, flag, typ, digits, off, size, _unknown, rng = struct.unpack_from(_INPUT_FMT, raw, offset + k * INPUT_SIZE)
        dtype = typ - TYPE_OFFSET
        if dtype in _DOUBLE_TYPES:
            start, step, stop = struct.unpack("<3d", rng)
        else:
            start, step, stop = struct.unpack("<3q", rng)
        inputs.append({
            "name": _wstr(name), "optimized": bool(flag), "type": dtype, "digits": digits,
            "start": start, "step": step, "stop": stop,
        })
    return inputs


def _finite(v: float):
    # STAT_PROFIT_FACTOR is DBL_MAX when gross loss is zero; JSON has no Infinity.
    return None if (isinstance(v, float) and (v != v or abs(v) > 1e300)) else v


def parse_opt_file(path: str | Path, max_passes: int = 100_000) -> dict:
    """Parse an MT5 `.opt` optimisation cache.

    Returns header metadata, the input descriptors, and *all* passes (each with the
    full ENUM_STATISTICS mirror plus the optimised input values). Falls back to a
    header-only result with an explicit error when the layout is not recognised;
    it never guesses field offsets.
    """
    p = Path(path)
    if not p.exists():
        return {"error": f"not found: {p}"}
    raw = p.read_bytes()
    if len(raw) < 64:
        return {"error": "file too small to be a valid .opt", "size": len(raw)}

    header = parse_opt_header(raw)
    if header is None:
        return {
            "path": str(p),
            "size": len(raw),
            "error": "not a TesterOptCache file (signature missing); refusing to guess the layout",
        }

    kind = _record_kind(header)
    # Verified on a real cache: the input descriptors follow the fixed struct immediately (offset 1442);
    # `header_size` already covers struct + descriptors + the common-parameter buffer, and the pass
    # records start right after the snapshot ints that follow it.
    inputs = _parse_inputs(raw, HEADER_SIZE, header["parameters_total"])
    pos = max(header["header_size"], HEADER_SIZE + header["parameters_total"] * INPUT_SIZE + header["parameters_size"]) \
        + header["snapshot_size"] * 4
    result = {
        "path": str(p),
        "format": SIGNATURE,
        "header": header,
        "inputs": inputs,
        "optimized_inputs": [i["name"] for i in inputs if i["optimized"]],
        "pass_count": 0,
        "passes": [],
    }
    if kind is None:
        result["error"] = (
            f"unknown record layout: record_size={header['record_size']} "
            f"opt_params_size={header['opt_params_size']} dwords_cnt={header['dwords_cnt']}"
        )
        return result

    rkind, base, appendix = kind
    rec_size = header["record_size"]
    opt_inputs = [i for i in inputs if i["optimized"]]
    passes: list[dict] = []
    for _ in range(min(header["passes_passed"], max_passes)):
        if pos + rec_size > len(raw):
            result["warning"] = "file truncated before passes_passed records were read"
            break
        chunk = raw[pos:pos + rec_size]
        if rkind == "math":
            pass_id, fitness = struct.unpack_from("<Qd", chunk, 0)
            rec = {"pass": pass_id, "custom_fitness": fitness}
        else:
            vals = struct.unpack_from(_SUMMARY_FMT, chunk, 0)
            rec = {"pass": vals[0]}
            rec.update({k: _finite(v) for k, v in zip(_SUMMARY_DOUBLES, vals[1:28])})
            rec.update(dict(zip(_SUMMARY_INTS, vals[28:42])))
            if rkind == "summary_by_symbols":
                rec["symbol"] = _wstr(chunk[SUMMARY_SIZE:SUMMARY_SIZE + 64])
        for alias, real in _ALIASES.items():
            if real in rec:
                rec[alias] = rec[real]
        # Optimised input values follow the result struct (and optional 8-byte appendix), stride 8.
        off = base + (8 if appendix else 0)
        values = {}
        for k, inp in enumerate(opt_inputs):
            if off + (k + 1) * 8 > rec_size:
                break
            if inp["type"] in _DOUBLE_TYPES:
                values[inp["name"]] = struct.unpack_from("<d", chunk, off + k * 8)[0]
            else:
                values[inp["name"]] = struct.unpack_from("<q", chunk, off + k * 8)[0]
        rec["inputs"] = values
        passes.append(rec)
        pos += rec_size

    result["pass_count"] = len(passes)
    result["passes"] = passes
    return result


def top_passes(passes: list[dict], criterion: str = "profit", n: int = 10,
               descending: bool = True) -> list[dict]:
    """Sort optimization passes by criterion and return the top N (operates on the full list)."""
    if not passes:
        return []
    key = criterion if criterion in passes[0] else _ALIASES.get(criterion, criterion)
    if key not in passes[0]:
        return []
    return sorted(passes, key=lambda r: (r.get(key) is None, r.get(key) or 0), reverse=descending)[:n]


def find_latest_opt(tester_dir: str | Path, expert: Optional[str] = None,
                    symbol: Optional[str] = None, period: Optional[str] = None) -> Optional[str]:
    """Return the newest `.opt` under `tester_dir`, optionally restricted by the documented
    filename schema ``Expert.Symbol.Period.From.To.<Gen><Opt>.Hash.opt``.

    Filtering by expert/symbol/period makes selection deterministic instead of
    "whatever was modified last".
    """
    p = Path(tester_dir)
    if not p.exists():
        return None
    files = list(p.rglob("*.opt"))
    if expert or symbol or period:
        def matches(f: Path) -> bool:
            parts = f.name.split(".")
            if len(parts) < 3:
                return False
            ok = True
            if expert:
                ok &= parts[0].lower() == Path(expert).stem.lower()
            if symbol:
                ok &= len(parts) > 1 and parts[1].lower() == symbol.lower()
            if period:
                ok &= len(parts) > 2 and parts[2].lower() == period.lower()
            return ok
        files = [f for f in files if matches(f)]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None
