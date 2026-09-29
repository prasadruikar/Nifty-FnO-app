"""
nse_scanner.py  -  NSE Option Scanner  (Clean Focus Mode)
=========================================================
INSTALL:  pip install "nse[local]"
RUN:      python nse_scanner.py
OPEN:     http://localhost:5050
STOP:     Ctrl+C

Shows only the stocks worth watching. Clean, no clutter.
Top 20 CALL setups + Top 20 PUT setups, ranked by probability.
"""

import os, sys, time, csv, json, datetime, threading, webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from nse import NSE
except ImportError:
    sys.exit('\nRun:  pip install "nse[local]"  then try again.\n')

POLL_SECONDS = 180
MARKET_START = (9, 30)
MARKET_END   = (15, 5)
MIN_OI       = 500
DATA_DIR     = Path("scan_data")
PORT         = 5050
TOP_N        = 20        # internal call/put lists saved to CSV (not the screen)
SHOW_IDEAS   = 20        # how many setup cards to show in the main list on screen

# -- DATA SOURCE -----------------------------------------------------------
# "upstox" = pull option-chain data from Upstox's authenticated API
#            (reliable, no 403s, needs access_token.json from upstox_auth.py)
# "nse"    = scrape NSE's free endpoints (works but 403s intermittently)
# Upstox is strongly preferred - it's the same data, reliable.
# Auto-fallback: if "upstox" is set but no token exists, uses NSE.
DATA_SOURCE = "upstox"

# -- SCORING BALANCE -------------------------------------------------------
# How much to favour stocks that are MOVING NOW vs stocks that are POSITIONED.
#   0.0 = pure positioning (enter early, but some setups sit still)
#   0.5 = balanced (recommended - movers pulled up, positioning still counts)
#   1.0 = movement-dominant (catch runners, but more chase/reversal risk)
# You noticed movers were ranked too low - 0.5 lifts them without pure chasing.
# Tune freely and see which gives better paper-trade outcomes in your CSV.
MOVEMENT_TILT = 0.5

# -- CONCURRENCY -----------------------------------------------------------
# The slow part of a scan is fetching ~208 option chains over the network.
# Doing them one-by-one (with a sleep) took ~2-4 min. We fetch them in
# parallel with a small thread pool instead. MAX_WORKERS is kept modest so
# we don't hammer NSE (which rate-limits) - 10 is a safe, fast sweet spot.
# The COMPUTE step (scoring, conviction, live-pressure) still runs single-
# threaded afterwards, so all the shared-state logic is 100% unchanged.
MAX_WORKERS   = 10       # parallel chain fetches (tune 6-12; higher = riskier)
FETCH_TIMEOUT = 15       # seconds per chain before giving up on it

_SKIP = {"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50","NIFTYIT"}
_FALLBACK = [
    "HDFCBANK","ICICIBANK","SBIN","AXISBANK","KOTAKBANK","INDUSINDBK",
    "BAJFINANCE","BAJAJFINSV","RELIANCE","LT","INFY","TCS","HCLTECH",
    "WIPRO","TECHM","BHARTIARTL","MARUTI","TATAMOTORS","M&M","ITC",
    "HINDUNILVR","ADANIENT","ADANIPORTS","ADANIGREEN","ZOMATO","PAYTM",
    "NYKAA","ZEEL","IDEA","IRCTC","IRFC","BEL","HAL","BSE","MCX",
    "TATASTEEL","JSWSTEEL","HINDALCO","SUNPHARMA","DRREDDY","CIPLA",
    "COALINDIA","ONGC","BPCL","GAIL","POWERGRID","NTPC",
    "SHRIRAMFIN","CHOLAFIN","MOTHERSON","GODREJPROP","LODHA",
]

_state = {
    "bulls": [], "bears": [], "momentum": [], "unified": [], "positioning": [], "all_stocks": [], "heatmap": [], "scan_num": 0,
    "scan_time": "--:--:--", "next_scan_in": 0,
    "nifty": "Loading...", "nifty_chg": 0.0, "total_stocks": 0,
    "top_movers": [],
    "nifty_bias": None, "sectors": [],
}
_lock = threading.Lock()

# -- PERSISTENCE TRACKING --------------------------------------------------
# Tracks how many CONSECUTIVE scans each stock has held a strong signal.
# This is the key to "sustainable" signals - a stock bullish for 5 scans
# in a row is real conviction; a stock bullish for 1 scan is noise.
# Structure: {symbol: {"call_streak": int, "put_streak": int,
#                      "first_seen_call": "HH:MM", "first_seen_put": "HH:MM",
#                      "call_scores": [last few], "put_scores": [last few]}}
_persist = {}
_PERSIST_THRESHOLD = 60   # score must be >= this to count as "holding" a signal

# -- OPENING RANGE TRACKER -------------------------------------------------
# The first 30 minutes (9:15-9:45) sets the day's opening range.
# Break above OR high = strongest call trigger; break below OR low = put.
# We capture the highest high and lowest low seen during that window.
# Structure: {symbol: {"or_high": float, "or_low": float, "locked": bool}}
_open_range = {}
_OR_END = (9, 45)   # opening range ends at 9:45 AM

# -- INTRADAY LIVE-PRESSURE TRACKER ----------------------------------------
# Stores a full SNAPSHOT of each stock every scan: spot, call OI, put OI,
# ATM IV, total volume, PCR. This lets us measure the CHANGE (velocity) of
# every dimension across scans - not just price, but whether positioning,
# volatility, and participation are all shifting the same way RIGHT NOW.
#
# To reduce noise: we compare the CURRENT scan against the AVERAGE of the
# last 2-3 scans (a smoothed baseline), so a single blip doesn't fire a
# false signal. Only a sustained shift across multiple scans registers.
#
# Structure: {symbol: [snapshot_dict, snapshot_dict, ...]}  (last ~5 scans)
# snapshot = {"t","spot","c_oi","p_oi","atm_iv","vol","pcr"}
_price_hist = {}

# Smoothed live-pressure per stock (EMA) - keeps the momentum list stable
# instead of flickering. Structure: {symbol: {"sp": float, "seen": int}}
_lp_smooth = {}

# -- UNIVERSE ------------------------------------------------------------------
def fetch_universe(nse):
    print("  Fetching F&O universe...", end=" ", flush=True)
    try:
        raw = nse.listEquityStocksByIndex(index="SECURITIES IN F&O")
        if raw and isinstance(raw, dict) and "data" in raw:
            syms = sorted({r.get("symbol","") for r in raw["data"]
                           if r.get("symbol") and r["symbol"] not in _SKIP})
            if len(syms) > 50:
                print(f"{len(syms)} stocks"); return syms
    except Exception as e:
        print(f"error: {e}")
    print("using fallback"); return list(dict.fromkeys(_FALLBACK))

# -- MARKET DATA ---------------------------------------------------------------
def fetch_day_data(nse):
    """Returns chg_map (symbol -> pct change) and a rich day_map with
    open/high/low/prevClose/last for every F&O stock. These real levels
    feed the trigger calculation."""
    chg_map = {}; day_map = {}; movers = []
    try:
        raw = nse.listEquityStocksByIndex(index="SECURITIES IN F&O")
        if raw and isinstance(raw, dict) and "data" in raw:
            for r in raw["data"]:
                sym = r.get("symbol","")
                if not sym: continue
                pchg = r.get("pChange") or 0
                try: chg_map[sym] = round(float(pchg), 2)
                except: pass
                # Capture real intraday levels for trigger calc
                try:
                    day_map[sym] = {
                        "open":  float(r.get("open") or 0),
                        "high":  float(r.get("dayHigh") or 0),
                        "low":   float(r.get("dayLow") or 0),
                        "prev":  float(r.get("previousClose") or 0),
                        "last":  float(r.get("lastPrice") or 0),
                        "vol":   float(r.get("totalTradedVolume") or 0),
                        "pchg":  float(pchg or 0),
                    }
                except: pass
    except: pass
    for sym, chg in chg_map.items():
        movers.append({"sym": sym, "chg": chg})
    movers.sort(key=lambda x: abs(x["chg"]), reverse=True)
    return chg_map, day_map, movers[:25]

def fetch_nifty(nse):
    try:
        for m in (nse.status() or []):
            if m.get("market") == "Capital Market":
                chg  = float(m.get("percentChange", 0))
                last = m.get("last", 0)
                return f"{last} ({'+' if chg>=0 else ''}{chg}%)", chg, float(last or 0)
    except: pass
    return "?", 0.0, 0.0

