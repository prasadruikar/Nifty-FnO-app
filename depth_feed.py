"""
depth_feed.py  -  The data-source abstraction layer.
=====================================================================
This is the piece that makes the scanner source-agnostic. The engine
NEVER talks to Upstox directly - it talks to a DepthFeed. Two feeds
implement the same tiny interface, so you can switch REST <-> websocket
by changing ONE line in config.py, with zero changes to the scan logic.

    DepthFeed (abstract)
      |-- RestDepthFeed        (polling, 5-level, any plan)
      |-- WebSocketDepthFeed   (streaming, up to 30-level, Upstox Plus)

Both return the SAME normalized snapshot shape, so downstream code
(absorption, imbalance, ranking) is identical regardless of source:

    {
      "SYMBOL": {
        "ltp": float,                  # last traded price
        "volume": int,                 # cumulative day volume
        "bids": [(price, qty, orders), ...],   # best-first, up to N levels
        "asks": [(price, qty, orders), ...],   # best-first, up to N levels
        "total_buy_qty": int,          # exchange-reported total buy qty
        "total_sell_qty": int,         # exchange-reported total sell qty
        "ts": "ISO timestamp",
      },
      ...
    }
"""

import time
import requests

UPSTOX_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
_MAX_PER_CALL = 500   # Upstox allows up to 500 instruments per quote call


# =====================================================================
# Abstract interface
# =====================================================================
class DepthFeed:
    """Every feed must implement get_snapshot(). That's the whole contract."""

    def get_snapshot(self):
        """Return {symbol: normalized_depth_dict}. Best bid/ask first."""
        raise NotImplementedError

    def close(self):
        pass


def _normalize_depth(sym, q):
    """Turn one Upstox quote entry into our normalized shape."""
    depth = q.get("depth", {}) or {}
    buy = depth.get("buy", []) or []
    sell = depth.get("sell", []) or []
    bids = [(float(l.get("price", 0)), int(l.get("quantity", 0)), int(l.get("orders", 0)))
            for l in buy if l.get("price")]
    asks = [(float(l.get("price", 0)), int(l.get("quantity", 0)), int(l.get("orders", 0)))
            for l in sell if l.get("price")]
    ltp = float(q.get("last_price", 0) or 0)
    ohlc = q.get("ohlc", {}) or {}
    ohlc_close = float(ohlc.get("close", 0) or 0)
    day_open = float(ohlc.get("open", 0) or 0)
    # --- PREVIOUS CLOSE (the source of the old "+0.00%" bug) ---------------
    # Upstox market-quote's ohlc.close is TODAY's *running* close, which during
    # live trading equals the last price -> (ltp - close) = 0 -> every stock
    # showed +0.00%. The reliable field is `net_change` = change vs previous
    # close. So prev_close = ltp - net_change. We fall back gracefully.
    net_change = float(q.get("net_change", 0) or 0)
    prev_close = 0.0
    if net_change and ltp:
        prev_close = ltp - net_change                       # #1 - the correct one
    elif ohlc_close and abs(ohlc_close - ltp) > 1e-6:
        prev_close = ohlc_close                             # #2 - close != ltp -> real prev close
    elif day_open:
        prev_close = day_open                               # #3 - last-resort intraday ref
    # % change vs previous close - exactly like the NSE website shows
    day_chg = round((ltp - prev_close) / prev_close * 100, 2) if prev_close else 0.0
    return {
        "ltp": ltp,
        "volume": int(q.get("volume", 0) or 0),
        "bids": bids,
        "asks": asks,
        "total_buy_qty": int(q.get("total_buy_quantity", 0) or 0),
        "total_sell_qty": int(q.get("total_sell_quantity", 0) or 0),
        "prev_close": prev_close,
        "day_open": day_open,
        "day_chg": day_chg,
        "ts": q.get("timestamp", ""),
    }


