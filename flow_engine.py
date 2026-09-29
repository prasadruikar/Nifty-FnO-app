"""
flow_engine.py  -  The orderflow signal engine.
=====================================================================
Pure logic. Takes normalized depth snapshots (from ANY feed) and the
previous snapshot, and produces per-stock orderflow signals + a ranked
score. Knows nothing about REST vs websocket - it just reads books.

THE SIGNALS (each explained where computed):
  1. Imbalance      - who has more resting quantity, buyers or sellers
  2. Absorption     - big resting order refilling while trades hit it
                      and price holds  = someone soaking up flow
  3. Big player     - large quantity in FEW orders = one institution,
                      not a retail crowd (stronger signal)
  4. Spoof pull     - a big level vanishing as price nears = fake
  5. Aggression     - volume rising with price = aggressive initiative

Everything is scan-to-scan (compare now vs previous), which is exactly
why a 3-5s REST poll is enough - we read the NET change, not every tick.
"""


def _safe(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _top(levels):
    """Best level (price, qty, orders) or zeros."""
    return levels[0] if levels else (0.0, 0, 0)


def _sum_qty(levels):
    return sum(q for _, q, _ in levels)


def _sum_orders(levels):
    return sum(o for _, _, o in levels)


def analyze(sym, now, prev):
    """
    Compute orderflow signals for one stock.
    now / prev = normalized depth dicts (prev may be None on first scan).
    Returns a dict of signals + a 0-100 flow score + direction.
    """
    bids = now.get("bids", [])
    asks = now.get("asks", [])
    if not bids or not asks:
        return None  # need both sides to read flow

    ltp = _safe(now.get("ltp"))
    vol = _safe(now.get("volume"))
    best_bid_p, best_bid_q, best_bid_o = _top(bids)
    best_ask_p, best_ask_q, best_ask_o = _top(asks)

    bid_qty = _sum_qty(bids)
    ask_qty = _sum_qty(asks)
    bid_ord = _sum_orders(bids)
    ask_ord = _sum_orders(asks)

    # ---- 1. IMBALANCE -------------------------------------------------
    # Ratio of resting buy qty to sell qty across visible levels.
    # >1 = more buyers stacked (support/pressure up). <1 = sellers heavier.
    total = bid_qty + ask_qty
    imbalance = round((bid_qty - ask_qty) / total, 3) if total else 0.0  # -1..+1
    imb_ratio = round(bid_qty / ask_qty, 2) if ask_qty else 9.99

    # ---- 3. BIG PLAYER (avg order size) -------------------------------
    # Large qty spread over FEW orders => big single participant (institution).
    # qty-per-order high on the bid = a big buyer; on the ask = a big seller.
    bid_qpo = (bid_qty / bid_ord) if bid_ord else 0        # qty per order, bids
    ask_qpo = (ask_qty / ask_ord) if ask_ord else 0        # qty per order, asks
    big_side = "buy" if bid_qpo > ask_qpo * 1.5 else \
               "sell" if ask_qpo > bid_qpo * 1.5 else "none"

    # ---- scan-to-scan pieces (need prev) ------------------------------
    absorption = "none"      # "buyers"/"sellers" absorbing
    absorb_note = ""
    spoof = "none"
    dvol = 0.0
    dprice = 0.0
    refill_bid = refill_ask = False

    if prev and prev.get("bids") and prev.get("asks"):
        p_best_bid_p, p_best_bid_q, _ = _top(prev["bids"])
        p_best_ask_p, p_best_ask_q, _ = _top(prev["asks"])
        p_vol = _safe(prev.get("volume"))
        p_ltp = _safe(prev.get("ltp"))
        dvol = vol - p_vol                     # traded volume since last scan
        dprice = (ltp - p_ltp) / p_ltp * 100 if p_ltp else 0.0

        # ---- 2. ABSORPTION --------------------------------------------
        # Buyers absorbing: big bid stays/refills at ~same price, real
        # volume traded (dvol high), but price did NOT fall. Sellers kept
        # hitting the bid and a big buyer soaked it all -> bullish.
        # Sellers absorbing: mirror (big ask holds, price didn't rise).
        VOL_ACTIVE = dvol > 0
        # bid "refilled": best bid qty is still large vs last scan at ~same price
        same_bid_price = abs(best_bid_p - p_best_bid_p) / p_best_bid_p < 0.0008 if p_best_bid_p else False
        same_ask_price = abs(best_ask_p - p_best_ask_p) / p_best_ask_p < 0.0008 if p_best_ask_p else False
        refill_bid = same_bid_price and best_bid_q >= p_best_bid_q * 0.8 and best_bid_q > 0
        refill_ask = same_ask_price and best_ask_q >= p_best_ask_q * 0.8 and best_ask_q > 0

        if VOL_ACTIVE and refill_bid and dprice >= -0.05 and best_bid_q > best_ask_q:
            absorption = "buyers"
            absorb_note = f"Big bid held at {best_bid_p:g} while {int(dvol):,} traded - buyer absorbing"
        elif VOL_ACTIVE and refill_ask and dprice <= 0.05 and best_ask_q > best_bid_q:
            absorption = "sellers"
            absorb_note = f"Big ask held at {best_ask_p:g} while {int(dvol):,} traded - seller absorbing"

        # ---- 4. SPOOF PULL --------------------------------------------
        # A big bid that was there last scan and vanished as price fell,
        # or big ask that vanished as price rose = pulled/fake liquidity.
        if p_best_bid_q > 0 and best_bid_q < p_best_bid_q * 0.4 and dprice < -0.03:
            spoof = "bid_pulled"    # support yanked -> bearish
        elif p_best_ask_q > 0 and best_ask_q < p_best_ask_q * 0.4 and dprice > 0.03:
            spoof = "ask_pulled"    # resistance yanked -> bullish

    # ---- 5. AGGRESSION ------------------------------------------------
    # Volume rising WITH price = aggressive buyers lifting offers.
    # Volume rising as price falls = aggressive sellers hitting bids.
    aggression = "none"
    if dvol > 0 and dprice > 0.05:
        aggression = "buyers"
    elif dvol > 0 and dprice < -0.05:
        aggression = "sellers"

    # ================================================================
    # SCORE + DIRECTION  (blend the signals into one 0-100 read)
    # ================================================================
    # Direction: net of all bullish vs bearish evidence.
    bull = 0.0
    bear = 0.0

    # imbalance
    if imbalance > 0.15: bull += imbalance * 25
    elif imbalance < -0.15: bear += -imbalance * 25

    # absorption is the strongest single tell
    if absorption == "buyers": bull += 35
    elif absorption == "sellers": bear += 35

    # big player
    if big_side == "buy": bull += 12
    elif big_side == "sell": bear += 12

    # aggression
    if aggression == "buyers": bull += 18
    elif aggression == "sellers": bear += 18

    # spoof (contrarian to the pull)
    if spoof == "ask_pulled": bull += 10      # resistance gone
    elif spoof == "bid_pulled": bear += 10    # support gone

    score = round(min(100, max(bull, bear)))
    direction = "up" if bull > bear else "down" if bear > bull else "flat"

    # plain-English story for the card
    if absorption == "buyers":
        story = "Buyers absorbing supply"
    elif absorption == "sellers":
        story = "Sellers absorbing demand"
    elif aggression == "buyers" and imbalance > 0.1:
        story = "Aggressive buying + bid stacked"
    elif aggression == "sellers" and imbalance < -0.1:
        story = "Aggressive selling + ask stacked"
    elif imbalance > 0.25:
        story = "Heavy bid imbalance"
    elif imbalance < -0.25:
        story = "Heavy ask imbalance"
    else:
        story = "Mild flow"

    return {
        "sym": sym,
        "ltp": round(ltp, 2),
        "score": score,
        "direction": direction,
        "story": story,
        "imbalance": imbalance,
        "imb_ratio": imb_ratio,
        "bid_qty": int(bid_qty),
        "ask_qty": int(ask_qty),
        "best_bid": best_bid_p,
        "best_ask": best_ask_p,
        "best_bid_qty": int(best_bid_q),
        "best_ask_qty": int(best_ask_q),
        "big_side": big_side,
        "bid_qpo": round(bid_qpo),
        "ask_qpo": round(ask_qpo),
        "absorption": absorption,
        "absorb_note": absorb_note,
        "absorb_price": best_bid_p if absorption == "buyers" else best_ask_p if absorption == "sellers" else 0,
        # rupee value absorbed = qty at the absorbing level x price (real money,
        # not shares - so a cheap and an expensive stock compare fairly)
        "absorb_value": int((best_bid_q * best_bid_p) if absorption == "buyers"
                            else (best_ask_q * best_ask_p) if absorption == "sellers" else 0),
        # total book value in rupees (for the stock's own-normal baseline)
        "book_value": int(bid_qty * best_bid_p + ask_qty * best_ask_p),
        "spoof": spoof,
        "aggression": aggression,
        "dvol": int(dvol),
        "dprice": round(dprice, 2),
    }


def rank(results):
    """Sort by score desc; strongest orderflow signals on top."""
    return sorted(results, key=lambda r: r["score"], reverse=True)
