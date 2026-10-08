#!/usr/bin/env python3
"""
oi_rank.py  -  STANDALONE end-of-day "OI RISING" ranker (price ignored).
=====================================================================
WHY THIS EXISTS
  The dashboard's swing_engine.py kept not getting replaced on your machine
  (file-sync gremlin), so logic changes never showed up. This script is
  SELF-CONTAINED: it has its OWN ranking logic inside it and imports NOTHING
  from swing_engine.py, so a stale file can't break it. New filename, nothing
  old to conflict with.

WHAT IT DOES
  Pulls each F&O stock's DAILY futures candles, looks at the last few sessions
  of OPEN INTEREST, and ranks the stocks whose OI is on a steady RISING TREND
  - price completely ignored. "OI is climbing => something is being built."

THE RANKING (the thing you asked for)
  - Ranks on the TREND (least-squares slope of OI across the sessions), NOT the
    net change. So a steady climb ranks top, and a d1<d2<d3 run with a small
    d4<d3 dip STILL ranks high (it's still growing). A stock that just spiked
    once days ago and is now flat/falling gets a weak slope -> drops down.
  - Soft demotes only: a sustained recent turn-down, or OI now well off its
    high (distributing). Never kicked out for a single dip.

RUN (at day-end, after market close is perfectly fine - it uses finalised
daily candles):
      python oi_rank.py
      python oi_rank.py 5       # lookback = 5 sessions (default)
      python oi_rank.py 10 30   # lookback 10, show top 30

OUTPUT
  1. A ranked table in the console.
  2. oi_rank.html  - open it in a browser for a nicer table with an OI sparkline
     per stock. (A plain file you just double-click; no server needed.)
"""

import sys
import time
import datetime
from urllib.parse import quote

import requests

import config
import upstox_auth

try:
    import instruments as instruments_mod
except Exception as e:
    sys.exit(f"  Could not import instruments.py: {e}")

# ---- daily candle endpoints (same as the dashboard uses) ----
HIST_V3 = "https://api.upstox.com/v3/historical-candle/{ek}/days/1/{to}/{frm}"
HIST_V2 = "https://api.upstox.com/v2/historical-candle/{ek}/day/{to}/{frm}"
CAL_DAYS = 25          # calendar days requested to be sure we get ~12 sessions


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


ROLL_JUMP_PCT = 120.0   # a 1-day OI jump above this % = expiry contract rollover


def _roll_idx(ois):
    """Index of the most recent contract-rollover day (OI jumped > ROLL_JUMP_PCT),
    or -1. Lets us measure OI change from AFTER the rollover, not across it."""
    r = -1
    for i in range(1, len(ois)):
        p = ois[i - 1]
        if p > 0 and (ois[i] - p) / p * 100.0 > ROLL_JUMP_PCT:
            r = i
    return r


def _fmt_oi(v):
    v = abs(float(v))
    if v >= 1e7:
        return f"{v/1e7:.2f}Cr"
    if v >= 1e5:
        return f"{v/1e5:.2f}L"
    if v >= 1e3:
        return f"{v/1e3:.1f}k"
    return f"{v:.0f}"


# ---------------------------------------------------------------------
# resolve each stock's near-month FUTURES instrument key (self-contained)
# ---------------------------------------------------------------------
def resolve_fut_keys():
    inst_map = instruments_mod.resolve_universe(config)      # {SYMBOL: equity key}
    symbols = {s.upper().strip() for s in inst_map}
    data = instruments_mod._download_instruments()
    rows = {}   # SYMBOL -> (key, expiry_sort)
    for row in data:
        if row.get("segment") != "NSE_FO":
            continue
        if str(row.get("instrument_type", "")).upper() not in ("FUT", "FUTSTK"):
            continue
        u = (row.get("underlying_symbol") or row.get("asset_symbol") or "").upper().strip()
        if u not in symbols:
            continue
        key = row.get("instrument_key")
        if not key:
            continue
        exp = row.get("expiry")
        expv = exp if isinstance(exp, (int, float)) else 0
        if u not in rows or expv < rows[u][1]:
            rows[u] = (key, expv)
    return {u: v[0] for u, v in rows.items()}


