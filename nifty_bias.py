"""
nifty_bias.py  -  Multi-lens NIFTY market-direction engine (Upstox data).
=====================================================================
Answers the trader's real question: "which way is the market leaning, and
how confident should I be?" by checking the lenses that actually predict
NIFTY direction, then giving ONE clear verdict + how many lenses agree.

THE LENSES (what an experienced trader watches):
  1. SPOT vs VWAP / open   - above VWAP = bulls control intraday, below = bears
  2. DAY MOMENTUM           - today's net move vs previous close
  2b. FUTURES premium/disc  - future above spot (premium) = bullish sentiment;
                              below (discount) = bearish. Pure positioning read.
                              Verified live via futures_probe.py before wiring in.
  3. OPTIONS PCR + OI walls  - put writers below = support (bullish); call
                              writers above = resistance (bearish); + max pain
  4. INDIA VIX direction     - rising VIX = fear/down; falling = calm/up
  5. BREADTH                 - how many NIFTY-50 stocks up vs down now
  6. HEAVYWEIGHTS            - the top-weight stocks that actually move the index

Each lens votes up / down / neutral (7 lenses total). The verdict = the
majority, and the CONFIDENCE = how many lenses agree (1-2 = noise, 6-7 =
strong trend).
"""

import datetime


NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY   = "NSE_INDEX|India VIX"

# NIFTY-50 heavyweights by approx index weight (the ones that move it)
HEAVYWEIGHTS = [
    ("HDFCBANK", 13.0), ("ICICIBANK", 8.5), ("RELIANCE", 8.0),
    ("INFY", 5.5), ("TCS", 4.0), ("BHARTIARTL", 4.0),
    ("LT", 3.7), ("ITC", 3.5), ("AXISBANK", 3.2), ("SBIN", 3.0),
    ("KOTAKBANK", 2.8), ("HINDUNILVR", 2.5), ("BAJFINANCE", 2.4),
]


def _vote(cond_up, cond_dn):
    return "up" if cond_up else "down" if cond_dn else "neutral"


