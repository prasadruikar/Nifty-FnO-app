"""
stock_futures.py  -  Per-stock FUTURES buildup score (price + OI + volume).
=====================================================================
Same idea as nifty_signal.py's futures layer, extended with RELATIVE
VOLUME, applied across the whole F&O stock universe in ONE bulk Upstox
quotes call per refresh - NOT one call per stock. With ~208 F&O stocks,
one call per symbol per cycle would blow through rate limits; this
batches all of them the same way orderflow_scanner.py already batches
depth quotes.

THE LOGIC (same as NIFTY's futures layer):
  Futures OI can never independently disagree with price - a stock's
  future is just the tradable proxy for what real traders are doing.
  It only tells you whether the CURRENT price move is fresh conviction
  (OI increasing = buildup, strong) or people closing out (OI
  decreasing = covering/unwinding, weak):
      price up   + OI up    -> Long Buildup    (bullish, STRONG)
      price up   + OI down  -> Short Covering  (bullish, weak)
      price down + OI up    -> Short Buildup   (bearish, STRONG)
      price down + OI down  -> Long Unwinding  (bearish, weak)

  ON TOP of that (the new piece vs. NIFTY, since single-stock futures
  actually carry meaningful volume, unlike the index), RELATIVE VOLUME
  scales how much to trust the reading - a "buildup" on dead volume for
  THIS stock means much less than one on a volume spike for THIS stock.
  Each stock is compared to its OWN recent average (not a fixed
  threshold), same principle as levels.py's per-stock book-size norm.

OUTPUT per stock: {bias, dir, label, rel_vol, d_oi, price_pct}
  bias = signed composite score, roughly -1.6..+1.6, meant to be added
  into session_engine.py's confirmation alongside book_bias/oi_bias.
"""

import json
import time
from pathlib import Path

import requests

QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
CACHE_FILE = Path("stock_futures_cache.json")
CACHE_MAX_AGE = 20 * 3600   # refresh once/day like instruments.py


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class StockFutures:
    """Resolves each F&O stock's near-month futures instrument_key once
    (cached to disk, refreshed daily), then refreshes price/OI/volume for
    the WHOLE universe in one bulk quotes call per refresh()."""

    def __init__(self, access_token, symbols):
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })
        self.symbols = {s.upper().strip() for s in symbols}
        self.fut_keys = {}        # {SYMBOL: instrument_key}
        self.prev = {}            # {SYMBOL: {price, oi, vol}} - last sample
        self.avg_vol_incr = {}    # {SYMBOL: rolling avg per-interval volume increment}
        self._resolve_keys()

    # ---- resolve each stock's near-month futures key (once/day, cached) ----
    def _resolve_keys(self):
        if CACHE_FILE.exists():
            try:
                cached = json.loads(CACHE_FILE.read_text())
                if time.time() - cached.get("_saved", 0) < CACHE_MAX_AGE:
                    m = cached.get("map", {})
                    if m:
                        self.fut_keys = {s: k for s, k in m.items() if s in self.symbols}
                        print(f"  [stock-fut] loaded {len(self.fut_keys)} futures keys from cache")
                        return
            except Exception:
                pass
        print("  [stock-fut] resolving stock futures instrument keys...", end=" ", flush=True)
        try:
            import instruments as instruments_mod
            data = instruments_mod._download_instruments()
        except Exception as e:
            print(f"failed: {e}")
            return
        # pick each symbol's NEAREST-expiry FUT/FUTSTK row
        rows = {}   # SYMBOL -> (instrument_key, expiry_sort_value)
        for row in data:
            if row.get("segment") != "NSE_FO":
                continue
            if str(row.get("instrument_type", "")).upper() not in ("FUT", "FUTSTK"):
                continue
            u = (row.get("underlying_symbol") or row.get("asset_symbol") or "").upper().strip()
            if u not in self.symbols:
                continue
            key = row.get("instrument_key")
            if not key:
                continue
            exp = row.get("expiry")
            expv = exp if isinstance(exp, (int, float)) else 0
            if u not in rows or expv < rows[u][1]:
                rows[u] = (key, expv)
        m = {u: v[0] for u, v in rows.items()}
        try:
            CACHE_FILE.write_text(json.dumps({"_saved": time.time(), "map": m}))
        except Exception:
            pass
        self.fut_keys = m
        print(f"{len(m)} stocks mapped.")

    # ---- one bulk quotes call across the whole universe's futures keys ----
    def refresh(self):
        if not self.fut_keys:
            return {}
        keys = list(self.fut_keys.values())
        sym_by_key_tail = {k.split("|")[-1]: s for s, k in self.fut_keys.items()}
        out = {}
        for i in range(0, len(keys), 500):   # Upstox quotes endpoint caps at ~500 keys/call
            chunk = keys[i:i + 500]
            try:
                r = self.s.get(QUOTES_URL, params={"instrument_key": ",".join(chunk)}, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json().get("data", {}) or {}
            except Exception:
                continue
            for k, q in data.items():
                sym = None
                for tail, s in sym_by_key_tail.items():
                    if tail in k:
                        sym = s; break
                if not sym:
                    continue
                price = _f(q.get("last_price"))
                oi = q.get("oi")
                vol = _f(q.get("volume"))
                out[sym] = self._score(sym, price, oi, vol)
        return out

    def _score(self, sym, price, oi, vol):
        prev = self.prev.get(sym)
        self.prev[sym] = {"price": price, "oi": oi, "vol": vol}
        if not prev or not price or oi is None or prev.get("oi") is None:
            return {"bias": 0.0, "dir": "neutral", "note": "warming up"}

        d_price_pct = (price - prev["price"]) / prev["price"] * 100 if prev["price"] else 0.0
        price_dir = _sign(d_price_pct)
        d_oi = oi - prev["oi"]
        d_vol = max(0.0, vol - prev["vol"])   # volume only accumulates intraday

        # rolling average of THIS stock's own per-interval volume increment,
        # so a naturally high-volume stock isn't compared to a thin one
        avg = self.avg_vol_incr.get(sym, d_vol or 1.0)
        self.avg_vol_incr[sym] = avg + (d_vol - avg) * 0.2
        rel_vol = (d_vol / avg) if avg > 0 else 1.0
        rel_vol = _clamp(rel_vol, 0.4, 2.5)

        if price_dir == 0 or d_oi == 0:
            return {"bias": 0.0, "dir": "neutral", "note": "flat", "rel_vol": round(rel_vol, 2)}

        is_fresh = d_oi > 0                     # OI increasing = buildup = fresh conviction
        oi_weight = 1.0 if is_fresh else 0.4     # buildup=strong, unwind/cover=weak
        bias = price_dir * oi_weight * rel_vol
        bias = _clamp(bias, -1.6, 1.6)

        label = ("Long Buildup" if is_fresh else "Short Covering") if price_dir > 0 \
            else ("Short Buildup" if is_fresh else "Long Unwinding")
        return {
            "bias": round(bias, 3),
            "dir": "bull" if bias > 0 else "bear" if bias < 0 else "neutral",
            "label": label,
            "rel_vol": round(rel_vol, 2),
            "d_oi": int(d_oi),
            "price_pct": round(d_price_pct, 2),
        }