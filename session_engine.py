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
SCALE          = 5.0      # (legacy) kept for reference

# ---- per-factor point weights -------------------------------------------
# The score answers "which stock has the most FORCES ALIGNED right now",
# not "which stock has travelled the furthest". That distinction is why
# price used to sit at 95-98%: its old contribution was the RAW move size,
# which accumulates without limit, while book/OI/futures are bounded per
# window - so any big mover drowned them out by construction, no matter the
# weights.
#
# FIX: price's per-window contribution is NORMALISED and CAPPED - a move is
# scored on whether it was a "full-strength" 3-min move (up to PRICE_CAP),
# not on its raw size. Price still GATES direction (and must clear FLAT_PCT),
# but it can no longer run away. All four factors now live in the same
# bounded range, so the score genuinely reflects ALIGNMENT, and breadth
# (several factors agreeing) beats a lone price spike. Tune the weights to
# shift the balance; tune REF_MOVE for what counts as a "full" 3-min move.
REF_MOVE  = 0.30   # a ~0.30% move in 3 min = "full strength" price (=1.0)
PRICE_CAP = 1.6    # a bigger move can't score more than this (no runaway)
PRICE_W = 4.0      # x normalised price strength (0..PRICE_CAP)  [lowered]
BOOK_W  = 5.0      # x order-book agreement (-1..+1)
OI_W    = 5.0      # x options-OI agreement (-1..+1)
FUT_W   = 5.5      # x futures agreement (-1.6..+1.6)  [raised - futures is a strong tell]

