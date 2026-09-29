"""
ws_feed.py  -  Production 30-level (or 5-level) websocket depth feed.
=====================================================================
Streams live full_d30 market depth from Upstox across multiple websocket
connections, decodes it into our normalized shape, and exposes the latest
book via get_snapshot() - the SAME interface as the REST feed, so the
engine downstream never changes.

CONFIRMED message shape (from live diagnostic):
  feeds[instrument_key].fullFeed.marketFF:
     ltpc:        {ltp, ltt, ltq, cp}                 # last price, prev close
     marketLevel: {bidAskQuote: [ {bidP,bidQ,askP,askQ}, ... ]}   # depth levels
     marketOHLC:  {ohlc: [ {interval:"1d", open,high,low,close,vol}, ... ]}
     vtt:  total traded volume
     tbq:  total buy qty     tsq: total sell qty
     atp:  average traded price

DYNAMIC LEVELS: bidAskQuote may hold 5 OR 30 entries. We read ALL non-empty
entries - whatever arrives - so it works with 5 or 30 automatically. Each
slot may carry bid fields, ask fields, or both; we handle every case.

PRODUCTION-GRADE:
  - one MarketDataStreamerV3 per <=50 instruments (Upstox cap), up to 5 conns
  - each connection runs in its own daemon thread
  - auto-reconnect enabled
  - a single lock-guarded shared snapshot dict, updated on every message
  - never blocks the scanner: get_snapshot() just reads the latest known book
  - defensive: partial/empty books are stored as-is; the engine's half-book
    guard decides whether to score them (never crashes)
"""

import threading
import time


