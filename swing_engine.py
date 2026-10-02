"""
swing_engine.py  -  Multi-day SWING setup finder (daily F&O footprint).
=====================================================================
Different timeframe, same brain as the intraday engines. Instead of
3-minute order flow, this reads the DAILY story: it pulls each F&O
stock's DAILY FUTURES candles (price + volume + OPEN INTEREST per day)
over the last ~5 weeks and reconstructs what institutions have been
doing session by session BEFORE a move - the accumulation / distribution
footprint an experienced desk would read off the open-interest tape.

THE STORY IT LOOKS FOR (the thing that precedes a real swing):
  A quiet base + steadily rising futures OI in one direction + volume
  leaning the same way = positions being built before the move. The
  daily buildup/unwind table is exactly the intraday one, read per DAY:
     price up   + OI up   -> Long Buildup    (bullish, fresh longs)
     price up   + OI down -> Short Covering   (bullish, weak)
     price down + OI up   -> Short Buildup    (bearish, fresh shorts)
     price down + OI down -> Long Unwinding   (bearish, weak)

  A run of Long-Buildup days while price coils under resistance, OI
  climbing, up-day volume heavier than down-day volume = accumulation.
  The mirror (Short-Buildup days, price pinned near support) = distribution.

OUTPUT per setup: {sym, dir, grade, score, story, levels, days[...], ...}
  - score 0-100 = quality / breadth of the footprint
  - grade A/B/C, dir 'long'/'short'
  - story = plain-English narrative of what happened over the days
  - days = last ~12 sessions' buildup classification (for the UI strip)

NOTE (honest scope): this is built on FUTURES price+volume+OI, which is
where the institutional positioning footprint actually lives. Full
day-by-day option-chain-by-strike history isn't cheaply available from
the broker API, so options aren't part of the daily story here - the
futures OI tape carries the real accumulation signal.
"""

import json
import time
import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# Upstox historical-candle endpoints (daily). v3 first, v2 fallback.
HIST_V3 = "https://api.upstox.com/v3/historical-candle/{ek}/days/1/{to}/{frm}"
HIST_V2 = "https://api.upstox.com/v2/historical-candle/{ek}/day/{to}/{frm}"
# HOURLY candles (for the per-hour OI buildup chart) - fetched on demand.
# The HISTORICAL endpoint returns only COMPLETED sessions (up to yesterday);
# TODAY's forming hourly candles come from the separate INTRADAY endpoint, so
# we pull BOTH and merge - otherwise the chart stops at yesterday during live
# market (which is exactly what happened).
HIST_HOUR = "https://api.upstox.com/v3/historical-candle/{ek}/hours/1/{to}/{frm}"
HIST_HOUR_INTRA = "https://api.upstox.com/v3/historical-candle/intraday/{ek}/hours/1"
HIST_HOUR_INTRA2 = "https://api.upstox.com/v3/historical-candle/intraday/{ek}/minutes/60"
HOUR_LOOKBACK = 14          # calendar days of hourly data to pull on demand
HOUR_CACHE_SEC = 300        # re-use a stock's hourly pull for 5 min (live-ish)
# OPTIONS - live chain snapshot (where the call/put OI walls are). There is no
# cheap HISTORICAL options-OI time series from the broker, so options is a
# live read of writer positioning (the walls that defend S/R), not a line.
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
OPT_EXPIRY_CACHE = Path("swing_opt_expiry_cache.json")
OPT_CACHE_SEC = 900         # re-use a stock's option snapshot for 15 min

# Bump this string every time the ranking logic changes. It is printed at
# startup AND shown in the UI, so you can VERIFY the new code actually loaded
# (if the version on screen doesn't match, the .py on disk wasn't replaced -
# delete __pycache__ and copy the file again).
SWING_ENGINE_VERSION = "oi-trend-v8  (2026-10-01)"

LOOKBACK_DAYS = 60     # calendar days requested (~40 trading sessions) per fetch
KEEP_DAYS     = 90     # how much daily history we accumulate/retain on disk
RECENT        = 10     # the window we count the recent buildup story over
MIN_DAYS      = 12     # need at least this many sessions to judge a setup
CACHE_FILE    = Path("swing_cache.json")


