"""
nifty_backtest.py  -  Backtests the logged NIFTY features against what
ACTUALLY HAPPENED AFTERWARD, to answer the real question: "under what
conditions does NIFTY actually start moving?"

Reads every ./nifty_log/nifty_*.csv (written live by nifty_logger.py, wired
into orderflow_scanner.py's scan loop - nothing here talks to Upstox, it
only reads what you've already logged). The more trading days you run the
scanner with logging on, the more this has to work with.

RUN:
    python nifty_backtest.py
    python nifty_backtest.py --horizon 5 --min-move 0.15
    python nifty_backtest.py --min-move 0.10 --align-thresh 40

  --horizon N       how many candles ahead to look for the "did it move"
                     verdict (each candle = 3 min, so 5 = 15 min ahead).
                     Default 5.
  --min-move PCT    the % move (in NIFTY's favour) that counts as "it
                     actually moved", not just noise. Default 0.15%.
  --align-thresh N  how strong a layer's dial (-100..100) must be to count
                     as "confirming" for the alignment-count breakdown.
                     Default 50.

WHAT IT PRINTS (and saves to nifty_log/backtest_report.md):
  1. Overall forward-return distribution (sanity check on the data).
  2. Win rate + avg forward return, broken down BY SIGNAL (BUY CALL / BUY
     PUT / TRAP / WAIT) - does the fused signal actually predict movement?
  3. Win rate + avg forward return BY ALIGNMENT COUNT - when price is
     directional, how much does it help that OI and/or futures OI CONFIRM
     it (0, 1, or 2 of them aligned and strong)? This is the core answer to
     "what conditions precede a real move."
  4. A price-strength x OI-strength grid - which quadrant of the two dials
     actually moves, and by how much, with sample counts so you don't
     over-trust a cell with 3 rows in it.
  5. Whether the 7-lens bias verdict agreeing with price direction helps.

Every stat is printed WITH its sample count (n=...) - a bucket with under
~20 rows is noise, not an edge. Don't trade off a cell you haven't seen
survive at least a few dozen occurrences.
"""

import argparse
import csv
import glob
import statistics
from pathlib import Path

LOG_DIR = Path("nifty_log")


