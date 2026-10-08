#!/usr/bin/env python3
"""
volclimax_backtest.py  -  First-hour VOLUME-CLIMAX breakout backtest.
=====================================================================
THE SETUP you asked for, per stock, per day:

  1. Look at the FIRST HOUR after open (09:15-10:15 by default).
  2. Is the first-hour VOLUME >= the MAX first-hour volume of the previous
     N days (default 10)?  -> a volume climax for THIS stock.
  3. SHORT side: if price FELL during that first hour (bearish hour),
     ENTRY = break of the first-hour LOW. Then: how often does price keep
     falling? (win rate).
  4. LONG side ("vice a versa"): if price ROSE in the first hour, ENTRY =
     break of the first-hour HIGH. How often does price keep rising?

Everything prints to the CONSOLE. Nothing is written to Excel/CSV.

----------------------------------------------------------------------
DATA NOTE (read this):
  Futures intraday history is SHORT. Upstox lists only currently-tradable
  contracts (~3 months), and expired-contract minute data usually isn't
  served, so INSTRUMENT="fut" realistically backtests only the last ~2-3
  months. For a LONG backtest (a year+), run the SAME logic on the cash
  series with INSTRUMENT="cash" (years of history, no expiry breaks; the
  first-hour cash volume tracks first-hour futures volume closely).
----------------------------------------------------------------------

RUN (needs your Upstox token from access_token.json, like the scanner):
    python volclimax_backtest.py                       # last ~45 days, futures, both sides
    python volclimax_backtest.py --from 2026-08-01 --to 2026-10-06
    python volclimax_backtest.py --instrument cash --from 2025-01-01 --to 2026-10-06
    python volclimax_backtest.py --side short --universe RELIANCE,TCS,INFY --verbose
    python volclimax_backtest.py --lookback 10 --first-hour-min 60 --interval 1

Outcome model (configurable): after the break, a trade is a WIN if price
travels TARGET% in your favour before it retraces STOP% against you; if
neither is hit by close, it's judged on the close vs the entry. Winrate =
wins / triggered. Setups whose level never broke are reported separately
(they are not wins and not losses - the trade simply never started).
"""