# ---------------------------------------------------------------------
# fetch one stock's daily OI/price series (oldest -> newest)
# ---------------------------------------------------------------------
def fetch_daily(sess, key):
    ek = quote(key, safe="")
    today = datetime.date.today()
    frm = (today - datetime.timedelta(days=CAL_DAYS)).isoformat()
    to = today.isoformat()
    for url in (HIST_V3.format(ek=ek, to=to, frm=frm),
                HIST_V2.format(ek=ek, to=to, frm=frm)):
        try:
            r = sess.get(url, timeout=12)
            if r.status_code == 200:
                raw = r.json().get("data", {}).get("candles", []) or []
                if raw:
                    out = []
                    for c in reversed(raw):       # Upstox returns newest-first
                        if len(c) < 6:
                            continue
                        out.append({
                            "date": str(c[0])[:10],
                            "c": _f(c[4]),
                            "oi": _f(c[6]) if len(c) > 6 else 0.0,
                        })
                    return out
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------
# the ranking: rising OI TREND over the last `lookback` sessions
# ---------------------------------------------------------------------
def score_stock(daily, lookback):
    if not daily or len(daily) < lookback + 2:
        return None
    ois = [x["oi"] for x in daily]
    closes = [x["c"] for x in daily]
    if not any(o > 0 for o in ois):
        return None
    lb = min(lookback, len(ois) - 1)
    w = ois[-lb - 1:]                          # lb+1 OI points
    rr = _roll_idx(w)                          # drop across an expiry rollover
    if rr >= 0:
        w = w[rr:]
    if len(w) < 3 or not w[0]:
        return None
    N = len(w)
    oi_pct = (w[-1] - w[0]) / w[0] * 100.0

    # least-squares slope of OI across the window (tolerant to a single dip)
    mean_x = (N - 1) / 2.0
    mean_y = sum(w) / N
    num = sum((i - mean_x) * (w[i] - mean_y) for i in range(N))
    den = sum((i - mean_x) ** 2 for i in range(N)) or 1.0
    slope = num / den
    slope_pct = (slope / mean_y * 100.0) if mean_y else 0.0

    steps = [w[i] - w[i - 1] for i in range(1, N)]
    rising_days = sum(1 for s in steps if s > 0)
    frac = rising_days / (N - 1)
    wr = w[-3:]
    rec_slope = wr[-1] - wr[0]
    peak = max(w)
    off_peak = ((peak - w[-1]) / peak * 100.0) if peak else 0.0

    # must be a real, mostly-one-way climb (NOT cut on a single dip)
    if oi_pct <= 1.0 or slope_pct <= 0 or frac < 0.5:
        return None

    slope_mag = _clamp(slope_pct, 0, 5) / 5
    base = slope_mag * 0.55 + frac * 0.45
    rec_factor = 0.6 if rec_slope < 0 else 1.0
    peak_factor = _clamp(1.0 - off_peak / 12.0, 0.4, 1.0)
    score = round(_clamp(base * rec_factor * peak_factor * 100, 0, 100), 1)

    price_pct = ((closes[-1] - closes[-lb - 1]) / closes[-lb - 1] * 100.0) \
        if closes[-lb - 1] else 0.0

    return {
        "oi_pct": round(oi_pct, 1), "slope_pct": round(slope_pct, 2),
        "rising_days": rising_days, "lb": lb, "off_peak": round(off_peak, 1),
        "price_pct": round(price_pct, 1), "score": score,
        "oi_now": w[-1], "spark": [int(x) for x in ois[-(lb + 1):]],
        "still": "at high" if off_peak < 0.5 else f"{off_peak:.1f}% off",
    }


# ---------------------------------------------------------------------
# HTML output (a plain file you double-click - no server)
# ---------------------------------------------------------------------
def _sparkline(vals, w=120, h=28):
    if len(vals) < 2:
        return ""
    mn, mx = min(vals), max(vals)
    rg = (mx - mn) or 1
    pts = []
    for i, v in enumerate(vals):
        x = i / (len(vals) - 1) * w
        y = h - (v - mn) / rg * h
        pts.append(f"{x:.1f},{y:.1f}")
    rising = vals[-1] >= vals[0]
    col = "#2DD4BF" if rising else "#F04E6E"
    return (f'<svg width="{w}" height="{h}" style="vertical-align:middle">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" '
            f'stroke-width="1.6"/></svg>')


def write_html(ranked, lookback, path="oi_rank.html"):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for i, r in enumerate(ranked):
        pcol = "#2DD4BF" if r["slope_pct"] > 0 else "#F04E6E"
        rows.append(f"""<tr>
      <td style="text-align:right;color:#6a7486">{i+1}</td>
      <td style="font-weight:700;color:#E8EAF0">{r['sym']}</td>
      <td style="text-align:right;color:#2DD4BF;font-weight:700">+{r['oi_pct']}%</td>
      <td style="text-align:right;color:{pcol}">{r['slope_pct']:+.2f}%/d</td>
      <td style="text-align:center;color:#aab">{r['rising_days']}/{r['lb']}</td>
      <td style="text-align:center;color:#aab">{r['still']}</td>
      <td style="text-align:right;color:{'#2DD4BF' if r['price_pct']>=0 else '#F04E6E'}">{r['price_pct']:+.1f}%</td>
      <td style="text-align:center">{_sparkline(r['spark'])}</td>
      <td style="text-align:right;font-weight:800;color:#10D98C">{r['score']:.0f}</td>
    </tr>""")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>OI Rising {lookback}D</title>