def _sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


class SwingEngine:
    """Reuses the near-month FUTURES instrument keys already resolved by
    stock_futures.py (passed in), so it does not re-download the instrument
    master. One daily-candle call per stock per refresh."""

    def __init__(self, access_token, fut_keys, under_keys=None):
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })
        self.fut_keys = dict(fut_keys or {})    # {SYMBOL: futures instrument_key}
        self.under_keys = dict(under_keys or {})  # {SYMBOL: underlying equity key} - for options
        self.daily = {}                        # {SYMBOL: [day dicts oldest->newest]}
        self.daily_date = {}                   # {SYMBOL: 'YYYY-MM-DD' last fetched}
        self.hourly_cache = {}                 # {SYMBOL: (fetched_ts, [hour dicts])}
        self.opt_expiry = {}                   # {SYMBOL: 'YYYY-MM-DD' nearest option expiry}
        self.opt_expiry_day = ""               # date the expiry map was built
        self.opt_cache = {}                    # {SYMBOL: (fetched_ts, snapshot dict)}
        self.last_updated = ""
        self.last_count = 0
        self.version = SWING_ENGINE_VERSION
        print(f"  [swing] engine {SWING_ENGINE_VERSION} loaded")
        self._load_cache()

    # ---------------- disk cache (survive a restart) ----------------
    def _load_cache(self):
        if not CACHE_FILE.exists():
            return
        try:
            c = json.loads(CACHE_FILE.read_text())
            self.daily = c.get("daily", {})
            self.daily_date = c.get("daily_date", {})
        except Exception:
            self.daily = {}; self.daily_date = {}

    def _save_cache(self):
        try:
            CACHE_FILE.write_text(json.dumps({
                "daily": self.daily, "daily_date": self.daily_date}))
        except Exception:
            pass

    # ---------------- fetch one stock's daily futures candles ----------------
    def _fetch_daily(self, key):
        ek = quote(key, safe="")
        today = datetime.date.today()
        frm = (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
        to = today.isoformat()
        raw = None
        for url in (HIST_V3.format(ek=ek, to=to, frm=frm),
                    HIST_V2.format(ek=ek, to=to, frm=frm)):
            try:
                r = self.s.get(url, timeout=12)
                if r.status_code == 200:
                    raw = r.json().get("data", {}).get("candles", []) or []
                    if raw:
                        break
            except Exception:
                continue
        if not raw:
            return []
        # Upstox returns [ts, o, h, l, c, volume, oi], NEWEST first -> flip
        out = []
        for c in reversed(raw):
            if len(c) < 6:
                continue
            out.append({
                "date": str(c[0])[:10],
                "o": _f(c[1]), "h": _f(c[2]), "l": _f(c[3]), "c": _f(c[4]),
                "v": _f(c[5]),
                "oi": _f(c[6]) if len(c) > 6 else 0.0,
            })
        return out

    # ---------------- hourly series for the per-hour OI buildup chart --------
    def fetch_hourly(self, sym):
        """On-demand: one HOURLY futures-candle call for a single stock, so the
        OI chart can show a dot for every HOUR (how institutions add in clips
        through the day) instead of one lump per day. Cached ~15 min per stock.
        Returns last ~10 sessions of hourly points, oldest->newest."""
        sym = (sym or "").upper().strip()
        key = self.fut_keys.get(sym)
        if not key:
            return []
        now = time.time()
        hit = self.hourly_cache.get(sym)
        if hit and (now - hit[0] < HOUR_CACHE_SEC):
            return hit[1]
        fut = self._hourly_raw(key)           # FUTURES: carries OI + volume
        if not fut:
            return []
        # Overlay the ACTUAL stock (cash/equity) price - the price the user
        # sees on their chart - matched hour-by-hour. OI & volume stay from the
        # future; only the price line becomes the real spot price.
        ekey = self.under_keys.get(sym)
        if ekey and ekey != key:
            eq = {p["ts"]: p["c"] for p in self._hourly_raw(ekey)}
            if eq:
                for p in fut:
                    if p["ts"] in eq:
                        p["cash"] = eq[p["ts"]]
        pts = []
        for p in fut[-66:]:                   # ~10 sessions x ~6-7 hrs
            pts.append({
                "d": p["d"], "hh": p["hh"],
                "c": round(p.get("cash", p["c"]), 1),   # actual price (fallback: futures)
                "oi": int(p["oi"]), "v": int(p["v"]),
            })
        self.hourly_cache[sym] = (now, pts)
        return pts

    def _hourly_raw(self, key):
        """Raw hourly candles for ANY instrument key -> list of
        {ts, d, hh, c, oi, v} oldest->newest. ts = 'YYYY-MM-DDTHH:MM'. Pulls
        COMPLETED sessions (historical) AND TODAY's forming session (intraday)
        and merges them, de-duped by timestamp, so the chart reaches the
        current hour during live market."""
        ek = quote(key, safe="")
        today = datetime.date.today()
        frm = (today - datetime.timedelta(days=HOUR_LOOKBACK)).isoformat()
        to = today.isoformat()
        rows = []
        # 1. historical hourly (up to yesterday)
        try:
            r = self.s.get(HIST_HOUR.format(ek=ek, to=to, frm=frm), timeout=12)
            if r.status_code == 200:
                rows += r.json().get("data", {}).get("candles", []) or []
        except Exception:
            pass
        # 2. intraday hourly (TODAY) - try the hours unit, fall back to 60-min
        for url in (HIST_HOUR_INTRA.format(ek=ek), HIST_HOUR_INTRA2.format(ek=ek)):
            try:
                r2 = self.s.get(url, timeout=12)
                if r2.status_code == 200:
                    got = r2.json().get("data", {}).get("candles", []) or []
                    if got:
                        rows += got
                        break
            except Exception:
                continue
        # parse + de-dupe by timestamp (historical & intraday can overlap)
        seen = {}
        for c in rows:
            if len(c) < 6:
                continue
            ts = str(c[0])
            seen[ts[:16]] = {"ts": ts[:16], "d": ts[5:10], "hh": ts[11:16],
                             "c": _f(c[4]), "oi": _f(c[6]) if len(c) > 6 else 0.0, "v": _f(c[5])}
        return sorted(seen.values(), key=lambda p: p["ts"])

    # ---------------- options: nearest expiry per stock (once/day) ----------
    def _ensure_opt_expiry(self):
        today = datetime.date.today()
        today_s = today.isoformat()
        if self.opt_expiry and self.opt_expiry_day == today_s:
            return
        if OPT_EXPIRY_CACHE.exists():
            try:
                c = json.loads(OPT_EXPIRY_CACHE.read_text())
                if c.get("day") == today_s and c.get("map"):
                    self.opt_expiry = c["map"]; self.opt_expiry_day = today_s
                    return
            except Exception:
                pass
        try:
            import instruments as instruments_mod
            data = instruments_mod._download_instruments()
        except Exception as e:
            print(f"  [swing] option-expiry download failed: {e}")
            self.opt_expiry_day = today_s
            return
        wanted = set(self.fut_keys) | set(self.under_keys)
        exp_by_sym = {}   # SYMBOL -> nearest future-dated expiry
        for row in data:
            if row.get("segment") != "NSE_FO":
                continue
            if str(row.get("instrument_type", "")).upper() not in ("CE", "PE"):
                continue
            u = (row.get("underlying_symbol") or row.get("asset_symbol") or "").upper().strip()
            if u not in wanted:
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
                if u not in exp_by_sym or d < exp_by_sym[u]:
                    exp_by_sym[u] = d
        self.opt_expiry = {u: d.isoformat() for u, d in exp_by_sym.items()}
        self.opt_expiry_day = today_s
        try:
            OPT_EXPIRY_CACHE.write_text(json.dumps({"day": today_s, "map": self.opt_expiry}))
        except Exception:
            pass
        print(f"  [swing] option expiries resolved for {len(self.opt_expiry)} stocks")

    def fetch_options(self, sym):
        """On-demand LIVE option-chain snapshot for one stock: total call/put OI,
        PCR, and the big OI WALLS (max put-OI strike = where writers are
        defending support, max call-OI strike = resistance). Plus the ATM-area
        strike ladder for a mini call-vs-put bar. Cached ~15 min."""
        sym = (sym or "").upper().strip()
        key = self.under_keys.get(sym) or self.fut_keys.get(sym)
        if not key:
            return {}
        now = time.time()
        hit = self.opt_cache.get(sym)
        if hit and (now - hit[0] < OPT_CACHE_SEC):
            return hit[1]
        self._ensure_opt_expiry()
        expiry = self.opt_expiry.get(sym)
        if not expiry:
            return {}
        try:
            r = self.s.get(OPTION_CHAIN_URL,
                           params={"instrument_key": key, "expiry_date": expiry}, timeout=12)
            if r.status_code != 200:
                return {}
            rows = r.json().get("data", []) or []
        except Exception:
            return {}
        if not rows:
            return {}
        spot = 0.0
        strikes = []
        tc = tp = 0
        for row in rows:
            st = _f(row.get("strike_price"))
            ce = (row.get("call_options", {}) or {}).get("market_data", {}) or {}
            pe = (row.get("put_options", {}) or {}).get("market_data", {}) or {}
            coi = int(_f(ce.get("oi")))
            poi = int(_f(pe.get("oi")))
            tc += coi; tp += poi
            if not spot:
                spot = _f(row.get("underlying_spot_price"))
            if st:
                strikes.append({"k": st, "coi": coi, "poi": poi})
        if not strikes:
            return {}
        strikes.sort(key=lambda x: x["k"])
        pcr = round(tp / tc, 2) if tc else 0.0
        call_wall = max(strikes, key=lambda x: x["coi"])      # resistance
        put_wall = max(strikes, key=lambda x: x["poi"])       # support
        # ATM-area ladder (nearest ~6 strikes each side) for the mini chart
        if not spot and strikes:
            spot = strikes[len(strikes) // 2]["k"]
        atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i]["k"] - spot))
        ladder = strikes[max(0, atm_i - 6): atm_i + 7]
        snap = {
            "sym": sym, "expiry": expiry, "spot": round(spot, 1),
            "pcr": pcr, "tot_call_oi": tc, "tot_put_oi": tp,
            "call_wall": round(call_wall["k"], 1), "call_wall_oi": call_wall["coi"],
            "put_wall": round(put_wall["k"], 1), "put_wall_oi": put_wall["poi"],
            "ladder": ladder, "atm": round(strikes[atm_i]["k"], 1),
        }
        self.opt_cache[sym] = (now, snap)
        return snap

    # ---------------- the analysis (the institutional read) ----------------
    def analyze(self, sym, daily):
        if not daily or len(daily) < MIN_DAYS:
            return None
        d = daily[-30:]                      # cap to last ~30 sessions
        n = len(d)
        closes = [x["c"] for x in d]
        highs = [x["h"] for x in d]
        lows = [x["l"] for x in d]
        vols = [x["v"] for x in d]
        ois = [x["oi"] for x in d]
        last = d[-1]
        if not last["c"]:
            return None
        have_oi = any(o > 0 for o in ois)

        rec = min(RECENT, n - 1)             # recent window size actually available

        # --- per-day buildup/unwind classification ---
        day_class = []                       # aligned to d[1:]
        for i in range(1, n):
            pu = closes[i] > closes[i - 1]
            ou = ois[i] > ois[i - 1]
            if not have_oi:
                cls = "UP" if pu else "DN"    # no OI: just mark the day's direction
            elif pu and ou:
                cls = "LB"
            elif pu and not ou:
                cls = "SC"
            elif (not pu) and ou:
                cls = "SB"
            else:
                cls = "LU"
            day_class.append(cls)
        recent_cls = day_class[-rec:]
        nLB = recent_cls.count("LB"); nSC = recent_cls.count("SC")
        nSB = recent_cls.count("SB"); nLU = recent_cls.count("LU")
        nUP = recent_cls.count("UP"); nDN = recent_cls.count("DN")

        # --- OI expansion over the recent window ---
        oi_then = ois[-rec - 1] if len(ois) > rec else (ois[0] or 0)
        oi_chg_pct = ((ois[-1] - oi_then) / oi_then * 100.0) if oi_then else 0.0

        # --- where price sits in its recent (10-day) range (context only -
        #     YOU read the real S/R & trendline on the chart yourself) ---
        win = min(RECENT, n)
        hi20 = max(highs[-win:]); lo20 = min(lows[-win:])
        rng = (hi20 - lo20) or (last["c"] * 0.01)
        pos_in_range = _clamp((last["c"] - lo20) / rng, 0.0, 1.0)

        # --- volume footprint over the recent window (accumulation vs distribution) ---
        up_v = [vols[i] for i in range(n - rec, n) if closes[i] > closes[i - 1]]
        dn_v = [vols[i] for i in range(n - rec, n) if closes[i] <= closes[i - 1]]
        up_avg = (sum(up_v) / len(up_v)) if up_v else 0.0
        dn_avg = (sum(dn_v) / len(dn_v)) if dn_v else 0.0
        vol_ratio = (up_avg / dn_avg) if dn_avg else (1.5 if up_avg else 1.0)

        win_chg_pct = ((closes[-1] - closes[-rec - 1]) / closes[-rec - 1] * 100.0) \
            if len(closes) > rec and closes[-rec - 1] else 0.0

        range_pct = round(rng / lo20 * 100, 2) if lo20 else 0.0   # width of the 10d range
        pos = "near top" if pos_in_range > 0.66 else "near bottom" if pos_in_range < 0.33 else "mid-range"
        bull_build = nLB          # price up + OI up  -> fresh LONGS
        bear_build = nSB          # price down + OI up -> fresh SHORTS

        # ============ COILED SPRING: OI rising while price is RANGE-BOUND ======
        # Conviction entering (OI climbing) but price still held in a tight range
        # = positions being BUILT before the move. Tighter range + bigger OI
        # build = more loaded. This is the "about to move" setup Prasad wants.
        if have_oi and oi_chg_pct >= 4.0 and range_pct <= 3.0:
            stype = "coiled"
            lean = bull_build - bear_build
            direction = "long" if lean > 0 else "short" if lean < 0 else "flat"
            tight = _clamp((3.0 - range_pct) / 3.0, 0.0, 1.0)      # tighter range = higher
            oi_mag = _clamp(oi_chg_pct, 0, 25) / 25
            score = round(_clamp(oi_mag * 55 + tight * 45, 0, 100), 1)
            strength = "Strong" if score >= 70 else "Building" if score >= 57 else "Early"
            build = max(bull_build, bear_build)
            lean_txt = ("slight long lean" if lean > 0 else "slight short lean" if lean < 0
                        else "no clear lean yet")
            line = (f"OI {oi_chg_pct:+.0f}% over {rec} days while price coiled in a "
                    f"{range_pct:.1f}% range — loaded, {lean_txt}. Watch for the break.")
        else:
            # ===== one-sided buildup: OI rising + price already moving one way =====
            if max(bull_build, bear_build) < 4:
                return None
            if have_oi and oi_chg_pct <= 1:
                return None
            stype = "buildup"
            if bull_build >= bear_build:
                direction, build = "long", bull_build
            else:
                direction, build = "short", bear_build
            consistency = build / max(1, rec)
            oi_mag = _clamp(abs(oi_chg_pct), 0, 20) / 20
            vol_conf = _clamp((vol_ratio - 1.0) if direction == "long"
                              else ((1.0 / vol_ratio - 1.0) if vol_ratio else 0.0), 0, 1)
            score = round(_clamp(consistency * 55 + oi_mag * 35 + vol_conf * 10, 0, 100), 1)
            if score < 45:
                return None
            strength = "Strong" if score >= 70 else "Building" if score >= 57 else "Early"
            vp = ""
            if direction == "long" and vol_ratio >= 1.3:
                vp = " · heavy up-vol"
            elif direction == "short" and vol_ratio and vol_ratio <= 0.77:
                vp = " · heavy down-vol"
            if not have_oi:
                line = (f"{build} of {rec} days leaned {'up' if direction == 'long' else 'down'} "
                        f"(no OI data){vp} · price {pos} of range")
            elif direction == "long":
                line = (f"Futures OI {oi_chg_pct:+.0f}% over {rec} days · {build} of {rec} days "
                        f"fresh LONG buildup{vp} · price {pos} of range")
            else:
                line = (f"Futures OI {oi_chg_pct:+.0f}% over {rec} days · {build} of {rec} days "
                        f"fresh SHORT buildup{vp} · price {pos} of range")

        # ONLY the last 10 days for the strip + charts. A month-long view hides
        # the fact that positions build AND get unwound - the recent 10 sessions
        # are the live picture of what's on RIGHT NOW, which is all that matters.
        strip = []
        for i in range(max(1, n - RECENT), n):
            strip.append({
                "date": d[i]["date"][5:],
                "cls": day_class[i - 1],
                "chg": round((closes[i] - closes[i - 1]) / closes[i - 1] * 100, 2) if closes[i - 1] else 0,
                "d_oi": int(ois[i] - ois[i - 1]) if have_oi else 0,
            })

        # last ~10 sessions for the inline OI / price / volume line charts
        hist = [{"d": x["date"][5:], "c": round(x["c"], 1),
                 "oi": int(x["oi"]), "v": int(x["v"])} for x in d[-(RECENT + 1):]]

        return {
            "sym": sym, "dir": direction, "type": stype, "strength": strength, "score": score,
            "line": line, "days": strip, "hist": hist,
            "oi_chg_pct": round(oi_chg_pct, 1), "win_chg_pct": round(win_chg_pct, 1),
            "range_pct": range_pct, "build": build, "rec": rec, "pos": pos,
            "ltp": round(last["c"], 1), "lo": round(lo20, 1), "hi": round(hi20, 1),
            "have_oi": have_oi,
        }

    # ---------------- PURE OI-RISING ranking (price ignored) ----------------
    def oi_rising(self, lookback=5):
        """Rank stocks ONLY by how much their FUTURES OI has grown over the last
        `lookback` sessions - price direction completely ignored. 'Positions are
        being ADDED here, whatever side.' Rewards both the SIZE of the OI build
        and its CONSISTENCY (OI up on most of the days, not one freak spike).
        Reuses already-fetched daily history, so no extra API calls."""
        out = []
        for sym in self.fut_keys:
            try:
                daily = self.daily.get(sym)
                if not daily or len(daily) < lookback + 2:
                    continue
                d = daily[-30:]; n = len(d)
                ois = [x["oi"] for x in d]
                closes = [x["c"] for x in d]
                highs = [x["h"] for x in d]; lows = [x["l"] for x in d]
                if not any(o > 0 for o in ois):
                    continue
                lb = min(lookback, n - 1)
                w = ois[-lb - 1:]            # lb+1 OI points across the window
                if len(w) < 3 or not w[0]:
                    continue
                N = len(w)
                oi_pct = (w[-1] - w[0]) / w[0] * 100.0          # net over the window
                # ---- overall OI TREND via least-squares slope. THIS is the fix:
                # ranking on the SLOPE (the steady climb), not the net change, means
                # d1<d2<d3 with a small d4<d3 dip STILL scores high (the slope stays
                # strongly positive) - "still growing, rank it good". A stock that
                # merely spiked once days ago and is now flat/falling has a weak or
                # negative slope, so it no longer sits on top. ----
                mean_x = (N - 1) / 2.0
                mean_y = sum(w) / N
                num = sum((i - mean_x) * (w[i] - mean_y) for i in range(N))
                den = sum((i - mean_x) ** 2 for i in range(N)) or 1.0
                slope = num / den                                      # OI units per session
                slope_pct = (slope / mean_y * 100.0) if mean_y else 0.0  # % of avg OI per session
                steps = [w[i] - w[i - 1] for i in range(1, N)]
                rising_days = sum(1 for s in steps if s > 0)
                frac = rising_days / (N - 1)                           # share of sessions that rose
                wr = w[-3:]                                            # recent direction (last 3 pts)
                rec_slope = wr[-1] - wr[0]
                recent_pct = ((wr[-1] - wr[0]) / wr[0] * 100.0) if wr[0] else 0.0
                peak = max(w); off_peak = ((peak - w[-1]) / peak * 100.0) if peak else 0.0
                # must be a real, mostly-one-way climb (NOT hard-cut on a single dip)
                if oi_pct <= 1.0 or slope_pct <= 0 or frac < 0.5:
                    continue
                slope_mag = _clamp(slope_pct, 0, 5) / 5               # ~5%/session = full strength
                base = slope_mag * 0.55 + frac * 0.45                 # 0..1 : strength of the climb
                # SOFT demotes only (never exclude on one dip):
                #  - a SUSTAINED recent turn-down (last 3 sessions net lower)
                #  - OI now well BELOW its window high (it's distributing, not building)
                rec_factor = 0.6 if rec_slope < 0 else 1.0
                peak_factor = _clamp(1.0 - off_peak / 12.0, 0.4, 1.0)  # ~12%+ off high = heavily demoted
                score = round(_clamp(base * rec_factor * peak_factor * 100, 0, 100), 1)
                have_oi = True
                # context strip + line chart (same shape the swing card expects)
                day_class = []
                for i in range(1, n):
                    pu = closes[i] > closes[i - 1]; ou = ois[i] > ois[i - 1]
                    day_class.append("LB" if pu and ou else "SC" if pu else "SB" if ou else "LU")
                strip = [{"date": d[i]["date"][5:], "cls": day_class[i - 1],
                          "chg": round((closes[i] - closes[i - 1]) / closes[i - 1] * 100, 2) if closes[i - 1] else 0,
                          "d_oi": int(ois[i] - ois[i - 1])}
                         for i in range(max(1, n - RECENT), n)]
                hist = [{"d": x["date"][5:], "c": round(x["c"], 1),
                         "oi": int(x["oi"]), "v": int(x["v"])} for x in d[-(RECENT + 1):]]
                win = min(RECENT, n); hi = max(highs[-win:]); lo = min(lows[-win:])
                price_pct = ((closes[-1] - closes[-lb - 1]) / closes[-lb - 1] * 100.0) \
                    if closes[-lb - 1] else 0.0
                still = "at its high" if off_peak < 0.5 else f"{off_peak:.1f}% off high"
                line = (f"Futures OI {oi_pct:+.0f}% over {lb} days · rising trend "
                        f"{slope_pct:+.1f}%/session · OI up {rising_days}/{lb} days · {still} · "
                        f"price {price_pct:+.1f}% (direction aside)")
                out.append({
                    "sym": sym, "dir": "flat", "type": "oirise",
                    "strength": "Strong" if score >= 70 else "Building" if score >= 55 else "Early",
                    "score": score, "line": line, "days": strip, "hist": hist,
                    "oi_chg_pct": round(oi_pct, 1), "rising_days": rising_days, "lb": lb,
                    "win_chg_pct": round(price_pct, 1), "range_pct": 0,
                    "build": rising_days, "rec": lb, "pos": "",
                    "ltp": round(closes[-1], 1), "lo": round(lo, 1), "hi": round(hi, 1),
                    "have_oi": True,
                })
            except Exception:
                continue
        out.sort(key=lambda x: (x["score"], x["oi_chg_pct"]), reverse=True)
        return out

    # ---------------- search: pull up ANY stock on demand ----------------
    def lookup(self, sym):
        """Return a swing card for ANY F&O stock the user searches - even if it
        isn't a ranked setup. If it has a real one-sided buildup, return the
        full analysis; otherwise return a bare card so the futures OI chart and
        options walls are still there to inspect."""
        sym = (sym or "").upper().strip()
        if not sym:
            return {}
        if sym not in self.fut_keys and sym not in self.under_keys:
            return {"sym": sym, "unknown": True}
        today = datetime.date.today().isoformat()
        if sym not in self.daily:
            key = self.fut_keys.get(sym)
            if key:
                dd = self._fetch_daily(key)
                if dd:
                    self.daily[sym] = self._merge_daily(self.daily.get(sym), dd)
                    self.daily_date[sym] = today
        daily = self.daily.get(sym)
        a = self.analyze(sym, daily) if daily else None
        if a:
            a["searched"] = True
            return a
        return self._bare_card(sym, daily)

    def _bare_card(self, sym, daily):
        card = {"sym": sym, "dir": "flat", "type": "inspect", "strength": "—", "score": 0, "searched": True,
                "line": "No strong one-sided buildup right now — open the charts to inspect "
                        "futures OI & the options walls.",
                "days": [], "hist": [], "oi_chg_pct": 0, "win_chg_pct": 0,
                "build": 0, "rec": 0, "pos": "", "ltp": 0, "lo": 0, "hi": 0, "have_oi": False}
        if not daily or len(daily) < 2:
            return card
        d = daily[-30:]; n = len(d)
        closes = [x["c"] for x in d]; highs = [x["h"] for x in d]
        lows = [x["l"] for x in d]; ois = [x["oi"] for x in d]
        have_oi = any(o > 0 for o in ois)
        cls = []
        for i in range(1, n):
            pu = closes[i] > closes[i - 1]; ou = ois[i] > ois[i - 1]
            cls.append(("UP" if pu else "DN") if not have_oi else
                       ("LB" if pu and ou else "SC" if pu else "SB" if ou else "LU"))
        strip = [{"date": d[i]["date"][5:], "cls": cls[i - 1],
                  "chg": round((closes[i] - closes[i - 1]) / closes[i - 1] * 100, 2) if closes[i - 1] else 0,
                  "d_oi": int(ois[i] - ois[i - 1]) if have_oi else 0}
                 for i in range(max(1, n - RECENT), n)]
        hist = [{"d": x["date"][5:], "c": round(x["c"], 1),
                 "oi": int(x["oi"]), "v": int(x["v"])} for x in d[-(RECENT + 1):]]
        win = min(RECENT, n); hi = max(highs[-win:]); lo = min(lows[-win:])
        rng = (hi - lo) or (closes[-1] * 0.01)
        frac = (closes[-1] - lo) / rng
        pos = "near top" if frac > 0.66 else "near bottom" if frac < 0.33 else "mid-range"
        card.update({"days": strip, "hist": hist, "ltp": round(closes[-1], 1),
                     "lo": round(lo, 1), "hi": round(hi, 1), "pos": pos,
                     "have_oi": have_oi, "rec": win})
        return card

    # ---------------- refresh the whole universe ----------------
    @staticmethod
    def _merge_daily(old, new):
        """Accumulate history: union of cached + freshly fetched days, keyed by
        date so a day is never duplicated and the freshly-fetched (finalised)
        value wins. Keeps the last KEEP_DAYS sessions. This way the stored
        history only GROWS over time - run it for a few weeks and every stock
        has a deep, clean base to build the story from, even if any single
        fetch returns a short window."""
        by_date = {x["date"]: x for x in (old or [])}
        for x in (new or []):
            by_date[x["date"]] = x
        out = sorted(by_date.values(), key=lambda r: r["date"])
        return out[-KEEP_DAYS:]

    def refresh(self, sleep_between=0.08, force=False):
        """One daily-candle call per stock (gentle spacing). Daily candles
        only change once a day + today's forming bar, so a cached same-day
        fetch is reused unless force=True. Fetched data is MERGED into the
        stored history, never replaced, so the base deepens over time."""
        today = datetime.date.today().isoformat()
        out = []
        for sym, key in self.fut_keys.items():
            try:
                if force or self.daily_date.get(sym) != today or sym not in self.daily:
                    dd = self._fetch_daily(key)
                    if dd:
                        self.daily[sym] = self._merge_daily(self.daily.get(sym), dd)
                        self.daily_date[sym] = today
                    if sleep_between:
                        time.sleep(sleep_between)
                a = self.analyze(sym, self.daily.get(sym))
                if a:
                    out.append(a)
            except Exception:
                continue
        out.sort(key=lambda x: x["score"], reverse=True)
        self.last_updated = datetime.datetime.now().strftime("%H:%M")
        self.last_count = len(out)
        self._save_cache()
        return out