def fetch_nifty_bias(nse):
    """
    Analyze NIFTY's own option chain to determine intraday bias.
    Returns a dict with bias label, score, and the reasoning.
    Uses the same OI logic as stocks: PCR, OI buildup, max pain, spot vs pain.
    """
    try:
        raw = nse.optionChain("nifty")   # lowercase required for index futures
        if not raw: return None
        rec  = raw.get("records", {})
        data = rec.get("data", [])
        spot = rec.get("underlyingValue")
        if not spot or not data: return None

        # nearest expiry only
        dates = {r.get("expiryDate") or r.get("expiry")
                 for r in data if r.get("expiryDate") or r.get("expiry")}
        nearest = min(dates, key=parse_expiry) if dates else None
        rows = [r for r in data
                if (r.get("expiryDate") or r.get("expiry")) == nearest] if nearest else data
        if not rows: rows = data

        strikes = {}
        tc_oi=tp_oi=tc_chg=tp_chg=0
        atm_civs=[]; atm_pivs=[]; band = spot*0.01  # 1% band for index
        for row in rows:
            st = row.get("strikePrice",0) or 0
            ce = row.get("CE") or {}; pe = row.get("PE") or {}
            coi=int(ce.get("openInterest",0) or 0)
            poi=int(pe.get("openInterest",0) or 0)
            cc =int(ce.get("changeinOpenInterest",0) or 0)
            pc =int(pe.get("changeinOpenInterest",0) or 0)
            if st: strikes[st] = {"c_oi":coi,"p_oi":poi}
            tc_oi+=coi; tp_oi+=poi; tc_chg+=cc; tp_chg+=pc
            if st and abs(st-spot) <= band:
                civ=float(ce.get("impliedVolatility",0) or 0)
                piv=float(pe.get("impliedVolatility",0) or 0)
                if civ>1: atm_civs.append(civ)
                if piv>1: atm_pivs.append(piv)

        pcr     = round(tp_oi/tc_oi, 2) if tc_oi else 1.0
        oi_bias = tc_chg - tp_chg
        mp      = max_pain(strikes)
        pain_d  = round((spot-mp)/mp*100, 2) if mp else 0
        atm_iv  = round((sum(atm_civs)/len(atm_civs) if atm_civs else 0), 1)

        # Bias scoring: -100 (very bearish) to +100 (very bullish)
        score = 0
        reasons = []
        # PCR
        if pcr < 0.7:
            score += 30; reasons.append(f"PCR {pcr} (call writers heavy = bullish)")
        elif pcr > 1.3:
            score -= 30; reasons.append(f"PCR {pcr} (put writers heavy = bearish)")
        else:
            reasons.append(f"PCR {pcr} (neutral)")
        # OI buildup
        if oi_bias < -300000:
            score += 25; reasons.append("Puts being written (floor forming = bullish)")
        elif oi_bias > 300000:
            score -= 25; reasons.append("Calls being written (ceiling forming = bearish)")
        # Spot vs max pain
        if mp:
            if pain_d > 0.3:
                score += 20; reasons.append(f"Above max pain {mp} by {pain_d}% (bullish momentum)")
            elif pain_d < -0.3:
                score -= 20; reasons.append(f"Below max pain {mp} by {pain_d}% (bearish momentum)")
            else:
                reasons.append(f"Pinned near max pain {mp}")

        if   score >= 40:  label = "BULLISH"
        elif score >= 15:  label = "MILD BULLISH"
        elif score <= -40: label = "BEARISH"
        elif score <= -15: label = "MILD BEARISH"
        else:              label = "NEUTRAL / RANGE"

        return {
            "spot": spot, "pcr": pcr, "oi_bias": oi_bias,
            "max_pain": mp or 0, "pain_dist": pain_d, "atm_iv": atm_iv,
            "score": score, "label": label, "reasons": reasons[:4],
            "expiry": nearest or "?",
        }
    except Exception as e:
        print(f"  [nifty bias err: {e}]")
        return None

# Sectoral indices to track (NSE index names)
_SECTORS = [
    ("NIFTY BANK",           "Banking"),
    ("NIFTY IT",             "IT"),
    ("NIFTY AUTO",           "Auto"),
    ("NIFTY FMCG",           "FMCG"),
    ("NIFTY PHARMA",         "Pharma"),
    ("NIFTY METAL",          "Metal"),
    ("NIFTY REALTY",         "Realty"),
    ("NIFTY ENERGY",         "Energy"),
    ("NIFTY FIN SERVICE",    "FinServ"),
    ("NIFTY PSU BANK",       "PSU Bank"),
    ("NIFTY MEDIA",          "Media"),
    ("NIFTY CONSUMER DURABLES","ConsDur"),
]

# Map each F&O stock to a sector bucket. Used for the heatmap: clicking a
# sector shows its stocks, and each sector's colour reflects its stocks'
# average move (buyer vs seller bias). Covers the liquid F&O names; anything
# not listed falls into "Other".
_STOCK_SECTOR = {
    # Banking
    "HDFCBANK":"Banking","ICICIBANK":"Banking","SBIN":"Banking","AXISBANK":"Banking",
    "KOTAKBANK":"Banking","INDUSINDBK":"Banking","BANDHANBNK":"Banking","FEDERALBNK":"Banking",
    "IDFCFIRSTB":"Banking","AUBANK":"Banking","BANKBARODA":"PSU Bank","PNB":"PSU Bank",
    "CANBK":"PSU Bank","UNIONBANK":"PSU Bank",
    # NBFC / FinServ
    "BAJFINANCE":"FinServ","BAJAJFINSV":"FinServ","SHRIRAMFIN":"FinServ","CHOLAFIN":"FinServ",
    "SBICARD":"FinServ","MUTHOOTFIN":"FinServ","LICHSGFIN":"FinServ","PFC":"FinServ",
    "RECLTD":"FinServ","HDFCLIFE":"FinServ","SBILIFE":"FinServ","ICICIPRULI":"FinServ",
    "ICICIGI":"FinServ","LICI":"FinServ","PAYTM":"FinServ","POLICYBZR":"FinServ",
    "360ONE":"FinServ","IEX":"FinServ","ANGELONE":"FinServ","BSE":"FinServ","MCX":"FinServ",
    # IT
    "TCS":"IT","INFY":"IT","HCLTECH":"IT","WIPRO":"IT","TECHM":"IT","LTIM":"IT",
    "LTTS":"IT","MPHASIS":"IT","PERSISTENT":"IT","COFORGE":"IT","OFSS":"IT",
    # Auto
    "MARUTI":"Auto","TATAMOTORS":"Auto","M&M":"Auto","BAJAJ-AUTO":"Auto","EICHERMOT":"Auto",
    "HEROMOTOCO":"Auto","TVSMOTOR":"Auto","ASHOKLEY":"Auto","BHARATFORG":"Auto",
    "MOTHERSON":"Auto","BOSCHLTD":"Auto","BALKRISIND":"Auto","MRF":"Auto","APOLLOTYRE":"Auto","EXIDEIND":"Auto",
    "TMPV":"Auto","SAMVARDHANA":"Auto","MINDAIND":"Auto","SONACOMS":"Auto","SUNDRMFAST":"Auto",
    # FMCG
    "ITC":"FMCG","HINDUNILVR":"FMCG","NESTLEIND":"FMCG","BRITANNIA":"FMCG","DABUR":"FMCG",
    "MARICO":"FMCG","GODREJCP":"FMCG","COLPAL":"FMCG","TATACONSUM":"FMCG","VBL":"FMCG","UBL":"FMCG","PGHH":"FMCG",
    # Pharma / Healthcare
    "SUNPHARMA":"Pharma","DRREDDY":"Pharma","CIPLA":"Pharma","DIVISLAB":"Pharma","AUROPHARMA":"Pharma",
    "LUPIN":"Pharma","BIOCON":"Pharma","ALKEM":"Pharma","TORNTPHARM":"Pharma","ZYDUSLIFE":"Pharma",
    "GLENMARK":"Pharma","LAURUSLABS":"Pharma","APOLLOHOSP":"Pharma","MAXHEALTH":"Pharma","SYNGENE":"Pharma","GRANULES":"Pharma",
    # Metal
    "TATASTEEL":"Metal","JSWSTEEL":"Metal","HINDALCO":"Metal","VEDL":"Metal","JINDALSTEL":"Metal",
    "SAIL":"Metal","NMDC":"Metal","NATIONALUM":"Metal","HINDZINC":"Metal","APLAPOLLO":"Metal","JSWENERGY":"Energy",
    # Energy / Oil & Gas / Power
    "RELIANCE":"Energy","ONGC":"Energy","BPCL":"Energy","IOC":"Energy","GAIL":"Energy",
    "NTPC":"Energy","POWERGRID":"Energy","COALINDIA":"Energy","TATAPOWER":"Energy","ADANIENT":"Energy",
    "ADANIPORTS":"Energy","ADANIGREEN":"Energy","ADANIPOWER":"Energy","HINDPETRO":"Energy","PETRONET":"Energy","IGL":"Energy","OIL":"Energy",
    # Realty / Infra / Cement
    "DLF":"Realty","LODHA":"Realty","GODREJPROP":"Realty","OBEROIRLTY":"Realty","PRESTIGE":"Realty",
    "LT":"Realty","ULTRACEMCO":"Realty","GRASIM":"Realty","AMBUJACEM":"Realty","ACC":"Realty","SHREECEM":"Realty","DALBHARAT":"Realty",
    # Media / Telecom
    "BHARTIARTL":"Media","IDEA":"Media","ZEEL":"Media","SUNTV":"Media","PVRINOX":"Media","INDUSTOWER":"Media",
    # Consumer durables / others
    "TITAN":"ConsDur","HAVELLS":"ConsDur","VOLTAS":"ConsDur","CROMPTON":"ConsDur","DIXON":"ConsDur",
    "KALYANKJIL":"ConsDur","BLUESTARCO":"ConsDur","POLYCAB":"ConsDur","BATAINDIA":"ConsDur","PIDILITIND":"ConsDur",
    "IRCTC":"Other","IRFC":"Other","BEL":"Other","HAL":"Other","BDL":"Other","COCHINSHIP":"Other",
    "TRENT":"Other","NYKAA":"Other","ZOMATO":"Other","INDIGO":"Other","DMART":"Other","CGPOWER":"Other",
    "SIEMENS":"Other","ABB":"Other","BHEL":"Other","GMRAIRPORT":"Other","INDHOTEL":"Other",
}
def _sector_of(sym):
    return _STOCK_SECTOR.get(sym, "Other")

def fnum_safe(v, d=0.0):
    try: return float(v)
    except: return d

def fetch_sectors(nse):
    """
    Fetch all sectoral index changes via listIndices(),
    return sorted list strongest-first.
    """
    sectors = []
    data_rows = []
    try:
        raw = nse.listIndices()
        if raw and isinstance(raw, dict) and "data" in raw:
            data_rows = raw["data"]
    except Exception as e:
        print(f"  [sectors err: {e}]")

    # Build name -> pct change map. listIndices rows have varied key names.
    name_to_chg = {}
    for row in data_rows:
        nm  = (row.get("index") or row.get("indexSymbol")
               or row.get("key") or "")
        chg = (row.get("percentChange") if row.get("percentChange") is not None
               else row.get("pChange"))
        if nm and chg is not None:
            try: name_to_chg[str(nm).upper().strip()] = round(float(chg), 2)
            except: pass

    for idx_name, short in _SECTORS:
        chg = name_to_chg.get(idx_name.upper())
        if chg is not None:
            sectors.append({"name": short, "full": idx_name, "chg": chg})

    sectors.sort(key=lambda x: x["chg"], reverse=True)
    return sectors

