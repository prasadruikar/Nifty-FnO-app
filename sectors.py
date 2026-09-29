"""
sectors.py  -  Sector heatmap + stock treemap data from Upstox.
=====================================================================
Two things for the heatmap page:
  1. SECTOR PERFORMANCE - fetch the NIFTY sectoral index % changes (Bank, IT,
     Auto, Pharma, etc.) via Upstox market-quote. One call, all sectors.
  2. STOCK TREEMAP - group all F&O stocks by sector, each sized by |% change|
     so the biggest movers get the biggest tiles.

Sector indices are Upstox instrument keys like "NSE_INDEX|Nifty Bank".
"""

import requests

QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"

# NIFTY sectoral indices - instrument key -> display name
SECTOR_INDICES = {
    "NSE_INDEX|Nifty Bank":            "Bank",
    "NSE_INDEX|Nifty IT":              "IT",
    "NSE_INDEX|Nifty Auto":            "Auto",
    "NSE_INDEX|Nifty Pharma":          "Pharma",
    "NSE_INDEX|Nifty FMCG":            "FMCG",
    "NSE_INDEX|Nifty Metal":           "Metal",
    "NSE_INDEX|Nifty Energy":          "Energy",
    "NSE_INDEX|Nifty Realty":          "Realty",
    "NSE_INDEX|Nifty PSU Bank":        "PSU Bank",
    "NSE_INDEX|Nifty Pvt Bank":        "Pvt Bank",
    "NSE_INDEX|Nifty Fin Service":     "FinServ",
    "NSE_INDEX|Nifty Media":           "Media",
    "NSE_INDEX|Nifty Infra":           "Infra",
    "NSE_INDEX|Nifty Consumer Durables": "ConsDur",
    "NSE_INDEX|Nifty Oil And Gas":     "Oil & Gas",
}

# Map each F&O stock to a sector (for the treemap grouping). Best-effort;
# stocks not listed fall under "Other".
STOCK_SECTOR = {
    # Bank / FinServ
    "HDFCBANK":"Bank","ICICIBANK":"Bank","AXISBANK":"Bank","KOTAKBANK":"Bank",
    "SBIN":"Bank","INDUSINDBK":"Bank","BANKBARODA":"PSU Bank","PNB":"PSU Bank",
    "CANBK":"PSU Bank","FEDERALBNK":"Bank","IDFCFIRSTB":"Bank","AUBANK":"Bank",
    "BAJFINANCE":"FinServ","BAJAJFINSV":"FinServ","SHRIRAMFIN":"FinServ",
    "CHOLAFIN":"FinServ","SBILIFE":"FinServ","HDFCLIFE":"FinServ","ICICIGI":"FinServ",
    "ICICIPRULI":"FinServ","LICHSGFIN":"FinServ","MANAPPURAM":"FinServ","MUTHOOTFIN":"FinServ",
    "PFC":"FinServ","RECLTD":"FinServ","IEX":"FinServ","BSE":"FinServ","MCX":"FinServ",
    # IT
    "TCS":"IT","INFY":"IT","WIPRO":"IT","HCLTECH":"IT","TECHM":"IT","LTIM":"IT",
    "PERSISTENT":"IT","COFORGE":"IT","MPHASIS":"IT","OFSS":"IT","LTTS":"IT",
    # Auto
    "MARUTI":"Auto","TATAMOTORS":"Auto","M&M":"Auto","BAJAJ-AUTO":"Auto","EICHERMOT":"Auto",
    "HEROMOTOCO":"Auto","TVSMOTOR":"Auto","ASHOKLEY":"Auto","BHARATFORG":"Auto",
    "MOTHERSON":"Auto","BOSCHLTD":"Auto","BALKRISIND":"Auto","MRF":"Auto","APOLLOTYRE":"Auto",
    "EXIDEIND":"Auto","TMPV":"Auto","TIINDIA":"Auto",
    # Pharma
    "SUNPHARMA":"Pharma","DRREDDY":"Pharma","CIPLA":"Pharma","DIVISLAB":"Pharma",
    "AUROPHARMA":"Pharma","LUPIN":"Pharma","BIOCON":"Pharma","ALKEM":"Pharma",
    "TORNTPHARM":"Pharma","ZYDUSLIFE":"Pharma","GLENMARK":"Pharma","LAURUSLABS":"Pharma",
    "APOLLOHOSP":"Pharma","MAXHEALTH":"Pharma","FORTIS":"Pharma","SYNGENE":"Pharma",
    # FMCG
    "HINDUNILVR":"FMCG","ITC":"FMCG","NESTLEIND":"FMCG","BRITANNIA":"FMCG",
    "TATACONSUM":"FMCG","DABUR":"FMCG","GODREJCP":"FMCG","MARICO":"FMCG",
    "COLPAL":"FMCG","VBL":"FMCG","UBL":"FMCG","PATANJALI":"FMCG","NAUKRI":"FMCG",
    # Metal
    "TATASTEEL":"Metal","JSWSTEEL":"Metal","HINDALCO":"Metal","VEDL":"Metal",
    "JINDALSTEL":"Metal","SAIL":"Metal","NMDC":"Metal","NATIONALUM":"Metal",
    "HINDZINC":"Metal","APLAPOLLO":"Metal","JSWENERGY":"Energy",
    # Energy / Oil & Gas
    "RELIANCE":"Energy","ONGC":"Oil & Gas","BPCL":"Oil & Gas","IOC":"Oil & Gas",
    "GAIL":"Oil & Gas","HINDPETRO":"Oil & Gas","PETRONET":"Oil & Gas","IGL":"Oil & Gas",
    "OIL":"Oil & Gas","NTPC":"Energy","POWERGRID":"Energy","TATAPOWER":"Energy",
    "ADANIGREEN":"Energy","ADANIENSOL":"Energy","ADANIPOWER":"Energy","NHPC":"Energy",
    "COALINDIA":"Energy",
    # Realty / Infra
    "DLF":"Realty","GODREJPROP":"Realty","OBEROIRLTY":"Realty","LODHA":"Realty",
    "PRESTIGE":"Realty","LT":"Infra","ADANIPORTS":"Infra","GMRAIRPORT":"Infra",
    "IRB":"Infra","NBCC":"Infra","RVNL":"Infra","IRCON":"Infra","NCC":"Infra",
    # Media / Telecom / Cons
    "BHARTIARTL":"Telecom","IDEA":"Telecom","INDUSTOWER":"Telecom","TATACOMM":"Telecom",
    "ZEEL":"Media","SUNTV":"Media","PVRINOX":"Media","NAM-INDIA":"FinServ",
    "TITAN":"ConsDur","HAVELLS":"ConsDur","VOLTAS":"ConsDur","DIXON":"ConsDur",
    "CROMPTON":"ConsDur","BLUESTARCO":"ConsDur","KALYANKJIL":"ConsDur","POLYCAB":"ConsDur",
    # Cement / Chem / other big F&O
    "ULTRACEMCO":"Cement","SHREECEM":"Cement","AMBUJACEM":"Cement","ACC":"Cement",
    "DALBHARAT":"Cement","GRASIM":"Cement","PIDILITIND":"Chemical","SRF":"Chemical",
    "UPL":"Chemical","PIIND":"Chemical","AARTIIND":"Chemical","DEEPAKNTR":"Chemical",
    "ADANIENT":"Conglomerate","BEL":"Defence","HAL":"Defence","BDL":"Defence",
    "MAZDOCK":"Defence","COCHINSHIP":"Defence","BHEL":"Capital Goods","SIEMENS":"Capital Goods",
    "ABB":"Capital Goods","CGPOWER":"Capital Goods","POWERINDIA":"Capital Goods",
    "BHARATFORG":"Capital Goods","CUMMINSIND":"Capital Goods","THERMAX":"Capital Goods",
    "IRCTC":"Other","IRFC":"FinServ","ZOMATO":"Other","PAYTM":"Other","NYKAA":"Other",
    "POLICYBZR":"FinServ","DMART":"FMCG","TRENT":"ConsDur","INDIGO":"Other",
}


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


