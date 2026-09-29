"""
levels.py  -  Multi-day absorption LEVEL engine.
=====================================================================
This is the heart of the "where do I enter" logic. It turns raw
absorption events into PERSISTENT, GRADED price levels that survive
across days - the memory of where big money defended.

THE TWO PROBLEMS THIS SOLVES (both spotted by you):

1. LEVELS PERSIST ACROSS DAYS.
   A level where institutions absorbed size doesn't die at 3:30 PM.
   We store levels to disk (levels_store.json), reinforce them when
   price returns and they hold, and only expire them after long neglect.
   A level held for 3 days, tested 5x = a fortress. One scan = noise.

2. RAW SHARE/ORDER COUNT IS MISLEADING.
   A Rs50 stock with 1L shares (Rs50L) looks bigger than a Rs3000 stock
   with 5k shares (Rs1.5cr) - but the second is 3x the real money.
   FIX A: measure absorption in RUPEES (qty x price), not shares.
   FIX B: judge each stock vs ITS OWN normal book size, so a small stock
          and RELIANCE compete fairly on "unusual FOR this stock".

LEVEL STRENGTH =
     rupee_value_absorbed        (real money committed)
   x relative_size_factor        (unusual for THIS stock)
   x times_tested                (proven repeatedly)
   x age_factor                  (survived across days)
"""

import json
import time
import datetime
from pathlib import Path

STORE_FILE = Path("levels_store.json")

# --- tunables (design decisions) ---
MERGE_PCT      = 0.003    # levels within 0.3% merge into one zone
DECAY_DAYS     = 5        # after N trading days untouched, strength decays
EXPIRE_DAYS    = 15       # after N trading days untouched, level is dropped
RELATIVE_MULT  = 3.0      # absorption must be >= 3x this stock's normal to count
MIN_VALUE      = 500000   # ignore absorption below Rs5 lakh (noise floor)


class LevelStore:
    """Holds all persistent absorption levels, keyed by symbol."""

    def __init__(self):
        # structure: {sym: {"levels": [level, ...], "avg_book": float, "n": int}}
        # level = {price, side, value, tests, first_day, last_day, last_ts, strength}
        self.data = {}
        self._load()

    # ---- persistence ----
    def _load(self):
        if STORE_FILE.exists():
            try:
                self.data = json.loads(STORE_FILE.read_text())
            except Exception:
                self.data = {}

    def save(self):
        try:
            STORE_FILE.write_text(json.dumps(self.data, indent=1))
        except Exception:
            pass

    # ---- rolling average book size per stock (for relative sizing) ----
    def update_book_norm(self, sym, book_value):
        """Track each stock's typical book size so we can judge 'unusual for it'."""
        s = self.data.setdefault(sym, {"levels": [], "avg_book": 0.0, "n": 0})
        n = s["n"]
        # exponential-ish rolling mean, capped so it adapts but isn't jumpy
        if n == 0:
            s["avg_book"] = book_value
        else:
            s["avg_book"] += (book_value - s["avg_book"]) * 0.05
        s["n"] = min(n + 1, 500)

    def is_unusual(self, sym, value):
        """Is this absorption big relative to the stock's own normal?"""
        s = self.data.get(sym)
        if not s or s["avg_book"] <= 0:
            return value >= MIN_VALUE   # fall back to absolute floor early on
        return value >= max(MIN_VALUE, s["avg_book"] * RELATIVE_MULT)

    # ---- record an absorption event -> create or reinforce a level ----
    def record_absorption(self, sym, price, side, rupee_value, day):
        """
        price       = level where absorption happened
        side        = "support" (buyers absorbed) / "resistance" (sellers)
        rupee_value = qty * price (real money, not shares)
        day         = date string, for age tracking
        """
        if rupee_value < MIN_VALUE:
            return None
        s = self.data.setdefault(sym, {"levels": [], "avg_book": 0.0, "n": 0})
        levels = s["levels"]

        # find an existing level within MERGE_PCT and same side
        for lv in levels:
            if lv["side"] == side and abs(lv["price"] - price) / price <= MERGE_PCT:
                # REINFORCE: this proven level got defended again
                lv["value"] += rupee_value
                lv["tests"] += 1
                lv["last_day"] = day
                lv["last_ts"] = time.strftime("%H:%M:%S")
                # nudge the level price toward the new absorption (value-weighted)
                lv["price"] = round((lv["price"] + price) / 2, 2)
                return lv

        # else CREATE a new level
        lv = {
            "price": round(price, 2), "side": side,
            "value": rupee_value, "tests": 1,
            "first_day": day, "last_day": day,
            "last_ts": time.strftime("%H:%M:%S"), "strength": 0,
        }
        levels.append(lv)
        return lv

    # ---- age, decay, expire, and score every level ----
    def refresh(self, today):
        today_d = _to_date(today)
        for sym, s in list(self.data.items()):
            keep = []
            for lv in s["levels"]:
                age_days = (today_d - _to_date(lv["last_day"])).days
                if age_days > EXPIRE_DAYS:
                    continue  # dead, drop it
                # age factor: fresh = 1.0, decays after DECAY_DAYS
                if age_days <= DECAY_DAYS:
                    age_factor = 1.0
                else:
                    age_factor = max(0.2, 1.0 - (age_days - DECAY_DAYS) * 0.15)
                # days-alive bonus: a level that has EXISTED across days is stronger
                alive_days = (today_d - _to_date(lv["first_day"])).days + 1
                alive_bonus = min(2.0, 1.0 + alive_days * 0.15)
                # strength = money * tests * age * multi-day existence
                lv["strength"] = round(
                    (lv["value"] / 100000.0)      # value in lakhs
                    * lv["tests"]
                    * age_factor
                    * alive_bonus
                )
                lv["age_days"] = age_days
                lv["alive_days"] = alive_days
                keep.append(lv)
            # keep only the strongest ~8 levels per stock (both sides)
            keep.sort(key=lambda x: x["strength"], reverse=True)
            s["levels"] = keep[:8]

    # ---- query: levels for a stock, and nearest level to current price ----
    def levels_for(self, sym):
        return self.data.get(sym, {}).get("levels", [])

    def nearest_level(self, sym, price, max_dist_pct=1.5):
        """The strongest level within max_dist_pct of current price."""
        best = None
        for lv in self.levels_for(sym):
            dist = abs(lv["price"] - price) / price * 100
            if dist <= max_dist_pct:
                if not best or lv["strength"] > best["strength"]:
                    best = {**lv, "dist_pct": round((lv["price"] - price) / price * 100, 2)}
        return best


def _to_date(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return datetime.date.today()
