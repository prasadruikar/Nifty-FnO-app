"""
nifty_data.py  -  Fetch all NIFTY-direction data from Upstox.
=====================================================================
Pulls everything the multi-lens NIFTY engine needs, all from Upstox:
  - NIFTY 50 spot + day OHLC   (NSE_INDEX|Nifty 50)
  - India VIX + prev close     (NSE_INDEX|India VIX)
  - NIFTY near-month FUTURE ltp (from instrument master, NSE_FO)
  - NIFTY option chain -> PCR + max pain

One quotes call covers spot + VIX + future; the option chain is one more call.
VWAP is taken from the quote's average price when present. Everything is
wrapped so a missing piece just returns 0 (that lens votes neutral).
"""

import datetime
import gzip
import io
import json
import requests
from pathlib import Path

QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"
INTRADAY_URL = "https://api.upstox.com/v2/historical-candle/intraday"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY   = "NSE_INDEX|India VIX"

_FUT_CACHE_FILE = Path("nifty_futures_cache.json")
_OPT_EXPIRY_CACHE_FILE = Path("nifty_option_expiry_cache.json")


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


class NiftyData:
    def __init__(self, access_token):
        self.token = access_token
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })
        self._vix_prev = 0.0          # remembered across scans
        self._nifty_prev = 0.0
        self._fut_key = None
        self._fut_key_day = None
        self._opt_expiry = None
        self._opt_expiry_day = None
        self._instruments_mem = None   # in-process cache: avoid downloading the
                                        # (large) instrument master twice on a
                                        # cold start (once for futures, once for
                                        # the option expiry lookup below)

    def _download_instruments(self):
        """Shared instrument-master download, memoized for the process's
        lifetime (both the futures-key lookup and the option-expiry lookup
        below need this same big file - fetch it once, not twice)."""
        if self._instruments_mem is not None:
            return self._instruments_mem
        r = requests.get(INSTRUMENTS_URL, timeout=60)
        r.raise_for_status()
        with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as f:
            data = json.loads(f.read().decode("utf-8"))
        self._instruments_mem = data
        return data

    # -- find NIFTY's nearest OPTION expiry (once per day) ------------------
    # BUG FIX: the option-chain endpoint SILENTLY returns nothing without a
    # valid expiry_date param (it's required, not optional - confirmed
    # against Upstox's docs). The old code called it with instrument_key
    # only, so pcr/max_pain/call_oi/put_oi were stuck at their defaults all
    # day, every day - the "OI not working for NIFTY bias" bug. This uses
    # the SAME reliable instrument-master file as the futures lookup (not
    # the separate /option/contract endpoint, which earlier testing found
    # flaky) to find NIFTY's real expiry dates and pick the nearest one.
    def _nifty_option_expiry(self):
        today = datetime.date.today()
        today_s = today.isoformat()
        if self._opt_expiry and self._opt_expiry_day == today_s:
            return self._opt_expiry

        if _OPT_EXPIRY_CACHE_FILE.exists():
            try:
                cached = json.loads(_OPT_EXPIRY_CACHE_FILE.read_text())
                if cached.get("day") == today_s and cached.get("expiry"):
                    self._opt_expiry = cached["expiry"]
                    self._opt_expiry_day = today_s
                    return self._opt_expiry
            except Exception:
                pass

        try:
            data = self._download_instruments()
        except Exception as e:
            print(f"  [nifty] option-expiry instrument download failed: {e}")
            self._opt_expiry_day = today_s
            return None

        expiries = set()
        for row in data:
            if row.get("segment") != "NSE_FO":
                continue
            if str(row.get("instrument_type", "")).upper() not in ("CE", "PE"):
                continue
            name = (row.get("name") or "").upper().strip()
            under = (row.get("underlying_symbol") or row.get("asset_symbol") or "").upper().strip()
            ts = (row.get("trading_symbol") or "").upper().strip()
            is_plain = ((name == "NIFTY" or under == "NIFTY" or ts.startswith("NIFTY"))
                        and "BANK" not in ts and "FIN" not in ts
                        and "MIDCP" not in ts and "NEXT" not in ts)
            if not is_plain:
                continue
            e = row.get("expiry")
            d = None
            if isinstance(e, (int, float)):
                try:
                    d = datetime.datetime.fromtimestamp(e / 1000).date()
                except Exception:
                    d = None
            else:
                try:
                    d = datetime.datetime.fromisoformat(str(e)).date()
                except Exception:
                    d = None
            if d and d >= today:
                expiries.add(d)

        if not expiries:
            print("  [nifty] no NIFTY option expiry found in instrument master")
            self._opt_expiry_day = today_s
            return None

        nearest = min(expiries)
        self._opt_expiry = nearest.isoformat()
        self._opt_expiry_day = today_s
        try:
            _OPT_EXPIRY_CACHE_FILE.write_text(json.dumps({"day": today_s, "expiry": self._opt_expiry}))
        except Exception:
            pass
        return self._opt_expiry

    # -- find NIFTY near-month future instrument key (once per day) --------
    # PROVEN via futures_probe.py: /option/contract was unreliable (often
    # returned nothing). This uses the instrument-master download instead -
    # the SAME file instruments.py already uses successfully every day for
    # the F&O stock universe - so it doesn't depend on a flaky endpoint.
    def _nifty_future_key(self):
        today = datetime.date.today().isoformat()
        if self._fut_key and self._fut_key_day == today:
            return self._fut_key

        # try disk cache first (refreshed once/day - the master is huge)
        if _FUT_CACHE_FILE.exists():
            try:
                cached = json.loads(_FUT_CACHE_FILE.read_text())
                if cached.get("day") == today and cached.get("key"):
                    self._fut_key = cached["key"]
                    self._fut_key_day = today
                    return self._fut_key
            except Exception:
                pass

        try:
            data = self._download_instruments()
        except Exception as e:
            print(f"  [nifty] futures instrument download failed: {e}")
            self._fut_key = None
            self._fut_key_day = today
            return None

        rows = []
        for row in data:
            if row.get("segment") != "NSE_FO":
                continue
            if str(row.get("instrument_type", "")).upper() not in ("FUT", "FUTIDX"):
                continue
            name = (row.get("name") or "").upper().strip()
            under = (row.get("underlying_symbol") or row.get("asset_symbol") or "").upper().strip()
            ts = (row.get("trading_symbol") or "").upper().strip()
            # STRICT: plain NIFTY only - exclude BANKNIFTY/FINNIFTY/
            # NIFTYNXT50/MIDCPNIFTY, which all also contain "NIFTY".
            is_plain = ((name == "NIFTY" or under == "NIFTY" or
                        ts.startswith("NIFTY ") or ts.startswith("NIFTY-"))
                        and "BANK" not in ts and "FIN" not in ts
                        and "MIDCP" not in ts and "NEXT" not in ts)
            if is_plain and row.get("instrument_key"):
                rows.append(row)

        if not rows:
            print("  [nifty] no NIFTY future found in instrument master")
            self._fut_key = None
            self._fut_key_day = today
            return None

        def expiry_key(r):
            e = r.get("expiry")
            if isinstance(e, (int, float)):
                return e
            try:
                return datetime.datetime.fromisoformat(str(e)).timestamp()
            except Exception:
                return 0
        rows.sort(key=expiry_key)
        self._fut_key = rows[0]["instrument_key"]
        self._fut_key_day = today
        try:
            _FUT_CACHE_FILE.write_text(json.dumps({"day": today, "key": self._fut_key,
                                                     "symbol": rows[0].get("trading_symbol")}))
        except Exception:
            pass
        return self._fut_key

    def fetch_futures_snapshot(self):
        """The NIFTY future's price + total OI + volume (for the buildup
        chart's futures score - price/OI drive direction+strength, volume
        drives confidence). Verified live end-to-end via futures_probe.py
        before being wired in here - the quote response for an F&O
        instrument carries 'oi' and 'volume' directly, no extra endpoint
        needed."""
        fut_key = self._nifty_future_key()
        if not fut_key:
            return 0, None, 0
        try:
            r = self.s.get(QUOTES_URL, params={"instrument_key": fut_key}, timeout=10)
            if r.status_code != 200:
                return 0, None, 0
            data = r.json().get("data", {}) or {}
            for k, q in data.items():
                return _f(q.get("last_price")), q.get("oi"), _f(q.get("volume"))
        except Exception:
            pass
        return 0, None, 0

    def fetch(self):
        """Return a dict of all NIFTY context, ready for analyze_nifty()."""
        out = {"spot": 0, "open": 0, "prev_close": 0, "vwap": 0,
               "fut_price": 0, "vix": 0, "vix_prev": self._vix_prev,
               "pcr": 1.0, "max_pain": 0}

        # ---- 1. spot + VIX + future in one quotes call ----
        keys = [NIFTY_KEY, VIX_KEY]
        fut_key = self._nifty_future_key()
        if fut_key:
            keys.append(fut_key)
        try:
            r = self.s.get(QUOTES_URL,
                           params={"instrument_key": ",".join(keys)}, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", {}) or {}
                for k, q in data.items():
                    ohlc = q.get("ohlc", {}) or {}
                    ltp = _f(q.get("last_price"))
                    # prev close: ohlc.close is TODAY's running close (= ltp) ->
                    # gives 0% change. net_change = change vs previous close.
                    net = _f(q.get("net_change"))
                    oclose = _f(ohlc.get("close"))
                    if net and ltp:
                        prev = ltp - net
                    elif oclose and abs(oclose - ltp) > 1e-6:
                        prev = oclose
                    else:
                        prev = 0.0
                    # match by the symbol/key tail
                    if "Nifty 50" in k or k.endswith("Nifty 50"):
                        out["spot"] = ltp
                        out["open"] = _f(ohlc.get("open"))
                        out["prev_close"] = prev
                        out["vwap"] = _f(q.get("average_price")) or 0
                    elif "India VIX" in k or "VIX" in k:
                        out["vix"] = ltp
                    elif fut_key and (k.endswith(fut_key.split("|")[-1]) or "FUT" in k.upper()):
                        out["fut_price"] = ltp
        except Exception:
            pass

        # remember prev VIX / prev nifty for direction next scan
        if out["vix"]:
            out["vix_prev"] = self._vix_prev or out["vix"]
            self._vix_prev = out["vix"]

        # ---- 2. NIFTY option chain -> PCR + max pain ----
        # NOTE: expiry_date is REQUIRED by this endpoint - without it, Upstox
        # returns an empty/error response and this whole block silently did
        # nothing (pcr stuck at 1.0, max_pain at 0, every day). See
        # _nifty_option_expiry() for how the expiry is now resolved.
        expiry = self._nifty_option_expiry()
        if not expiry:
            print("  [nifty] option chain skipped: no expiry resolved")
        else:
            try:
                r = self.s.get(OPTION_CHAIN_URL,
                               params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
                               timeout=10)
                if r.status_code == 200:
                    rows = r.json().get("data", []) or []
                    if not rows:
                        print(f"  [nifty] option chain HTTP 200 but 0 rows (expiry={expiry})")
                    tc_oi = tp_oi = 0
                    strikes = {}
                    spot = out["spot"] or (rows[0].get("underlying_spot_price", 0) if rows else 0)
                    for row in rows:
                        st = row.get("strike_price", 0)
                        ce = (row.get("call_options", {}) or {}).get("market_data", {}) or {}
                        pe = (row.get("put_options", {}) or {}).get("market_data", {}) or {}
                        coi = int(ce.get("oi", 0) or 0)
                        poi = int(pe.get("oi", 0) or 0)
                        tc_oi += coi; tp_oi += poi
                        if st:
                            strikes[st] = {"c_oi": coi, "p_oi": poi}
                    if tc_oi:
                        out["pcr"] = round(tp_oi / tc_oi, 2)
                    # max pain
                    if strikes:
                        best_k, best_pain = None, float("inf")
                        for K in strikes:
                            pain = sum(max(0, S - K) * d["c_oi"] + max(0, K - S) * d["p_oi"]
                                       for S, d in strikes.items())
                            if pain < best_pain:
                                best_pain, best_k = pain, K
                        out["max_pain"] = best_k or 0
                    if not out["spot"] and spot:
                        out["spot"] = spot
                else:
                    print(f"  [nifty] option chain HTTP {r.status_code} (expiry={expiry}): {r.text[:150]}")
            except Exception as e:
                print(f"  [nifty] option chain error: {e}")

        return out

    def fetch_candles(self, interval_min=3):
        """
        Fetch today's NIFTY intraday candles at the given interval.
        Returns list of {o,h,l,c,v,ts}, oldest first. Uses the future if we
        have its key (tradable proxy), else the index spot.
        """
        # Use the INDEX key directly for candles - it's reliable. (The future
        # key from option/contract is flaky; index intraday candles always work.)
        key = NIFTY_KEY
        from urllib.parse import quote
        ek = quote(key, safe='')
        # Upstox MOVED intraday candles to a v3 path that also supports arbitrary
        # minute intervals (1-300), so we request 3-min candles DIRECTLY:
        #   v3: /v3/historical-candle/intraday/{key}/minutes/{n}
        #   v2 (fallback): /v2/historical-candle/intraday/{key}/1minute  (1-min only)
        # The old v2-only code 404'd silently -> the chart stayed blank with no
        # clue. Now we try v3, fall back to v2, and PRINT exactly what happened.
        candles_raw, src = [], None
        v3 = f"https://api.upstox.com/v3/historical-candle/intraday/{ek}/minutes/{interval_min}"
        try:
            r = self.s.get(v3, timeout=10)
            if r.status_code == 200:
                candles_raw = r.json().get("data", {}).get("candles", []) or []
                src = "v3"
            else:
                print(f"  [nifty] candles v3: HTTP {r.status_code} {r.text[:90]}")
        except Exception as e:
            print(f"  [nifty] candles v3: error {e}")
        if not candles_raw:                       # fall back to v2 1-minute + aggregate
            v2 = f"{INTRADAY_URL}/{ek}/1minute"
            try:
                r = self.s.get(v2, timeout=10)
                if r.status_code == 200:
                    candles_raw = r.json().get("data", {}).get("candles", []) or []
                    src = "v2-1m"
                else:
                    print(f"  [nifty] candles v2: HTTP {r.status_code} {r.text[:90]}")
            except Exception as e:
                print(f"  [nifty] candles v2: error {e}")
        if not candles_raw:
            print("  [nifty] NO candles from v3 or v2 - chart will stay empty")
            return []
        # Upstox returns [ts, open, high, low, close, volume, oi], newest first
        rows = []
        for c in reversed(candles_raw):
            if len(c) < 6:
                continue
            rows.append({"ts": c[0], "o": _f(c[1]), "h": _f(c[2]),
                         "l": _f(c[3]), "c": _f(c[4]), "v": _f(c[5])})
        # only the v2 fallback needs aggregating (v3 already gave us 3-min)
        if src == "v2-1m" and interval_min == 3 and rows:
            rows = self._aggregate(rows, 3)
        print(f"  [nifty] loaded {len(rows)} candles ({src})")
        return rows

    @staticmethod
    def _aggregate(one_min, n):
        """Aggregate 1-min candles into n-min candles."""
        out = []
        for i in range(0, len(one_min), n):
            chunk = one_min[i:i + n]
            if not chunk:
                continue
            out.append({
                "ts": chunk[0]["ts"],
                "o": chunk[0]["o"],
                "h": max(c["h"] for c in chunk),
                "l": min(c["l"] for c in chunk),
                "c": chunk[-1]["c"],
                "v": sum(c["v"] for c in chunk),
            })
        return out

    def fetch_nifty_oi_totals(self):
        """Total call OI, put OI, call volume, put volume for NIFTY nearest
        expiry (for the buildup chart's options score - OI drives
        direction+strength, volume drives confidence)."""
        expiry = self._nifty_option_expiry()
        if not expiry:
            return 0, 0, 0, 0
        try:
            r = self.s.get(OPTION_CHAIN_URL,
                           params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
                           timeout=10)
            if r.status_code != 200:
                print(f"  [nifty] OI totals HTTP {r.status_code} (expiry={expiry}): {r.text[:150]}")
                return 0, 0, 0, 0
            rows = r.json().get("data", []) or []
            tc = tp = tcv = tpv = 0
            for row in rows:
                ce = (row.get("call_options", {}) or {}).get("market_data", {}) or {}
                pe = (row.get("put_options", {}) or {}).get("market_data", {}) or {}
                tc += int(ce.get("oi", 0) or 0)
                tp += int(pe.get("oi", 0) or 0)
                tcv += int(ce.get("volume", 0) or 0)
                tpv += int(pe.get("volume", 0) or 0)
            return tc, tp, tcv, tpv
        except Exception as e:
            print(f"  [nifty] OI totals error: {e}")
            return 0, 0, 0, 0