# =====================================================================
# REST implementation (polling)  -  works on any Upstox plan, 5-level
# =====================================================================
class RestDepthFeed(DepthFeed):
    def __init__(self, access_token, instrument_map):
        """
        instrument_map = {SYMBOL: instrument_key}
        e.g. {"RELIANCE": "NSE_EQ|INE002A01018", ...}
        """
        self.token = access_token
        self.instrument_map = instrument_map
        # reverse map: upstox returns keys like "NSE_EQ:RELIANCE" in the response
        self._keys = list(instrument_map.values())
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })

    def get_snapshot(self):
        out = {}
        # chunk into <=500-instrument calls (all F&O fits in one, but be safe)
        for i in range(0, len(self._keys), _MAX_PER_CALL):
            chunk = self._keys[i:i + _MAX_PER_CALL]
            params = {"instrument_key": ",".join(chunk)}
            try:
                r = self.session.get(UPSTOX_QUOTE_URL, params=params, timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json().get("data", {}) or {}
            except Exception:
                continue
            # response keys look like "NSE_EQ:RELIANCE"; the part after ":" is the symbol
            for resp_key, q in data.items():
                sym = q.get("symbol") or resp_key.split(":")[-1]
                out[sym] = _normalize_depth(sym, q)
        return out


# =====================================================================
# WebSocket implementation (streaming)  -  Upstox Plus, up to 30-level
# =====================================================================
# NOTE: This is a working skeleton wired to Upstox's V3 market-data feed.
# It keeps a background thread updating a shared snapshot dict, so
# get_snapshot() just returns the latest known book - same interface as REST.
# Upstox streams binary protobuf; decoding needs their .proto (MarketDataFe
# V3). To keep this dependency-light and swappable, the decode step is
# isolated in _decode(). When you enable Plus + websocket, install
# `upstox-python-sdk` which bundles the protobuf, and this class uses its
# MarketDataStreamerV3 under the hood.
class WebSocketDepthFeed(DepthFeed):
    def __init__(self, access_token, instrument_map,
                 depth_mode="full_d30", max_per_conn=50):
        self.token = access_token
        self.instrument_map = instrument_map
        self.depth_mode = depth_mode
        self.max_per_conn = max_per_conn
        self._snapshot = {}
        self._lock = __import__("threading").Lock()
        self._streamers = []
        self._start()

    def _start(self):
        try:
            import upstox_client
        except ImportError:
            raise SystemExit(
                "\n  WebSocket mode needs the Upstox SDK:\n"
                "    pip install upstox-python-sdk\n"
                "  Or set DATA_SOURCE='rest' in config.py.\n")

        keys = list(self.instrument_map.values())
        # Upstox caps instruments per connection; split across connections.
        conf = upstox_client.Configuration()
        conf.access_token = self.token

        for i in range(0, len(keys), self.max_per_conn):
            chunk = keys[i:i + self.max_per_conn]
            streamer = upstox_client.MarketDataStreamerV3(
                upstox_client.ApiClient(conf),
                instrumentKeys=chunk,
                mode=self.depth_mode,   # "full" (5) or "full_d30" (30, Plus)
            )
            streamer.auto_reconnect(True, 5, 5)
            streamer.on("message", self._on_message)
            # connect in a background thread so it doesn't block
            th = __import__("threading").Thread(target=streamer.connect, daemon=True)
            th.start()
            self._streamers.append(streamer)

    def _on_message(self, message):
        """Upstox SDK already decodes protobuf -> dict. Normalize + store."""
        try:
            feeds = (message or {}).get("feeds", {})
            for inst_key, feed in feeds.items():
                sym = self._sym_for_key(inst_key)
                if not sym:
                    continue
                norm = self._normalize_ws(sym, feed)
                if norm:
                    with self._lock:
                        self._snapshot[sym] = norm
        except Exception:
            pass

    def _sym_for_key(self, inst_key):
        for sym, key in self.instrument_map.items():
            if key == inst_key:
                return sym
        return None

    def _normalize_ws(self, sym, feed):
        # V3 full feed carries marketFF -> marketLevel -> bidAskQuote (list of levels)
        try:
            ff = feed.get("fullFeed", {}) or feed.get("ff", {})
            market = ff.get("marketFF", {}) or ff.get("marketOHLC", {})
            level = (ff.get("marketFF", {}) or {}).get("marketLevel", {})
            quotes = level.get("bidAskQuote", []) or []
            bids, asks = [], []
            for lv in quotes:
                bp = float(lv.get("bidP", lv.get("bp", 0)) or 0)
                bq = int(lv.get("bidQ", lv.get("bq", 0)) or 0)
                ap = float(lv.get("askP", lv.get("ap", 0)) or 0)
                aq = int(lv.get("askQ", lv.get("aq", 0)) or 0)
                if bp:
                    bids.append((bp, bq, 0))
                if ap:
                    asks.append((ap, aq, 0))
            ltpc = (ff.get("marketFF", {}) or {}).get("ltpc", {})
            ltp = float(ltpc.get("ltp", 0) or 0)
            vol = int((ff.get("marketFF", {}) or {}).get("vtt", 0) or 0)
            return {
                "ltp": ltp, "volume": vol,
                "bids": bids, "asks": asks,
                "total_buy_qty": sum(q for _, q, _ in bids),
                "total_sell_qty": sum(q for _, q, _ in asks),
                "ts": time.strftime("%H:%M:%S"),
            }
        except Exception:
            return None

    def get_snapshot(self):
        with self._lock:
            return dict(self._snapshot)

    def close(self):
        for s in self._streamers:
            try:
                s.disconnect()
            except Exception:
                pass


# =====================================================================
# Factory - reads config and returns the right feed. ONE switch point.
# =====================================================================
def make_feed(source, access_token, instrument_map, cfg):
    if source == "websocket":
        return WebSocketDepthFeed(
            access_token, instrument_map,
            depth_mode=getattr(cfg, "WS_DEPTH_MODE", "full_d30"),
            max_per_conn=getattr(cfg, "WS_MAX_PER_CONN", 50))
    # default: REST
    return RestDepthFeed(access_token, instrument_map)