def load_rows():
    files = sorted(glob.glob(str(LOG_DIR / "nifty_*.csv")))
    rows = []
    for fp in files:
        with open(fp, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    return rows


def _f(r, k):
    v = r.get(k)
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def compute_forward_returns(rows, horizon):
    """Adds 'fwd_ret' to each row: % change from this candle's close to the
    close `horizon` candles later, WITHIN THE SAME DATE (never looks across
    a day boundary - a gap overnight isn't 'NIFTY moved')."""
    by_date = {}
    for r in rows:
        by_date.setdefault(r.get("date", ""), []).append(r)
    out = []
    for date, day_rows in by_date.items():
        day_rows.sort(key=lambda r: r.get("ts", ""))
        closes = [_f(r, "c") for r in day_rows]
        n = len(day_rows)
        for i, r in enumerate(day_rows):
            j = i + horizon
            c0 = closes[i]
            if j < n and c0:
                c1 = closes[j]
                r["fwd_ret"] = round((c1 - c0) / c0 * 100, 4) if c1 else None
            else:
                r["fwd_ret"] = None
            out.append(r)
    return out


def hit(direction, fwd_ret, min_move):
    if fwd_ret is None or direction not in ("bull", "bear"):
        return None
    if direction == "bull":
        return fwd_ret >= min_move
    return fwd_ret <= -min_move


def summarize(label, items, min_move):
    """items: list of (direction, fwd_ret) tuples."""
    valid = [(d, r) for d, r in items if r is not None and d in ("bull", "bear")]
    n = len(valid)
    if n == 0:
        return f"  {label:<32} n=0 (no data)"
    hits = [hit(d, r, min_move) for d, r in valid]
    hits = [h for h in hits if h is not None]
    win_rate = 100.0 * sum(hits) / len(hits) if hits else 0.0
    # "edge" return = forward return signed IN THE PREDICTED DIRECTION
    signed = [(r if d == "bull" else -r) for d, r in valid]
    avg = statistics.mean(signed)
    med = statistics.median(signed)
    return (f"  {label:<32} n={n:<5} win-rate={win_rate:5.1f}%   "
            f"avg edge={avg:+.3f}%   median={med:+.3f}%")


def bucket_strength(v, thresh):
    if v is None:
        return None
    if v >= thresh:
        return "bull"
    if v <= -thresh:
        return "bear"
    return "flat"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, default=5,
                     help="candles ahead to check for movement (default 5 = 15min)")
    ap.add_argument("--min-move", type=float, default=0.15,
                     help="%% move that counts as 'it actually moved' (default 0.15)")
    ap.add_argument("--align-thresh", type=float, default=50.0,
                     help="dial strength (0-100) needed to count as confirming (default 50)")
    args = ap.parse_args()

    if not LOG_DIR.exists() or not list(LOG_DIR.glob("nifty_*.csv")):
        print(f"\n  No logs found in ./{LOG_DIR}/ yet.")
        print("  Run orderflow_scanner.py during market hours for a while (it logs")
        print("  automatically), or run nifty_demo.py to generate synthetic log data")
        print("  to test this script's mechanics right now.\n")
        return

    rows = load_rows()
    rows = compute_forward_returns(rows, args.horizon)
    rows = [r for r in rows if r.get("fwd_ret") is not None]

    out_lines = []

    def emit(s=""):
        print(s)
        out_lines.append(s)

    emit("=" * 72)
    emit(f"  NIFTY BACKTEST  -  horizon={args.horizon} candles (~{args.horizon*3}min)  "
         f"min-move={args.min_move}%  align-thresh={args.align_thresh}")
    emit(f"  {len(rows)} candles with a valid forward return, from "
         f"{len(set(r.get('date') for r in rows))} day(s) of logs")
    emit("=" * 72)

    # ---- 1. overall distribution ----
    all_fwd = [_f(r, "fwd_ret") for r in rows]
    all_fwd = [v for v in all_fwd if v is not None]
    emit("\n1) OVERALL FORWARD-RETURN DISTRIBUTION (sanity check)")
    emit(f"   mean={statistics.mean(all_fwd):+.3f}%  median={statistics.median(all_fwd):+.3f}%  "
         f"stdev={statistics.pstdev(all_fwd):.3f}%")
    big_moves = sum(1 for v in all_fwd if abs(v) >= args.min_move)
    emit(f"   {big_moves}/{len(all_fwd)} candles ({100*big_moves/len(all_fwd):.1f}%) "
         f"moved >= {args.min_move}%% in EITHER direction within the horizon")

    # ---- 2. by fused signal ----
    emit("\n2) BY SIGNAL (does the fused BUY CALL/BUY PUT actually predict movement?)")
    for sig in ("BUY CALL", "BUY PUT", "TRAP", "WAIT"):
        items = []
        for r in rows:
            if r.get("signal") != sig:
                continue
            fwd = _f(r, "fwd_ret")
            if sig == "BUY CALL":
                d = "bull"
            elif sig == "BUY PUT":
                d = "bear"
            elif sig == "TRAP":
                # trap direction = the price side of the divergence
                d = "bull" if (r.get("price_dir") == "bull") else \
                    "bear" if (r.get("price_dir") == "bear") else None
            else:
                d = None  # WAIT has no predicted direction - just show base rate below
            items.append((d, fwd))
        if sig == "WAIT":
            vals = [_f(r, "fwd_ret") for r in rows if r.get("signal") == "WAIT" and _f(r, "fwd_ret") is not None]
            if vals:
                emit(f"  {'WAIT (no direction predicted)':<32} n={len(vals):<5} "
                     f"avg |fwd_ret|={statistics.mean([abs(v) for v in vals]):.3f}%  "
                     f"(baseline noise level)")
            else:
                emit(f"  {'WAIT':<32} n=0")
        else:
            emit(summarize(sig, items, args.min_move))
    emit("   NOTE: for TRAP, 'hit' means price reversed AWAY from where it was heading "
         "(i.e. the trap warning paid off) - so TRAP's win-rate here is really measuring "
         "'did the reversal risk materialize'.")

    # ---- 3. by alignment count (the core question) ----
    emit(f"\n3) BY ALIGNMENT COUNT (price directional & strong; how many of OI/futures CONFIRM it, "
         f"each needing |strength|>={args.align_thresh:g})")
    buckets = {0: [], 1: [], 2: []}
    for r in rows:
        p = _f(r, "price_strength")
        if p is None or abs(p) < args.align_thresh:
            continue  # price itself not directional enough - not a real setup
        direction = "bull" if p > 0 else "bear"
        o = _f(r, "oi_strength")
        fu = _f(r, "fut_strength")
        confirms = 0
        if o is not None and abs(o) >= args.align_thresh and (o > 0) == (p > 0):
            confirms += 1
        if fu is not None and abs(fu) >= args.align_thresh and (fu > 0) == (p > 0):
            confirms += 1
        buckets[confirms].append((direction, _f(r, "fwd_ret")))
    for k in (0, 1, 2):
        label = {0: "price alone (0 confirming)", 1: "price + 1 confirming (OI or fut)",
                  2: "price + BOTH confirming"}[k]
        emit(summarize(label, buckets[k], args.min_move))

    # ---- 4. price-strength x OI-strength grid ----
    emit(f"\n4) PRICE-STRENGTH x OI-STRENGTH GRID (avg forward return %, n=sample count)")
    emit(f"   rows=price bucket, cols=OI bucket   (bucket edges at +/-{args.align_thresh:g})")
    grid = {}
    for r in rows:
        pb = bucket_strength(_f(r, "price_strength"), args.align_thresh)
        ob = bucket_strength(_f(r, "oi_strength"), args.align_thresh)
        fwd = _f(r, "fwd_ret")
        if pb is None or ob is None or fwd is None:
            continue
        grid.setdefault((pb, ob), []).append(fwd)
    order = ["bear", "flat", "bull"]
    header = "         " + "".join(f"{c:>18}" for c in order)
    emit(header)
    for pb in order:
        cells = []
        for ob in order:
            vals = grid.get((pb, ob), [])
            if vals:
                cells.append(f"{statistics.mean(vals):+.3f}% (n={len(vals)})")
            else:
                cells.append("--")
        emit(f"  {pb:<6} " + "".join(f"{c:>18}" for c in cells))

    # ---- 5. bias verdict agreement ----
    emit(f"\n5) DOES THE 7-LENS BIAS VERDICT AGREEING WITH PRICE DIRECTION HELP?")
    agree_items, disagree_items = [], []
    for r in rows:
        p = _f(r, "price_strength")
        if p is None or abs(p) < args.align_thresh:
            continue
        direction = "bull" if p > 0 else "bear"
        bd = r.get("bias_direction")
        fwd = _f(r, "fwd_ret")
        if bd in ("up", "down"):
            bd_dir = "bull" if bd == "up" else "bear"
            (agree_items if bd_dir == direction else disagree_items).append((direction, fwd))
    emit(summarize("bias verdict AGREES with price", agree_items, args.min_move))
    emit(summarize("bias verdict DISAGREES with price", disagree_items, args.min_move))

    emit("\n" + "=" * 72)
    emit("  Read every 'n=' before trusting a row - a stat built on a handful of")
    emit("  candles is noise, not an edge. Re-run this after more trading days logged.")
    emit("=" * 72 + "\n")

    report_path = LOG_DIR / "backtest_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("```\n" + "\n".join(out_lines) + "\n```\n")
    print(f"  (also saved to {report_path})")


if __name__ == "__main__":
    main()