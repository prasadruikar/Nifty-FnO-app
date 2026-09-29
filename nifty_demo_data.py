"""
nifty_demo.py  -  DUMMY-DATA demo server for the NIFTY Direction tab.
=====================================================================
Run this INSTEAD of orderflow_scanner.py to see the whole NIFTY Direction
tab (signal card + dials, price/positioning chart, 7-lens verdict) working
end-to-end with FAKE but realistic data - no Upstox token, no market hours,
no internet needed.

It does NOT reimplement any scoring - it feeds made-up candles/OI/futures
OI into the SAME real engines (nifty_signal.NiftySignalEngine,
nifty_bias.analyze_nifty) that the live app uses, so what you see is a true
test of the actual logic, just driven by a scripted scenario instead of
Upstox.

The scenario cycles through phases every few seconds so you can watch the
signal/dials/verdict react and change instead of staring at one frozen
state:

  1. warm-up (flat)              -> WAIT, dials near 0
  2. clean uptrend + confirming OI + futures buildup   -> BUY CALL, dials green
  3. same uptrend but futures unwinding (short covering) -> weaker green futures dial
  4. bull trap (price still up, OI turns bearish)      -> TRAP (amber)
  5. clean downtrend + confirming OI + futures buildup -> BUY PUT, dials red
  6. bear trap (price down, OI turns bullish)           -> TRAP (amber)
  7. choppy / flat                                      -> WAIT
  (loops back to 2)

RUN:
    python nifty_demo.py
    -> opens http://localhost:5062  (same port/UI file as the real app -
       don't run this at the same time as orderflow_scanner.py)

Every ~1.2s of real time = one new simulated 3-min candle, so a full phase
cycle plays out in well under a minute.
"""

import json
import random
import datetime
import threading
import time
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import nifty_signal
import nifty_bias
import nifty_logger

try:
    import config
    PORT = getattr(config, "PORT", 5062)
except Exception:
    PORT = 5062

_UI_FILE = Path(__file__).parent / "orderflow_ui.html"
TICK_SECONDS = 1.2          # real seconds between simulated candles
INTERVAL_MIN = 3            # simulated candle size (matches the real app)

# ---------------------------------------------------------------------
# Scenario: (label, price_drift_pct_per_candle, price_noise_pct,
#            oi_dir, fut_dir, length_in_candles)
#   oi_dir/fut_dir: 'bull' | 'bear' | 'neutral'  (fut 'bull'=buildup up,
#   'bear'=buildup down; direction here means WHICH price move it reinforces
#   is fresh - for 'neutral' OI barely moves)
# ---------------------------------------------------------------------
PHASES = [
    ("Warm-up (flat)",                       0.00, 0.02, "neutral", "neutral", 11),
    ("Clean uptrend + confirming OI + buildup", 0.07, 0.03, "bull",  "bull",    18),
    ("Uptrend continues, futures unwinding",  0.05, 0.03, "bull",  "bear",     8),
    ("Bull trap forming (OI turns bearish)",  0.03, 0.03, "bear",  "bear",     6),
    ("Reversal: clean downtrend + buildup",  -0.08, 0.03, "bear",  "bear",    18),
    ("Bear trap (price down, OI buying)",    -0.03, 0.03, "bull",  "bull",     6),
    ("Choppy consolidation",                  0.00, 0.05, "neutral", "neutral", 14),
]

_state_lock = threading.Lock()
_state = {
    "session": [], "session_all": [], "sector_perf": [], "treemap": [],
    "sharp": [], "nifty_view": None, "nifty_signal": None, "nifty_chart": [],
    "trades": [], "building": [], "running": [], "ranked": [], "at_level": [],
    "setups": [], "conviction_updated": "", "nifty_bias": "", "bridge_alive": False,
    "scan_num": 0, "scan_time": "--:--:--", "next_scan_in": 0, "total": 0,
    "source": "DEMO (dummy data)", "stale": False,
    "error": "DEMO MODE - all data is simulated, not live",
}


