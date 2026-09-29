"""
diagnose.py  -  Check all the reported issues against live Upstox data.
=====================================================================
RUN (during market hours):  python diagnose.py

Checks each thing that looked broken and prints exactly what's happening,
so we fix the real cause instead of guessing:
  1. Is StockRanker writing conviction_bridge.json? How many stocks?
  2. NIFTY candles - does the intraday endpoint actually return data?
  3. NIFTY future key - can we find it? (needed for candles + futures lens)
  4. Day open - what does Upstox report as the real day open?
  5. Sample depth - what do the bid/ask numbers actually look like?
"""

import json
import datetime
from pathlib import Path

import upstox_auth
import config

try:
    import requests
except ImportError:
    raise SystemExit("pip install requests")


def main():
    token = upstox_auth.load_token()
    if not token:
        raise SystemExit("Run: python upstox_auth.py first")
    s = requests.Session()
    s.headers.update({"Accept": "application/json",
                      "Authorization": f"Bearer {token}"})

    print("\n" + "=" * 60)
    print("  DIAGNOSTIC - checking each reported issue")
    print("=" * 60)

    # ---- 1. conviction bridge ----
    print("\n[1] conviction_bridge.json (the OI engine's output)")
    bp = Path("conviction_bridge.json")
    if bp.exists():
        try:
            b = json.loads(bp.read_text())
            stocks = b.get("stocks", {})
            print(f"    exists, updated {b.get('updated')}, {len(stocks)} stocks with conviction")
            if stocks:
                sample = list(stocks.items())[:3]
                for sym, v in sample:
                    print(f"      {sym}: conv={v.get('conviction')} dir={v.get('direction')}")
            else:
                print("    *** EMPTY - StockRanker isn't finding conviction>=45, OR isn't running ***")
        except Exception as e:
            print(f"    error reading: {e}")
    else:
        print("    *** MISSING - StockRanker (nse_scanner.py) is not running ***")

    # ---- 2 & 3. NIFTY intraday candles + future key ----
    print("\n[2] NIFTY intraday candles")
    NIFTY = "NSE_INDEX|Nifty 50"
    from urllib.parse import quote
    url = f"https://api.upstox.com/v2/historical-candle/intraday/{quote(NIFTY, safe='')}/1minute"
    try:
        r = s.get(url, timeout=10)
        print(f"    index candle call: HTTP {r.status_code}")
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            print(f"    got {len(candles)} 1-min candles for NIFTY index")
            if candles:
                print(f"      newest: {candles[0]}")
        else:
            print(f"      body: {r.text[:200]}")
    except Exception as e:
        print(f"    error: {e}")

    print("\n[3] NIFTY future instrument key")
    try:
        r = s.get("https://api.upstox.com/v2/option/contract",
                  params={"instrument_key": NIFTY}, timeout=10)
        print(f"    option/contract call: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json().get("data", [])
            types = set(str(d.get("instrument_type", "")) for d in data)
            print(f"    instrument types returned: {types}")
            futs = [d for d in data if "FUT" in str(d.get("instrument_type", "")).upper()]
            print(f"    futures found: {len(futs)}")
            if futs:
                print(f"      e.g. {futs[0].get('instrument_key')} exp {futs[0].get('expiry')}")
            else:
                print("    *** no futures here - need a different source for the future key ***")
    except Exception as e:
        print(f"    error: {e}")

    # ---- 4. real day open (from quotes OHLC) ----
    print("\n[4] Real NIFTY day open (vs what the engine guesses)")
    try:
        r = s.get("https://api.upstox.com/v2/market-quote/quotes",
                  params={"instrument_key": NIFTY}, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", {})
            for k, q in data.items():
                ohlc = q.get("ohlc", {})
                print(f"    {k}: ltp={q.get('last_price')} open={ohlc.get('open')} "
                      f"close(prev)={ohlc.get('close')} high={ohlc.get('high')} low={ohlc.get('low')}")
    except Exception as e:
        print(f"    error: {e}")

    # ---- 5. sample stock depth ----
    print("\n[5] Sample stock day OHLC (for correct % change)")
    try:
        import instruments
        inst = instruments.resolve_universe(config)
        sample_keys = list(inst.values())[:3]
        r = s.get("https://api.upstox.com/v2/market-quote/quotes",
                  params={"instrument_key": ",".join(sample_keys)}, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", {})
            for k, q in data.items():
                ohlc = q.get("ohlc", {})
                ltp = q.get("last_price", 0)
                op = ohlc.get("open", 0)
                chg = round((ltp - op) / op * 100, 2) if op else 0
                print(f"    {k.split(':')[-1]}: ltp={ltp} open={op} -> {chg:+}% today")
    except Exception as e:
        print(f"    error: {e}")

    print("\n" + "=" * 60)
    print("  Paste this whole output back so we fix the real causes.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()