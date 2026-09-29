"""
orderflow_scanner.py  -  Live F&O orderflow pressure scanner.
=====================================================================
SETUP (once):
  1. pip install requests
  2. Fill API_KEY / API_SECRET / REDIRECT_URI in config.py
EACH MORNING:
  3. python upstox_auth.py         (log in, saves today's token)
RUN:
  4. python orderflow_scanner.py   (opens http://localhost:5060)

WHAT IT DOES
  Every few seconds it pulls the order book (depth) for all F&O stocks
  through a swappable feed (REST now, websocket later - one config switch),
  runs the orderflow engine (imbalance / absorption / big-player /
  aggression / spoof), ranks stocks by flow pressure, and serves a live
  dashboard of "where a move is brewing right now". Logs every scan to CSV.

RESILIENCE (this reads LIVE money data, so it's built to not fall over):
  - token missing/expired  -> clear message, tells you to re-auth
  - network blip           -> skips the scan, keeps running, retries
  - one bad stock          -> skipped, rest still scanned
  - feed returns nothing    -> holds last good state, marks data stale
  - market closed          -> warns, still runs if you insist (for testing)
  - Ctrl+C                  -> clean shutdown
"""

import os, sys, csv, json, time, datetime, threading, webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import requests  # noqa: F401  (used by feeds)
except ImportError:
    sys.exit("\n  Run:  pip install requests\n")

import config
import upstox_auth
import instruments
import depth_feed
import flow_engine
import levels as levels_mod
import merge as merge_mod
import rank_engine as rank_mod
import sharp_move as sharp_mod
import ws_feed as ws_mod
import session_engine as session_mod
import sectors as sectors_mod
import nifty_bias as nifty_mod
import nifty_data as nifty_data_mod
import nifty_signal as nifty_sig_mod
import nifty_logger as nifty_log_mod
import stock_futures as stock_fut_mod

MARKET_START = (9, 15)
MARKET_END   = (15, 30)

_state = {
    "session": [], "session_all": [], "sector_perf": [], "treemap": [], "sharp": [], "nifty_view": None, "nifty_signal": None, "nifty_chart": [], "trades": [], "building": [], "running": [], "ranked": [], "at_level": [], "setups": [],
    "conviction_updated": "", "nifty_bias": "", "bridge_alive": False,
    "scan_num": 0, "scan_time": "--:--:--",
    "next_scan_in": 0, "total": 0, "source": config.DATA_SOURCE,
    "stale": False, "error": "",
}
_lock = threading.Lock()
_prev_snapshot = {}          # last scan's depth, for scan-to-scan signals
_levels = levels_mod.LevelStore()   # persistent multi-day absorption levels
_merge = merge_mod.MergeEngine()    # combines conviction + levels, steadily
_coil = rank_mod.MoveRanker()      # additive move ranker (building + running)
_sharp_smoother = sharp_mod.ScoreSmoother(ease=0.35, hold=5)  # anti-flicker
_session = session_mod.SessionEngine()   # day-long accumulation ranking (primary)
_sectors = None               # Sectors fetcher (set in main)
_sector_cache = [[], []]      # [sector_perf, treemap]
_sector_last = [0.0]
_nifty_data = None            # NiftyData fetcher (set up in main when token present)
_nifty_cache = [None]         # last fetched NIFTY context (throttled)
_nifty_last = [0.0]           # timestamp of last NIFTY fetch
_nifty_sig = nifty_sig_mod.NiftySignalEngine()  # candle-by-candle NIFTY signal
_nifty_candle_last = [0.0]    # last candle refresh time
_nifty_logger = nifty_log_mod.NiftyLogger()   # logs every closed candle for backtesting
_nifty_oi_totals = [None, None, None]         # cache: [call_oi, put_oi, fut_oi] between 30s refreshes
_stock_fut = None             # StockFutures fetcher (set up in main when token present)
_stock_fut_cache = [{}]       # last fetched {SYMBOL: fut_score_dict} (throttled)
_stock_fut_last = [0.0]       # timestamp of last stock-futures fetch
_last_flow = {}              # {sym: latest flow signal} - for search lookup
_UI_FILE = Path(__file__).parent / "orderflow_ui.html"


