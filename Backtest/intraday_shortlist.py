#!/usr/bin/env python3
"""
intraday_shortlist.py  -  TODAY's one-stock-a-day intraday candidates.
=====================================================================
The LIVE version of the volume-climax setup you backtested. Run it once the
first hour has closed (~10:15 IST) and it hands you a ranked shortlist of
stocks that qualify TODAY, so you pick one and trade it - instead of hunting
random names.

THE RULE (identical to volclimax_backtest.py, the one that showed an edge):
  1. First hour (09:15-10:15) volume >= MAX first-hour volume of the previous
     10 trading days  -> a volume climax for THIS stock.
  2. Liquidity floor: the first hour must have turned over real rupees
     (vol * price), so thin small-caps can't sneak in.
  3. Direction:
       first hour DOWN -> SHORT watch, entry = break of first-hour LOW
       first hour UP   -> LONG  watch, entry = break of first-hour HIGH
  4. OI direction (futures) is shown as confirmation (OI up = fresh buildup).

  The backtest said SHORT has the edge (+0.33R) and LONG was a coin flip, so
  longs are listed but flagged WEAK. Trust the data, not the label.

This file REUSES volclimax_backtest.py (keep both in the same folder).

RUN (after 10:15 IST, needs access_token.json like the scanner):
    python intraday_shortlist.py                 # today, futures, both sides
    python intraday_shortlist.py --side short     # only the setup with edge
    python intraday_shortlist.py --date 2026-10-06 --instrument cash   # replay a past day
    python intraday_shortlist.py --top 15 --min-turn 5
"""

import argparse
import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# reuse the backtested, proven helpers (same math, no duplication)
import futures_volume_batest as B

MIN_FH_TURN_CR = 5.0    # first-hour rupee turnover floor (crore) - liquidity gate
LOOKBACK = 5           # previous N days whose MAX first-hour volume to beat


