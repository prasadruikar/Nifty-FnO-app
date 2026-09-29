"""
instruments.py  -  Map F&O stock symbols to Upstox instrument keys.
=====================================================================
Upstox identifies instruments by keys like "NSE_EQ|INE002A01018", not by
ticker names. This module downloads Upstox's official instrument master
(a gzipped JSON they publish daily), finds the EQUITY instrument key for
each F&O stock, and caches the result to instruments_cache.json so we
don't re-download every run.

We derive the F&O universe from the instrument file itself: any NSE stock
that has derivatives (NSE_FO) is an F&O stock, and we map to its cash-market
(NSE_EQ) key so the depth we read is the underlying stock's order book.
"""

import gzip
import json
import io
import time
from pathlib import Path

import requests

# Upstox publishes the complete instrument master here (updated daily).
UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
CACHE_FILE = Path("instruments_cache.json")
CACHE_MAX_AGE = 20 * 3600   # refresh cache if older than ~20h (daily data)


def _download_instruments():
    r = requests.get(UPSTOX_INSTRUMENTS_URL, timeout=60)
    r.raise_for_status()
    with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as f:
        return json.loads(f.read().decode("utf-8"))


def build_fno_map(force=False):
    """
    Returns {SYMBOL: instrument_key} for every F&O underlying's EQUITY key.
    Uses cache unless it's stale or force=True.
    """
    # try cache
    if CACHE_FILE.exists() and not force:
        try:
            cached = json.loads(CACHE_FILE.read_text())
            if time.time() - cached.get("_saved", 0) < CACHE_MAX_AGE:
                m = cached.get("map", {})
                if m:
                    print(f"  Loaded {len(m)} F&O stocks from cache.")
                    return m
        except Exception:
            pass

    print("  Downloading Upstox instrument master (once per day)...", end=" ", flush=True)
    try:
        data = _download_instruments()
    except Exception as e:
        print(f"failed: {e}")
        # fall back to stale cache if we have one
        if CACHE_FILE.exists():
            print("  Using stale cache.")
            return json.loads(CACHE_FILE.read_text()).get("map", {})
        raise
    print(f"{len(data)} instruments.")

    # 1. Find all NSE stocks that have F&O (derivatives) -> the F&O universe.
    #    Upstox rows have segment like "NSE_FO" for derivatives; the
    #    underlying's trading symbol identifies the stock.
    fno_underlyings = set()
    for row in data:
        seg = row.get("segment", "")
        if seg == "NSE_FO":
            # futures/options rows carry an 'underlying_symbol' or 'asset_symbol'
            u = (row.get("underlying_symbol") or row.get("asset_symbol")
                 or row.get("trading_symbol", "").split()[0])
            if u:
                fno_underlyings.add(u.upper().strip())

    # 2. For each F&O underlying, find its NSE_EQ (cash) instrument key.
    eq_map = {}
    for row in data:
        if row.get("segment") != "NSE_EQ":
            continue
        if row.get("instrument_type") not in (None, "EQ", "EQUITY", ""):
            continue
        ts = (row.get("trading_symbol") or "").upper().strip()
        name = (row.get("name") or "").upper().strip()
        key = row.get("instrument_key")
        if not key:
            continue
        # match by trading symbol
        if ts in fno_underlyings:
            eq_map[ts] = key

    # 3. Skip index underlyings (we want stocks)
    for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
        eq_map.pop(idx, None)

    if not eq_map:
        raise SystemExit("  Could not map any F&O stocks - Upstox instrument format may have changed.")

    CACHE_FILE.write_text(json.dumps({"_saved": time.time(), "map": eq_map}))
    print(f"  Mapped {len(eq_map)} F&O stocks to instrument keys.")
    return eq_map


def resolve_universe(cfg):
    """Return {SYMBOL: key} for whatever config.UNIVERSE requests."""
    full = build_fno_map()
    uni = getattr(cfg, "UNIVERSE", "all_fno")
    if uni == "all_fno" or not uni:
        return full
    if isinstance(uni, (list, tuple)):
        want = {s.upper().strip() for s in uni}
        sub = {s: k for s, k in full.items() if s in want}
        missing = want - set(sub.keys())
        if missing:
            print(f"  Note: not found in F&O map: {', '.join(sorted(missing))}")
        return sub
    return full


if __name__ == "__main__":
    m = build_fno_map(force=True)
    print(f"\n  Total F&O stocks mapped: {len(m)}")
    for s in sorted(list(m.keys()))[:10]:
        print(f"    {s:14} -> {m[s]}")
    print("    ...")
