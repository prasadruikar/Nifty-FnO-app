"""
merge.py  -  Combine StockRanker conviction with orderflow proven levels.
=====================================================================
This is what turns two data tools into ONE clear answer: "buy this,
here, now." It reads the conviction bridge (from nse_scanner) and joins
it with the orderflow tool's proven absorption levels + live flow.

A stock becomes a "CONVICTION SETUP" only when signals AGREE:
  - StockRanker says it's high-conviction positioned (WHICH stock)
  - Orderflow has a proven level in the SAME direction (WHERE to enter)
  - (bonus) live flow is currently pushing that way too

STEADINESS (the key requirement):
  Raw orderflow flickers every scan. Traders can't act on a jumpy screen.
  So each setup is SMOOTHED and STICKY:
    - a setup that qualifies STAYS on the board for a hold window even if
      one scan dips (no disappearing/reappearing)
    - its displayed numbers are eased toward new values, not snapped
    - it only drops after it's genuinely gone for several scans
  Result: a calm board where setups appear, hold steady, and fade slowly -
  you can actually read and act on it.
"""

import json
import time
from pathlib import Path

BRIDGE_FILE = Path("conviction_bridge.json")

# steadiness tuning
HOLD_SCANS   = 8      # keep a setup visible this many scans after it stops qualifying
EASE         = 0.4    # how fast displayed numbers move toward new values (0=frozen,1=instant)
NEAR_PCT     = 0.6    # price within this % of a proven level = "at entry zone"
WATCH_PCT    = 1.5    # within this % = "approaching, watch"


class MergeEngine:
    def __init__(self):
        # sticky store: {sym: {...smoothed setup..., "hold": int, "live": bool}}
        self._setups = {}

    def _read_bridge(self):
        try:
            if BRIDGE_FILE.exists():
                return json.loads(BRIDGE_FILE.read_text())
        except Exception:
            pass
        return {"stocks": {}, "updated": "", "nifty_bias": ""}

    def build(self, last_flow, level_store):
        """
        last_flow    = {sym: latest flow signal}  (from orderflow engine)
        level_store  = LevelStore instance         (proven levels)
        Returns a STEADY, ranked list of conviction setups.
        """
        bridge = self._read_bridge()
        conv = bridge.get("stocks", {})

        # 1. find raw qualifying setups this scan
        raw = {}
        for sym, c in conv.items():
            want_side = "support" if c["direction"] == "up" else "resistance"
            spot = c.get("spot", 0) or (last_flow.get(sym, {}) or {}).get("ltp", 0)
            if not spot:
                continue
            # nearest proven level in the conviction's direction
            best = None
            for lv in level_store.levels_for(sym):
                if lv["side"] != want_side:
                    continue
                dist = abs(lv["price"] - spot) / spot * 100
                if dist <= WATCH_PCT and (not best or lv["strength"] > best["strength"]):
                    best = {**lv, "dist": round((lv["price"] - spot) / spot * 100, 2)}
            if not best:
                continue  # high conviction but no proven entry level yet -> not a setup

            flow = last_flow.get(sym, {}) or {}
            flow_dir = flow.get("direction", "flat")
            flow_agrees = (flow_dir == c["direction"])

            # stage of the entry
            adist = abs(best["dist"])
            if adist <= NEAR_PCT:
                stage = "AT ENTRY"
            else:
                stage = "APPROACHING"

            # combined quality score: conviction + level strength + agreement
            quality = (
                c["conviction"] * 0.5
                + min(50, best["strength"] / 10) * 0.3
                + (20 if flow_agrees else 0) * 1.0
                + (15 if stage == "AT ENTRY" else 0)
            )

            raw[sym] = {
                "sym": sym,
                "direction": c["direction"],
                "side": c["side"],
                "conviction": c["conviction"],
                "spot": round(spot, 2),
                "chg": c.get("chg", 0),
                "level": best["price"],
                "level_side": best["side"],
                "level_strength": best["strength"],
                "level_tests": best["tests"],
                "level_days": best.get("alive_days", 1),
                "level_value": best["value"],
                "dist": best["dist"],
                "stage": stage,
                "flow_agrees": flow_agrees,
                "flow_dir": flow_dir,
                "quality": round(quality),
                "story": c.get("story", ""),
            }

        # 2. merge into sticky store with smoothing + hold
        # update/insert current qualifiers
        for sym, r in raw.items():
            if sym in self._setups:
                s = self._setups[sym]
                # ease numeric fields toward new values (steady, not jumpy)
                for k in ("conviction", "quality", "level_strength", "spot", "dist"):
                    s[k] = round(s[k] + (r[k] - s[k]) * EASE, 2)
                # snap the categorical/label fields
                for k in ("direction","side","level","level_side","level_tests",
                          "level_days","level_value","stage","flow_agrees",
                          "flow_dir","story","chg"):
                    s[k] = r[k]
                s["hold"] = HOLD_SCANS
                s["live"] = True
            else:
                r["hold"] = HOLD_SCANS
                r["live"] = True
                self._setups[sym] = r

        # decay the ones that did NOT qualify this scan
        for sym in list(self._setups.keys()):
            if sym not in raw:
                s = self._setups[sym]
                s["hold"] -= 1
                s["live"] = False
                if s["hold"] <= 0:
                    del self._setups[sym]

        # 3. output: sort by quality, mark fading ones
        out = []
        for s in self._setups.values():
            item = dict(s)
            item["fading"] = not s["live"]
            out.append(item)
        out.sort(key=lambda x: (x["live"], x["quality"]), reverse=True)
        return {
            "setups": out,
            "conviction_updated": bridge.get("updated", ""),
            "nifty_bias": bridge.get("nifty_bias", ""),
            "bridge_alive": bool(conv),
        }