def run(cfg):
    root = B.bootstrap_root()
    print(f"  Project root: {root}")
    token = B.load_token()

    want = None
    if cfg["universe"] and cfg["universe"] != "all":
        want = {s.strip().upper() for s in cfg["universe"].split(",") if s.strip()}

    print("  Resolving instruments...", flush=True)
    cash_keys, fut_contracts = B.resolve_keys(
        {"instrument": cfg["instrument"]}, want)
    universe = sorted(set(cash_keys) | set(fut_contracts))
    if want:
        universe = [s for s in universe if s in want]
    if not universe:
        sys.exit("  No matching stocks resolved.")

    today = cfg["date"] or dt.date.today()
    # weekdays from ~20 calendar days back up to and including `today`
    days = []
    d = today - dt.timedelta(days=LOOKBACK * 2 + 10)
    while d <= today:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)

    print(f"  Instrument : {cfg['instrument'].upper()}   Date: {today}")
    print(f"  Universe   : {len(universe)} stocks")
    print(f"  Rule       : first-hour vol >= max(prev {LOOKBACK}d)  +  "
          f"turnover >= Rs{cfg['min_turn']}cr")
    print(f"  Fetching first-hour data with {cfg['workers']} threads...\n", flush=True)

    fetch = B.Fetcher(token, cfg["interval"], cfg["sleep"])

    # ---- parallel fetch of every (stock, day) ------------------------------
    tasks = []
    for sym in universe:
        for dd in days:
            if cfg["instrument"] == "cash":
                key = cash_keys.get(sym)
            else:
                key = B.active_fut_key(fut_contracts.get(sym, []), dd)
            if key:
                tasks.append((sym, dd, key))

    bars_by = {}
    done, total = 0, len(tasks)

    def _job(t):
        sym, dd, key = t
        return (sym, dd, fetch.day(key, dd))

    with ThreadPoolExecutor(max_workers=max(1, cfg["workers"])) as ex:
        for f in as_completed([ex.submit(_job, t) for t in tasks]):
            sym, dd, bars = f.result()
            if bars:
                bars_by[(sym, dd)] = bars
            done += 1
            if done % 25 == 0 or done == total:
                print(f"    fetched {done}/{total} (fails={fetch.fails})",
                      end="\r", flush=True)
    print()

    # ---- build today's shortlist -------------------------------------------
    cands = []
    for sym in universe:
        fh_by_day = {}
        for dd in days:
            bars = bars_by.get((sym, dd))
            if not bars:
                continue
            fh, _rest = B.first_hour(bars, cfg["open_h"], cfg["open_m"],
                                     cfg["first_hour_min"])
            if fh:
                fh_by_day[dd] = fh

        if today not in fh_by_day:
            continue
        prior = [p for p in sorted(fh_by_day) if p < today][-LOOKBACK:]
        if len(prior) < LOOKBACK:
            continue
        max_prev = max(fh_by_day[p]["vol"] for p in prior)
        fh = fh_by_day[today]
        if max_prev <= 0 or fh["vol"] < max_prev:
            continue                                   # no volume climax
        turn_cr = fh["vol"] * fh["close"] / 1e7
        if turn_cr < cfg["min_turn"]:
            continue                                   # too illiquid
        mv = fh["move_pct"]
        side = "short" if mv < 0 else "long"
        if cfg["side"] != "both" and side != cfg["side"]:
            continue

        buf = cfg["break_buf_pct"] / 100.0
        if side == "short":
            entry = fh["lo"] * (1 - buf)
            stop = entry * (1 + cfg["stop_pct"] / 100.0)
            target = entry * (1 - cfg["target_pct"] / 100.0)
        else:
            entry = fh["hi"] * (1 + buf)
            stop = entry * (1 - cfg["stop_pct"] / 100.0)
            target = entry * (1 + cfg["target_pct"] / 100.0)

        cands.append({
            "sym": sym, "side": side, "mv": mv,
            "climax": fh["vol"] / max_prev,
            "turn": turn_cr, "oi": fh["oi_chg_pct"], "oi_up": fh["oi_up"],
            "ltp": fh["close"], "entry": entry, "stop": stop, "target": target,
        })

    # rank: edge side (short) first, then biggest volume climax
    cands.sort(key=lambda c: (0 if c["side"] == "short" else 1, -c["climax"]))

    # ---- print -------------------------------------------------------------
    if not cands:
        print("  No stock qualifies today (no first-hour volume climax that "
              "clears the liquidity floor). That's normal on a quiet day.\n")
        return

    print("\n" + "=" * 92)
    print(f"  TODAY'S INTRADAY SHORTLIST  {today}   "
          f"(first-hour volume climax, target {cfg['target_pct']}% / stop {cfg['stop_pct']}%)")
    print("=" * 92)
    hdr = (f"  {'#':>2} {'STOCK':<12}{'SIDE':<7}{'1hMOVE':>8}{'CLIMAX':>8}"
           f"{'TURN':>9}{'OIΔ':>8}{'ENTRY':>10}{'STOP':>10}{'TARGET':>10}")
    print(hdr)
    print("  " + "-" * 88)
    for i, c in enumerate(cands[:cfg["top"]], 1):
        oi_flag = "+" if c["oi_up"] else "-"
        weak = "  (weak edge)" if c["side"] == "long" else ""
        print(f"  {i:>2} {c['sym']:<12}{c['side'].upper():<7}{c['mv']:>+7.2f}%"
              f"{c['climax']:>7.1f}x{c['turn']:>7.0f}cr{c['oi']:>+6.1f}%{oi_flag}"
              f"{c['entry']:>10.2f}{c['stop']:>10.2f}{c['target']:>10.2f}{weak}")
    print("  " + "-" * 88)
    print(f"  {len(cands)} qualified. CLIMAX = first-hour volume vs its 10-day max. "
          f"OIΔ + = fresh buildup (confirming).")
    if cfg["instrument"] == "fut":
        print(f"  Tip: prefer SHORT + OIΔ positive (OI rising on a down hour = short "
              f"buildup). That's the combination with the edge.")
    print(f"  Entry = break of the first-hour {('low (short) / high (long)')}. "
          f"Square off by close - no overnight.\n")


def main():
    p = argparse.ArgumentParser(description="Today's intraday volume-climax shortlist")
    p.add_argument("--instrument", choices=["fut", "cash"], default="fut")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                   help="replay a past day (default: today)")
    p.add_argument("--side", choices=["short", "long", "both"], default="both")
    p.add_argument("--universe", default="all",
                   help="'all' or comma list e.g. RELIANCE,TCS")
    p.add_argument("--top", type=int, default=20, help="how many to print")
    p.add_argument("--min-turn", type=float, default=MIN_FH_TURN_CR,
                   dest="min_turn", help="first-hour turnover floor (crore)")
    p.add_argument("--first-hour-min", type=int, default=60)
    p.add_argument("--open-h", type=int, default=9)
    p.add_argument("--open-m", type=int, default=15)
    p.add_argument("--interval", type=int, default=5,
                   help="candle minutes (5 is plenty for first-hour stats, faster)")
    p.add_argument("--target", type=float, default=3.0)
    p.add_argument("--stop", type=float, default=1.0)
    p.add_argument("--break-buf", type=float, default=0.0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--sleep", type=float, default=0.0)
    a = p.parse_args()

    cfg = {
        "instrument": a.instrument,
        "date": B._parse_date(a.date) if a.date else None,
        "side": a.side, "universe": a.universe, "top": a.top,
        "min_turn": a.min_turn, "first_hour_min": a.first_hour_min,
        "open_h": a.open_h, "open_m": a.open_m, "interval": a.interval,
        "target_pct": a.target, "stop_pct": a.stop, "break_buf_pct": a.break_buf,
        "workers": a.workers, "sleep": a.sleep,
    }
    try:
        run(cfg)
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()