# -- SIGNAL ENGINE -------------------------------------------------------------
def _sc(mp, val, lo, hi, inv=False):
    if hi == lo: return 0.0
    r = (val - lo) / (hi - lo)
    if inv: r = 1 - r
    return max(0.0, min(1.0, r)) * mp

def parse_expiry(s):
    try: return datetime.datetime.strptime(s, "%d-%b-%Y").date()
    except: return datetime.date.max

def max_pain(strikes):
    if not strikes: return None
    best_k = None; best_pain = float('inf')
    for K in strikes:
        p = sum(max(0,S-K)*d["c_oi"] + max(0,K-S)*d["p_oi"]
                for S,d in strikes.items())
        if p < best_pain: best_pain = p; best_k = K
    return best_k

def momentum_score(day, nifty_chg):
    """
    A SEPARATE edge from OI: pure price momentum.
    Big move + big volume + position in day range = something is running.
    This catches stocks like HAL +4.84% that OI signals miss.
    Returns dict with score 0-100 and direction, or None.

    day = {"open","high","low","prev","last","vol","pchg"}
    """
    if not day: return None
    pchg = day.get("pchg", 0)
    last = day.get("last", 0)
    high = day.get("high", 0)
    low  = day.get("low", 0)
    opn  = day.get("open", 0)
    if not last or not high or not low or high <= low:
        return None

    # Direction from day change
    direction = "up" if pchg > 0 else "down"
    absmove = abs(pchg)

    sc = 0
    reasons = []

    # 1. Size of move (max 40 pts) - the bigger the move, the stronger
    if absmove >= 4:    sc += 40; reasons.append(f"Big move {pchg:+.1f}%")
    elif absmove >= 2.5: sc += 30; reasons.append(f"Strong move {pchg:+.1f}%")
    elif absmove >= 1.5: sc += 20; reasons.append(f"Move {pchg:+.1f}%")
    elif absmove >= 0.8: sc += 10
    else: return None  # not enough momentum to care

    # 2. Position in day range (max 30 pts)
    # For UP momentum: price near day HIGH = strong (closing at highs)
    # For DOWN: price near day LOW = strong
    rng = high - low
    pos = (last - low) / rng if rng else 0.5  # 0=at low, 1=at high
    if direction == "up":
        if pos >= 0.85:   sc += 30; reasons.append("Near day high")
        elif pos >= 0.7:  sc += 20; reasons.append("Upper range")
        elif pos >= 0.5:  sc += 10
        else:             sc -= 10; reasons.append("Faded from high")
    else:
        if pos <= 0.15:   sc += 30; reasons.append("Near day low")
        elif pos <= 0.3:  sc += 20; reasons.append("Lower range")
        elif pos <= 0.5:  sc += 10
        else:             sc -= 10; reasons.append("Bounced from low")

    # 3. Opening drive (max 15 pts) - did it open and keep going?
    if opn and last:
        drive = (last - opn) / opn * 100
        if direction == "up" and drive > 0.5:
            sc += 15; reasons.append("Held gains from open")
        elif direction == "down" and drive < -0.5:
            sc += 15; reasons.append("Held losses from open")

    # 4. Beating NIFTY (max 15 pts) - real relative strength
    rs = pchg - nifty_chg
    if direction == "up" and rs > 1:
        sc += 15; reasons.append(f"RS +{rs:.1f}% vs NIFTY")
    elif direction == "down" and rs < -1:
        sc += 15; reasons.append(f"RS {rs:.1f}% vs NIFTY")

    sc = max(0, min(100, sc))
    return {
        "mom_score": sc,
        "mom_dir": direction,
        "mom_pchg": round(pchg, 2),
        "mom_pos": round(pos, 2),  # position in day range
        "mom_reasons": reasons[:3],
    }

def live_pressure(sym, snap, direction):
    """
    THE LIVE-PRESSURE ENGINE.
    Measures the scan-to-scan CHANGE across ALL dimensions - price, OI, IV,
    volume, PCR - to answer: is the full picture shifting in one direction
    RIGHT NOW, or is it a stale snapshot?

    Noise reduction: compares the CURRENT scan against the AVERAGE of the last
    2-3 scans (smoothed baseline), not a single prior scan. A one-scan blip
    won't fire; only a sustained multi-scan shift registers.

    snap = current snapshot {"spot","c_oi","p_oi","atm_iv","vol","pcr"}
    direction = "up" (bullish setup) or "down" (bearish setup)

    Returns dict with per-dimension velocities, a combined LIVE PRESSURE score
    (-100..+100, positive = pressure building in the signal direction), and a
    state label: SURGING / LIVE / BUILDING / FLAT / FADING / NEW.
    """
    hist = _price_hist.get(sym, [])
    empty = {"lp_price":0,"lp_oi":0,"lp_iv":0,"lp_vol":0,"lp_pcr":0,
             "lp_score":0,"lp_state":"NEW","lp_note":"Building history...",
             "lp_signals":[]}
    if len(hist) < 2:
        return empty

    up = (direction == "up")

    # Smoothed baseline = average of last 2-3 snapshots (not just the last one)
    base = hist[-3:] if len(hist) >= 3 else hist[-2:]
    def avg(key):
        vals = [s.get(key,0) for s in base if s.get(key) is not None]
        return sum(vals)/len(vals) if vals else 0

    b_spot = avg("spot"); b_coi = avg("c_oi"); b_poi = avg("p_oi")
    b_iv   = avg("atm_iv"); b_vol = avg("vol"); b_pcr = avg("pcr")

    # ---- 1. PRICE velocity (% move vs smoothed baseline) ----
    price_v = ((snap["spot"] - b_spot) / b_spot * 100) if b_spot else 0
    dir_price = price_v if up else -price_v   # + = moving our way

    # ---- 2. OI velocity ----
    # For a bull (call) setup: we want PUT OI building (floor) OR call OI NOT
    # building too fast. Put writers = floor = bullish.
    # For a bear (put) setup: we want CALL OI building (ceiling).
    coi_chg = ((snap["c_oi"] - b_coi) / b_coi * 100) if b_coi else 0
    poi_chg = ((snap["p_oi"] - b_poi) / b_poi * 100) if b_poi else 0
    if up:
        # bullish: put OI building good (+), call OI building bad (-)
        oi_v = poi_chg - coi_chg
    else:
        # bearish: call OI building good (+), put OI building bad (-)
        oi_v = coi_chg - poi_chg

    # ---- 3. IV velocity (rising IV = players paying up = conviction entering)
    iv_chg = ((snap["atm_iv"] - b_iv) / b_iv * 100) if b_iv else 0
    # Rising IV supports a live move either direction; treat as conviction fuel
    iv_v = iv_chg

    # ---- 4. VOLUME acceleration (this scan's volume vs baseline) ----
    vol_chg = ((snap["vol"] - b_vol) / b_vol * 100) if b_vol else 0
    vol_v = vol_chg   # surge = live participation

    # ---- 5. PCR shift (sentiment turning) ----
    pcr_chg = (snap["pcr"] - b_pcr)   # absolute PCR change
    # Rising PCR = more puts = bearish; falling PCR = bullish
    if up:
        pcr_v = -pcr_chg * 100   # PCR falling (bullish) = positive
    else:
        pcr_v =  pcr_chg * 100   # PCR rising (bearish) = positive

    # ---- COMBINE into a single live-pressure score (weighted) ----
    # Price leads (it's the outcome). OI + PCR = positioning. IV + Vol = fuel.
    # Weights tuned so no single dimension dominates -> less noise.
    lp = (dir_price * 8.0      # price move is the strongest evidence
        + max(-15,min(15,oi_v)) * 1.2   # OI shift (capped, OI % can be wild)
        + max(-20,min(20,iv_v)) * 0.5   # IV change (capped)
        + max(-50,min(50,vol_v)) * 0.15 # volume surge (capped)
        + max(-30,min(30,pcr_v)) * 0.4) # PCR shift (capped)
    lp = max(-100, min(100, round(lp)))

    # Build human-readable signal list (only meaningful shifts)
    signals = []
    if abs(dir_price) >= 0.25:
        signals.append(f"Price {'+' if price_v>=0 else ''}{price_v:.1f}% vs recent")
    if up and poi_chg > 3:
        signals.append(f"Put OI +{poi_chg:.0f}% (floor building live)")
    if up and coi_chg > 3:
        signals.append(f"Call OI +{coi_chg:.0f}% (ceiling - caution)")
    if not up and coi_chg > 3:
        signals.append(f"Call OI +{coi_chg:.0f}% (ceiling building live)")
    if not up and poi_chg > 3:
        signals.append(f"Put OI +{poi_chg:.0f}% (floor - caution)")
    if iv_chg > 2:
        signals.append(f"IV rising +{iv_chg:.0f}% (conviction entering)")
    elif iv_chg < -3:
        signals.append(f"IV falling {iv_chg:.0f}% (interest fading)")
    if vol_chg > 15:
        signals.append(f"Volume surge +{vol_chg:.0f}%")
    if up and pcr_chg < -0.05:
        signals.append(f"PCR falling (turning bullish)")
    if not up and pcr_chg > 0.05:
        signals.append(f"PCR rising (turning bearish)")

    # ---- STATE classification (needs price confirmation + pressure) ----
    if lp >= 45 and dir_price >= 0.3:
        state = "SURGING"; note = "Full pressure building NOW - best entry window"
    elif lp >= 25 and dir_price >= 0.15:
        state = "LIVE"; note = "Moving with support - live and tradeable"
    elif lp >= 12:
        state = "BUILDING"; note = "Pressure starting to build - watch closely"
    elif lp <= -20:
        state = "FADING"; note = "Pressure reversing - avoid"
    else:
        state = "FLAT"; note = "No live pressure - move already happened or stalled"

    return {
        "lp_price": round(price_v,2), "lp_oi": round(oi_v,1),
        "lp_iv": round(iv_chg,1), "lp_vol": round(vol_chg,0),
        "lp_pcr": round(pcr_chg,2),
        "lp_score": lp, "lp_state": state, "lp_note": note,
        "lp_signals": signals[:4],
    }

