"""
nifty_signal.py  -  Tradable NIFTY option signal engine.
=====================================================================
Fuses three layers into ONE smooth, high-probability signal for index
option buying, and flags TRAPS (price vs positioning divergence - the
idea Prasad described: a green price candle with selling underneath).

THE THREE LAYERS:

  PRICE (from NIFTY future - the tradable proxy for the index):
     - EMA(9) vs EMA(21) trend
     - price vs VWAP (intraday control)
     - RSI(14) (momentum + overbought/oversold)

  POSITIONING (from option OI, per interval):
     - net OI flow: call writing/unwinding vs put writing/unwinding
     - PCR direction

  STRENGTH (volume):
     - volume-weighted candle direction (buyers vs sellers)

HIGH-PROBABILITY DISCIPLINE (so signals are tradable, not noise):
  - each layer votes bull / bear / neutral
  - a BUY CALL / BUY PUT signal fires ONLY when price + at least one of
    (positioning, volume) agree, AND the signal HOLDS for >=2 intervals
    (confirmation - kills single-candle fakes)
  - the signal strength is smoothed (EMA) so it doesn't flicker
  - DIVERGENCE (price up but positioning/volume down, or vice versa) does
    NOT give a trade signal - it raises a TRAP flag instead (reversal warning)

OUTPUT:
  {signal: BUY CALL/BUY PUT/WAIT/TRAP, strength, trend, layers{}, trap{}, note}
"""

import json
import math
import datetime
from pathlib import Path

STORE_FILE = Path(__file__).parent / "nifty_signal_state.json"


def _sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# --- indicators inlined so this engine NEVER depends on a stale indicators.py
# (a missing ema_series there was throwing AttributeError and blanking the tab)
def _ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0, d)); losses.append(max(0, -d))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def _vwap(candles):
    num = den = 0.0
    for c in candles:
        tp = (c["h"] + c["l"] + c["c"]) / 3.0
        v = c.get("v", 0) or 0
        num += tp * v; den += v
    return round(num / den, 2) if den else (candles[-1]["c"] if candles else 0)


def _to_epoch(ts):
    """Candle ts (ISO '2026-09-25T09:15:00+05:30') or epoch -> float seconds."""
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        try:
            return float(ts)
        except Exception:
            return None


