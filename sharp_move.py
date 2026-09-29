"""
sharp_move.py  -  Rank stocks by ability to make a SHARP one-sided move.
=====================================================================
The real trader logic for reading the order book BEFORE a strong move.
Works on 5-level or 30-level depth (richer with 30). Every factor returns
a labelled contribution so the UI can show WHY a stock ranks where it does.

THE BID/ASK SIGNALS (what predicts a sharp move), heaviest first:

  1. EXECUTION IMBALANCE  - are trades hitting the ASK (buyers lifting) far
     more than the BID, or vice versa? Inferred from price move + volume
     between scans. The strongest short-term predictor of direction.

  2. THIN OPPOSITION (the air pocket) - is the side price wants to move INTO
     nearly empty? Thick bids + thin asks above = loaded to pop UP, because
     there's little supply to absorb it. Most-missed, very powerful signal.
     Richer with 30 levels (we see the whole wall, not just 5).

  3. DEPTH IMBALANCE (touch-weighted) - total bid vs ask quantity, weighted
     so orders near the touch count far more than deep ones.

  4. SPREAD SIGNAL - a tight spread + rising volume = coiling agreement.
     A suddenly widening spread = imbalance, often just before a fast move.

  5. VOLUME SURGE - rising participation = the move is real, not a drift.

CONFIRMATION MULTIPLIER (the alignment edge):
  When the book says UP, AND OI is positioned UP, AND price is moving UP -
  all three independent lenses agree - that's the highest-probability sharp
  move. Alignment boosts the score; disagreement holds it back (never a
  hard penalty - it just doesn't get the boost).

OUTPUT per stock:
  score (0-10 decimal), direction, aligned (bool), and a `factors` list of
  {name, points, note, dir} so the UI shows each factor's contribution.
"""


def _levels_qty(levels):
    return sum(q for _, q, _ in levels) if levels else 0


def _touch_weighted(levels):
    """Quantity weighted so nearest-touch levels dominate (1, .7, .5, ...)."""
    tot = 0.0
    for i, (_, q, _) in enumerate(levels or []):
        w = max(0.15, 1.0 - i * 0.12)
        tot += q * w
    return tot