def _sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def run_simulation():
    engine = nifty_signal.NiftySignalEngine(persist=False)  # never touch the real app's saved state
    logger = nifty_logger.NiftyLogger()

    sim_time = datetime.datetime.combine(datetime.date.today(), datetime.time(9, 15))
    price = 23150.0
    day_open = price
    prev_close = price - 42.0
    call_oi = 12_000_000
    put_oi = 11_000_000
    fut_oi = 13_300_000
    vix = 13.4
    vix_prev = 13.5

    # fake breadth universe: NIFTY heavyweights + a batch of filler stocks
    fake_syms = [s for s, _ in nifty_bias.HEAVYWEIGHTS] + \
        [f"STK{i}" for i in range(1, 31)]

    phase_i = 0
    phase_left = PHASES[0][5]
    scan_n = 0

    print(f"\n  DEMO running: http://localhost:{PORT}  (Ctrl+C to stop)")
    print("  Feeding scripted candles into the REAL nifty_signal / nifty_bias engines...\n")

    while True:
        label, drift, noise, oi_dir, fut_dir, length = PHASES[phase_i]
        if phase_left <= 0:
            phase_i = (phase_i + 1) % len(PHASES)
            # loop skips the one-time warm-up phase after first pass
            if phase_i == 0:
                phase_i = 1
            label, drift, noise, oi_dir, fut_dir, length = PHASES[phase_i]
            phase_left = length
            print(f"  --- phase: {label} ---")

        # ---- advance price ----
        pct = drift + random.gauss(0, noise)
        o = price
        c = price * (1 + pct / 100)
        h = max(o, c) + abs(random.gauss(0, 1.2))
        l = min(o, c) - abs(random.gauss(0, 1.2))
        price = c

        ts = sim_time.isoformat()
        engine.push_candle(round(o, 2), round(h, 2), round(l, 2), round(c, 2), 0, ts)

        # ---- advance option OI (call/put) ----
        if oi_dir == "bull":
            d_put = random.randint(15000, 40000)
            d_call = -random.randint(5000, 20000)
        elif oi_dir == "bear":
            d_call = random.randint(15000, 40000)
            d_put = -random.randint(5000, 20000)
        else:
            d_put = random.randint(-6000, 6000)
            d_call = random.randint(-6000, 6000)
        put_oi = max(0, put_oi + d_put)
        call_oi = max(0, call_oi + d_call)
        oi_ts = (sim_time + datetime.timedelta(seconds=90)).isoformat()
        engine.push_oi(call_oi, put_oi, oi_ts)

        # ---- advance futures OI ----
        if fut_dir == "bull":
            d_fut = random.randint(20000, 60000)     # buildup (increasing)
        elif fut_dir == "bear":
            d_fut = -random.randint(10000, 40000)    # unwind/cover (decreasing)
        else:
            d_fut = random.randint(-8000, 8000)
        fut_oi = max(0, fut_oi + d_fut)
        engine.push_futures_oi(fut_oi, oi_ts)

        # ---- VIX drifts opposite-ish to price momentum, small noise ----
        vix_prev = vix
        vix += random.gauss(-drift * 0.4, 0.06)
        vix = max(9.0, vix)

        # ---- fake breadth (correlated with the phase's price drift) ----
        stock_flows = {}
        for sym in fake_syms:
            base = drift * random.uniform(0.4, 1.6)
            stock_flows[sym] = {"dprice": round(base + random.gauss(0, 0.15), 2)}

        # ---- run the REAL engines on this fake data ----
        try:
            nifty_signal_out = engine.evaluate() if engine.candles else None
        except Exception as e:
            print("  [demo] evaluate error:", e)
            nifty_signal_out = None
        try:
            nifty_chart = engine.chart_data(100)
        except Exception as e:
            print("  [demo] chart error:", e)
            nifty_chart = []

        fut_price = price * (1 + (0.0006 if fut_dir == "bull" else -0.0004 if fut_dir == "bear" else 0))
        pcr = round(put_oi / call_oi, 3) if call_oi else 1.0
        max_pain = round(price / 50) * 50 - (50 if oi_dir == "bull" else -50 if oi_dir == "bear" else 0)
        try:
            nifty_view = nifty_bias.analyze_nifty(
                spot=round(price, 1), day_open=round(day_open, 1),
                prev_close=round(prev_close, 1), vwap=round((price + day_open) / 2, 1),
                fut_price=round(fut_price, 1),
                oi_data={"pcr": pcr, "max_pain": max_pain},
                vix=round(vix, 2), vix_prev=round(vix_prev, 2),
                stock_flows=stock_flows)
        except Exception as e:
            print("  [demo] bias error:", e)
            nifty_view = None

        # log this closed candle's full feature set (same logger the real
        # app uses) - lets nifty_backtest.py be tested end-to-end on a full
        # synthetic day before ever touching live Upstox logs.
        try:
            if engine.candles:
                nd_like = {"vix": round(vix, 2), "fut_price": round(fut_price, 1),
                           "spot": round(price, 1), "pcr": pcr, "max_pain": max_pain}
                logger.log(engine.candles[-1], nifty_signal_out, nifty_view, nd=nd_like,
                           call_oi=call_oi, put_oi=put_oi, fut_oi=fut_oi)
        except Exception as e:
            print("  [demo] log error:", e)

        scan_n += 1
        with _state_lock:
            _state.update({
                "nifty_view": nifty_view,
                "nifty_signal": nifty_signal_out,
                "nifty_chart": nifty_chart,
                "scan_num": scan_n,
                "scan_time": sim_time.strftime("%H:%M:%S") + " (sim)",
                "next_scan_in": int(TICK_SECONDS),
                "error": f"DEMO MODE - phase: {label}",
            })

        sim_time += datetime.timedelta(minutes=INTERVAL_MIN)
        phase_left -= 1
        time.sleep(TICK_SECONDS)


# ---------------------------------------------------------------------
# Web server (same /data shape + same HTML file as the real app)
# ---------------------------------------------------------------------
def _safe_json(obj):
    import math

    def clean(x):
        if isinstance(x, float):
            return x if math.isfinite(x) else None
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(v) for v in x]
        return x
    try:
        return json.dumps(clean(obj))
    except Exception:
        return json.dumps({"error": "state serialize failed"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/data":
            with _state_lock:
                payload = _safe_json(_state)
            data = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            try:
                html = _UI_FILE.read_bytes()
            except FileNotFoundError:
                html = b"<h1>orderflow_ui.html not found next to nifty_demo.py</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)


def main():
    print("\n" + "=" * 60)
    print("  NIFTY DIRECTION TAB - DUMMY DATA DEMO")
    print("=" * 60)
    print("  No Upstox token / market hours needed - everything is simulated,")
    print("  but run through the REAL scoring engines (nifty_signal.py, nifty_bias.py).")

    t = threading.Thread(target=run_simulation, daemon=True)
    t.start()

    server = HTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n  Open: {url}  -> click the 'NIFTY Direction' tab")
    time.sleep(1.0)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()