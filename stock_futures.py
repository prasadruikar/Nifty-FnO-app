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

import datetime
import json
import re
import time
from pathlib import Path

import requests

QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
CACHE_FILE = Path("stock_futures_cache.json")
CACHE_MAX_AGE = 20 * 3600   # refresh once/day like instruments.py


def _norm(x):
    """Strip everything but A-Z0-9 and uppercase, so 'RELIANCE 24 OCT FUT',
    'RELIANCE24OCTFUT' and 'reliance-24oct-fut' all collapse to the same
    token for matching a quote back to its stock."""
    return re.sub(r'[^A-Z0-9]', '', str(x or "").upper())


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
        self.fut_tsyms = {}       # {SYMBOL: futures trading_symbol} - for matching quotes back
        self._dumped = False      # one-shot diagnostic guard
        self.prev = {}            # {SYMBOL: {price, oi, vol}} - last sample
        self.avg_vol_incr = {}    # {SYMBOL: rolling avg per-interval volume increment}
        self.avg_oi_incr = {}     # {SYMBOL: rolling avg per-interval |OI change|}
        self.day_open_oi = {}     # {SYMBOL: first OI seen TODAY} - for the day/session OI-change watermark
        self.day = datetime.date.today().isoformat()
        self.last_matched = 0     # diagnostics: how many symbols got a real quote last refresh()
        self.last_total = 0
        self._resolve_keys()

    # ---- resolve each stock's near-month futures key (once/day, cached) ----
    def _resolve_keys(self):
        if CACHE_FILE.exists():
            try:
                cached = json.loads(CACHE_FILE.read_text())
                # require the NEW cache shape (has "tmap") - an old cache without
                # trading symbols would leave matching blind, so re-resolve once.
                if time.time() - cached.get("_saved", 0) < CACHE_MAX_AGE and cached.get("tmap"):
                    m = cached.get("map", {})
                    t = cached.get("tmap", {})
                    if m:
                        self.fut_keys = {s: k for s, k in m.items() if s in self.symbols}
                        self.fut_tsyms = {s: t.get(s, "") for s in self.fut_keys}
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
        rows = {}   # SYMBOL -> (instrument_key, trading_symbol, expiry_sort_value)
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
            tsym = row.get("trading_symbol") or row.get("tradingsymbol") or row.get("name") or ""
            exp = row.get("expiry")
            expv = exp if isinstance(exp, (int, float)) else 0
            if u not in rows or expv < rows[u][2]:
                rows[u] = (key, tsym, expv)
        m = {u: v[0] for u, v in rows.items()}
        t = {u: v[1] for u, v in rows.items()}
        try:
            CACHE_FILE.write_text(json.dumps({"_saved": time.time(), "map": m, "tmap": t}))
        except Exception:
            pass
        self.fut_keys = m
        self.fut_tsyms = t
        print(f"{len(m)} stocks mapped.")

    # ---- one bulk quotes call across the whole universe's futures keys ----
    def refresh(self):
        if not self.fut_keys:
            return {}
        # new trading day -> reset the day-open OI baseline so the watermark
        # measures THIS session's buildup, not yesterday's
        today = datetime.date.today().isoformat()
        if today != self.day:
            self.day = today
            self.day_open_oi = {}
        keys = list(self.fut_keys.values())
        # BUG FIX: this used to match by checking whether the LAST '|'-segment
        # of our instrument_key (e.g. "NSE_FO|53001") appeared as a substring
        # of the response dict's key - but Upstox's bulk quotes response is
        # keyed by "EXCHANGE:TRADINGSYMBOL" (e.g. "NSE_FO:RELIANCE24OCTFUT"),
        # which never contains that numeric token. So `sym` was almost always
        # None, `out` stayed empty, and every stock's futures score silently
        # stayed at 0 forever - this is why "Futures build" never moved.
        # Upstox's bulk quotes response is keyed by "EXCHANGE:TRADINGSYMBOL"
        # (e.g. "NSE_FO:RELIANCE24OCTFUT"), NOT the "NSE_FO|53001" key we sent,
        # and the per-account exact shape (which field carries the original
        # key, whether it's the full key or a bare token) varies. So instead
        # of betting on one field we match through EVERY reliable handle:
        #   1. quote's own instrument_token == our full instrument_key
        #   2. the response dict key == our full instrument_key
        #   3. the bare token after '|' (some responses give just "53001")
        #   4. the trading symbol - from the quote's 'symbol' field OR the
        #      tail of the response key after ':' - normalised and matched to
        #      the futures trading symbol we cached at resolve time.
        # Whichever hits first wins. A one-shot dump prints the real shape if
        # NOTHING matches, so the exact field is never a guessing game again.
        key_to_sym = {k: s for s, k in self.fut_keys.items()}
        tail_to_sym = {k.split("|")[-1]: s for s, k in self.fut_keys.items()}
        tsym_to_sym = {_norm(t): s for s, t in self.fut_tsyms.items() if t}
        out = {}
        matched = 0
        for i in range(0, len(keys), 500):   # Upstox quotes endpoint caps at ~500 keys/call
            chunk = keys[i:i + 500]
            try:
                r = self.s.get(QUOTES_URL, params={"instrument_key": ",".join(chunk)}, timeout=15)
                if r.status_code != 200:
                    print(f"  [stock-fut] quotes HTTP {r.status_code}: {r.text[:150]}")
                    continue
                data = r.json().get("data", {}) or {}
            except Exception as e:
                print(f"  [stock-fut] quotes error: {e}")
                continue
            for k, q in data.items():
                tok = q.get("instrument_token")
                sym = (key_to_sym.get(tok)
                       or key_to_sym.get(k)
                       or tail_to_sym.get(str(tok))
                       or tsym_to_sym.get(_norm(q.get("symbol") or q.get("trading_symbol")))
                       or tsym_to_sym.get(_norm(k.split(":")[-1])))
                if not sym:
                    continue
                matched += 1
                price = _f(q.get("last_price"))
                oi = q.get("oi")
                vol = _f(q.get("volume"))
                out[sym] = self._score(sym, price, oi, vol)
        self.last_matched, self.last_total = matched, len(self.fut_keys)
        if matched == 0 and self.fut_keys:
            print(f"  [stock-fut] WARNING: 0 of {len(self.fut_keys)} futures keys matched a quote")
            # one-shot: print exactly what the response looked like so the
            # matching can be pinned precisely if none of the handles above hit
            if not self._dumped:
                self._dumped = True
                try:
                    sample_k = next(iter(data)) if data else None
                    sample_q = data.get(sample_k, {}) if sample_k else {}
                    print(f"  [stock-fut] DEBUG resp-key={sample_k!r} "
                          f"quote-fields={list(sample_q.keys())} "
                          f"instrument_token={sample_q.get('instrument_token')!r} "
                          f"symbol={sample_q.get('symbol')!r}")
                    ex_s = next(iter(self.fut_keys))
                    print(f"  [stock-fut] DEBUG our-key={self.fut_keys.get(ex_s)!r} "
                          f"our-tsym={self.fut_tsyms.get(ex_s)!r}")
                except Exception as _e:
                    print(f"  [stock-fut] DEBUG dump failed: {_e}")
        return out

    def _score(self, sym, price, oi, vol):
        prev = self.prev.get(sym)
        self.prev[sym] = {"price": price, "oi": oi, "vol": vol}

        # --- DAY / SESSION futures-OI change (for the row watermark) ---------
        # Lock the first OI reading of the day as the baseline, then every
        # refresh reports how far total futures OI has moved from it. This is
        # the CUMULATIVE session buildup (OI now vs 9:15 open), not the tiny
        # per-interval d_oi used for the live bias.
        day_oi_pct = 0.0
        day_oi_chg = 0
        if oi is not None:
            if sym not in self.day_open_oi and oi:
                self.day_open_oi[sym] = oi
            base = self.day_open_oi.get(sym)
            if base:
                day_oi_chg = int(oi - base)
                day_oi_pct = round((oi - base) / base * 100, 2)

        if not prev or not price or oi is None or prev.get("oi") is None:
            return {"bias": 0.0, "dir": "neutral", "note": "warming up",
                    "day_oi_pct": day_oi_pct, "day_oi_chg": day_oi_chg}

        d_price_pct = (price - prev["price"]) / prev["price"] * 100 if prev["price"] else 0.0
        price_dir = _sign(d_price_pct)
        d_oi = oi - prev["oi"]
        d_vol = max(0.0, vol - prev["vol"])   # volume only accumulates intraday

        # rolling average of THIS stock's own per-interval volume increment,
        # so a naturally high-volume stock isn't compared to a thin one
        avg = self.avg_vol_incr.get(sym, d_vol or 1.0)
        self.avg_vol_incr[sym] = avg + (d_vol - avg) * 0.2
        rel_vol = _clamp((d_vol / avg) if avg > 0 else 1.0, 0.4, 2.5)

        # rolling average of THIS stock's own per-interval |OI change|. THIS IS
        # THE FIX: the old score used only the SIGN of d_oi (buildup vs unwind)
        # and threw away HOW BIG the buildup was - so a massive fresh buildup
        # scored exactly the same as a one-lot nudge. Now the OI change is sized
        # against the stock's OWN normal churn, so a genuinely strong buildup
        # produces a genuinely stronger bias (which is what should rank first).
        a_oi = self.avg_oi_incr.get(sym, abs(d_oi) or 1.0)
        self.avg_oi_incr[sym] = a_oi + (abs(d_oi) - a_oi) * 0.2
        rel_oi = _clamp((abs(d_oi) / a_oi) if a_oi > 0 else 1.0, 0.4, 2.0)

        if price_dir == 0 or d_oi == 0:
            return {"bias": 0.0, "dir": "neutral", "note": "flat",
                    "rel_vol": round(rel_vol, 2),
                    "day_oi_pct": day_oi_pct, "day_oi_chg": day_oi_chg}

        is_fresh = d_oi > 0                     # OI increasing = buildup = fresh conviction
        fresh_w = 1.0 if is_fresh else 0.4       # buildup=strong, unwind/cover=weak
        # strength blends HOW BIG the OI change was and HOW HEAVY the volume was,
        # each vs this stock's own norm - so a big buildup on a volume spike
        # scores far higher than a marginal one on dead volume
        strength = 0.5 * rel_oi + 0.5 * rel_vol
        bias = price_dir * fresh_w * strength
        bias = _clamp(bias, -1.6, 1.6)

        label = ("Long Buildup" if is_fresh else "Short Covering") if price_dir > 0 \
            else ("Short Buildup" if is_fresh else "Long Unwinding")
        return {
            "bias": round(bias, 3),
            "dir": "bull" if bias > 0 else "bear" if bias < 0 else "neutral",
            "label": label,
            "rel_vol": round(rel_vol, 2),
            "rel_oi": round(rel_oi, 2),
            "d_oi": int(d_oi),
            "price_pct": round(d_price_pct, 2),
            "day_oi_pct": day_oi_pct,     # CUMULATIVE session OI change % (watermark)
            "day_oi_chg": day_oi_chg,     # CUMULATIVE session OI change (contracts)
        }