def analyze_sharp(sym, now, prev, conv):
    """
    now / prev = normalized depth snapshots (bids/asks best-first).
    conv       = {"direction","conviction"} from StockRanker, or None.
    Returns a scored dict or None if the book is unusable.
    """
    bids = now.get("bids", [])
    asks = now.get("asks", [])
    # DEFENSIVE: Upstox full_d30 sometimes drops the ask side. Never score a
    # half-book - it would produce garbage. Require both sides present.
    if not bids or not asks:
        return None

    ltp = float(now.get("ltp", 0) or 0)
    if ltp <= 0:
        return None
    best_bid_p, best_bid_q, _ = bids[0]
    best_ask_p, best_ask_q, _ = asks[0]
    if best_bid_p <= 0 or best_ask_p <= 0:
        return None

    bid_qty = _levels_qty(bids)
    ask_qty = _levels_qty(asks)
    tw_bid = _touch_weighted(bids)
    tw_ask = _touch_weighted(asks)

    factors = []      # each: {name, points, note, dir}
    up_pts = 0.0
    dn_pts = 0.0

    # ---- 1. EXECUTION IMBALANCE (inferred) - weight up to 3.5 ----------
    # price moved with volume between scans -> which side was aggressor.
    exec_pts = 0.0; exec_dir = "flat"; exec_note = "no clear execution flow"
    if prev:
        p_ltp = float(prev.get("ltp", 0) or 0)
        p_vol = float(prev.get("volume", 0) or 0)
        vol = float(now.get("volume", 0) or 0)
        dvol = vol - p_vol
        dprice = (ltp - p_ltp) / p_ltp * 100 if p_ltp else 0
        if dvol > 0 and abs(dprice) >= 0.03:
            # volume traded AND price moved => aggressor identified.
            # No cap - a violent move earns proportionally big points, so an
            # exceptional setup visibly towers over a merely-decent one.
            strength = abs(dprice) * 2.5 + (0.5 if dvol > 0 else 0)
            if dprice > 0:
                up_pts += strength; exec_dir = "up"
                exec_note = f"buyers lifting asks (+{dprice:.2f}%, vol {int(dvol):,})"
            else:
                dn_pts += strength; exec_dir = "down"
                exec_note = f"sellers hitting bids ({dprice:.2f}%, vol {int(dvol):,})"
            exec_pts = strength
    factors.append({"name": "Execution flow", "points": round(exec_pts, 1),
                    "note": exec_note, "dir": exec_dir})

    # ---- 2. THIN OPPOSITION (air pocket) - weight up to 3.0 -----------
    # thick one side, thin the other => easy move into the thin side.
    thin_pts = 0.0; thin_dir = "flat"; thin_note = "balanced walls"
    if bid_qty > 0 and ask_qty > 0:
        ratio = bid_qty / ask_qty
        if ratio >= 1.6:
            # lots of bids, thin asks above => loaded UP (heaviest signal,
            # uncapped - an extreme air pocket scores extremely high)
            thin_pts = (ratio - 1) * 1.6
            up_pts += thin_pts; thin_dir = "up"
            thin_note = f"thin asks above (bid/ask {ratio:.1f}x) - room to run up"
        elif ratio <= 0.62:
            inv = ask_qty / bid_qty
            thin_pts = (inv - 1) * 1.6
            dn_pts += thin_pts; thin_dir = "down"
            thin_note = f"thin bids below (ask/bid {inv:.1f}x) - room to drop"
    factors.append({"name": "Thin opposition", "points": round(thin_pts, 1),
                    "note": thin_note, "dir": thin_dir})

    # ---- 3. DEPTH IMBALANCE (touch-weighted) - weight up to 2.0 -------
    depth_pts = 0.0; depth_dir = "flat"; depth_note = "even depth"
    twt = tw_bid + tw_ask
    if twt > 0:
        di = (tw_bid - tw_ask) / twt   # -1..+1
        if abs(di) >= 0.12:
            depth_pts = abs(di) * 3.0
            if di > 0:
                up_pts += depth_pts; depth_dir = "up"
                depth_note = f"buy depth heavier at touch ({di*100:.0f}%)"
            else:
                dn_pts += depth_pts; depth_dir = "down"
                depth_note = f"sell depth heavier at touch ({-di*100:.0f}%)"
    factors.append({"name": "Depth imbalance", "points": round(depth_pts, 1),
                    "note": depth_note, "dir": depth_dir})

    # ---- 4. SPREAD SIGNAL - weight up to 1.0 -------------------------
    spread_pts = 0.0; spread_note = "normal spread"
    spread = (best_ask_p - best_bid_p) / ltp * 100  # % spread
    if prev and prev.get("bids") and prev.get("asks"):
        p_spread = (prev["asks"][0][0] - prev["bids"][0][0]) / ltp * 100
        vol_rising = float(now.get("volume",0)) > float(prev.get("volume",0))
        if spread < p_spread * 0.7 and vol_rising:
            spread_pts = 1.0
            spread_note = "spread tightening + volume (coiling)"
            # tightening doesn't pick a side; adds to whichever exec/thin points
            if up_pts >= dn_pts: up_pts += spread_pts
            else: dn_pts += spread_pts
        elif spread > p_spread * 1.5:
            spread_pts = 0.6
            spread_note = "spread widening (move may be imminent)"
            if up_pts >= dn_pts: up_pts += spread_pts
            else: dn_pts += spread_pts
    factors.append({"name": "Spread", "points": round(spread_pts, 1),
                    "note": spread_note, "dir": "flat"})

    # ---- 5. VOLUME SURGE - weight up to 1.0 --------------------------
    vol_pts = 0.0; vol_note = "no volume surge"
    if prev:
        vol = float(now.get("volume",0)); pv = float(prev.get("volume",0))
        if pv > 0 and (vol - pv) / pv > 0.02:
            vol_pts = 1.0; vol_note = "volume surging"
            if up_pts >= dn_pts: up_pts += vol_pts
            else: dn_pts += vol_pts
    factors.append({"name": "Volume", "points": round(vol_pts, 1),
                    "note": vol_note, "dir": "flat"})

    # ---- direction from the book ----
    book_up = up_pts > dn_pts
    direction = "up" if book_up else "down"
    book_score = max(up_pts, dn_pts)   # 0..~10.5 raw

    # ---- CONFIRMATION MULTIPLIER (alignment) ------------------------
    # OI aligned + price aligned = the boost. Never a hard penalty.
    aligned_count = 1  # the book itself
    align_note = []
    oi_pts = 0.0
    if conv and conv.get("direction") == direction:
        oi_pts = conv.get("conviction", 0) / 50.0
        book_score += oi_pts
        aligned_count += 1
        align_note.append(f"OI positioned {direction}")
    factors.append({"name": "OI alignment", "points": round(oi_pts, 1),
                    "note": (f"OI agrees ({conv.get('conviction')})" if oi_pts > 0
                             else "OI not aligned / unknown"),
                    "dir": direction if oi_pts > 0 else "flat"})

    # price alignment (already partly in exec flow, but count as a lens)
    price_aligned = (exec_dir == direction and exec_pts > 0)
    if price_aligned:
        aligned_count += 1
        align_note.append(f"price moving {direction}")

    # all-three-aligned bonus (the high-probability confluence)
    triple = aligned_count >= 3
    if triple:
        book_score += 1.5
        align_note.append("ALL ALIGNED")

    # score is UNCAPPED - it reflects true magnitude. An exceptional setup
    # (thin opposition + execution + depth + OI + price + volume all aligned)
    # can score 15, 20, 25+ and visibly tower over merely-good ones. Rounded
    # to one decimal for readability.
    score = round(book_score, 1)

    return {
        "sym": sym,
        "ltp": round(ltp, 2),
        "score": score,
        "direction": direction,
        "aligned": triple,
        "aligned_count": aligned_count,
        "align_note": " - ".join(align_note) if align_note else "book only",
        "spread_pct": round(spread, 3),
        "bid_qty": int(bid_qty),
        "ask_qty": int(ask_qty),
        "factors": factors,
    }