class Sectors:
    def __init__(self, access_token):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json",
                               "Authorization": f"Bearer {access_token}"})

    def fetch_sector_perf(self):
        """Return [{name, chg}] for each NIFTY sectoral index, sorted desc."""
        keys = list(SECTOR_INDICES.keys())
        out = []
        try:
            r = self.s.get(QUOTES_URL,
                           params={"instrument_key": ",".join(keys)}, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", {}) or {}
                for k, q in data.items():
                    # match returned key back to a sector name
                    name = None
                    for ik, nm in SECTOR_INDICES.items():
                        if ik.split("|")[-1].replace(" ", "") in k.replace(" ", ""):
                            name = nm; break
                    if not name:
                        continue
                    ltp = _f(q.get("last_price"))
                    # ohlc.close is TODAY's running close (= ltp) -> gives 0%.
                    # net_change = change vs PREVIOUS close, the reliable field.
                    net = _f(q.get("net_change"))
                    ohlc_close = _f((q.get("ohlc", {}) or {}).get("close"))
                    if net and ltp:
                        prev = ltp - net
                    elif ohlc_close and abs(ohlc_close - ltp) > 1e-6:
                        prev = ohlc_close
                    else:
                        prev = 0.0
                    chg = round((ltp - prev) / prev * 100, 2) if prev else 0.0
                    out.append({"name": name, "chg": chg, "ltp": round(ltp, 1)})
        except Exception:
            pass
        out.sort(key=lambda x: x["chg"], reverse=True)
        return out

    @staticmethod
    def build_treemap(session_ranked, all_day_chg):
        """
        Build treemap tiles for ALL F&O stocks, grouped by sector, each sized
        by |% change|. all_day_chg = {sym: day_chg%}.
        Returns [{sector, stocks:[{sym, chg, sector}]}] sorted by sector move.
        """
        by_sector = {}
        for sym, chg in all_day_chg.items():
            sec = STOCK_SECTOR.get(sym, "Other")
            by_sector.setdefault(sec, []).append({
                "sym": sym, "chg": round(chg, 2), "sector": sec,
                "size": abs(chg) or 0.1,   # tile size = |move|, floor so visible
            })
        # sort stocks within each sector by |move| desc; sectors by avg move
        sectors = []
        for sec, stocks in by_sector.items():
            stocks.sort(key=lambda x: x["size"], reverse=True)
            avg = round(sum(s["chg"] for s in stocks) / len(stocks), 2) if stocks else 0
            sectors.append({"sector": sec, "avg": avg, "stocks": stocks})
        sectors.sort(key=lambda x: x["avg"], reverse=True)
        return sectors