# ---------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------
_FIELDS = ["date", "time", "symbol", "ltp", "score", "direction", "story",
           "imbalance", "imb_ratio", "bid_qty", "ask_qty",
           "best_bid", "best_ask", "best_bid_qty", "best_ask_qty",
           "big_side", "bid_qpo", "ask_qpo", "absorption", "spoof",
           "aggression", "dvol", "dprice"]


def save_csv(rows, ts):
    d = Path(config.DATA_DIR)
    d.mkdir(exist_ok=True)
    path = d / f"flow_{ts:%Y%m%d}.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({"date": f"{ts:%Y-%m-%d}", "time": f"{ts:%H:%M:%S}",
                        **{k: r.get(k, "") for k in _FIELDS[3:]}})


def mkt_open():
    t = datetime.datetime.now()
    return MARKET_START <= (t.hour, t.minute) < MARKET_END


# ---------------------------------------------------------------------
# The scan loop
# ---------------------------------------------------------------------
def scanner_main(feed):
    global _prev_snapshot, _last_flow, _merge, _coil
    while True:
        try:
            t0 = datetime.datetime.now()
            scan_n = _state["scan_num"] + 1
            print(f"  [{t0:%H:%M:%S}] Scan #{scan_n} ...", end=" ", flush=True)

            # --- pull depth from whatever feed is active ---
            try:
                snap = feed.get_snapshot()
            except Exception as e:
                print(f"feed error: {e} | holding last state")
                with _lock:
                    _state["stale"] = True
                    _state["error"] = f"feed error: {e}"
                time.sleep(config.POLL_SECONDS)
                continue

            if not snap:
                # show WS stats so we can see if messages are even arriving
                ws_info = ""
                if hasattr(feed, "stats"):
                    st = feed.stats()
                    ws_info = f" (ws msgs={st.get('messages',0)} conns={st.get('connections',0)})"
                print(f"empty snapshot | holding last state{ws_info}")
                with _lock:
                    _state["stale"] = True
                    _state["error"] = f"waiting for depth data{ws_info}"
                time.sleep(config.POLL_SECONDS)
                continue

            # --- run the engine per stock (scan-to-scan) ---
            today = t0.strftime("%Y-%m-%d")
            results = []
            for sym, book in snap.items():
                try:
                    prev = _prev_snapshot.get(sym)
                    sig = flow_engine.analyze(sym, book, prev)
                    if not sig:
                        continue

                    # feed the stock's book size into its own-normal baseline
                    if sig.get("book_value"):
                        _levels.update_book_norm(sym, sig["book_value"])

                    # if real absorption happened AND it's unusual for THIS
                    # stock (money-normalized), record/reinforce a level
                    if sig["absorption"] != "none" and sig["absorb_value"] > 0:
                        if _levels.is_unusual(sym, sig["absorb_value"]):
                            side = "support" if sig["absorption"] == "buyers" else "resistance"
                            _levels.record_absorption(
                                sym, sig["absorb_price"], side,
                                sig["absorb_value"], today)

                    # attach the nearest strong level + a pullback-entry flag
                    near = _levels.nearest_level(sym, sig["ltp"], max_dist_pct=1.5)
                    if near:
                        sig["near_level"] = near["price"]
                        sig["near_side"] = near["side"]
                        sig["near_strength"] = near["strength"]
                        sig["near_dist"] = near["dist_pct"]
                        sig["near_tests"] = near["tests"]
                        sig["near_days"] = near.get("alive_days", 1)
                        sig["near_value"] = near["value"]
                        # PULLBACK ENTRY: price is drifting back onto a proven
                        # level (within 0.4%) - the setup you described
                        if abs(near["dist_pct"]) <= 0.4:
                            sig["at_level"] = True
                            sig["level_note"] = (
                                f"At {near['side']} {near['price']:g} "
                                f"(tested {near['tests']}x, {near.get('alive_days',1)}d old)")
                    results.append(sig)
                except Exception:
                    continue  # one bad stock never kills the scan

            # capture the previous depth for the sharp-move engine BEFORE we
            # overwrite _prev_snapshot (sharp needs scan-to-scan depth deltas)
            prev_depth_for_sharp = dict(_prev_snapshot)
            _prev_snapshot = snap
            _levels.refresh(today)
            _levels.save()
            ranked = flow_engine.rank(results)

            # keep every stock's latest flow for the search bar
            with _lock:
                _last_flow = {r["sym"]: r for r in results}

            # build the STEADY combined conviction setups (conviction + levels)
            merged = _merge.build(_last_flow, _levels)

            # --- THE UNIFIED MOVE RANKING (orderflow + OI + coil + price) ---
            # read conviction bridge for OI positioning, then rank every stock
            # by how LOADED it is for a move (coiling > already-moved).
            conv_map = {}
            try:
                bridge_path = Path("conviction_bridge.json")
                if bridge_path.exists():
                    bj = json.loads(bridge_path.read_text())
                    conv_map = bj.get("stocks", {})
            except Exception:
                pass
            move_building, move_running, move_unified = _coil.update_all(_last_flow, conv_map)

            # === THE SHARP-MOVE RANKING (bid/ask microstructure) ===========
            # This is the primary list now: ranks stocks by ability to make a
            # sharp one-sided move, reading the order book (execution flow,
            # thin opposition, depth imbalance, spread) + OI + price alignment.
            # Uses the raw depth snapshots (current + previous), 5 or 30 levels.
            sharp_ranked = sharp_mod.rank_sharp(snap, prev_depth_for_sharp, conv_map)
            # smooth to prevent flicker from the high-frequency websocket feed
            sharp_ranked = _sharp_smoother.apply(sharp_ranked)

            # === PRIMARY RANKING: alignment accumulation (book+price+OI) ====
            # feed the session engine: ltp, book qtys (from depth), so it can
            # score order-book bias + price move + OI alignment per scan.
            sess_flows = {}
            for sym, book in snap.items():
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                sess_flows[sym] = {
                    "ltp": book.get("ltp", 0),
                    "day_chg": book.get("day_chg", 0),   # % vs prev close (NSE-style)
                    "bid_qty": sum(q for _, q, _ in bids),
                    "ask_qty": sum(q for _, q, _ in asks),
                }
            # === per-stock FUTURES score (price + OI + volume), throttled ~45s -
            # OI/volume don't need 4s freshness, and it's one bulk call across
            # the whole universe (see stock_futures.py), same pattern as the
            # NIFTY futures/OI refresh below. ===
            try:
                now_ts3 = time.time()
                if _stock_fut and (now_ts3 - _stock_fut_last[0] >= 45 or not _stock_fut_cache[0]):
                    _stock_fut_cache[0] = _stock_fut.refresh()
                    _stock_fut_last[0] = now_ts3
            except Exception as _e:
                print(f"  [stock-fut] refresh error: {_e}")

            session_ranked = _session.update(sess_flows, conv_map, _stock_fut_cache[0])
            _session.save()

            # === SECTOR HEATMAP + F&O TREEMAP (throttled ~30s) ==============
            try:
                now_sec = time.time()
                if now_sec - _sector_last[0] >= 30 or not _sector_cache[0]:
                    if _sectors:
                        _sector_cache[0] = _sectors.fetch_sector_perf()
                    # treemap from all stocks' day_chg (from the depth snapshot)
                    all_chg = {sym: book.get("day_chg", 0)
                               for sym, book in snap.items() if book.get("day_chg") is not None}
                    _sector_cache[1] = sectors_mod.Sectors.build_treemap([], all_chg)
                    _sector_last[0] = now_sec
            except Exception:
                pass

            # === NIFTY market-direction (multi-lens, all Upstox data) ======
            try:
                # breadth + heavyweights from the SESSION engine's day moves
                # (real per-stock % move today, all stocks - richer than dprice)
                flow_for_breadth = {r["sym"]: {"dprice": r.get("day_move", 0)}
                                    for r in session_ranked}
                # also include flat/unranked stocks from last_flow for full breadth
                for sym, f in _last_flow.items():
                    if sym not in flow_for_breadth:
                        # approximate day move from open tracking in session book
                        bb = _session.book.get(sym, {})
                        flow_for_breadth[sym] = {"dprice": bb.get("day_move", 0)}
                # fetch live NIFTY context from Upstox, throttled to ~15s
                # (direction doesn't shift every 4s; saves API calls)
                now_ts = time.time()
                if _nifty_data and (now_ts - _nifty_last[0] >= 15 or not _nifty_cache[0]):
                    _nifty_cache[0] = _nifty_data.fetch()
                    _nifty_last[0] = now_ts
                nd = _nifty_cache[0] or {}
                nifty_view = nifty_mod.analyze_nifty(
                    spot=nd.get("spot", 0), day_open=nd.get("open", 0),
                    prev_close=nd.get("prev_close", 0), vwap=nd.get("vwap", 0),
                    fut_price=nd.get("fut_price", 0), oi_data=nd,
                    vix=nd.get("vix", 0), vix_prev=nd.get("vix_prev", 0),
                    stock_flows=flow_for_breadth)
            except Exception:
                nifty_view = None

            # === NIFTY candle-by-candle SIGNAL (price vs positioning + trap) ==
            import traceback as _tb
            if _nifty_data is None:
                # _nifty_data never got built (see the "NIFTY data : DISABLED"
                # line at startup for why) - say so periodically instead of
                # staying completely silent while the tab sits empty forever.
                if int(time.time()) % 60 == 0:
                    print("  [nifty] _nifty_data is None - NIFTY Direction tab will stay empty "
                          "(see 'NIFTY data' line printed at startup for the cause)")
            try:
                now_ts2 = time.time()
                # refresh candles + OI every 30s (3-min candles don't need faster)
                if _nifty_data and (now_ts2 - _nifty_candle_last[0] >= 30 or not _nifty_sig.candles):
                    candles = _nifty_data.fetch_candles(interval_min=3)
                    if candles:
                        # rebuild the engine's candle history from fresh data
                        _nifty_sig.candles = candles[-60:]
                    call_oi, put_oi = _nifty_data.fetch_nifty_oi_totals()
                    if call_oi or put_oi:
                        _nifty_sig.push_oi(call_oi, put_oi, str(now_ts2))
                        _nifty_oi_totals[0], _nifty_oi_totals[1] = call_oi, put_oi
                    _fp, _foi = _nifty_data.fetch_futures_snapshot()
                    if _foi is not None:
                        _nifty_sig.push_futures_oi(_foi, str(now_ts2))
                        _nifty_oi_totals[2] = _foi
                    _nifty_candle_last[0] = now_ts2
            except Exception:
                print("  [nifty] fetch error:"); _tb.print_exc()

            # score + chart in SEPARATE guards, so a failure in one NEVER blanks
            # the other, and the real error is printed instead of swallowed.
            try:
                nifty_signal = _nifty_sig.evaluate() if _nifty_sig.candles else None
            except Exception:
                print("  [nifty] evaluate error:"); _tb.print_exc(); nifty_signal = None
            try:
                # log every CLOSED candle's full feature set for backtesting
                # (nifty_backtest.py). De-duped internally on candle ts, so
                # calling this every scan is fine - it only writes on a new candle.
                if _nifty_sig.candles:
                    _nifty_logger.log(
                        _nifty_sig.candles[-1], nifty_signal, nifty_view,
                        nd=_nifty_cache[0],
                        call_oi=_nifty_oi_totals[0], put_oi=_nifty_oi_totals[1],
                        fut_oi=_nifty_oi_totals[2])
            except Exception:
                print("  [nifty] log error:"); _tb.print_exc()
            try:
                # 100 candles (~5h) so there's real history to drag/pan through
                # in the UI, not just today's most recent hour or two
                nifty_chart = _nifty_sig.chart_data(100) if _nifty_sig.candles else []
                if _nifty_sig.candles and not nifty_chart:
                    print(f"  [nifty] chart empty despite {len(_nifty_sig.candles)} candles")
            except Exception:
                print("  [nifty] chart error:"); _tb.print_exc(); nifty_chart = []
            try:
                # persist engine state to disk so a restart mid-session (crash,
                # code reload, deploy) resumes from here instead of blanking
                # the NIFTY Direction tab back to WAIT/no-candles.
                _nifty_sig.save()
            except Exception:
                print("  [nifty] save error:"); _tb.print_exc()

            # --- log + publish ---
            if ranked:
                save_csv(ranked, t0)
            elapsed = (datetime.datetime.now() - t0).total_seconds()
            wait = max(config.POLL_SECONDS - elapsed, 1)

            n_sharp = len(sharp_ranked)
            n_aligned = sum(1 for r in sharp_ranked if r.get("aligned"))
            at_level = [r for r in ranked if r.get("at_level")]
            print(f"{len(snap)} books | {n_sharp} sharp | {n_aligned} aligned | {elapsed:.1f}s")

            with _lock:
                _state.update({
                    "session": session_ranked[:40],  # PRIMARY: day accumulation
                    "session_all": session_ranked,   # full list for search
                    "sector_perf": _sector_cache[0], # sectoral index % changes
                    "treemap": _sector_cache[1],     # F&O stocks by sector, sized
                    "sharp": sharp_ranked[:30],  # microstructure (reference)
                    "nifty_view": nifty_view,    # multi-lens market direction
                    "nifty_signal": nifty_signal,  # candle-by-candle signal + trap
                    "nifty_chart": nifty_chart,    # per-candle price + positioning
                    "trades": move_unified,
                    "building": move_building,
                    "running": move_running,
                    "ranked": ranked[:40],
                    "at_level": at_level[:20],
                    "setups": merged["setups"],
                    "conviction_updated": merged["conviction_updated"],
                    "nifty_bias": merged["nifty_bias"],
                    "bridge_alive": merged["bridge_alive"],
                    "scan_num": scan_n,
                    "scan_time": t0.strftime("%H:%M:%S"),
                    "next_scan_in": int(wait),
                    "total": len(ranked),
                    "stale": False,
                    "error": "",
                })

            for s in range(int(wait), 0, -1):
                with _lock:
                    _state["next_scan_in"] = s
                time.sleep(1)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"\n  Scan error: {e} | retry in 5s")
            time.sleep(5)