class NiftySignalEngine:
    def __init__(self, persist=True):
        self.candles = []          # NIFTY future candles: {o,h,l,c,v,ts}
        self.oi_hist = []          # per-interval: {call_oi, put_oi, pcr, ts}
        self.fut_oi_hist = []      # per-interval: {oi, ts} - NIFTY FUTURES total OI
        self._sig_hist = []        # recent raw signal directions (for hold check)
        self._strength_ema = 0.0   # smoothed strength
        self._last_signal = "WAIT"
        self._persist = persist
        if persist:
            self._load()

    # ---- persistence (so a restart of orderflow_scanner.py mid-session
    # doesn't blank the NIFTY Direction tab back to WAIT/no-candles - see
    # nifty_signal_state.json next to this file, same pattern as
    # levels.py's levels_store.json) ----
    def _load(self):
        if not STORE_FILE.exists():
            return
        try:
            saved = json.loads(STORE_FILE.read_text())
        except Exception:
            return
        # only reuse state from the SAME trading day - a restart the next
        # morning should start fresh, not drag in yesterday's candles/OI
        last_ts = None
        if saved.get("candles"):
            last_ts = saved["candles"][-1].get("ts")
        elif saved.get("oi_hist"):
            last_ts = saved["oi_hist"][-1].get("ts")
        if last_ts is not None:
            saved_ep = _to_epoch(last_ts)
            if saved_ep is None:
                return
            saved_date = datetime.datetime.fromtimestamp(saved_ep).date()
            if saved_date != datetime.date.today():
                return   # stale (previous day) - ignore, start clean
        self.candles = saved.get("candles", [])
        self.oi_hist = saved.get("oi_hist", [])
        self.fut_oi_hist = saved.get("fut_oi_hist", [])
        self._sig_hist = saved.get("sig_hist", [])
        self._strength_ema = saved.get("strength_ema", 0.0)
        self._last_signal = saved.get("last_signal", "WAIT")

    def save(self):
        if not self._persist:
            return
        try:
            STORE_FILE.write_text(json.dumps({
                "candles": self.candles,
                "oi_hist": self.oi_hist,
                "fut_oi_hist": self.fut_oi_hist,
                "sig_hist": self._sig_hist,
                "strength_ema": self._strength_ema,
                "last_signal": self._last_signal,
            }))
        except Exception:
            pass

    # ---- feed data ----
    def push_candle(self, o, h, l, c, v, ts):
        # merge into the current bucket or append; caller passes closed candles
        self.candles.append({"o": o, "h": h, "l": l, "c": c, "v": v, "ts": ts})
        self.candles = self.candles[-120:]   # keep ~6h of 3-min candles

    def push_oi(self, call_oi, put_oi, ts, call_vol=0, put_vol=0):
        pcr = round(put_oi / call_oi, 3) if call_oi else 1.0
        self.oi_hist.append({"call_oi": call_oi, "put_oi": put_oi, "pcr": pcr, "ts": ts,
                              "call_vol": call_vol or 0, "put_vol": put_vol or 0})
        # BUG FIX: this was capped at 120 samples. OI is pushed every ~30s, so
        # 120 samples = only 60 MINUTES of history - but candles keep 120
        # entries = 6 HOURS (full session). Once the app had been running for
        # over an hour, candles older than 60min fell outside the OI window
        # and their buildup bars went blank ("vanished") even though they'd
        # shown real data earlier. Match candles' 6h retention: 6h / 30s = 720.
        self.oi_hist = self.oi_hist[-800:]

    def push_futures_oi(self, oi, ts, price=None, vol=0):
        """Total NIFTY FUTURES open interest + the future's OWN price/volume
        (verified live via futures_probe.py - the same quote call that gives
        us the future's price also returns 'oi' and 'volume'). Price/volume
        here are the FUTURE's own tape, not the spot index - the futures
        score is built entirely from this instrument's own data."""
        if oi is None:
            return
        self.fut_oi_hist.append({"oi": oi, "ts": ts, "price": price, "vol": vol or 0})
        self.fut_oi_hist = self.fut_oi_hist[-800:]   # see push_oi() - match candles' 6h window

    def _oi_window(self, start_ep, end_ep):
        """OI + volume change over a candle's [start,end) time window:
        (d_call_oi, d_put_oi, d_call_vol, d_put_vol). Returns None if we
        have no OI samples covering that window (e.g. the backfilled
        morning candles from before the app started). We only ever invent
        positioning where we actually measured it."""
        if start_ep is None or len(self.oi_hist) < 1:
            return None
        before = None; ins = []
        for o in self.oi_hist:
            ep = _to_epoch(o.get("ts"))
            if ep is None:
                continue
            if ep < start_ep:
                before = o
            elif start_ep <= ep < end_ep:
                ins.append(o)
        if not ins:
            return None
        end_snap = ins[-1]
        start_snap = before or ins[0]
        return (end_snap["call_oi"] - start_snap["call_oi"],
                end_snap["put_oi"] - start_snap["put_oi"],
                max(0, end_snap.get("call_vol", 0) - start_snap.get("call_vol", 0)),
                max(0, end_snap.get("put_vol", 0) - start_snap.get("put_vol", 0)))

    def _futures_oi_window(self, start_ep, end_ep):
        """OI + own-price + own-volume change over a candle's window:
        (d_oi, d_price, d_vol). Same time-align approach as _oi_window."""
        if start_ep is None or len(self.fut_oi_hist) < 1:
            return None
        before = None; ins = []
        for o in self.fut_oi_hist:
            ep = _to_epoch(o.get("ts"))
            if ep is None:
                continue
            if ep < start_ep:
                before = o
            elif start_ep <= ep < end_ep:
                ins.append(o)
        if not ins:
            return None
        end_snap = ins[-1]
        start_snap = before or ins[0]
        d_oi = end_snap["oi"] - start_snap["oi"]
        sp, ep_ = start_snap.get("price"), end_snap.get("price")
        d_price = (ep_ - sp) if (sp is not None and ep_ is not None) else None
        d_vol = max(0, (end_snap.get("vol", 0) or 0) - (start_snap.get("vol", 0) or 0))
        return (d_oi, d_price, d_vol)

    def chart_data(self, n=40, interval_min=3):
        """
        Per-candle data for the two-panel chart: the PRICE candle (top) plus a
        SCORED positioning-buildup bar (bottom, volume-style) made of TWO
        independently-scored halves - a FUTURES score and an OPTIONS score -
        shown as their own segment inside the same bar. No standalone price
        line item anywhere; price only ever appears INSIDE these two scores
        as their momentum/confirmation ingredient.

        FUTURES SCORE - fires only once price (the futures' own momentum
        proxy) AND futures OI are both actually moving; three ingredients,
        all signed by momentum's direction and summed:
          1. MOMENTUM - how far price moved this candle.
          2. OI       - fresh BUILDUP (strong) vs COVERING/UNWINDING (weak):
                 price up   + OI up    -> Long Buildup   (bullish, STRONG)
                 price up   + OI down  -> Short Covering (bullish, weak)
                 price down + OI up    -> Short Buildup  (bearish, STRONG)
                 price down + OI down  -> Long Unwinding (bearish, weak)
          3. VOLUME   - this candle's FUTURES volume vs its own recent
             average (real futures tape volume, not the index's).

        OPTIONS SCORE - fires only once options OI is actually moving; the
        sign comes from OI ITSELF (put-writing minus call-writing) - not
        from price - because options positioning genuinely CAN disagree
        with price, and that disagreement is the real trap signal. Price
        and options volume only confirm/shrink the OI reading's size:
          1. OI       - net put-writing vs call-writing (sets the sign).
          2. MOMENTUM - price agreeing with OI boosts the score, price
             fighting OI shrinks it (confirmation, not sign-setter).
          3. VOLUME   - options volume vs its own recent average, another
             confirmation multiplier.

        Each item:
          {o,h,l,c,v,ts,
           pos_flow,     # SIGNED total = fut_score + opt_score. null = no data yet
           fut_score,    # futures half (momentum+OI+volume, all agreeing)
           opt_score,    # options half (OI-led, momentum+volume confirm it)
           fut_raw, fut_type,   # raw futures OI change + LB/SC/SB/LU label
           pos_raw, pos_type,   # raw options OI flow + PW/CW/PU/CU label
           build_dir,    # 'bull'/'bear'/'' - the bar's NET direction
           divergence}   # futures and options disagree, or options fights price
        """
        if not self.candles:
            return []
        cands = self.candles[-n:]
        win = interval_min * 60
        MOM_SCALE = 35.0
        FUT_VOL_SCALE = 6.0
        OPT_MOM_AGREE, OPT_MOM_FIGHT = 1.2, 0.7
        OPT_VOL_LO, OPT_VOL_HI = 0.7, 1.5

        # ---- first pass: pull each candle's raw OI/price/volume windows,
        # so relative volume can be judged against THIS engine's own recent
        # average (never a fixed threshold) ----
        raw = []
        for c in cands:
            start_ep = _to_epoch(c.get("ts"))
            end_ep = (start_ep + win) if start_ep is not None else None
            dw = self._oi_window(start_ep, end_ep) if end_ep is not None else None
            df = self._futures_oi_window(start_ep, end_ep) if end_ep is not None else None
            raw.append((c, dw, df))

        fut_vols = [df[2] for (_, _, df) in raw if df is not None]
        opt_vols = [dw[2] + dw[3] for (_, dw, _) in raw if dw is not None]
        fut_vol_avg = (sum(fut_vols) / len(fut_vols)) if fut_vols else 0
        opt_vol_avg = (sum(opt_vols) / len(opt_vols)) if opt_vols else 0

        out = []
        for c, dw, df in raw:
            price_pct = (c["c"] - c["o"]) / c["o"] * 100 if c["o"] else 0.0
            price_dir = _sign(price_pct)

            if dw is None and df is None:
                # nothing measured for this candle at all (pre-launch backfill)
                out.append({
                    "o": round(c["o"], 1), "h": round(c["h"], 1),
                    "l": round(c["l"], 1), "c": round(c["c"], 1),
                    "v": int(c.get("v", 0)), "ts": c["ts"],
                    "pos_flow": None, "fut_score": 0, "opt_score": 0,
                    "fut_raw": 0, "fut_type": "", "pos_raw": 0, "pos_type": "",
                    "build_dir": "", "divergence": False,
                })
                continue

            # ---- FUTURES SCORE: momentum + OI + volume, all agreeing ----
            fut_score = 0.0; fut_type = ""; d_fut_oi = None
            if df is not None:
                d_fut_oi, _d_fut_price, d_fut_vol = df
                fut_oi_dir = _sign(d_fut_oi)
                if price_dir != 0 and fut_oi_dir != 0:
                    is_fresh = fut_oi_dir > 0            # OI increasing = buildup = fresh conviction
                    base_weight = 1.4 if is_fresh else 0.6   # buildup=strong, unwind/cover=weak
                    fut_mag = math.log1p(abs(d_fut_oi))
                    rel_vol = _clamp((d_fut_vol / fut_vol_avg) if fut_vol_avg > 0 else 1.0, 0.4, 2.5)
                    mom_part = abs(price_pct) * MOM_SCALE
                    oi_part = fut_mag * base_weight
                    vol_part = (rel_vol - 1.0) * FUT_VOL_SCALE
                    fut_score = price_dir * (mom_part + oi_part + vol_part)
                    fut_type = ("LB" if price_dir > 0 else "SB") if is_fresh else \
                               ("SC" if price_dir > 0 else "LU")

            # ---- OPTIONS SCORE: OI's own direction, confirmed by momentum+volume ----
            opt_score = 0.0; oi_flow = 0; pos_type = ""; opt_fights_price = False
            if dw is not None:
                d_call, d_put, d_call_vol, d_put_vol = dw
                oi_flow = d_put - d_call            # + = put-writing/call-unwinding = bullish
                oi_dir = _sign(oi_flow)
                if oi_dir != 0:
                    opt_mag = math.log1p(abs(oi_flow))
                    mom_conf = 1.0 if price_dir == 0 else (OPT_MOM_AGREE if price_dir == oi_dir else OPT_MOM_FIGHT)
                    d_opt_vol = d_call_vol + d_put_vol
                    vol_conf = _clamp((d_opt_vol / opt_vol_avg) if opt_vol_avg > 0 else 1.0, OPT_VOL_LO, OPT_VOL_HI)
                    opt_score = oi_dir * opt_mag * mom_conf * vol_conf
                    opt_fights_price = (price_dir != 0 and price_dir != oi_dir)
                    acts = {"PW": max(0, d_put), "CU": max(0, -d_call),
                            "CW": max(0, d_call), "PU": max(0, -d_put)}
                    pos_type = max(acts, key=acts.get) if any(acts.values()) else ""

            score = fut_score + opt_score
            combined_dir = _sign(score)
            # divergence: options fighting price (the classic trap), OR the
            # futures and options halves outright disagree with each other
            divergence = opt_fights_price or (
                fut_score != 0 and opt_score != 0 and _sign(fut_score) != _sign(opt_score))

            out.append({
                "o": round(c["o"], 1), "h": round(c["h"], 1),
                "l": round(c["l"], 1), "c": round(c["c"], 1),
                "v": int(c.get("v", 0)), "ts": c["ts"],
                "pos_flow": round(score, 3),
                "fut_score": round(fut_score, 3),
                "opt_score": round(opt_score, 3),
                "fut_raw": round(d_fut_oi) if d_fut_oi is not None else 0,
                "fut_type": fut_type,
                "pos_raw": round(oi_flow),
                "pos_type": pos_type,
                "build_dir": "bull" if combined_dir > 0 else "bear" if combined_dir < 0 else "",
                "divergence": divergence,
            })
        return out

    # ---- the analysis ----
    def evaluate(self):
        if len(self.candles) < 10:
            return self._out("WAIT", 0, "flat", {}, None,
                             "warming up (need ~10 candles)")

        closes = [c["c"] for c in self.candles]
        last = self.candles[-1]

        # ===== PRICE LAYER =====
        e9 = _ema_series(closes, 9)
        e21 = _ema_series(closes, 21)
        vw = _vwap(self.candles)
        r = _rsi(closes, 14)
        price = last["c"]

        price_votes = 0
        price_bits = []
        if e9[-1] > e21[-1]:
            price_votes += 1; price_bits.append("EMA9>EMA21")
        elif e9[-1] < e21[-1]:
            price_votes -= 1; price_bits.append("EMA9<EMA21")
        if price > vw:
            price_votes += 1; price_bits.append("above VWAP")
        elif price < vw:
            price_votes -= 1; price_bits.append("below VWAP")
        # RSI as a tilt (not extreme = trend continuation)
        if r > 55:
            price_votes += 0.5
        elif r < 45:
            price_votes -= 0.5
        price_dir = "bull" if price_votes > 0 else "bear" if price_votes < 0 else "neutral"
        # numeric dial value: price_votes max magnitude is 2.5 (EMA ±1, VWAP ±1, RSI ±0.5)
        price_strength = max(-100, min(100, round(price_votes / 2.5 * 100)))

        # ===== POSITIONING LAYER (OI flow) =====
        pos_dir = "neutral"; pos_bits = []; pos_score = 0
        if len(self.oi_hist) >= 2:
            prev = self.oi_hist[-2]; cur = self.oi_hist[-1]
            d_call = cur["call_oi"] - prev["call_oi"]
            d_put = cur["put_oi"] - prev["put_oi"]
            # call writing (call OI up) = bearish; put writing (put OI up) = bullish
            # unwinding is the reverse. net positioning bias:
            pos_score = 0
            if d_put > 0: pos_score += 1        # put writing = support = bullish
            if d_put < 0: pos_score -= 1         # put unwinding = support gone = bearish
            if d_call > 0: pos_score -= 1        # call writing = resistance = bearish
            if d_call < 0: pos_score += 1        # call unwinding = resistance gone = bullish
            pcr_slope = cur["pcr"] - prev["pcr"]
            if pcr_slope > 0.02: pos_score += 0.5   # PCR rising = bullish
            elif pcr_slope < -0.02: pos_score -= 0.5
            pos_dir = "bull" if pos_score > 0 else "bear" if pos_score < 0 else "neutral"
            if d_put > 0: pos_bits.append("put writing")
            if d_call > 0: pos_bits.append("call writing")
            if d_call < 0: pos_bits.append("call unwinding")
            if d_put < 0: pos_bits.append("put unwinding")
        # numeric dial value: pos_score max magnitude is 2.5 (call/put ±1 each, PCR ±0.5)
        oi_strength = max(-100, min(100, round(pos_score / 2.5 * 100)))

        # ===== FUTURES LAYER (total futures OI - buildup/unwind, same table as
        # chart_data()'s per-candle bars, applied to the latest live sample) =====
        # Futures OI can never independently disagree with price - it only says
        # whether the current price move is fresh conviction (buildup, strong)
        # or people closing out (covering/unwinding, weak). So its direction
        # always follows price_dir; only ITS MAGNITUDE is its own number.
        fut_dir = "neutral"; fut_bits = []; fut_strength = 0
        if len(self.fut_oi_hist) >= 2 and price_votes != 0:
            d_fut = self.fut_oi_hist[-1]["oi"] - self.fut_oi_hist[-2]["oi"]
            price_sign = _sign(price_votes)
            if d_fut != 0:
                is_fresh = d_fut > 0          # OI increasing = buildup = strong conviction
                weight = 1.0 if is_fresh else 0.4   # buildup=strong, unwind/cover=weak
                mag = math.log1p(abs(d_fut))
                norm = max(0.0, min(100.0, mag / 11.0 * 100))   # ~11 ~ log1p(60000) typical big move
                fut_strength = int(round(price_sign * weight * norm))
                fut_strength = max(-100, min(100, fut_strength))
                fut_dir = "bull" if fut_strength > 0 else "bear" if fut_strength < 0 else "neutral"
                if price_sign > 0:
                    fut_bits.append("Long Buildup" if is_fresh else "Short Covering")
                else:
                    fut_bits.append("Short Buildup" if is_fresh else "Long Unwinding")

        # ===== STRENGTH LAYER =====
        # NIFTY index candles have ZERO volume (indices don't trade), so we
        # measure candle STRENGTH by body size + direction instead: strong
        # green bodies = buyers in control, strong red = sellers. Works whether
        # volume is present (futures) or zero (index).
        vol_dir = "neutral"; vol_bits = []
        recent = self.candles[-3:]
        up_body = sum(abs(c["c"] - c["o"]) for c in recent if c["c"] >= c["o"])
        dn_body = sum(abs(c["c"] - c["o"]) for c in recent if c["c"] < c["o"])
        # if real volume exists, weight by it; else pure body strength
        has_vol = any(c.get("v", 0) > 0 for c in recent)
        if has_vol:
            up_body = sum(c["v"] for c in recent if c["c"] >= c["o"])
            dn_body = sum(c["v"] for c in recent if c["c"] < c["o"])
        if up_body > dn_body * 1.3:
            vol_dir = "bull"; vol_bits.append("strong up candles" if not has_vol else "buy volume")
        elif dn_body > up_body * 1.3:
            vol_dir = "bear"; vol_bits.append("strong down candles" if not has_vol else "sell volume")

        # ===== DIVERGENCE / TRAP DETECTION (the core idea) =====
        # price says one way, positioning says the opposite = trap forming.
        # Require a clearly directional price move AND clear opposing OI flow.
        # (volume can be anything - the price-vs-positioning conflict is the tell)
        trap = None
        price_strong = abs(price_votes) >= 1.0   # price directional
        if price_strong and price_dir == "bull" and pos_dir == "bear":
            trap = {"type": "BULL TRAP",
                    "note": "Price rising but option positioning is selling - reversal risk"}
        elif price_strong and price_dir == "bear" and pos_dir == "bull":
            trap = {"type": "BEAR TRAP",
                    "note": "Price falling but option positioning is buying - reversal risk"}

        # ===== FUSE INTO SIGNAL =====
        dirs = {"bull": 0, "bear": 0}
        for d in (price_dir, pos_dir, vol_dir):
            if d in dirs: dirs[d] += 1

        # price leads; needs confirmation from >=1 other layer; trap kills it
        raw_dir = "neutral"
        if not trap:
            if price_dir == "bull" and (pos_dir == "bull" or vol_dir == "bull"):
                raw_dir = "bull"
            elif price_dir == "bear" and (pos_dir == "bear" or vol_dir == "bear"):
                raw_dir = "bear"

        align = dirs["bull"] if raw_dir == "bull" else dirs["bear"] if raw_dir == "bear" else 0
        # strength: layers aligned (each ~28) + price conviction. 2 layers +
        # decent price = ~70, all 3 = ~90+. Clear, tradable magnitudes.
        raw_strength = align * 28 + min(20, abs(price_votes) * 10)
        if raw_dir == "neutral":
            raw_strength = 0

        # ---- HOLD confirmation: direction must persist >=2 intervals ----
        # BUT a trap immediately breaks the hold (positioning flipped = exit).
        self._sig_hist.append(raw_dir)
        self._sig_hist = self._sig_hist[-4:]
        held = (self._sig_hist[-2:] == [raw_dir, raw_dir]) and raw_dir != "neutral"
        if trap:
            self._sig_hist = ["neutral", "neutral"]
            held = False

        # ---- smooth the strength (anti-flicker, but responsive) ----
        self._strength_ema += (raw_strength - self._strength_ema) * 0.5
        strength = round(self._strength_ema)

        # ---- final signal (smooth + confirmed = tradable) ----
        # require BOTH the hold AND a minimum smoothed strength, so a signal
        # only shows when it's genuinely built up - not on one candle.
        MIN_STRENGTH = 45
        if trap:
            signal = "TRAP"
        elif held and raw_dir == "bull" and strength >= MIN_STRENGTH:
            signal = "BUY CALL"
        elif held and raw_dir == "bear" and strength >= MIN_STRENGTH:
            signal = "BUY PUT"
        else:
            signal = "WAIT"
        self._last_signal = signal

        trend = "up" if e9[-1] > e21[-1] else "down"
        note = self._make_note(signal, price_dir, pos_dir, vol_dir, trap)

        layers = {
            "price": {"dir": price_dir, "bits": price_bits, "strength": price_strength,
                      "ema9": round(e9[-1], 1), "ema21": round(e21[-1], 1),
                      "vwap": vw, "rsi": r},
            "positioning": {"dir": pos_dir, "bits": pos_bits, "strength": oi_strength,
                            "pcr": self.oi_hist[-1]["pcr"] if self.oi_hist else 0},
            "futures": {"dir": fut_dir, "bits": fut_bits, "strength": fut_strength,
                        "oi": self.fut_oi_hist[-1]["oi"] if self.fut_oi_hist else None},
            "volume": {"dir": vol_dir, "bits": vol_bits},
        }
        return self._out(signal, strength, trend, layers, trap, note)

    def _make_note(self, signal, pd, od, vd, trap):
        if trap:
            return trap["note"]
        if signal == "BUY CALL":
            return "Price up, confirmed by " + (od == "bull" and "positioning" or "volume") + " - calls favoured"
        if signal == "BUY PUT":
            return "Price down, confirmed by " + (od == "bear" and "positioning" or "volume") + " - puts favoured"
        return "No confirmed signal - price/positioning/volume not aligned. Wait."

    def _out(self, signal, strength, trend, layers, trap, note):
        return {
            "signal": signal, "strength": strength, "trend": trend,
            "layers": layers, "trap": trap, "note": note,
            "spot": round(self.candles[-1]["c"], 1) if self.candles else 0,
        }