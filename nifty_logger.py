"""
nifty_logger.py  -  Logs every NIFTY 3-min candle's FULL feature set to a
daily CSV, so you can backtest later: "under what conditions does NIFTY
actually start moving?"

This is a pure side-effect logger - it changes nothing about how the live
app behaves. It's wired into orderflow_scanner.py's scan loop and writes
ONE ROW PER NEW CLOSED CANDLE (not per scan - candles close every 3 min,
scans run every few seconds, so writes are de-duped on the candle's own
timestamp: calling .log() many times for the same still-open candle is a
no-op until a genuinely new candle appears).

Files land in ./nifty_log/nifty_YYYYMMDD.csv - one file per trading day,
so you can just point pandas at nifty_log/*.csv later and concat.

WHAT'S IN EACH ROW (one closed 3-min candle):
  - the candle itself (o,h,l,c)
  - price layer: direction + numeric strength (-100..100) + which bits fired
    (EMA cross, VWAP side, RSI tilt)
  - options OI layer: direction + strength + PCR + raw call/put OI
  - futures OI layer: direction + strength + raw futures OI
  - the fused signal (BUY CALL/BUY PUT/WAIT/TRAP), its strength, trend, and
    the trap type if one fired
  - the 7-lens NIFTY bias verdict + how many lenses agreed
  - VIX, futures price/premium, max pain

WHAT'S DELIBERATELY *NOT* IN THE ROW: any forward-looking "did NIFTY move
after this" column. That's a backtest-time computation (nifty_backtest.py
does it by joining each row against the close N candles later in the same
CSV) - logging it live would just be duplicating the close price under a
different name and inviting hindsight bugs.
"""

import csv
import datetime
from pathlib import Path

LOG_DIR = Path("nifty_log")

FIELDS = [
    "date", "time", "ts",
    "o", "h", "l", "c", "spot",
    "price_dir", "price_strength", "price_bits",
    "oi_dir", "oi_strength", "oi_bits", "pcr", "call_oi", "put_oi",
    "fut_oi", "fut_dir", "fut_strength", "fut_bits",
    "vol_dir",
    "signal", "sig_strength", "trend", "trap_type",
    "verdict", "bias_direction", "bias_strength", "agree", "total",
    "vix", "fut_price", "prem_pct", "max_pain",
]


class NiftyLogger:
    def __init__(self):
        self._last_logged_ts = None

    def _path(self, dt):
        LOG_DIR.mkdir(exist_ok=True)
        return LOG_DIR / f"nifty_{dt:%Y%m%d}.csv"

    def log(self, candle, nifty_signal, nifty_view, nd=None,
             call_oi=None, put_oi=None, fut_oi=None):
        """
        candle       : latest CLOSED candle dict {o,h,l,c,ts,...}
        nifty_signal : NiftySignalEngine.evaluate() output (or None)
        nifty_view   : nifty_bias.analyze_nifty() output (or None)
        nd           : raw NiftyData.fetch() dict (vix, fut_price, pcr,
                       max_pain) - optional, fills a few extra columns
        call_oi/put_oi/fut_oi : latest raw totals - optional, logged as-is
        """
        if not candle or not nifty_signal:
            return
        ts = candle.get("ts")
        if not ts or ts == self._last_logged_ts:
            return   # same (still-open or already-logged) candle - skip
        self._last_logged_ts = ts

        layers = (nifty_signal or {}).get("layers", {}) or {}
        price_l = layers.get("price", {}) or {}
        oi_l = layers.get("positioning", {}) or {}
        fut_l = layers.get("futures", {}) or {}
        vol_l = layers.get("volume", {}) or {}
        trap = nifty_signal.get("trap") or {}
        nv = nifty_view or {}
        nd = nd or {}

        now = datetime.datetime.now()
        row = {
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
            "ts": ts,
            "o": candle.get("o"), "h": candle.get("h"),
            "l": candle.get("l"), "c": candle.get("c"),
            "spot": nifty_signal.get("spot"),
            "price_dir": price_l.get("dir"), "price_strength": price_l.get("strength"),
            "price_bits": "|".join(price_l.get("bits") or []),
            "oi_dir": oi_l.get("dir"), "oi_strength": oi_l.get("strength"),
            "oi_bits": "|".join(oi_l.get("bits") or []),
            "pcr": oi_l.get("pcr") or nd.get("pcr"),
            "call_oi": call_oi, "put_oi": put_oi,
            "fut_oi": fut_oi if fut_oi is not None else fut_l.get("oi"),
            "fut_dir": fut_l.get("dir"), "fut_strength": fut_l.get("strength"),
            "fut_bits": "|".join(fut_l.get("bits") or []),
            "vol_dir": vol_l.get("dir"),
            "signal": nifty_signal.get("signal"), "sig_strength": nifty_signal.get("strength"),
            "trend": nifty_signal.get("trend"), "trap_type": trap.get("type", ""),
            "verdict": nv.get("verdict"), "bias_direction": nv.get("direction"),
            "bias_strength": nv.get("strength"), "agree": nv.get("agree"), "total": nv.get("total"),
            "vix": nd.get("vix"), "fut_price": nd.get("fut_price"),
            "prem_pct": (round((nd["fut_price"] - nd["spot"]) / nd["spot"] * 100, 3)
                         if nd.get("fut_price") and nd.get("spot") else None),
            "max_pain": nd.get("max_pain"),
        }

        path = self._path(now)
        new = not path.exists()
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception as e:
            print(f"  [nifty_logger] write failed: {e}")