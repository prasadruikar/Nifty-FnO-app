"""
session_engine.py  -  3-minute window, PRICE-GATED accumulation ranking.
=====================================================================
THE MODEL (Prasad's design - the correct one):

  Time is sliced into 3-MINUTE WINDOWS. Nothing about a stock's score
  changes during a window - we just quietly collect samples. When the
  window closes, we ask ONE question:

      "Did PRICE move this way in the last 3 minutes,
       AND did the order book / OI push it the same way?"

  PRICE IS THE GATE. If price did not move up in the window, buyers being
  heavy means nothing - no up-score is given. If price did not move down,
  sellers being heavy means nothing. This is exactly the BHARTIARTL case:
  price was falling while buyers were heavy -> under the old model it ranked
  #1 with "BUY CALL", which is wrong. Here, because price fell, it can only
  ever earn a DOWN score, and because buyers (book) fought the fall, even
  that down-score is shrunk. It will NOT rank top.

  Per closed window, the award is:

      award = |price_move_%|  x  confirmation  x  SCALE

    - price_move_% : how far price actually travelled in the 3 min (the base;
                     a 0.5% move is worth far more than a 0.05% wiggle).
    - confirmation : starts at 1.0, BOOSTED when book+OI agree with the price
                     direction, SHRUNK toward ~0 when they fight it.

  The award is added to the stock's running day total for that direction.
  The total only GROWS (never subtracts) -> the ranking is steady and a
  score can only change once every 3 minutes, never on a single scan.

WHY THIS BEATS THE OLD MODEL:
  * Chop: price ends each window near where it started -> ~0% -> ~0 award.
  * Buyers-into-a-falling-stock: price down gates it to a down-score, and the
    fighting book shrinks it. No fake "BUY CALL".
  * Clean one-directional mover: real % move every window, book+OI confirming
    -> big awards stack up fast -> rises to the top. Exactly what we want.

PERSISTED to session_store.json (keyed by date) - restart keeps the day.
"""

import json
import time
import datetime
from pathlib import Path

STORE = Path("session_store.json")

WINDOW_SECONDS = 180      # 3 minutes - the score can only change this often
FLAT_PCT       = 0.05     # price move smaller than this (%) = noise, no award
SCALE          = 5.0      # overall award scale (score has no fixed ceiling)