import argparse
import datetime as dt
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# --------------------------------------------------------- project bootstrap --
def bootstrap_root():
    """This script can live in a subfolder (e.g. Backtest\\). But instruments.py,
    config.py and access_token.json live in the MAIN project folder. Find that
    folder (the one holding instruments.py), add it to the import path, and cd
    into it so the token + instrument cache resolve exactly like the scanner."""
    here = os.path.dirname(os.path.abspath(__file__))
    tried = []
    d = here
    for _ in range(5):                       # walk up to 5 levels
        tried.append(d)
        if os.path.exists(os.path.join(d, "instruments.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            os.chdir(d)
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    sys.exit("  Could not find the project folder (the one with instruments.py).\n"
             "  Looked in:\n    " + "\n    ".join(tried) +
             "\n  Put this script inside the project, or next to instruments.py.")

# ------------------------------------------------------------------ CONFIG --
# Every one of these is overridable on the command line (see argparse below).
DEFAULTS = {
    "instrument":     "fut",   # "fut" (near-month futures) or "cash" (equity)
    "lookback":       5,      # previous N days whose MAX first-hour volume to beat
    "first_hour_min": 60,      # length of the "first hour" window, minutes
    "open_h":         9,
    "open_m":         15,      # session open (IST) -> 09:15
    "interval":       1,       # candle size in minutes (1 = precise break detection)
    "side":           "both",  # "short", "long", or "both"
    "target_pct":     3,    # favourable move that = a WIN (% from entry)
    "stop_pct":      1,    # adverse move that = a LOSS (% from entry)
    "require_oi":     False,   # futures only: require first-hour OI to RISE
                               # (fresh buildup) for the setup to qualify
    "break_mode":     "close", # "close" = a 1-min candle must CLOSE beyond the
                               #           level (confirmed break); "touch" = a wick
    "break_buf_pct":  0.00,    # how far BEYOND the level counts as a break (%)
    "min_fh_move":    0.00,    # require the first hour to move at least this % (abs)
    "days_back":      1,      # default range length if --from not given
    "workers":        8,       # parallel fetch threads (I/O bound -> big speedup)
    "sleep":          0.0,      # per-call delay; 0 is fine with threads. Raise if
                               # Upstox rate-limits (fails climbing) e.g. 0.05-0.1
    "verbose":        False,   # print every signal line, not just the summary
    "per_stock":      False,   # also print a per-stock winrate table
}

HIST_URL = "https://api.upstox.com/v3/historical-candle/{key}/minutes/{iv}/{to}/{frm}"


# ------------------------------------------------------------- small helpers --
def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _parse_date(s):
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def _expiry_to_date(exp):
    """Upstox 'expiry' is usually epoch-ms; be tolerant of a few shapes."""
    if exp is None:
        return None
    try:
        if isinstance(exp, (int, float)):
            v = float(exp)
            if v > 1e12:          # milliseconds
                v /= 1000.0
            return dt.datetime.fromtimestamp(v).date()
        return _parse_date(str(exp)[:10])
    except Exception:
        return None


# --------------------------------------------------------------- token load --
def load_token():
    try:
        import upstox_auth
        t = upstox_auth.load_token()
        if t:
            return t
    except Exception:
        pass
    # fallback: read access_token.json directly
    try:
        import json
        from pathlib import Path
        d = json.loads(Path("access_token.json").read_text())
        t = d.get("access_token")
        if t:
            return t
    except Exception:
        pass
    sys.exit("  No Upstox access token found. Run upstox_auth.py first "
             "(same token the scanner uses: access_token.json).")


# ------------------------------------------------- resolve instrument keys --
def resolve_keys(cfg, want_syms):
    """
    Returns:
      cash_keys: {SYM: instrument_key}                          (equity)
      fut_contracts: {SYM: [(expiry_date, instrument_key), ...]} (all listed FUT)
    `want_syms` is a set of UPPER symbols, or None for the whole F&O universe.
    """
    import instruments as I
    data = I._download_instruments()
    cash_keys = {}
    fut_contracts = defaultdict(list)
    fno_underlyings = set()

    for row in data:
        seg = row.get("segment", "")
        if seg == "NSE_FO":
            u = (row.get("underlying_symbol") or row.get("asset_symbol")
                 or (row.get("trading_symbol", "").split() or [""])[0]).upper().strip()
            if u:
                fno_underlyings.add(u)
            it = str(row.get("instrument_type", "")).upper()
            if it in ("FUT", "FUTSTK") and row.get("instrument_key"):
                ed = _expiry_to_date(row.get("expiry"))
                if ed:
                    fut_contracts[u].append((ed, row["instrument_key"]))

    for row in data:
        if row.get("segment") != "NSE_EQ":
            continue
        if row.get("instrument_type") not in (None, "EQ", "EQUITY", ""):
            continue
        ts = (row.get("trading_symbol") or "").upper().strip()
        key = row.get("instrument_key")
        if key and ts in fno_underlyings:
            cash_keys[ts] = key

    for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
        cash_keys.pop(idx, None)
        fut_contracts.pop(idx, None)

    for s in fut_contracts:
        fut_contracts[s].sort()   # by expiry ascending

    if want_syms:
        cash_keys = {s: k for s, k in cash_keys.items() if s in want_syms}
        fut_contracts = {s: v for s, v in fut_contracts.items() if s in want_syms}
    return cash_keys, dict(fut_contracts)


def active_fut_key(contracts, d):
    """Near-month contract live on date d = smallest expiry >= d."""
    for ed, key in contracts:        # sorted ascending
        if ed >= d:
            return key
    return None


# ------------------------------------------------------------- candle fetch --
class Fetcher:
    """Thread-safe candle fetcher. requests.Session isn't guaranteed
    thread-safe, so each worker thread gets its OWN session (thread-local);
    the call/fail counters are tiny and guarded by a lock."""
    def __init__(self, token, interval, sleep):
        self.token = token
        self.iv = interval
        self.sleep = sleep
        self._local = threading.local()
        self._lock = threading.Lock()
        self.calls = 0
        self.fails = 0

    def _sess(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"Accept": "application/json",
                              "Authorization": f"Bearer {self.token}"})
            self._local.s = s
        return s

    def day(self, key, d):
        """Chronological [(datetime, o,h,l,c,volume)] for one key on one date,
        or [] if nothing. Historical minute volume is PER-BAR (not cumulative)."""
        ds = d.isoformat()
        url = HIST_URL.format(key=key, iv=self.iv, to=ds, frm=ds)
        with self._lock:
            self.calls += 1
        try:
            if self.sleep:
                time.sleep(self.sleep)
            r = self._sess().get(url, timeout=20)
            if r.status_code != 200:
                with self._lock:
                    self.fails += 1
                return []
            cs = (r.json().get("data", {}) or {}).get("candles", []) or []
        except Exception:
            with self._lock:
                self.fails += 1
            return []
        out = []
        for c in cs:                     # [ts,o,h,l,c,volume,oi], newest-first
            try:
                t = dt.datetime.fromisoformat(c[0])
                oi = _f(c[6]) if len(c) > 6 else 0.0
                out.append((t, _f(c[1]), _f(c[2]), _f(c[3]), _f(c[4]), _f(c[5]), oi))
            except Exception:
                continue
        out.sort(key=lambda x: x[0])     # -> chronological
        return out


# --------------------------------------------------- first-hour statistics --
def first_hour(bars, open_h, open_m, minutes):
    """Split a day's bars into (first_hour_summary, rest_of_day_bars)."""
    if not bars:
        return None, []
    d = bars[0][0].date()
    start = dt.datetime.combine(d, dt.time(open_h, open_m), tzinfo=bars[0][0].tzinfo)
    end = start + dt.timedelta(minutes=minutes)
    fh = [b for b in bars if start <= b[0] < end]
    rest = [b for b in bars if b[0] >= end]
    if not fh:
        return None, rest
    vol = sum(b[5] for b in fh)
    hi = max(b[2] for b in fh)
    lo = min(b[3] for b in fh)
    op = fh[0][1]
    cl = fh[-1][4]
    oi_open = fh[0][6]
    oi_close = fh[-1][6]
    oi_chg_pct = ((oi_close - oi_open) / oi_open * 100.0) if oi_open else 0.0
    return {"vol": vol, "hi": hi, "lo": lo, "open": op, "close": cl,
            "move_pct": ((cl - op) / op * 100.0) if op else 0.0,
            "oi_open": oi_open, "oi_close": oi_close,
            "oi_chg_pct": oi_chg_pct, "oi_up": oi_close > oi_open}, rest


# ------------------------------------------------------- outcome of a break --
def simulate(side, entry, rest, target_pct, stop_pct, break_mode="close"):
    """
    Walk the rest-of-day bars after the first hour. Return:
      (triggered, win, exit_move_pct, mfe_pct, mae_pct, outcome)
    exit_move_pct is signed in the trade's FAVOUR (+ = good).
    outcome is one of "target", "stop", "eod", "none".

    INTRADAY ONLY - NO CARRY FORWARD. `rest` is just THIS day's bars after the
    first hour, so everything resolves within the same session. If neither
    target nor stop is hit, the trade is SQUARED OFF at the day's close (the
    "eod" outcome), never held overnight.

    break_mode decides what counts as the level BREAKING:
      "close" -> a 1-min candle must CLOSE beyond the level (confirmed break,
                 what you asked for) - a wick that pokes through and snaps back
                 does NOT count.
      "touch" -> any wick trading beyond the level triggers.

    Entry fill is taken at the level itself (entry). Conservative on same-bar
    ambiguity: if a bar hits both stop and target, the STOP is assumed first,
    so the winrate is never flattered.
    """
    if side == "short":
        target = entry * (1 - target_pct / 100.0)   # price down = good
        stop = entry * (1 + stop_pct / 100.0)       # price up   = bad
    else:
        target = entry * (1 + target_pct / 100.0)
        stop = entry * (1 - stop_pct / 100.0)

    triggered = False
    mfe = mae = 0.0
    for t, o, h, l, c, v, oi in rest:
        if not triggered:
            # has the level broken yet?
            if break_mode == "close":
                if side == "short" and c < entry:
                    triggered = True
                elif side == "long" and c > entry:
                    triggered = True
            else:  # touch
                if side == "short" and l <= entry:
                    triggered = True
                elif side == "long" and h >= entry:
                    triggered = True
            if not triggered:
                continue
        # trade is live from here
        if side == "short":
            fav = (entry - l) / entry * 100.0   # best drop so far
            adv = (h - entry) / entry * 100.0   # worst pop so far
            mfe = max(mfe, fav); mae = max(mae, adv)
            if h >= stop:
                return True, False, -stop_pct, mfe, mae, "stop"   # stopped (assume first)
            if l <= target:
                return True, True, target_pct, mfe, mae, "target"
        else:
            fav = (h - entry) / entry * 100.0
            adv = (entry - l) / entry * 100.0
            mfe = max(mfe, fav); mae = max(mae, adv)
            if l <= stop:
                return True, False, -stop_pct, mfe, mae, "stop"
            if h >= target:
                return True, True, target_pct, mfe, mae, "target"

    if not triggered:
        return False, False, 0.0, 0.0, 0.0, "none"
    # neither target nor stop hit -> SQUARE OFF at this day's close (intraday)
    last_close = rest[-1][4]
    if side == "short":
        move = (entry - last_close) / entry * 100.0
    else:
        move = (last_close - entry) / entry * 100.0
    return True, (move > 0), move, mfe, mae, "eod"


# ----------------------------------------------------------------- the run --
def run(cfg):
    root = bootstrap_root()
    print(f"  Project root: {root}")
    token = load_token()
    want = None
    if cfg["universe"] and cfg["universe"] != "all":
        want = {s.strip().upper() for s in cfg["universe"].split(",") if s.strip()}

    print("  Resolving instruments...", flush=True)
    cash_keys, fut_contracts = resolve_keys(cfg, want)
    universe = sorted(set(cash_keys) | set(fut_contracts))
    if want:
        universe = [s for s in universe if s in want]
    if not universe:
        sys.exit("  No matching stocks resolved.")

    d_to = cfg["to"] or dt.date.today()
    d_from = cfg["from"] or (d_to - dt.timedelta(days=cfg["days_back"]))
    # pull extra calendar days before d_from so the N-day lookback is covered
    pull_from = d_from - dt.timedelta(days=cfg["lookback"] * 2 + 10)

    all_days = []
    d = pull_from
    while d <= d_to:
        if d.weekday() < 5:                 # Mon-Fri (holidays just return empty)
            all_days.append(d)
        d += dt.timedelta(days=1)
    eval_days = [x for x in all_days if x >= d_from]

    print(f"  Instrument : {cfg['instrument'].upper()}")
    print(f"  Universe   : {len(universe)} stocks"
          + (f"  {universe}" if len(universe) <= 12 else ""))
    print(f"  Backtest   : {d_from} -> {d_to}   ({len(eval_days)} eval days, "
          f"+{len(all_days)-len(eval_days)} warm-up days)")
    print(f"  First hour : {cfg['open_h']:02d}:{cfg['open_m']:02d} for "
          f"{cfg['first_hour_min']}m   | candle {cfg['interval']}m")
    print(f"  Climax rule: first-hour vol >= MAX of previous {cfg['lookback']} days")
    print(f"  Break on   : {cfg['break_mode']}  "
          f"({'1-min candle CLOSE beyond the level' if cfg['break_mode']=='close' else 'any wick beyond the level'})")
    print(f"  Side(s)    : {cfg['side']}   | target {cfg['target_pct']}%  "
          f"stop {cfg['stop_pct']}%  break-buf {cfg['break_buf_pct']}%  "
          f"min-fh-move {cfg['min_fh_move']}%")
    if cfg["require_oi"]:
        print(f"  OI filter  : ON  (only setups where first-hour futures OI rose)"
              + ("   [ignored: cash has no OI]" if cfg["instrument"] == "cash" else ""))
    print(f"  (~{len(universe)*len(all_days)} candle calls; Ctrl-C to stop early)\n")

    fetch = Fetcher(token, cfg["interval"], cfg["sleep"])
    sides = ["short", "long"] if cfg["side"] == "both" else [cfg["side"]]

    # per side: count of setups, how many never broke, and a list of trade
    # records (one per TRIGGERED trade) carrying move%, R-multiple, MFE/MAE and
    # the first-hour OI direction - everything the report + OI split needs.
    agg = {s: {"setups": 0, "nobreak": 0, "trades": []} for s in sides}
    per_stock = defaultdict(lambda: {"setups": 0, "trig": 0, "win": 0})

    # ---- PHASE 1: fetch every (stock, day) candle IN PARALLEL ---------------
    # Network I/O is the whole cost, so a thread pool collapses it. Each task
    # resolves its own key (futures key is date-dependent) and returns bars.
    tasks = []
    for sym in universe:
        for d in all_days:
            if cfg["instrument"] == "cash":
                key = cash_keys.get(sym)
            else:
                key = active_fut_key(fut_contracts.get(sym, []), d)
            if key:
                tasks.append((sym, d, key))

    bars_by = {}   # (sym, day) -> bars
    done = 0
    total = len(tasks)
    workers = max(1, cfg["workers"])
    print(f"  Fetching {total} candle-days with {workers} threads...\n", flush=True)

    def _job(t):
        sym, d, key = t
        return (sym, d, fetch.day(key, d))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_job, t) for t in tasks]
        for f in as_completed(futs):
            sym, d, bars = f.result()
            if bars:
                bars_by[(sym, d)] = bars
            done += 1
            if done % 25 == 0 or done == total:
                print(f"    fetched {done}/{total}  (calls={fetch.calls}, "
                      f"fails={fetch.fails})", end="\r", flush=True)
    print()

    # ---- PHASE 2: analyse per stock, SINGLE-THREADED (deterministic) --------
    for si, sym in enumerate(universe, 1):
        fh_by_day = {}
        rest_by_day = {}
        for d in all_days:
            bars = bars_by.get((sym, d))
            if not bars:
                continue
            fh, rest = first_hour(bars, cfg["open_h"], cfg["open_m"], cfg["first_hour_min"])
            if fh:
                fh_by_day[d] = fh
                rest_by_day[d] = rest

        sdays = sorted(fh_by_day)
        for d in sdays:
            if d < d_from:
                continue
            # previous N trading days that actually have data
            prior = [p for p in sdays if p < d][-cfg["lookback"]:]
            if len(prior) < cfg["lookback"]:
                continue                      # not enough history yet
            max_prev_vol = max(fh_by_day[p]["vol"] for p in prior)
            fh = fh_by_day[d]
            if fh["vol"] < max_prev_vol or max_prev_vol <= 0:
                continue                      # no volume climax
            mv = fh["move_pct"]
            if abs(mv) < cfg["min_fh_move"]:
                continue

            # OI confirmation (the "OI taste"): first-hour OI RISING = fresh
            # buildup (short buildup on a down hour / long buildup on an up hour)
            # = real conviction; OI falling = covering/unwinding = weak. Only
            # meaningful for futures (cash candles carry no OI).
            oi_up = fh["oi_up"]
            if cfg["require_oi"] and cfg["instrument"] == "fut" and not oi_up:
                continue

            for side in sides:
                if side == "short" and mv >= 0:    # need a bearish first hour
                    continue
                if side == "long" and mv <= 0:     # need a bullish first hour
                    continue
                buf = cfg["break_buf_pct"] / 100.0
                entry = fh["lo"] * (1 - buf) if side == "short" else fh["hi"] * (1 + buf)
                trig, win, move, mfe, mae, outcome = simulate(
                    side, entry, rest_by_day[d], cfg["target_pct"], cfg["stop_pct"],
                    cfg["break_mode"])

                agg[side]["setups"] += 1
                per_stock[sym]["setups"] += 1
                if not trig:
                    agg[side]["nobreak"] += 1
                else:
                    R = (move / cfg["stop_pct"]) if cfg["stop_pct"] else 0.0
                    agg[side]["trades"].append({
                        "win": win, "move": move, "R": R,
                        "mfe": mfe, "mae": mae, "oi_up": oi_up,
                        "oi_chg": fh["oi_chg_pct"], "outcome": outcome})
                    per_stock[sym]["trig"] += 1
                    if win:
                        per_stock[sym]["win"] += 1
                if cfg["verbose"]:
                    st = "n/a " if not trig else ("WIN " if win else "loss")
                    oi_t = f" OI{fh['oi_chg_pct']:+5.1f}%" if cfg["instrument"] == "fut" else ""
                    print(f"  {d} {sym:<12} {side:<5} fhVol={fh['vol']:>10.0f} "
                          f">max10={max_prev_vol:>10.0f} fhMove={mv:+5.2f}%{oi_t} "
                          f"entry={entry:>9.2f} -> {st} {move:+5.2f}%")

    # ------------------------------------------------------------- report --
    def avg(xs):
        return (sum(xs) / len(xs)) if xs else 0.0

    def bar(pct, width=22, fill="█", empty="░"):
        f = int(round(max(0.0, min(100.0, pct)) / 100.0 * width))
        return fill * f + empty * (width - f)

    H = "─" * 68      # horizontal rule
    DASH = "—"        # em dash
    WARN = "⚠"        # warning sign
    rr = (cfg["target_pct"] / cfg["stop_pct"]) if cfg["stop_pct"] else 0.0
    print("\n\n" + "=" * 72)
    print(f"  RESULTS   target {cfg['target_pct']}%  /  stop {cfg['stop_pct']}%   "
          f"=  {rr:.1f}R reward:risk")
    print(f"  (winrate = wins / TRIGGERED trades.  R = move / stop, so a target "
          f"hit = +{rr:.1f}R, a stop = -1.0R)")
    print(f"  INTRADAY ONLY {DASH} if neither target nor stop is hit, squared off "
          f"at the day's close. No overnight carry.")
    print("=" * 72)

    for side in sides:
        a = agg[side]
        T = a["trades"]
        n = len(T)
        wins = [t for t in T if t["win"]]
        losses = [t for t in T if not t["win"]]
        nw, nl = len(wins), len(losses)
        wr = (nw / n * 100.0) if n else 0.0
        brokepct = (n / a["setups"] * 100.0) if a["setups"] else 0.0
        avg_win = avg([t["move"] for t in wins])
        avg_loss = avg([t["move"] for t in losses])
        exp_pct = avg([t["move"] for t in T])
        exp_R = avg([t["R"] for t in T])
        gw = sum(t["move"] for t in wins)
        gl = -sum(t["move"] for t in losses)
        pf = (gw / gl) if gl > 0 else float("inf")
        lvl = "LOW" if side == "short" else "HIGH"

        print(f"\n  {H}")
        print(f"  {side.upper()}  {DASH}  break of first-hour {lvl} after a volume climax")
        print(f"  {H}")
        print(f"    setups {a['setups']:>5}   broke {n:>5} ({brokepct:.0f}%)"
              f"   never broke {a['nobreak']:>5}")
        print()
        print(f"    WIN   {nw:>5}  {wr:5.1f}%  {bar(wr)}  avg {avg_win:+.2f}%")
        print(f"    LOSS  {nl:>5}  {100-wr:5.1f}%  {bar(100-wr)}  avg {avg_loss:+.2f}%")
        print()
        print(f"    EXPECTANCY  {exp_R:+.2f}R / trade   ({exp_pct:+.2f}% / trade)")
        print(f"    PROFIT FACTOR  {pf:4.2f}"
              + (" (gross win / gross loss)" if gl > 0 else ""))
        print(f"    avg MFE {avg([t['mfe'] for t in T]):.2f}%  "
              f"(best unrealised gain)   avg MAE {avg([t['mae'] for t in T]):.2f}%  "
              f"(worst drawdown)")
        # how each triggered trade actually ENDED (intraday) -----------------
        n_tgt = len([t for t in T if t["outcome"] == "target"])
        n_stp = len([t for t in T if t["outcome"] == "stop"])
        n_eod = [t for t in T if t["outcome"] == "eod"]
        eod_g = len([t for t in n_eod if t["move"] > 0])
        eod_r = len(n_eod) - eod_g
        if n:
            print(f"    exits:  target {n_tgt} ({n_tgt/n*100:.0f}%)   "
                  f"stop {n_stp} ({n_stp/n*100:.0f}%)   "
                  f"squared off at close {len(n_eod)} ({len(n_eod)/n*100:.0f}%) "
                  f"[green {eod_g} / red {eod_r}]")
        if n < 20:
            print(f"    {WARN}  under 20 trades {DASH} noise, not an edge")

        # ---- OI TASTE: split by first-hour OI direction (futures only) ------
        if cfg["instrument"] == "fut" and n:
            up = [t for t in T if t["oi_up"]]
            dn = [t for t in T if not t["oi_up"]]
            print(f"\n    OI taste (first-hour OI direction):")
            for lab, grp in (("OI UP   (fresh buildup)", up),
                             ("OI DOWN (cover/unwind)", dn)):
                if not grp:
                    print(f"      {lab:<24}  — none")
                    continue
                g = len(grp)
                g_wr = len([t for t in grp if t["win"]]) / g * 100.0
                g_R = avg([t["R"] for t in grp])
                flag = f"  {WARN} n<20" if g < 20 else ""
                print(f"      {lab:<24} n={g:>4}  win {g_wr:4.1f}%  "
                      f"expR {g_R:+.2f}  {bar(g_wr, 14)}{flag}")

    if cfg["per_stock"]:
        print(f"\n  {H}")
        print("  PER-STOCK  (triggered >= 3; best winrate first)")
        rows = []
        for s, p in per_stock.items():
            if p["trig"] >= 3:
                rows.append((p["win"] / p["trig"] * 100.0, s, p))
        for wr, s, p in sorted(rows, reverse=True):
            print(f"    {s:<12} setups {p['setups']:>3}  trig {p['trig']:>3}  "
                  f"win {p['win']:>3}  = {wr:5.1f}%  {bar(wr, 14)}")

    print(f"\n  (API calls {fetch.calls}, empty/failed {fetch.fails}. "
          f"A bucket under ~20 trades is noise, not an edge.)\n")


