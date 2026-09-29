"""
rank_engine.py  -  Additive move-ranking engine (score only rises).
=====================================================================
PHILOSOPHY (Prasad's design - it's the right one):
  - NO penalties, ever. A stock's score only goes UP while its setup is alive.
  - Points accrue at each stage: setup forms, absorption persists, OI agrees,
    volume builds, and - the big one - PRICE MOVES our predicted way.
  - A stock that built a setup AND then moved our way earns the MOST points,
    so it naturally OVERTAKES the still-building ones and rises to the top.
    The mover isn't penalized-down others; it earns its way up.
  - Because score never decreases, the list is naturally STEADY - things climb
    and hold their rank, nothing flickers out. A stock only leaves when its
    move is completely finished: it RETIRES (clean exit), never gets docked.

TWO PANELS the engine feeds:
  BUILDING - setup loading, price hasn't run yet (get ready, enter early)
  RUNNING  - price now moving our way, move confirmed & live (enter into strength)
  A stock graduates BUILDING -> RUNNING when price starts moving our direction.

STAGES (each ADDS to the running score, kept in _acc per stock):
  +absorption (persist/intensify)  the coil loading
  +conviction (OI same direction)  smart money agrees
  +pressure   (imbalance/aggression) book tilt now
  +volume     (fuel)               participation
  +MOVE       (price our way)      the proof - biggest points, sends it to top
  +run        (move continues)     fully confirmed, running
"""

import time