def score(chain_data, spot, day_chg, nifty_chg):
    if not chain_data or not spot or spot <= 0: return None

    # nearest expiry only
    dates = {r.get("expiryDate") or r.get("expiry") for r in chain_data if r.get("expiryDate") or r.get("expiry")}
    nearest = min(dates, key=parse_expiry) if dates else None
    rows = [r for r in chain_data if (r.get("expiryDate") or r.get("expiry")) == nearest] if nearest else chain_data
    if not rows: rows = chain_data

    strikes = {}
    tc_oi=tp_oi=tc_chg=tp_chg=tc_vol=tp_vol=0
    atm_civs=[]; atm_pivs=[]; band = spot*0.025

    for row in rows:
        st = row.get("strikePrice",0) or 0
        ce = row.get("CE") or {}; pe = row.get("PE") or {}
        coi=int(ce.get("openInterest",0) or 0)
        poi=int(pe.get("openInterest",0) or 0)
        cc =int(ce.get("changeinOpenInterest",0) or 0)
        pc =int(pe.get("changeinOpenInterest",0) or 0)
        cv =int(ce.get("totalTradedVolume",0) or 0)
        pv =int(pe.get("totalTradedVolume",0) or 0)
        civ=float(ce.get("impliedVolatility",0) or 0)
        piv=float(pe.get("impliedVolatility",0) or 0)
        tc_oi+=coi; tp_oi+=poi; tc_chg+=cc; tp_chg+=pc
        tc_vol+=cv; tp_vol+=pv
        if st:
            strikes[st]={"c_oi":coi,"p_oi":poi,"c_chg":cc,"p_chg":pc,
                         "c_iv":civ,"p_iv":piv,"c_vol":cv,"p_vol":pv}
            if abs(st-spot)<=band:
                if civ>1: atm_civs.append(civ)
                if piv>1: atm_pivs.append(piv)

    if tc_oi+tp_oi < MIN_OI: return None

    pcr      = round(tp_oi/tc_oi,2) if tc_oi else 9.99
    oi_bias  = tc_chg - tp_chg
    oi_dir   = "C+" if oi_bias>500 else ("P+" if oi_bias<-500 else "=")
    avg_civ  = sum(atm_civs)/len(atm_civs) if atm_civs else 0
    avg_piv  = sum(atm_pivs)/len(atm_pivs) if atm_pivs else 0
    atm_iv   = round((avg_civ+avg_piv)/2,1)
    iv_skew  = round(avg_piv - avg_civ, 1)
    rs       = round(day_chg - nifty_chg, 2)
    mp_st    = max_pain(strikes)
    pain_d   = round((spot-mp_st)/mp_st*100,1) if mp_st else 0
    vr       = round(tc_vol/tp_vol,2) if tp_vol else (2.0 if tc_vol else 1.0)

    # ATM concentration
    if strikes:
        atm_st = min(strikes.keys(), key=lambda x: abs(x-spot))
        c_conc = strikes[atm_st]["c_oi"]/tc_oi if tc_oi else 0
        p_conc = strikes[atm_st]["p_oi"]/tp_oi if tp_oi else 0
    else:
        c_conc=p_conc=0; atm_st=0

    # ---- BULL (100 pts) ----
    b  = _sc(20, pcr, 1.5, 0.3, inv=True)          # S1 PCR
    b += 20 if (oi_bias<-1000 and day_chg>0.5) else \
         13 if (oi_bias<-500  and day_chg>0)   else \
         7  if  oi_bias<0 else 0                    # S2 OI+price
    b += 15 if 0.5<=pain_d<=2.5 else \
         5  if pain_d>2.5 else \
         10 if -2.5<=pain_d<-0.5 else \
         15 if pain_d<-2.5 else 5                   # S3 max pain
    b += 15 if iv_skew<-3 else \
         10 if iv_skew<-1 else \
         7  if iv_skew<1  else \
         3  if iv_skew<3  else 0                    # S4 IV skew
    b += _sc(15, rs, -3.0, 3.0)                     # S5 rel strength
    b += 10 if (spot>atm_st and c_conc>0.15) else \
         5  if c_conc>0.20 else 3                   # S6 ATM conc
    b += 5 if vr<0.5 else 3 if vr<0.75 else \
         2 if vr<1.5 else 0                         # S7 volume
    if atm_iv>25: b+=5
    elif atm_iv>18: b+=3

    # ---- BEAR (100 pts) ----
    r  = _sc(20, pcr, 0.7, 2.5)                     # S1 PCR
    r += 20 if (oi_bias>1000 and day_chg<-0.5) else \
         13 if (oi_bias>500  and day_chg<0)    else \
         7  if  oi_bias>0 else 0                    # S2 OI+price
    r += 15 if -2.5<=pain_d<-0.5 else \
         10 if pain_d>2.5 else \
         5  if 0.5<=pain_d<=2.5 else \
         15 if pain_d<-2.5 else 5                   # S3 max pain
    r += 15 if iv_skew>3  else \
         10 if iv_skew>1  else \
         7  if iv_skew>-1 else \
         3  if iv_skew>-3 else 0                    # S4 IV skew
    r += _sc(15, rs, 3.0, -3.0)                     # S5 rel strength
    r += 10 if (spot<atm_st and p_conc>0.15) else \
         5  if p_conc>0.20 else 3                   # S6 ATM conc
    r += 5 if vr>2.0 else 3 if vr>1.5 else \
         2 if vr>0.75 else 0                        # S7 volume
    if atm_iv>25: r+=5
    elif atm_iv>18: r+=3

    bull = min(int(b), 100); bear = min(int(r), 100)

    # probability label
    def label(s, n):
        net = s - n
        if s >= 75 and net >= 20: return "HIGH"
        if s >= 60 and net >= 12: return "MEDIUM"
        if s >= 45 and net >= 5:  return "LOW"
        return ""

    return {
        "spot": spot, "chg": day_chg, "rs": rs, "pcr": pcr,
        "oi_dir": oi_dir, "atm_iv": atm_iv, "iv_skew": iv_skew,
        "max_pain": mp_st or 0, "pain_dist": pain_d,
        "c_oi": tc_oi, "p_oi": tp_oi, "expiry": nearest or "?",
        "bull": bull, "bear": bear,
        "bull_label": label(bull, bear), "bear_label": label(bear, bull),
    }

# -- CSV -----------------------------------------------------------------------
_FIELDS = ["date","time","symbol","spot","chg","rs","pcr","oi_dir",
           "atm_iv","iv_skew","max_pain","pain_dist","c_oi","p_oi",
           "expiry","bull","bear","bull_label","bear_label",
           "call_rank",       # rank in top 5 calls this scan (1=best)
           "put_rank",        # rank in top 5 puts this scan (1=best)
           "call_conv",       # call conviction 0-100 (decay-based, smooth)
           "put_conv",        # put conviction 0-100 (decay-based, smooth)
           "call_trend",      # rising / steady / fading
           "put_trend",       # rising / steady / fading
           "call_avg",        # avg bull score over recent scans
           "put_avg",         # avg bear score over recent scans
           "lp_smooth",       # smoothed live pressure (EMA)
           "first_call",      # time signal first appeared (HH:MM)
           "first_put",       # time signal first appeared (HH:MM)
           "align_score",     # 0-3: how many of 3 alignment layers agree
           "nifty_spot",      # NIFTY level at scan time (market backdrop)
           "nifty_chg",       # NIFTY day change % at scan time
           "atm_strike",      # ATM strike at scan time
           "call_trigger",    # price spot must BREAK ABOVE to enter call
           "put_trigger",     # price spot must BREAK BELOW to enter put
           "call_trig_src",   # WHICH level the call trigger is based on
           "put_trig_src",    # WHICH level the put trigger is based on
           "est_premium",     # estimated option premium at scan time
           "call_target",     # premium target (+30%) for call trade
           "put_target",      # premium target (+30%) for put trade
           "mom_score",       # momentum score 0-100 (price+volume edge)
           "mom_dir",         # up/down momentum direction
           "mom_pchg",        # day change driving momentum
           "mom_pos",         # position in day range (1=at high, 0=at low)
           "lp_price",        # price % change vs smoothed recent baseline
           "lp_oi",           # OI shift velocity (dir-adjusted)
           "lp_iv",           # IV % change vs baseline
           "lp_vol",          # volume % change vs baseline
           "lp_pcr",          # PCR absolute change vs baseline
           "lp_score",        # combined LIVE PRESSURE -100..+100
           "lp_state",        # SURGING/LIVE/BUILDING/FLAT/FADING
           "confluence",      # True if in BOTH OI top-5 AND momentum
           "rule_side",       # which side these rules evaluate (call/put)
           "rule_pos",        # Y/N positioning strong (conviction>=40)
           "rule_align",      # Y/N market+sector aligned
           "rule_moving",     # Y/N moving our way now
           "rule_trig",       # Y/N trigger within reach
           "rule_confl",      # Y/N confluence
           "rule_auto_count", # how many of 5 auto-rules passed (0-5)
           "fav_pcr",         # PCR favors: call / put / neutral
           "fav_oi",          # OI direction favors: call / put / neutral
           "fav_pain",        # max pain favors: call / put / neutral
           "fav_rs",          # relative strength favors: call / put / neutral
           "fav_now",         # live pressure favors: call / put / neutral
           "fav_call_count",  # how many signals favor CALL (0-5)
           "fav_put_count",   # how many signals favor PUT (0-5)
           "fav_agree",       # do most signals agree with the chosen side? Y/N
           # --- YOU FILL THESE MANUALLY AFTER MARKET (chart + outcome) ---
           # pivot_broken     Y/N - pivot/level broken with volume
           # retest_held      Y/N - price retested and held
           # ema9_ok          Y/N - correct side of 9 EMA at retest
           # entry_time       when you actually entered
           # entry_premium    real premium paid
           # exit_premium     premium at exit
           # exit_reason      target / stop / time
           # result           WIN / LOSS / NO_ENTRY
           # --- YOU FILL THESE MANUALLY AFTER MARKET ---
           # trigger_hit      Y/N - did price cross the trigger?
           # actual_premium   real option price when trigger was hit
           # exit_premium     option price at exit (target/stop/time)
           # result           WIN / LOSS / NO_ENTRY
           ]