# ----------------------------------------------------------------- argparse --
def main():
    p = argparse.ArgumentParser(description="First-hour volume-climax breakout backtest")
    p.add_argument("--instrument", choices=["fut", "cash"], default=DEFAULTS["instrument"],
                   help="fut = near-month futures (short history ~3mo); "
                        "cash = equity (long history)")
    p.add_argument("--from", dest="from_", metavar="YYYY-MM-DD", default=None)
    p.add_argument("--to", dest="to", metavar="YYYY-MM-DD", default=None)
    p.add_argument("--days-back", type=int, default=DEFAULTS["days_back"],
                   help="range length if --from omitted")
    p.add_argument("--lookback", type=int, default=DEFAULTS["lookback"],
                   help="previous N days whose MAX first-hour volume to beat")
    p.add_argument("--first-hour-min", type=int, default=DEFAULTS["first_hour_min"])
    p.add_argument("--open-h", type=int, default=DEFAULTS["open_h"])
    p.add_argument("--open-m", type=int, default=DEFAULTS["open_m"])
    p.add_argument("--interval", type=int, default=DEFAULTS["interval"],
                   help="candle size in minutes (1 = precise break detection)")
    p.add_argument("--side", choices=["short", "long", "both"], default=DEFAULTS["side"])
    p.add_argument("--target", type=float, default=DEFAULTS["target_pct"],
                   help="favourable move %% that counts as a WIN")
    p.add_argument("--stop", type=float, default=DEFAULTS["stop_pct"],
                   help="adverse move %% that counts as a LOSS")
    p.add_argument("--require-oi", dest="require_oi", action="store_true",
                   default=DEFAULTS["require_oi"],
                   help="futures only: keep only setups where first-hour OI ROSE "
                        "(fresh buildup). Without it, both are shown split in the report.")
    p.add_argument("--break-on", dest="break_mode", choices=["close", "touch"],
                   default=DEFAULTS["break_mode"],
                   help="'close' = a 1-min candle must CLOSE beyond the first-hour "
                        "level (confirmed break); 'touch' = any wick triggers")
    p.add_argument("--break-buf", type=float, default=DEFAULTS["break_buf_pct"],
                   help="how far BEYOND the level counts as a break (%%)")
    p.add_argument("--min-fh-move", type=float, default=DEFAULTS["min_fh_move"],
                   help="require first hour to move at least this %% (abs)")
    p.add_argument("--universe", default="all",
                   help="'all' or comma list e.g. RELIANCE,TCS,INFY")
    p.add_argument("--workers", type=int, default=DEFAULTS["workers"],
                   help="parallel fetch threads (default 8; raise to ~16 if your "
                        "connection + Upstox limit allow, lower if you see fails)")
    p.add_argument("--sleep", type=float, default=DEFAULTS["sleep"],
                   help="per-call delay; 0 with threads. Raise (0.05-0.1) if rate-limited")
    p.add_argument("--verbose", action="store_true", default=DEFAULTS["verbose"])
    p.add_argument("--per-stock", action="store_true", default=DEFAULTS["per_stock"])
    a = p.parse_args()

    cfg = {
        "instrument": a.instrument,
        "from": _parse_date(a.from_) if a.from_ else None,
        "to": _parse_date(a.to) if a.to else None,
        "days_back": a.days_back,
        "lookback": a.lookback,
        "first_hour_min": a.first_hour_min,
        "open_h": a.open_h, "open_m": a.open_m,
        "interval": a.interval,
        "side": a.side,
        "target_pct": a.target, "stop_pct": a.stop,
        "require_oi": a.require_oi,
        "break_mode": a.break_mode,
        "break_buf_pct": a.break_buf, "min_fh_move": a.min_fh_move,
        "universe": a.universe, "workers": a.workers, "sleep": a.sleep,
        "verbose": a.verbose, "per_stock": a.per_stock,
    }
    try:
        run(cfg)
    except KeyboardInterrupt:
        print("\n  Stopped early by user.\n")


if __name__ == "__main__":
    main()