# BREADTH BONUS: reward windows where MANY factors agree, so a stock with
# most forces aligned climbs the ranking faster than a lone-price mover and
# shows up at the TOP. The award is multiplied by (1 + ALIGN_BONUS*(aligned-1)):
# 1 factor -> 1.0x, 2 -> 1.3x, 3 -> 1.6x, all 4 -> 1.9x. Set to 0 to disable.
ALIGN_BONUS = 0.30


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

    @staticmethod
    def _fut_detail(fut):
        """The raw ingredients BEHIND the single futures bias number (its own
        price move %, OI change, relative volume, buildup/unwind label) - so
        the breakdown card can say WHY futures voted the way it did, not just
        show the blended score."""
        if not fut:
            return {}
        return {
            "label": fut.get("label", ""),
            "rel_vol": fut.get("rel_vol"),
            "d_oi": fut.get("d_oi"),
            "price_pct": fut.get("price_pct"),
            "day_oi_pct": fut.get("day_oi_pct", 0.0),   # session futures-OI change (watermark)
            "day_oi_chg": fut.get("day_oi_chg", 0),
        }

    def _new_stock(self, ltp, now_hhmm, now_t):
        return {
            "score": 0.0, "dir": "flat", "since": now_hhmm, "day": self.day,
            "last_price": ltp, "day_chg": 0.0,
            "up_pts": 0.0, "dn_pts": 0.0,
            # current (open) window state
            "win_start": now_t, "win_anchor": ltp,
            "book_sum": 0.0, "book_n": 0, "oi_bias": 0.0, "fut_bias": 0.0, "fut_detail": {},
            # last CLOSED window read (for the breakdown card)
            "w_price_pct": 0.0, "w_book_bias": 0.0, "w_oi_bias": 0.0, "w_fut_bias": 0.0,
            "w_dir": "flat", "w_aligned": 0, "w_award": 0.0,
            # exact points-contribution decomposition of w_award, one per
            # factor, computed at window-close (see _close_window) - these
            # SUM to w_award, so the UI can show a true proportional
            # distribution of "which factor contributed how much"
            "w_price_pts": 0.0, "w_book_pts": 0.0, "w_oi_pts": 0.0, "w_fut_pts": 0.0,
            "w_fut_detail": {},
            # CUMULATIVE per-factor points across EVERY closed window today,
            # kept per side. cup_* sum to up_pts, cdn_* sum to dn_pts -> the
            # breakdown card can show how the WHOLE day's score was built
            # (broad vs one-factor), not just the last 3-min window.
            "cup_price": 0.0, "cup_book": 0.0, "cup_oi": 0.0, "cup_fut": 0.0,
            "cdn_price": 0.0, "cdn_book": 0.0, "cdn_oi": 0.0, "cdn_fut": 0.0,
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
            # migrate a record saved before per-factor cumulation existed:
            # attribute its already-earned score to PRICE (the base/gate) so
            # cumulative totals stay consistent with the score from the start;
            # real per-factor detail then accrues on every window from here.
            if "cup_price" not in b:
                b["cup_price"] = b.get("up_pts", 0.0); b["cup_book"] = 0.0
                b["cup_oi"] = 0.0; b["cup_fut"] = 0.0
                b["cdn_price"] = b.get("dn_pts", 0.0); b["cdn_book"] = 0.0
                b["cdn_oi"] = 0.0; b["cdn_fut"] = 0.0

            # --- collect samples for the OPEN window (no scoring yet) ---
            bias = self._book_bias(f)
            if bias is not None:
                b["book_sum"] += bias
                b["book_n"] += 1
            b["oi_bias"] = self._oi_bias(conv_map.get(sym))    # latest OPTIONS OI read
            b["fut_bias"] = self._fut_bias(fut_map.get(sym))   # latest FUTURES buildup read
            b["fut_detail"] = self._fut_detail(fut_map.get(sym))
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
        price_pts = book_pts = oi_pts = fut_pts = 0.0
        if wdir != "flat":
            s = 1 if wdir == "up" else -1
            book_agree = s * book_bias     # +1 fully confirms .. -1 fully fights
            oi_agree = s * oi_bias
            fut_agree = s * fut_bias
            # each factor earns its OWN weighted points (see PRICE_W..FUT_W).
            # price is the NORMALISED, CAPPED move strength (so a monster move
            # can't dominate); the other three are signed agreements - positive
            # when they confirm the move, NEGATIVE when they fight it (a genuine
            # drag). These four SUM to the award, so the breakdown is exact.
            price_str = min(PRICE_CAP, abs(price_pct) / REF_MOVE)
            price_pts = price_str * PRICE_W
            book_pts = book_agree * BOOK_W
            oi_pts = oi_agree * OI_W
            fut_pts = fut_agree * FUT_W
            blend = price_pts + book_pts + oi_pts + fut_pts
            if blend > 0:
                award = blend
            else:
                # net flow fought the move hard enough to cancel it - this
                # window earns nothing (the day score only ever grows) and
                # contributes nothing to the per-factor split
                award = 0.0
                price_pts = book_pts = oi_pts = fut_pts = 0.0

            aligned = (1 + (1 if book_agree > 0.10 else 0)
                         + (1 if oi_agree > 0.10 else 0)
                         + (1 if fut_agree > 0.10 else 0))

            # BREADTH BONUS: the more forces that agreed this window, the more
            # the award is worth - so broadly-aligned stocks accumulate faster
            # and rise to the TOP. Every factor's share is scaled by the same
            # bonus, so they still SUM to the award (breakdown stays exact).
            if award > 0 and ALIGN_BONUS:
                bonus = 1.0 + ALIGN_BONUS * (aligned - 1)
                award *= bonus
                price_pts *= bonus; book_pts *= bonus
                oi_pts *= bonus; fut_pts *= bonus

            if wdir == "up":
                b["up_pts"] += award
                b["cup_price"] = b.get("cup_price", 0.0) + price_pts
                b["cup_book"] = b.get("cup_book", 0.0) + book_pts
                b["cup_oi"] = b.get("cup_oi", 0.0) + oi_pts
                b["cup_fut"] = b.get("cup_fut", 0.0) + fut_pts
            else:
                b["dn_pts"] += award
                b["cdn_price"] = b.get("cdn_price", 0.0) + price_pts
                b["cdn_book"] = b.get("cdn_book", 0.0) + book_pts
                b["cdn_oi"] = b.get("cdn_oi", 0.0) + oi_pts
                b["cdn_fut"] = b.get("cdn_fut", 0.0) + fut_pts

        # remember this closed window for the breakdown card
        b["w_price_pct"] = round(price_pct, 2)
        b["w_book_bias"] = round(book_bias, 2)
        b["w_oi_bias"] = round(oi_bias, 2)
        b["w_fut_bias"] = round(fut_bias, 2)
        b["w_dir"] = wdir
        b["w_aligned"] = aligned
        b["w_award"] = round(award, 1)
        b["w_price_pts"] = round(price_pts, 2)
        b["w_book_pts"] = round(book_pts, 2)
        b["w_oi_pts"] = round(oi_pts, 2)
        b["w_fut_pts"] = round(fut_pts, 2)
        b["w_fut_detail"] = dict(b.get("fut_detail") or {})

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
            # cumulative per-factor contribution for the WINNING side - these
            # four sum to the stock's total score (how the whole day built up)
            if up:
                cum_p = b.get("cup_price", 0.0); cum_b = b.get("cup_book", 0.0)
                cum_o = b.get("cup_oi", 0.0); cum_f = b.get("cup_fut", 0.0)
            else:
                cum_p = b.get("cdn_price", 0.0); cum_b = b.get("cdn_book", 0.0)
                cum_o = b.get("cdn_oi", 0.0); cum_f = b.get("cdn_fut", 0.0)
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
                # points-contribution decomposition - these 4 SUM to w_award,
                # a true proportional "which factor contributed how much" split
                "price_pts": b.get("w_price_pts", 0.0),
                "book_pts": b.get("w_book_pts", 0.0),
                "oi_pts": b.get("w_oi_pts", 0.0),
                "fut_pts": b.get("w_fut_pts", 0.0),
                "award": b.get("w_award", 0.0),
                "fut_detail": b.get("w_fut_detail", {}),   # {label, rel_vol, d_oi, price_pct}
                # LIVE cumulative session futures-OI change (freshest read, for
                # the row-background watermark) - updates every scan, not every 3min
                "day_oi_pct": (b.get("fut_detail") or {}).get("day_oi_pct", 0.0),
                "day_oi_chg": (b.get("fut_detail") or {}).get("day_oi_chg", 0),
                # CUMULATIVE per-factor points (sum to score) - the day's story
                "cum_price_pts": round(cum_p, 1),
                "cum_book_pts": round(cum_b, 1),
                "cum_oi_pts": round(cum_o, 1),
                "cum_fut_pts": round(cum_f, 1),
                "ltp": round(b["last_price"], 1),
                "since": b["since"],
                "reasons": reasons or ["building"],
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out