# ---------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------
def _safe_json(obj):
    """json.dumps but NaN/Infinity -> null, so the browser's JSON.parse never
    chokes on the payload (a single NaN made the WHOLE UI go blank)."""
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
            with _lock:
                payload = _safe_json(_state)
            self._json(payload)
        elif self.path.startswith("/stock"):
            # /stock?sym=RELIANCE -> that stock's LIVE flow + proven levels
            from urllib.parse import parse_qs, urlparse
            sym = parse_qs(urlparse(self.path).query).get("sym", [""])[0].upper().strip()
            with _lock:
                flow = _last_flow.get(sym)
            lvls = _levels.levels_for(sym)
            found = flow is not None or bool(lvls)
            self._json(json.dumps({
                "sym": sym, "found": found,
                "flow": flow, "levels": lvls,
                "scan_time": _state.get("scan_time", ""),
            }))
        elif self.path.startswith("/levels"):
            # /levels?sym=RELIANCE  -> all proven levels for that stock
            sym = ""
            if "?" in self.path:
                from urllib.parse import parse_qs, urlparse
                sym = parse_qs(urlparse(self.path).query).get("sym", [""])[0].upper()
            lvls = _levels.levels_for(sym) if sym else []
            self._json(json.dumps({"sym": sym, "levels": lvls}))
        else:
            try:
                html = _UI_FILE.read_bytes()
            except FileNotFoundError:
                html = b"<h1>orderflow_ui.html not found next to the script</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    def _json(self, payload):
        data = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    print("\n" + "=" * 60)
    print("  F&O ORDERFLOW SCANNER")
    print("=" * 60)

    # 1. token
    token = upstox_auth.load_token()
    if not token:
        sys.exit("\n  No access token found.\n  Run first:  python upstox_auth.py\n")

    # 2. market-hours warning (still allow running for testing)
    launched = os.environ.get("COCKPIT_LAUNCH") == "1"
    now = datetime.datetime.now()
    if not mkt_open() and not launched:
        print(f"\n  Time {now:%H:%M} is outside market hours (9:15-15:30).")
        print("  Depth/absorption signals need LIVE trading to be meaningful.")
        print("  Press Enter to run anyway (for testing), Ctrl+C to exit.")
        try:
            input()
        except KeyboardInterrupt:
            return

    # 3. resolve F&O universe -> instrument keys
    try:
        inst_map = instruments.resolve_universe(config)
    except Exception as e:
        sys.exit(f"\n  Could not build instrument map: {e}\n")
    if not inst_map:
        sys.exit("\n  No instruments resolved. Check config.UNIVERSE.\n")
    with _lock:
        _state["total"] = len(inst_map)

    # 4. build the feed - production websocket (30-level) or REST fallback
    print(f"\n  Data source : {config.DATA_SOURCE.upper()}")
    print(f"  Universe    : {len(inst_map)} F&O stocks")
    # NIFTY direction data fetcher (spot, VIX, future, options) from Upstox
    global _nifty_data
    try:
        _nifty_data = nifty_data_mod.NiftyData(token)
        print("  NIFTY data  : Upstox (spot, VIX, futures, options)")
    except Exception as e:
        # BUG FIX: this used to swallow the exception silently, so if
        # NiftyData() ever failed to construct, the NIFTY Direction tab
        # would just stay empty forever with NOTHING printed to explain
        # why (no "[nifty] ..." lines at all, since fetch_candles() etc.
        # never even got called). Now we say exactly what broke.
        print(f"  NIFTY data  : DISABLED - NiftyData() failed to init: {e}")
        print("                (NIFTY Direction tab will stay empty until this is fixed)")
        _nifty_data = None
    # per-stock FUTURES score (price + OI + volume) - same lens as NIFTY's
    # futures layer, applied to the whole F&O stock universe
    global _stock_fut
    try:
        _stock_fut = stock_fut_mod.StockFutures(token, inst_map.keys())
        print(f"  Stock futures: Upstox ({len(_stock_fut.fut_keys)} of {len(inst_map)} stocks mapped)")
    except Exception as e:
        print(f"  Stock futures: DISABLED - StockFutures() failed to init: {e}")
        _stock_fut = None
    global _sectors
    try:
        _sectors = sectors_mod.Sectors(token)
        print("  Sectors     : Upstox sectoral indices + F&O treemap")
    except Exception:
        _sectors = None
    try:
        if config.DATA_SOURCE == "websocket":
            feed = ws_mod.WSDepthFeed(token, inst_map)
            feed.start()
            # give the stream a moment to connect and receive first books
            time.sleep(3)
        else:
            print(f"  Poll every  : {config.POLL_SECONDS}s")
            feed = depth_feed.make_feed(config.DATA_SOURCE, token, inst_map, config)
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"\n  Could not start feed: {e}\n")

    # 5. start scanner + server threads
    Path(config.DATA_DIR).mkdir(exist_ok=True)
    t = threading.Thread(target=scanner_main, args=(feed,), daemon=True)
    t.start()
    server = HTTPServer(("localhost", config.PORT), Handler)
    st = threading.Thread(target=server.serve_forever, daemon=True)
    st.start()

    url = f"http://localhost:{config.PORT}"
    print(f"\n  Web UI      : {url}")
    print(f"  Data logs   : ./{config.DATA_DIR}/")
    if not launched:
        print("  Opening browser... (Ctrl+C to stop)\n")
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    else:
        print("  (cockpit will open the unified dashboard)\n")

    try:
        while t.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Stopping...")
        try:
            feed.close()
        except Exception:
            pass
        server.shutdown()
        print("  Stopped cleanly.\n")


if __name__ == "__main__":
    main()