MAX_PER_CONN = 50      # Upstox hard cap: instruments per websocket connection
MAX_CONNS    = 2       # Upstox's REAL concurrent websocket limit is ~2 even on
                       # Plus (connections 2+ get 403 Forbidden). 2 x 50 = 100
                       # stocks streamed live. Set higher only if 403s stop.
                       # connections, your plan allows fewer - lower this. 3 x 50
                       # = 150 stocks streamed. Raise toward 5 only if 403-free.


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _i(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


class WSDepthFeed:
    def __init__(self, access_token, instrument_map):
        """
        instrument_map = {SYMBOL: instrument_key}
        """
        self.token = access_token
        self.instrument_map = instrument_map
        self.key_to_sym = {v: k for k, v in instrument_map.items()}
        self._snap = {}                 # {sym: normalized book}
        self._lock = threading.Lock()
        self._streamers = []
        self._connected = 0
        self._msg_count = 0
        self._started = False

    # -- start all connections (chunked, threaded, STAGGERED) --
    def start(self):
        try:
            import upstox_client
        except ImportError:
            raise SystemExit(
                "\n  WebSocket mode needs the Upstox SDK:\n"
                "    pip install upstox-python-sdk\n")
        import time as _t

        keys = list(self.instrument_map.values())
        chunks = [keys[i:i + MAX_PER_CONN] for i in range(0, len(keys), MAX_PER_CONN)]
        if len(chunks) > MAX_CONNS:
            kept = chunks[:MAX_CONNS]
            dropped = sum(len(c) for c in chunks[MAX_CONNS:])
            print(f"  [WS] {len(chunks)} chunks needed but cap is {MAX_CONNS}; "
                  f"streaming {sum(len(c) for c in kept)} stocks, {dropped} not streamed.")
            chunks = kept

        conf = upstox_client.Configuration()
        conf.access_token = self.token

        # STAGGER: opening many connections at once trips Upstox with 403s.
        # Open them one at a time with a gap so each handshake completes first.
        for idx, chunk in enumerate(chunks):
            self._spawn(upstox_client, conf, chunk, idx)
            _t.sleep(2.0)   # let this connection's handshake finish before the next
        self._started = True
        print(f"  [WS] {len(chunks)} connection(s) started (staggered) for "
              f"{sum(len(c) for c in chunks)} stocks.")

    def _spawn(self, upstox_client, conf, chunk, idx):
        try:
            streamer = upstox_client.MarketDataStreamerV3(
                upstox_client.ApiClient(conf), chunk, "full_d30")
            try:
                streamer.auto_reconnect(True, 5, 10)
            except Exception:
                pass

            def on_message(msg):
                self._on_message(msg)

            def on_open(*a):
                self._connected += 1
                print(f"  [WS] connection {idx} OPEN ({len(chunk)} stocks)")

            def on_error(*a):
                print(f"  [WS] connection {idx} error: {a[:1]}")

            def on_close(*a):
                print(f"  [WS] connection {idx} closed")

            streamer.on("message", on_message)
            for evt, cb in (("open", on_open), ("error", on_error), ("close", on_close)):
                try:
                    streamer.on(evt, cb)
                except Exception:
                    pass

            def _run():
                # The SDK's connect() runs a websocket loop that expects an
                # asyncio event loop registered on THIS thread. A bare thread
                # has none, so the socket connects but never pumps messages.
                # Create + set a fresh loop for this thread before connecting.
                try:
                    import asyncio
                    asyncio.set_event_loop(asyncio.new_event_loop())
                except Exception:
                    pass
                try:
                    streamer.connect()
                except Exception as e:
                    print(f"  [WS] connection {idx} connect() error: {e}")

            th = threading.Thread(target=_run, daemon=True, name=f"ws-{idx}")
            th.start()
            self._streamers.append(streamer)
        except Exception as e:
            print(f"  [WS] connection {idx} failed to start: {e}")

    # -- decode one message into normalized books --
    def _on_message(self, msg):
        try:
            if not isinstance(msg, dict):
                return
            feeds = msg.get("feeds")
            if not feeds:
                return
            self._msg_count += 1
            updates = {}
            for key, feed in feeds.items():
                sym = self.key_to_sym.get(key)
                if not sym:
                    continue
                norm = self._decode_one(feed)
                if norm:
                    updates[sym] = norm
            if updates:
                with self._lock:
                    self._snap.update(updates)
        except Exception:
            # never let a bad message kill the stream
            pass

    def _decode_one(self, feed):
        """Turn one feed entry into our normalized book. Dynamic 5/30 levels."""
        ff = feed.get("fullFeed") or feed.get("ff") or {}
        m = ff.get("marketFF") or {}
        if not m:
            return None

        ltpc = m.get("ltpc") or {}
        ltp = _f(ltpc.get("ltp"))
        cp = _f(ltpc.get("cp"))               # previous close

        # ---- depth: read ALL non-empty levels (works for 5 or 30) ----
        levels = ((m.get("marketLevel") or {}).get("bidAskQuote")) or []
        bids, asks = [], []
        for lv in levels:
            if not lv:
                continue
            # a slot may carry bid fields, ask fields, or both
            bp = lv.get("bidP"); bq = lv.get("bidQ")
            ap = lv.get("askP"); aq = lv.get("askQ")
            if bp is not None and _f(bp) > 0:
                bids.append((_f(bp), _i(bq), 0))
            if ap is not None and _f(ap) > 0:
                asks.append((_f(ap), _i(aq), 0))

        # sort best-first: bids high->low, asks low->high (defensive - order
        # should already be correct, but never trust the wire)
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        # ---- volume + day OHLC ----
        vtt = _i(m.get("vtt"))
        high = low = openp = 0.0
        for o in ((m.get("marketOHLC") or {}).get("ohlc") or []):
            if o.get("interval") == "1d":
                high = _f(o.get("high")); low = _f(o.get("low"))
                openp = _f(o.get("open"))
                break

        return {
            "ltp": ltp,
            "prev_close": cp,
            "volume": vtt,
            "bids": bids,
            "asks": asks,
            "total_buy_qty": _i(m.get("tbq")),
            "total_sell_qty": _i(m.get("tsq")),
            "day_high": high, "day_low": low, "day_open": openp,
            "levels_seen": max(len(bids), len(asks)),
            "ts": time.strftime("%H:%M:%S"),
        }

    # -- the interface the engine uses --
    def get_snapshot(self):
        with self._lock:
            return dict(self._snap)

    def stats(self):
        with self._lock:
            n = len(self._snap)
        return {"stocks": n, "connections": len(self._streamers),
                "messages": self._msg_count}

    def close(self):
        for s in self._streamers:
            try:
                s.disconnect()
            except Exception:
                pass