class MoveRanker:
    """
    Stateful, additive. Call update(sym, flow, conv) each scan.
    Holds a running score per stock that only rises while alive, and retires
    a stock when its move is clearly finished.
    """

    def __init__(self):
        # per-stock accumulator:
        # {sym: {score, direction, peak_price, start_price, absorb_scans,
        #        moved, run_scans, last_seen_scan, dead_scans, stage, reasons}}
        self._acc = {}
        self._scan = 0

    # ---- helpers ----
    @staticmethod
    def _dir_from(flow, conv):
        ab = flow.get("absorption", "none")
        if ab == "buyers":
            return "up"
        if ab == "sellers":
            return "down"
        if conv and conv.get("direction") in ("up", "down"):
            return conv["direction"]
        return flow.get("direction", "flat")

    def update_all(self, flows, conv_map):
        """Process one scan across all stocks. Returns (building, running) lists."""
        self._scan += 1
        seen = set()

        for sym, flow in flows.items():
            seen.add(sym)
            conv = conv_map.get(sym)
            self._update_one(sym, flow, conv)

        # age out stocks not seen this scan (data gap) - count toward death slowly
        for sym, a in list(self._acc.items()):
            if sym not in seen:
                a["dead_scans"] = a.get("dead_scans", 0) + 1
                if a["dead_scans"] >= 6:
                    del self._acc[sym]

        # build the two panels from live accumulators
        building, running = [], []
        for sym, a in self._acc.items():
            if a["score"] < 15:
                continue
            item = {
                "sym": sym,
                "score": round(a["score"]),
                "direction": a["direction"],
                "stage": a["stage"],
                "ltp": a.get("ltp", 0),
                "dprice": round(a.get("move_pct", 0), 2),
                "absorb_scans": a.get("absorb_scans", 0),
                "run_scans": a.get("run_scans", 0),
                "reasons": a.get("reasons", [])[:4],
                "confluence": a.get("confluence", False),
            }
            if a["stage"] == "RUNNING":
                running.append(item)
            else:
                building.append(item)

        # both sorted by score desc - movers (higher score) sit on top
        running.sort(key=lambda x: x["score"], reverse=True)
        building.sort(key=lambda x: x["score"], reverse=True)
        # ONE unified list (TradeFinder-style): everything ranked together,
        # movers naturally on top because they earned the most points.
        unified = sorted(running + building, key=lambda x: x["score"], reverse=True)
        return building[:20], running[:20], unified[:25]

    def _update_one(self, sym, flow, conv):
        direction = self._dir_from(flow, conv)
        up = (direction == "up")
        ltp = flow.get("ltp", 0) or 0

        a = self._acc.get(sym)
        # start / reset accumulator if new, or if direction flipped hard
        if not a or (a["direction"] != direction and direction != "flat"):
            a = {
                "score": 0.0, "direction": direction,
                "start_price": ltp, "peak_price": ltp,
                "absorb_scans": 0, "run_scans": 0, "moved": False,
                "dead_scans": 0, "stage": "FORMING", "reasons": [],
                "move_pct": 0.0, "ltp": ltp, "confluence": False,
            }
            self._acc[sym] = a

        a["dead_scans"] = 0
        a["ltp"] = ltp
        a["direction"] = direction
        reasons = []
        gained = 0.0  # points ADDED this scan (never negative)

        # =============================================================
        # WEIGHTED SCORING - factors are NOT equal. Points reflect how
        # strongly each factor predicts a real move (highest to lowest):
        #   THE MOVE (price our way)     : up to +18/scan  (strongest proof)
        #   ABSORPTION persisting        : up to +7/scan   (the pre-move coil)
        #   CONFLUENCE bonus (both agree): +10 one-time     (2 signals aligned)
        #   VOLUME surge                 : +3              (fuel, confirms real)
        #   AGGRESSION (initiative now)  : +3
        #   OI conviction alone          : up to +12 topup  (early, slow, weakest solo)
        #   IMBALANCE (book tilt)        : +2
        # =============================================================

        # ---- ABSORPTION (the coil) - HIGH weight, accrues while it holds ----
        ab = flow.get("absorption", "none")
        ab_aligned = (up and ab == "buyers") or (not up and ab == "sellers")
        if ab_aligned:
            a["absorb_scans"] += 1
            gained += 5                                   # each held scan (was 3)
            if a["absorb_scans"] >= 3:
                gained += 2                               # sustained coil bonus
            reasons.append(f"Absorbing {a['absorb_scans']} scans")

        # ---- OI CONVICTION alone - LOWER weight (early but slow) ----
        if conv and conv.get("direction") == direction:
            target = min(12, conv.get("conviction", 0) * 0.12)   # capped lower (was 20)
            cur_conv = a.get("_conv_pts", 0)
            add = max(0, target - cur_conv)
            if add > 0:
                gained += add
                a["_conv_pts"] = target
                reasons.append(f"OI positioned {direction}")

        # ---- CONFLUENCE bonus - HIGH (two independent signals agree) ----
        if a["absorb_scans"] >= 2 and a.get("_conv_pts", 0) >= 8 and not a.get("_conf_paid"):
            gained += 10                                  # one-time big bonus
            a["_conf_paid"] = True
            reasons.append("Confluence: OI + absorption agree")

        # ---- PRESSURE (imbalance + aggression) - MEDIUM ----
        imb = flow.get("imbalance", 0)
        imb_dir = imb if up else -imb
        if imb_dir > 0.15:
            gained += 2
        aggr = flow.get("aggression", "none")
        if (up and aggr == "buyers") or (not up and aggr == "sellers"):
            gained += 3
            reasons.append("Aggressive " + ("buying" if up else "selling"))

        # ---- VOLUME (fuel) - MEDIUM ----
        if flow.get("dvol", 0) > 0:
            gained += 3

        # ---- THE MOVE (price our way) - HIGHEST weight, the proof ----
        start = a["start_price"] or ltp
        if start:
            move_pct = ((ltp - start) / start * 100) if up else ((start - ltp) / start * 100)
        else:
            move_pct = 0
        a["move_pct"] = move_pct
        prev_peak_pct = ((a["peak_price"] - start) / start * 100) if up else ((start - a["peak_price"]) / start * 100)

        if (up and ltp > a["peak_price"]) or (not up and ltp < a["peak_price"]):
            a["peak_price"] = ltp

        new_peak_pct = ((a["peak_price"] - start) / start * 100) if up else ((start - a["peak_price"]) / start * 100)
        move_gain = max(0, new_peak_pct - max(0, prev_peak_pct))
        if move_gain > 0.02:
            gained += min(18, move_gain * 14)             # STRONGEST points (was 15/12)
            a["moved"] = True
            a["run_scans"] += 1
            reasons.append(f"Moving {direction} +{new_peak_pct:.2f}%")

        if a["run_scans"] >= 2 and move_gain > 0.02:
            gained += 3                                   # sustained run

        # ---- accrue (never subtract) ----
        a["score"] = min(100, a["score"] + gained)
        if reasons:
            a["reasons"] = reasons

        # confluence: absorbed AND OI agreed
        a["confluence"] = a["absorb_scans"] >= 2 and a.get("_conv_pts", 0) >= 10

        # ---- STAGE + retirement ----
        # RUNNING once price has moved our way meaningfully and is near peak.
        moved_enough = new_peak_pct >= 0.25
        near_peak = abs(ltp - a["peak_price"]) / (a["peak_price"] or 1) < 0.004
        if moved_enough and (near_peak or move_gain > 0.02):
            a["stage"] = "RUNNING"
        elif a["absorb_scans"] >= 2 or (conv and conv.get("direction") == direction):
            a["stage"] = "BUILDING"
        else:
            a["stage"] = "FORMING"

        # RETIRE: a runner whose move is clearly over (pulled back far from peak,
        # no fresh movement, absorption gone) exits cleanly - not penalized.
        pulled_back = (new_peak_pct - move_pct) > max(0.4, new_peak_pct * 0.5)
        stalling = move_gain <= 0.02 and not ab_aligned
        if a["stage"] == "RUNNING" and pulled_back and stalling:
            a["_retire"] = a.get("_retire", 0) + 1
            if a["_retire"] >= 4:      # confirmed done over a few scans
                del self._acc[sym]
                return
        else:
            a["_retire"] = 0