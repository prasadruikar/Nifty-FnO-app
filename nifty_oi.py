"""
nifty_oi.py  -  NIFTY futures OI/price chart data + option S/R walls.
=====================================================================
Powers the NIFTY OI page (nifty_oi.html). On demand only - nothing here runs in
the background; it's called when the page is open.

WHAT IT GIVES
  1. candles(tf, sessions) - NIFTY near-month FUTURE candles at 15m or 1h, for the
     last N sessions, each {ts,d,hh,c,oi,v}. Merges COMPLETED history + TODAY's
     forming intraday bars so it stays live during market.
  2. levels() - the strong OI SUPPORT / RESISTANCE from the live option chain:
     biggest put-OI strikes = support (writers defending below), biggest call-OI
     strikes = resistance (writers capping above). Drawn as lines on the chart.

It reuses NiftyData (nifty_data.py) ONLY to resolve the NIFTY future instrument
key and the nearest option expiry (the tricky 'plain NIFTY only' matching lives
there already), so this file stays small.
"""

import time
import datetime
from urllib.parse import quote

import requests

import nifty_data as nifty_data_mod

# interval -> (unit, value) for Upstox v3 historical/intraday candle endpoints.
# 15m/1h are intraday timeframes; 4h/1d are the HIGHER timeframes (HTF) for the
# bigger positioning picture. Daily has no intraday endpoint (today's forming bar
# comes from the historical days/1 call), which _raw() handles fine.
_TF = {
    "15m": ("minutes", "15"),
    "1h":  ("hours", "1"),
    "4h":  ("hours", "4"),
    "1d":  ("days", "1"),
}
HIST   = "https://api.upstox.com/v3/historical-candle/{ek}/{unit}/{val}/{to}/{frm}"
INTRA  = "https://api.upstox.com/v3/historical-candle/intraday/{ek}/{unit}/{val}"
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
NIFTY_KEY = "NSE_INDEX|Nifty 50"

CACHE_SEC   = 30          # re-use a (tf,sessions) pull for 30s (live-ish, light on API)
LEVELS_SEC  = 120         # option walls move slowly - refresh every 2 min


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


class NiftyOI:
    def __init__(self, access_token):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json",
                               "Authorization": f"Bearer {access_token}"})
        # reuse NiftyData purely for the future-key + expiry resolvers
        self._nd = nifty_data_mod.NiftyData(access_token)
        self._cache = {}          # {(tf,sessions): (ts, payload)}
        self._levels_cache = None  # (ts, payload)

    # ---------------- candles ----------------
    def _raw(self, key, unit, val, cal_days):
        """Completed history + today's intraday bars, merged, oldest->newest."""
        ek = quote(key, safe="")
        today = datetime.date.today()
        frm = (today - datetime.timedelta(days=cal_days)).isoformat()
        to = today.isoformat()
        rows = []
        try:
            r = self.s.get(HIST.format(ek=ek, unit=unit, val=val, to=to, frm=frm), timeout=15)
            if r.status_code == 200:
                rows += r.json().get("data", {}).get("candles", []) or []
        except Exception:
            pass
        try:
            r2 = self.s.get(INTRA.format(ek=ek, unit=unit, val=val), timeout=15)
            if r2.status_code == 200:
                rows += r2.json().get("data", {}).get("candles", []) or []
        except Exception:
            pass
        seen = {}
        for c in rows:
            if len(c) < 6:
                continue
            ts = str(c[0])
            seen[ts[:16]] = {
                "ts": ts[:16], "d": ts[5:10], "hh": ts[11:16],
                "c": _f(c[4]), "oi": _f(c[6]) if len(c) > 6 else 0.0, "v": _f(c[5]),
            }
        return sorted(seen.values(), key=lambda p: p["ts"])

    def candles(self, tf="15m", sessions=5):
        tf = tf if tf in _TF else "15m"
        sessions = max(1, min(30, int(sessions)))
        hit = self._cache.get((tf, sessions))
        now = time.time()
        if hit and now - hit[0] < CACHE_SEC:
            return hit[1]
        key = self._nd._nifty_future_key()
        if not key:
            return {"tf": tf, "sessions": sessions, "points": [], "error": "NIFTY future key not resolved"}
        unit, val = _TF[tf]
        cal_days = int(sessions * 1.7) + 6        # cover weekends/holidays
        raw = self._raw(key, unit, val, cal_days)
        # trim to the last `sessions` distinct trading dates
        dates = []
        for p in raw:
            if p["d"] not in dates:
                dates.append(p["d"])
        keep = set(dates[-sessions:])
        pts = [{"d": p["d"], "hh": p["hh"], "c": round(p["c"], 1),
                "oi": int(p["oi"]), "v": int(p["v"])} for p in raw if p["d"] in keep]
        payload = {"tf": tf, "sessions": sessions, "points": pts,
                   "updated": datetime.datetime.now().strftime("%H:%M:%S")}
        self._cache[(tf, sessions)] = (now, payload)
        return payload

    # ---------------- option S/R walls ----------------
    def levels(self):
        now = time.time()
        if self._levels_cache and now - self._levels_cache[0] < LEVELS_SEC:
            return self._levels_cache[1]
        expiry = None
        try:
            expiry = self._nd._nifty_option_expiry()
        except Exception:
            expiry = None
        out = {"support": [], "resistance": [], "spot": 0, "expiry": expiry or "", "stale": False}
        if not expiry:
            out["error"] = "no option expiry resolved"
            # fall back to last good if we have it
            if self._levels_cache:
                prev = dict(self._levels_cache[1]); prev["stale"] = True
                return prev
            return out
        try:
            r = self.s.get(OPTION_CHAIN_URL,
                           params={"instrument_key": NIFTY_KEY, "expiry_date": expiry}, timeout=12)
            rows = r.json().get("data", []) or [] if r.status_code == 200 else []
        except Exception:
            rows = []
        if not rows:
            if self._levels_cache:
                prev = dict(self._levels_cache[1]); prev["stale"] = True
                return prev
            out["error"] = "live chain empty (market shut / holiday)"
            return out
        spot = 0.0
        strikes = []
        for row in rows:
            k = _f(row.get("strike_price"))
            ce = (row.get("call_options", {}) or {}).get("market_data", {}) or {}
            pe = (row.get("put_options", {}) or {}).get("market_data", {}) or {}
            coi = int(_f(ce.get("oi"))); poi = int(_f(pe.get("oi")))
            if not spot:
                spot = _f(row.get("underlying_spot_price"))
            if k:
                strikes.append({"k": k, "coi": coi, "poi": poi})
        if not strikes:
            out["error"] = "no strikes"
            return out
        # resistance = biggest CALL-OI strikes above/around spot; support = biggest PUT-OI
        res = sorted(strikes, key=lambda x: x["coi"], reverse=True)[:3]
        sup = sorted(strikes, key=lambda x: x["poi"], reverse=True)[:3]
        out["spot"] = round(spot, 1)
        out["resistance"] = [{"price": round(x["k"], 1), "oi": x["coi"]} for x in res if x["coi"] > 0]
        out["support"] = [{"price": round(x["k"], 1), "oi": x["poi"]} for x in sup if x["poi"] > 0]
        out["updated"] = datetime.datetime.now().strftime("%H:%M:%S")
        self._levels_cache = (now, out)
        return out