def _strike_step(spot):
    if spot < 200:  return 5
    if spot < 500:  return 10
    if spot < 1000: return 20
    if spot < 2000: return 50
    if spot < 5000: return 100
    return 200

def compute_entry_plan(spot, max_pain, day=None, orange=None):
    """
    Compute a REAL entry trigger from actual price structure, combining:
      1. Opening Range high/low (9:15-9:45) - strongest intraday breakout level
      2. Day's high / low - momentum breakout beyond today's extreme
      3. Max Pain / big OI strike - where option writers defend
      4. Fallback: small 0.4% breakout if no structure available

    The trigger is the NEAREST meaningful level above (call) / below (put) spot,
    so price doesn't have to travel far, but the level actually means something.

    day    = {"open","high","low","prev","last"} for this stock (may be None)
    orange = {"or_high","or_low"} opening range (may be None early in day)
    """
    step  = _strike_step(spot)
    atm   = round(spot / step) * step
    prem  = max(5, round(spot * 0.013 / step) * step)

    # ---- Gather candidate levels ABOVE spot (for call trigger) ----
    call_levels = []
    put_levels  = []

    # 1. Opening range
    if orange:
        if orange.get("or_high", 0) > spot:
            call_levels.append(("OR high", orange["or_high"]))
        if orange.get("or_low", 0) and orange["or_low"] < spot:
            put_levels.append(("OR low", orange["or_low"]))

    # 2. Day high / low
    if day:
        dh = day.get("high", 0); dl = day.get("low", 0)
        if dh > spot:
            call_levels.append(("Day high", dh))
        if dl and dl < spot:
            put_levels.append(("Day low", dl))
        # If spot is AT day high already (breaking out), trigger = tiny buffer above
        if dh and abs(spot - dh) / spot < 0.001:
            call_levels.append(("New high", round(dh + spot*0.002, 1)))
        if dl and abs(spot - dl) / spot < 0.001:
            put_levels.append(("New low", round(dl - spot*0.002, 1)))

    # 3. Max pain / OI wall
    if max_pain:
        if max_pain > spot:
            call_levels.append(("Max Pain", max_pain))
        elif max_pain < spot:
            put_levels.append(("Max Pain", max_pain))

    # 4. Fallback small breakout (always available)
    call_levels.append(("0.4% breakout", round(spot * 1.004, 1)))
    put_levels.append(("0.4% breakout", round(spot * 0.996, 1)))

    # ---- Pick the NEAREST level above spot (call) / below spot (put) ----
    # Nearest = most reachable, still a real level.
    call_above = [(lbl, lv) for lbl, lv in call_levels if lv > spot]
    put_below  = [(lbl, lv) for lbl, lv in put_levels  if lv < spot]

    if call_above:
        call_label, call_trig = min(call_above, key=lambda x: x[1] - spot)
    else:
        call_label, call_trig = "0.4% breakout", round(spot * 1.004, 1)

    if put_below:
        put_label, put_trig = min(put_below, key=lambda x: spot - x[1])
    else:
        put_label, put_trig = "0.4% breakout", round(spot * 0.996, 1)

    return {
        "atm_strike":   atm,
        "call_trigger": round(call_trig, 1),
        "put_trigger":  round(put_trig, 1),
        "call_trig_src": call_label,   # WHY this level (for display)
        "put_trig_src":  put_label,
        "est_premium":  prem,
        "call_target":  round(prem * 1.3),
        "put_target":   round(prem * 1.3),
    }

def save_csv(results, ts):
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"scan_{ts:%Y%m%d}.csv"
    new  = not path.exists()
    with open(path,"a",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
        if new: w.writeheader()
        for r in results:
            w.writerow({"date":f"{ts:%Y-%m-%d}","time":f"{ts:%H:%M:%S}",
                        "symbol":r["sym"],**{k:r.get(k,"") for k in _FIELDS[3:]}})

def mkt_open():
    t = datetime.datetime.now()
    return MARKET_START <= (t.hour, t.minute) < MARKET_END

# -- CONCURRENT FETCH ----------------------------------------------------------
def fetch_one_chain(nse, sym):
    """
    Fetch a SINGLE option chain. Pure I/O, no shared state touched - this is
    what makes it safe to run in many threads at once. Returns a tuple
    (sym, data, spot) or (sym, None, None) on any failure.
    """
    try:
        raw = nse.optionChain(sym)
        if not raw:
            return (sym, None, None)
        rec  = raw.get("records", {})
        data = rec.get("data", [])
        spot = rec.get("underlyingValue")
        if not spot or not data:
            return (sym, None, None)
        return (sym, data, spot)
    except Exception:
        return (sym, None, None)

def fetch_all_chains(nse, symbols):
    """
    Fetch ALL option chains in parallel using a thread pool.
    Returns {sym: (data, spot)} for every stock that returned valid data.

    `nse` here is whatever data source is active - the NSE client OR the
    UpstoxOI adapter. Both expose the same call (via fetch_one_chain), so
    the parallel machinery and everything downstream is identical.
    With MAX_WORKERS parallel fetches, ~200 stocks come back in ~10-20s.
    """
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one_chain, nse, s): s for s in symbols}
        for fut in as_completed(futures):
            try:
                sym, data, spot = fut.result(timeout=FETCH_TIMEOUT)
                if data and spot:
                    out[sym] = (data, spot)
            except Exception:
                continue
    return out


def fetch_one_chain_upstox(oi_client, sym):
    """Upstox version of fetch_one_chain - same return shape (sym, data, spot)."""
    try:
        raw = oi_client.option_chain(sym)
        if not raw:
            return (sym, None, None)
        rec = raw.get("records", {})
        data = rec.get("data", [])
        spot = rec.get("underlyingValue")
        if not spot or not data:
            return (sym, None, None)
        return (sym, data, spot)
    except Exception:
        return (sym, None, None)


def fetch_all_chains_upstox(oi_client, symbols):
    """Parallel Upstox chain fetch. Same shape as fetch_all_chains."""
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one_chain_upstox, oi_client, s): s for s in symbols}
        for fut in as_completed(futures):
            try:
                sym, data, spot = fut.result(timeout=FETCH_TIMEOUT)
                if data and spot:
                    out[sym] = (data, spot)
            except Exception:
                continue
    return out

def _warmup_nse(nse, tries=4):
    """
    NSE returns 403 Forbidden until a browser-like session with valid
    cookies is established. The nse library sets cookies on first contact,
    but under some networks/first-run it needs a nudge + retry. We hit a
    couple of light endpoints and retry with backoff until they stop 403ing.
    """
    import time as _t
    for attempt in range(1, tries + 1):
        try:
            # a light call that forces cookie establishment
            status = nse.status()
            if status:
                return True
        except Exception as e:
            pass
        # also try to (re)fetch the equity list which is what 403'd
        try:
            raw = nse.listEquityStocksByIndex(index="SECURITIES IN F&O")
            if raw and isinstance(raw, dict) and raw.get("data"):
                return True
        except Exception:
            pass
        wait = attempt * 2
        print(f"  NSE warming up (403 blocked, retry {attempt}/{tries} in {wait}s)...",
              flush=True)
        _t.sleep(wait)
    print("  NSE still blocking after warmup - will use fallback list and keep retrying live.")
    return False


def _day_data_from_chains(chains):
    """
    Build chg_map / day_map / movers from the Upstox chains we already fetched.
    The chain gives us spot (underlyingValue). For intraday high/low/change we
    track per-symbol running extremes across scans in _day_track, and compute
    % change vs the first spot we saw today (a clean intraday proxy).
    """
    chg_map, day_map, movers = {}, {}, []
    for sym, (data, spot) in chains.items():
        spot = float(spot or 0)
        if not spot:
            continue
        tr = _day_track.setdefault(sym, {"open": spot, "high": spot, "low": spot, "prev": spot})
        tr["high"] = max(tr["high"], spot)
        tr["low"]  = min(tr["low"], spot)
        # % change vs today's first-seen spot (intraday move proxy)
        base = tr["open"] or spot
        pchg = round((spot - base) / base * 100, 2) if base else 0.0
        chg_map[sym] = pchg
        day_map[sym] = {
            "open": tr["open"], "high": tr["high"], "low": tr["low"],
            "prev": tr["open"], "last": spot, "vol": 0, "pchg": pchg,
        }
        movers.append({"sym": sym, "chg": pchg})
    movers.sort(key=lambda x: abs(x["chg"]), reverse=True)
    return chg_map, day_map, movers[:25]


def _nifty_from_ctx(nse_ctx):
    """NIFTY level/change - try NSE index (light, wrapped); fall back to blank."""
    if nse_ctx is not None:
        try:
            return fetch_nifty(nse_ctx)
        except Exception:
            pass
    return "NIFTY", 0.0, 0.0


# per-symbol intraday tracking for the Upstox day-data proxy
_day_track = {}


