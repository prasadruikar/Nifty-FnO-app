"""
upstox_oi.py  -  Upstox option-chain data in NSE format.
=====================================================================
Kills the NSE 403 problem. StockRanker's score() engine expects NSE's
exact shape (records.data with CE/PE objects). This adapter fetches the
same data from Upstox's authenticated option-chain API and reshapes it
so score() works UNCHANGED. Reliable, no scraping, no 403s.

Upstox response per strike:
  { "expiry","pcr","strike_price","underlying_spot_price",
    "call_options":{"market_data":{ltp,volume,oi,prev_oi,bid_*,ask_*}, "option_greeks":{iv,...}},
    "put_options":{ ...same... } }

We map -> NSE shape:
  { "records": { "underlyingValue": spot,
                 "data": [ {"strikePrice", "expiryDate",
                            "CE":{openInterest,changeinOpenInterest,totalTradedVolume,impliedVolatility},
                            "PE":{...}} ] } }
Where changeinOpenInterest = oi - prev_oi (Upstox gives prev_oi).
"""

import time
import datetime
import requests
from pathlib import Path

OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"


class UpstoxOI:
    def __init__(self, access_token, instrument_map):
        """
        instrument_map = {SYMBOL: instrument_key}  (equity keys, e.g.
                         "RELIANCE": "NSE_EQ|INE002A01018")
        For option chain, Upstox needs the UNDERLYING key. For F&O stocks
        the equity key works as the underlying_key for the chain call.
        """
        self.token = access_token
        self.instrument_map = instrument_map
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })
        self._expiry_cache = {}   # {sym: (expiry_str, fetched_date)}

    # -- nearest expiry per underlying (cached daily) --
    def _nearest_expiry(self, sym, key):
        today = datetime.date.today().isoformat()
        cached = self._expiry_cache.get(sym)
        if cached and cached[1] == today:
            return cached[0]
        try:
            r = self.session.get(OPTION_CONTRACT_URL,
                                 params={"instrument_key": key}, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", []) or []
                exps = sorted({row.get("expiry") for row in data if row.get("expiry")})
                if exps:
                    # nearest future expiry
                    fut = [e for e in exps if e >= today] or exps
                    nearest = fut[0]
                    self._expiry_cache[sym] = (nearest, today)
                    return nearest
        except Exception:
            pass
        return None

    # -- fetch one stock's chain, reshaped to NSE format --
    def option_chain(self, sym):
        key = self.instrument_map.get(sym)
        if not key:
            return None
        expiry = self._nearest_expiry(sym, key)
        params = {"instrument_key": key}
        if expiry:
            params["expiry_date"] = expiry
        try:
            r = self.session.get(OPTION_CHAIN_URL, params=params, timeout=10)
            if r.status_code != 200:
                return None
            rows = r.json().get("data", []) or []
        except Exception:
            return None
        if not rows:
            return None

        spot = rows[0].get("underlying_spot_price", 0)
        data = []
        for row in rows:
            ce = row.get("call_options", {}) or {}
            pe = row.get("put_options", {}) or {}
            ce_md = ce.get("market_data", {}) or {}
            pe_md = pe.get("market_data", {}) or {}
            ce_gk = ce.get("option_greeks", {}) or {}
            pe_gk = pe.get("option_greeks", {}) or {}

            def chg_oi(md):
                oi = md.get("oi", 0) or 0
                prev = md.get("prev_oi", 0) or 0
                return int(oi) - int(prev)

            data.append({
                "strikePrice": row.get("strike_price", 0),
                "expiryDate": row.get("expiry", ""),
                "CE": {
                    "openInterest": int(ce_md.get("oi", 0) or 0),
                    "changeinOpenInterest": chg_oi(ce_md),
                    "totalTradedVolume": int(ce_md.get("volume", 0) or 0),
                    "impliedVolatility": float(ce_gk.get("iv", 0) or 0),
                    "lastPrice": float(ce_md.get("ltp", 0) or 0),
                },
                "PE": {
                    "openInterest": int(pe_md.get("oi", 0) or 0),
                    "changeinOpenInterest": chg_oi(pe_md),
                    "totalTradedVolume": int(pe_md.get("volume", 0) or 0),
                    "impliedVolatility": float(pe_gk.get("iv", 0) or 0),
                    "lastPrice": float(pe_md.get("ltp", 0) or 0),
                },
            })
        return {"records": {"underlyingValue": spot, "data": data}}