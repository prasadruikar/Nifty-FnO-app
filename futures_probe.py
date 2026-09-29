"""
futures_probe.py  -  STANDALONE test: fetch the NIFTY future reliably.
=====================================================================
Purpose: verify we can get the NIFTY near-month FUTURE price cleanly and
CONSISTENTLY, using the same instrument-master download that instruments.py
already uses successfully (NOT the flaky /option/contract endpoint that
nifty_data.py's old lookup relied on).

This script touches NOTHING in the live app. Run it on its own:
    python futures_probe.py

It will:
  1. Download Upstox's instrument master (same file instruments.py uses).
  2. Find every NIFTY INDEX future (FUT/FUTIDX, underlying = NIFTY, not
     BANKNIFTY/FINNIFTY/etc) and print them all, so we can SEE the exact
     rows Upstox gives us before trusting any one of them.
  3. Pick the nearest (current-month) expiry - that's the one that's
     actually liquid and worth tracking.
  4. Fetch spot + future LTP together, print the premium/discount.
  5. Repeat the fetch 5 times (10s apart) so we can see whether the
     premium reading is STABLE (small natural wiggle) or ERRATIC (a sign
     something's wrong with the key or the data).

Once you run this and confirm the output looks sane and consistent, we
wire the SAME lookup into nifty_data.py / nifty_bias.py for real.
"""

import sys
import time
import gzip
import io
import json
import datetime
from pathlib import Path

import requests

import upstox_auth

UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
NIFTY_KEY = "NSE_INDEX|Nifty 50"

FUT_CACHE = Path("futures_probe_cache.json")


# ---------------------------------------------------------------------
# 1. Instrument master (same download instruments.py already uses)
# ---------------------------------------------------------------------
def download_instruments():
    print("  [1] Downloading Upstox instrument master ...", end=" ", flush=True)
    r = requests.get(UPSTOX_INSTRUMENTS_URL, timeout=60)
    r.raise_for_status()
    with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as f:
        data = json.loads(f.read().decode("utf-8"))
    print(f"{len(data)} instruments.")
    return data


# ---------------------------------------------------------------------
# 2. Find every NIFTY INDEX future - print them ALL so nothing is hidden
# ---------------------------------------------------------------------
def find_nifty_futures(data):
    print("\n  [2] Scanning for NIFTY index futures (FUT / FUTIDX) ...")
    rows = []
    for row in data:
        seg = row.get("segment", "")
        itype = str(row.get("instrument_type", "")).upper()
        if seg != "NSE_FO" or itype not in ("FUT", "FUTIDX"):
            continue
        name = (row.get("name") or "").upper().strip()
        under = (row.get("underlying_symbol") or row.get("asset_symbol") or "").upper().strip()
        ts = (row.get("trading_symbol") or "").upper().strip()
        # STRICT match: exactly "NIFTY" as the underlying/name - never
        # BANKNIFTY, FINNIFTY, MIDCPNIFTY etc (those all CONTAIN "NIFTY").
        is_plain_nifty = (
            name == "NIFTY" or under == "NIFTY"
            or ts.startswith("NIFTY ") or ts.startswith("NIFTY-")
        ) and "BANK" not in name and "BANK" not in under and "BANK" not in ts \
          and "FIN" not in name and "FIN" not in under \
          and "MIDCP" not in name and "MIDCP" not in under \
          and "NEXT" not in name and "NEXT" not in under
        if not is_plain_nifty:
            continue
        rows.append({
            "instrument_key": row.get("instrument_key"),
            "trading_symbol": ts,
            "name": name,
            "underlying": under,
            "expiry": row.get("expiry"),
            "lot_size": row.get("lot_size"),
        })

    if not rows:
        print("      !! NO NIFTY futures found. Printing 5 sample NSE_FO rows so we")
        print("         can see the actual field names Upstox is giving us:")
        sample = [r for r in data if r.get("segment") == "NSE_FO"][:5]
        for s in sample:
            print("        ", json.dumps(s, indent=None))
        return []

    # sort by expiry ascending (nearest first). Expiry may be epoch-ms or ISO.
    def expiry_key(r):
        e = r["expiry"]
        if isinstance(e, (int, float)):
            return e
        try:
            return datetime.datetime.fromisoformat(str(e)).timestamp()
        except Exception:
            return 0
    rows.sort(key=expiry_key)

    print(f"      Found {len(rows)} NIFTY future row(s):")
    for r in rows:
        exp = r["expiry"]
        exp_disp = exp
        if isinstance(exp, (int, float)):
            try:
                exp_disp = datetime.datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d")
            except Exception:
                pass
        print(f"        {r['trading_symbol']:<20} key={r['instrument_key']:<28} "
              f"expiry={exp_disp} lot={r['lot_size']}")
    return rows