def _resolve_source():
    """Decide Upstox vs NSE. Returns ('upstox', oi_client, inst_map) or ('nse', None, None)."""
    want = str(DATA_SOURCE).lower()
    if want == "upstox":
        try:
            import upstox_auth, instruments as _inst, config as _cfg
            from upstox_oi import UpstoxOI
            token = upstox_auth.load_token()
            if not token:
                print("  [DATA] Upstox requested but no token - falling back to NSE.")
                return ("nse", None, None)
            inst_map = _inst.resolve_universe(_cfg)
            if not inst_map:
                print("  [DATA] Could not map F&O instruments - falling back to NSE.")
                return ("nse", None, None)
            oi = UpstoxOI(token, inst_map)
            print(f"  [DATA] Using UPSTOX option-chain API ({len(inst_map)} F&O stocks).")
            return ("upstox", oi, inst_map)
        except Exception as e:
            print(f"  [DATA] Upstox init failed ({e}) - falling back to NSE.")
            return ("nse", None, None)
    return ("nse", None, None)


def scanner_main():
    source, oi_client, inst_map = _resolve_source()

    if source == "upstox":
        # Upstox path: reliable, no NSE object needed for chains.
        # We still open a light NSE client ONLY for NIFTY bias + sectors
        # (index-level context), but wrap it so its 403s never break the scan.
        wl = sorted(inst_map.keys())
        _nse_ctx = None
        try:
            _nse_ctx = NSE(download_folder=Path("."))
            _warmup_nse(_nse_ctx, tries=2)
        except Exception:
            _nse_ctx = None
        with _lock: _state["total_stocks"] = len(wl)
        _run_scan_loop(source, oi_client, _nse_ctx, wl)
    else:
        # NSE path (original behaviour)
        with NSE(download_folder=Path(".")) as nse:
            _warmup_nse(nse)
            wl = fetch_universe(nse)
            with _lock: _state["total_stocks"] = len(wl)
            _run_scan_loop(source, nse, nse, wl)