def _sign(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class SessionEngine:
    def __init__(self):
        self.book = {}
        self.day = datetime.date.today().isoformat()
        self._load()

    # ---------- persistence ----------
    def _load(self):
        if STORE.exists():
            try:
                data = json.loads(STORE.read_text())
                # only reload today's data AND only the current format (v:3)
                if data.get("day") == self.day and data.get("v") == 3:
                    self.book = data.get("book", {})
                    print(f"  [session] reloaded {len(self.book)} stocks from today")
                else:
                    print("  [session] fresh start (new day or format)")
            except Exception:
                self.book = {}

    def save(self):
        try:
            STORE.write_text(json.dumps({"day": self.day, "v": 3, "book": self.book}))
        except Exception:
            pass

    # ---------- helpers ----------
    @staticmethod
    def _book_bias(f):
        """Signed order-book imbalance for ONE sample, in -1..+1.
        + = buyers heavier, - = sellers heavier. We AVERAGE this across the
        whole 3-min window, so a momentary spike can't dominate."""
        bq = float(f.get("bid_qty", 0) or 0)
        aq = float(f.get("ask_qty", 0) or 0)
        if bq + aq <= 0:
            return None
        return (bq - aq) / (bq + aq)

    @staticmethod
    def _oi_bias(conv):
        """Signed OI bias in ~-1..+1. + = call/up building, - = put/down."""
        if not conv:
            return 0.0
        strength = _clamp(conv.get("conviction", 0) / 50.0, 0.0, 1.0)
        return (1 if conv.get("direction") == "up" else -1) * strength

    @staticmethod
    def _fut_bias(fut):
        """Signed FUTURES bias, ~-1.6..+1.6 (stock_futures.py's composite of
        futures price move + OI buildup/unwind + relative volume). Not the
        same thing as _oi_bias above - that's OPTIONS OI (from the bridge),
        this is the stock's own single-stock FUTURE, same lens the NIFTY
        Direction tab already applies to the index."""
        if not fut:
            return 0.0
        return _clamp(fut.get("bias", 0.0), -1.6, 1.6)

    def _new_stock(self, ltp, now_hhmm, now_t):
        return {
            "score": 0.0, "dir": "flat", "since": now_hhmm, "day": self.day,
            "last_price": ltp, "day_chg": 0.0,
            "up_pts": 0.0, "dn_pts": 0.0,
            # current (open) window state
            "win_start": now_t, "win_anchor": ltp,
            "book_sum": 0.0, "book_n": 0, "oi_bias": 0.0, "fut_bias": 0.0,
            # last CLOSED window read (for the breakdown card)
            "w_price_pct": 0.0, "w_book_bias": 0.0, "w_oi_bias": 0.0, "w_fut_bias": 0.0,
            "w_dir": "flat", "w_aligned": 0, "w_award": 0.0,
        }

    # ---------- the update (called every scan; commits only every 3 min) ----------
    def update(self, flows, conv_map, fut_map=None):
        fut_map = fut_map or {}
        now_t = time.time()
        now_hhmm = datetime.datetime.now().strftime("%H:%M")
        today = datetime.date.today().isoformat()
        if today != self.day:
            self.day = today
            self.book = {}

        for sym, f in flows.items():
            ltp = float(f.get("ltp", 0) or 0)
            if ltp <= 0:
                continue
            b = self.book.get(sym)
            if not b or "win_start" not in b or "fut_bias" not in b:
                # new stock OR an old-format record -> start fresh, never crash
                b = self._new_stock(ltp, now_hhmm, now_t)
                self.book[sym] = b

            # --- collect samples for the OPEN window (no scoring yet) ---
            bias = self._book_bias(f)
            if bias is not None:
                b["book_sum"] += bias
                b["book_n"] += 1
            b["oi_bias"] = self._oi_bias(conv_map.get(sym))    # latest OPTIONS OI read
            b["fut_bias"] = self._fut_bias(fut_map.get(sym))   # latest FUTURES buildup read
            b["last_price"] = ltp
            b["day_chg"] = float(f.get("day_chg", 0) or 0)    # NSE-style % change

            # --- has the 3-minute window closed? ---
            if now_t - b["win_start"] >= WINDOW_SECONDS:
                self._close_window(b, ltp, now_t)

        return self._ranked()

    def _close_window(self, b, ltp, now_t):
        """Score the window that just ended: PRICE gates, book+OI+futures confirm."""
        anchor = b["win_anchor"] or ltp
        price_pct = (ltp - anchor) / anchor * 100 if anchor else 0.0
        book_bias = (b["book_sum"] / b["book_n"]) if b["book_n"] else 0.0
        oi_bias = b["oi_bias"]
        fut_bias = b.get("fut_bias", 0.0)

        # direction comes from PRICE and nothing else
        if price_pct > FLAT_PCT:
            wdir = "up"
        elif price_pct < -FLAT_PCT:
            wdir = "down"
        else:
            wdir = "flat"

        award = 0.0
        aligned = 0
        if wdir != "flat":
            s = 1 if wdir == "up" else -1
            book_agree = s * book_bias     # +1 fully confirms .. -1 fully fights
            oi_agree = s * oi_bias
            fut_agree = s * fut_bias
            # confirmation: 1.0 baseline, book+OI+futures push it up when they
            # agree, down toward ~0 when they fight the price move. Futures
            # gets a weight between book (fastest, noisiest) and options OI
            # (slowest, most deliberate) - it's price+OI+volume combined, a
            # meaningfully strong confirming signal on its own.
            conf = 1.0 + book_agree * 1.4 + oi_agree * 0.6 + fut_agree * 0.8
            conf = _clamp(conf, 0.12, 3.6)
            award = abs(price_pct) * conf * SCALE
            aligned = (1 + (1 if book_agree > 0.10 else 0)
                         + (1 if oi_agree > 0.10 else 0)
                         + (1 if fut_agree > 0.10 else 0))
            if wdir == "up":
                b["up_pts"] += award
            else:
                b["dn_pts"] += award

        # remember this closed window for the breakdown card
        b["w_price_pct"] = round(price_pct, 2)
        b["w_book_bias"] = round(book_bias, 2)
        b["w_oi_bias"] = round(oi_bias, 2)
        b["w_fut_bias"] = round(fut_bias, 2)
        b["w_dir"] = wdir
        b["w_aligned"] = aligned
        b["w_award"] = round(award, 1)

        # commit direction + score (whichever side owns the day)
        if b["up_pts"] >= b["dn_pts"]:
            b["dir"] = "up"; b["score"] = b["up_pts"]
        else:
            b["dir"] = "down"; b["score"] = b["dn_pts"]

        # open the next window from here
        b["win_start"] = now_t
        b["win_anchor"] = ltp
        b["book_sum"] = 0.0
        b["book_n"] = 0

    # ---------- ranked output ----------
    def _ranked(self):
        out = []
        for sym, b in self.book.items():
            if b["score"] < 1.0 or b["dir"] == "flat":
                continue
            up = b["dir"] == "up"
            pp = b["w_price_pct"]; bb = b["w_book_bias"]; oo = b["w_oi_bias"]; ff = b.get("w_fut_bias", 0.0)
            reasons = []
            # price is always the reason it's ranked (it's the gate)
            if (up and pp > 0) or (not up and pp < 0):
                reasons.append(f"price {'up' if up else 'down'} {abs(pp):.2f}% in 3min")
            s = 1 if up else -1
            if s * bb > 0.10:
                reasons.append("buyers confirmed" if up else "sellers confirmed")
            elif s * bb < -0.10:
                reasons.append("book fighting it")   # honest: order flow disagrees
            if s * oo > 0.10:
                reasons.append("OI supports")
            if s * ff > 0.10:
                reasons.append("futures buildup confirms")
            elif s * ff < -0.10:
                reasons.append("futures unwinding against it")
            out.append({
                "sym": sym,
                "score": round(b["score"], 1),
                "direction": b["dir"],
                "day_chg": round(b.get("day_chg", 0), 2),
                "aligned": b["w_aligned"],                 # 1-4 forces agreeing last window
                # breakdown card values reflect the LAST CLOSED 3-min window
                "price_score": pp,                         # % price moved in the window
                "book_score": bb,                          # avg bid/ask bias -1..+1
                "fut_score": ff,                           # futures price+OI+volume bias -1.6..+1.6
                "oi_score": oo,                            # OI bias -1..+1
                "ltp": round(b["last_price"], 1),
                "since": b["since"],
                "reasons": reasons or ["building"],
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out