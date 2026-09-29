"""
analyze_scans.py  -  Honest pattern analysis of your scanner's CSV data
=======================================================================
RUN:  python analyze_scans.py

Reads every scan_data/scan_*.csv and reports the SHAPE of your signals.

IMPORTANT - READ THIS FIRST:
  This does NOT compute win-rates. Your CSV has no outcome column
  (trigger_hit / result), so any win-rate would be fabricated.
  What this DOES do is describe what your scanner is actually doing:
    - how often each state/signal fires
    - how stable vs flickery your top ideas are
    - how confluence relates to conviction and live pressure
    - whether the "favor" signals tend to agree or conflict
  Treat every number as a DESCRIPTION, not a prediction. And note the
  sample size printed next to each - 3 days is a tiny sample; nothing
  here is statistically reliable yet. Keep logging for 2-3 weeks.

  To get REAL win-rates later, you must add outcome columns yourself
  (see the note at the very end of the output).
"""

import csv, glob, os, sys
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path("scan_data")

def load_rows():
    files = sorted(glob.glob(str(DATA_DIR / "scan_*.csv")))
    if not files:
        # also try current directory in case script sits next to the csvs
        files = sorted(glob.glob("scan_*.csv"))
    if not files:
        print("No scan_*.csv files found.")
        print(f"Looked in: {DATA_DIR.resolve()} and current folder.")
        print("Run this script from your project folder (where scan_data/ lives).")
        sys.exit(1)
    rows = []
    for fp in files:
        with open(fp, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["_file"] = os.path.basename(fp)
                rows.append(r)
    return rows, files

def fnum(v, d=0.0):
    try: return float(v)
    except: return d

def pct(n, total):
    return f"{(100.0*n/total):.0f}%" if total else "--"

def bar(frac, width=24):
    fill = int(round(frac*width))
    return "#"*fill + "."*(width-fill)

def section(title):
    print("\n" + "="*64)
    print(f"  {title}")
    print("="*64)

def main():
    rows, files = load_rows()
    n = len(rows)

    print("\n" + "#"*64)
    print("#  SCANNER SIGNAL ANALYSIS  (descriptive, not predictive)")
    print("#"*64)
    print(f"\nFiles read : {len(files)}")
    for fp in files:
        print(f"   - {os.path.basename(fp)}")
    print(f"Total rows : {n}   (each row = one stock in one scan)")
    days = len({r.get('date','') for r in rows})
    scans = len({(r.get('date',''), r.get('time','')) for r in rows})
    print(f"Days       : {days}")
    print(f"Scans      : {scans}")
    if days < 10:
        print("\n  *** SMALL SAMPLE WARNING ***")
        print(f"  Only {days} day(s) of data. Everything below is a rough")
        print("  description, NOT a reliable statistic. Keep logging.")

    # -- 1. Live-pressure state distribution --
    section("1. LIVE-PRESSURE STATE  (how often each fires)")
    lp = Counter(r.get("lp_state","") or "NEW" for r in rows)
    for state in ["SURGING","LIVE","BUILDING","FLAT","FADING","NEW"]:
        c = lp.get(state,0)
        print(f"  {state:9} {c:6}  {bar(c/n)}  {pct(c,n)}")
    live = lp.get("SURGING",0)+lp.get("LIVE",0)
    print(f"\n  Actively moving (SURGING+LIVE): {live} rows = {pct(live,n)} of all readings.")
    print("  If this is tiny, most rows are stocks NOT moving - which is normal.")

    # -- 2. Confluence --
    section("2. CONFLUENCE  (positioned AND moving together)")
    conf = sum(1 for r in rows if str(r.get("confluence","")).lower()=="true")
    print(f"  Confluence rows: {conf} = {pct(conf,n)} of all readings.")
    print("  Confluence is meant to be RARE - that's the point. A few % is healthy.")

    # -- 3. fav_agree: do signals agree with the chosen side? --
    section("3. SIGNAL AGREEMENT  (do the 5 favors line up?)")
    agree = Counter(r.get("fav_agree","") for r in rows)
    y = agree.get("Y",0); nn = agree.get("N",0)
    print(f"  Majority AGREES with chosen side : {y} = {pct(y,n)}")
    print(f"  Majority CONFLICTS               : {nn} = {pct(nn,n)}")
    # distribution of call-favor counts
    section("3b. HOW CLEAN ARE THE SETUPS  (fav_call_count spread)")
    cc = Counter(int(fnum(r.get("fav_call_count",0))) for r in rows)
    for k in range(6):
        print(f"  {k}/5 favor CALL : {cc.get(k,0):6}  {bar(cc.get(k,0)/n)}  {pct(cc.get(k,0),n)}")
    print("  5/0 or 0/5 = clean agreement.  3/2 or 2/3 = conflicted (like your GODREJCP).")

    # -- 4. Conviction distribution for the top ideas --
    section("4. CONVICTION TIERS  (only rows that made a real idea)")
    ideas = [r for r in rows
             if fnum(r.get("call_conv"))>=15 or fnum(r.get("put_conv"))>=15]
    print(f"  Rows with a tradeable conviction (>=15): {len(ideas)} = {pct(len(ideas),n)}")
    tiers = Counter()
    for r in ideas:
        c = max(fnum(r.get("call_conv")), fnum(r.get("put_conv")))
        tiers["SOLID (70+)" if c>=70 else "BUILDING (40-69)" if c>=40 else "FORMING (15-39)"] += 1
    for t in ["SOLID (70+)","BUILDING (40-69)","FORMING (15-39)"]:
        c=tiers.get(t,0); tot=len(ideas) or 1
        print(f"  {t:18} {c:6}  {bar(c/tot)}  {pct(c,tot)}")

    # -- 5. Stability: how often do top symbols persist across scans? --
    section("5. STABILITY  (do top ideas persist or flicker?)")
    # a symbol is a "strong idea" in a scan if conviction>=40 either side
    per_scan = defaultdict(set)
    for r in rows:
        c = max(fnum(r.get("call_conv")), fnum(r.get("put_conv")))
        if c>=40:
            per_scan[(r.get("date"),r.get("time"))].add(r.get("symbol"))
    scan_keys = sorted(per_scan.keys())
    if len(scan_keys)>=2:
        carry=[]
        for i in range(1,len(scan_keys)):
            prev=per_scan[scan_keys[i-1]]; cur=per_scan[scan_keys[i]]
            if prev:
                carry.append(len(prev&cur)/len(prev))
        if carry:
            avg=sum(carry)/len(carry)
            print(f"  Avg carryover of strong ideas scan-to-scan: {avg*100:.0f}%")
            print("  (What fraction of scan N's strong ideas are still strong in N+1.)")
            print("  Higher = more stable. Decay engine should keep this reasonably high.")
    else:
        print("  Need at least 2 scans to measure stability.")

    # -- 6. Most frequently surfaced symbols --
    section("6. MOST-SURFACED SYMBOLS  (appeared strong most often)")
    strong = Counter()
    for r in rows:
        c = max(fnum(r.get("call_conv")), fnum(r.get("put_conv")))
        if c>=40: strong[r.get("symbol")]+=1
    for sym,c in strong.most_common(15):
        print(f"  {sym:14} {c:4} scans strong")

    # -- Final honest note --
    section("HOW TO GET REAL WIN-RATES (the missing piece)")
    print("""  This report describes signals only. To measure if they WORK, add
  these columns to each row you actually acted on (in Excel, after market):

     trigger_hit    Y / N        did price cross the trigger?
     entry_premium  number       what you paid (or would have)
     exit_premium   number       what it was worth at your exit
     result         WIN/LOSS/NO_ENTRY

  Do that for ~2-3 weeks. Then a REAL backtest can answer:
     "When lp_state=LIVE and fav_agree=Y and confluence=True,
      what % were WINS?"  <- that number is your actual edge.

  Until then: paper trade, log honestly, trust nothing.
""")

if __name__ == "__main__":
    main()