def _run_scan_loop(source, chain_src, nse_ctx, wl):
    """The scan loop, shared by both data sources.
    chain_src = UpstoxOI (upstox) or NSE client (nse) - fetches option chains.
    nse_ctx   = NSE client for index context (bias/sectors), or None.
    """
    while True:
            try:
                t0 = datetime.datetime.now()
                scan_n = _state["scan_num"] + 1
                print(f"  [{t0:%H:%M:%S}] Scan #{scan_n} | market data...",
                      end=" ", flush=True)

                # -- CHAINS (parallel) - the core conviction data ---------
                print(f"fetching {len(wl)} chains x{MAX_WORKERS}...", end=" ", flush=True)
                if source == "upstox":
                    chains = fetch_all_chains_upstox(chain_src, wl)
                    # derive day-change map + NIFTY context from chains/NSE
                    chg_map, day_map, movers = _day_data_from_chains(chains)
                    nifty_str, nifty_chg, nifty_spot = _nifty_from_ctx(nse_ctx)
                    nifty_bias = fetch_nifty_bias(nse_ctx) if nse_ctx else None
                    sectors    = fetch_sectors(nse_ctx) if nse_ctx else []
                else:
                    chg_map, day_map, movers = fetch_day_data(chain_src)
                    nifty_str, nifty_chg, nifty_spot = fetch_nifty(chain_src)
                    nifty_bias = fetch_nifty_bias(chain_src)
                    sectors    = fetch_sectors(chain_src)
                    chains = fetch_all_chains(chain_src, wl)

                # -- Update opening range (only during 9:15-9:45 window) --
                now_t = (t0.hour, t0.minute)
                in_or_window = now_t < _OR_END
                for sym, dd in day_map.items():
                    hi = dd.get("high", 0); lo = dd.get("low", 0)
                    if not hi or not lo: continue
                    o = _open_range.setdefault(sym, {"or_high":0,"or_low":1e12,"locked":False})
                    if in_or_window:
                        o["or_high"] = max(o["or_high"], hi)
                        o["or_low"]  = min(o["or_low"],  lo)
                    else:
                        o["locked"] = True

                # -- SEQUENTIAL COMPUTE: unchanged logic, ordered & safe ---
                # Runs single-threaded so all shared-state (_price_hist,
                # _persist, _lp_smooth) stays consistent - identical to before.
                results = []
                for sym in wl:
                    got = chains.get(sym)
                    if not got:
                        continue
                    data, spot = got
                    try:
                        sig = score(data, spot, chg_map.get(sym,0), nifty_chg)
                        if sig:
                            orng = _open_range.get(sym)
                            # only use opening range if it's locked (window over) and valid
                            or_use = None
                            if orng and orng.get("locked") and orng["or_low"] < 1e11:
                                or_use = orng
                            entry = compute_entry_plan(
                                spot, sig.get("max_pain", 0),
                                day=day_map.get(sym), orange=or_use)
                            # momentum is a SEPARATE edge - compute alongside
                            mom = momentum_score(day_map.get(sym), nifty_chg) or {}
                            mdir = mom.get("mom_dir", "up")
                            # LIVE PRESSURE - scan-to-scan change across ALL dims
                            dvol = (day_map.get(sym) or {}).get("vol", 0)
                            snap = {
                                "t": t0.strftime("%H:%M"), "spot": spot,
                                "c_oi": sig.get("c_oi",0), "p_oi": sig.get("p_oi",0),
                                "atm_iv": sig.get("atm_iv",0), "vol": dvol,
                                "pcr": sig.get("pcr",1),
                            }
                            lp = live_pressure(sym, snap, mdir)
                            results.append({"sym":sym, **sig, **entry, **mom, **lp})
                            # store this scan's snapshot AFTER computing (compares
                            # against past scans, not itself)
                            hist = _price_hist.setdefault(sym, [])
                            hist.append(snap)
                            _price_hist[sym] = hist[-5:]  # keep last 5 scans
                    except Exception:
                        continue

                if results:
                    # -- compute ranks and alignment scores ------------------
                    # Sort separately for call and put ranking
                    call_sorted = sorted(
                        [r for r in results if r.get("bull_label")],
                        key=lambda x: x["bull"], reverse=True
                    )
                    put_sorted = sorted(
                        [r for r in results if r.get("bear_label")],
                        key=lambda x: x["bear"], reverse=True
                    )
                    # Build rank lookup dicts
                    call_ranks = {r["sym"]: i+1 for i, r in enumerate(call_sorted)}
                    put_ranks  = {r["sym"]: i+1 for i, r in enumerate(put_sorted)}

                    # Compute align score (0-3) for each result
                    # Same logic as the JS: NIFTY dir + RS + OI dir
                    def align_sc(r, side):
                        ib = (side == "call")
                        sc = 0
                        if ib and nifty_chg > 0.1:  sc += 1
                        elif not ib and nifty_chg < -0.1: sc += 1
                        if ib and r.get("rs", 0) > 0.5:   sc += 1
                        elif not ib and r.get("rs", 0) < -0.5: sc += 1
                        if ib and r.get("oi_dir") == "P+":  sc += 1
                        elif not ib and r.get("oi_dir") == "C+": sc += 1
                        return sc

                    # Attach rank + align + nifty context to every result row
                    for r in results:
                        sym = r["sym"]
                        r["call_rank"]   = call_ranks.get(sym, "")
                        r["put_rank"]    = put_ranks.get(sym, "")
                        r["align_score"] = max(align_sc(r,"call"), align_sc(r,"put"))
                        r["nifty_spot"]  = nifty_spot
                        r["nifty_chg"]   = round(nifty_chg, 2)

                    # -- UPDATE PERSISTENCE (the sustainability engine) ------
                    # -- CONVICTION ENGINE (decay-based, not hard reset) ------
                    # Instead of streak counters that reset to 0 on one miss,
                    # each stock has a CONVICTION score (0-100) that:
                    #   - RISES when the signal is present and strong (rewards holding)
                    #   - DECAYS gradually when the signal weakens (not wiped to 0)
                    #   - RECOVERS if the signal returns (a fader can come back)
                    # This kills the flicker: a stock that misses one scan drops a
                    # little, stays on the board, and climbs back if it returns.
                    now_hhmm = t0.strftime("%H:%M")
                    RISE = 0.55      # how fast conviction builds on a present signal
                    DECAY = 0.72     # multiplier when signal absent (keeps 72% each scan)
                    result_syms = {r["sym"] for r in results}

                    for r in results:
                        sym = r["sym"]
                        p = _persist.setdefault(sym, {
                            "call_conv":0.0, "put_conv":0.0,
                            "first_call":"", "first_put":"",
                            "call_scores":[], "put_scores":[],
                            "call_seen":0, "put_seen":0,
                        })
                        # ---- CALL conviction ----
                        if r["bull"] >= _PERSIST_THRESHOLD and r.get("bull_label"):
                            # target = how strong the signal is now (0-100 scaled)
                            target = r["bull"]
                            # move conviction toward target, weighted by RISE
                            p["call_conv"] += (target - p["call_conv"]) * RISE
                            if not p["first_call"]:
                                p["first_call"] = now_hhmm
                            p["call_scores"].append(r["bull"])
                            p["call_scores"] = p["call_scores"][-10:]
                            p["call_seen"] += 1
                        else:
                            # signal absent this scan - DECAY, don't reset
                            p["call_conv"] *= DECAY
                            if p["call_conv"] < 8:  # fully faded, clear memory
                                p["call_conv"] = 0.0; p["first_call"] = ""; p["call_scores"] = []
                        # ---- PUT conviction ----
                        if r["bear"] >= _PERSIST_THRESHOLD and r.get("bear_label"):
                            target = r["bear"]
                            p["put_conv"] += (target - p["put_conv"]) * RISE
                            if not p["first_put"]:
                                p["first_put"] = now_hhmm
                            p["put_scores"].append(r["bear"])
                            p["put_scores"] = p["put_scores"][-10:]
                            p["put_seen"] += 1
                        else:
                            p["put_conv"] *= DECAY
                            if p["put_conv"] < 8:
                                p["put_conv"] = 0.0; p["first_put"] = ""; p["put_scores"] = []

                    # Decay stocks NOT scanned this round (shouldn't happen, but safe)
                    for sym, p in _persist.items():
                        if sym not in result_syms:
                            p["call_conv"] *= DECAY; p["put_conv"] *= DECAY
                            if p["call_conv"] < 8: p["call_conv"] = 0.0
                            if p["put_conv"] < 8: p["put_conv"] = 0.0

                    # Attach conviction + avg score to each result
                    for r in results:
                        sym = r["sym"]
                        p = _persist.get(sym, {})
                        cs = p.get("call_scores", [])
                        ps = p.get("put_scores", [])
                        r["call_conv"]  = round(p.get("call_conv", 0))
                        r["put_conv"]   = round(p.get("put_conv", 0))
                        r["call_seen"]  = p.get("call_seen", 0)
                        r["put_seen"]   = p.get("put_seen", 0)
                        r["first_call"] = p.get("first_call", "")
                        r["first_put"]  = p.get("first_put", "")
                        r["call_avg"]   = round(sum(cs)/len(cs)) if cs else r["bull"]
                        r["put_avg"]    = round(sum(ps)/len(ps)) if ps else r["bear"]
                        r["call_swing"] = (max(cs)-min(cs)) if len(cs) >= 2 else 0
                        r["put_swing"]  = (max(ps)-min(ps)) if len(ps) >= 2 else 0
                        # conviction TREND: is it rising or fading right now?
                        r["call_trend"] = ("rising" if r["bull"] >= _PERSIST_THRESHOLD
                                           and r["call_conv"] < r["bull"] else
                                           "fading" if r["bull"] < _PERSIST_THRESHOLD
                                           and r["call_conv"] > 8 else "steady")
                        r["put_trend"]  = ("rising" if r["bear"] >= _PERSIST_THRESHOLD
                                           and r["put_conv"] < r["bear"] else
                                           "fading" if r["bear"] < _PERSIST_THRESHOLD
                                           and r["put_conv"] > 8 else "steady")

                    elapsed = int((datetime.datetime.now()-t0).total_seconds())
                    wait    = max(POLL_SECONDS-elapsed, 10)

                    # -- RANK BY CONVICTION (smooth, decay-based) -------------
                    # === UNIFIED SCORING - ONE SCORE, ONE LIST ===============
                    # Blend everything into a single 0-100 FINAL score per stock
                    # per direction. This replaces separate OI / momentum lists.
                    #
                    # Ingredients (kid-simple):
                    #   OI conviction  = is smart money positioned this way? (0-100)
                    #   Live pressure  = is it moving that way RIGHT NOW? (-100..100)
                    #   Alignment      = do NIFTY + sector + stock agree? (0-3)
                    #   Confluence     = do BOTH positioning AND move agree? bonus
                    #
                    # First compute smoothed live pressure (carries between scans).
                    SMOOTH = 0.5
                    for r in results:
                        sym = r["sym"]
                        pp = _lp_smooth.setdefault(sym, {"sp":0.0, "seen":0})
                        raw_lp = r.get("lp_score", 0)
                        if r.get("mom_score", 0) >= 40:
                            pp["sp"] += (raw_lp - pp["sp"]) * SMOOTH
                            pp["seen"] += 1
                        else:
                            pp["sp"] *= 0.6
                            if abs(pp["sp"]) < 4: pp["sp"] = 0.0
                        r["lp_smooth"] = round(pp["sp"])
                    for sym, pp in _lp_smooth.items():
                        if sym not in result_syms:
                            pp["sp"] *= 0.6
                            if abs(pp["sp"]) < 4: pp["sp"] = 0.0

                    def final_score(r, side, tilt):
                        """One blended 0-100 score. side='call'/'put'.
                        tilt: 0.0 = pure positioning, higher = more movement.
                        """
                        ib = (side == "call")
                        conv = r["call_conv"] if ib else r["put_conv"]   # 0-100
                        lp   = r.get("lp_smooth", 0)                     # -100..100
                        mdir = r.get("mom_dir", "up")
                        aligned = (ib and mdir=="up") or (not ib and mdir=="down")
                        lp_dir = lp if aligned else -abs(lp)*0.3
                        mom = r.get("mom_score", 0) if aligned else 0
                        asc  = max(align_sc(r,"call") if ib else 0,
                                   align_sc(r,"put") if not ib else 0)
                        t = tilt
                        pos_w  = 0.65 * (1 - t*0.5)
                        move_w = 0.4  + t*0.5
                        mom_w  = t*0.25
                        base = conv * pos_w
                        move = max(-25, min(45, lp_dir*move_w))
                        mom_bonus = mom * mom_w
                        align_bonus = asc * 4
                        s = base + move + mom_bonus + align_bonus
                        return max(0, min(100, round(s)))

                    # Two views of the same stocks:
                    #   POSITIONING block = tilt 0.0 (where smart money is set up)
                    #   BALANCED block    = tilt 0.3 (positioning + movement evenly)
                    for r in results:
                        r["pos_call"]  = final_score(r, "call", 0.0)
                        r["pos_put"]   = final_score(r, "put",  0.0)
                        r["bal_call"]  = final_score(r, "call", 0.3)
                        r["bal_put"]   = final_score(r, "put",  0.3)
                        # keep final_call/put as the balanced one for CSV + rules
                        r["final_call"] = r["bal_call"]
                        r["final_put"]  = r["bal_put"]

                    # Confluence: positioned AND moving the same way
                    for r in results:
                        mdir = r.get("mom_dir","up")
                        strong_move = r.get("lp_smooth",0) >= 12 and r.get("mom_score",0) >= 40
                        r["conf_call"] = bool(strong_move and mdir=="up" and r["call_conv"]>=30)
                        r["conf_put"]  = bool(strong_move and mdir=="down" and r["put_conv"]>=30)
                        if r["conf_call"]:
                            r["bal_call"] = min(100, r["bal_call"]+10)
                            r["final_call"] = r["bal_call"]
                        if r["conf_put"]:
                            r["bal_put"]  = min(100, r["bal_put"]+10)
                            r["final_put"] = r["bal_put"]
                        r["confluence"] = r["conf_call"] or r["conf_put"]

                    def build_list(call_key, put_key):
                        """Build a ranked one-per-stock list using given score keys."""
                        lst = []
                        for r in results:
                            fc, fp = r[call_key], r[put_key]
                            if fc >= 45 and fc >= fp:
                                lst.append({**r, "side":"call", "final":fc})
                            elif fp >= 45 and fp > fc:
                                lst.append({**r, "side":"put", "final":fp})
                        lst.sort(key=lambda x: x["final"], reverse=True)
                        return lst[:SHOW_IDEAS]

                    # BLOCK 1: pure positioning (OI conviction only).
                    # BLOCK 2: balanced (movement + positioning blend).
                    # De-duplicate: unified excludes stocks already in positioning
                    # top-10 so the two blocks show genuinely different ideas.
                    positioning = build_list("pos_call", "pos_put")
                    pos_syms = {r["sym"] for r in positioning[:10]}
                    unified_full = build_list("bal_call", "bal_put")
                    # prefer stocks not in positioning; if not enough, fill from rest
                    unified_fresh = [r for r in unified_full if r["sym"] not in pos_syms]
                    unified_overlap = [r for r in unified_full if r["sym"] in pos_syms]
                    unified = (unified_fresh + unified_overlap)[:SHOW_IDEAS]

                    # keep bulls/bears for CSV compatibility
                    bulls = sorted([r for r in results if r["call_conv"]>=15],
                                   key=lambda x:x["call_conv"], reverse=True)[:TOP_N]
                    bears = sorted([r for r in results if r["put_conv"]>=15],
                                   key=lambda x:x["put_conv"], reverse=True)[:TOP_N]

                    # -- CONVICTION BRIDGE (shared with the orderflow tool) ----
                    # Write high-conviction stocks to a shared file so the
                    # combined dashboard can cross-reference positioning (WHICH
                    # stock) with orderflow proven levels (WHERE to enter).
                    # Only strong, directional convictions are exported.
                    try:
                        conv_export = {}
                        for r in results:
                            cc, pc = r.get("call_conv",0), r.get("put_conv",0)
                            # export ANY stock with a directional lean (was >=45,
                            # which left most stocks at 0). Lower bar = real data
                            # flows to the OI-confirmation everywhere.
                            if max(cc, pc) >= 15:
                                side = "call" if cc >= pc else "put"
                                conv_export[r["sym"]] = {
                                    "side": side,
                                    "conviction": round(max(cc, pc)),
                                    "direction": "up" if side=="call" else "down",
                                    "spot": r.get("spot", 0),
                                    "chg": r.get("chg", 0),
                                    "rs": r.get("rs", 0),
                                    "story": ("bulls positioned" if side=="call"
                                              else "bears positioned"),
                                }
                        bridge = {
                            "updated": t0.strftime("%H:%M:%S"),
                            "nifty_bias": (nifty_bias or {}).get("label", ""),
                            "stocks": conv_export,
                        }
                        Path("conviction_bridge.json").write_text(json.dumps(bridge))
                    except Exception as _e:
                        pass

                    # -- AUTO RULE FLAGS (for CSV backtest) -------------------
                    # Log which auto-rules passed each scan, so later you can ask:
                    # "when all 5 auto-rules were Y, what was the win rate?"
                    for r in results:
                        # figure the stronger side for this stock
                        side = "call" if r["final_call"] >= r["final_put"] else "put"
                        ib = (side == "call")
                        conv = r["call_conv"] if ib else r["put_conv"]
                        lp = r.get("lp_smooth", 0)
                        lp_dir = lp if ib else -lp
                        asc = max(align_sc(r,"call") if ib else 0,
                                  align_sc(r,"put") if not ib else 0)
                        lp_state = r.get("lp_state","NEW")
                        moving = (lp_state in ("SURGING","LIVE","BUILDING")) and lp_dir >= 8
                        # trigger distance
                        spot = r.get("spot",0) or 1
                        trig = (r.get("call_trigger",0) if ib else r.get("put_trigger",0)) or spot
                        near = abs((trig-spot)/spot*100) <= 0.8
                        r["rule_side"]   = side
                        r["rule_pos"]    = "Y" if conv>=40 else "N"
                        r["rule_align"]  = "Y" if asc>=2 else "N"
                        r["rule_moving"] = "Y" if moving else "N"
                        r["rule_trig"]   = "Y" if near else "N"
                        r["rule_confl"]  = "Y" if r.get("confluence") else "N"
                        r["rule_auto_count"] = sum(1 for x in
                            [conv>=40, asc>=2, moving, near, r.get("confluence")] if x)

                        # -- WHICH SIDE EACH SIGNAL FAVORS (same logic as UI) --
                        # So later you can check: did the stock actually move the
                        # way the signals leaned?
                        pcrV  = r.get("pcr", 1) or 1
                        painV = r.get("pain_dist", 0) or 0
                        rsV   = r.get("rs", 0) or 0
                        oi    = r.get("oi_dir", "=")
                        fav_pcr  = "call" if pcrV<0.7 else "put" if pcrV>1.3 else "neutral"
                        fav_oi   = "call" if oi=="P+" else "put" if oi=="C+" else "neutral"
                        fav_pain = "call" if painV<-0.5 else "put" if painV>0.5 else "neutral"
                        fav_rs   = "call" if rsV>0.5 else "put" if rsV<-0.5 else "neutral"
                        if moving:
                            fav_now = "call" if ib else "put"
                        elif lp_dir < -4:
                            fav_now = "put" if ib else "call"
                        else:
                            fav_now = "neutral"
                        favs = [fav_pcr, fav_oi, fav_pain, fav_rs, fav_now]
                        r["fav_pcr"]  = fav_pcr
                        r["fav_oi"]   = fav_oi
                        r["fav_pain"] = fav_pain
                        r["fav_rs"]   = fav_rs
                        r["fav_now"]  = fav_now
                        r["fav_call_count"] = favs.count("call")
                        r["fav_put_count"]  = favs.count("put")
                        # does the majority agree with the side we picked?
                        want = "call" if ib else "put"
                        r["fav_agree"] = "Y" if favs.count(want) > favs.count(
                            "put" if ib else "call") else "N"

                    save_csv(results, t0)

                    # -- SEARCHABLE: every scanned stock, with its stronger side
                    # This lets the UI search box pull up ANY F&O stock's full
                    # card, not just the top 10. Each entry carries the same
                    # fields a unified card needs, tagged with the better side.
                    all_stocks = []
                    for r in results:
                        fc, fp = r.get("final_call",0), r.get("final_put",0)
                        side = "call" if fc >= fp else "put"
                        all_stocks.append({**r, "side":side,
                                           "final": fc if side=="call" else fp})
                    all_stocks.sort(key=lambda x: x["sym"])

                    # -- SECTOR HEATMAP -------------------------------------
                    # Group the scanned F&O stocks by sector. For each sector:
                    #   avg move  = average day change of its stocks (buyer/seller bias)
                    #   up/down   = how many stocks green vs red (breadth)
                    #   stocks    = the sector's stocks, ranked by POSITIONING score
                    # This reveals where the real buying/selling pressure is:
                    # a sector green across most of its stocks = broad buying.
                    sector_map = {}
                    for r in results:
                        sec = _sector_of(r["sym"])
                        sector_map.setdefault(sec, []).append(r)
                    heatmap = []
                    for sec, members in sector_map.items():
                        chgs = [fnum_safe(m.get("chg")) for m in members]
                        avg  = round(sum(chgs)/len(chgs), 2) if chgs else 0
                        ups  = sum(1 for c in chgs if c > 0.1)
                        dns  = sum(1 for c in chgs if c < -0.1)
                        # rank this sector's stocks by positioning score (pos block)
                        ranked = sorted(members,
                            key=lambda x: max(x.get("pos_call",0), x.get("pos_put",0)),
                            reverse=True)
                        stocks = []
                        for m in ranked:
                            pc, pp = m.get("pos_call",0), m.get("pos_put",0)
                            side = "call" if pc >= pp else "put"
                            stocks.append({
                                "sym": m["sym"], "chg": fnum_safe(m.get("chg")),
                                "spot": m.get("spot",0), "rs": m.get("rs",0),
                                "side": side, "final": max(pc, pp),
                                "call_conv": m.get("call_conv",0), "put_conv": m.get("put_conv",0),
                                "lp_smooth": m.get("lp_smooth",0), "lp_state": m.get("lp_state","NEW"),
                                "confluence": m.get("confluence",False),
                                "pcr": m.get("pcr",1), "oi_dir": m.get("oi_dir","="),
                                "max_pain": m.get("max_pain",0), "pain_dist": m.get("pain_dist",0),
                                "atm_iv": m.get("atm_iv",0), "expiry": m.get("expiry","?"),
                                "call_trigger": m.get("call_trigger",0), "put_trigger": m.get("put_trigger",0),
                                "call_trig_src": m.get("call_trig_src",""), "put_trig_src": m.get("put_trig_src",""),
                                "est_premium": m.get("est_premium",0),
                                "call_target": m.get("call_target",0), "put_target": m.get("put_target",0),
                                "atm_strike": m.get("atm_strike",0),
                            })
                        heatmap.append({
                            "name": sec, "avg": avg, "count": len(members),
                            "ups": ups, "downs": dns, "stocks": stocks[:15],
                            # bias: broad up = buyers, broad down = sellers
                            "bias": ("buyers" if avg > 0.2 and ups > dns else
                                     "sellers" if avg < -0.2 and dns > ups else "mixed"),
                        })
                    heatmap.sort(key=lambda x: x["avg"], reverse=True)

                    n_conf = sum(1 for u in unified if u.get("confluence"))
                    print(f"done | {len(results)} scanned | "
                          f"{len(unified)} ideas | {n_conf} confluence | {elapsed}s")
                    with _lock:
                        _state.update({
                            "unified": unified, "positioning": positioning,
                            "all_stocks": all_stocks, "heatmap": heatmap,
                            "bulls": bulls, "bears": bears, "momentum": [],
                            "scan_num": scan_n, "scan_time": t0.strftime("%H:%M:%S"),
                            "next_scan_in": wait, "nifty": nifty_str,
                            "total_stocks": len(results), "top_movers": movers,
                            "nifty_bias": nifty_bias, "sectors": sectors, "nifty_chg": nifty_chg,
                        })
                else:
                    print("no data"); wait = 60

                for s in range(wait, 0, -1):
                    with _lock: _state["next_scan_in"] = s
                    time.sleep(1)

            except KeyboardInterrupt: raise
            except Exception as e:
                print(f"\n  Error: {e} | retry 30s")
                time.sleep(30)