def analyze_nifty(spot, day_open, prev_close, vwap,
                  fut_price, oi_data, vix, vix_prev,
                  stock_flows):
    """
    Returns a dict with per-lens votes, an overall verdict, and confidence.
    Any input may be None/0 - that lens just votes neutral (never crashes).
    """
    lenses = []

    # ---- 1. SPOT vs VWAP / open ----
    if spot and vwap:
        v = _vote(spot > vwap, spot < vwap)
        note = f"spot {spot:.0f} {'above' if spot>vwap else 'below'} VWAP {vwap:.0f}"
    elif spot and day_open:
        v = _vote(spot > day_open, spot < day_open)
        note = f"spot {spot:.0f} {'above' if spot>day_open else 'below'} open {day_open:.0f}"
    else:
        v, note = "neutral", "no spot/VWAP data"
    lenses.append({"name": "Price vs VWAP", "vote": v, "note": note})

    # ---- 2. DAY MOMENTUM (spot vs prev close - is today up or down?) ----
    if spot and prev_close:
        pct = (spot - prev_close) / prev_close * 100
        v = _vote(pct > 0.1, pct < -0.1)
        note = f"day {'up' if pct>0 else 'down'} {abs(pct):.2f}% vs prev close"
    else:
        v, note = "neutral", "no close data"
    lenses.append({"name": "Day momentum", "vote": v, "note": note})

    # ---- 2b. FUTURES premium/discount (real futures data) ----
    # Future trading ABOVE spot (premium) = bulls paying up = bullish
    # sentiment; BELOW spot (discount) = bearish. A tiny band (+-0.03%) is
    # treated as flat - normal cost-of-carry noise, not a real lean.
    # Verified live end-to-end via futures_probe.py before this was wired in.
    if spot and fut_price:
        prem_pct = (fut_price - spot) / spot * 100
        v = _vote(prem_pct > 0.03, prem_pct < -0.03)
        note = (f"future {fut_price:.1f} {'above' if prem_pct>0 else 'below'} "
                f"spot {spot:.1f} ({prem_pct:+.3f}%)")
    else:
        v, note = "neutral", "no futures data"
    lenses.append({"name": "Futures premium", "vote": v, "note": note})

    # ---- 3. OPTIONS PCR + OI + max pain ----
    if oi_data:
        pcr = oi_data.get("pcr", 1.0)
        pain = oi_data.get("max_pain", 0)
        pain_side = "neutral"
        if pain and spot:
            if spot < pain * 0.998:
                pain_side = "up"     # below max pain -> pull up
            elif spot > pain * 1.002:
                pain_side = "down"
        pcr_side = _vote(pcr < 0.8, pcr > 1.2)
        # combine pcr + max pain
        if pcr_side == pain_side and pcr_side != "neutral":
            v = pcr_side
        elif pcr_side != "neutral":
            v = pcr_side
        else:
            v = pain_side
        note = f"PCR {pcr} - max pain {pain:.0f} ({'above' if spot and pain and spot>pain else 'below'} spot)"
    else:
        v, note = "neutral", "no options data"
    lenses.append({"name": "Options (PCR/OI)", "vote": v, "note": note})

    # ---- 4. INDIA VIX direction ----
    if vix and vix_prev:
        # rising VIX = fear = bearish; falling VIX = calm = bullish
        v = _vote(vix < vix_prev * 0.99, vix > vix_prev * 1.01)
        note = f"VIX {vix:.1f} {'falling (calm)' if vix<vix_prev else 'rising (fear)' if vix>vix_prev else 'flat'}"
    else:
        v, note = "neutral", "no VIX data"
    lenses.append({"name": "India VIX", "vote": v, "note": note})

    # ---- 5. BREADTH (NIFTY-50 up vs down) ----
    n50 = [s for s, _ in HEAVYWEIGHTS]
    ups = downs = 0
    for sym, f in (stock_flows or {}).items():
        chg = f.get("dprice", 0) or 0
        if chg > 0.1: ups += 1
        elif chg < -0.1: downs += 1
    if ups + downs >= 5:
        v = _vote(ups > downs * 1.3, downs > ups * 1.3)
        note = f"{ups} up / {downs} down across F&O"
    else:
        v, note = "neutral", "insufficient breadth data"
    lenses.append({"name": "Breadth", "vote": v, "note": note})

    # ---- 6. HEAVYWEIGHTS ----
    hw_up = hw_dn = 0.0
    hw_detail = []
    for sym, wt in HEAVYWEIGHTS:
        f = (stock_flows or {}).get(sym)
        if not f:
            continue
        chg = f.get("dprice", 0) or 0
        if chg > 0.1: hw_up += wt
        elif chg < -0.1: hw_dn += wt
    if hw_up + hw_dn > 0:
        v = _vote(hw_up > hw_dn * 1.2, hw_dn > hw_up * 1.2)
        note = f"heavyweight weight {hw_up:.0f} up vs {hw_dn:.0f} down"
    else:
        v, note = "neutral", "no heavyweight data"
    lenses.append({"name": "Heavyweights", "vote": v, "note": note})

    # ---- VERDICT + CONFIDENCE ----
    up_votes = sum(1 for l in lenses if l["vote"] == "up")
    dn_votes = sum(1 for l in lenses if l["vote"] == "down")
    active = up_votes + dn_votes

    if up_votes > dn_votes:
        direction, agree = "up", up_votes
    elif dn_votes > up_votes:
        direction, agree = "down", dn_votes
    else:
        direction, agree = "neutral", 0

    total = len(lenses)
    # thresholds scale with lens count (now 7, was 6) so the SAME proportion
    # of agreement is needed for each strength tier, not just the same count.
    strong_at = max(4, round(total * 5 / 6))   # was >=5 of 6 (~83%)
    clear_at  = max(3, round(total * 4 / 6))   # was >=4 of 6 (~67%)
    if direction == "neutral":
        verdict, strength = "NEUTRAL / RANGE", "mixed"
    elif agree >= strong_at:
        verdict = "STRONG BULLISH" if direction == "up" else "STRONG BEARISH"
        strength = "strong"
    elif agree >= clear_at:
        verdict = "BULLISH" if direction == "up" else "BEARISH"
        strength = "clear"
    else:
        verdict = "MILD BULLISH" if direction == "up" else "MILD BEARISH"
        strength = "mild"

    return {
        "verdict": verdict,
        "direction": direction,
        "strength": strength,
        "agree": agree,
        "total": total,
        "up_votes": up_votes,
        "dn_votes": dn_votes,
        "spot": round(spot, 1) if spot else 0,
        "chg_pct": round((spot - prev_close) / prev_close * 100, 2) if spot and prev_close else 0,
        "lenses": lenses,
        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
    }