# ---------------------------------------------------------------------
# 3. Fetch spot + future LTP + FUTURES OI together (same call - the quote
#    response for an F&O instrument already carries "oi" directly, per
#    Upstox's full-quote field list - no extra endpoint needed).
# ---------------------------------------------------------------------
def fetch_spot_and_future(session, fut_key):
    r = session.get(QUOTES_URL, params={"instrument_key": f"{NIFTY_KEY},{fut_key}"}, timeout=10)
    if r.status_code != 200:
        print(f"      !! quotes HTTP {r.status_code}: {r.text[:150]}")
        return None, None, None
    data = r.json().get("data", {}) or {}
    spot = fut = fut_oi = None
    for k, q in data.items():
        ltp = q.get("last_price")
        if "Nifty 50" in k or k.endswith("Nifty 50"):
            spot = ltp
        else:
            fut = ltp
            fut_oi = q.get("oi")
    return spot, fut, fut_oi


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------
def main():
    token = upstox_auth.load_token()
    if not token:
        sys.exit("\n  No access token found. Run:  python upstox_auth.py\n")

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Authorization": f"Bearer {token}"})

    data = download_instruments()
    futs = find_nifty_futures(data)
    if not futs:
        sys.exit("\n  Could not find a NIFTY future row. See sample rows above and "
                  "send them back so we can fix the filter.\n")

    nearest = futs[0]
    fut_key = nearest["instrument_key"]
    print(f"\n  [3] Using nearest expiry: {nearest['trading_symbol']} ({fut_key})")

    print("\n  [4] Sampling spot + future + FUTURES OI, 5x 10s apart - checking CONSISTENCY ...")
    print(f"      {'time':<10} {'spot':>10} {'future':>10} {'premium(%)':>11} {'fut OI':>12} {'OI chg':>10}  read")
    samples = []
    oi_samples = []
    prev_oi = None
    for i in range(5):
        spot, fut, fut_oi = fetch_spot_and_future(session, fut_key)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if spot and fut:
            prem_pts = round(fut - spot, 2)
            prem_pct = round((fut - spot) / spot * 100, 3)
            read = ("bullish (premium)" if prem_pct > 0.03
                    else "bearish (discount)" if prem_pct < -0.03
                    else "flat / neutral")
            oi_disp = f"{fut_oi:,}" if fut_oi is not None else "MISSING"
            d_oi = (fut_oi - prev_oi) if (fut_oi is not None and prev_oi is not None) else None
            d_disp = f"{d_oi:+,}" if d_oi is not None else "-"
            print(f"      {ts:<10} {spot:>10.1f} {fut:>10.1f} {prem_pct:>10.3f}% {oi_disp:>12} {d_disp:>10}  {read}")
            samples.append(prem_pct)
            if fut_oi is not None:
                oi_samples.append(fut_oi)
                prev_oi = fut_oi
        else:
            print(f"      {ts:<10}  !! missing data (spot={spot}, future={fut})")
        if i < 4:
            time.sleep(10)

    print("\n  [5] Consistency check:")
    if oi_samples:
        print(f"      futures OI field present: YES ({len(oi_samples)}/5 samples had it)")
        print(f"      OI values seen: {oi_samples}")
        if len(set(oi_samples)) == 1:
            print("      -> OI unchanged across samples (expected if market is closed/quiet).")
        else:
            print("      -> OI is moving between samples - good, it's live data.")
    else:
        print("      !! futures OI field was MISSING/None on every sample - the quote response")
        print("         may not include 'oi' on your plan, or the future key/segment needs a")
        print("         different param. Send this output back before we wire it in.")

    if len(samples) >= 2:
        spread = max(samples) - min(samples)
        print(f"      premium%% samples: {samples}")
        print(f"      spread across 5 samples: {spread:.3f}%%")
        if spread < 0.05:
            print("      -> premium STABLE. Safe to wire into nifty_bias.py as a real lens.")
        else:
            print("      -> WIDE swings for ~40s of market time - investigate before "
                  "trusting this for scoring (could be a stale quote or wrong key).")
    else:
        print("      Not enough successful samples to judge consistency.")


if __name__ == "__main__":
    main()