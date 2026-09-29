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

import math
import datetime


def _sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


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
    def __init__(self):
        self.candles = []          # NIFTY future candles: {o,h,l,c,v,ts}
        self.oi_hist = []          # per-interval: {call_oi, put_oi, pcr, ts}
        self._sig_hist = []        # recent raw signal directions (for hold check)
        self._strength_ema = 0.0   # smoothed strength
        self._last_signal = "WAIT"

    # ---- feed data ----
    def push_candle(self, o, h, l, c, v, ts):
        # merge into the current bucket or append; caller passes closed candles
        self.candles.append({"o": o, "h": h, "l": l, "c": c, "v": v, "ts": ts})
        self.candles = self.candles[-120:]   # keep ~6h of 3-min candles

    def push_oi(self, call_oi, put_oi, ts):
        pcr = round(put_oi / call_oi, 3) if call_oi else 1.0
        self.oi_hist.append({"call_oi": call_oi, "put_oi": put_oi, "pcr": pcr, "ts": ts})
        self.oi_hist = self.oi_hist[-120:]

    def _oi_window(self, start_ep, end_ep):
        """OI change (d_call, d_put) over a candle's [start,end) time window.
        Returns None if we have no OI samples covering that window (e.g. the
        backfilled morning candles from before the app started). We only ever
        invent positioning where we actually measured it."""
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
                end_snap["put_oi"] - start_snap["put_oi"])

    def chart_data(self, n=40, interval_min=3):
        """
        Per-candle data for the two-panel chart: the PRICE candle (top) plus a
        SCORED positioning-buildup bar (bottom, volume-style).

        The buildup is scored with the SAME idea as the stock ranker: price is
        the reference, and positioning that AGREES with the candle's price move
        is a high-conviction build (bar boosted); positioning that FIGHTS the
        price move is a divergence/trap (bar shrunk + flagged amber). Raw OI
        deltas are log-compressed so one strike-roll can't dwarf the panel.

        Each item:
          {o,h,l,c,v,ts,
           pos_flow,     # SIGNED scored buildup (+bullish / -bearish). null = no OI yet
           pos_raw,      # raw signed OI flow (put-writing minus call-writing)
           pos_type,     # 'PW'/'CW'/'PU'/'CU' dominant OI action this candle
           build_dir,    # 'bull'/'bear'/'' from positioning
           divergence}   # price candle direction disagrees with positioning
        """
        if not self.candles:
            return []
        cands = self.candles[-n:]
        win = interval_min * 60
        out = []
        for c in cands:
            start_ep = _to_epoch(c.get("ts"))
            end_ep = (start_ep + win) if start_ep is not None else None
            dw = self._oi_window(start_ep, end_ep) if end_ep is not None else None

            price_pct = (c["c"] - c["o"]) / c["o"] * 100 if c["o"] else 0.0

            if dw is None:
                # no positioning measured for this candle (pre-launch backfill)
                out.append({
                    "o": round(c["o"], 1), "h": round(c["h"], 1),
                    "l": round(c["l"], 1), "c": round(c["c"], 1),
                    "v": int(c.get("v", 0)), "ts": c["ts"],
                    "pos_flow": None, "pos_raw": 0, "pos_type": "",
                    "build_dir": "", "divergence": False,
                })
                continue

            d_call, d_put = dw
            oi_flow = d_put - d_call            # + = bullish build, - = bearish build
            # log-compress magnitude (like the book log-scale), keep sign
            mag = _sign(oi_flow) * math.log1p(abs(oi_flow))
            # SAME price-gated confirmation as the stock engine:
            pos_dir = _sign(oi_flow)
            price_dir = _sign(price_pct)
            if pos_dir != 0 and price_dir != 0:
                conf = 1.4 if pos_dir == price_dir else 0.5   # agree boosts, fight shrinks
            else:
                conf = 1.0
            score = mag * conf                  # signed scored buildup (bar height source)
            divergence = (pos_dir != 0 and price_dir != 0 and pos_dir != price_dir)

            acts = {"PW": max(0, d_put), "CU": max(0, -d_call),
                    "CW": max(0, d_call), "PU": max(0, -d_put)}
            pos_type = max(acts, key=acts.get) if any(acts.values()) else ""
            out.append({
                "o": round(c["o"], 1), "h": round(c["h"], 1),
                "l": round(c["l"], 1), "c": round(c["c"], 1),
                "v": int(c.get("v", 0)), "ts": c["ts"],
                "pos_flow": round(score, 3), "pos_raw": round(oi_flow),
                "pos_type": pos_type,
                "build_dir": "bull" if pos_dir > 0 else "bear" if pos_dir < 0 else "",
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

        # ===== POSITIONING LAYER (OI flow) =====
        pos_dir = "neutral"; pos_bits = []
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
            "price": {"dir": price_dir, "bits": price_bits,
                      "ema9": round(e9[-1], 1), "ema21": round(e21[-1], 1),
                      "vwap": vw, "rsi": r},
            "positioning": {"dir": pos_dir, "bits": pos_bits,
                            "pcr": self.oi_hist[-1]["pcr"] if self.oi_hist else 0},
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