class ScoreSmoother:
    """
    The websocket updates many times a second, so raw scores jump around and
    the ranking would flicker - untradable. This eases each stock's DISPLAYED
    score toward its latest raw score (an EMA), so the list stays calm and
    readable while real surges still come through. Also holds a stock briefly
    after it drops out, so it doesn't blink in and out.
    """
    def __init__(self, ease=0.35, hold=5):
        self.ease = ease          # 0=frozen, 1=instant. 0.35 = smooth but responsive
        self.hold = hold          # scans to keep a vanished stock before dropping
        self._s = {}              # {sym: {"score","dir","hold","raw"}}

    def apply(self, ranked):
        seen = set()
        for r in ranked:
            sym = r["sym"]; seen.add(sym)
            raw = r["score"]
            cur = self._s.get(sym)
            if cur:
                # ease displayed score toward raw
                cur["score"] += (raw - cur["score"]) * self.ease
                cur["dir"] = r["direction"]
                cur["hold"] = self.hold
                cur["data"] = r
            else:
                self._s[sym] = {"score": raw, "dir": r["direction"],
                                "hold": self.hold, "data": r}
        # decay stocks not seen this scan
        for sym in list(self._s.keys()):
            if sym not in seen:
                self._s[sym]["hold"] -= 1
                # ease their score down toward 0 gently while holding
                self._s[sym]["score"] *= 0.85
                if self._s[sym]["hold"] <= 0:
                    del self._s[sym]
        # emit smoothed, re-ranked
        out = []
        for sym, s in self._s.items():
            item = dict(s["data"])
            item["score"] = round(s["score"], 1)
            item["raw_score"] = s["data"]["score"]   # keep the instant value too
            out.append(item)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out


def rank_sharp(flows_depth, prev_depth, conv_map):
    """
    flows_depth = {sym: current normalized depth}
    prev_depth  = {sym: previous normalized depth}
    conv_map    = {sym: conviction dict}
    Returns list ranked by sharp-move score, best first.
    """
    out = []
    for sym, now in flows_depth.items():
        prev = (prev_depth or {}).get(sym)
        conv = conv_map.get(sym)
        r = analyze_sharp(sym, now, prev, conv)
        if r and r["score"] >= 2.0:   # ignore weak books
            out.append(r)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out