<style>
 body{{background:#0b0e13;color:#c7cedb;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:26px}}
 h1{{font-size:18px;margin:0 0 4px}}
 .sub{{color:#6a7486;font-size:12px;margin-bottom:18px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:right;color:#6a7486;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:6px 10px;border-bottom:1px solid #1c222e}}
 th.l{{text-align:left}} th.c{{text-align:center}}
 td{{padding:8px 10px;border-bottom:1px solid #131822}}
 tr:hover td{{background:#10151e}}
</style></head><body>
<h1>OI Rising — {lookback}-session trend <span style="color:#6a7486;font-weight:400">(price ignored)</span></h1>
<div class="sub">{len(ranked)} stocks with rising futures-OI · generated {now} · ranked by OI trend slope</div>
<table>
 <tr><th>#</th><th class="l">Stock</th><th>OI Δ {lookback}D</th><th>Slope</th>
     <th class="c">Up days</th><th class="c">Vs high</th><th>Price</th><th class="c">OI trend</th><th>Score</th></tr>
 {''.join(rows)}
</table>
<div class="sub" style="margin-top:16px">Rising OI = positions being built. Now go to the chart and check where price is
vs support/resistance — that tells you which side (long vs short) the build is.</div>
</body></html>"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path
    except Exception as e:
        print(f"  (could not write {path}: {e})")
        return None


# ---------------------------------------------------------------------
def main():
    lookback = 5
    topn = 40
    if len(sys.argv) > 1:
        try:
            lookback = int(sys.argv[1])
        except Exception:
            pass
    if len(sys.argv) > 2:
        try:
            topn = int(sys.argv[2])
        except Exception:
            pass

    print("\n" + "=" * 64)
    print(f"  OI RISING  —  {lookback}-session trend ranking (price ignored)")
    print("  standalone ranker · self-contained · not affected by swing_engine.py")
    print("=" * 64)

    token = upstox_auth.load_token()
    if not token:
        sys.exit("\n  No access token. Run:  python upstox_auth.py\n")

    print("  Resolving F&O futures keys...", end=" ", flush=True)
    fut_keys = resolve_fut_keys()
    print(f"{len(fut_keys)} stocks.")
    if not fut_keys:
        sys.exit("  No futures keys resolved. Check config.UNIVERSE / instruments.py")

    sess = requests.Session()
    sess.headers.update({"Accept": "application/json",
                         "Authorization": f"Bearer {token}"})

    print(f"  Pulling daily OI for {len(fut_keys)} stocks (this takes ~30-60s)...")
    ranked = []
    done = 0
    for sym, key in fut_keys.items():
        done += 1
        if done % 25 == 0:
            print(f"    ...{done}/{len(fut_keys)}")
        try:
            daily = fetch_daily(sess, key)
            r = score_stock(daily, lookback)
            if r:
                r["sym"] = sym
                ranked.append(r)
        except Exception:
            pass
        time.sleep(0.05)       # gentle on the API

    ranked.sort(key=lambda x: (x["score"], x["oi_pct"]), reverse=True)

    # ---- console table ----
    print("\n" + "-" * 78)
    print(f"  {'#':>2}  {'STOCK':<13}{'OI Δ':>8}{'SLOPE':>10}  {'UP':>5}  {'VS HIGH':>9}{'PRICE':>8}{'SCORE':>7}")
    print("-" * 78)
    for i, r in enumerate(ranked[:topn]):
        print(f"  {i+1:>2}  {r['sym']:<13}{'+'+str(r['oi_pct'])+'%':>8}"
              f"{r['slope_pct']:>+8.2f}/d  {str(r['rising_days'])+'/'+str(r['lb']):>5}  "
              f"{r['still']:>9}{r['price_pct']:>+7.1f}%{r['score']:>7.0f}")
    print("-" * 78)
    print(f"  {len(ranked)} stocks with a rising OI trend (showing top {min(topn,len(ranked))}).")

    path = write_html(ranked[:topn], lookback)
    if path:
        print(f"\n  Also written: {path}  (double-click to open in a browser)\n")


if __name__ == "__main__":
    main()