# -- HTML ----------------------------------------------------------------------

# -- SERVER --------------------------------------------------------------------
_UI_FILE = Path(__file__).parent / "scanner_ui.html"

def _read_ui():
    """Read the HTML UI file from disk (so you can edit it without restarting)."""
    try:
        return _UI_FILE.read_bytes()
    except FileNotFoundError:
        return b"<h1>scanner_ui.html not found</h1><p>Place it next to nse_scanner.py</p>"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        # -- /data - full payload for the setups page ---------------------
        if self.path == "/data":
            with _lock:
                s = _state
                p = json.dumps({k: s[k] for k in
                    ("unified","positioning","all_stocks","heatmap",
                     "bulls","bears","momentum","scan_num","scan_time",
                     "next_scan_in","nifty","nifty_chg","total_stocks",
                     "top_movers","nifty_bias","sectors")})
            self._json(p)
        # -- /sectors - lighter payload for heatmap tab -------------------
        elif self.path == "/sectors":
            with _lock:
                s = _state
                p = json.dumps({"heatmap": s["heatmap"],
                                "sectors": s["sectors"],
                                "scan_time": s["scan_time"]})
            self._json(p)
        # -- /nifty-oi - NIFTY bias only for OI tab -----------------------
        elif self.path == "/nifty-oi":
            with _lock:
                p = json.dumps({"nifty_bias": _state["nifty_bias"],
                                "nifty":      _state["nifty"],
                                "nifty_chg":  _state["nifty_chg"],
                                "scan_time":  _state["scan_time"]})
            self._json(p)
        # -- everything else -> serve the UI HTML file ---------------------
        else:
            html = _read_ui()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    def _json(self, payload):
        data = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

# -- MAIN ---------------------------------------------------------------------
def main():
    print("\n" + "="*60)
    print("  NSE Option Scanner  -  Focus Mode (Parallel)")
    print("="*60)
    # When launched by the cockpit (start.py), run headless: no blocking
    # input prompt, no browser tab (the cockpit opens the unified dashboard).
    launched = os.environ.get("COCKPIT_LAUNCH") == "1"
    now = datetime.datetime.now()
    if not (MARKET_START <= (now.hour, now.minute) < MARKET_END) and not launched:
        print(f"\n  Time: {now:%H:%M:%S} | Market: 9:30 AM - 3:05 PM IST")
        print(f"  Outside market hours - data may be stale.")
        print(f"  Press Enter to run anyway, Ctrl+C to exit.\n")
        try: input()
        except KeyboardInterrupt: return
    DATA_DIR.mkdir(exist_ok=True)
    t  = threading.Thread(target=scanner_main, daemon=True); t.start()
    sv = HTTPServer(("localhost", PORT), Handler)
    st = threading.Thread(target=sv.serve_forever, daemon=True); st.start()
    url = f"http://localhost:{PORT}"
    print(f"\n  Web UI  : {url}")
    print(f"  Data    : ./scan_data/")
    if not launched:
        print(f"  Opening browser...")
        time.sleep(2); webbrowser.open(url)
    else:
        print(f"  (headless - conviction feeding the cockpit)")
    print(f"  Running. Ctrl+C to stop.\n")
    try:
        while t.is_alive(): time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Stopped."); sv.shutdown()

if __name__ == "__main__":
    main()